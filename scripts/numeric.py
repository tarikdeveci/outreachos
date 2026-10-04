"""
Sayı denetimi: taslak gövdesindeki sayılar profilde ya da izin verilen metinde (firma adı,
şirketin kendi sitesi) geçmeli.

LLM doğrulamasının yanında deterministik bir ikinci kat: model bazen metriği gözden
kaçırıyor, bu kontrol kaçırmıyor. '150K+ aylık etkin kullanıcı' uydurmasını yakalayan buydu.

Karşılaştırma yazıya değil DEĞERE bakar: '100,000', '100.000', '100 bin' ve '100K' aynı
sayıdır; '150' ile '150K+' ya da '4.9' ile '9' farklıdır. Eki atıp yalnızca rakamı
karşılaştırmak sitedeki "150 employees" ile gövdedeki "150K+ kullanıcı" uydurmasını
geçiriyordu.
"""
from __future__ import annotations

import json
import re

NUM_RE = re.compile(r"\d[\d.,]*(?:[ \t]?(?:%|k\b\+?|m\b\+?|(?:bin|mil(?:yon|lion)|milyar|billion)"
                    r"[a-zçğıöşü']*))?", re.I)
NUM_WHITELIST = {"1", "2", "3", "4", "5", "2022", "2023", "2024", "2025", "2026"}
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_SAYI = re.compile(r"([\d.,]*\d)\s*(k|m|bin|mil(?:yon|lion)|milyar|billion)?[a-zçğıöşü']*\+?(%)?$")
_CARPAN = {"k": 1e3, "bin": 1e3, "m": 1e6, "milyon": 1e6, "million": 1e6,
           "milyar": 1e9, "billion": 1e9}


def strip_urls(body: str) -> str:
    """Gövdedeki URL'leri denetim dışına çıkarır.

    URL bir İDDİA değildir; içindeki rakamlar da öyle. Gmail, taslak API üzerinden
    yazılan linkleri `google.com/url?q=...&ust=1790242035715000&sa=E` biçimine sarıyor
    ve o 16 haneli zaman damgası hem numeric_check'e "profilde olmayan sayı" diye
    takılıyor hem de doğrulayan modele uydurma metrik gibi görünebiliyordu. Denetim
    metni değil iddiaları okumalı."""
    return URL_RE.sub(" ", body or "")


def value(tok: str) -> tuple | None:
    """Sayının değeri: (değer, '%' ya da '', ayraçlı mı, düz mü). Okunamazsa None.

    '1,5' ve '4.9' ondalıktır, '100,000' ve '2.026' binliktir (ayraçtan sonra tam üç hane).
    Düz = ayraçsız ve eksiz tam sayı: yalnızca bu biçim beyaz listeye ve yıl korumasına girer."""
    m = _SAYI.match(tok.strip().lower().replace(" ", "").rstrip(".,"))
    if not m:
        return None
    n, ek, yuzde = m.groups()
    if re.fullmatch(r"\d{1,3}([.,])\d{3}(?:\1\d{3})*", n):
        deger, ayracli = float(re.sub(r"[.,]", "", n)), True
    elif re.fullmatch(r"\d+[.,]\d+", n):
        deger, ayracli = float(n.replace(",", ".")), False
    elif n.isdigit():
        deger, ayracli = float(n), False
    else:
        return None                      # '1.2.3' gibi sürüm numarası
    return (round(deger * _CARPAN.get(ek or "", 1), 6), yuzde or "", ayracli,
            not (ayracli or ek or yuzde or "." in n or "," in n))


def _values(text: str) -> list:
    return [v for v in map(value, NUM_RE.findall(text or "")) if v]


def _eslesir(v: tuple, kaynak: tuple) -> bool:
    # Ayraçlı gövde sayısı kaynaktaki yıla eşlenmez: profilde 'Temmuz 2026' var diye
    # '2.026 kullanıcı' geçmesin.
    if v[2] and kaynak[3] and 1900 <= kaynak[0] <= 2100:
        return False
    return v[:2] == kaynak[:2]


def foreign_numbers(body: str, profile: dict, allow: str = "") -> list:
    """Gövdede geçip ne profilde ne de `allow` metninde bulunan sayılar (yazıldığı haliyle)."""
    kaynak = _values(json.dumps(profile, ensure_ascii=False)) + _values(allow)
    out = []
    for raw in NUM_RE.findall(strip_urls(body)):
        v = value(raw)
        if v is None:
            continue
        if v[3] and raw.strip().rstrip(".,") in NUM_WHITELIST:
            continue
        if not any(_eslesir(v, k) for k in kaynak):
            out.append(raw.strip())
    return out


def numeric_check(body: str, profile: dict, allow: str = "") -> str | None:
    """Profilde geçmeyen bir sayı varsa sebebini döndürür.

    allow: içindeki sayılar iddia sayılmayan metin. Firmanın adı ya da domaini
    ('83sciences.ai' firmasına yazılan her taslak '83' yüzünden uydurma sayılıp eleniyordu)
    ve şirketin kendi sitesinin metni: model siteyi okuyup 'sitenizdeki 100,000 kullanıcı'
    yazınca şirketin kendi sayısı uydurma sayılıyordu."""
    yabanci = foreign_numbers(body, profile, allow)
    return f"profilde olmayan sayı: '{yabanci[0]}'" if yabanci else None


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    prof = {"projeler": ["Acme Panel"], "metrik": "ortalama 0.56", "standart": "ISO 14064, GHG",
            "mezuniyet": "Temmuz 2026"}
    assert numeric_check("skor 0.56 oldu", prof) is None
    assert numeric_check("150K+ kullanıcı", prof) is not None
    assert numeric_check("ISO 14064 raporu", prof) is None
    assert numeric_check("56 puan", prof) is not None and numeric_check("500 kWh", prof) is not None
    # Firma adındaki rakam iddia değildir; aynı rakam başka firmada hâlâ yakalanır
    assert numeric_check("83 Sciences ekibine yazıyorum", prof) is not None
    assert numeric_check("83 Sciences ekibine yazıyorum", prof, "83sciences.ai") is None
    assert numeric_check("Company42 için 97 müşteri", prof, "company42.com") is not None
    # URL içindeki sayı iddia değildir
    assert numeric_check("bkz https://x.io/url?ust=1790242035715000&sa=E", prof) is None
    # Şirketin sitesinde yazan sayı serbesttir (ayraç ve ek farkıyla); yazmayan değil
    site = "acme.io Trusted by 100,000 users in 12 countries. 700K+ downloads, rated 4.9, 2 million rides"
    for metin in ("100.000 kullanıcınız", "100,000 users", "100 bin kullanıcınız", "100K kullanıcı",
                  "12 ülkede", "700k+ indirme", "4.9 puan", "4,9 puan", "2 milyon yolculuk"):
        assert numeric_check(metin, prof, site) is None, metin
    assert numeric_check("150K+ kullanıcı, 12 ülke", prof, site) == "profilde olmayan sayı: '150K+'"
    for metin in ("100 kullanıcı", "9 puan", "70 indirme", "12 milyon kullanıcı", "700 indirme"):
        assert numeric_check(metin, prof, site) is not None, metin
    # Ek, sitedeki küçük sayıya izin almaz
    assert numeric_check("150K+ aylık etkin kullanıcıya ulaşan ürün kurdum", prof, "acme.io 150 employees")
    assert numeric_check("20.000 müşteriniz", prof, "acme.io 20K customers") is None
    # Profildeki ayraçlı sayı da değeriyle eşleşir; ayraçlı gövde sayısı yıla eşlenmez
    assert numeric_check("20.000 kullanıcı", {"m": "20,000 kullanıcı"}) is None
    assert numeric_check("20000 kullanıcı", {"m": "20,000 kullanıcı"}) is None
    assert numeric_check("2.026 kullanıcıya ulaştım", prof) is not None
    assert numeric_check("2.019 müşteri", prof, "acme.io since 2019") is not None
    # Beyaz liste yalnızca düz sayıya: '5K+' beyaz listede değil; satır sonu eki çalmaz
    assert numeric_check("5 yıl, 2026 mezunu", prof) is None
    assert numeric_check("5K+ kullanıcı", prof) is not None
    assert numeric_check("Mezuniyet 2026\nBinlerce satır", prof) is None
    assert foreign_numbers("12 ülkede 100,000 kullanıcı, skor 0.56", prof, "acme.io") == ["12", "100,000"]
    assert numeric_check("12 ülkede 100,000 kullanıcı", prof, "acme.io 12 100,000") is None
    assert value("1.2.3") is None and value("%") is None
    print("numeric self-test OK")
