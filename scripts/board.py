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
import jobboard
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
        if led.get(k, {}).get("kapali"):                   # aynı rol yeni bir linkle yeniden açılmış
            del led[k]
        if k not in led:
            led[k] = {"firma": a["firma"], "title": a["title"], "ilk": today, "basvuru": None}
            a["yeni"] = True
            new += 1
        led[k].update(son=today, link=a["link"],
                      **{f: a[f] for f in ("rol", "yer", "yayin", "kontrol") if a.get(f)})
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


def dogrula_ilanlar(state: dict, digest: list, dogrula_fn, rol_uygun, today: str,
                    cap: int = 60) -> list:
    """İlanları panonun kendi kaydına sorar (dogrula_fn(link) → jobboard.verify_posting cevabı).

    Kapanan ilan defterde kapanır ve listeye girmez. Açık olanın başlığı, konumu ve yayın
    tarihi kaynağından yazılır: arama sonucunun başlığı çoğu zaman ilanın değil sayfanın adıdır
    ("Careers"), gerçek başlığı role uymayan ilan da kapanır. Doğrulanamayan ilan olduğu gibi
    kalır. Bugünün ilanlarından açık kalanları döndürür."""
    led = state.setdefault(LEDGER_KEY, {})

    def sor(v: dict) -> str:
        """Kaydı kaynağına göre günceller; listeden düşmesi gerekiyorsa sebebini döndürür."""
        s = dogrula_fn(v["link"])
        if s["durum"] == jobboard.KAPALI:
            return "ilan kapanmış"
        if s["durum"] == jobboard.ACIK:
            if s["title"] and not rol_uygun(s["title"]):
                return f"gerçek başlığı role uymuyor: {s['title'][:80]}"
            if s["title"]:
                v.update(rol=s["title"], yer=s["yer"], title=jobboard.baslik(s["title"], s["yer"]))
            v.update(kontrol=today, yayin=s["yayin"] or v.get("yayin", ""))
        return ""

    sirada = sorted((k for k, v in led.items() if not v.get("basvuru") and not v.get("kapali")
                     and v.get("kontrol") != today and v.get("link")),
                    key=lambda k: led[k].get("kontrol", ""))          # en eski kontrol önce
    for k in sirada[:cap]:
        v = led[k]
        neden = sor(v)
        if neden:
            v.update(kapali=today, neden=neden)
        elif v.get("rol") and ledger_key(v) != k:   # başlık düzeldi: anahtar da düzelir
            ayni = led.get(ledger_key(v))
            if ayni and not ayni.get("kapali"):     # aynı ilan iki adla girmiş, eskisi kalır
                ayni["ilk"] = min(ayni.get("ilk", today), v.get("ilk", today))
            else:                                   # o adla kapanmış eski bir ilan varsa açık olan geçer
                led[ledger_key(v)] = v
            del led[k]

    kimlik = {jobboard.ilan_kimligi(v.get("link", "")): k for k, v in led.items()}
    out = []
    for a in digest:
        kid = jobboard.ilan_kimligi(a["link"])
        k = kimlik.get(kid)
        v = led.get(k) if k else None
        if a.get("aday"):                           # panodan bu run'da okundu: açık
            a["kontrol"] = today
            if v and v.get("kapali"):               # yeniden açılmış, kayıt baştan kurulur
                del led[k]
        elif v and v.get("kapali"):
            continue
        elif v:                                     # defterdeki doğrulanmış ad geçerli
            a.update({f: v[f] for f in ("firma", "title", "rol", "yer", "yayin", "kontrol")
                      if v.get(f)})
        else:
            neden = sor(a)
            if neden:                               # hatırlanır: arama aynı ölü linki her gün getirir
                kimlik[kid] = f"kapali|{kid}"
                led[kimlik[kid]] = {"firma": a["firma"], "title": a["title"], "link": a["link"],
                                    "ilk": today, "son": today, "basvuru": None,
                                    "kapali": today, "neden": neden}
                continue
        out.append(a)
    return out


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
    defter = state.get(LEDGER_KEY, {}).values()
    ats = sorted((v for v in defter if not v.get("basvuru") and not v.get("kapali")),
                 key=lambda v: v.get("ilk", ""), reverse=True)
    kapanan = [v for v in defter if v.get("kapali") == today]
    if ats:
        yeni = sum(1 for v in ats if v.get("ilk") == today)
        ilgilen.append(f"BAŞVUR: {yeni} yeni ilan, toplam {len(ats)} açık ilan")
    return {"sayim": Counter(r["durum"] for r in rows), "ilgilen": ilgilen, "kapanan": kapanan,
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
          "Her run'da panonun kendi kaydından doğrulanır; kapanan ilan listeden düşer.", "",
          "| Firma | Rol | Yayın | Son kontrol | Link |", "|---|---|---|---|---|"]
    L += [f"| {_cell(v.get('firma'))} | {_cell(v.get('title'))} | {v.get('yayin') or '?'} | "
          f"{v.get('kontrol') or 'doğrulanamadı'} | {v.get('link', '')} |" for v in board["ats"]]
    if board.get("kapanan"):
        L += ["", f"Bugün listeden düşen {len(board['kapanan'])} ilan: "
              + "; ".join(f"{_cell(v.get('firma'))} ({_cell(v.get('neden'))})"
                          for v in board["kapanan"])]
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

    # ilan doğrulama: kapanan düşer ve hatırlanır, açık olanın adı kaynağından gelir
    G = "https://boards.greenhouse.io/{}/jobs/{}".format
    cevap = {G("udemy", 1): (jobboard.KAPALI, "", "", ""),
             G("figma", 2): (jobboard.ACIK, "Account Executive", "London", "2026-04-17"),
             G("palantir", 3): (jobboard.ACIK, "Software Engineer, New Grad", "New York, NY", "2025-06-26"),
             G("lyft", 4): (jobboard.KAPALI, "", "", ""),
             G("stripe", 5): (jobboard.ACIK, "Software Engineer", "Dublin", "2026-10-01")}
    sorulan = []

    def dogrula(link):
        sorulan.append(link)
        d, t, y, g = cevap.get(link, (jobboard.BILINMIYOR, "", "", ""))
        return {"durum": d, "title": t, "yer": y, "yayin": g}

    def eski(firma, title, no, **kw):
        return {"firma": firma, "title": title, "link": G(firma.lower(), no), "ilk": "2026-10-04",
                "son": "2026-10-06", "basvuru": None, **kw}

    st3 = {LEDGER_KEY: {"udemy|careers": eski("Udemy", "Careers", 1),
                        "figma|careers": eski("Figma", "Careers", 2),
                        "palantir|palantir technologies": eski("Palantir", "Palantir Technologies", 3),
                        "acme|se": eski("Acme", "SE (Berlin)", 9),                  # doğrulanamıyor
                        "beta|se": eski("Beta", "SE", 8, basvuru="2026-10-05")}}    # başvuruldu, sorulmaz
    muh = lambda t: "engineer" in t.lower()                                  # noqa: E731
    bugun = [{"firma": "Palantir", "title": "Palantir Technologies", "link": G("palantir", 3) + "?gh_jid=3"},
             {"firma": "Lyft", "title": "Lyft open jobs", "link": G("lyft", 4)},
             {"firma": "Stripe", "title": "Careers", "link": G("stripe", 5)},
             {"firma": "Udemy", "title": "Careers", "link": G("udemy", 1)},
             {"firma": "Acme", "title": "SE (Berlin)", "rol": "SE", "link": G("acme", 9), "aday": True}]
    kalan = dogrula_ilanlar(st3, bugun, dogrula, muh, "2026-10-07")
    led3 = st3[LEDGER_KEY]
    assert [a["title"] for a in kalan] == ["Software Engineer, New Grad (New York, NY)",
                                           "Software Engineer (Dublin)", "SE (Berlin)"], kalan
    assert led3["udemy|careers"]["neden"] == "ilan kapanmış"
    assert led3["figma|careers"]["neden"].startswith("gerçek başlığı role uymuyor")
    assert "palantir|software engineer, new grad" in led3 and "palantir|palantir technologies" not in led3
    assert led3["kapali|greenhouse:lyft:4"]["kapali"] == "2026-10-07"
    assert "kontrol" not in led3["acme|se"] and G("beta", 8) not in sorulan
    assert update_ledger(st3, kalan, "2026-10-07") == 1                      # yalnızca Stripe yeni
    assert led3["stripe|software engineer"]["yayin"] == "2026-10-01"
    assert led3["palantir|software engineer, new grad"]["ilk"] == "2026-10-04"
    n = len(sorulan)
    assert dogrula_ilanlar(st3, [dict(bugun[1]), dict(bugun[3])], dogrula, muh, "2026-10-08") == []
    assert G("lyft", 4) not in sorulan[n:] and G("udemy", 1) not in sorulan[n:]   # ölü link yeniden sorulmaz
    b3 = build_board(st3, _iso_ms("2026-10-07"), today="2026-10-07")
    assert sorted(v["firma"] for v in b3["ats"]) == ["Acme", "Palantir", "Stripe"]
    assert len(b3["kapanan"]) == 3 and "2 yeni ilan" not in b3["ilgilen"][-1]
    md3 = board_markdown(b3, "2026-10-07")
    assert "| 2026-10-01 | 2026-10-08 |" in md3 and "| ? | 2026-10-07 |" in md3
    assert "Bugün listeden düşen 3 ilan: Udemy (ilan kapanmış)" in md3
    # panoda yeniden görülen ilan kapalı kalmaz
    led3["acme|se"].update(kapali="2026-10-07", neden="ilan kapanmış")
    assert len(dogrula_ilanlar(st3, [dict(bugun[4])], dogrula, muh, "2026-10-08")) == 1 and "acme|se" not in led3
    # aynı rol yeni linkle yeniden açılınca kapalı kaydın arkasında kalmaz (arama ve defter yolu)
    cevap[G("stripe", 6)] = (jobboard.ACIK, "Software Engineer", "Dublin", "2026-10-08")
    led3["stripe|software engineer"].update(kapali="2026-10-08", neden="ilan kapanmış")
    yeniden = dogrula_ilanlar(st3, [{"firma": "Stripe", "title": "Careers", "link": G("stripe", 6)}],
                              dogrula, muh, "2026-10-09")
    assert update_ledger(st3, yeniden, "2026-10-09") == 1
    assert led3["stripe|software engineer"]["link"] == G("stripe", 6) and not led3["stripe|software engineer"].get("kapali")
    led3["stripe|careers"] = eski("Stripe", "Careers", 6)
    led3["stripe|software engineer"] = {**eski("Stripe", "Software Engineer", 5), "kapali": "2026-10-08"}
    dogrula_ilanlar(st3, [], dogrula, muh, "2026-10-10")
    assert led3["stripe|software engineer"]["link"] == G("stripe", 6) and "stripe|careers" not in led3

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
