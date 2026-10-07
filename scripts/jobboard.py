"""
Şirketin kendi ilan panosu (Greenhouse, Lever, Ashby, Workable).

Yazılacak adresi olmayan aday eskiden sessizce eleniyordu; oysa sitesinde ilan panosu varsa
başvuru yolu açıktır. Pano bağlantısı sitede aranır (`find_board`), açık ilanlar panonun
herkese açık API'sinden okunur (`board_postings`), role uyanlar konum tercihine göre sıralanıp
"sen başvur" listesine girer (`board_candidates`, `tavana_sigan`).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import unquote

from report import GENERIC_ATS_SEGMENTS
from tracking import _low

# İlan konumu tercihi. Filtre değil, sıra: hiçbir ilan konumu yüzünden elenmez.
# "Dublin, CA" gibi ABD şehri Avrupa adıyla karışmasın diye "şehir, EYALET" önce ABD'ye çevrilir
# (DE bilerek yok: "Berlin, DE" Almanya'dır).
_YER_EYALET = re.compile(
    r"[^,;/|()]*,\s*(?:AL|AK|AZ|AR|CA|CO|CT|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|"
    r"NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b")
_YER_BOS = re.compile(       # yer bildirmeyen sözcükler ve harf olmayan her şey
    r"(?<!\w)(?:hybrid|on-?site|in-?office|multiple|various|global|flexible|locations?|tbd|n/a|"
    r"fully|first|friendly|only|ok|or|and)(?!\w)|[\W\d_]+")
_YER_IYI = re.compile(
    r"(?<!\w)(?:emea|europe(?:an)?|avrupa|eu|eea|dach|nordics?|benelux|cet|türkiye|turkiye|turkey|tr|"
    r"istanbul|ankara|izmir|bursa|antalya|kocaeli|united kingdom|uk|gb|(?<!new )england|scotland|"
    r"(?<!south )wales|ireland|germany|deutschland|france|netherlands|belgium|luxembourg|spain|"
    r"portugal|italy|switzerland|austria|sweden|norway|denmark|finland|iceland|poland|"
    r"czech(?:ia| republic)?|slovakia|hungary|romania|bulgaria|greece|croatia|slovenia|serbia|"
    r"estonia|latvia|lithuania|ukraine|cyprus|malta|london|manchester|edinburgh|glasgow|dublin|"
    r"berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart|paris|amsterdam|rotterdam|"
    r"brussels?|madrid|barcelona|lisbon|porto(?! alegre)|milan|rome|zurich|zürich|geneva|vienna|"
    r"stockholm|oslo|copenhagen|helsinki|warsaw|krakow|kraków|prague|budapest|bucharest|sofia|"
    r"athens|tallinn|riga|vilnius|belgrade|zagreb|kyiv)(?!\w)")
_YER_UZAK = re.compile(r"(?<!\w)(?:remote|uzaktan|distributed|anywhere|worldwide|work from home)(?!\w)")


def yer_onceligi(yer: str) -> int:
    """İlan konumunun tercih sırası (filtre değil): 0 = Türkiye/Avrupa ya da yere bağlanmamış
    uzaktan, 1 = konum belirsiz ya da boş, 2 = diğer (ör. ABD şehri, "Remote (US)" gibi
    başka bir yerle sınırlı uzaktan ilan)."""
    t = _low(_YER_EYALET.sub(" usa ", yer or ""))
    if not _YER_BOS.sub("", t):
        return 1
    # Uzaktan ilan, yanında tercih dışı bir yer adı kalmıyorsa tercih edilir.
    if _YER_IYI.search(t) or (_YER_UZAK.search(t) and not _YER_BOS.sub("", _YER_UZAK.sub("", t))):
        return 0
    return 2


def tavana_sigan(ilanlar: list, yeni: int, diger: int, tavan: int) -> list:
    """Run tavanına sığan ilanlar, sıra korunarak. yeni/diger: o ana kadar alınan yeni ilan
    sayısı ve bunların konum önceliği 2 olanları. Öncelik 2 tavanın en fazla yarısını kullanır
    (en az 1: küçük tavanda da konum yüzünden tam dışlama olmasın); 0 ve 1 tamamını kullanabilir."""
    out: list = []
    for a in ilanlar:
        d = yer_onceligi(a.get("yer", "")) == 2
        if yeni + len(out) < tavan and not (d and diger >= max(1, tavan // 2)):
            out.append(a)
            diger += d
    return out


PANO_RE = re.compile(
    r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/"
    r"(?:embed/job_(?:board|app)(?:/js)?\?(?:[^\"'\s<>]*?&(?:amp;)?)?for=)?(?P<greenhouse>[A-Za-z0-9_-]+)"
    r"|jobs\.eu\.lever\.co/(?P<lever_eu>[A-Za-z0-9_-]+)"
    r"|jobs\.lever\.co/(?P<lever>[A-Za-z0-9_-]+)"
    r"|jobs\.ashbyhq\.com/(?P<ashby>[A-Za-z0-9_.%-]+)"
    r"|apply\.workable\.com/(?P<workable>[A-Za-z0-9_-]+)", re.I)
PANO_API = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
            "lever": "https://api.lever.co/v0/postings/{}?mode=json",
            "lever_eu": "https://api.eu.lever.co/v0/postings/{}?mode=json",
            "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
            "workable": "https://apply.workable.com/api/v1/widget/accounts/{}"}
# Kelime sınırlı: alt dizgi araması "Internal Tools" ve "International" ilanlarını öne alıyordu.
GIRIS_SEVIYESI = re.compile(r"(?<!\w)(?:junior|jr|intern(?:ship)?|stajyer|staj|associate|entry|"
                            r"new grad|yeni mezun|graduate|trainee)(?!\w)", re.I)


def find_board(html: str, domain: str = "") -> tuple | None:
    """Sayfada şirketin ATS panosuna giden bağlantı varsa (tür, pano adı). Birden çok pano
    varsa adı alan adının ilk parçasına benzeyen seçilir: sayfada önce başka bir şirketin
    (müşteri, yatırımcı) panosu geçince eleme site sahibine, ilan öteki şirkete ait oluyordu."""
    bulunan = []
    for m in PANO_RE.finditer((html or "").replace("\\/", "/")):    # JSON içinde kaçışlı bağlantı
        tur = m.lastgroup
        ad = m.group(tur).rstrip(".")                  # cümle sonu noktası ada girmesin
        if ad and ad.lower() not in GENERIC_ATS_SEGMENTS | {"j", "api", "embed"}:
            bulunan.append((tur, ad))
    kok = re.sub(r"[^a-z0-9]", "", domain.lower().split(".")[0])
    benzer = [p for p in bulunan if kok and kok[:4] in re.sub(r"[^a-z0-9]", "", unquote(p[1]).lower())]
    return (benzer or bulunan or [None])[0]


def _satirlar(raw: bytes) -> tuple[list, bool]:
    """Pano cevabındaki ilan satırları ve cevabın sonuna kadar okunup okunmadığı."""
    text = (raw or b"").decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else (data.get("jobs") if isinstance(data, dict) else [])
        return list(rows or []), True
    except json.JSONDecodeError:
        # İlan metinlerini de taşıyan cevap boyut tavanında kesilir: okunabilen ilanlar alınır.
        rows, dec = [], json.JSONDecoder()
        i = 0 if text.lstrip().startswith("[") else text.find('"jobs"')
        while i >= 0:
            i = text.find("{", i)
            if i < 0:
                break
            try:
                r, i = dec.raw_decode(text, i)
            except json.JSONDecodeError:
                break
            rows.append(r)
        return rows, False


def _gun(v) -> str:
    """Panonun verdiği yayın zamanı (ISO metin ya da ms) → YYYY-AA-GG; okunamıyorsa boş."""
    if isinstance(v, (int, float)) and v > 0:
        return time.strftime("%Y-%m-%d", time.gmtime(v / 1000))
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return m.group(0) if m else ""


def _ilan(r: dict) -> dict:
    """Bir pano satırı → {title, link, yer, yayin}. Dört panonun alan adları burada birleşir."""
    kat = r.get("categories") if isinstance(r.get("categories"), dict) else {}
    yer = r.get("location") or kat.get("location") or \
        " ".join(str(r[k]) for k in ("city", "country") if r.get(k))
    if isinstance(yer, dict):
        yer = yer.get("name") or " ".join(str(yer[k]) for k in ("city", "country") if yer.get(k))
    return {"title": str(r.get("title") or r.get("text") or "").strip(),
            "link": str(r.get("absolute_url") or r.get("hostedUrl") or r.get("jobUrl") or r.get("url") or ""),
            "yer": str(yer or "").strip(),
            "yayin": _gun(r.get("first_published") or r.get("publishedAt") or r.get("published_on")
                          or r.get("createdAt") or r.get("created_at") or r.get("updated_at"))}


def board_postings(tur: str, ad: str, get_fn) -> list[dict]:
    """Panonun açık ilanları: [{title, link, yer, yayin}]. get_fn(url) → bytes | None."""
    raw = get_fn(PANO_API[tur].format(ad))
    if not raw:
        return []
    out = []
    for r in _satirlar(raw)[0]:
        if not isinstance(r, dict) or r.get("isListed") is False:
            continue
        ilan = _ilan(r)
        if ilan["title"] and ilan["link"].startswith("http"):
            out.append(ilan)
    return out


def baslik(rol: str, yer: str) -> str:
    """Listede görünen ilan başlığı: rol ve varsa konum."""
    return (f"{rol} ({yer})" if yer else rol)[:110]


def board_candidates(pano: tuple, get_fn, role_ok, limit: int = 2) -> list[dict]:
    """Panodaki role uyan ilanlar, digest kaydı biçiminde. Şirket başına en fazla `limit`
    ilan; önce konumu tercih edilenler (`yer_onceligi`), sonra giriş seviyesi olanlar;
    aynı başlığın farklı şehirleri tek kayıt (konumu en uygun olan). Firma adı pano adından
    gelir (arama özetindeki gibi): başvuru onay maili şirketi o adla anar, ilan defteri de
    ilanı o adla kapatır. Konum ayrıca `yer` alanında taşınır; `title` biçimi defter anahtarıdır."""
    firma = unquote(pano[1]).replace("-", " ").title()
    gorulen: set = set()
    uygun = sorted((x for x in board_postings(pano[0], pano[1], get_fn) if role_ok(x["title"])),
                   key=lambda x: (yer_onceligi(x["yer"]), not GIRIS_SEVIYESI.search(x["title"])))
    uygun = [x for x in uygun if not (x["title"].lower() in gorulen or gorulen.add(x["title"].lower()))]
    return [{"firma": firma, "link": x["link"], "aday": True, "yer": x["yer"], "rol": x["title"],
             "yayin": x["yayin"], "title": baslik(x["title"], x["yer"])}
            for x in uygun[:limit]]


# Tekil ilan linki → (tür, pano adı, ilan no). Workable'ın kısa linkinde pano adı yoktur.
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ILAN_RE = re.compile(
    r"(?:job-boards|boards)(?P<gh_eu>\.eu)?\.greenhouse\.io/(?P<gh>[A-Za-z0-9_-]+)/jobs/(?P<gh_no>\d+)"
    r"|jobs\.(?P<lv_eu>eu\.)?lever\.co/(?P<lv>[A-Za-z0-9_-]+)/(?P<lv_no>" + _UUID + r")"
    r"|jobs\.ashbyhq\.com/(?P<ab>[A-Za-z0-9_.%-]+)/(?P<ab_no>" + _UUID + r")"
    r"|apply\.workable\.com/(?:(?P<wk>[A-Za-z0-9_-]+)/)?j/(?P<wk_no>[A-Za-z0-9]+)", re.I)
ILAN_API = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs/{}",
            "lever": "https://api.lever.co/v0/postings/{}/{}",
            "lever_eu": "https://api.eu.lever.co/v0/postings/{}/{}"}
ACIK, KAPALI, BILINMIYOR = "acik", "kapali", "bilinmiyor"
_YOK = (404, 410)


def ilan_coz(link: str) -> tuple | None:
    """İlan linkinden (tür, pano adı, ilan no); bilinen bir panonun tekil ilanı değilse None."""
    m = ILAN_RE.search(link or "")
    if not m:
        return None
    g = m.groupdict()
    if g["gh_no"]:
        return ("greenhouse_eu" if g["gh_eu"] else "greenhouse", g["gh"], g["gh_no"])
    if g["lv_no"]:
        return ("lever_eu" if g["lv_eu"] else "lever", g["lv"], g["lv_no"])
    if g["ab_no"]:
        return ("ashby", g["ab"], g["ab_no"])
    return ("workable", g["wk"] or "", g["wk_no"])


def ilan_kimligi(link: str) -> str:
    """Aynı ilanı başka yazılmış linkte de tanıyan anahtar (sorgu dizgisi, kısa link, AB alanı)."""
    c = ilan_coz(link)
    if not c:
        return (link or "").split("#")[0].rstrip("/").lower()
    return f"{c[0].split('_')[0]}:{'' if c[0] == 'workable' else c[1]}:{c[2]}".lower()


def verify_posting(link: str, get_fn) -> dict:
    """İlan hâlâ açık mı, panonun kendi kaydına göre: {durum, title, yer, yayin}.
    get_fn(url) → (HTTP kodu, gövde, son URL); kod 0 ağ hatasıdır.

    Kapalı demek kanıt ister: ilanın kendi ucu 404 döner ya da sonuna kadar okunan panoda ilan
    yoktur. Ağ hatası, kesilmiş cevap ve tanınmayan site "bilinmiyor" döner; açık bir ilan
    bağlantı sorunu yüzünden listeden düşmesin."""
    sonuc = {"durum": BILINMIYOR, "title": "", "yer": "", "yayin": ""}
    c = ilan_coz(link)
    if not c:
        return {**sonuc, "durum": KAPALI if get_fn(link)[0] in _YOK else BILINMIYOR}
    tur, ad, no = c
    if tur == "greenhouse_eu":              # AB panosunun API'si ayrı; yanlış uçtan 404 kanıt değil
        return sonuc
    if tur in ILAN_API:
        kod, raw, _ = get_fn(ILAN_API[tur].format(ad, no))
        if kod != 200:
            return {**sonuc, "durum": KAPALI if kod in _YOK else BILINMIYOR}
        try:
            r = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            r = None
        return {**sonuc, "durum": ACIK, **({k: v for k, v in _ilan(r).items() if k != "link"}
                                           if isinstance(r, dict) else {})}
    if tur == "workable" and not ad:        # kısa link: yönlendirildiği adres pano adını verir
        kod, _, son = get_fn(link)
        ad = (ilan_coz(son) or ("", "", ""))[1]
        if not ad:
            kapali = kod in _YOK or (kod == 200 and son.rstrip("/").endswith("/oops"))
            return {**sonuc, "durum": KAPALI if kapali else BILINMIYOR}
    kod, raw, _ = get_fn(PANO_API[tur].format(ad))
    if kod != 200:
        return {**sonuc, "durum": KAPALI if kod in _YOK else BILINMIYOR}
    rows, tam = _satirlar(raw)
    for r in rows:
        if isinstance(r, dict) and no.lower() in (str(r.get("id", "")).lower(),
                                                  str(r.get("shortcode", "")).lower()):
            return {**sonuc, "durum": ACIK, **{k: v for k, v in _ilan(r).items() if k != "link"}}
    return {**sonuc, "durum": KAPALI if tam else BILINMIYOR}


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    # konum tercihi: 0 uzaktan ya da Türkiye/Avrupa, 1 belirsiz, 2 diğer
    for y in ("Remote", "Uzaktan", "Fully Remote", "Remote - Global", "Hybrid or Remote",
              "Remote (EMEA)", "Remote, Türkiye", "İstanbul", "Izmir Turkey", "Ankara, TÜRKİYE",
              "Berlin, DE", "London, United Kingdom", "Hybrid - Amsterdam",
              "London, UK; New York, NY", "Remote, Canada; Remote, Poland", "Remote (US or Europe)"):
        assert yer_onceligi(y) == 0, y
    for y in ("", "  ", "Hybrid", "Multiple Locations", "N/A"):
        assert yer_onceligi(y) == 1, y
    for y in ("San Francisco, CA", "New York", "New York, New York, USA", "Remote (US)",
              "Remote - United States", "US Remote", "Remote, Singapore", "Austin, TX or Remote",
              "New York or Remote", "Dublin, CA", "Cambridge, MA", "New England", "Milwaukee",
              "Bangalore, India"):
        assert yer_onceligi(y) == 2, y

    # run tavanı: öncelik 2 en fazla yarısını kullanır, 0 ve 1 tamamını; sıra korunur
    il = [{"title": str(i), "yer": y}
          for i, y in enumerate(["Austin, TX", "Berlin", "New York", "", "Boston", "Remote"])]
    sec = lambda *a: [x["title"] for x in tavana_sigan(*a)]              # noqa: E731
    assert sec(il, 0, 0, 4) == ["0", "1", "2", "3"]                      # 2 ABD (yarı) + 2 öteki, tavan doldu
    assert sec(il, 0, 0, 5) == ["0", "1", "2", "3", "5"]                 # yarı dolunca ABD atlanır
    assert sec([il[0]] * 9, 0, 0, 8) == ["0"] * 4 and sec([il[1]] * 9, 0, 0, 8) == ["1"] * 8
    assert sec(il, 2, 2, 4) == ["1", "3"] and sec(il, 4, 0, 4) == []     # yarı dolu; tavan dolu
    assert sec(il[:1], 0, 0, 1) == ["0"] and sec(il, 0, 0, 0) == []      # küçük tavanda dışlama yok

    # find_board: sitedeki pano bağlantısı; jenerik uç nokta ve tekil ilan kısayolu pano değildir
    assert find_board('<a href="https://jobs.ashbyhq.com/acme/123">Careers</a>') == ("ashby", "acme")
    assert find_board('src="https://boards.greenhouse.io/embed/job_board/js?for=acme-labs"') \
        == ("greenhouse", "acme-labs")
    assert find_board("https://job-boards.greenhouse.io/acme/jobs/1") == ("greenhouse", "acme")
    assert find_board("https://jobs.eu.lever.co/acme") == ("lever_eu", "acme")
    assert find_board("https://apply.workable.com/j/AB12 https://apply.workable.com/acme/") \
        == ("workable", "acme")
    assert find_board("https://boards.greenhouse.io/embed/job_app <p>no board</p>") is None
    # cümle sonu noktası, token'lı embed, JSON kaçışı; ASCII dışı ad yok; alan adına benzeyen pano kazanır
    for metin, ad in (("See https://jobs.ashbyhq.com/acme. Thanks", "acme"), ("jobs.lever.co/şirket", None),
                      ("boards.greenhouse.io/embed/job_app?token=4&amp;for=acme", "acme"),
                      ('{"u": "https:\\/\\/jobs.lever.co\\/acme"}', "acme"),
                      ("jobs.lever.co/musteri jobs.ashbyhq.com/acmehq", "acmehq")):
        assert (find_board(metin, "acme.io") or (0, None))[1] == ad, metin
    assert find_board("jobs.lever.co/musteri jobs.ashbyhq.com/acmehq") == ("lever", "musteri")
    assert [bool(GIRIS_SEVIYESI.search(t)) for t in ("Junior AI Engineer", "Software Intern",
            "Internal Tools Engineer", "International Solutions Engineer")] == [True, True, False, False]

    # board_postings: dört panonun cevap biçimi; kesilmiş cevapta okunabilen ilanlar kalır
    gh = {"jobs": [{"title": "Junior Software Engineer", "absolute_url": "https://x.io/j/1",
                    "location": {"name": "Berlin"}, "updated_at": "2026-09-30T03:39:26-04:00",
                    "first_published": "2026-04-17T12:21:54-04:00"}], "meta": {"total": 1}}
    assert board_postings("greenhouse", "acme", lambda u: json.dumps(gh).encode()) == [
        {"title": "Junior Software Engineer", "link": "https://x.io/j/1", "yer": "Berlin",
         "yayin": "2026-04-17"}]
    assert [_gun(v) for v in (1778700315006, "2026-09-01", "2025-07-19T01:53:05.987+00:00", None, "dün")] \
        == ["2026-05-13", "2026-09-01", "2025-07-19", "", ""]
    lv = [{"text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/1",
           "categories": {"location": "Remote"}, "description": "x" * 50},
          {"text": "Senior Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/2"}]
    kesik = json.dumps(lv).encode()[:-30]                 # ikinci ilan yarıda kesildi
    assert [p["title"] for p in board_postings("lever", "acme", lambda u: kesik)] == ["Backend Engineer"]
    ab = {"apiVersion": "1", "jobs": [
        {"title": "Product Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "location": "London",
         "isListed": True},
        {"title": "Gizli", "jobUrl": "https://jobs.ashbyhq.com/acme/2", "isListed": False}]}
    assert [p["title"] for p in board_postings("ashby", "acme", lambda u: json.dumps(ab).encode())] \
        == ["Product Engineer"]
    assert [p["title"] for p in board_postings("ashby", "acme", lambda u: json.dumps(ab).encode()[:-60])] \
        == ["Product Engineer"]
    wk = {"name": "Acme [TR]", "jobs": [{"title": "AI Engineer", "url": "https://apply.workable.com/j/1",
                                        "city": "Izmir", "country": "Turkey"}]}
    assert board_postings("workable", "acme", lambda u: json.dumps(wk).encode())[0]["yer"] == "Izmir Turkey"
    assert board_postings("lever", "acme", lambda u: None) == []
    assert board_postings("lever", "acme", lambda u: b"<html>404</html>") == []

    # board_candidates: rol filtresi; önce konum tercihi, sonra giriş seviyesi; aynı başlığın
    # konumu en uygun olanı tek kayıt; şirket başına 2
    cok = [{"text": t, "hostedUrl": f"https://jobs.lever.co/acme/{i}", "categories": {"location": y}}
           for i, (t, y) in enumerate([("Software Engineer", "New York, NY"), ("Software Engineer", "Berlin"),
                                       ("Senior Software Engineer", ""), ("Account Executive", ""),
                                       ("Junior Data Engineer", "Austin, TX"), ("Backend Engineer", ""),
                                       ("Junior AI Engineer", "")])]
    args = (("lever", "acme-labs"), lambda u: json.dumps(cok).encode(),
            lambda t: "engineer" in t.lower() and "senior" not in t.lower())
    secim = board_candidates(*args, limit=4)
    assert [a["title"] for a in secim] == ["Software Engineer (Berlin)", "Junior AI Engineer",
                                           "Backend Engineer", "Junior Data Engineer (Austin, TX)"], secim
    assert [a["yer"] for a in secim] == ["Berlin", "", "", "Austin, TX"]
    assert [a["title"] for a in board_candidates(*args)] == [a["title"] for a in secim[:2]]
    assert all(a["firma"] == "Acme Labs" and a["aday"] for a in secim)

    # ilan linki: tür, pano, ilan no; aynı ilanın farklı yazılışları tek kimlik
    U1, U2 = "1a665cf5-1f3f-4610-be00-b46ffa13a675", "87a9e986-307a-4680-8658-512cb15a410f"
    assert ilan_coz("https://boards.greenhouse.io/figma/jobs/5822886004?gh_jid=5822886004") \
        == ("greenhouse", "figma", "5822886004")
    assert ilan_coz("https://job-boards.eu.greenhouse.io/acme/jobs/12")[0] == "greenhouse_eu"
    assert ilan_coz(f"https://jobs.eu.lever.co/acme/{U1}/apply") == ("lever_eu", "acme", U1)
    assert ilan_coz(f"https://jobs.ashbyhq.com/Anima/{U2}") == ("ashby", "Anima", U2)
    assert ilan_coz("https://apply.workable.com/j/6D0FEF463C") == ("workable", "", "6D0FEF463C")
    assert ilan_coz("https://jobs.lever.co/palantir") is None and ilan_coz("https://acme.io/kariyer") is None
    assert ilan_kimligi("https://apply.workable.com/open-252/j/6D0FEF463C/") \
        == ilan_kimligi("https://apply.workable.com/j/6D0FEF463C") == "workable::6d0fef463c"
    assert ilan_kimligi("https://boards.greenhouse.io/figma/jobs/58?gh_jid=58") \
        == ilan_kimligi("https://job-boards.greenhouse.io/figma/jobs/58") == "greenhouse:figma:58"
    assert ilan_kimligi("https://Acme.io/Kariyer/7/#basvur") == "https://acme.io/kariyer/7"

    # verify_posting: açık ilan kaynağındaki adı, konumu ve tarihi alır; kapalı demek kanıt ister
    def ag(cevaplar):
        return lambda u: next(((k, json.dumps(g).encode() if not isinstance(g, bytes) else g, s or u)
                               for parca, (k, g, s) in cevaplar.items() if parca in u), (0, b"", u))

    gh1 = {"title": "Software Engineer, New Grad", "location": {"name": "London"},
           "first_published": "2026-09-07T09:24:28-04:00"}
    v = verify_posting("https://boards.greenhouse.io/acme/jobs/7", ag({"boards/acme/jobs/7": (200, gh1, "")}))
    assert v == {"durum": ACIK, "title": "Software Engineer, New Grad", "yer": "London", "yayin": "2026-09-07"}
    for kod, durum in ((404, KAPALI), (410, KAPALI), (0, BILINMIYOR), (429, BILINMIYOR), (503, BILINMIYOR)):
        assert verify_posting("https://boards.greenhouse.io/acme/jobs/7",
                              ag({"jobs/7": (kod, b"", "")}))["durum"] == durum, kod
    assert verify_posting("https://job-boards.eu.greenhouse.io/acme/jobs/7",
                          ag({"jobs/7": (404, b"", "")}))["durum"] == BILINMIYOR
    lv1 = {"text": "Associate AI Engineer", "createdAt": 1778700315006, "categories": {"location": "Toronto"}}
    v = verify_posting(f"https://jobs.lever.co/acme/{U1}", ag({f"postings/acme/{U1}": (200, lv1, "")}))
    assert (v["durum"], v["title"], v["yer"], v["yayin"]) == (ACIK, "Associate AI Engineer", "Toronto", "2026-05-13")
    pano_ab = {"jobs": [{"id": U2, "title": "Product Engineer ", "location": "Remote",
                         "publishedAt": "2025-04-17T14:51:06.582+00:00"}, {"id": "x", "title": "Diğer"}]}
    ab_link = f"https://jobs.ashbyhq.com/acme/{U2}"
    v = verify_posting(ab_link, ag({"job-board/acme": (200, pano_ab, "")}))
    assert (v["durum"], v["title"], v["yayin"]) == (ACIK, "Product Engineer", "2025-04-17")
    assert verify_posting(f"https://jobs.ashbyhq.com/acme/{U1}",
                          ag({"job-board/acme": (200, pano_ab, "")}))["durum"] == KAPALI
    # ilan panonun kesilen kısmında olabilir: kapalı denmez
    kesik_ab = json.dumps({"jobs": [{"id": "x", "title": "Diğer"}, {"id": U2, "title": "P"}]}).encode()[:-25]
    assert verify_posting(ab_link, ag({"job-board/acme": (200, kesik_ab, "")}))["durum"] == BILINMIYOR
    assert verify_posting(ab_link, ag({"job-board/acme": (404, b"", "")}))["durum"] == KAPALI
    wk1 = {"jobs": [{"title": "Product Engineer", "shortcode": "6D0F", "country": "Jordan",
                     "published_on": "2026-09-01"}]}
    kisa = "https://apply.workable.com/j/6D0F"
    v = verify_posting(kisa, ag({"/j/6D0F": (200, b"", "https://apply.workable.com/open-252/j/6D0F"),
                                 "accounts/open-252": (200, wk1, "")}))
    assert (v["durum"], v["yer"], v["yayin"]) == (ACIK, "Jordan", "2026-09-01")
    assert verify_posting(kisa, ag({"/j/6D0F": (200, b"", "https://apply.workable.com/oops")}))["durum"] == KAPALI
    assert verify_posting(kisa, ag({}))["durum"] == BILINMIYOR                     # ağ hatası
    assert verify_posting("https://apply.workable.com/open-252/j/ZZZZ",
                          ag({"accounts/open-252": (200, wk1, "")}))["durum"] == KAPALI
    assert verify_posting("https://acme.io/kariyer/7", ag({"kariyer/7": (404, b"", "")}))["durum"] == KAPALI
    assert verify_posting("https://www.linkedin.com/jobs/view/1", ag({"view/1": (999, b"", "")}))["durum"] \
        == BILINMIYOR
    print("jobboard self-test: OK")
