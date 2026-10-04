"""
Taslak üretimi ve halüsinasyon denetimi.

Üç ayrı adım, üç ayrı sorumluluk — hepsini tek çağrıda yapmak kaliteyi düşürüyordu:

  1. judge()   — "bu şirket makul bir hedef mi" (Haiku, ucuz, çok çağrılıyor)
  2. draft()   — taslak metnini yaz (Sonnet, sadece elemeden geçenler için)
  3. verify()  — "bu metindeki her iddia profilde var mı" (Sonnet, bağımsız göz)

Dördüncü adım sonradan eklendi: repair() denetimden kalan BEKLEYEN bir taslağı en az
değişiklikle düzeltir, sonucu yine verify() denetler (repair_and_verify).

3. adım neden ayrı: yazan modele "uydurma" demek yetmedi. Gerçek bir vakada
Haiku, profilde hiç olmayan "fizyoterapi platformunda ürün geliştirdim" cümlesini
kurdu ve mail gerçek bir şirkete gitti. Aynı çağrı içinde kendi çıktısını
denetlemesini istemek güvenilir değil; ayrı bir çağrı, sadece "şu metni şu
profille karşılaştır" işine odaklanınca bunu yakalıyor.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numeric
from numeric import foreign_numbers, numeric_check, strip_urls  # noqa: F401  (dışarıdan drafting.* diye çağrılıyor)

# Model ID'leri tarih ekisiz yazilir. claude-sonnet-4-5 emekliye ayrildi ve her
# cagriya HTTP 400 donuyor; guncel karsiligi claude-sonnet-5. Ucuz/pahali
# kademelendirme korunuyor: eleme Haiku (~20 cagri/gun), taslak+dogrulama Sonnet.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5")
DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "claude-sonnet-5")
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "claude-sonnet-5")
KREDI_BITTI = False          # Anthropic "credit balance is too low" döndü; run boyunca çağrı yapılmaz


def _call(model: str, system: str, user: str, key: str, max_tokens: int = 1500,
          temperature: float | None = None) -> dict | None:
    """Modelin JSON cevabı; `key` cevapta olması gereken anahtardır (bkz. last_object)."""
    global KREDI_BITTI
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or KREDI_BITTI:
        return None
    istek = {"model": model, "max_tokens": max_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]}
    if temperature is not None:
        istek["temperature"] = temperature
    payload = json.dumps(istek).encode()
    req = Request("https://api.anthropic.com/v1/messages", data=payload,
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"})
    try:
        with urlopen(req, timeout=90) as r:
            data = json.load(r)
    except HTTPError as e:
        # Gövdeyi bas: yalın "400 Bad Request" emekli model ID'sini gizliyordu ve
        # hata sessizce "içerik doğrulanamadı" verdict'ine dönüşüyordu.
        govde = e.read().decode(errors="replace")
        # Kredi bitince her çağrı aynı 400'ü alıyordu (bir run'da 641 kez); ilk hatada kesilir,
        # kalan adımlar çağrı yapmadan None döner, run raporu bunu açıkça yazar.
        KREDI_BITTI = "credit balance" in govde.lower()
        print(f"    ! {model} hatası: HTTP {e.code}: {govde[:200]}")
        return None
    except (URLError, TimeoutError, OSError) as e:
        print(f"    ! {model} bağlantı hatası: {e}")
        return None
    # Buradan sonrası da sessizce None dönüyordu: çağrı BAŞARILI olup cevap JSON
    # olarak okunamadığında denetim "içerik otomatik doğrulanamadı" diyor, ama
    # log'da tek satır hata olmuyor — yani teşhis edilemiyor. HTTP yolu konuşkan
    # yapılmıştı, ayrıştırma yolları unutulmuştu. stop_reason da basılıyor çünkü
    # en olası sebep max_tokens: cevap ortasında kesilince JSON kapanmıyor.
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    stop = data.get("stop_reason", "?")
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        print(f"    ! {model} JSON döndürmedi (stop_reason={stop}): {text[:160]!r}")
        return None
    try:
        # strict=False: model gövdedeki satır sonunu kaçışsız yazınca taslak boşa gidiyordu.
        return json.loads(text[s:e + 1], strict=False)
    except json.JSONDecodeError as err:
        son = last_object(text, key, s)
        if son is None:
            print(f"    ! {model} JSON ayrıştırılamadı (stop_reason={stop}, {err}): "
                  f"{text[s:s + 160]!r}")
        return son


def last_object(text: str, key: str, i: int = 0) -> dict | None:
    """Metindeki, `key` anahtarını taşıyan son tam JSON nesnesi. Model nesneyi kapatıp yazmaya
    devam edebiliyor (açıklama ya da düzelttiği ikinci nesne); temperature=0 ile aynı aday her
    gün aynı yerde düşerdi. Anahtar şartı açıklamadaki parça nesneyi ({"sektor": ...}) ve kesik
    dış nesnenin içindekini dışarıda bırakır. Denetimde (`temiz`) kirli hüküm her zaman kazanır:
    "şu cümle çıkarılırsa temiz olur" diye yazılan ikinci nesne sorunu gizlemesin."""
    dec, son, kirli, i = json.JSONDecoder(strict=False), None, None, text.find("{", i)
    while i >= 0:
        try:
            obj, j = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and key in obj:
            son = obj
            if key == "temiz" and obj["temiz"] is False:
                kirli = obj
        i = text.find("{", j) if isinstance(obj, dict) and key in obj else text.find("{", i + 1)
    return kirli or son


# ---------------------------------------------------------------- sayfa metni
_KOD_BLOGU = re.compile(r"<!--.*?-->|<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.S | re.I)
_META = re.compile(r"<meta\b[^>]*>", re.I)


def page_text(page: str, limit: int = 6000) -> str:
    """Sayfanın modele verilecek düz metni: script, stil, svg ve yorum blokları atılır,
    meta açıklama başa alınır.

    Yalnızca etiketler silinince JS ağırlıklı sitelerde modele giden ilk 3000 karakter
    font tanımı ve izleme koduydu: eleme adımı şirketi "metin bozuk" diye düşürüyor,
    geçenlerde de mail şirketin ne yaptığını görmeden yazılıyordu."""
    ozet = ""
    for tag in _META.findall(page[:60000]):
        if re.search(r"""(?:name|property)=["'](?:og:)?description["']""", tag, re.I):
            m = re.search(r"""content=(["'])(.*?)\1""", tag, re.S | re.I)
            if m and m.group(2).strip():
                ozet = m.group(2).strip() + ". "
                break
    govde = re.sub(r"<[^>]+>", " ", _KOD_BLOGU.sub(" ", page))
    return re.sub(r"\s+", " ", unescape(ozet + govde)).strip()[:limit]


# ---------------------------------------------------------------- 1) eleme
JUDGE_SYSTEM = (
    "Bir şirketin web sitesinden alınmış metin veriliyor. Tek işin: bu şirket, "
    "junior seviye bir yazılım/AI/ürün insanı için makul bir spekülatif başvuru "
    "hedefi mi karar vermek.\n"
    "GENİŞ DAVRAN: yazılım geliştiren, dijital ürünü olan, teknoloji üreten her "
    "şirket uygundur (robotik, IoT, biyoteknoloji yazılımı, enerji, lojistik, "
    "endüstriyel SaaS, e-ticaret altyapısı, tüketici ve sosyal uygulamalar, fintech, "
    "ödeme ve kart ürünleri, pazaryeri, oyun dahil). Ürününü bir uygulama ya da platform "
    "üzerinden sunan şirket, sattığı şey yazılım olmasa da (kart, sigorta, teslimat, "
    "topluluk) teknoloji şirketidir.\n"
    "SADECE şunlara uygun=false ver: (a) hiç yazılım/teknoloji üretmeyen işletme, "
    "(b) ANA ürünü savunma sanayi ya da siber güvenlik olan şirket (güvenlik, genel amaçlı "
    "bir ürünün kullanım alanlarından ya da özelliklerinden yalnızca biriyse uygundur), "
    "(c) şirket değil (dernek, üniversite, haber sitesi, yatırım fonu), (d) metin şirketin "
    "ne yaptığını anlamaya yetmeyecek kadar boş/bozuk.\n"
    "Kararsız kaldığında uygun=true ver.\n"
    'SADECE şu JSON: {"uygun": true/false, "gerekce": "...", "sektor": "..."}'
)


def judge(company: dict, site_text: str) -> dict | None:
    # temperature 0: sınırdaki şirket bir run'da geçip ertesi gün elenmesin.
    return _call(JUDGE_MODEL, JUDGE_SYSTEM,
                 f"ŞİRKET: {company['domain']}\nSite metni:\n{site_text[:3000]}",
                 "uygun", max_tokens=400, temperature=0)


# ---------------------------------------------------------------- 2) yazma
DRAFT_SYSTEM = (
    "Bir yazılım/AI mühendisinin iş arama outreach mailini ONUN AĞZINDAN yazıyorsun.\n\n"
    "MUTLAK KURAL — UYDURMA YOK: Sadece profilde AÇIKÇA yazan deneyim, proje, "
    "sertifika ve metriklerden bahsedebilirsin. Profilde geçmeyen bir sektör "
    "deneyimi, müşteri, kullanıcı sayısı, gelir, büyüme oranı ya da rol ASLA yazma. "
    "Bir alana 'ilgi duymak' ile o alanda 'deneyim sahibi olmak' farklı şeylerdir — "
    "profilde deneyim yoksa ilgi olarak yaz, deneyim gibi sunma.\n"
    "Profildeki bir metriği çarpıtma: 'ortalama 0.56, pik 0.81' ise pik değeri tek "
    "başına başlık yapma. Emin değilsen sayıyı hiç yazma. Şirket hakkında sayı "
    "yazacaksan yalnızca site metninde geçen sayıyı, oradaki haliyle yaz; site metninde "
    "olmayan bir sayıyı şirkete de yakıştırma.\n\n"
    "ZAMAN KİPİ: Bugünün tarihi veriliyor. Bitiş tarihi geçmiş işleri geçmiş zamanla "
    "yaz ('stajımda çalıştım'), 'şu anda çalışıyorum' deme. Mezuniyet geçtiyse "
    "'mezunuyum' de.\n"
    "AĞIZ: Birinci tekil şahıs ('kurdum', 'geliştirdim'). Başvurandan üçüncü şahısla "
    "bahsetme. Tek kişilik projelerde 'kurduk' değil 'kurdum'.\n"
    "BİÇİM: ~150 kelime, samimi ama profesyonel. Şirketin gerçekten ne yaptığına dair "
    "somut bir detay geçir. Sektöre en uygun 1-2 projeyi bağla. Profilde "
    "'targeting.role_summary' varsa başvuranın aradığı rol odur: yeri gelirse tek cümleyle, "
    "şirketin işine bağlayarak söyle; oradan deneyim ya da başarı iddiası çıkarma, maaştan "
    "söz etme. Türkçe yaz (şirket "
    "yabancıysa İngilizce). Linkleri düz metin (ornek.com). Klişe, abartılı övgü, "
    "telefon numarası yok. Kapanış mailin diliyle aynı olsun.\n\n"
    "AYRICA (mail dışında, rapor için):\n"
    "- uygunluk_skoru: 0-100, başvuranın profiliyle şirketin örtüşmesi (85+ çok güçlü, "
    "60-84 makul, 60 altı zayıf) + tek cümlelik skor_gerekce.\n"
    "- hedef_kisiler: LinkedIn'de MESAJ atılacak 1-3 ROL ünvanı (ör. 'Head of AI', "
    "'Engineering Manager', 'Technical Recruiter'). GERÇEK İSİM/E-POSTA UYDURMA, sadece rol.\n"
    "- linkedin_mesaji: LinkedIn için 1-2 cümlelik kısa bağlantı notu (aynı uydurma-yok kuralı).\n"
    "- cv: profilde 'cv_links' varsa, şirketin işine ve aranan role en uygun CV'nin ANAHTARI "
    "(cv_links içindeki anahtarlardan biri, aynen). CV linkini gövdeye yazma, sistem ekler.\n\n"
    'SADECE şu JSON: {"proje": "...", "konu": "...", "govde": "...", "uygunluk_skoru": 0-100, '
    '"skor_gerekce": "...", "hedef_kisiler": ["rol1", "rol2"], "linkedin_mesaji": "...", '
    '"cv": "..."}'
)


def draft(company: dict, site_text: str, profile: dict, sektor: str,
          today: str, duzeltme: str = "") -> dict | None:
    user = (f"BUGÜNÜN TARİHİ: {today}\n\n"
            f"ŞİRKET: {company['domain']} ({sektor})\nSite metni:\n{site_text[:3000]}\n\n"
            f"--- BAŞVURAN PROFİLİ (tek gerçek kaynak) ---\n"
            f"{json.dumps(profile, ensure_ascii=False)}")
    if duzeltme:
        user += (f"\n\n--- ÖNCEKİ DENEMEN REDDEDİLDİ ---\n{duzeltme}\n"
                 "Bu sorunları gidererek yeniden yaz. Şüphelendiğin iddiayı "
                 "yazmaktansa çıkar.")
    # 1500 → 4000: aynı model, aynı kesilme riski (bkz. verify). Taslak JSON'u mail
    # gövdesi + skor + hedef roller + LinkedIn notu taşıyor, yani verify'dan uzun.
    return _call(DRAFT_MODEL, DRAFT_SYSTEM, user, "govde", max_tokens=4000)


def attach_cv(d: dict, profile: dict) -> None:
    """Modelin seçtiği CV'nin linkini gövdenin sonuna ekler. Linki model yazmaz: uzun bir
    adresi harf harf kopyalarken bozabilir; seçimi model yapar, metni profil verir."""
    link = (profile.get("cv_links") or {}).get(str(d.get("cv") or ""))
    if link and link not in d.get("govde", ""):
        d["govde"] = d["govde"].rstrip() + f"\n\nCV: {link}"


# ---------------------------------------------------------------- 3) doğrulama
VERIFY_SYSTEM = (
    "Bir iş başvurusu mailini, başvuranın profiliyle karşılaştırıyorsun. "
    "Tek işin: maildeki her somut iddianın profilde karşılığı var mı denetlemek.\n\n"
    "DESTEKLENMEYEN sayılır:\n"
    "- Profilde olmayan bir sektörde deneyim iddiası (ör. profilde fizyoterapi "
    "projesi yokken 'fizyoterapi platformunda ürün geliştirdim')\n"
    "- Profilde olmayan sayı/metrik (kullanıcı sayısı, gelir, müşteri sayısı)\n"
    "- Profilde olmayan unvan, şirket, sertifika, teknoloji deneyimi\n"
    "- Profilde olmayan bir teknoloji/veri-tipi/altyapı iddiası (ör. profilde "
    "'IoT sensor data' yokken 'real-time IoT sensor data işledim'; ya da profilde "
    "olmayan spesifik veritabanı/altyapı özelliklerini isimlendirmek: 'tsvector index', "
    "'JSONB agregatör', 'Kafka', 'Kubernetes', 'Redis' vb. — profilde birebir geçmiyorsa YAZILAMAZ)\n"
    "- Profilde olmayan uyumluluk/standart iddiası ('GDPR-level', 'HIPAA uyumlu', "
    "'SOC2', 'ISO 27001' vb. — profilde açıkça geçmiyorsa DESTEKLENMEZ)\n"
    "- Profildeki bir metriğin çarpıtılması\n"
    "- Geçmiş bir işin şu an sürüyormuş gibi yazılması\n\n"
    "DESTEKLENİR sayılır: şirkete duyulan ilgi, öğrenme isteği, profildeki "
    "gerçeklerin farklı kelimelerle ifadesi, hedef şirket hakkındaki bilgiler "
    "(bunlar profilde olmak zorunda değil).\n\n"
    "Şüphedeyken DESTEKLENMEYEN say — bu mail gerçek bir şirkete gidiyor ve "
    "uydurma bir iddia başvuranın itibarına mal olur.\n\n"
    'SADECE şu JSON: {"temiz": true/false, "sorunlar": ["...", "..."]}'
)


def verify(body: str, profile: dict) -> dict | None:
    # max_tokens 800 değil 3000: 800'de claude-sonnet-5 cevabı JSON kapanmadan kesiliyordu
    # (stop_reason=max_tokens, çoğu zaman metin bile boş). Ölçülen: 2026-09-22 run'ında
    # 16 doğrulama çağrısının 16'sı böyle düştü. Çağrı BAŞARILI sayıldığı için hata sessiz
    # kalıyor, denetim "içerik otomatik doğrulanamadı" diyor ve o taslak GÖNDERİLEMİYOR;
    # sonuç: bekleyen taslak sayısı 12'nin altına inemedi ve üretim ~2 hafta kilitlendi.
    # Cevap normalde ~200 token; tavan maliyeti değil, kesilmeyi engelliyor.
    return _call(VERIFY_MODEL, VERIFY_SYSTEM,
                 f"--- PROFİL ---\n{json.dumps(profile, ensure_ascii=False)}\n\n"
                 f"--- MAİL METNİ ---\n{strip_urls(body)}",
                 "temiz", max_tokens=3000)


def rules_version() -> str:
    """Denetim kurallarının parmak izi (audit cache anahtarına girer).

    Elle artırılan bir sürüm numarası unutulur; kuralın kendisinden türetilen hash
    unutulamaz. Doğrulama prompt'u, sayı kalıbı, URL kalıbı ya da beyaz liste
    değiştiği anda eski kararlar kendiliğinden geçersiz olur. Sayı denetiminin tamamı
    (`numeric.py`) hash'e girer: eşleme mantığı değişince de eski karar kalmaz."""
    with open(numeric.__file__, encoding="utf-8") as f:
        blob = VERIFY_SYSTEM + "|" + f.read()
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------- 4) onarım
REPAIR_SYSTEM = (
    "Bir iş başvurusu maili, başvuranın profiline karşı yapılan denetimden geçemedi. "
    "Tek işin: maili EN AZ değişiklikle düzeltmek.\n\n"
    "- Listelenen her sorunlu iddiayı profildeki gerçeğe çevir. Profilde karşılığı "
    "yoksa o ifadeyi ya da cümleyi çıkar.\n"
    "- YENİ iddia, sayı, teknoloji, proje, unvan EKLEME. Sorunlu olmayan cümlelere dokunma.\n"
    "- Dili, tonu, hitabı, kapanışı ve imzayı koru. Mail yine akıcı ve bütün okunmalı; "
    "çıkarılan cümlenin bıraktığı boşluk hissedilmesin.\n"
    "- Linkleri olduğu gibi, düz metin bırak.\n"
    "- Bugünün tarihi veriliyor: bitmiş işler geçmiş zamanla yazılır.\n\n"
    'SADECE şu JSON: {"govde": "..."}'
)


def repair(body: str, problems: list, profile: dict, today: str) -> dict | None:
    user = (f"BUGÜNÜN TARİHİ: {today}\n\n"
            f"--- BAŞVURAN PROFİLİ (tek gerçek kaynak) ---\n"
            f"{json.dumps(profile, ensure_ascii=False)}\n\n"
            f"--- DENETİMİN BULDUĞU SORUNLAR ---\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\n\n--- MAİL METNİ ---\n{body}")
    return _call(DRAFT_MODEL, REPAIR_SYSTEM, user, "govde", max_tokens=4000)


def repair_and_verify(body: str, problems: list, profile: dict, today: str,
                      rounds: int = 2, repair_fn=None, verify_fn=None,
                      allow: str = "") -> tuple:
    """(yeni_govde | None, sebep, sayilir).

    `sayilir` False ise başarısızlık İÇERİK kaynaklı değil (model cevap vermedi, kota,
    ağ): çağıran bunu "onarılamadı" diye kaydetmemeli, yoksa bir altyapı arızası
    taslakları kalıcı olarak onarılamaz damgalar (ve silme açıksa siler).

    İkinci tur ilk onarımın çıktısı üzerinden, kalan sorunlarla yapılır. Onarılan metin
    yazılan metinle aynı denetimden geçer: numeric_check + verify. Denetimi geçmeyen
    hiçbir metin dönmez."""
    repair_fn, verify_fn = repair_fn or repair, verify_fn or verify
    sorunlar, metin = [str(p) for p in problems], body
    for _ in range(rounds):
        r = repair_fn(metin, sorunlar, profile, today)
        # govde string değilse (null, liste) cevap bozuktur: str(None) "None" verir, kısalık
        # kontrolüne takılır ve içerik hatası sayılıp silmeye kadar giderdi.
        govde = (r or {}).get("govde")
        yeni = govde.strip() if isinstance(govde, str) else ""
        if not yeni:
            return None, "onarım adımı cevap vermedi", False
        # Model "sorunlu cümleyi çıkar" talimatını mailin yarısını silerek yerine
        # getirirse elde gönderilecek bir mail kalmaz; bunu onarım sayma.
        if len(yeni) < len(body) * 0.5:
            return None, "onarım metni yarıdan fazla kısalttı", True
        sayi = numeric_check(yeni, profile, allow)
        v = verify_fn(yeni, profile)
        if v is None:
            return None, "doğrulama adımı cevap vermedi", False
        if v.get("temiz") and not sayi:
            return yeni, "", True
        sorunlar = [str(x) for x in v.get("sorunlar", [])] + ([sayi] if sayi else [])
        metin = yeni
    return None, "; ".join(sorunlar)[:300], True


def judge_draft_verify(company: dict, site_text: str, profile: dict,
                       today: str) -> tuple[dict | None, str]:
    """(taslak, sebep) — taslak None ise sebep neden elendiğini söyler."""
    j = judge(company, site_text)
    if not j:
        return None, "eleme adımı cevap vermedi"
    if not j.get("uygun"):
        return None, "eleme: " + str(j.get("gerekce", ""))[:120]

    sektor = j.get("sektor", "")
    duzeltme = ""
    alan = str(company.get("domain", ""))
    for deneme in range(2):                      # bir kez düzeltme şansı
        d = draft(company, site_text, profile, sektor, today, duzeltme)
        if not d or not d.get("govde"):
            return None, "taslak üretilemedi"
        # Deterministik sayı denetimi önce: ucuz ve kesin. Modelin gördüğü site metnindeki
        # sayılar şirketin kendi sayısıdır; başvuranın iddialarını verify() profile bağlar.
        sayi_sorunu = numeric_check(d["govde"], profile, f"{alan} {site_text[:3000]}")

        v = verify(d["govde"], profile)
        if v is None:
            return None, "doğrulama adımı cevap vermedi (güvenli tarafta kalındı)"
        if v.get("temiz") and not sayi_sorunu:
            d["sektor"] = sektor
            # Site sayesinde geçen sayılar kayda yazılır: bekleyen taslak denetimi siteyi
            # görmez, bu izin olmadan ertesi gün aynı sayıyı uydurma sayıp taslağı bozardı.
            d["sayi_izni"] = " ".join(foreign_numbers(d["govde"], profile, alan))
            attach_cv(d, profile)
            return d, ""
        sorunlar = "; ".join(v.get("sorunlar", []) + ([sayi_sorunu] if sayi_sorunu else []))[:300]
        if deneme == 0:
            duzeltme = sorunlar
            print(f"    ~ doğrulama reddetti, yeniden yazılıyor: {sorunlar[:100]}")
        else:
            return None, f"HALÜSİNASYON — {sorunlar}"
    return None, "taslak doğrulanamadı"


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    sayfa = ('<html><head><title>Acme</title><style>@font-face{font-family:X}</style>'
             '<meta content="AI tools for hotels &amp; hostels" name="description">'
             '<script>window.dataLayer=[];function gtag(){}</script></head><body>'
             '<!-- <div>eski</div> --><svg><path d="M0 0"/></svg><h1>We build &#x27;Acme&#x27;</h1></body></html>')
    assert page_text(sayfa) == "AI tools for hotels & hostels. Acme We build 'Acme'", page_text(sayfa)
    assert page_text("<p>" + "a" * 50 + "</p>", limit=10) == "a" * 10
    # nesneden sonra yazmaya devam eden cevap: beklenen anahtarı taşıyan son tam nesne geçerlidir
    assert last_object('{"uygun": false}\nYeniden bakınca:\n{"uygun": true}', "uygun") == {"uygun": True}
    assert last_object('ön söz {"uygun": true}\nNot: {yarım', "uygun") == {"uygun": True}
    assert last_object('{"uygun": tr', "uygun") is None and last_object("nesne yok", "uygun") is None
    assert last_object('{"uygun": true, "sektor": "f"}\nNot: {"sektor": "f"} seçtim.', "uygun")["uygun"]
    assert last_object('Şablon {x} yerine: {"uygun": true} bitti {', "uygun") == {"uygun": True}
    assert last_object('{"uygun": true, "x": {"uygun": false', "uygun") is None    # kesik dış nesne
    # denetimde kirli bir hüküm varsa, sonradan yazılan temiz nesne onu silmez
    assert last_object('{"temiz": false, "sorunlar": ["a"]}\nÇıkarılırsa {"temiz": true, "sorunlar": []}',
                       "temiz") == {"temiz": False, "sorunlar": ["a"]}

    prof = {"projeler": ["Acme Panel"], "metrik": "ortalama 0.56"}
    assert rules_version() == rules_version() and len(rules_version()) == 8

    # kredi bitince ilk 400'de kesilir, sonraki çağrı ağa çıkmaz (ağsız: urlopen sahte)
    import io
    cagri = []

    def _sahte(req, timeout=0):
        cagri.append(1)
        raise HTTPError("u", 400, "x", {}, io.BytesIO(b'{"message": "Your credit balance is too low"}'))
    eski_key, eski_open = os.environ.get("ANTHROPIC_API_KEY"), urlopen
    os.environ["ANTHROPIC_API_KEY"], urlopen = "test", _sahte
    assert judge({"domain": "a.io"}, "") is None and judge({"domain": "b.io"}, "") is None
    assert KREDI_BITTI and len(cagri) == 1
    KREDI_BITTI, urlopen = False, eski_open
    os.environ.pop("ANTHROPIC_API_KEY") if eski_key is None else os.environ.update(ANTHROPIC_API_KEY=eski_key)

    temiz, kirli = {"temiz": True, "sorunlar": []}, {"temiz": False, "sorunlar": ["uydurma"]}
    govde = "Merhaba, Acme Panel projesini kurdum. Fizyoterapi platformu geliştirdim. Selamlar."

    # 1. turda düzelir
    y, sebep, say = repair_and_verify(
        govde, ["uydurma"], prof, "2026-10-02",
        repair_fn=lambda b, p, pr, t: {"govde": b.replace(" Fizyoterapi platformu geliştirdim.", "")},
        verify_fn=lambda b, pr: kirli if "Fizyoterapi" in b else temiz)
    assert y and "Fizyoterapi" not in y and sebep == "" and say, (y, sebep, say)

    # 2 turda da düzelmez → içerik başarısızlığı (sayılır)
    y, sebep, say = repair_and_verify(govde, ["uydurma"], prof, "2026-10-02",
                                      repair_fn=lambda b, p, pr, t: {"govde": b},
                                      verify_fn=lambda b, pr: kirli)
    assert y is None and "uydurma" in sebep and say

    # model cevap vermedi / doğrulama cevap vermedi → altyapı, SAYILMAZ
    assert repair_and_verify(govde, [], prof, "t", repair_fn=lambda *a: None,
                             verify_fn=lambda b, pr: temiz)[2] is False
    assert repair_and_verify(govde, [], prof, "t", repair_fn=lambda b, *a: {"govde": b},
                             verify_fn=lambda b, pr: None)[2] is False

    # maili yarıdan fazla kısaltan "onarım" kabul edilmez
    y, sebep, say = repair_and_verify(govde, [], prof, "t",
                                      repair_fn=lambda *a: {"govde": "Merhaba."},
                                      verify_fn=lambda b, pr: temiz)
    assert y is None and "kısalttı" in sebep and say

    # onarım yeni bir sayı uydurursa deterministik kat yakalar
    y, _s, say = repair_and_verify(govde, [], prof, "t",
                                   repair_fn=lambda b, *a: {"govde": b + " 9000 kullanıcı."},
                                   verify_fn=lambda b, pr: temiz)
    assert y is None and say
    cvp = {"cv_links": {"ai": "www.ornek.com/cv-ai.pdf", "yazilim": "www.ornek.com/cv-yazilim.pdf"}}
    t1, t2 = {"govde": "Merhaba.\n", "cv": "ai"}, {"govde": "Merhaba.", "cv": "olmayan"}
    attach_cv(t1, cvp), attach_cv(t1, cvp), attach_cv(t2, cvp)
    assert t1["govde"] == "Merhaba.\n\nCV: www.ornek.com/cv-ai.pdf" and t2["govde"] == "Merhaba."
    assert numeric_check(t1["govde"], prof) is None
    print("drafting self-test: OK")
