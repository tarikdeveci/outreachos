"""
Otomasyon pipeline'ının KARAR MOTORU (deterministik, test edilebilir).

Manuel outreach akışını koda döker:
  1) role_filter      -> zero_tolerance_rule'a harfiyen uyar (senior/hard-exclude = ASLA girmez)
  2) sector_filter    -> excluded_sectors / excluded_companies_seed(_personal)
  3) classify_contact -> gerçek ATS linki > doğrulanmış genel email; KİŞİYE ÖZEL EMAIL TAHMİN ETMEZ
  4) decide           -> hepsini birleştirir, önerilen durum + gerekçe döndürür

Keşif (discovery) kısmı bir arama API anahtarı gerektirir (SEARCH_API_KEY) — bkz. README.
Bu modül anahtarsız da çalışır: eldeki bir aday sözlüğünü kurallara göre sınıflandırır.

CLI:
  python src/pipeline.py role "Sr. Frontend Developer"
  python src/pipeline.py decide '{"firma":"Acme","title":"Junior AI Engineer","link":"https://jobs.lever.co/acme/x"}'
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn, STATE_PATH  # noqa: E402


# ---------- kuralları yükle ----------
def load_profile():
    """Önce DB'deki profile, yoksa state.json."""
    try:
        conn = get_conn()
        r = conn.execute("SELECT data_json FROM profile WHERE id=1").fetchone()
        conn.close()
        if r:
            return json.loads(r["data_json"])
    except Exception:
        pass
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("profile", {})


def load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


ATS_HOSTS = ["lever.co", "greenhouse.io", "ashbyhq.com", "workable.com",
             "applytojob.com", "recruitee.com", "teamtailor.com", "breezy.hr"]
GENERIC_PREFIXES = ["info@", "hello@", "careers@", "kariyer@", "jobs@",
                    "contact@", "hr@", "ik@", "team@", "sales@", "support@"]


# ---------- 1) rol filtresi (ZERO TOLERANCE) ----------
def role_filter(title: str, profile=None):
    """(include: bool, reason: str, status_if_excluded: str|None)"""
    profile = profile or load_profile()
    rf = profile.get("role_filters", {})
    t = f" {(title or '').lower()} "

    def hit(keywords):
        for kw in keywords:
            k = kw.lower().strip()
            if not k:
                continue
            if k in t:
                return kw
        return None

    # zero tolerance: seniority_exclude veya hard-exclude eşleşirse ASLA girmez
    sr = hit(rf.get("seniority_exclude_keywords", []))
    if sr:
        return False, f"ZERO_TOLERANCE: kıdem anahtarı '{sr}' eşleşti", "ELENEN_SENIORITY"
    hard = hit(rf.get("title_exclude_keywords_hard", []))
    if hard:
        return False, f"ZERO_TOLERANCE: sert-ele anahtarı '{hard}' eşleşti", "ELENEN_UYGUNSUZ"

    # frontend/UI: sadece full-stack/backend/AI unsuru yoksa ele
    soft = hit(rf.get("title_exclude_keywords_unless_fullstack", []))
    if soft:
        allow = any(x in t for x in ["full stack", "full-stack", "fullstack", "backend", "back-end", "ai", "ml"])
        if not allow:
            return False, f"frontend/UI ('{soft}') ve full-stack/backend/AI unsuru yok", "ELENEN_UYGUNSUZ"

    # dahil edilecek kategoriler
    cats = rf.get("role_categories_include", {})
    for cat, kws in cats.items():
        m = hit(kws)
        if m:
            return True, f"uygun rol (kategori: {cat}, eşleşme: '{m}')", None

    # kategori eşleşmedi ama açıkça elenmedi -> araştırılsın
    return True, "rol açıkça elenmedi (kategori eşleşmesi zayıf, manuel doğrula)", None


# ---------- 2) sektör / şirket filtresi ----------
def sector_filter(firma: str, sektor: str = "", state=None):
    """(ok: bool, reason, status_if_excluded)"""
    state = state or load_state()
    f = (firma or "").lower()
    s = (sektor or "").lower()

    for entry in state.get("excluded_companies_seed_personal", []):
        base = entry.split("(")[0].strip().lower()
        if base and base in f:
            return False, f"kişisel tanıdık şirketi ({entry.split('(')[0].strip()})", "ELENEN_KISISEL"

    for name in state.get("excluded_companies_seed", []):
        base = name.split("(")[0].strip().lower()
        if base and (base in f or f in base):
            return False, f"hariç tutulan şirket ({name})", "ELENEN_SEKTOR"

    for sec in state.get("excluded_sectors", []):
        for token in re.split(r"[/,]", sec.lower()):
            token = token.strip()
            if len(token) > 3 and token in s:
                return False, f"hariç sektör ('{token}')", "ELENEN_SEKTOR"

    return True, "sektör/şirket uygun", None


# ---------- 3) iletişim doğrulama ----------
def classify_contact(candidate: dict):
    """
    Öncelik: gerçek ATS linki > doğrulanmış genel email.
    KİŞİYE ÖZEL EMAIL (isim.soyisim@) ASLA ÜRETİLMEZ.
    (kanal, deger, onerilen_durum) döndürür.
    """
    link = (candidate.get("link") or candidate.get("eposta_veya_link") or "").strip()
    email = (candidate.get("email") or "").strip()

    if link and any(h in link.lower() for h in ATS_HOSTS):
        return "ats", link, "ATS_DIGEST"

    if email:
        low = email.lower()
        if any(low.startswith(p) for p in GENERIC_PREFIXES):
            return "gmail_draft", email, "TASLAK"
        # kişiye özel görünen email (isim.soyisim@) -> doğrulanmadıysa GÜVENME
        local = low.split("@")[0]
        looks_personal = ("." in local or re.match(r"^[a-z]+$", local)) and local not in \
            [p.rstrip("@") for p in GENERIC_PREFIXES]
        if looks_personal and not candidate.get("email_verified"):
            return None, None, "ELENEN_EPOSTA_BULUNAMADI"
        # doğrulanmış (kullanıcı işaretlediyse) veya bariz genel değilse taslak
        return "gmail_draft", email, "TASLAK"

    if link:
        # ATS değil ama kariyer sayfası/form -> insanın doldurması için digest
        return "career_form", link, "ATS_DIGEST"

    return None, None, "ELENEN_EPOSTA_BULUNAMADI"


# ---------- skor: 0-100 kaba uygunluk (deterministik; LLM skoru varsa o önceliklidir) ----------
JUNIOR_SIGNALS = ["junior", "jr.", "jr ", "intern", "staj", "associate", "entry",
                  "new grad", "yeni mezun", "graduate", "başlangıç"]


def score_fit(candidate: dict, profile=None, kanal: str | None = None) -> int:
    """Şirket+rol+iletişim sinyallerinden 45-100 arası kaba bir eşleşme skoru.
    Kesin karar değil, sıralama içindir; LLM `uygunluk_skoru` varsa rapor onu gösterir."""
    profile = profile or load_profile()
    title = f" {(candidate.get('title') or '').lower()} "
    score = 45
    cats = profile.get("role_filters", {}).get("role_categories_include", {})
    if any(kw.lower().strip() and kw.lower().strip() in title
           for kws in cats.values() for kw in kws):
        score += 25
    if any(j in title for j in JUNIOR_SIGNALS):
        score += 10
    if kanal == "ats":
        score += 15
    elif kanal in ("gmail_draft", "career_form"):
        score += 8
    sektor = (candidate.get("sektor") or "").lower()
    mapping = {k.lower(): v for k, v in (profile.get("project_sector_mapping", {}) or {}).items()}
    if sektor and any(tok in sektor for tok in mapping):
        score += 7
    return max(0, min(100, score))


# ---------- 4) tam karar ----------
def decide(candidate: dict):
    """
    candidate: {firma, title, sektor, link, email, email_verified}
    -> {include, durum, kanal, deger, skor, reasons[]}
    """
    reasons = []
    ok, why, st = sector_filter(candidate.get("firma", ""), candidate.get("sektor", ""))
    reasons.append(why)
    if not ok:
        return {"include": False, "durum": st, "kanal": "elendi", "deger": None,
                "skor": 0, "reasons": reasons}

    ok, why, st = role_filter(candidate.get("title", ""))
    reasons.append(why)
    if not ok:
        return {"include": False, "durum": st, "kanal": "elendi", "deger": None,
                "skor": 0, "reasons": reasons}

    kanal, deger, durum = classify_contact(candidate)
    reasons.append(f"iletişim: {kanal or 'bulunamadı'}")
    include = durum not in ("ELENEN_EPOSTA_BULUNAMADI",)
    skor = score_fit(candidate, kanal=kanal) if include else 0
    return {"include": include, "durum": durum, "kanal": kanal or "elendi",
            "deger": deger, "skor": skor, "reasons": reasons}


def already_processed(url: str, firma: str, state=None):
    state = state or load_state()
    if url and url in state.get("processed_posting_urls", []):
        return True
    contacted = {k.lower() for k in state.get("companies_already_contacted", {})}
    return (firma or "").lower() in contacted


# ---------- CLI ----------
def _main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "role":
        inc, why, st = role_filter(sys.argv[2])
        print(json.dumps({"include": inc, "reason": why, "status": st}, ensure_ascii=False, indent=2))
    elif cmd == "decide":
        cand = json.loads(sys.argv[2])
        print(json.dumps(decide(cand), ensure_ascii=False, indent=2))
    elif cmd == "selftest":
        _selftest()
    else:
        print("komutlar: role <title> | decide <json> | selftest")


def _selftest():
    cases = [
        ({"title": "Sr. Frontend Developer"}, False),   # zero tolerance seniority
        ({"title": "Senior AI Engineer"}, False),        # seniority
        ({"title": "QA Engineer"}, False),               # hard exclude
        ({"title": "Frontend Developer"}, False),        # frontend-only
        ({"title": "Junior Full-Stack Developer"}, True),
        ({"title": "AI Product Engineer"}, True),
        ({"title": "Sürdürülebilirlik Uzmanı"}, True),
    ]
    ok = 0
    for cand, expected in cases:
        inc, why, _ = role_filter(cand["title"])
        mark = "PASS" if inc == expected else "FAIL"
        if inc == expected:
            ok += 1
        print(f"[{mark}] {cand['title']:35s} include={inc}  ({why})")
    print(f"\n{ok}/{len(cases)} geçti.")


if __name__ == "__main__":
    _main()
