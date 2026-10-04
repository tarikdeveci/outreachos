"""Günlük takip, pano tarafı: kimle yazıştık, nereye başvuracağız, bugün ne yapılacak.

`tracking.py`'nin firma kayıtlarına işlediği yanıtları, Gönderilenler taramasını, taslak
listesini ve ATS özetini tek panoya çevirir (takip.md ve raporun başındaki blok):

  1. Her firmanın durumunu kayıtlardan türetir; elle işaretlenen bir alan yoktur.
  2. Yanıtın cevaplanıp cevaplanmadığını Gönderilenler'den anlar; cevaplanan iş listeden düşer.
  3. Taslağı kaybolmuş kayıtlara mail gidip gitmediğini Gmail'e sorar, sonucu kalıcı yazar.
  4. ATS ilanlarını günden güne taşır; başvuru maili gelen ilan kendiliğinden kapanır.

Saf modül (Gmail'e soran tek yer enjekte edilen count_fn).
Dosya sonundaki __main__ kendini test eder: `python scripts/board.py`
"""
from __future__ import annotations

import re
from collections import Counter

import autosend
from tracking import (APPS_KEY, ETIKET, GORUSME, GUN_MS, GURULTU, OTOMATIK, RET, YANIT,
                      _day, _iso_ms, _low)

LEDGER_KEY = "ats_ledger"            # firma|rol → ilan kaydı
GORUSME_GUN, YANIT_GUN, BEKLEME_GUN = 14, 30, 14

DURUM_SIRA = ("gorusme", "yanit", "otomatik", "ret", "gonderildi", "taslak", "bounce", "kapali")
DURUM_AD = {"gorusme": "görüşme", "yanit": "yanıt geldi", "otomatik": "otomatik yanıt",
            "ret": "ret", "gonderildi": "gönderildi (yanıt yok)", "taslak": "taslak bekliyor",
            "bounce": "bounce", "kapali": "kapandı (taslak silindi, mail gitmedi)"}
_TUR_DURUM = {GORUSME: "gorusme", YANIT: "yanit", OTOMATIK: "otomatik", RET: "ret"}


def sent_index(state: dict, sent_events, free_mail=frozenset()) -> dict:
    """Hedef anahtarı → o hedefe son gönderim zamanı (ms; zamanı bilinmiyorsa 0)."""
    out = {k: 0 for k in autosend.sent_keys(state, free_mail)}
    for s in state.get(autosend.SENT_KEY, []):
        if s.get("to"):
            k = autosend.target_key(s["to"], free_mail)
            out[k] = max(out.get(k, 0), _iso_ms(s.get("date")))
    for e, ms in sent_events or []:
        k = autosend.target_key(e, free_mail)
        out[k] = max(out.get(k, 0), int(ms or 0))
    return out


def status_of(rec: dict, key: str, sent: dict, live_drafts) -> str:
    """Firma kaydının durumu. live_drafts None ise taslak listesi bilinmiyor demektir:
    gönderilmemiş taslak kaydı 'taslak' sayılır, 'kapandı' denmez."""
    tur = rec.get("yanit_turu")
    if tur in _TUR_DURUM:
        return _TUR_DURUM[tur]
    if rec.get("email_dead") or "BOUNCE" in str(rec.get("last_reply_seen") or ""):
        return "bounce"
    if key in sent:
        return "gonderildi"
    if rec.get("draft_id") and not rec.get("gonderim_soruldu") \
            and (live_drafts is None or rec["draft_id"] in live_drafts):
        return "taslak"
    return "kapali"


def resolve_sent(state: dict, sent: dict, live_drafts, count_fn, free_mail=frozenset(),
                 today: str = "", cap: int = 60) -> int:
    """Taslağı artık olmayan, gönderildiği de bilinmeyen kayıtlar için Gmail'e sorar
    (count_fn: hedef → Gönderilenler'deki mail sayısı | None). Gittiyse kalıcı işaretler
    (mükerrer kapısı da görür), gitmediyse bir daha sormamak üzere not düşer."""
    if live_drafts is None:
        return 0
    asked = 0
    for rec in state.get("companies_already_contacted", {}).values():
        if asked >= cap:
            break
        if not isinstance(rec, dict) or not rec.get("email") or rec.get("gonderim_soruldu"):
            continue
        key = autosend.target_key(rec["email"], free_mail)
        if status_of(rec, key, sent, live_drafts) != "kapali":
            continue
        asked += 1
        n = count_fn(key)
        if n:
            rec["sent_confirmed"] = today
            sent.setdefault(key, 0)
        elif n == 0:
            rec["gonderim_soruldu"] = today
    return asked


def ledger_key(a: dict) -> str:
    """Firma|rol. Panodan gelen ilanda rol şehirsiz başlıktır (`rol`): pano aynı başlığın
    şehirlerini başka sırayla verince ilan yeniden "yeni" sayılmasın."""
    return f"{a['firma']}|{a.get('rol') or a['title']}".lower()


# Tek kelimelik bu adlar onay mailinde şirket adı olmadan da geçer ("keep your application
# open"); onlarda ilanı ad eşleşmesiyle kapatmak yanlış kapatır.
GENEL_AD = {"open", "remote", "engine", "front", "home", "work", "team", "hiring", "apply",
            "jobs", "careers", "talent", "people", "labs", "data", "cloud", "app", "hello", "next"}


def firma_deseni(firma: str):
    """Onay mailinde firmayı arayan desen, ya da kapatmaya elverişsizse None. Kelime sınırlı:
    sıkıştırılmış metinde alt dizgi arayınca 'Juni' 'Junior'da, 'Ramp' 'for Amplitude'da
    eşleşiyordu. Pano adındaki sondaki sayı ('Open 252') atılır; kelimeler arası boşluk
    serbesttir ('Lite LLM' ve 'LiteLLM')."""
    kel = re.findall(r"\w+", _low(firma))
    while len(kel) > 1 and kel[-1].isdigit():
        kel.pop()
    if not kel or len("".join(kel)) < 3 or (len(kel) == 1 and kel[0] in GENEL_AD):
        return None
    return re.compile(r"(?<!\w)" + r"\W*".join(map(re.escape, kel)) + r"(?!\w)")


def update_ledger(state: dict, digest: list, today: str, keep_days: int = 30) -> int:
    """Bugünkü ATS ilanlarını deftere işler; yeni olanları `yeni`, başvurulmuş olanları
    `basvuruldu` diye işaretler. Uzun süredir görülmeyen ilan defterden düşer."""
    led = state.setdefault(LEDGER_KEY, {})
    apps = [a for a in state.get(APPS_KEY, {}).values() if a.get("tur") != GURULTU]
    new = 0
    for a in digest:
        k = ledger_key(a)
        eski = f"{a['firma']}|{a['title']}".lower()        # anahtar şehirsiz olmadan önceki kayıt
        if k not in led and eski in led:
            led[k] = led.pop(eski)
        if k not in led:
            led[k] = {"firma": a["firma"], "title": a["title"], "ilk": today, "basvuru": None}
            a["yeni"] = True
            new += 1
        led[k].update(son=today, link=a["link"])
    now = _iso_ms(today)
    for k, v in list(led.items()):
        desen = None if v.get("basvuru") else firma_deseni(v.get("firma", ""))
        if desen:
            hit = next((x for x in apps if desen.search(_low(f"{x.get('kimden')} {x.get('konu')}"))),
                       None)
            if hit:
                v["basvuru"] = _day(hit.get("ms"))
        if now - _iso_ms(v.get("son")) > (90 if v.get("basvuru") else keep_days) * GUN_MS:
            del led[k]
    for a in digest:
        if led.get(ledger_key(a), {}).get("basvuru"):
            a["basvuruldu"] = True
    return new


def build_board(state: dict, now_ms: int, sent_events=(), live_drafts=None,
                free_mail=frozenset(), today: str = "") -> dict:
    """State → pano: durum sayımı, bugünün işleri, yazışmalar, bekleyenler, ilanlar."""
    sent = sent_index(state, sent_events, free_mail)

    def age(ms) -> int:
        return int((now_ms - ms) // GUN_MS) if ms else 9999

    rows = []
    for firma, rec in state.get("companies_already_contacted", {}).items():
        if not isinstance(rec, dict) or not rec.get("email"):
            continue
        email = rec["email"].lower()
        if rec.get("channel") == "sent_scan" and not rec.get("basvuru"):
            continue                      # elle yazılmış ama başvuru olmayan mail (kişisel, destek)
        key = autosend.target_key(email, free_mail)
        yanit_ms = int(rec.get("yanit_ms") or 0)
        rows.append({"firma": firma, "durum": status_of(rec, key, sent, live_drafts),
                     "yanit_ms": yanit_ms, "ozet": rec.get("yanit_ozet", ""),
                     "gonderim_ms": sent.get(key) or _iso_ms(rec.get("date")),
                     "cevaplandi": bool(yanit_ms) and sent.get(key, 0) > yanit_ms})

    ilgilen = []
    for r in sorted(rows, key=lambda r: -r["yanit_ms"]):
        if r["durum"] == "gorusme" and age(r["yanit_ms"]) <= GORUSME_GUN:
            ilgilen.append(f"GÖRÜŞME: {r['firma']} ({_day(r['yanit_ms'])}"
                           f"{', cevapladın' if r['cevaplandi'] else ''}): {r['ozet'][:140]}")
        elif r["durum"] == "yanit" and not r["cevaplandi"] and age(r["yanit_ms"]) <= YANIT_GUN:
            ilgilen.append(f"YANITLA: {r['firma']} ({_day(r['yanit_ms'])}, "
                           f"{age(r['yanit_ms'])} gündür cevapsız): {r['ozet'][:140]}")
    apps = sorted((a for a in state.get(APPS_KEY, {}).values() if a.get("tur") != GURULTU),
                  key=lambda a: -int(a.get("ms") or 0))
    for a in apps:
        if a["tur"] == GORUSME and age(a.get("ms")) <= GORUSME_GUN:
            ilgilen.append(f"GÖRÜŞME (başvurudan): {a.get('kimden', '')}: {a.get('konu', '')}")
    ats = sorted((v for v in state.get(LEDGER_KEY, {}).values() if not v.get("basvuru")),
                 key=lambda v: v.get("ilk", ""), reverse=True)
    if ats:
        yeni = sum(1 for v in ats if v.get("ilk") == today)
        ilgilen.append(f"BAŞVUR: {yeni} yeni ilan, toplam {len(ats)} açık ilan")
    return {"sayim": Counter(r["durum"] for r in rows), "ilgilen": ilgilen,
            "yazisma": sorted((r for r in rows if r["yanit_ms"] or r["durum"] in _TUR_DURUM.values()),
                              key=lambda r: -r["yanit_ms"]),
            "bekleyen": sorted((r for r in rows if r["durum"] == "gonderildi"
                                and age(r["gonderim_ms"]) >= BEKLEME_GUN),
                               key=lambda r: r["gonderim_ms"]),
            "ats": ats, "basvurular": apps}


def count_line(board: dict) -> str:
    s = board["sayim"]
    return "takip: " + (", ".join(f"{s[k]} {DURUM_AD[k]}" for k in DURUM_SIRA if s.get(k))
                        or "kayıt yok")


def action_lines(board: dict) -> list:
    """Raporun başına konan blok: bugünün işleri ve tek satır durum özeti."""
    return (board["ilgilen"] or ["Bugün senden beklenen bir şey yok."]) + [count_line(board)]


def _cell(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()


def board_markdown(board: dict, today: str) -> str:
    """takip.md: motor her run'da baştan yazar."""
    s = board["sayim"]
    insan = s.get("gorusme", 0) + s.get("yanit", 0) + s.get("ret", 0)
    giden = insan + s.get("otomatik", 0) + s.get("gonderildi", 0)
    L = [f"# Takip panosu ({today})", "",
         "Bu dosyayı motor her run'da baştan yazar; elle yapılan değişiklik kalıcı olmaz.", "",
         "## Bugün ilgilenmen gerekenler", ""]
    L += [f"- {x}" for x in board["ilgilen"]] or ["- Bugün senden beklenen bir şey yok."]
    L += ["", "## Durum özeti", "", "| Durum | Firma sayısı |", "|---|---|"]
    L += [f"| {DURUM_AD[k]} | {s[k]} |" for k in DURUM_SIRA if s.get(k)]
    if giden:
        L += ["", f"Mail giden {giden} firmadan {insan} tanesi bir insan eliyle yanıtladı "
                  f"(%{insan * 100 // giden})."]
    L += ["", f"## Kimle yazıştık ({len(board['yazisma'])})", "",
          "| Firma | Durum | Tarih | Cevapladın mı | Özet |", "|---|---|---|---|---|"]
    L += [f"| {_cell(r['firma'])} | {DURUM_AD[r['durum']]} | {_day(r['yanit_ms'])} | "
          f"{'evet' if r['cevaplandi'] else 'hayır'} | {_cell(r['ozet'])[:160]} |"
          for r in board["yazisma"]]
    L += ["", f"## Başvurulacak ilanlar ({len(board['ats'])})", "",
          "| Firma | Rol | İlk görülme | Link |", "|---|---|---|---|"]
    L += [f"| {_cell(v.get('firma'))} | {_cell(v.get('title'))} | {v.get('ilk', '?')} | "
          f"{v.get('link', '')} |" for v in board["ats"]]
    L += ["", f"## Başvuru mailleri ({len(board['basvurular'])})", "",
          "| Tarih | Kimden | Konu | Tür |", "|---|---|---|---|"]
    L += [f"| {_day(a.get('ms'))} | {_cell(a.get('kimden'))} | {_cell(a.get('konu'))} | "
          f"{ETIKET.get(a.get('tur'), '?')} |" for a in board["basvurular"]]
    L += ["", f"## Yanıt bekleyenler ({len(board['bekleyen'])} firma, {BEKLEME_GUN} gün ve üzeri)", ""]
    L += [", ".join(f"{_cell(r['firma'])} ({_day(r['gonderim_ms'])})" for r in board["bekleyen"])
          or "Yok."]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    D = GUN_MS
    now = _iso_ms("2026-10-04") + D // 2
    free = frozenset({"gmail.com"})

    def rec(email, channel="autosend_vetted", day="2026-09-20", **kw):
        return {"email": email, "channel": channel, "date": day, "last_reply_seen": None, **kw}

    st = {"companies_already_contacted": {
        "acme": rec("hi@acme.io", yanit_turu=GORUSME, yanit_ms=now - 2 * D,
                    yanit_ozet="Re: Hello: Can we have a chat on Tuesday?"),
        "beta": rec("ik@beta.dev", yanit_turu=RET, yanit_ms=now - 3 * D, yanit_ozet="not moving"),
        "gama": rec("info@gama.co", "sent_manual_reviewed", "2026-09-01", yanit_turu=YANIT,
                    yanit_ms=now - 4 * D, yanit_ozet="Could you send your portfolio?"),
        "bltn": rec("a@bltn.com", day="2026-07-01", yanit_turu=OTOMATIK,
                    yanit_ms=_iso_ms("2026-08-03")),
        "eski": rec("a@eski.com", day="2026-07-01"),
        "tslk": rec("a@tslk.com", "gmail_draft_speculative", "2026-10-02", draft_id="D1"),
        "kyp": rec("a@kyp.com", "gmail_draft_speculative", "2026-08-02", draft_id="D2"),
        "git": rec("a@git.com", "gmail_draft_speculative", "2026-08-02", draft_id="D3"),
        "olu": rec("a@olu.com", day="2026-08-02", email_dead=True),
        "arkadas": rec("biri@gmail.com", "sent_scan", "sent_detected")},
        APPS_KEY: {"m5": {"ms": now - D, "tur": OTOMATIK, "kimden": "Hire",
                          "konu": "Your application to Delta Labs"},
                   "m8": {"ms": now - D, "tur": GURULTU, "kimden": "Shop", "konu": "Application"}}}
    cc = st["companies_already_contacted"]

    # durum, Gmail'e sorma
    events = [("ada@acme.io", now - D), ("info@gama.co", now - 10 * D)]
    sent = sent_index(st, events, free)
    asked = resolve_sent(st, sent, {"D1"}, {"kyp.com": 0, "git.com": 2}.get, free, "2026-10-04")
    assert asked == 2 and cc["git"]["sent_confirmed"] and cc["kyp"]["gonderim_soruldu"]
    assert resolve_sent(st, sent, {"D1"}, lambda k: 1 / 0, free, "2026-10-04") == 0   # bir daha sorulmaz
    assert resolve_sent(st, sent, None, lambda k: 1 / 0, free) == 0                   # liste eksikse sorma
    assert "git.com" in autosend.sent_keys(st, free)                                  # mükerrer kapısı da görür

    # ATS defteri
    digest = [{"firma": "Delta Labs", "title": "Junior Engineer", "link": "https://x/1"},
              {"firma": "Omega", "title": "Product Engineer", "link": "https://x/2"}]
    assert update_ledger(st, digest, "2026-10-04") == 2
    assert digest[0].get("basvuruldu") and not digest[1].get("basvuruldu")   # başvuru maili ilanı kapattı
    assert update_ledger(st, digest[1:], "2026-10-05") == 0 and "yeni" in digest[1]
    st[LEDGER_KEY]["omega|product engineer"]["son"] = "2026-08-01"
    update_ledger(st, [], "2026-10-05")
    assert "omega|product engineer" not in st[LEDGER_KEY]                    # eski ilan düştü
    update_ledger(st, digest[1:], "2026-10-04")
    # onay maili firmayı kelime olarak anmalı; sondaki pano sayısı atılır, genel ad kapatmaz
    kapanir = {"Litellm": "Thanks for applying to LiteLLM", "Deel 2": "Deel Hiring Team",
               "Lite LLM": "LiteLLM Hiring Team", "N26": "Your application to N26"}
    assert firma_deseni("Open 252") is None and firma_deseni("ab") is None
    kapanmaz = {"Juni": "Your application for Junior Software Engineer",
                "Engine": "Application received: Software Engineer",
                "Front": "Your application for Frontend Developer",
                "Ramp": "Your application for Amplitude", "Open": "We keep your application open"}
    for ad, konu in kapanir.items():
        assert firma_deseni(ad).search(_low(konu)), ad
    for ad, konu in kapanmaz.items():
        assert not (firma_deseni(ad) and firma_deseni(ad).search(_low(konu))), ad
    assert ledger_key({"firma": "A", "title": "SE (Paris)", "rol": "SE"}) == ledger_key(
        {"firma": "A", "title": "SE (Berlin)", "rol": "SE"}) == "a|se"
    st2 = {LEDGER_KEY: {"a|se (paris)": {"firma": "A", "title": "SE (Paris)", "son": "2026-10-04"}}}
    ilan = [{"firma": "A", "title": "SE (Paris)", "rol": "SE", "link": "https://x/3"}]
    assert update_ledger(st2, ilan, "2026-10-05") == 0 and list(st2[LEDGER_KEY]) == ["a|se"]

    # pano
    b = build_board(st, now, events, {"D1"}, free, "2026-10-04")
    assert dict(b["sayim"]) == {"gorusme": 1, "ret": 1, "yanit": 1, "otomatik": 1, "taslak": 1,
                                "kapali": 1, "gonderildi": 2, "bounce": 1}, b["sayim"]
    assert any(x.startswith("GÖRÜŞME: acme (2026-10-02, cevapladın)") for x in b["ilgilen"]), b["ilgilen"]
    assert any(x.startswith("YANITLA: gama") and "4 gündür" in x for x in b["ilgilen"])
    assert b["ilgilen"][-1] == "BAŞVUR: 1 yeni ilan, toplam 1 açık ilan"
    assert [r["firma"] for r in b["bekleyen"]] == ["eski", "git"], b["bekleyen"]
    assert [a["kimden"] for a in b["basvurular"]] == ["Hire"]                # gürültü panoya girmez
    events.append(("info@gama.co", now - D))                                  # kullanıcı cevapladı
    b2 = build_board(st, now, events, None, free, "2026-10-04")
    assert not any("gama" in x for x in b2["ilgilen"]) and b2["sayim"]["taslak"] == 1
    md = board_markdown(b, "2026-10-04")
    assert "| acme | görüşme |" in md and "Omega" in md and "Hire" in md
    assert "arkadas" not in md and count_line(b).startswith("takip: 1 görüşme")
    assert action_lines({"ilgilen": [], "sayim": Counter()})[0].startswith("Bugün senden")
    print("board self-test: OK")
