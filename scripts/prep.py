#!/usr/bin/env python3
"""
Görüşme hazırlık notu: bir firmadan görüşme daveti geldiğinde bir kez üretilir.

Girdi yalnızca firmanın kendi sitesi, state'teki profil ve gelen yanıtın özetidir. Not bu
üçünde olmayan bir bilgi taşıyamaz: sitede ya da profilde geçmeyen bir sayı varsa not atılır
(`drafting.numeric_check`, taslaklardaki uydurma denetiminin aynısı). Not kayda `hazirlik`
olarak yazılır, üretildiği günün raporuna ve takip.md'ye girer; ikinci kez üretilmez.
"""
import json
import os
import re

import autosend
import drafting
from tracking import GORUSME, GUN_MS, _day

PREP_GUN = 14        # bundan eski davet için not üretilmez: görüşme büyük olasılıkla geçti
MAX_DENEME = 2       # site açılmıyor ya da not denetimden dönüyorsa sonsuza dek denenmez

PREP_SYSTEM = (
    "Bir iş görüşmesine hazırlanan adaya kısa bir hazırlık notu yazıyorsun. Yalnızca sana "
    "verilen ŞİRKET SİTESİ metnindeki ve ADAY PROFİLİ'ndeki bilgileri kullan. Sitede yazmayan "
    "hiçbir şeyi (ekip büyüklüğü, yatırım, müşteri adı, tarih, sayı) yazma; emin değilsen o "
    "alanı boş bırak. Türkçe yaz, kısa cümleler kur. Yalnızca şu JSON'u döndür: "
    '{"sirket": "şirket ne yapıyor, en fazla 2 cümle", '
    '"uyum": ["adayın profilinden bu şirkete en çok uyan, en fazla 3 somut nokta"], '
    '"sorulabilir": ["görüşmede adaya sorulması muhtemel, en fazla 4 soru"], '
    '"sor": ["adayın şirkete sorabileceği, siteye dayanan en fazla 3 soru"]}')
ALANLAR = (("uyum", 3, "Sana uyan noktalar"), ("sorulabilir", 4, "Sorulabilecekler"),
           ("sor", 3, "Sen sor"))
TARAYICI = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en,tr;q=0.8"}


def _binlik(s: str) -> str:
    return re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", s)


def _s(x) -> str:
    return re.sub(r"\s+", " ", re.sub("\\s*[\u2014\u2013]\\s*", ", ", str(x or ""))).strip()


def pending(contacted: dict, now_ms: int, cap: int = 5) -> list:
    """Yeni görüşme daveti gelmiş, notu henüz yazılmamış kayıtlar."""
    out = []
    for firma, rec in contacted.items():
        if not isinstance(rec, dict) or rec.get("yanit_turu") != GORUSME or rec.get("hazirlik"):
            continue
        if now_ms - int(rec.get("yanit_ms") or 0) > PREP_GUN * GUN_MS:
            continue
        if "@" in (rec.get("email") or "") and int(rec.get("hazirlik_deneme") or 0) < MAX_DENEME:
            out.append((firma, rec))
    return out[:cap]


def site_text(get_page, domain: str) -> str:
    # Görüşmeye çağıran firmanın tek sayfası okunuyor. Bot kimliğini reddeden site için
    # tarayıcı başlıklarıyla bir kez daha denenir; soğuk taramada bu yapılmaz.
    raw = get_page(f"https://{domain}") or get_page(f"https://{domain}", headers=TARAYICI)
    if not raw:
        return ""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw.decode("utf-8", errors="replace"),
                  flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:6000]


def make_note(firma: str, rec: dict, text: str, profile: dict, call) -> tuple:
    """(not, sebep): not None ise sebep neden yazılamadığını söyler."""
    user = (f"ŞİRKET: {firma}\n\nGELEN YANITIN ÖZETİ: {rec.get('yanit_ozet', '')}\n\n"
            f"ADAY PROFİLİ (JSON):\n{json.dumps(profile, ensure_ascii=False)}\n\n"
            f"ŞİRKET SİTESİ:\n{text}")
    out = call(drafting.DRAFT_MODEL, PREP_SYSTEM, user, 3000)
    if not isinstance(out, dict):
        return None, "model cevap vermedi"
    note = {"sirket": _s(out.get("sirket"))[:400]}
    for alan, n, _baslik in ALANLAR:
        v = out.get(alan)
        note[alan] = [_s(x)[:240] for x in v if _s(x)][:n] if isinstance(v, list) else []
    if not note["sirket"] and not note["uyum"]:
        return None, "model boş not döndürdü"
    duz = " ".join([note["sirket"]] + [x for alan, _n, _b in ALANLAR for x in note[alan]])
    # Sitede "20,000", Türkçe notta "20.000": aynı sayı. Binlik ayracı atılmış hali de sınanır.
    izin = f"{firma} {text} {_binlik(text)}"
    sayi = (drafting.numeric_check(duz, profile, allow=izin)
            and drafting.numeric_check(_binlik(duz), profile, allow=izin))
    if sayi:
        return None, f"uydurma denetimi: {sayi}"[:120]   # sitede ve profilde olmayan sayı
    return note, ""


def run(state: dict, get_page, now_ms: int, today: str, free_mail=frozenset(), call=None) -> tuple:
    """Bekleyen davetler için not üretir, kayda yazar.
    Döner: (notu yazılan firmalar, yazılamayanlar için "firma: sebep" satırları)."""
    if call is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return [], []                 # anahtar yokken deneme hakkı harcanmaz
        call = drafting._call
    done, sorun = [], []
    for firma, rec in pending(state.get("companies_already_contacted", {}), now_ms):
        key = autosend.target_key(rec["email"], free_mail)
        text = "" if "@" in key else site_text(get_page, key)
        note, neden = (make_note(firma, rec, text, state.get("profile", {}), call) if text
                       else (None, "site açılmadı"))
        if note:
            rec["hazirlik"] = {"tarih": today, **note}
            done.append(firma)
        else:
            rec["hazirlik_deneme"] = int(rec.get("hazirlik_deneme") or 0) + 1
            sorun.append(f"{firma}: {neden}")
    return done, sorun


def report_lines(contacted: dict, today: str) -> list:
    """Bugün yazılan notlar, raporun 'ilgilenmen gerekenler' bloğuna girecek satırlar."""
    out = []
    for firma, rec in contacted.items():
        h = rec.get("hazirlik") if isinstance(rec, dict) else None
        if not h or h.get("tarih") != today:
            continue
        out.append(f"Görüşme hazırlığı, {firma}: {h.get('sirket', '')}")
        out += [f"{firma}, {baslik.lower()}: " + " | ".join(h[alan])
                for alan, _n, baslik in ALANLAR if h.get(alan)]
    return out


def markdown(contacted: dict, now_ms: int) -> str:
    """takip.md'nin sonuna eklenen bölüm: güncel davetlerin hazırlık notları."""
    parts = []
    for firma, rec in contacted.items():
        h = rec.get("hazirlik") if isinstance(rec, dict) else None
        if not h or now_ms - int(rec.get("yanit_ms") or 0) > 2 * PREP_GUN * GUN_MS:
            continue
        parts.append(f"### {firma} (davet: {_day(rec.get('yanit_ms'))}, not: {h.get('tarih', '?')})\n")
        if h.get("sirket"):
            parts.append(h["sirket"] + "\n")
        for alan, _n, baslik in ALANLAR:
            if h.get(alan):
                parts.append(f"**{baslik}**\n\n" + "\n".join(f"- {x}" for x in h[alan]) + "\n")
    return ("\n## Görüşme hazırlığı\n\n" + "\n".join(parts)) if parts else ""


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    now = 1_790_000_000_000
    prof = {"projeler": ["LifeOS", "MuniGo"], "deneyim": "3 yıl"}
    st = {"profile": prof, "companies_already_contacted": {
        "acme": {"email": "hi@acme.io", "yanit_turu": GORUSME, "yanit_ms": now - GUN_MS,
                 "yanit_ozet": "Interview with Acme"},
        "uydur": {"email": "hi@uydur.io", "yanit_turu": GORUSME, "yanit_ms": now - GUN_MS},
        "gecmis": {"email": "hi@gecmis.io", "yanit_turu": GORUSME, "yanit_ms": now - 40 * GUN_MS},
        "kisi": {"email": "biri@gmail.com", "yanit_turu": GORUSME, "yanit_ms": now - GUN_MS},
        "ret": {"email": "hi@ret.io", "yanit_turu": "ret", "yanit_ms": now - GUN_MS}}}
    cc = st["companies_already_contacted"]

    def page(url: str, headers=None):
        if "gecmis" in url or ("acme" in url and not headers):   # acme bot kimliğini reddediyor
            return None
        return b"<html><script>var x = 99;</script><p>Acme builds AI tools for 40 hotels, 20,000 rooms.</p>"

    def fake(model, system, user, max_tokens):
        if "ŞİRKET: uydur" in user:
            return {"sirket": "250 müşterisi olan bir şirket.", "uyum": ["LifeOS"]}
        return {"sirket": "Oteller için yapay zeka araçları \u2014 40 otelde.", "uyum": ["LifeOS", ""],
                "sorulabilir": ["20.000 oda için ölçek?", "b", "c", "d", "e"], "sor": "liste değil"}

    assert "99" not in site_text(page, "acme.io") and "40 hotels" in site_text(page, "acme.io")
    assert [f for f, _ in pending(cc, now)] == ["acme", "uydur", "kisi"]
    yazilan, sorunlar = run(st, page, now, "2026-10-04", frozenset({"gmail.com"}), fake)
    assert yazilan == ["acme"] and len(sorunlar) == 2 and "uydurma" in sorunlar[0], sorunlar
    h = cc["acme"]["hazirlik"]
    assert h["sirket"] == "Oteller için yapay zeka araçları, 40 otelde." and h["uyum"] == ["LifeOS"]
    assert len(h["sorulabilir"]) == 4 and h["sor"] == []
    assert "hazirlik" not in cc["uydur"] and cc["uydur"]["hazirlik_deneme"] == 1   # 250 uydurma
    assert cc["kisi"]["hazirlik_deneme"] == 1                                      # sitesi yok
    assert run(st, page, now, "2026-10-04", frozenset({"gmail.com"}), fake)[0] == []
    assert [f for f, _ in pending(cc, now)] == []                                  # hak bitti
    satir = report_lines(cc, "2026-10-04")
    assert satir[0].startswith("Görüşme hazırlığı, acme:") and len(satir) == 3, satir
    assert report_lines(cc, "2026-10-05") == []
    md = markdown(cc, now)
    assert "### acme" in md and "**Sorulabilecekler**" in md and "uydur" not in md
    assert markdown({"ret": cc["ret"]}, now) == ""
    os.environ.pop("ANTHROPIC_API_KEY", None)
    cc["acme"].pop("hazirlik")
    assert run(st, page, now, "2026-10-04") == ([], []) and "hazirlik_deneme" not in cc["acme"]
    print("prep self-test: OK")
