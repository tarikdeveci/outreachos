"""
Taslak üretimi ve halüsinasyon denetimi.

Üç ayrı adım, üç ayrı sorumluluk — hepsini tek çağrıda yapmak kaliteyi düşürüyordu:

  1. judge()   — "bu şirket makul bir hedef mi" (Haiku, ucuz, çok çağrılıyor)
  2. draft()   — taslak metnini yaz (Sonnet, sadece elemeden geçenler için)
  3. verify()  — "bu metindeki her iddia profilde var mı" (Sonnet, bağımsız göz)

3. adım neden ayrı: yazan modele "uydurma" demek yetmedi. Gerçek bir vakada
Haiku, profilde hiç olmayan "fizyoterapi platformunda ürün geliştirdim" cümlesini
kurdu ve mail gerçek bir şirkete gitti. Aynı çağrı içinde kendi çıktısını
denetlemesini istemek güvenilir değil; ayrı bir çağrı, sadece "şu metni şu
profille karşılaştır" işine odaklanınca bunu yakalıyor.
"""
from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Model ID'leri tarih ekisiz yazilir. claude-sonnet-4-5 emekliye ayrildi ve her
# cagriya HTTP 400 donuyor; guncel karsiligi claude-sonnet-5. Ucuz/pahali
# kademelendirme korunuyor: eleme Haiku (~20 cagri/gun), taslak+dogrulama Sonnet.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5")
DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "claude-sonnet-5")
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "claude-sonnet-5")


def _call(model: str, system: str, user: str, max_tokens: int = 1500) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    payload = json.dumps({"model": model, "max_tokens": max_tokens, "system": system,
                          "messages": [{"role": "user", "content": user}]}).encode()
    req = Request("https://api.anthropic.com/v1/messages", data=payload,
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"})
    try:
        with urlopen(req, timeout=90) as r:
            data = json.load(r)
    except HTTPError as e:
        # Gövdeyi bas: yalın "400 Bad Request" emekli model ID'sini gizliyordu ve
        # hata sessizce "içerik doğrulanamadı" verdict'ine dönüşüyordu.
        print(f"    ! {model} hatası: HTTP {e.code} — {e.read().decode(errors='replace')[:200]}")
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
        return json.loads(text[s:e + 1])
    except json.JSONDecodeError as err:
        print(f"    ! {model} JSON ayrıştırılamadı (stop_reason={stop}, {err}): "
              f"{text[s:s + 160]!r}")
        return None


# ---------------------------------------------------------------- 1) eleme
JUDGE_SYSTEM = (
    "Bir şirketin web sitesinden alınmış metin veriliyor. Tek işin: bu şirket, "
    "junior seviye bir yazılım/AI/ürün insanı için makul bir spekülatif başvuru "
    "hedefi mi karar vermek.\n"
    "GENİŞ DAVRAN: yazılım geliştiren, dijital ürünü olan, teknoloji üreten her "
    "şirket uygundur (robotik, IoT, biyoteknoloji yazılımı, enerji, lojistik, "
    "endüstriyel SaaS, e-ticaret altyapısı dahil).\n"
    "SADECE şunlara uygun=false ver: (a) hiç yazılım/teknoloji üretmeyen işletme, "
    "(b) savunma sanayi veya siber güvenlik ürünü, (c) şirket değil (dernek, "
    "üniversite, haber sitesi, yatırım fonu), (d) metin şirketin ne yaptığını "
    "anlamaya yetmeyecek kadar boş/bozuk.\n"
    'SADECE şu JSON: {"uygun": true/false, "gerekce": "...", "sektor": "..."}'
)


def judge(company: dict, site_text: str) -> dict | None:
    return _call(JUDGE_MODEL, JUDGE_SYSTEM,
                 f"ŞİRKET: {company['domain']}\nSite metni:\n{site_text[:3000]}",
                 max_tokens=400)


# ---------------------------------------------------------------- 2) yazma
DRAFT_SYSTEM = (
    "Bir yazılım/AI mühendisinin iş arama outreach mailini ONUN AĞZINDAN yazıyorsun.\n\n"
    "MUTLAK KURAL — UYDURMA YOK: Sadece profilde AÇIKÇA yazan deneyim, proje, "
    "sertifika ve metriklerden bahsedebilirsin. Profilde geçmeyen bir sektör "
    "deneyimi, müşteri, kullanıcı sayısı, gelir, büyüme oranı ya da rol ASLA yazma. "
    "Bir alana 'ilgi duymak' ile o alanda 'deneyim sahibi olmak' farklı şeylerdir — "
    "profilde deneyim yoksa ilgi olarak yaz, deneyim gibi sunma.\n"
    "Profildeki bir metriği çarpıtma: 'ortalama 0.56, pik 0.81' ise pik değeri tek "
    "başına başlık yapma. Emin değilsen sayıyı hiç yazma.\n\n"
    "ZAMAN KİPİ: Bugünün tarihi veriliyor. Bitiş tarihi geçmiş işleri geçmiş zamanla "
    "yaz ('stajımda çalıştım'), 'şu anda çalışıyorum' deme. Mezuniyet geçtiyse "
    "'mezunuyum' de.\n"
    "AĞIZ: Birinci tekil şahıs ('kurdum', 'geliştirdim'). Başvurandan üçüncü şahısla "
    "bahsetme. Tek kişilik projelerde 'kurduk' değil 'kurdum'.\n"
    "BİÇİM: ~150 kelime, samimi ama profesyonel. Şirketin gerçekten ne yaptığına dair "
    "somut bir detay geçir. Sektöre en uygun 1-2 projeyi bağla. Türkçe yaz (şirket "
    "yabancıysa İngilizce). Linkleri düz metin (ornek.com). Klişe, abartılı övgü, "
    "telefon numarası yok. Kapanış mailin diliyle aynı olsun.\n\n"
    "AYRICA (mail dışında, rapor için):\n"
    "- uygunluk_skoru: 0-100, başvuranın profiliyle şirketin örtüşmesi (85+ çok güçlü, "
    "60-84 makul, 60 altı zayıf) + tek cümlelik skor_gerekce.\n"
    "- hedef_kisiler: LinkedIn'de MESAJ atılacak 1-3 ROL ünvanı (ör. 'Head of AI', "
    "'Engineering Manager', 'Technical Recruiter'). GERÇEK İSİM/E-POSTA UYDURMA, sadece rol.\n"
    "- linkedin_mesaji: LinkedIn için 1-2 cümlelik kısa bağlantı notu (aynı uydurma-yok kuralı).\n\n"
    'SADECE şu JSON: {"proje": "...", "konu": "...", "govde": "...", "uygunluk_skoru": 0-100, '
    '"skor_gerekce": "...", "hedef_kisiler": ["rol1", "rol2"], "linkedin_mesaji": "..."}'
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
    return _call(DRAFT_MODEL, DRAFT_SYSTEM, user, max_tokens=1500)


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
    return _call(VERIFY_MODEL, VERIFY_SYSTEM,
                 f"--- PROFİL ---\n{json.dumps(profile, ensure_ascii=False)}\n\n"
                 f"--- MAİL METNİ ---\n{body}",
                 max_tokens=800)


# ---------------------------------------------------------------- sayı denetimi
NUM_RE = re.compile(r"\d[\d.,]*\s*(?:%|k\+|m\+|bin|milyon)?", re.I)
NUM_WHITELIST = {"1", "2", "3", "4", "5", "2022", "2023", "2024", "2025", "2026"}


def numeric_check(body: str, profile: dict) -> str | None:
    """Profilde geçmeyen bir sayı varsa sebebini döndürür.

    LLM doğrulamasının yanında deterministik bir ikinci kat: model bazen
    metriği gözden kaçırıyor, bu kontrol kaçırmıyor. '150K+ aylık etkin
    kullanıcı' uydurmasını yakalayan buydu.
    """
    prof_text = json.dumps(profile, ensure_ascii=False).lower()
    for raw in NUM_RE.findall(body):
        tok = raw.strip().rstrip(".,").lower().replace(" ", "")
        if not tok or tok in NUM_WHITELIST:
            continue
        digits = tok.rstrip("%k+mbinmilyon").rstrip(".,")
        if not digits or digits in NUM_WHITELIST:
            continue
        # Sınır kontrolü rakam ve noktaya bakar, VİRGÜLE bakmaz.
        #   - rakam komşusu engellenmeli: '500' aranırken 'ISO 50001' eşleşmemeli
        #   - nokta komşusu engellenmeli: '56' aranırken '0.56' eşleşmemeli
        #   - virgül engellenMEmeli: profilde 'ISO 14064, GHG' yazıyorken '14064'
        #     aranınca virgül yüzünden eşleşme reddediliyordu (yanlış alarm)
        if not re.search(rf"(?<![\d.]){re.escape(digits)}(?![\d.])", prof_text):
            return f"profilde olmayan sayı: '{raw.strip()}'"
    return None


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
    for deneme in range(2):                      # bir kez düzeltme şansı
        d = draft(company, site_text, profile, sektor, today, duzeltme)
        if not d or not d.get("govde"):
            return None, "taslak üretilemedi"
        # Deterministik sayı denetimi önce — ucuz ve kesin.
        sayi_sorunu = numeric_check(d["govde"], profile)

        v = verify(d["govde"], profile)
        if v is None:
            return None, "doğrulama adımı cevap vermedi (güvenli tarafta kalındı)"
        if v.get("temiz") and not sayi_sorunu:
            d["sektor"] = sektor
            return d, ""
        sorunlar = "; ".join(v.get("sorunlar", []) + ([sayi_sorunu] if sayi_sorunu else []))[:300]
        if deneme == 0:
            duzeltme = sorunlar
            print(f"    ~ doğrulama reddetti, yeniden yazılıyor: {sorunlar[:100]}")
        else:
            return None, f"HALÜSİNASYON — {sorunlar}"
    return None, "taslak doğrulanamadı"
