"""
AI modülü — BYOK (Bring Your Own Key) Anthropic entegrasyonu.
Kullanıcı kendi API anahtarını bağlar; bu modül Claude'a (claude-opus-4-8) gider:
  - generate_draft: şirkete özel outreach mail taslağı üretir (kullanıcının profiline göre)
  - match_project: şirket için en uygun projeyi + gerekçesini önerir

anthropic SDK kuruluysa çalışır (pip install anthropic). Kurulu değilse AI özellikleri
kapalı kalır, dashboard'ın geri kalanı sorunsuz çalışmaya devam eder.
Anahtar asla koda gömülmez — ayarlar'dan (app_config) ya da ANTHROPIC_API_KEY env'inden okunur.
"""
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

try:
    import anthropic  # type: ignore
    _SDK = True
except ImportError:
    _SDK = False


def available() -> bool:
    return True


def _client(api_key: str):
    # BYOK: kullanıcının bağladığı anahtar; yoksa ortam değişkeni.
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Anthropic API anahtarı bağlı değil (Ayarlar'dan bağlayın).")
    return anthropic.Anthropic(api_key=key)


def _message(api_key, system, user, max_tokens):
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Anthropic API anahtarı bağlı değil (Ayarlar'dan bağlayın).")
    if _SDK:
        resp = anthropic.Anthropic(api_key=key).messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    payload = json.dumps({"model": MODEL, "max_tokens": max_tokens, "system": system,
                          "messages": [{"role": "user", "content": user}]}).encode()
    req = Request("https://api.anthropic.com/v1/messages", data=payload,
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urlopen(req, timeout=40) as response:
            data = json.load(response)
    except HTTPError as e:
        raise RuntimeError(f"Anthropic API ({e.code}): {e.read().decode(errors='replace')[:300]}") from e
    return "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text").strip()


def _profile_context(profile: dict) -> str:
    """Profili modele verilecek kompakt bir metne çevirir."""
    p = profile or {}
    parts = [
        f"Ad: {p.get('name','')}",
        f"Ünvan: {p.get('title','')}",
        f"Lokasyon: {p.get('location','')}",
        f"Portfolyo: {p.get('portfolio','')} | GitHub: {p.get('github','')} | LinkedIn: {p.get('linkedin','')}",
    ]
    projects = p.get("projects", {})
    if projects:
        parts.append("Projeler:")
        for name, desc in projects.items():
            parts.append(f"  - {name}: {desc}")
    mapping = p.get("project_sector_mapping", {})
    if mapping:
        parts.append("Sektör→proje eşlemesi:")
        for sec, proj in mapping.items():
            parts.append(f"  - {sec}: {proj}")
    return "\n".join(parts)


DRAFT_SYSTEM = (
    "Sen, bir yazılım/AI mühendisinin iş arama outreach maillerini onun ağzından yazan bir asistansın. "
    "KURALLAR: (1) Profilde OLMAYAN hiçbir deneyim, sertifika veya başarı UYDURMA. "
    "(2) Kısa, samimi ama profesyonel yaz (en fazla ~150 kelime). "
    "(3) Şirketin sektörüne en uygun 1-2 projeyi somut kanıt olarak bağla. "
    "(4) Türkçe yaz (şirket yabancıysa ve İngilizce uygunsa İngilizce). "
    "(5) Abartılı övgü, klişe ve spam dili kullanma. "
    "(6) Telefon numarası ekleme. Sadece mail gövdesini ve tek satırlık bir konu başlığını üret."
)


def generate_draft(company: dict, profile: dict, api_key: str = "") -> dict:
    """(ok, subject, body) — şirkete özel taslak."""
    try:
        user = (
            f"Şirket: {company.get('firma','')}\n"
            f"Sektör: {company.get('sektor','')}\n"
            f"Önerilen proje eşleşmesi: {company.get('proje_eslesme','')}\n"
            f"Kanal: {company.get('kanal','')}\n\n"
            f"--- BAŞVURAN PROFİLİ ---\n{_profile_context(profile)}\n\n"
            "Bu şirkete gönderilecek kısa bir outreach maili yaz. "
            "Yanıtı SADECE şu JSON formatında ver: {\"subject\": \"...\", \"body\": \"...\"}"
        )
        text = _message(api_key, DRAFT_SYSTEM, user, 2000)
        subject, body = _parse_subject_body(text)
        return {"ok": True, "subject": subject, "body": body}
    except Exception as e:  # noqa: BLE001 — kullanıcıya net hata döndür
        return {"ok": False, "error": _friendly_error(e)}


MATCH_SYSTEM = (
    "Sen bir iş-eşleştirme asistanısın. Verilen şirket ve başvuran profiline göre, "
    "profildeki projelerden hangisinin bu şirket için EN güçlü somut kanıt olduğunu seç. "
    "Kısa ve net gerekçe ver. Profilde olmayan bir şey uydurma."
)


def match_project(company: dict, profile: dict, api_key: str = "") -> dict:
    try:
        user = (
            f"Şirket: {company.get('firma','')}\nSektör: {company.get('sektor','')}\n\n"
            f"--- PROFİL ---\n{_profile_context(profile)}\n\n"
            "En uygun projeyi ve tek cümlelik gerekçesini ver. "
            "Yanıtı SADECE şu JSON ile: {\"proje\": \"...\", \"gerekce\": \"...\"}"
        )
        text = _message(api_key, MATCH_SYSTEM, user, 1000)
        data = _extract_json(text) or {}
        return {"ok": True, "proje": data.get("proje", ""), "gerekce": data.get("gerekce", text)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _friendly_error(e)}


def _extract_json(text: str):
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _parse_subject_body(text: str):
    data = _extract_json(text)
    if data and "body" in data:
        return data.get("subject", ""), data.get("body", "")
    return "", text  # JSON çıkmazsa tüm metni gövde yap


def _friendly_error(e: Exception) -> str:
    name = type(e).__name__
    if "Authentication" in name or "401" in str(e):
        return "API anahtarı geçersiz veya süresi dolmuş — Ayarlar'dan yeniden bağlayın."
    if "RateLimit" in name or "429" in str(e):
        return "Anthropic hız limiti aşıldı — biraz sonra tekrar deneyin."
    return f"AI hatası: {e}"
