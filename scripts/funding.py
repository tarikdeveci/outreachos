#!/usr/bin/env python3
"""
Yatırım haberlerinden aday: son aylarda tur kapatmış şirketler.

Neden: havuzun çoğu YC ve HN listesinden geliyordu; ne zaman yatırım aldıkları bilinmiyordu.
Yeni tur kapatmış şirket parayı büyümeye harcamak zorundadır ve hızlı iş çıkaracak insan arar;
soğuk mailin en çok karşılık bulduğu an budur. Kaynaklar RSS: anahtarsız, kotasız, ağır HTML
kazıma yok. Her biri 2026-10-13'te yerelde denendi (`?paged=N` ile geriye gidilebiliyor).

Haberden şirket alan adı şöyle bulunur: haber gövdesindeki dış linklerden, alan adının ilk
parçası başlıkta geçen ("Obsero raises..." ve obsero.ai). Yatırımcı ve medya linkleri bu
kuralı zaten geçemez ya da açıkça atılır. Gövdesi akışta olmayan haberlerde sayfanın kendisi
okunur (run başına sınırlı).

    python scripts/funding.py              # canlı: akışları oku, bulunanları yaz
    python scripts/funding.py --self-test  # ağsız
"""
from __future__ import annotations

import html as _html
import os
import re
import sys
import unicodedata
from datetime import date, datetime
from email.utils import parsedate_to_datetime

# (akış, etiket, en fazla sayfa). Sayfa başına 10 ile 20 haber.
FEEDS = [
    ("https://webrazzi.com/kategori/yatirim/feed/", "Webrazzi yatırım", 4),
    ("https://egirisim.com/kategori/yatirim/feed/", "eGirişim yatırım", 4),
    ("https://www.eu-startups.com/category/fundin/feed/", "EU-Startups funding", 6),
    ("https://tech.eu/feed/", "Tech.eu", 4),
    ("https://sifted.eu/feed", "Sifted", 1),
    ("https://www.uktech.news/feed", "UKTN", 4),
    ("https://siliconcanals.com/feed/", "Silicon Canals", 4),
    ("https://techcrunch.com/category/venture/feed/", "TechCrunch venture", 4),
    ("https://betakit.com/feed/", "BetaKit", 1),
]
FON_GUN = int(os.environ.get("FON_GUN", "180"))           # bundan eski haber aday olmaz
HABER_OKUMA = int(os.environ.get("HABER_OKUMA", "30"))    # run başına açılan haber sayfası

FON_RE = re.compile(r"(?i)\b(raises?|raised|lands|secures?|closes?|bags|nabs|snags|funding|"
                    r"pre-?seed|seed|series [a-f]|round)\b|yatırım (?:aldı|turu|turunu)|"
                    r"tohum yatırım|seri [a-f]\b")
# Satın alma, fon kurma, yatırımcının kendi haberi: şirket yatırım almamış.
HARIC_RE = re.compile(r"(?i)\b(acquires?|acquired|acquisition|merges?|fund (?:i|ii|iii|iv|v)\b|"
                      r"launches .*fund|new fund|deploys|invests? in|backs)\b|satın al|fon kurdu|"
                      r"fonunu|fonu kurdu|turuna katıldı|yatırım yaptı")
TUR_RE = re.compile(r"(?i)\b(pre-?seed|seed|series [a-f]|seri [a-f]|tohum|growth|bridge)\b")
# Para birimi şart: "100 billion litres" tutar değildir.
TUTAR_RE = re.compile(r"(?i)(?:([$€£])\s?(\d+(?:[.,]\d+)?)\s?-?(m|mn|million|b|bn|billion|k)\b"
                      r"|(\d+(?:[.,]\d+)?)\s?(milyon|milyar|million|billion|bin)\s?"
                      r"(dolar|euro|avro|sterlin|tl|usd|eur)\b)")
DEGERLEME = re.compile(r"(?i)^\W*(?:\w+\W+)?(değerleme|valuation)")
YATIRIMCI = re.compile(r"(?i)(ventures?|capital|partners|invest|holding|fund|vc$|\.vc$)")
# Kurucu adı: unvanın yanında iki ya da üç büyük harfle başlayan kelime.
_AD = r"[A-ZÇĞİÖŞÜ][a-zçğıöşüé'’]+(?:\s+[A-ZÇĞİÖŞÜ][a-zçğıöşüé'’]+){1,2}"
_UNVAN = r"(?i:co-?founder|founder|kurucu(?:su)?(?: ortağı| ortak)?|ceo|cto|chief executive)"
KURUCU_RE = re.compile(rf"{_UNVAN}(?:\s*(?i:and|&|ve)\s*{_UNVAN})?\s*[,:]?\s*({_AD})"
                       rf"|({_AD})\s*[,(]\s*(?i:the\s+)?{_UNVAN}")
# Ad gibi görünen ama ad olmayan büyük harfli kelimeler (başlık, şirket eki, unvan).
AD_DEGIL = {"and", "the", "of", "at", "in", "for", "with", "says", "said", "team", "story",
            "insights", "report", "officer", "ai", "inc", "ltd", "group", "labs", "technologies",
            "ventures", "capital", "partners", "chief", "head", "office", "series", "seed",
            "startup", "startups", "founder", "founders"}


def ascii_(s: str) -> str:
    s = (s or "").translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def sikistir(s: str) -> str:
    """Karşılaştırma için: küçük harf, Türkçe harfler ASCII, yalnızca harf ve rakam."""
    return re.sub(r"[^a-z0-9]", "", ascii_(s).lower())


def parse_feed(xml: str) -> list[dict]:
    """RSS öğeleri: başlık, link, tarih (ISO gün), gövde (HTML, yoksa açıklama)."""
    out = []
    for it in re.findall(r"<item\b[^>]*>(.*?)</item>", xml or "", re.S):
        def alan(ad: str) -> str:
            m = re.search(rf"<{ad}\b[^>]*>(.*?)</{ad}>", it, re.S)
            v = m.group(1).strip() if m else ""
            return re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
        try:
            gun = parsedate_to_datetime(alan("pubDate")).date().isoformat()
        except (TypeError, ValueError):
            gun = ""
        out.append({"baslik": _html.unescape(alan("title")), "link": _html.unescape(alan("link")),
                    "tarih": gun,
                    "govde": _html.unescape(alan("content:encoded") or alan("description"))})
    return out


def fon_haberi_mi(baslik: str) -> bool:
    return bool(FON_RE.search(baslik)) and not HARIC_RE.search(baslik)


def tur_of(metin: str) -> str:
    m = TUR_RE.search(metin or "")
    if not m:
        return ""
    t = m.group(1).lower().replace("seri ", "series ").replace("tohum", "seed")
    return t.replace("preseed", "pre-seed")


def tutar_musd(metin: str) -> float | None:
    """Başlıktaki tur tutarı, milyon cinsinden (para birimi ayrımı yapılmaz: sıralama için)."""
    # Şirket değerlemesi tur tutarı değildir ("3,1 milyar dolar değerleme üzerinden 200 milyon").
    m = next((m for m in TUTAR_RE.finditer(metin or "")
              if not DEGERLEME.match(metin[m.end():m.end() + 30])), None)
    if not m:
        return None
    sayi, birim = (m.group(2), m.group(3)) if m.group(2) else (m.group(4), m.group(5))
    try:
        v = float(sayi.replace(",", "."))
    except ValueError:
        return None
    birim = birim.lower()
    if birim in ("b", "bn", "billion", "milyar"):
        return v * 1000
    if birim in ("k", "bin"):
        return v / 1000
    return v


def _etiketler(domain: str) -> list[str]:
    """Alan adının karşılaştırılacak biçimleri: acme, getacme → acme, acmehq → acme."""
    ilk = domain.split(".")[0]
    adaylar = {ilk}
    for on in ("get", "try", "use", "join", "hello", "go", "with"):
        if ilk.startswith(on) and len(ilk) - len(on) >= 4:
            adaylar.add(ilk[len(on):])
    for son in ("hq", "app", "labs", "ai", "io", "tech", "hub"):
        if ilk.endswith(son) and len(ilk) - len(son) >= 4:
            adaylar.add(ilk[:-len(son)])
    return [a for a in adaylar if len(a) >= 3]


def sirket_alani(baslik: str, linkler: list[str], domain_of, is_noise,
                 haric: str = "") -> str | None:
    """Başlıkta adı geçen şirketin alan adı. Bulunamazsa None: tahmin yok."""
    sik = sikistir(baslik)
    kelimeler = set(re.findall(r"[a-z0-9]+", ascii_(baslik).lower()))
    for href in linkler:
        d = domain_of(href)
        if not d or d == haric or is_noise(d) or YATIRIMCI.search(d.split(".")[0]):
            continue
        for e in _etiketler(d):
            if (len(e) >= 4 and e in sik) or e in kelimeler:
                return d
    return None


def kurucu_adlari(metin: str) -> list[tuple[str, str]]:
    """Metinde kurucu/CEO/CTO unvanının yanında geçen adlar: (ad, çevresindeki metin)."""
    duz = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", metin or ""))
    out: list[tuple[str, str]] = []
    for m in KURUCU_RE.finditer(duz):
        ad = (m.group(1) or m.group(2) or "").strip()
        parca = ad.split()
        if not 2 <= len(parca) <= 3 or any(p.lower() in AD_DEGIL for p in parca):
            continue
        if ad not in (a for a, _ in out):
            out.append((ad, duz[max(0, m.start() - 80):m.end() + 80].strip()))
    return out[:3]


def _linkler(html_: str) -> list[str]:
    return re.findall(r'href=["\'](https?://[^"\'>\s]+)', html_ or "")


def haber_adaylari(get_fn, domain_of, is_noise, bugun: date | None = None,
                   okunan: dict | None = None, log: list | None = None,
                   feeds: list | None = None) -> dict[str, dict]:
    """Akışlardaki yatırım haberlerinden aday sözlüğü (alan adı → aday).

    `okunan` (haber linki → gün) run'lar arasında tutulur: aynı haber sayfası ikinci kez
    açılmaz, bir sayfanın tüm haberleri okunmuşsa daha eskiye gidilmez."""
    bugun = bugun or date.today()
    okunan = okunan if okunan is not None else {}
    adaylar: dict[str, dict] = {}
    acilan = 0
    for url, etiket, sayfa_tavan in feeds or FEEDS:
        bulunan = 0
        kendi = domain_of(url)
        for sayfa in range(1, sayfa_tavan + 1):
            raw = get_fn(url if sayfa == 1 else f"{url}{'&' if '?' in url else '?'}paged={sayfa}")
            ogeler = parse_feed(raw.decode("utf-8", errors="replace")) if raw else []
            if not ogeler:
                break
            yeni = [o for o in ogeler if o["link"] not in okunan]
            eski_sayfa = all(o["tarih"] and (bugun - date.fromisoformat(o["tarih"])).days > FON_GUN
                             for o in ogeler)
            for o in yeni:
                okunan[o["link"]] = bugun.isoformat()
                if not o["tarih"] or (bugun - date.fromisoformat(o["tarih"])).days > FON_GUN:
                    continue
                if not fon_haberi_mi(o["baslik"]):
                    continue
                govde = o["govde"]
                d = sirket_alani(o["baslik"], _linkler(govde), domain_of, is_noise, kendi)
                if not d and acilan < HABER_OKUMA and o["link"]:
                    acilan += 1
                    sayfa_html = get_fn(o["link"])
                    if sayfa_html:
                        govde = sayfa_html.decode("utf-8", errors="replace")
                        d = sirket_alani(o["baslik"], _linkler(govde), domain_of, is_noise, kendi)
                if not d or d in adaylar:
                    continue
                adaylar[d] = {"domain": d, "kaynak": f"{etiket}: {o['baslik'][:90]}",
                              "haber": o["baslik"][:200], "fon_tarihi": o["tarih"],
                              "fon_turu": tur_of(o["baslik"]), "fon_musd": tutar_musd(o["baslik"]),
                              "kurucular": [[a, m[:240]] for a, m in kurucu_adlari(govde)],
                              "olcek": True, "ulke": "??"}
                bulunan += 1
            if not yeni or eski_sayfa:
                break
        if log is not None:
            log.append(f"  {etiket} → {bulunan} yeni yatırım haberi")
    return adaylar


def birlestir(kayit: dict, yeni: dict, bugun: date | None = None, gun: int = 365) -> dict:
    """Kalıcı kayda yeni adayları ekler, `gun`den eski yatırımları atar. Yeni kayıt kazanır."""
    bugun = bugun or date.today()
    kayit = {**kayit, **yeni}
    return {d: c for d, c in kayit.items()
            if c.get("fon_tarihi") and (bugun - date.fromisoformat(c["fon_tarihi"])).days <= gun}


# ---------------------------------------------------------------- self-test
def _self_test() -> int:
    def dom(u: str) -> str:
        m = re.match(r"https?://(?:www\.)?([^/:?#]+)", u)
        return m.group(1).lower() if m else ""

    def gurultu(d: str) -> bool:
        return any(n in d for n in ("linkedin.com", "eu-startups.com", "webrazzi.com"))

    assert fon_haberi_mi("London’s Obsero raises €1 million to help brands")
    assert fon_haberi_mi("Yapay zeka destekli Ambi, 26 milyon dolar yatırım aldı")
    assert fon_haberi_mi("egaranti, Seri A öncesi turda 1.1 milyon dolar yatırım aldı")
    assert not fon_haberi_mi("Accenture acquires UK customer intelligence firm Sagacity")
    assert not fon_haberi_mi("Borusan Ventures, Magentic’in 18 milyon dolarlık yatırım turuna katıldı")
    assert not fon_haberi_mi("TechCrunch Disrupt 2026: Day 1")
    assert tur_of("Causa Prima lands $20M seed") == "seed"
    assert tur_of("Acme closes Series B") == "series b" and tur_of("Seri A turu") == "series a"
    assert tutar_musd("Causa Prima lands $20M seed") == 20.0
    assert tutar_musd("Ramp, 1,85 milyar dolar yatırım aldı") == 1850.0
    assert tutar_musd("Obsero raises €1 million") == 1.0 and tutar_musd("no money") is None
    assert tutar_musd("Rodinia aims to save 100 billion litres of water") is None
    assert tutar_musd("Arena, 3,1 milyar dolar değerleme üzerinden 200 milyon dolar yatırım") == 200.0
    assert tutar_musd("OSCP closes $6-million USD Series A") == 6.0

    # başlıkta adı geçen şirket; yatırımcı, medya ve LinkedIn linkleri atlanır
    lk = ["https://www.linkedin.com/in/x/", "https://base10.vc/", "https://luminousvc.com/",
          "https://www.ambi.ai/en", "https://en.huaqin.com/"]
    assert sirket_alani("Ambi, 26 milyon dolar yatırım aldı", lk, dom, gurultu) == "ambi.ai"
    assert sirket_alani("Jev'in geliştiricisi TypeSafe AI yatırım aldı",
                        ["https://a16z.com/", "https://typesafe.ai/"], dom, gurultu) == "typesafe.ai"
    assert sirket_alani("Undecimal Bio lands €16 million",
                        ["https://undecimalbio.com/"], dom, gurultu) == "undecimalbio.com"
    assert sirket_alani("Lago raises Series A", ["https://getlago.com/"], dom, gurultu) == "getlago.com"
    assert sirket_alani("Acme raises $5M", ["https://other.io/"], dom, gurultu) is None
    assert sirket_alani("Webrazzi: X raises", ["https://webrazzi.com/a"], dom, gurultu) is None

    adlar = kurucu_adlari("<p>The round was led by Base10, said co-founder and CEO Henrik Gebbing."
                          "</p><p>Ayşe Yılmaz, kurucu ortak, ekledi.</p><p>CEO Insights Report</p>")
    assert [a for a, _ in adlar] == ["Henrik Gebbing", "Ayşe Yılmaz"], adlar
    assert kurucu_adlari("Şirketin kurucusu Mehmet Kaya yaptığı açıklamada")[0][0] == "Mehmet Kaya"

    rss = ("<rss><channel><item><title><![CDATA[Obsero raises €1 million]]></title>"
           "<link>https://www.eu-startups.com/a</link><pubDate>Tue, 13 Oct 2026 08:00:00 +0000</pubDate>"
           "<content:encoded><![CDATA[<a href=\"https://obsero.ai/\">Obsero</a> founder Jane Doe"
           "]]></content:encoded></item>"
           "<item><title>Old Co raises seed</title><link>https://www.eu-startups.com/b</link>"
           "<pubDate>Mon, 01 Jan 2024 08:00:00 +0000</pubDate><description>x</description></item>"
           "<item><title>Sifted Co raises Series A</title><link>https://sifted.eu/c</link>"
           "<pubDate>Tue, 13 Oct 2026 08:00:00 +0000</pubDate></item></channel></rss>")
    istek: list = []

    def get(u: str) -> bytes | None:
        istek.append(u)
        if u == "https://feed.test/":
            return rss.encode()
        if u == "https://sifted.eu/c":
            return b'<a href="https://siftedco.com/">site</a>'
        return None

    okunan: dict = {}
    a = haber_adaylari(get, dom, gurultu, date(2026, 10, 13), okunan,
                       feeds=[("https://feed.test/", "Test", 3)])
    assert set(a) == {"obsero.ai", "siftedco.com"}, a
    assert a["obsero.ai"]["fon_tarihi"] == "2026-10-13" and a["obsero.ai"]["fon_musd"] == 1.0
    assert [k[0] for k in a["obsero.ai"]["kurucular"]] == ["Jane Doe"] and a["siftedco.com"]["fon_turu"] == "series a"
    # ikinci run: haberler okunmuş, sayfa yeniden açılmaz, eski sayfaya gidilmez
    n = len(istek)
    assert haber_adaylari(get, dom, gurultu, date(2026, 10, 13), okunan,
                          feeds=[("https://feed.test/", "Test", 3)]) == {}
    assert len(istek) == n + 1 and "paged" not in istek[-1]

    k = birlestir({"eski.io": {"fon_tarihi": "2025-01-01"}, "orta.io": {"fon_tarihi": "2026-05-01"}},
                  {"yeni.io": {"fon_tarihi": "2026-10-01"}}, date(2026, 10, 13))
    assert set(k) == {"orta.io", "yeni.io"}
    print("funding self-test: OK")
    return 0


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import discover as d  # noqa: E402

    log: list = []
    a = haber_adaylari(lambda u: d._get(u, timeout=15, limit=2_000_000), d.domain_of,
                       d.is_noise, log=log)
    print("\n".join(log))
    for c in sorted(a.values(), key=lambda c: c["fon_tarihi"], reverse=True):
        print(f"{c['fon_tarihi']}  {c['domain']:28s} {c['fon_turu'] or '-':9s} "
              f"{c['fon_musd'] or '-'!s:>6}  {', '.join(k[0] for k in c['kurucular']) or '-'}  | {c['haber'][:70]}")
    print(f"\n{len(a)} şirket ({datetime.now():%H:%M})")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
