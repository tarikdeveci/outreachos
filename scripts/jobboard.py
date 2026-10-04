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


def board_postings(tur: str, ad: str, get_fn) -> list[dict]:
    """Panonun açık ilanları: [{title, link, yer}]. get_fn(url) → bytes | None."""
    raw = get_fn(PANO_API[tur].format(ad))
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else (data.get("jobs") if isinstance(data, dict) else [])
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
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("isListed") is False:
            continue
        kat = r.get("categories") if isinstance(r.get("categories"), dict) else {}
        yer = r.get("location") or kat.get("location") or \
            " ".join(str(r[k]) for k in ("city", "country") if r.get(k))
        if isinstance(yer, dict):
            yer = yer.get("name", "")
        title = str(r.get("title") or r.get("text") or "").strip()
        link = str(r.get("absolute_url") or r.get("hostedUrl") or r.get("jobUrl") or r.get("url") or "")
        if title and link.startswith("http"):
            out.append({"title": title, "link": link, "yer": str(yer or "").strip()})
    return out


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
             "title": (f"{x['title']} ({x['yer']})" if x["yer"] else x["title"])[:110]}
            for x in uygun[:limit]]


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
                    "location": {"name": "Berlin"}}], "meta": {"total": 1}}
    assert board_postings("greenhouse", "acme", lambda u: json.dumps(gh).encode()) == [
        {"title": "Junior Software Engineer", "link": "https://x.io/j/1", "yer": "Berlin"}]
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
    print("jobboard self-test: OK")
