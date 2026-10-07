"""Günlük raporun HTML gövdesi: düz metin raporla aynı bilgi, bir bakışta okunacak sırayla.

Sıra okuyanın sorusunu izler: bugün ne oldu (sayılar), benden ne bekleniyor (görüşme, yanıt),
nereye başvuracağım (yalnızca açık olduğu doğrulanan ilanlar, tarihiyle), motor ne gönderdi,
sistemin durumu. Taslak triyajının satır satır dökümü maile girmez; gunluk_ozet dosyasında durur.

Mail istemcileri <style> bloğunu ve JavaScript'i güvenilir işlemez: düzen tablo ve satır içi
stille kurulur. Saf modül; dosya sonundaki __main__ kendini test eder: `python scripts/report_html.py`
"""
from __future__ import annotations

import re
from html import escape

import board
import jobboard
import report

AYLAR = ("Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara")
YAZI = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
GRI, KOYU, CIZGI = "#64748b", "#0f172a", "#e2e8f0"
YESIL, SARI, MAVI, NOTR = ("#0b6b3a", "#e3f5ea"), ("#8a5300", "#fdf0d5"), ("#1d4ed8", "#e4ecfd"), \
    ("#475569", "#eef1f5")
ETIKET_RENK = {"GÖRÜŞME": YESIL, "YANITLA": SARI, "BAŞVUR": MAVI}
ILAN_TAVAN, TAZE_GUN, BAYAT_GUN = 20, 30, 120
_IS = re.compile(r"^(GÖRÜŞME|YANITLA|BAŞVUR)(?: \(başvurudan\))?: (.*)$", re.S)


def e(s) -> str:
    """Metni HTML'e güvenli koyar; düz metin rapordan gelen uzun tireyi virgüle çevirir."""
    return escape(re.sub(r"\s+[—–]\s+", ", ", "" if s is None else str(s)).replace("**", "").replace("`", ""))


def tarih(gun: str) -> str:
    """2026-10-07 → 7 Eki 2026; okunamıyorsa olduğu gibi."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", gun or "")
    return f"{int(m.group(3))} {AYLAR[int(m.group(2)) - 1]} {m.group(1)}" if m else str(gun or "")


def gruplar(lines) -> list:
    """Düz metin rapor satırları → [(başlık, maddeler, girintili mi)]. Madde, bir başlığın
    altında '•', '-' ya da '…' ile başlayan satırdır."""
    out: list = []
    for ln in lines or []:
        s = str(ln).strip()
        if s and s[0] in "•-…" and out:
            out[-1][1].append(s.lstrip("•-… ").strip())
        elif s:
            out.append((s, [], str(ln)[:1].isspace()))
    return out


def bag(u) -> str:
    """href'e konacak adres: yalnızca http(s), HTML'e güvenli."""
    u = str(u or "")
    return escape(u, quote=True) if u.startswith(("http://", "https://")) else "#"


def _rozet(metin: str, renk: tuple) -> str:
    return (f'<span style="display:inline-block;background:{renk[1]};color:{renk[0]};font-size:11px;'
            f'font-weight:700;letter-spacing:.04em;padding:3px 8px;border-radius:999px">{e(metin)}</span>')


def _bolum(baslik: str, govde: str, sayi=None) -> str:
    if not govde:
        return ""
    ek = f' <span style="font-weight:400">({sayi})</span>' if sayi is not None else ""
    # Büyük harf burada yapılır: CSS'in çevirisi Türkçe i ve ı harflerini yanlış büyütür.
    buyuk = baslik.replace("i", "İ").replace("ı", "I").upper()
    return (f'<tr><td style="padding:24px 24px 0"><div style="font-size:12px;letter-spacing:.06em;'
            f'color:{GRI};font-weight:700;padding-bottom:10px">{e(buyuk)}{ek}</div>{govde}</td></tr>')


def _kutular(kutular: list) -> str:
    td = "".join(
        f'<td width="{100 // len(kutular)}%" style="padding:0 4px;vertical-align:top">'
        f'<div style="background:{renk[1]};border-radius:10px;padding:12px 4px;text-align:center">'
        f'<div style="font-size:26px;font-weight:700;line-height:1.1;color:{renk[0]}">{e(n)}</div>'
        f'<div style="font-size:12px;color:#475569;padding-top:4px">{e(ad)}</div></div></td>'
        for n, ad, renk in kutular)
    return (f'<tr><td style="padding:18px 20px 0"><table role="presentation" width="100%" '
            f'cellpadding="0" cellspacing="0"><tr>{td}</tr></table></td></tr>')


def _satir(sol: str, sag: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-top:1px solid {CIZGI}"><tr><td width="92" style="padding:10px 0;'
            f'vertical-align:top">{sol}</td><td style="padding:10px 0;font-size:14px;line-height:1.45;'
            f'color:{KOYU}">{sag}</td></tr></table>')


def _is_satiri(line: str) -> str:
    """'YANITLA: gromo (2026-09-27, 10 gündür cevapsız): özet' → etiket, firma, ayrıntı, özet."""
    m = _IS.match(str(line))
    if not m:
        return _satir(_rozet("NOT", NOTR), e(line))
    etiket, kalan = m.groups()
    bas, ayrac, ozet = kalan.partition("): ")
    if not ayrac:
        bas, _, ozet = kalan.partition(": ")
    firma, _, ayrinti = bas.partition(" (")
    sag = f"<b>{e(firma)}</b>"
    if ayrinti:
        ayrinti = re.sub(r"\d{4}-\d{2}-\d{2}", lambda t: tarih(t.group(0)), ayrinti.rstrip(")"))
        sag += f' <span style="color:{GRI}">{e(ayrinti)}</span>'
    if ozet:
        sag += f'<div style="color:#334155;padding-top:2px">{e(ozet[:170])}</div>'
    return _satir(_rozet(etiket, ETIKET_RENK[etiket]), sag)


def ilan_sirasi(ilanlar: list, day: str) -> list:
    """Önce bugün ilk görülenler, sonra konumu tercih edilenler, sonra en yeni yayınlananlar.
    Aylardır açık duran ilan (sürekli açık tutulan havuz ilanı) konumu ne olursa olsun sona kalır."""
    def anahtar(v: dict) -> tuple:
        yas = report.ilan_yasi(v.get("yayin", ""), day)
        return (v.get("ilk") != day and not v.get("yeni"), yas is not None and yas > BAYAT_GUN,
                jobboard.yer_onceligi(v.get("yer", "")), BAYAT_GUN if yas is None else yas)
    return sorted(ilanlar, key=anahtar)


def _ilan_karti(v: dict, day: str) -> str:
    yas = report.ilan_yasi(v.get("yayin", ""), day)
    if yas is None:
        notu, renk = (("bugün açık olduğu doğrulandı", GRI) if v.get("kontrol") == day
                      else ("açık olduğu doğrulanamadı", SARI[0]))
    else:
        notu = f"{tarih(v['yayin'])} tarihinde yayınlandı, {yas} gündür açık"
        renk = YESIL[0] if yas <= TAZE_GUN else (SARI[0] if yas > BAYAT_GUN else GRI)
    yeni = (" " + _rozet("YENİ", MAVI)) if v.get("ilk") == day or v.get("yeni") else ""
    alt = " · ".join(e(x) for x in (v.get("firma"), v.get("yer")) if x)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-top:1px solid {CIZGI}"><tr><td style="padding:11px 8px 11px 0">'
            f'<div style="font-size:15px;font-weight:600;color:{KOYU}">'
            f'{e(v.get("rol") or v.get("title"))}{yeni}</div>'
            f'<div style="font-size:13px;color:#334155;padding-top:2px">{alt}</div>'
            f'<div style="font-size:12px;color:{renk};padding-top:2px">{e(notu)}</div></td>'
            f'<td width="104" align="right" style="vertical-align:middle;white-space:nowrap">'
            f'<a href="{bag(v.get("link"))}" style="display:inline-block;'
            f'background:{MAVI[0]};color:#ffffff;text-decoration:none;font-size:13px;font-weight:600;'
            f'padding:8px 14px;border-radius:6px">İlanı aç</a></td></tr></table>')


def _liste(maddeler: list, tavan: int = 12) -> str:
    li = "".join(f'<div style="font-size:13px;line-height:1.5;color:#334155;padding:1px 0">{m}</div>'
                 for m in maddeler[:tavan])
    if len(maddeler) > tavan:
        li += f'<div style="font-size:13px;color:{GRI}">ve {len(maddeler) - tavan} tane daha</div>'
    return li


def _alt_baslik(metin: str) -> str:
    return f'<div style="font-size:14px;font-weight:600;color:{KOYU};padding:8px 0 3px">{e(metin)}</div>'


def _taslak(d: dict) -> str:
    skor = f' <span style="color:{YESIL[0]}">%{e(d["skor"])} uygun</span>' if d.get("skor") else ""
    out = (f'<div style="border-top:1px solid {CIZGI};padding:9px 0;font-size:14px;color:{KOYU}">'
           f'<b>{e(d.get("firma"))}</b>{skor} <span style="color:{GRI}">{e(d.get("email"))}</span>')
    if d.get("skor_gerekce"):
        out += f'<div style="font-size:13px;color:#334155">{e(d["skor_gerekce"])}</div>'
    baglar = [(d["kariyer"], "kendi başvuru sayfası")] if d.get("kariyer") else []
    baglar += [(report.linkedin_search_url(d.get("firma", ""), r), f"LinkedIn: {r}")
               for r in d.get("hedef_kisiler") or []]
    if baglar:
        out += '<div style="font-size:13px;padding-top:2px">' + " · ".join(
            f'<a href="{bag(u)}" style="color:{MAVI[0]}">{e(ad)}</a>' for u, ad in baglar) + "</div>"
    return out + "</div>"


def build_report_html(day: str, pano: dict | None, ats: list, drafted: list, reply_notes: list,
                      skipped: list, giden=(), kuyruk=(), banner_lines=(), audit_lines=(),
                      send_lines=(), takip_lines=()) -> str:
    """Günlük raporun HTML'i. pano: board.build_board çıktısı (kurulamadıysa None; o zaman
    ilan listesi bugünün `ats` listesinden gelir). giden / kuyruk: bugün gönderilenler ve
    gönderim sırasındakiler (autosend kayıtları)."""
    ilanlar = ilan_sirasi((pano or {}).get("ats") or ats or [], day)
    isler = [x for x in takip_lines or [] if not str(x).startswith("takip:")]
    bekleyen_is = [x for x in isler if _IS.match(str(x)) and not str(x).startswith("BAŞVUR")]
    kapanan = (pano or {}).get("kapanan") or []

    govde = _kutular([(len(giden), "bugün giden mail", YESIL if giden else NOTR),
                      (len(kuyruk), "yarın gidecek", NOTR),
                      (len(bekleyen_is), "senden beklenen", SARI if bekleyen_is else NOTR),
                      (len(ilanlar), "açık ilan", MAVI if ilanlar else NOTR)])
    govde += _bolum("Senin işin", "".join(_is_satiri(x) for x in isler if not str(x).startswith("BAŞVUR"))
                    or _satir(_rozet("TAMAM", YESIL), "Bugün senden beklenen bir yanıt ya da görüşme yok."))

    kartlar = "".join(_ilan_karti(v, day) for v in ilanlar[:ILAN_TAVAN])
    if len(ilanlar) > ILAN_TAVAN:
        kartlar += (f'<div style="font-size:13px;color:{GRI};padding-top:8px">ve {len(ilanlar) - ILAN_TAVAN} '
                    f'ilan daha, tam liste veri reposundaki takip.md dosyasında</div>')
    if kapanan:
        kartlar += (f'<div style="font-size:13px;color:{GRI};padding-top:8px">Bugünkü kontrolde '
                    f'{len(kapanan)} ilan kapanmış ya da role uymuyor çıktı, listeye alınmadı.</div>')
    govde += _bolum("Başvuracağın ilanlar", kartlar or (
        f'<div style="font-size:14px;color:{GRI}">Açık olduğu doğrulanan ilan yok.</div>'), len(ilanlar))

    gonderim = ""
    if giden:
        gonderim += _alt_baslik(f"Bugün gönderildi ({len(giden)})") + _liste(
            [f'{e(s.get("company"))} <span style="color:{GRI}">{e(s.get("to"))}</span>' for s in giden])
    if kuyruk:
        gonderim += _alt_baslik(f"Yarın gidecek ({len(kuyruk)})") + _liste(
            [f'{e(q.get("company"))} <span style="color:{GRI}">{e(str(q.get("subject", ""))[:70])}</span>'
             for q in kuyruk])
        gonderim += (f'<div style="font-size:12px;color:{SARI[0]};padding-top:4px">Gitmesini istemediğin '
                     f"taslağı Gmail'de Taslaklar'dan sil, gönderilmez.</div>")
    for baslik, maddeler, _ in gruplar(send_lines):
        if baslik[:1] not in "📤🕐":           # gidenler ve kuyruk yukarıda; kalanlar istisnalar
            gonderim += _alt_baslik(baslik) + _liste([e(m) for m in maddeler], 8)
    govde += _bolum("Otomatik gönderim", gonderim)

    govde += _bolum("Gelen yanıt ve bounce", _liste([e(n) for n in reply_notes or []]),
                    len(reply_notes or []))
    govde += _bolum("Bugün açılan taslaklar", "".join(_taslak(d) for d in drafted or []),
                    len(drafted or []))

    durum = "".join(f'<div style="font-size:13px;line-height:1.55;color:#334155">{e(b)}</div>'
                    for b, _, _ in gruplar(banner_lines))
    durum += "".join(f'<div style="font-size:13px;line-height:1.55;color:#334155;padding-top:4px">{e(b)}</div>'
                     for b, _, girintili in gruplar(audit_lines) if not girintili)
    if pano and pano.get("sayim"):
        durum += (f'<div style="font-size:13px;line-height:1.55;color:#334155;padding-top:4px">'
                  f'{e(board.count_line(pano))}</div>')
    if skipped:
        durum += f'<div style="font-size:13px;color:{GRI};padding-top:4px">Bugün elenen {len(skipped)} aday: ' \
            + e(", ".join(f"{adet} {grup}" for grup, adet in report.skip_breakdown(skipped)[:6])) + "</div>"
    govde += _bolum("Sistem durumu", durum)

    ozet = f"{len(giden)} mail gitti, {len(bekleyen_is)} iş senden bekleniyor, {len(ilanlar)} açık ilan"
    return (
        f'<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:#f1f5f9;{YAZI}">'
        f'<div style="display:none;max-height:0;overflow:hidden">{e(ozet)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9">'
        f'<tr><td align="center" style="padding:16px 8px"><table role="presentation" width="100%" '
        f'cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:14px;{YAZI}">'
        f'<tr><td style="padding:22px 24px 0"><div style="font-size:20px;font-weight:700;color:{KOYU}">'
        f'İş arama raporu</div><div style="font-size:14px;color:{GRI};padding-top:2px">{e(tarih(day))} · '
        f'{e(ozet)}</div></td></tr>{govde}'
        f'<tr><td style="padding:22px 24px 24px;font-size:12px;color:{GRI}">Ayrıntılı döküm veri reposunda: '
        f'gunluk_ozet/{e(day)}.md ve takip.md. Bu rapor yalnızca kendi adresine gönderildi.</td></tr>'
        f'</table></td></tr></table></body></html>')


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    from collections import Counter

    assert tarih("2026-10-07") == "7 Eki 2026" and tarih("?") == "?"
    assert bag("https://x.io/a?b=1&c=2") == "https://x.io/a?b=1&amp;c=2" and bag("javascript:alert(1)") == "#"
    assert e("A — B <b> **x**") == "A, B &lt;b&gt; x"
    g = gruplar(["📋 Bekleyen 3 taslak", "  ⚠ İÇERİK HATASI (2):", "    • a (x@a.io)", "    • b", "", "🔧 Onarım: 1"])
    assert [(b, m, gi) for b, m, gi in g] == [("📋 Bekleyen 3 taslak", [], False),
                                              ("⚠ İÇERİK HATASI (2):", ["a (x@a.io)", "b"], True),
                                              ("🔧 Onarım: 1", [], False)]
    gun = "2026-10-07"
    ilanlar = [
        {"firma": "Axel", "rol": "Software Engineer", "yer": "Bellevue, WA", "yayin": "2025-08-12",
         "ilk": "2026-10-04", "kontrol": gun, "link": "https://jobs.ashbyhq.com/Axel/1"},
        {"firma": "Adaptyv", "rol": "Software Engineer, Backend", "yer": "London", "yayin": "2026-08-26",
         "ilk": "2026-10-05", "kontrol": gun, "link": "https://jobs.ashbyhq.com/adaptyv/2?a=1&b=2"},
        {"firma": "Stripe", "rol": "New Grad <Engineer>", "yer": "Dublin", "yayin": "2026-10-01",
         "ilk": gun, "kontrol": gun, "link": "https://boards.greenhouse.io/stripe/jobs/5"},
        {"firma": "Eski", "title": "SE (Berlin)", "ilk": "2026-10-04", "link": "https://x.io/j/9"},
        {"firma": "Anima", "rol": "Product Engineer", "yer": "Remote", "yayin": "2025-04-17",
         "ilk": "2026-10-04", "kontrol": gun, "link": "https://jobs.ashbyhq.com/Anima/3"},
        {"firma": "Accord", "rol": "Product Manager", "yer": "San Francisco, CA", "yayin": "2026-09-28",
         "ilk": "2026-10-04", "kontrol": gun, "link": "https://jobs.ashbyhq.com/Accord/4"}]
    sira = [v["firma"] for v in ilan_sirasi(ilanlar, gun)]
    # yeni; taze olanlar konum sırasıyla (Avrupa, belirsiz, ABD); aylardır açık olanlar en sonda
    assert sira == ["Stripe", "Adaptyv", "Eski", "Accord", "Anima", "Axel"], sira
    pano = {"ats": ilanlar, "kapanan": [{"firma": "Udemy"}], "sayim": Counter(gonderildi=165, gorusme=5)}
    takip = ["GÖRÜŞME: wibbi (2026-10-05): Yaklaşan toplantı — hatırlatma",
             "YANITLA: gromo (2026-09-27, 10 gündür cevapsız): Re: AI Product Engineer: Dear Tarık",
             "GÖRÜŞME (başvurudan): Hire: Interview invitation", "Görüşme hazırlığı, wibbi: şirket notu",
             "BAŞVUR: 1 yeni ilan, toplam 6 açık ilan", "takip: 5 görüşme, 165 gönderildi (yanıt yok)"]
    html = build_report_html(
        gun, pano, [], [{"firma": "Acme", "email": "hi@acme.io", "skor": 82, "skor_gerekce": "AI ürünü",
                         "kariyer": "https://acme.io/careers", "hedef_kisiler": ["CTO"]}],
        ["**seal** — `contact@seal.run` bounce aldı"], [("a.io", "sayfalarda e-posta bulunamadı")],
        giden=[{"company": "axlehealth", "to": "info@axlehealth.com"}],
        kuyruk=[{"company": "goin", "to": "contact@goin.app", "subject": "Software Engineer application"}],
        banner_lines=["🟡 Mail sağlığı: İZLEME — hard-bounce %4.2", "   → bugünkü hedef: 0/20"],
        audit_lines=["📋 Bekleyen 155 taslak triyajı: ✅15 temiz", "  ⚠ İÇERİK HATASI (25):",
                     "    • openworklabs (team@openworklabs.com)  — profilde olmayan sayı"],
        send_lines=["📤 Otomatik gönderildi (1/10):", "    • axlehealth (info@axlehealth.com)",
                    "🔁 Aynı yere ikinci mail engellendi (1):", "    • beta (a@beta.io): daha önce yazıldı"],
        takip_lines=takip)
    for parca in ("7 Eki 2026", "1 mail gitti, 3 iş senden bekleniyor, 6 açık ilan", "<b>gromo</b>",
                  "SENİN İŞİN", "BAŞVURACAĞIN İLANLAR", '<meta charset="utf-8">',
                  "27 Eyl 2026, 10 gündür cevapsız", "New Grad &lt;Engineer&gt;", "YENİ", "26 Ağu 2026 tarihinde yayınlandı, 42 gündür açık",
                  "açık olduğu doğrulanamadı", 'href="https://jobs.ashbyhq.com/adaptyv/2?a=1&amp;b=2"',
                  "Bugün gönderildi (1)", "Yarın gidecek (1)", "Aynı yere ikinci mail engellendi",
                  "daha önce yazıldı", "1 ilan kapanmış ya da role uymuyor", "Mail sağlığı: İZLEME, hard-bounce",
                  "Bekleyen 155 taslak triyajı", "takip: 5 görüşme", "1 sitede adres yok", "%82 uygun",
                  "LinkedIn: CTO", "seal, contact@seal.run bounce aldı"):
        assert parca in html, parca
    for yok in ("openworklabs", "İÇERİK HATASI", "—", "<Engineer>", "toplam 6 açık ilan"):   # döküm, ham metin girmez
        assert yok not in html, yok
    # pano kurulamadıysa bugünün listesi kullanılır; hiçbir şey yokken de rapor çıkar
    assert "Stripe" in build_report_html(gun, None, ilanlar[2:3], [], [], [])
    bos = build_report_html(gun, None, [], [], [], [])
    assert "Açık olduğu doğrulanan ilan yok" in bos and "Bugün senden beklenen" in bos
    print("report_html self-test: OK")
