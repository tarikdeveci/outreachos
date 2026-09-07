"""Bekleyen Gmail taslaklarını denetler — mükerrer mi + içerik doğru mu diye triyaj eder.

Neden gerekli: "12'den fazla taslak varsa yeni üretme" freni tek başına sistemi KİLİTLER.
Backlog, kullanıcı taslakları gönderemediği için birikiyor (mükerrer korkusu + içerik
şüphesi). Bu modül her bekleyen taslağa bir karar verir, kullanıcı backlog'u güvenle
boşaltır, sayı düşer, üretim yeniden açılır. Kilit ancak böyle çözülür.

Üç karar:
  🔁 MUKERRER — bu firmaya Gönderilenler'de zaten mail var → tekrar gönderme, sil.
  ⚠ ICERIK   — profile karşı doğrulama uydurma/desteklenmeyen iddia yakaladı → düzelt.
  ✅ GUVENLI  — mükerrer değil, içerik temiz → gönderebilirsin.
  👀 INCELE   — içerik otomatik doğrulanamadı (LLM yok) → elle bak; güvenli varsayma.

GÜVENLİK: bu modül OKUR ve RAPORLAR; asla otomatik göndermez, asla silmez (silme geri
alınamaz — kararı kullanıcı verir). Drafts-only ilkesi korunur.

Maliyet: içerik doğrulaması taslak başına bir LLM çağrısı. Gövde değişmediyse karar
`cache` (state['draft_audit']) üzerinden gövde+profil hash'iyle yeniden kullanılır —
ilk geçişten sonra günlük maliyet ~sıfır. Profil değişirse kararlar tazelenir.
Doğrulama/sayı fonksiyonları enjekte edilir (drafting.verify / drafting.numeric_check),
böylece çekirdek ağsız test edilebilir.

Saf çekirdek dosya sonundaki __main__ ile kendini test eder: `python scripts/audit_drafts.py`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

SAFE, DUPLICATE, CONTENT, REVIEW, PENDING = "GUVENLI", "MUKERRER", "ICERIK", "INCELE", "BEKLEMEDE"
_EMOJI = {SAFE: "✅", DUPLICATE: "🔁", CONTENT: "⚠", REVIEW: "👀", PENDING: "⏳"}
_LABEL = {SAFE: "GÖNDER", DUPLICATE: "SİL (mükerrer)", CONTENT: "DÜZELT",
          REVIEW: "ELLE BAK", PENDING: "SIRADA (sonraki run)"}


def body_hash(body: str) -> str:
    """Gövdenin normalize edilmiş kararlı hash'i — içerik denetimini cache'lemek için."""
    norm = re.sub(r"\s+", " ", (body or "")).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def profile_hash(profile: dict | None) -> str:
    """Profilin kararlı parmak izi. Anahtar sırası önemsiz — yalnızca içerik sayılır."""
    blob = json.dumps(profile or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def verdict_hash(body: str, profile: dict | None = None) -> str:
    """Cache anahtarı = gövde + profil.

    Karar "bu gövde bu profile karşı doğru mu" sorusunun cevabı, yani SADECE
    gövdeye bakmak yanlış: profil düzeltilince eski kararlar geçersiz hale gelir
    ama gövde değişmediği için cache onları sonsuza kadar canlı tutar. Gerçekte
    yaşandı: profile mezuniyet ayı ve NLP deneyimi eklendi, buna rağmen o iddiaları
    "uydurma" diye işaretleyen kararlar DÜZELT listesinde kalmaya devam etti.

    Profil değişince tüm kararlar tazelenir; maliyet AUDIT_MAX_VERIFY ile run
    başına sınırlı olduğu için bu birkaç run'a yayılır.
    """
    return f"{body_hash(body)}:{profile_hash(profile)}"


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def decode_body(payload: dict) -> str:
    """Gmail mesaj payload'ından text/plain gövdeyi çıkarır (çok parçalıyı gezerek)."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data and mime.startswith("text/plain"):
        return _b64(data)
    for part in payload.get("parts", []) or []:
        text = decode_body(part)
        if text:
            return text
    # text/plain yoksa text/html'e düş (kaba temizlik)
    if data and mime.startswith("text/html"):
        return re.sub(r"<[^>]+>", " ", _b64(data))
    return ""


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def parse_draft(draft_json: dict) -> dict:
    """Gmail draft kaynağından {id, to, domain, subject, body} çıkarır (format=full)."""
    msg = draft_json.get("message", draft_json)
    payload = msg.get("payload", {})
    to = _header(payload, "To")
    emails = EMAIL_RE.findall(to)
    email = emails[0].lower() if emails else ""
    return {"id": draft_json.get("id") or msg.get("id", ""),
            "to": email,
            "domain": email.split("@")[-1] if "@" in email else "",
            "subject": _header(payload, "Subject"),
            "body": decode_body(payload)}


def content_verdict(verify_result, numeric_problem) -> tuple:
    """İçerik kararı: (verdict, reasons). Doğrulama yapılamadıysa güvenli varsayMA."""
    problems: list[str] = []
    if numeric_problem:
        problems.append(numeric_problem)
    if verify_result is None:
        # LLM cevap vermedi — repo ilkesi: şüphedeyken güvenli sayma, elle baktır
        return REVIEW, (problems or ["içerik otomatik doğrulanamadı — göndermeden önce elle oku"])
    if not verify_result.get("temiz", False):
        problems += verify_result.get("sorunlar", [])
    return (CONTENT, problems) if problems else (SAFE, [])


def audit_one(draft: dict, profile: dict, sent_domains: set, verify_fn, numeric_fn,
              cache: dict, may_verify: bool = True) -> dict:
    """Tek taslağı denetler. Mükerrer ise içerik LLM'i hiç çağrılmaz (boşa maliyet yok).
    may_verify False ve cache'te yoksa: LLM çağrılmaz, verdict=BEKLEMEDE (run kotası doldu —
    sonraki run'da denetlenir) ve cache'e YAZILMAZ (kalıcı 'bekleme' oluşmasın)."""
    base = {"id": draft["id"], "company": (draft["domain"].split(".")[0] if draft["domain"] else "?"),
            "to": draft["to"], "domain": draft["domain"], "subject": draft["subject"]}
    dom = draft["domain"]
    if dom and dom in sent_domains:
        return {**base, "verdict": DUPLICATE,
                "reasons": [f"{dom} adresine Gönderilenler'de zaten mail var — tekrar gönderme"]}

    h = verdict_hash(draft.get("body", ""), profile)
    cached = cache.get(draft["id"]) if cache is not None else None
    if cached and cached.get("hash") == h:
        verdict, reasons = cached["verdict"], cached["reasons"]
    elif may_verify and verify_fn is not None:
        numeric_problem = numeric_fn(draft["body"], profile) if numeric_fn else None
        vr = verify_fn(draft["body"], profile)
        verdict, reasons = content_verdict(vr, numeric_problem)
        # Doğrulama CEVAP VEREMEDİĞİNDE (vr is None) sonucu önbelleğe ALMA: bu bir
        # içerik kararı değil, altyapı hatası (ölü model ID'si, kota, ağ). Cache'lenirse
        # taslak bir daha hiç denetlenmez ve gövdesi değişmediği sürece sonsuza kadar
        # "elle bak" listesinde kalır — hata düzelse bile. Gerçekte yaşandı: emekli
        # model ID'si 40 taslağı kalıcı olarak "doğrulanamadı" damgasıyla kilitledi.
        if cache is not None and vr is not None:
            cache[draft["id"]] = {"hash": h, "verdict": verdict, "reasons": reasons}
    else:
        verdict, reasons = PENDING, ["içerik denetimi sıraya alındı — sonraki run'da"]
    return {**base, "verdict": verdict, "reasons": reasons}


def audit(drafts: list, profile: dict, sent_domains: set, verify_fn=None,
          numeric_fn=None, cache: dict | None = None, max_verify: int | None = None) -> list:
    """Tüm bekleyen taslakları denetler → karar listesi. cache: state['draft_audit'].
    max_verify: bu run'da en fazla kaç YENİ (cache'siz, mükerrer-olmayan) taslağa içerik LLM'i
    çağrılsın (maliyet/zaman koruması). Aşılınca kalanlar BEKLEMEDE olur, cache'lenmez → sonraki
    run'da denetlenir. Mükerrer ve cache'li kararlar her zaman (bedava) verilir."""
    out, verified = [], 0
    for d in drafts:
        dom = d["domain"]
        is_dup = bool(dom) and dom in sent_domains
        cached = cache.get(d["id"]) if cache is not None else None
        is_cached = bool(cached and cached.get("hash") == verdict_hash(d.get("body", ""), profile))
        may = (not is_dup) and (not is_cached) and (max_verify is None or verified < max_verify)
        if may and verify_fn is not None:
            verified += 1
        out.append(audit_one(d, profile, sent_domains, verify_fn, numeric_fn, cache, may_verify=may))
    return out


def counts(verdicts: list) -> dict:
    out = {SAFE: 0, DUPLICATE: 0, CONTENT: 0, REVIEW: 0, PENDING: 0}
    for v in verdicts:
        out[v["verdict"]] = out.get(v["verdict"], 0) + 1
    return out


def gmail_draft_url(subject: str, to: str) -> str:
    """Taslağı Gmail'de bulmak için hazır arama linki (konu/adrese göre)."""
    from urllib.parse import quote
    q = f'in:drafts {("subject:" + chr(34) + subject[:40] + chr(34)) if subject else ("to:" + to)}'
    return "https://mail.google.com/mail/u/0/#search/" + quote(q)


def summary_lines(verdicts: list, max_list: int = 40) -> list:
    """Rapor bloğu: tek satır özet + karar bazında gruplu liste + eylem."""
    if not verdicts:
        return ["📋 Bekleyen taslak yok — backlog temiz."]
    c = counts(verdicts)
    head = (f"📋 Bekleyen {len(verdicts)} taslak triyajı: "
            f"✅{c[SAFE]} gönder · 🔁{c[DUPLICATE]} sil · ⚠{c[CONTENT]} düzelt · "
            f"👀{c[REVIEW]} elle bak · ⏳{c[PENDING]} sırada")
    lines = [head]
    shown = 0
    for verdict in (DUPLICATE, CONTENT, REVIEW, SAFE, PENDING):   # önce eylem gerektirenler
        group = [v for v in verdicts if v["verdict"] == verdict]
        if not group:
            continue
        lines.append(f"  {_EMOJI[verdict]} {_LABEL[verdict]} ({len(group)}):")
        for v in group:
            if shown >= max_list:
                lines.append(f"    … ve {len(verdicts) - shown} taslak daha")
                return lines
            reason = ("  — " + "; ".join(v["reasons"])[:160]) if v["reasons"] else ""
            lines.append(f"    • {v['company']} ({v['to']}){reason}")
            shown += 1
    return lines


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    assert body_hash("Merhaba  DÜNYA") == body_hash("merhaba dünya")
    assert body_hash("a") != body_hash("b")

    # Cache anahtarı profile de bağlı: profil değişince karar tazelenmeli
    p1, p2 = {"nlp": "yok"}, {"nlp": "var"}
    assert verdict_hash("ayni govde", p1) != verdict_hash("ayni govde", p2)
    assert verdict_hash("ayni govde", p1) == verdict_hash("ayni  GOVDE", p1)
    assert profile_hash({"a": 1, "b": 2}) == profile_hash({"b": 2, "a": 1})

    # decode_body: düz base64url + çok parçalı
    enc = base64.urlsafe_b64encode("Selam ekip, Acme Panel projesini kurdum.".encode()).decode().rstrip("=")
    assert "Acme Panel" in decode_body({"mimeType": "text/plain", "body": {"data": enc}})
    multipart = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": enc}},
        {"mimeType": "text/html", "body": {"data": enc}}]}
    assert "Acme Panel" in decode_body(multipart)

    dj = {"id": "d1", "message": {"payload": {
        "headers": [{"name": "To", "value": "Careers <careers@acme.io>"},
                    {"name": "Subject", "value": "Başvuru"}],
        "mimeType": "text/plain", "body": {"data": enc}}}}
    p = parse_draft(dj)
    assert p["to"] == "careers@acme.io" and p["domain"] == "acme.io" and "Acme Panel" in p["body"], p

    profile = {"name": "Aday", "projeler": ["Acme Panel"]}
    clean = {"temiz": True, "sorunlar": []}
    dirty = {"temiz": False, "sorunlar": ["profilde olmayan: fizyoterapi platformu"]}

    # mükerrer: içerik fonksiyonları HİÇ çağrılmamalı
    called = {"n": 0}
    def spy_verify(_b, _p):
        called["n"] += 1
        return clean
    drafts = [
        {"id": "a", "to": "careers@acme.io", "domain": "acme.io", "subject": "x", "body": "temiz gövde"},
        {"id": "b", "to": "hello@new.io", "domain": "new.io", "subject": "y", "body": "temiz gövde"},
        {"id": "c", "to": "jobs@bad.io", "domain": "bad.io", "subject": "z", "body": "uydurma gövde"},
        {"id": "d", "to": "hi@rev.io", "domain": "rev.io", "subject": "w", "body": "belirsiz"},
    ]
    sent_domains = {"acme.io"}          # acme'ye zaten gönderilmiş
    cache: dict = {}

    def verify_fn(b, _p):
        return dirty if "uydurma" in b else (None if "belirsiz" in b else clean)

    def numeric_fn(_b, _p):
        return None

    res = audit(drafts, profile, sent_domains, verify_fn, numeric_fn, cache)
    by = {r["id"]: r["verdict"] for r in res}
    assert by == {"a": DUPLICATE, "b": SAFE, "c": CONTENT, "d": REVIEW}, by
    assert "acme.io" in res[0]["reasons"][0]

    # cache: aynı gövde ikinci turda LLM'i tekrar çağırmamalı
    calls = {"n": 0}
    def counting_verify(b, _p):
        calls["n"] += 1
        return clean
    d2 = [{"id": "e", "to": "x@z.io", "domain": "z.io", "subject": "s", "body": "sabit gövde"}]
    audit(d2, profile, set(), counting_verify, numeric_fn, cache)
    audit(d2, profile, set(), counting_verify, numeric_fn, cache)   # 2. tur cache'ten
    assert calls["n"] == 1, calls["n"]

    c = counts(res)
    assert c == {SAFE: 1, DUPLICATE: 1, CONTENT: 1, REVIEW: 1, PENDING: 0}, c

    # max_verify: kota dolunca fazlası BEKLEMEDE (cache'lenmez, sonraki run'da denetlenir)
    fresh = [{"id": f"m{i}", "to": f"x@m{i}.io", "domain": f"m{i}.io", "subject": "s",
              "body": f"gövde {i}"} for i in range(5)]
    cache3, calls3 = {}, {"n": 0}
    def cap_verify(_b, _p):
        calls3["n"] += 1
        return clean
    res3 = audit(fresh, profile, set(), cap_verify, numeric_fn, cache3, max_verify=2)
    assert calls3["n"] == 2, calls3["n"]                      # yalnız 2 LLM çağrısı yapıldı
    assert sum(1 for r in res3 if r["verdict"] == PENDING) == 3   # kalan 3 sırada
    assert len(cache3) == 2                                   # sadece doğrulananlar cache'lendi

    lines = summary_lines(res)
    assert any("triyaj" in l for l in lines) and any("SİL" in l for l in lines)
    print("audit_drafts self-test: OK")
