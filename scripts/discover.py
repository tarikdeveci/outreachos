#!/usr/bin/env python3
"""
outreachos hibrit keşif scripti — GitHub Actions cron ile günlük çalışır.

Mekanik iş (arama, sayfa çekme, e-posta doğrulama, dedup) saf Python;
sadece "bu şirket uygun mu + taslak ne yazsın" kararı için küçük bir LLM
çağrısı yapılır. Böylece maliyet günde birkaç kuruşta kalır ve hiçbir
Claude plan limiti tüketilmez.

E-posta doğrulama: adres, şirketin kendi sayfasının HTML'inde birebir geçmeli
ve domainin MX kaydı olmalı. Bu, uydurulmuş adresi ve ölü domaini eler.
NOT: şirketin sitesinde yayınladığı ama artık okunmayan bir kutuyu (texinsight,
vendorside vakaları) eleyemez — orayı ancak bounce geldikten sonra öğreniyoruz,
o yüzden bounce'lar okunup adres "ölü" işaretleniyor ve bir daha denenmiyor.

Gmail izni `gmail.compose` + `gmail.readonly` + `gmail.send`: taslak yazar, gelen
kutusunu okur (yanıt/bounce takibi) ve YALNIZCA kullanıcının KENDİ adresine günlük
rapor yollar (report.send_self_report; hedef adres bağlı hesabın kendi adresiyle
eşleşmezse gönderim reddedilir). ŞİRKETLERE otomatik gönderim YOK — şirket outreach'i
her zaman sadece `create_draft` ile taslak kalır.

Env (GitHub Actions secrets):
  ANTHROPIC_API_KEY, SERPER_API_KEY,
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
Opsiyonel: DAILY_TARGET (varsayılan 8), DRY_RUN=1 (taslak/rapor göndermez),
           REPORT_TO (rapor adresi; boşsa bağlı hesabın kendi adresi kullanılır)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

import deliverability
import audit_drafts
import autosend

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)                # bu repo: karar motoru (src/pipeline.py)

# Veri kökü. Kod public, veri private repoda durur; cron veri reposunu checkout edip
# OUTREACHOS_DATA_DIR ile buraya işaret eder. Verilmezse kod reposunun kökü kullanılır
# — kodu ve veriyi tek klasörde tutan yerel/self-host kurulumu bozulmasın diye.
DATA_DIR = os.environ.get("OUTREACHOS_DATA_DIR") or CODE
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "outreach_log.csv")
OZET_DIR = os.path.join(DATA_DIR, "gunluk_ozet")

TARGET = int(os.environ.get("DAILY_TARGET", "8"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Zaman bütçesi: workflow 60 dk'da kesiliyor. Bütçe dolunca aday döngüsü
# durur ve o ana kadarki taslaklar yine de kaydedilir — bir run zaman aşımına
# takıldığında bütün işin çöpe gitmesi (ilk denemede oldu) kabul edilemez.
BUDGET_SEC = int(os.environ.get("BUDGET_SEC", "2400"))       # 40 dk
# Paralel işçi sayısı. Darboğaz HTTP beklemesi olduğu için thread yeterli;
# çok yükseltmek hedef sitelere karşı saldırgan olur ve rate-limit yer.
WORKERS = int(os.environ.get("WORKERS", "6"))
DELIVER_WINDOW_DAYS = int(os.environ.get("DELIVER_WINDOW_DAYS", "30"))
BOUNCE_WATCH = float(os.environ.get("BOUNCE_WATCH", "0.03"))
BOUNCE_CRITICAL = float(os.environ.get("BOUNCE_CRITICAL", "0.06"))
DELIVER_MIN_SENT = int(os.environ.get("DELIVER_MIN_SENT", "12"))
MAX_PENDING_DRAFTS = int(os.environ.get("MAX_PENDING_DRAFTS", "12"))
# Bekleyen taslak içerik-denetimi (audit_drafts): run başına en fazla kaç YENİ taslağa
# LLM doğrulaması (maliyet/zaman koruması). Mükerrer + cache'li kararlar bedava.
AUDIT_MAX_VERIFY = int(os.environ.get("AUDIT_MAX_VERIFY", "20"))
# Onaylı taslakların otomatik gönderimi (autosend.py). VARSAYILAN KAPALI: "1" yapılmadan
# hiçbir şirkete mail gitmez, kuyruk sadece raporda gösterilir.
AUTO_SEND = os.environ.get("AUTO_SEND") == "1"
AUTO_SEND_CAP = int(os.environ.get("AUTO_SEND_CAP", "5"))
_STARTED = time.monotonic()


UA = "Mozilla/5.0 (compatible; outreachos/1.0; +https://github.com/tarikdeveci/outreachos)"

# SERPER_API_KEY yokken her aramada aynı uyarıyı basmamak için
_SERPER_WARNED = False

def budget_left() -> float:
    return BUDGET_SEC - (time.monotonic() - _STARTED)


GENERIC_PREFIXES = ["info", "hello", "contact", "careers", "kariyer", "jobs",
                    "hr", "ik", "team", "iletisim", "bilgi", "recruitment", "career"]

# Şirket sitesi olmayan, sadece "hub" olarak kullanılacak alanlar
HUBS = ["webrazzi.com", "ycombinator.com", "itucekirdek.com", "startups.watch",
        "eu-startups.com", "sifted.eu", "tech.eu", "producthunt.com",
        "wellfound.com", "angel.co", "crunchbase.com", "linkedin.com",
        "startupcentrum.com", "ariteknokent.com.tr"]

# İş ilanı toplayıcıları, startup liste siteleri ve haber siteleri.
# Bunlar aramada bolca çıkıyor ama şirket değil — aday havuzuna girerlerse
# her run bunları tarayıp "e-posta bulunamadı" diye eliyor, zaman israfı.
AGGREGATORS = ["arc.dev", "jooble.org", "nodesk.co", "remoterocketship.com",
               "startup.jobs", "beststartup.us", "ycfounderlist.com", "vcbacked.co",
               "foundertrace.com", "ycstartupschools.com", "remoteok.com",
               "weworkremotely.com", "remotive.", "otta.com", "himalayas.app",
               "welcometothejungle.", "kariyer.net", "indeed.", "glassdoor.",
               "jobs.", "careers.", "duckduckgo.com", "bing.com", "yandex."]
MEDIA = ["cioupdate.com.tr", "turk-internet.com", "girisimin.com", "isvegirisim.com",
         "cozumpark.com", "startupistanbul.com", "dailysabah.com", "aa.com.tr",
         "hurriyet.", "milliyet.", "techcrunch.com", "forbes.", "bloomberg.",
         "turkiyetoday.com", "europesays.com", "kolaystartup.com"]
# CDN / statik varlık alt alanları — hiçbir zaman şirket sitesi değil
ASSET_PREFIXES = ("static.", "assets.", "cdn.", "fonts.", "img.", "images.",
                  "media.", "s3.", "storage.", "cdnjs.", "maps.", "ssl.", "fd.")
# Analitik/reklam/izleme — her sitenin HTML'inde geçiyor, şirket değil
TRACKERS = ["googletagmanager.com", "doubleclick.net", "google-analytics.com",
            "cleantalk.org", "hotjar.", "segment.", "mixpanel.", "sentry.io",
            "bit.ly", "goo.gl", "t.co", "recaptcha", "jquery", "bootstrapcdn",
            "unpkg.com", "jsdelivr.net", "typekit", "addthis", "sharethis"]

NOISE = ["google.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.",
         "medium.com", "github.com", "wikipedia.org", "apple.com", "play.google",
         "amazon.", "microsoft.com", "cloudflare.", "gravatar.", "gstatic.",
         "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com",
         "notion.so", "substack.com", "kpmg.com", "deloitte.", "pwc.",
         "ey.com", "mckinsey.", "meetup.com", "gmpg.org", "w3.org",
         "schema.org", "wordpress.", "wp.com", "innogate.org", "ku.edu.tr",
         "itu.edu.tr", "gov.tr", "tubitak."] + HUBS + AGGREGATORS + MEDIA + TRACKERS

# Doğrudan taranacak portföy/dizin sayfaları.
# Arama sorguları "startuplar hakkında yazı" döndürüyor, "startup sitesi" değil;
# bu sayfalar ise birebir şirket listesi olduğu için çok daha verimli.
#
# NOT: bazı teknopark siteleri (Teknopark İstanbul, ODTÜ, Ege, Cyberpark) firma
# listesini JavaScript ile yüklüyor; düz HTML çekince 0 link dönüyor. Listede
# tutuluyorlar çünkü zararsız ve site yapıları değişirse otomatik devreye girerler.
# Alan uzantısından ülke tahmini. Kesin değil (bir Alman şirketi .com kullanabilir)
# ama ülke bazlı daraltma için yeterli sinyal; belirsizler her zaman geçirilir.
TLD_COUNTRY = {
    "tr": "TR", "de": "DE", "fr": "FR", "nl": "NL", "uk": "GB", "es": "ES",
    "it": "IT", "se": "SE", "dk": "DK", "no": "NO", "fi": "FI", "pl": "PL",
    "pt": "PT", "be": "BE", "at": "AT", "ch": "CH", "ie": "IE", "cz": "CZ",
    "gr": "GR", "ro": "RO", "hu": "HU", "ee": "EE", "lt": "LT", "lv": "LV",
    "bg": "BG", "hr": "HR", "si": "SI", "sk": "SK", "eu": "EU",
}
# COUNTRIES="TR,DE,NL" gibi bir liste verilirse sadece o ülkeler işlenir.
# Boş bırakılırsa (varsayılan) hepsi işlenir. Uzantıdan ülkesi çıkarılamayan
# (.com/.io/.ai gibi) alanlar HER ZAMAN geçer — aksi halde havuzun çoğu elenir.
COUNTRIES = {c.strip().upper() for c in os.environ.get("COUNTRIES", "").split(",") if c.strip()}


def country_of(domain: str) -> str:
    parts = domain.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "co", "org", "net", "gov", "ac"):
        return TLD_COUNTRY.get(parts[-1], "??")      # ör. sirket.com.tr
    return TLD_COUNTRY.get(parts[-1], "??") if len(parts) >= 2 else "??"


def country_allowed(domain: str) -> bool:
    if not COUNTRIES:
        return True
    c = country_of(domain)
    return c == "??" or c in COUNTRIES


SEED_DIRECTORIES = [
    # --- Türkiye
    ("https://kworks.ku.edu.tr/girisimler/", "KWORKS portföy"),
    ("https://www.itucekirdek.com/en/startups", "İTÜ Çekirdek portföy"),
    ("https://itucekirdek.com/girisimler", "İTÜ Çekirdek girişimler"),
    ("https://startupcentrum.com/startups", "StartupCentrum dizini"),
    ("https://www.ariteknokent.com.tr/en/companies", "İTÜ ARI Teknokent"),
    ("https://www.eu-startups.com/category/turkey-startups/", "EU-Startups Türkiye"),
    ("https://www.teknoparkistanbul.com.tr/tr/firmalar", "Teknopark İstanbul"),
    ("https://endeavor.org.tr/girisimciler/", "Endeavor Türkiye"),
    ("https://www.odtuteknokent.com.tr/tr/firmalar", "ODTÜ Teknokent"),
    ("https://egeteknopark.com.tr/firmalar/", "Ege Teknopark (İzmir)"),
    ("https://cyberpark.com.tr/firmalar", "Bilkent Cyberpark"),
    # --- Avrupa / global
    # VC ve hızlandırıcı portföyleri birebir şirket listesi olduğu için çok verimli.
    # Çoğu Avrupa VC'si (Atomico, Balderton, Creandum, Northzone, Seedcamp, HV) listeyi
    # JavaScript ile yüklüyor, düz HTML'de 0 dönüyor — aşağıdakiler statik olanlar.
    ("https://www.antler.co/portfolio", "Antler portföy (global/EU)"),
    ("https://techstars.com/portfolio", "Techstars portföy"),
    ("https://joinef.com/companies/", "Entrepreneur First"),
    ("https://www.stationf.co/startups", "Station F (Fransa)"),
]


# ---------------------------------------------------------------- http
def _get(url: str, headers: dict | None = None, timeout: int = 20) -> bytes | None:
    req = Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.read(400_000)          # sayfa başına 400KB tavan
    except (HTTPError, URLError, TimeoutError, OSError):
        return None


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    req = Request(url, data=json.dumps(payload).encode(), method="POST",
                  headers={"Content-Type": "application/json", **headers})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post_form(url: str, data: dict, timeout: int = 20) -> dict:
    req = Request(url, data=urlencode(data).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- arama
def serper_search(query: str, num: int = 10) -> list[dict]:
    """Serper.dev üzerinden Google sonuçları.

    Google'ın kendi Custom Search JSON API'sinin yerine geçti: o API yeni
    müşterilere kapatıldı (her çağrı 403 döndürüyordu) ve 2027-01-01'de
    tamamen kapanıyor. Serper gerçek Google SERP'i döndürdüğü için
    `site:` operatörlü sorgular aynen çalışmaya devam ediyor.

    Hatayı yutmaz: anahtar yoksa/kota bittiyse sebebini basar, çünkü sessizce
    0 sonuç dönmek teşhisi imkânsız kılıyor — CSE'nin haftalarca fark
    edilmeden bozuk kalmasının sebebi tam olarak buydu.
    """
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        global _SERPER_WARNED
        if not _SERPER_WARNED:
            print("  ! SERPER_API_KEY tanımlı değil — arama DDG yedeğiyle dönüyor")
            _SERPER_WARNED = True
        return []
    try:
        data = _post_json("https://google.serper.dev/search",
                          {"q": query, "num": min(num, 100)},
                          {"X-API-KEY": key}, timeout=25)
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        msg = body[:200]
        try:
            msg = json.loads(body).get("message", msg)
        except json.JSONDecodeError:
            pass
        print(f"  ! Serper HTTP {e.code}: {msg}")
        if e.code in (401, 403):
            print("    → SERPER_API_KEY geçersiz. serper.dev/api-key'ten kontrol et.")
        elif e.code == 429:
            print("    → Kredi bitti veya hız limiti. serper.dev/dashboard'a bak.")
        return []
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        print(f"  ! Serper bağlantı hatası: {e}")
        return []

    items = data.get("organic", [])
    if not items:
        print(f"  ! Serper 0 organik sonuç: {query[:60]}")
    return [{"title": i.get("title", ""), "link": i.get("link", ""),
             "snippet": i.get("snippet", "")} for i in items[:num]]


def ddg_search(query: str, num: int = 10) -> list[dict]:
    """Anahtarsız yedek arama (DuckDuckGo HTML).

    Serper anahtarı yok/kotası dolu olduğunda sistemin tamamen durmaması için.
    HTML kazıdığı için Serper'dan kırılgan — DDG sayfa yapısını değiştirirse
    sessizce 0 döner, o yüzden birincil kaynak değil yedek."""
    try:
        req = Request("https://html.duckduckgo.com/html/",
                      data=urlencode({"q": query}).encode(),
                      headers={"User-Agent": UA,
                               "Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req, timeout=25) as r:
            html = r.read(600_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"  ! DDG hatası: {e}")
        return []

    out: list[dict] = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                         html, re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        # DDG bazen /l/?uddg=<encoded> ile sarmalıyor
        if "uddg=" in href:
            q = urlparse(href).query
            for part in q.split("&"):
                if part.startswith("uddg="):
                    href = unquote(part[5:])
                    break
        if href.startswith("http"):
            out.append({"title": title, "link": href, "snippet": ""})
        if len(out) >= num:
            break
    return out


def search(query: str, num: int = 10) -> list[dict]:
    """Serper varsa onu kullan, yoksa/başarısızsa DDG'ye düş."""
    res = serper_search(query, num)
    if res:
        return res
    res = ddg_search(query, num)
    if res:
        print(f"    (DDG yedeğinden {len(res)} sonuç)")
    return res


def todays_queries() -> list[str]:
    """Haftanın gününe göre kaynak rotasyonu — aynı kaynağı her gün tarayınca
    birkaç günde tükeniyor, bu yüzden gün gün farklı havuzlara giriyoruz."""
    wd = date.today().weekday()
    common = ["yeni yatırım alan Türk startup 2026 teknoloji"]
    by_day = {
        0: ["İTÜ Çekirdek portföy şirketleri", "Teknopark İstanbul yazılım şirketleri",
            "Ege Teknopark İzmir yazılım şirketi", "ODTÜ Teknokent yapay zeka şirketi"],
        1: ["site:ycombinator.com/companies AI hiring", "Work at a Startup junior engineer remote",
            "YC startup Turkey founder", "site:ycombinator.com/companies climate"],
        2: ["site:webrazzi.com yatırım turu 2026", "Startups.watch yatırım alan girişim 2026",
            "Türkiye startup seed yatırım Temmuz 2026"],
        3: ["site:eu-startups.com raised seed 2026 AI", "site:tech.eu funding round 2026",
            "European startup hiring remote junior engineer 2026"],
        4: ["climate tech startup Türkiye karbon yazılım", "ESG sürdürülebilirlik yazılım şirketi Türkiye",
            "site:producthunt.com AI product 2026", "carbon accounting software startup hiring"],
        5: ["KWORKS Koç Üniversitesi girişim portföy", "Endeavor Türkiye şirketleri teknoloji"],
        6: ["TÜBİTAK BiGG destekli girişim yazılım", "Bilkent Cyberpark yazılım şirketi"],
    }
    return by_day.get(wd, []) + common


# ---------------------------------------------------------------- e-posta doğrulama
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_emails(html: str) -> set[str]:
    """Sayfada BİREBİR geçen adresler. mailto: ve düz metin ikisi de sayılır."""
    found = set()
    for m in EMAIL_RE.findall(html or ""):
        e = m.strip().strip(".").lower()
        if any(e.endswith(x) for x in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            continue
        if "@2x" in e or "sentry" in e or "example." in e or "domain.com" in e:
            continue
        found.add(e)
    return found


def mx_ok(domain: str) -> bool:
    """DNS-over-HTTPS ile MX kaydı var mı — ölü/parked domainleri eler."""
    raw = _get("https://dns.google/resolve?" + urlencode({"name": domain, "type": "MX"}))
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return data.get("Status") == 0 and bool(data.get("Answer"))


CAREER_PATHS = ["/careers", "/kariyer", "/career", "/jobs", "/join-us",
                "/en/careers", "/tr/kariyer", "/company/careers"]


def find_career_page(domain: str) -> str | None:
    if budget_left() < 180:      # bütçe azken bu lüks aramayı atla
        return None
    """Şirketin kendi başvuru/kariyer sayfası varsa URL'ini döndürür.

    Bazı şirketler maile cevap vermiyor ama kendi sitelerinden başvuru alıyor;
    bu linkler günlük bildirimde ayrıca listeleniyor ki elle başvurulabilsin."""
    for p in CAREER_PATHS:
        url = f"https://{domain}{p}"
        raw = _get(url, timeout=6)
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace").lower()
        # 404 sayfaları da 200 dönebiliyor — içerikte kariyer sinyali arıyoruz
        if any(k in text for k in ("apply", "başvur", "pozisyon", "open role",
                                   "join our team", "açık pozisyon", "vacanc")):
            return url
    return None


def find_verified_email(domain: str) -> tuple[str | None, str]:
    """(email, kanıt) — adres şirketin kendi sayfasında geçmiyorsa None döner.
    ASLA info@<domain> gibi bir tahmin üretmez."""
    # Kısa timeout bilinçli: ölü/yavaş sitelerde 10 yol × 20s = tek şirket için
    # 200 saniye ediyordu ve run'ı zaman aşımına sokuyordu. Gerçekten çalışan bir
    # site 8 saniyede yanıt verir; vermiyorsa zaten sıradaki adaya geçmek daha verimli.
    paths = ["", "/contact", "/iletisim", "/contact-us", "/about", "/hakkimizda",
             "/careers", "/kariyer", "/en/contact", "/tr/iletisim"]
    seen: set[str] = set()
    for p in paths:
        for scheme in ("https://",):
            html_bytes = _get(f"{scheme}{domain}{p}", timeout=8)
            if not html_bytes:
                continue
            html = html_bytes.decode("utf-8", errors="replace")
            for e in extract_emails(html):
                if e.split("@")[-1].lower().endswith(domain.lower()):
                    seen.add(e)
            if seen:
                break
        if seen:
            break
    if not seen:
        return None, "sayfalarda e-posta bulunamadı"
    generic = [e for e in sorted(seen) if e.split("@")[0].lower() in GENERIC_PREFIXES]
    if not generic:
        return None, f"sadece kişiye özel adres bulundu ({sorted(seen)[0]}) — kural gereği kullanılmaz"
    if not mx_ok(domain):
        return None, f"{generic[0]} bulundu ama {domain} MX kaydı yok (ölü domain)"
    return generic[0], "sitede birebir geçiyor + MX doğrulandı"


# ---------------------------------------------------------------- aday toplama
def domain_of(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_noise(d: str) -> bool:
    if not d or d.startswith(ASSET_PREFIXES):
        return True
    return any(n in d for n in NOISE)


def harvest_links(url: str, label: str, cands: dict[str, dict], cap: int = 150) -> int:
    """Bir dizin/portföy sayfasındaki dış şirket linklerini aday havuzuna ekler.

    Bazı dizinler (ör. KWORKS) GitHub Actions IP'lerinden yavaş/kararsız yanıt
    veriyor — yerelde 44 domain dönen sayfa CI'da 0 dönebiliyor. Bu yüzden
    tarayıcı benzeri header'larla ve birkaç deneme ile çekiyoruz."""
    raw = None
    for attempt in range(3):
        raw = _get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        }, timeout=45)
        if raw:
            break
        time.sleep(2 * (attempt + 1))
    if not raw:
        print(f"    ! {label}: sayfa çekilemedi ({url})")
        return 0
    html = raw.decode("utf-8", errors="replace")
    added = 0
    for href in re.findall(r'href=["\'](https?://[^"\'>\s]+)', html)[:cap]:
        hd = domain_of(href)
        if hd and not is_noise(hd) and hd not in cands and domain_of(url) != hd:
            cands[hd] = {"domain": hd, "kaynak": label, "baslik": "", "ozet": ""}
            added += 1
    return added


def gather_candidates(queries: list[str], log: list[str]) -> dict[str, dict]:
    """Aday havuzu iki kaynaktan gelir:

    1. Portföy/dizin sayfaları (birincil) — bunlar birebir şirket listesi olduğu
       için verim çok yüksek.
    2. Arama sonuçları (ikincil) — çoğu zaman "startuplar hakkında yazı" döndürüp
       toplayıcı/haber sitesi getiriyor, o yüzden destek amaçlı.
    """
    cands: dict[str, dict] = {}

    # 1) Önceden toplanmış havuz (birincil kaynak).
    # Türk startup dizinleri GitHub Actions IP'lerini engellediği için bu havuz
    # yerelde `scripts/refresh_pool.py` ile toplanıp repoya commit'leniyor.
    pool_path = os.path.join(DATA_DIR, "candidate_pool.json")
    if os.path.exists(pool_path):
        try:
            with open(pool_path, encoding="utf-8") as f:
                data = json.load(f)
            for x in data.get("candidates", []):
                dom = x.get("domain")
                if dom and dom not in cands:
                    cands[dom] = {"domain": dom, "kaynak": x.get("kaynak", "havuz"),
                                  "baslik": "", "ozet": x.get("aciklama", ""),
                                  "ise_aliyor": x.get("ise_aliyor", False),
                                  "ekip": x.get("ekip"), "ulke": x.get("ulke", "??")}
            log.append(f"**Havuz** (`candidate_pool.json`, güncellendi "
                       f"{data.get('guncellendi','?')}): {len(cands)} domain\n")
        except (json.JSONDecodeError, OSError) as e:
            log.append(f"**Havuz okunamadı:** {e}\n")
    else:
        log.append("**Havuz yok** — `python scripts/refresh_pool.py` ile oluştur.\n")

    # 2) Dizinleri canlı çekmeyi yine de dene (CI'dan çoğu engelli, yerelde çalışır)
    log.append("**Dizin sayfaları (canlı):**\n")
    for url, label in SEED_DIRECTORIES:
        n = harvest_links(url, label, cands)
        log.append(f"  {label} → {n} yeni domain")
        time.sleep(0.4)

    log.append("\n**Arama sorguları (ikincil):**\n")
    for q in queries:
        results = search(q)
        found = 0
        for r in results:
            d = domain_of(r["link"])
            if is_noise(d):
                # Toplayıcı/haber değil de bilinen bir hub ise içindeki
                # şirket linklerini hasat et; gerisini tamamen atla.
                if any(h in d for h in HUBS):
                    found += harvest_links(r["link"], f"{d} (hub)", cands)
                continue
            if d not in cands:
                cands[d] = {"domain": d, "kaynak": q,
                            "baslik": r["title"], "ozet": r["snippet"]}
                found += 1
        log.append(f"  sorgu `{q}` → {len(results)} sonuç, {found} yeni domain")
        time.sleep(0.3)
    return cands


# ---------------------------------------------------------------- LLM
# ---------------------------------------------------------------- gmail
def google_access_token() -> str | None:
    cid, sec, ref = (os.environ.get("GMAIL_CLIENT_ID"), os.environ.get("GMAIL_CLIENT_SECRET"),
                     os.environ.get("GMAIL_REFRESH_TOKEN"))
    if not (cid and sec and ref):
        return None
    try:
        d = _post_form("https://oauth2.googleapis.com/token",
                       {"client_id": cid, "client_secret": sec,
                        "refresh_token": ref, "grant_type": "refresh_token"})
        return d.get("access_token")
    except (HTTPError, URLError, OSError) as e:
        print(f"! Gmail token alınamadı: {e}")
        return None


def create_draft(to: str, subject: str, body: str, token: str) -> str | None:
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    try:
        d = _post_json("https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                       {"message": {"raw": raw}}, {"Authorization": "Bearer " + token})
        return d.get("id")
    except (HTTPError, URLError, OSError) as e:
        print(f"    ! Taslak oluşturulamadı: {e}")
        return None


def send_draft(draft_id: str, token: str) -> str | None:
    """(gmail.send) Mevcut bir TASLAĞI gönderir → message id. autosend katmanı kullanır.

    ŞİRKETE gönderim yapan TEK yer burasıdır ve yalnızca şu zincirden geçmiş taslaklar için
    çağrılır: audit ✅GUVENLI (mükerrer değil + içerik doğrulandı) → bir gün veto penceresi →
    bounce guard izni → MX yeniden doğrulandı. Zincirin herhangi bir halkası koparsa çağrılmaz.
    """
    try:
        d = _post_json("https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
                       {"id": draft_id}, {"Authorization": "Bearer " + token})
        return d.get("id")
    except (HTTPError, URLError, OSError) as e:
        hint = " (gmail.send izni yok — token'ı yeni scope ile yenile)" \
            if (isinstance(e, HTTPError) and e.code in (401, 403)) else ""
        print(f"    ! Taslak gönderilemedi ({draft_id}): {e}{hint}")
        return None


def gmail_search(query: str, token: str, limit: int = 25) -> list[dict]:
    """(gmail.readonly) Sorguya uyan mesajların başlıklarını döndürür."""
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?"
           + urlencode({"q": query, "maxResults": limit}))
    raw = _get(url, {"Authorization": "Bearer " + token})
    if not raw:
        return []
    try:
        ids = [m["id"] for m in json.loads(raw).get("messages", [])]
    except (json.JSONDecodeError, KeyError):
        return []
    out = []
    for mid in ids:
        m = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
                 "?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
                 "&metadataHeaders=Date", {"Authorization": "Bearer " + token})
        if not m:
            continue
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            continue
        h = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
        out.append({"from": h.get("from", ""), "subject": h.get("subject", ""),
                    "date": h.get("date", ""), "snippet": d.get("snippet", "")})
    return out


def track_replies(state: dict, token: str) -> list[str]:
    """Bounce ve yanıtları gelen kutusundan okuyup state'e işler.

    Bounce'lar önemli: ölü bir adres bir kez bounce aldıysa bir daha o şirkete
    taslak açmanın anlamı yok. Adresi state'te işaretleyip bir daha denemiyoruz.
    """
    notes: list[str] = []
    contacted = state.get("companies_already_contacted", {})
    by_email = {v.get("email", "").lower(): k for k, v in contacted.items() if v.get("email")}

    for m in gmail_search("from:mailer-daemon newer_than:14d", token):
        blob = (m["snippet"] + " " + m["subject"]).lower()
        for email, firma in by_email.items():
            if email and email in blob:
                cur = contacted[firma].get("last_reply_seen") or ""
                if "BOUNCE" not in cur:
                    contacted[firma]["last_reply_seen"] = f"BOUNCE ({m['date'][:16]}): adres teslim edilemedi"
                    contacted[firma]["email_dead"] = True
                    notes.append(f"**{firma}** — `{email}` bounce aldı, ölü olarak işaretlendi")

    domains = {e.split("@")[-1] for e in by_email if "@" in e}
    for dom in list(domains)[:20]:
        for m in gmail_search(f"from:{dom} newer_than:14d -from:mailer-daemon", token):
            firma = next((f for e, f in by_email.items() if e.endswith("@" + dom)), None)
            if not firma:
                continue
            cur = contacted[firma].get("last_reply_seen") or ""
            if m["date"][:16] not in cur:
                contacted[firma]["last_reply_seen"] = f"YANIT ({m['date'][:16]}): {m['snippet'][:160]}"
                notes.append(f"**{firma}** — yanıt geldi: {m['subject'][:80]}")
    return notes


# ---------------------------------------------------------------- gönderilen/taslak dedup
def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


EMAIL_IN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def existing_draft_domains(token: str, own: str = "") -> tuple[set[str], int, list]:
    """Gmail Taslaklar'ı TEK geçişte tarar → (domainler, bekleyen_outreach_sayısı, parsed_drafts).
    parsed_drafts: audit_drafts.parse_draft çıktısı (id/to/domain/subject/body). Aynı firmaya
    2. taslak açmamak (domainler) + bekleyen-taslak cap'i (sayı) + mükerrer/içerik triyajı
    (audit_drafts) hepsi bu tek listeden beslenir. format=full: gövde içerik denetimi için gerekli;
    maliyet audit'in id-bazlı cache'i + AUDIT_MAX_VERIFY kotasıyla sınırlanır."""
    doms: set[str] = set()
    pending = 0
    parsed: list = []
    raw = _get("https://gmail.googleapis.com/gmail/v1/users/me/drafts?maxResults=100",
               {"Authorization": "Bearer " + token})
    if not raw:
        return doms, pending, parsed
    try:
        ids = [d["id"] for d in json.loads(raw).get("drafts", [])]
    except (json.JSONDecodeError, KeyError):
        return doms, pending, parsed
    for did in ids:
        m = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/drafts/{did}?format=full",
                 {"Authorization": "Bearer " + token})
        if not m:
            continue
        try:
            dj = json.loads(m)
        except json.JSONDecodeError:
            continue
        pd = audit_drafts.parse_draft(dj)
        parsed.append(pd)
        if pd["domain"]:
            doms.add(pd["domain"])
        if deliverability.is_outreach_recipient(pd["to"], own):
            pending += 1
    return doms, pending, parsed


def scan_sent(state: dict, token: str, own: str, sink=None) -> int:
    """Gönderilenler'deki her alıcıyı companies_already_contacted'a işler (yoksa).
    Böylece manuel/dışarıdan gönderilen başvurular da sayılır ve o firmaya BİR DAHA
    taslak açılmaz — 'aynı işe iki kez başvurma' güvencesi state'e değil Gmail'e dayanır."""
    contacted = state.setdefault("companies_already_contacted", {})
    # DOMAIN bazlı bilinen küme: aynı firmanın farklı adresi (iris@ vs hello@) mevcut
    # zengin kaydı (draft_id, yanıt geçmişi) ezmesin — dedup domain düzeyinde yeter.
    known_doms = {v.get("email", "").split("@")[-1].lower()
                  for v in contacted.values() if v.get("email")}
    added, page = 0, ""
    for _ in range(4):                       # ~4 sayfa × 50 = 200 sent, budget'ı yormaz
        url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?"
               + urlencode({"q": "in:sent newer_than:180d", "maxResults": 50})
               + (f"&pageToken={page}" if page else ""))
        raw = _get(url, {"Authorization": "Bearer " + token})
        if not raw:
            break
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            break
        for mid in [m["id"] for m in data.get("messages", [])]:
            m = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
                     "?format=metadata&metadataHeaders=To", {"Authorization": "Bearer " + token})
            if not m:
                continue
            try:
                mj = json.loads(m)
            except json.JSONDecodeError:
                continue
            payload = mj.get("payload", {})
            try:
                internal_ms = int(mj.get("internalDate", 0))
            except (TypeError, ValueError):
                internal_ms = 0
            for e in EMAIL_IN.findall(_header(payload, "To")):
                el, dom = e.lower(), e.lower().split("@")[-1]
                if sink is not None:
                    sink.append((el, internal_ms))
                if el == own.lower() or dom in known_doms:
                    continue
                firma = dom.split(".")[0]
                key = firma if firma not in contacted else dom   # isim çakışırsa ezme
                contacted[key] = {"date": "sent_detected", "channel": "sent_scan",
                                  "email": el, "last_reply_seen": None}
                known_doms.add(dom)
                added += 1
        page = data.get("nextPageToken", "")
        if not page:
            break
    return added


# Aynı firma en fazla bu kadar kez yeniden kuyruğa alınır. Kullanıcı taslağı tekrar
# tekrar silerse sonsuz "üret → sil → üret" döngüsü oluşurdu; ikinci denemeden sonra
# firma contacted'ta bırakılır (bir daha üretilmez), kaydı kuyrukta görünür kalır.
MAX_REDRAFT = 2


def requeue_deleted_drafts(state: dict, parsed_drafts: list, sent_events: list) -> list:
    """Gönderilmeden silinen taslakların firmalarını havuza geri kazandırır.

    Bir firma companies_already_contacted'a girdiği anda bir daha taslak açılmıyordu;
    taslağın gönderilip gönderilmediğine bakılmıyordu. Bekleyen-taslak freni (>12)
    kullanıcıyı backlog'u silmeye zorlayınca, silinen her taslağın firması "iletişime
    geçildi" damgasıyla kalıcı olarak kayboluyordu — hiç mail gitmemiş olmasına rağmen.

    Kural: draft_id var + taslak artık Gmail'de yok + Gönderilenler'de o adrese mail
    yok + bounce/yanıt kaydı yok → gönderilmeden silinmiş. Kayıt redraft_queue'ya
    taşınır, contacted'tan çıkar ve ertesi run'da yeniden değerlendirilir.

    Muhafazakâr taraf: bounce/yanıt izi olan ya da Gönderilenler'de görünen hiçbir
    kayda dokunulmaz — şüphedeyken firma 'iletişime geçilmiş' sayılır, çünkü aynı
    şirkete ikinci kez mail atmak, bir firmayı kaçırmaktan daha pahalı bir hata.

    Görünürlük sınırı: scan_sent 180 gün DEĞİL, en fazla ~200 mesaj tarıyor (4 sayfa).
    Günlük self-report mailleri de o kotayı yediği için tarama birkaç hafta öncesinde
    kesilebilir. O sınırın ötesindeki bir gönderim 'yok' gibi görünür ve firma haksız
    yere kuyruğa düşerdi — bu yüzden yalnızca taramanın GERÇEKTEN ulaştığı en eski
    mesajdan sonra oluşturulmuş taslaklar değerlendirilir."""
    contacted = state.setdefault("companies_already_contacted", {})
    queue = state.setdefault("redraft_queue", [])
    live_ids = {d.get("id") for d in (parsed_drafts or []) if d.get("id")}
    sent_addrs = {e.lower() for (e, _ms) in (sent_events or []) if e}
    seen = {str(q.get("firma", "")).lower(): q for q in queue}

    stamps = [ms for (_e, ms) in (sent_events or []) if ms]
    oldest_scanned = min(stamps) if stamps else None

    # Kuyruktaki bir firmaya yeniden taslak açıldıysa artık "bekliyor" değil. Kayıt
    # silinmiyor: deneme sayacı döngü korumasının belleği, onu kaybedersek aynı firma
    # sonsuza kadar üretilip silinebilir. Bunun yerine durumu güncelleniyor.
    for q in queue:
        nm = str(q.get("firma", ""))
        v = contacted.get(nm)
        if v and v.get("draft_id") in live_ids:
            q["durum"] = "yeniden oluşturuldu"

    def within_scan(day: str) -> bool:
        """Taslak tarihi, Gönderilenler taramasının ulaştığı aralıkta mı."""
        if oldest_scanned is None:
            return False                  # hiç sent taranamadı → hiçbir şeye dokunma
        try:
            ts = time.mktime(time.strptime(str(day)[:10], "%Y-%m-%d")) * 1000.0
        except (ValueError, TypeError):
            return False                  # tarih okunamadı → güvenli tarafta kal
        return ts >= oldest_scanned

    moved: list = []
    for name in list(contacted):
        v = contacted[name]
        did = v.get("draft_id")
        email = (v.get("email") or "").lower()
        if not did or did in live_ids:
            continue                      # taslak hiç açılmamış ya da hâlâ duruyor
        if v.get("last_reply_seen") or v.get("email_dead"):
            continue                      # bounce/yanıt izi → mail gerçekten gitmiş
        if email and email in sent_addrs:
            continue                      # Gönderilenler'de var → gitmiş
        if not within_scan(v.get("date", "")):
            continue                      # tarama o tarihe ulaşmadı → emin olamayız
        prev = seen.get(name.lower())
        tries = int((prev or {}).get("deneme", 0)) + 1
        durum = "bekliyor" if tries <= MAX_REDRAFT else "vazgeçildi (tekrar tekrar silindi)"
        if prev:
            prev.update({"deneme": tries, "son_tarih": v.get("date", ""), "durum": durum})
        else:
            queue.append({"firma": name, "eposta": v.get("email", ""),
                          "ilk_tarih": v.get("date", ""), "kanal": v.get("channel", ""),
                          "deneme": tries, "durum": durum,
                          "sebep": "taslak gönderilmeden silindi"})
        if tries > MAX_REDRAFT:
            continue                      # döngü koruması: contacted'ta bırak
        del contacted[name]
        moved.append(name)
    return moved


# ---------------------------------------------------------------- ana akış
def main() -> int:
    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    profile = state.get("profile", {})

    sys.path.insert(0, HERE)
    import drafting                                     # noqa: E402
    import report                                       # noqa: E402  self-report + ATS digest
    today = date.today().isoformat()

    # pipeline.py verisini src/db.py üzerinden çözer; db.py de OUTREACHOS_DATA_DIR'e
    # bakar. İkisi aynı env değişkenini okuduğu için karar motoru otomatik olarak
    # bizim state.json'ımızı görür — eskiden gereken kopyalama hack'i kalktı.
    sys.path.insert(0, os.path.join(CODE, "src"))
    try:
        import pipeline                                    # noqa: E402
    except ImportError:
        print("! pipeline.py bulunamadı — src/ yanında mı?")
        return 1

    token = google_access_token()

    reply_notes: list[str] = []
    draft_domains: set[str] = set()
    own = ""
    sent_events: list = []
    pending_count = 0
    parsed_drafts: list = []
    requeued: list = []            # token yoksa hiç kuyruğa alınmaz; rapor yine de okur
    health = {"state": "UNKNOWN", "sent": 0, "hard": 0, "soft": 0, "replies": 0,
              "bounce_rate": 0.0, "trend": "—", "window_days": DELIVER_WINDOW_DAYS,
              "watch": BOUNCE_WATCH, "critical": BOUNCE_CRITICAL,
              "min_sent": DELIVER_MIN_SENT, "note": "Gmail token yok"}
    if token:
        reply_notes = track_replies(state, token)
        for n in reply_notes:
            print(f"  ~ {n}")
        own = report.own_address(token) or profile.get("email", "")
        added = scan_sent(state, token, own, sink=sent_events)
        if added:
            print(f"  ~ {added} gönderilen mail state'e işlendi — o firmalara tekrar mail yok")
        draft_domains, pending_count, parsed_drafts = existing_draft_domains(token, own)
        if draft_domains:
            print(f"  ~ {len(draft_domains)} mevcut taslak domaini — aynı firmaya 2. taslak açılmayacak")
        # scan_sent + taslak listesi hazır: gönderilmeden silinen taslakların firmalarını
        # havuza geri al. contacted/seen_domains kümeleri AŞAĞIDA kuruluyor, o yüzden burada.
        requeued = requeue_deleted_drafts(state, parsed_drafts, sent_events)
        if requeued:
            print(f"  ↩ {len(requeued)} firma yeniden taranabilir: taslağı gönderilmeden "
                  f"silinmişti ({', '.join(requeued[:5])}{' …' if len(requeued) > 5 else ''})")
        health = deliverability.assess(
            gmail_search=lambda q: gmail_search(q, token, limit=60), token=token,
            contacted=state.get("companies_already_contacted", {}), sent_events=sent_events,
            own=own, window_days=DELIVER_WINDOW_DAYS, watch=BOUNCE_WATCH,
            critical=BOUNCE_CRITICAL, min_sent=DELIVER_MIN_SENT,
            history=state.get("deliverability", {}).get("history", []))

    run_target, guard_reasons = deliverability.circuit(
        TARGET, health, pending_count, MAX_PENDING_DRAFTS)
    banner_lines = deliverability.banner(
        health, pending_count, MAX_PENDING_DRAFTS, run_target, TARGET)
    for ln in banner_lines:
        print(ln)
    for reason in guard_reasons:
        print(f"  ⚠ {reason}")

    # --- Bekleyen taslak triyajı: mükerrer (Gönderilenler'e karşı) + içerik denetimi.
    # Backlog>12 iken keşif zaten durur (run_target=0); o run'ın zamanı bu denetime kalır.
    # İçerik LLM'i id-bazlı cache'li + run başına AUDIT_MAX_VERIFY ile sınırlı; DRY_RUN'da hiç çağrılmaz.
    audit_lines: list[str] = []
    send_lines: list[str] = []
    if token and parsed_drafts:
        sent_domains = {e.split("@")[-1] for (e, _ms) in sent_events
                        if deliverability.is_outreach_recipient(e, own)}
        audit_cache = state.setdefault("draft_audit", {})
        # Eski run'larda doğrulama hatası bir içerik kararıymış gibi cache'lenmişti;
        # o kayıtlar gövde değişmediği için bir daha hiç denetlenmezdi. Artık böyle bir
        # sonuç yazılmıyor (audit_drafts.audit_one), ama geçmişte yazılanları temizle.
        stale = [k for k, v in audit_cache.items()
                 if v.get("verdict") == audit_drafts.REVIEW
                 and any("otomatik doğrulanamadı" in str(r) for r in v.get("reasons", []))]
        for k in stale:
            del audit_cache[k]
        if stale:
            print(f"  ~ {len(stale)} taslak denetimi yeniden kuyruğa alındı "
                  "(önceki sonuç doğrulama hatasından geliyordu, içerik kararı değil)")
        audit_results = audit_drafts.audit(
            parsed_drafts, profile, sent_domains,
            verify_fn=(None if DRY_RUN else drafting.verify),
            numeric_fn=drafting.numeric_check,
            cache=audit_cache, max_verify=AUDIT_MAX_VERIFY)
        audit_lines = audit_drafts.summary_lines(audit_results)
        for ln in audit_lines:
            print(ln)
        live_ids = {d["id"] for d in parsed_drafts}
        state["draft_audit"] = {k: v for k, v in audit_cache.items() if k in live_ids}

        # --- Onaylı taslakların VETO PENCERELİ gönderimi (AUTO_SEND=1 değilse hiç göndermez).
        # Zincir: audit ✅GUVENLI → bir gün bekleme (kullanıcı silerse veto) → bounce guard →
        # MX yeniden doğrulama → gönder. Her halka bağımsız olarak gönderimi iptal edebilir.
        allowed, send_cap, send_reasons = autosend.gate(health, AUTO_SEND, AUTO_SEND_CAP)
        safe_ids = {r["id"] for r in audit_results if r["verdict"] == audit_drafts.SAFE}
        bekleyen = autosend.due(state, today)
        # VETO: kuyruktayken Gmail'den silinen taslak artık canlı değil → gönderilmez, düşürülür.
        vetolu = {i["id"] for i in bekleyen} - live_ids
        if vetolu and not DRY_RUN:
            autosend.drop(state, vetolu)
            print(f"  ~ {len(vetolu)} kuyruk öğesi taslağı silindiği için gönderilmedi (veto)")
        bekleyen = [i for i in bekleyen if i["id"] in live_ids]
        gonderilen: list = []
        # Bu run'da kuyruktan DÜŞÜRÜLEN id'ler. Kuyruğu yeniden doldururken bunlar hariç
        # tutulmazsa aynı taslak aynı run içinde geri kuyruğa girer: audit_results hâlâ
        # ✅GUVENLI diyor ve drop sonrası id artık "kuyrukta" sayılmıyor. Gönderilen 5 taslak
        # 2026-09-06'da tam olarak böyle geri kuyruğa düştü ve rapor onları "yarın gönderilecek"
        # diye listeledi (mükerrer gönderim olmaz — taslak artık canlı değil, ertesi gün veto
        # olarak düşer — ama kullanıcıya yanlış liste gösterir ve MX'i ölü adres sonsuza dek
        # kuyruğa girip düşmeye devam eder).
        islenen: set = set()
        if allowed and not DRY_RUN:
            for item in bekleyen[:send_cap]:
                if item["id"] not in safe_ids:      # bugünkü denetimde artık temiz değil
                    autosend.drop(state, {item["id"]})
                    islenen.add(item["id"])
                    print(f"    x {item['to']} — yeniden denetimde temiz çıkmadı, gönderilmedi")
                    continue
                if item["domain"] and not mx_ok(item["domain"]):   # bounce koruması
                    autosend.drop(state, {item["id"]})
                    islenen.add(item["id"])
                    print(f"    x {item['to']} — MX kaybolmuş (ölü adres), gönderilmedi")
                    continue
                mid = send_draft(item["id"], token)
                if mid:
                    autosend.record_sent(state, item, today, mid)
                    autosend.drop(state, {item["id"]})
                    islenen.add(item["id"])
                    gonderilen.append(item)
                    print(f"    ✉ gönderildi: {item['company']} ({item['to']})")
        # Bugün onaylananları yarına kuyrukla — kullanıcı raporda görüp veto edebilsin.
        kuyrukta = {i["id"] for i in state.get(autosend.QUEUE_KEY, [])} | islenen | vetolu
        yeni_kuyruk = autosend.pick(audit_results, AUTO_SEND_CAP, exclude_ids=kuyrukta)
        if yeni_kuyruk and not DRY_RUN:
            autosend.enqueue(state, yeni_kuyruk, today)
        send_lines = autosend.summary_lines(bekleyen, gonderilen, yeni_kuyruk,
                                            allowed, send_reasons, send_cap)
        for ln in send_lines:
            print(ln)

    contacted = {k.lower() for k in state.get("companies_already_contacted", {})}
    portal = {c.lower() for c in state.get("companies_logged_portal_only", [])}
    # seen_domains'e mevcut Gmail taslaklarının domainleri de eklenir: kullanıcının
    # bekleyen taslaklarıyla veya birbirleriyle çakışan yeni taslak açılmaz.
    seen_domains = {v.get("email", "").split("@")[-1].lower()
                    for v in state.get("companies_already_contacted", {}).values()
                    if v.get("email")} | draft_domains
    # bounce almış adreslerin domainleri bir daha denenmez
    dead_domains = {v.get("email", "").split("@")[-1].lower()
                    for v in state.get("companies_already_contacted", {}).values()
                    if v.get("email_dead")}

    log: list[str] = []
    log.append("## Taranan Kaynaklar ve Verim\n")
    cands = gather_candidates(todays_queries(), log)
    log.append(f"\n**Toplam aday domain: {len(cands)}**\n")

    if not token and not DRY_RUN:
        print("! Gmail token yok — taslak oluşturulamayacak, DRY_RUN gibi devam ediliyor")

    drafted, skipped = [], []
    # Adaylar birbirinden bağımsız olduğu için paralel işleniyor. Darboğaz
    # HTTP bekleme (site çekme, e-posta arama) — tek tek yapınca 8 taslak
    # ~35 dakika sürüyordu, aynı sürede 30 taslak çıkarmanın tek yolu bu.
    lock = threading.Lock()
    dur = threading.Event()

    def isle(d: str, c: dict) -> None:
        if dur.is_set():
            return
        name = d.split(".")[0]
        if not country_allowed(d):
            with lock:
                skipped.append((d, f"ülke filtresi dışı ({country_of(d)})"))
            return
        if d in dead_domains:
            with lock:
                skipped.append((d, "daha önce bounce aldı — adres ölü"))
            return
        if d in seen_domains or name in contacted or any(name in p for p in portal):
            with lock:
                skipped.append((d, "daha önce işlendi"))
            return

        raw = _get(f"https://{d}", timeout=10)
        if not raw:
            with lock:
                skipped.append((d, "site açılmadı"))
            return
        site_text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", errors="replace"))
        site_text = re.sub(r"\s+", " ", site_text)[:6000]

        email, kanit = find_verified_email(d)
        if not email:
            with lock:
                skipped.append((d, kanit))
            return
        if dur.is_set():                       # LLM'e girmeden son kontrol
            return

        verdict, neden = drafting.judge_draft_verify(c, site_text, profile, today)
        if not verdict:
            with lock:
                skipped.append((d, neden))
            if "HALÜSİNASYON" in neden:
                print(f"  x {d} → taslak reddedildi ({neden[:110]})")
            return

        decision = pipeline.decide({"firma": name, "title": "spekülatif outreach",
                                    "sektor": verdict.get("sektor", ""), "email": email,
                                    "email_verified": True})
        if not decision.get("include"):
            with lock:
                skipped.append((d, "pipeline: " + "; ".join(decision.get("reasons", []))[:120]))
            return

        draft_id = None
        if token and not DRY_RUN:
            draft_id = create_draft(email, verdict.get("konu", ""), verdict.get("govde", ""), token)
        hedef = verdict.get("hedef_kisiler") or []
        if isinstance(hedef, str):
            hedef = [h.strip() for h in re.split(r"[;,]", hedef) if h.strip()]
        with lock:
            drafted.append({"domain": d, "firma": name, "email": email, "kanit": kanit,
                            "sektor": verdict.get("sektor", ""), "proje": verdict.get("proje", ""),
                            "kaynak": c.get("kaynak", ""), "draft_id": draft_id,
                            "kariyer": find_career_page(d),
                            "konu": verdict.get("konu", ""),
                            "skor": verdict.get("uygunluk_skoru") or decision.get("skor"),
                            "skor_gerekce": verdict.get("skor_gerekce", ""),
                            "hedef_kisiler": hedef[:3],
                            "linkedin_mesaji": verdict.get("linkedin_mesaji", "")})
            n = len(drafted)
        print(f"  + {d} → {email} ({'taslak ' + str(draft_id) if draft_id else 'DRY_RUN'}) [{n}/{run_target}]")
        if n >= run_target:
            dur.set()

    if run_target <= 0:
        skipped.append(("(guard)", "; ".join(guard_reasons) or "mail sağlığı guard'ı durdurdu"))
    else:
        with ThreadPoolExecutor(max_workers=WORKERS) as havuz:
            # Öncelik: (1) şu an işe alan şirketler — soğuk mailin en sıcak hedefi,
            # (2) küçük ekipler — junior'ın etkisi büyük olur ve info@ adresini
            # genelde kurucu okur. Alfabetik sıralama 5800 adayda her gün aynı
            # baştaki isimlere takılmak demekti.
            def oncelik(kv):
                _, c = kv
                ekip = c.get("ekip") if isinstance(c.get("ekip"), int) else 9999
                return (0 if c.get("ise_aliyor") else 1, ekip, kv[0])

            isler = {havuz.submit(isle, d, c): d
                     for d, c in sorted(cands.items(), key=oncelik)}
            for is_ in as_completed(isler):
                try:
                    is_.result()
                except Exception as e:                                   # noqa: BLE001
                    with lock:
                        skipped.append((isler[is_], f"beklenmeyen hata: {e}"))
                if budget_left() < 120 and not dur.is_set():
                    print(f"  ! zaman bütçesi doldu — {len(drafted)} taslakla kapatılıyor "
                          f"(kalan adaylar yarın işlenecek)")
                    skipped.append(("(kalanlar)", "zaman bütçesi doldu"))
                    dur.set()

    # DRY_RUN state'i KİRLETMEMELİ: taslak oluşturulmadığı halde şirketi
    # "iletişime geçildi" diye işaretlersek, gerçek run onu atlar ve o şirkete
    # hiç mail gitmez. (Bu tam olarak bir kez başımıza geldi.)
    if DRY_RUN:
        print(f"\n[DRY_RUN] {len(drafted)} aday bulundu, state/log/özet YAZILMADI:")
        for x in drafted:
            print(f"   - {x['firma']} ({x['email']}) — {x['sektor']}")
        print(f"Bitti: {len(drafted)}/{TARGET} aday, {len(skipped)} elendi.")
        return 0

    # --- state + log güncelle
    for x in drafted:
        state.setdefault("companies_already_contacted", {})[x["firma"]] = {
            "date": today, "channel": "gmail_draft_speculative", "email": x["email"],
            "draft_id": x["draft_id"], "last_reply_seen": None}
    state["last_run_date"] = today
    state["total_drafts_created_lifetime"] = state.get("total_drafts_created_lifetime", 0) + len(drafted)
    state["daily_caps_note"] = (f"{today} hibrit run: {len(cands)} aday domain, "
                                f"{len(drafted)} taslak, {len(skipped)} elendi.")
    deliverability.history_push(state, health, today)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    with open(LOG_PATH, "a", encoding="utf-8", newline="") as f:
        for x in drafted:
            f.write(f'{today},{x["firma"]},{x["sektor"]},gmail_draft_speculative,'
                    f'{x["proje"]},TASLAK,{x["email"]}\n')

    # --- ikincil: ATS/portal digest (SEN başvuracaksın; taslak değil)
    ats_digest = report.gather_ats_digest(search, lambda t: pipeline.role_filter(t, profile)[0], log)

    os.makedirs(OZET_DIR, exist_ok=True)
    with open(os.path.join(OZET_DIR, f"{today}.md"), "w", encoding="utf-8") as f:
        f.write(f"# Günlük Özet — {today} (hibrit script)\n\n")
        f.write("## Mail Sağlığı\n\n")
        f.write("\n".join(banner_lines) + "\n\n")
        if audit_lines:
            f.write("## Bekleyen Taslak Triyajı\n\n")
            f.write("\n".join(audit_lines) + "\n\n")
        if send_lines:
            f.write("## Otomatik Gönderim (veto pencereli)\n\n")
            f.write("\n".join(send_lines) + "\n\n")
        f.write(f"## Gelen Yanıtlar / Bounce ({len(reply_notes)})\n\n")
        for n in reply_notes:
            f.write(f"- {n}\n")
        if not reply_notes:
            f.write("_Yeni yanıt veya bounce yok._\n")
        f.write(f"\n## Bugün Açılan Gmail Taslakları ({len(drafted)})\n\n")
        for x in drafted:
            kisiler = (" — LinkedIn: " + ", ".join(x["hedef_kisiler"])) if x.get("hedef_kisiler") else ""
            f.write(f"- **{x['firma']}** ({x['domain']}) — %{x.get('skor', '?')} uygun — {x['sektor']} "
                    f"— {x['email']} — proje: {x['proje']} — kaynak: {x['kaynak']}{kisiler}\n")
        if not drafted:
            f.write("_Bugün taslak açılmadı._\n")
        f.write(f"\n## ATS/Portal Üzerinden Başvurulacaklar ({len(ats_digest)})\n\n")
        for a in ats_digest:
            f.write(f"- **{a['firma']}** — {a['title']} — {a['link']}\n")
        if not ats_digest:
            f.write("_Bugün uygun yeni ATS ilanı bulunamadı._\n")
        queue = state.get("redraft_queue", [])
        bekleyen = [q for q in queue if q.get("durum", "bekliyor") == "bekliyor"]
        if requeued or bekleyen:
            f.write(f"\n## Tekrar Oluşturulacaklar ({len(bekleyen)})\n\n")
            f.write("Taslağı gönderilmeden silinen firmalar. Havuza geri alındılar; "
                    "fren kalkınca sıraya girip yeniden taslak açılacak.\n\n")
            if requeued:
                f.write(f"Bu run'da eklenen: {len(requeued)}\n\n")
            for q in bekleyen[:40]:
                f.write(f"- **{q.get('firma','')}** — {q.get('eposta','')} "
                        f"(ilk: {q.get('ilk_tarih','?')}, deneme {q.get('deneme',1)})\n")
            if len(bekleyen) > 40:
                f.write(f"- … ve {len(bekleyen) - 40} firma daha\n")
            done = len(queue) - len(bekleyen)
            if done:
                f.write(f"\n_{done} firmanın taslağı yeniden oluşturuldu._\n")
        f.write(f"\n## Elenenler ({len(skipped)})\n\n")
        for d, why in skipped[:80]:
            f.write(f"- `{d}` — {why}\n")
        f.write("\n" + "\n".join(log) + "\n")

    # --- Günlük bildirim: iki kanal.
    # (1) GERÇEK MAIL: rapor kullanıcının KENDİ Gmail adresine gönderilir
    #     (report.send_self_report; gmail.send, yalnızca kendi adresine — şirketlere DEĞİL).
    # (2) YEDEK: run_summary.md → GitHub issue (workflow açar, GitHub mail atar). Token'da
    #     gmail.send izni yoksa (yenilenmediyse) (1) atlanır ama (2) yine de eline ulaşır.
    lines = list(banner_lines) + ["", f"**{len(drafted)} yeni taslak** Gmail'de hazır. Gönderme kararı sende.  ",
             f"**{len(ats_digest)} ATS/portal ilanı** — bunlara SEN başvuracaksın.\n"]
    if audit_lines:
        lines += ["### Bekleyen taslak triyajı (mükerrer + içerik)"] + audit_lines + [""]
    if send_lines:
        lines += ["### Otomatik gönderim (veto pencereli)"] + send_lines + [""]
    if reply_notes:
        lines.append("### Yanıt / bounce\n")
        lines += [f"- {n}" for n in reply_notes] + [""]
    if drafted:
        lines.append("### Taslaklar (Gmail'de hazır)\n")
        for x in drafted:
            lines.append(f"**{x['firma']}** — %{x.get('skor','?')} uygun — {x['sektor']}  ")
            lines.append(f"`{x['email']}` · konu: _{x['konu']}_  ")
            lines.append(f"kaynak: {x['kaynak']} · eşleşen proje: {x['proje'][:120]}  ")
            if x.get("hedef_kisiler"):
                lines.append(f"👤 LinkedIn'de hedefle: {', '.join(x['hedef_kisiler'])}  ")
            if x.get("kariyer"):
                lines.append(f"🔗 **Kendi başvuru sayfası var:** {x['kariyer']}  ")
            lines.append("")
    if ats_digest:
        lines.append("### ATS/Portal üzerinden başvurulacaklar (sen başvur)\n")
        lines += [f"- **{a['firma']}** — {a['title']} — {a['link']}" for a in ats_digest] + [""]
    li = report.build_linkedin_targets(drafted)
    if li:
        lines.append("### LinkedIn — bugün elle ulaş (en uygun 5 firma; hesabın güvende)\n")
        for t in li:
            lines.append(f"**{t['firma']}** (%{t['skor']} uygun)  ")
            lines += [f"- {r['rol']}: {r['url']}" for r in t["roller"]]
            if t["mesaj"]:
                lines.append(f"_hazır not: {t['mesaj']}_  ")
            lines.append("")
    kariyerli = [x for x in drafted if x.get("kariyer")]
    if kariyerli:
        lines.append("### Kendi sitesinden de başvurulabilecekler\n")
        lines += [f"- [{x['firma']}]({x['kariyer']})" for x in kariyerli] + [""]
    lines.append(f"---\nHavuzda kalan işlenmemiş aday: yaklaşık {len(cands) - len(skipped) - len(drafted)}. "
                 f"Havuz tükenirse yerelde `python scripts/refresh_pool.py` çalıştır.")
    with open(os.path.join(DATA_DIR, "run_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # --- (1) raporu kullanıcının KENDİ adresine gönder
    if token:
        to = os.environ.get("REPORT_TO") or report.own_address(token) or profile.get("email", "")
        text = report.build_report_text(
            today, profile, drafted, ats_digest, reply_notes, skipped,
            banner_lines=banner_lines, audit_lines=audit_lines, send_lines=send_lines)
        subject = f"İş arama raporu {today}: {len(drafted)} taslak, {len(ats_digest)} ATS"
        rid = report.send_self_report(to, subject, text, token) if to else None
        durum = f"gönderildi ({rid})" if rid else "gönderilemedi — GitHub issue yedeği devrede"
        print(f"  ✉ Rapor {durum} → {to}")

    print(f"\nBitti: {len(drafted)}/{TARGET} taslak, {len(ats_digest)} ATS, {len(skipped)} elendi.")
    return 0


def _self_test() -> int:
    """`python scripts/discover.py --self-test` — ağsız saf mantık testleri."""
    ms = lambda day: time.mktime(time.strptime(day, "%Y-%m-%d")) * 1000.0   # noqa: E731

    def fresh():
        return {"companies_already_contacted": {
            "canli":      {"email": "info@canli.com", "draft_id": "D1", "date": "2026-08-01", "last_reply_seen": None},
            "silinmis":   {"email": "info@silinmis.com", "draft_id": "D2", "date": "2026-08-02", "last_reply_seen": None},
            "gonderilmis": {"email": "info@gonder.com", "draft_id": "D3", "date": "2026-08-03", "last_reply_seen": None},
            "bounce":     {"email": "info@bounce.com", "draft_id": "D4", "date": "2026-08-04", "last_reply_seen": "BOUNCE: adres yok"},
            "yanit":      {"email": "info@yanit.com", "draft_id": "D5", "date": "2026-08-05", "last_reply_seen": "YANIT (...)"},
            "taslaksiz":  {"email": "info@yok.com", "draft_id": None, "date": "2026-08-06", "last_reply_seen": None},
            "tarama_disi": {"email": "info@eski.com", "draft_id": "D9", "date": "2026-05-01", "last_reply_seen": None},
        }}

    live = [{"id": "D1"}]                                    # sadece D1 hâlâ Gmail'de
    sent = [("info@gonder.com", ms("2026-08-03")), ("x@y.com", ms("2026-07-20"))]

    s = fresh()
    moved = requeue_deleted_drafts(s, live, sent)
    assert moved == ["silinmis"], moved
    c = s["companies_already_contacted"]
    assert "silinmis" not in c                               # gönderilmeden silindi → havuza
    assert {"canli", "gonderilmis", "bounce", "yanit", "taslaksiz", "tarama_disi"} <= set(c)
    assert s["redraft_queue"][0]["deneme"] == 1

    # Gönderilenler hiç taranamadıysa hiçbir şeye dokunma (fail-safe)
    assert requeue_deleted_drafts(fresh(), live, []) == []

    # Tarama penceresinden eski kayıt korunur (scan_sent ~200 mesajda kesiliyor)
    assert "tarama_disi" in s["companies_already_contacted"]

    # Döngü koruması: MAX_REDRAFT'tan sonra firma contacted'ta kalır, kuyrukta tek kayıt
    for tur in range(2, 5):
        s["companies_already_contacted"]["silinmis"] = {
            "email": "info@silinmis.com", "draft_id": f"D2-{tur}",
            "date": "2026-08-02", "last_reply_seen": None}
        requeue_deleted_drafts(s, live, sent)
    assert s["redraft_queue"][0]["deneme"] == 4
    assert "silinmis" in s["companies_already_contacted"]
    assert len([q for q in s["redraft_queue"] if q["firma"] == "silinmis"]) == 1
    assert s["redraft_queue"][0]["durum"].startswith("vazgeçildi")

    # Yeniden taslak açılan firma raporda "bekliyor" görünmemeli
    s2 = fresh()
    requeue_deleted_drafts(s2, live, sent)
    assert s2["redraft_queue"][0]["durum"] == "bekliyor"
    s2["companies_already_contacted"]["silinmis"] = {
        "email": "info@silinmis.com", "draft_id": "D1",      # D1 canlı taslaklarda
        "date": "2026-08-02", "last_reply_seen": None}
    requeue_deleted_drafts(s2, live, sent)
    assert s2["redraft_queue"][0]["durum"] == "yeniden oluşturuldu"
    assert s2["redraft_queue"][0]["deneme"] == 1             # sayaç korundu

    # --- Serper cevabı doğru ayrıştırılıyor mu (ağa çıkmadan) ---
    # CSE'den geçerken sessizce yanlış alan adı okumak, aramanın haftalarca
    # bozuk kalmasıyla aynı sınıfta bir hata olurdu; o yüzden şekli sabitliyoruz.
    global _post_json, _SERPER_WARNED
    gercek_post = _post_json  # noqa: N806
    try:
        _post_json = lambda *a, **k: {"organic": [
            {"title": "A", "link": "https://a.com", "snippet": "s1"},
            {"title": "B", "link": "https://b.com", "snippet": "s2"},
            {"title": "C", "link": "https://c.com", "snippet": "s3"}]}
        os.environ["SERPER_API_KEY"] = "test"
        r = serper_search("q", 2)
        assert r == [{"title": "A", "link": "https://a.com", "snippet": "s1"},
                     {"title": "B", "link": "https://b.com", "snippet": "s2"}], r
        _post_json = lambda *a, **k: {}                      # organic yoksa boş liste
        assert serper_search("q", 5) == []
        del os.environ["SERPER_API_KEY"]                     # anahtarsız → boş, çökmez
        _SERPER_WARNED = False
        assert serper_search("q", 5) == []
    finally:
        _post_json = gercek_post
        os.environ.pop("SERPER_API_KEY", None)

    print("discover self-test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
