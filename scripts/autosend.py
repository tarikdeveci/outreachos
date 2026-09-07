"""Onaylı taslakları günlük küçük partiler halinde gönderen kat — VARSAYILAN KAPALI.

Neden var: ~238 taslak birikti ve `deliverability` freni backlog>12 iken yeni üretimi
durduruyor. Kullanıcı taslakları elle gönderemediği için sistem kilitleniyordu. Bu kat
backlog'u güvenli hızda boşaltır; boşaldıkça üretim kendiliğinden yeniden açılır.

NEDEN TAM OTOMATİK DEĞİL — ölçülen gerçek: 2026-08-21'de elle denetlenen ~14 taslağın
3'ünde profilde karşılığı olmayan iddia vardı (%20) ve o taslaklar ZATEN drafting.verify'dan
geçmişti. Körlemesine gönderim, itibara bounce'tan çok daha pahalıya mal olur. O yüzden:

  VETO PENCERESİ — bugün ✅GUVENLI bulunanlar KUYRUĞA alınır ve raporda listelenir;
  gönderim BİR SONRAKİ run'da olur. Kullanıcı fikrini değiştirirse taslağı Gmail'den
  siler: taslak artık "canlı" olmadığı için gönderilmez. Veto = silmek, ekstra arayüz yok.

Bounce koruması (kullanıcının asıl derdi):
  1. Gönderimden HEMEN ÖNCE MX yeniden doğrulanır — adres kuyrukta beklerken ölmüş olabilir.
  2. Sert günlük tavan (AUTO_SEND_CAP, varsayılan 5) — hacim sıçraması spam sinyalidir.
  3. hard-bounce KRİTİK ise hiç gönderilmez; İZLEME'de tavan yarıya iner.
  4. Yalnızca audit'in ✅GUVENLI dediği (mükerrer değil + içerik doğrulanmış) taslaklar.

Güvenlik: bu kat AUTO_SEND=1 olmadan HİÇBİR ŞEY göndermez (varsayılan "0"). Kapalıyken
kuyruk yine de hesaplanır ve raporda "gönderilebilir" olarak gösterilir — kullanıcı önce
kararları görür, sonra isterse açar.

Saf çekirdek (gate/pick/kuyruk mantığı) ağsız; dosya sonundaki __main__ kendini test eder:
`python scripts/autosend.py`
"""
from __future__ import annotations

QUEUE_KEY = "autosend_queue"
SENT_KEY = "autosend_sent"


def pick(audit_results: list, cap: int, exclude_ids: set | None = None) -> list:
    """audit çıktısından gönderilmeye aday olanlar: SADECE ✅GUVENLI, en fazla `cap` tane.

    MUKERRER/ICERIK/INCELE/BEKLEMEDE hiçbir koşulda seçilmez — şüphe varsa gönderme.
    exclude_ids: zaten kuyrukta olanlar (aynı taslağı iki kez kuyruğa almayalım).
    """
    out = []
    skip = exclude_ids or set()
    for r in audit_results:
        if r.get("verdict") != "GUVENLI" or r.get("id") in skip:
            continue
        if not r.get("to") or "@" not in r.get("to", ""):
            continue
        out.append({"id": r["id"], "to": r["to"], "domain": r.get("domain", ""),
                    "company": r.get("company", ""), "subject": r.get("subject", "")})
        if len(out) >= cap:
            break
    return out


def gate(health: dict, enabled: bool, cap: int) -> tuple:
    """(izin, efektif_tavan, sebepler) — bugün kaç mail gönderilebilir.

    KRİTİK bounce  → 0 (fren; deliverability.circuit ile aynı mantık).
    İZLEME         → tavan yarıya iner (yavaşla ama durma).
    Kapalıysa      → 0 ama sebep 'kapalı' (rapor bunu 'gönderilebilirdi' diye gösterir).
    """
    reasons: list[str] = []
    if not enabled:
        return False, 0, ["AUTO_SEND kapalı (varsayılan) — kuyruk sadece gösteriliyor"]
    state = health.get("state")
    if state == "CRITICAL":
        return False, 0, [f"hard-bounce %{health.get('bounce_rate', 0) * 100:.1f} kritik — "
                          "otomatik gönderim durduruldu"]
    if state == "WATCH":
        cap = max(1, cap // 2)
        reasons.append(f"bounce izleme seviyesinde — günlük tavan {cap}'e indirildi")
    return True, cap, reasons


def enqueue(state: dict, picks: list, day: str) -> int:
    """Bugün onaylananları veto penceresine alır. Zaten kuyrukta olan id tekrar eklenmez."""
    q = state.setdefault(QUEUE_KEY, [])
    have = {i.get("id") for i in q}
    added = 0
    for p in picks:
        if p["id"] in have:
            continue
        q.append({**p, "queued": day})
        added += 1
    return added


def due(state: dict, day: str) -> list:
    """Veto penceresi dolmuş kuyruk öğeleri: BUGÜNDEN ÖNCE kuyruğa girmiş olanlar.
    Aynı gün kuyruğa girip aynı gün gitmez — kullanıcının görüp veto etme şansı olsun."""
    return [i for i in state.get(QUEUE_KEY, []) if i.get("queued", "") < day]


def drop(state: dict, ids: set) -> None:
    """Kuyruktan çıkar (gönderildi, veto edildi ya da artık geçersiz)."""
    q = state.get(QUEUE_KEY, [])
    state[QUEUE_KEY] = [i for i in q if i.get("id") not in ids]


def record_sent(state: dict, item: dict, day: str, message_id: str) -> None:
    """Gönderileni kalıcı kayda geçir + o firmayı contacted'a işle (bir daha taslak açılmasın)."""
    log = state.setdefault(SENT_KEY, [])
    log.append({"date": day, "to": item["to"], "company": item.get("company", ""),
                "message_id": message_id})
    firma = (item.get("company") or item["to"].split("@")[-1].split(".")[0]).lower()
    cc = state.setdefault("companies_already_contacted", {})
    if firma not in cc:
        cc[firma] = {"date": day, "channel": "autosend_vetted", "email": item["to"],
                     "draft_id": item.get("id"), "last_reply_seen": None}


def summary_lines(due_items: list, sent: list, queued: list, allowed: bool,
                  reasons: list, cap: int) -> list:
    """Rapor bloğu — ne gitti, ne kuyrukta, veto nasıl yapılır."""
    lines: list[str] = []
    if sent:
        lines.append(f"📤 Otomatik gönderildi ({len(sent)}/{cap}):")
        lines += [f"    • {s['company']} ({s['to']})" for s in sent]
    elif due_items and not allowed:
        lines.append(f"⏸ {len(due_items)} onaylı taslak gönderilmeyi bekliyor ama gönderilmedi:")
        lines += [f"    - {r}" for r in reasons]
    if queued:
        lines.append(f"🕐 Yarın gönderilecek ({len(queued)}) — istemediğini Gmail'den SİL, gitmez:")
        lines += [f"    • {q['company']} ({q['to']}) — {q['subject'][:60]}" for q in queued]
    return lines


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    res = [
        {"id": "a", "verdict": "GUVENLI", "to": "hi@a.com", "domain": "a.com", "company": "a", "subject": "s"},
        {"id": "b", "verdict": "MUKERRER", "to": "hi@b.com", "domain": "b.com", "company": "b", "subject": "s"},
        {"id": "c", "verdict": "ICERIK", "to": "hi@c.com", "domain": "c.com", "company": "c", "subject": "s"},
        {"id": "d", "verdict": "GUVENLI", "to": "hi@d.com", "domain": "d.com", "company": "d", "subject": "s"},
        {"id": "e", "verdict": "INCELE", "to": "hi@e.com", "domain": "e.com", "company": "e", "subject": "s"},
        {"id": "f", "verdict": "BEKLEMEDE", "to": "hi@f.com", "domain": "f.com", "company": "f", "subject": "s"},
        {"id": "g", "verdict": "GUVENLI", "to": "hi@g.com", "domain": "g.com", "company": "g", "subject": "s"},
    ]
    # yalnız GUVENLI seçilir, cap uygulanır
    assert [p["id"] for p in pick(res, cap=10)] == ["a", "d", "g"]
    assert [p["id"] for p in pick(res, cap=2)] == ["a", "d"]
    assert [p["id"] for p in pick(res, cap=10, exclude_ids={"a"})] == ["d", "g"]

    # gate
    ok, cap, why = gate({"state": "OK"}, enabled=False, cap=5)
    assert not ok and cap == 0 and "kapalı" in why[0]
    ok, cap, why = gate({"state": "CRITICAL", "bounce_rate": 0.09}, enabled=True, cap=5)
    assert not ok and cap == 0 and "kritik" in why[0]
    ok, cap, why = gate({"state": "WATCH", "bounce_rate": 0.04}, enabled=True, cap=5)
    assert ok and cap == 2 and why, (ok, cap, why)
    ok, cap, why = gate({"state": "OK"}, enabled=True, cap=5)
    assert ok and cap == 5 and not why

    # kuyruk + veto penceresi: aynı gün kuyruğa girenler o gün GİTMEZ
    st: dict = {}
    assert enqueue(st, pick(res, 10), "2026-08-21") == 3
    assert enqueue(st, pick(res, 10), "2026-08-21") == 0        # tekrar eklemez
    assert due(st, "2026-08-21") == []                          # aynı gün → veto penceresi açık
    assert [i["id"] for i in due(st, "2026-08-22")] == ["a", "d", "g"]

    # gönderim kaydı + contacted
    item = st[QUEUE_KEY][0]
    record_sent(st, item, "2026-08-22", "msg1")
    assert st[SENT_KEY][0]["message_id"] == "msg1"
    assert "a" in st["companies_already_contacted"]
    assert st["companies_already_contacted"]["a"]["channel"] == "autosend_vetted"
    drop(st, {"a"})
    assert [i["id"] for i in st[QUEUE_KEY]] == ["d", "g"]

    # veto: kullanıcı taslağı sildiyse (canlı id listesinde yok) gönderilmez
    live = {"g"}
    kalan = [i for i in due(st, "2026-08-23") if i["id"] in live]
    assert [i["id"] for i in kalan] == ["g"]

    # REGRESYON (2026-09-06): gönderilen taslak AYNI run'da geri kuyruğa girmemeli.
    # pick doğru çalışıyor; sözleşme çağırana ait — drop ettiği id'leri exclude_ids'e koymalı.
    st2: dict = {}
    enqueue(st2, pick(res, 10), "2026-09-05")
    gitti = {"a"}
    drop(st2, gitti)
    hepsi = {i["id"] for i in st2[QUEUE_KEY]} | gitti
    assert [p["id"] for p in pick(res, 10, exclude_ids=hepsi)] == []

    lines = summary_lines([], [{"company": "a", "to": "hi@a.com"}], st[QUEUE_KEY], True, [], 5)
    assert any("gönderildi" in x for x in lines) and any("SİL" in x for x in lines)
    print("autosend self-test: OK")
