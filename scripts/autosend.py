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


# Bir firmaya GERÇEKTEN mail gittiğini gösteren kanallar (taslak açılmış olması yetmez).
SENT_CHANNELS = {"sent_scan", "sent_manual_reviewed", "autosend_vetted"}


def target_key(to: str, free_mail=frozenset()) -> str:
    """Mükerrer karşılaştırmasının anahtarı: kurumsal adreste domain (iris@ ile hello@ aynı
    firmadır), ücretsiz postada adresin kendisi (gmail.com tek bir 'firma' değildir)."""
    e = (to or "").strip().lower()
    dom = e.split("@")[-1]
    return e if dom in free_mail else dom


def sent_keys(state: dict, free_mail=frozenset()) -> dict:
    """State'in 'buraya mail gitti' dediği her hedef → sebep.

    Kalıcı kayıttır: Gönderilenler taraması son 200 mesajı okur, daha eski bir gönderim
    o pencereden düşer ama burada durur."""
    out: dict = {}
    for v in state.get("companies_already_contacted", {}).values():
        if not isinstance(v, dict) or not v.get("email"):
            continue
        if v.get("channel") in SENT_CHANNELS or v.get("sent_confirmed"):
            out[target_key(v["email"], free_mail)] = "daha önce mail gittiği kayıtlı"
    for s in state.get(SENT_KEY, []):
        if s.get("to"):
            out[target_key(s["to"], free_mail)] = f"motor {s.get('date', '?')} tarihinde gönderdi"
    return out


def duplicate_check(item: dict, known: dict, sent_domains: set, run_keys: set,
                    live_fn=None, free_mail=frozenset()) -> tuple:
    """Gönderimden HEMEN ÖNCEKİ son kapı → (karar, sebep).

    karar: 'gonder' | 'engel' (gönderme, kuyruktan düşür) | 'bilinmiyor' (Gmail'e
    sorulamadı: bu run'da gönderme, kuyrukta kalsın). Denetimin MUKERRER kararından
    bağımsızdır: o karar bir gün önce verilmiş olabilir ve Gönderilenler'in son 200
    mesajına dayanır. Bu kapı sırayla aynı run'a (run_keys), kalıcı kayda (known,
    sent_keys çıktısı), bugünkü Gönderilenler taramasına ve Gmail'in kendisine bakar
    (live_fn: hedef → o hedefe giden mail sayısı, cevap alınamadıysa None)."""
    key = target_key(item.get("to", ""), free_mail)
    if key in run_keys:
        return "engel", "bu run'da aynı firmaya zaten gönderildi"
    if key in known:
        return "engel", known[key]
    if key in sent_domains:
        return "engel", "Gönderilenler'de bu firmaya giden mail var"
    if live_fn is not None:
        n = live_fn(key)
        if n is None:
            return "bilinmiyor", "Gönderilenler'e sorulamadı"
        if n:
            return "engel", "Gmail Gönderilenler'de bu firmaya giden mail bulundu"
    return "gonder", ""


def mark_sent_confirmed(state: dict, draft_id: str, day: str) -> None:
    """Son kapının yakaladığı mükerreri kalıcı kayda işler. Yoksa taslak ertesi gün yine
    ✅ çıkar, kuyruğa girer ve kapıda tekrar durur; işaretlenince denetim onu MUKERRER
    sayar ve onarım siler."""
    for v in state.get("companies_already_contacted", {}).values():
        if isinstance(v, dict) and v.get("draft_id") == draft_id:
            v["sent_confirmed"] = day


def summary_lines(due_items: list, sent: list, queued: list, allowed: bool,
                  reasons: list, cap: int, unowned: list | None = None,
                  blocked: list | None = None) -> list:
    """Rapor bloğu — ne gitti, ne kuyrukta, veto nasıl yapılır.

    unowned: kuyrukta olup motorun kendi açtığı taslaklar arasında bulunmayanlar. Bunlar
    gönderilmez ve kuyruktan düşer; kullanıcı neden gitmediğini raporda görsün.
    blocked: son kapının (duplicate_check) durdurdukları; her öğede 'why' var."""
    lines: list[str] = []
    if unowned:
        lines.append(f"🚫 Motorun kaydında yok, gönderilmedi ({len(unowned)}):")
        lines += [f"    • {u.get('company', '')} ({u['to']})" for u in unowned]
    if blocked:
        lines.append(f"🔁 Aynı yere ikinci mail engellendi ({len(blocked)}):")
        lines += [f"    • {b.get('company', '')} ({b['to']}): {b.get('why', '')}" for b in blocked]
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

    # sahiplik: motorun kaydında olmayan kuyruk öğesi raporda ayrı görünür
    assert not any("kaydında yok" in x for x in lines)
    lines = summary_lines([], [], [], True, [], 5, unowned=[{"company": "z", "to": "hi@z.com"}])
    assert "kaydında yok" in lines[0] and "hi@z.com" in lines[1]

    # son kapı: aynı yere ikinci mail hiçbir yoldan gitmez
    free = frozenset({"gmail.com"})
    assert target_key("Iris@Acme.io", free) == "acme.io"
    assert target_key("ayse@gmail.com", free) == "ayse@gmail.com"
    st3 = {SENT_KEY: [{"date": "2026-09-10", "to": "hello@acme.io"}],
           "companies_already_contacted": {
               "beta": {"channel": "sent_scan", "email": "jobs@beta.dev"},
               "gama": {"channel": "gmail_draft_speculative", "email": "hi@gama.co", "draft_id": "G1"},
               "delta": {"channel": "gmail_draft_speculative", "email": "hi@delta.co",
                         "draft_id": "D1", "sent_confirmed": "2026-10-01"}}}
    known = sent_keys(st3, free)
    assert set(known) == {"acme.io", "beta.dev", "delta.co"}, known     # taslak açmak gönderim değil

    def chk(to, sent=frozenset(), run=frozenset(), live=None):
        return duplicate_check({"to": to}, known, set(sent), set(run), live, free)[0]

    assert chk("iris@acme.io") == "engel"                    # motor göndermiş, başka adres
    assert chk("ik@beta.dev") == "engel"                     # elle gönderilmiş, kayıtlı
    assert chk("hi@gama.co") == "gonder"                     # yalnızca taslağı var
    assert chk("hi@gama.co", sent={"gama.co"}) == "engel"    # bugünkü Gönderilenler taraması
    assert chk("hi@gama.co", run={"gama.co"}) == "engel"     # aynı run'da ikinci taslak
    assert chk("hi@gama.co", live=lambda k: 1) == "engel"    # Gmail'de eski bir gönderim var
    assert chk("hi@gama.co", live=lambda k: 0) == "gonder"
    assert chk("hi@gama.co", live=lambda k: None) == "bilinmiyor"     # sorulamadı: gönderme
    assert chk("ali@gmail.com", sent={"gmail.com"}, live=lambda k: 0) == "gonder"   # adres bazlı
    mark_sent_confirmed(st3, "G1", "2026-10-03")
    assert "gama.co" in sent_keys(st3, free)
    lines = summary_lines([], [], [], True, [], 5,
                          blocked=[{"company": "acme", "to": "iris@acme.io", "why": "kayıtlı"}])
    assert "ikinci mail engellendi" in lines[0] and "kayıtlı" in lines[1]
    print("autosend self-test: OK")
