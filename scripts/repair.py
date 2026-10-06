"""Bekleyen taslakların otomatik onarımı: tam otomasyonun eksik halkası.

Neden gerekli: audit_drafts ⚠ DÜZELT damgası vuruyor ama düzelten kimse yoktu. Damgalı
taslaklar Gmail'de sonsuza kadar duruyor, bekleyen sayısı tavanın üstünde kaldığı için
üretim kapalı kalıyor ve sistem, kullanıcı elle düzeltene kadar ilerleyemiyordu
(2026-09-24'ten 2026-10-02'ye kadar tek bir yeni taslak açılmadı).

Üç iş, hepsi discover.py'deki AUTO_REPAIR=1 anahtarının arkasında:

  1. Link açma (deterministik, LLM yok): Gmail arayüzünden ya da başka bir araçla
     düzenlenen taslakların linkleri `google.com/url?q=...&ust=...` biçimine sarılıyor.
     Soğuk mailde takip linki gibi görünür. Sarmalayıcı sökülür, taslak text/plain olarak
     yeniden yazılır. ✅ taslaklara da uygulanır: gönderilecek olan onlar.
  2. İçerik onarımı: ⚠ taslak, denetimin bulduğu sorunlarla birlikte modele verilir,
     çıkan metin aynı denetimden (sayı + doğrulama) geçerse taslak güncellenir ve ✅ olur.
     Aynı run'da veto kuyruğuna girer; bir gün sonra gönderilir.
  3. Silme (AYRI anahtar, AUTO_REPAIR_DELETE=1): iki turda onarılamayan taslak ve 🔁
     mükerrer taslak silinir. Silme geri alınamaz, o yüzden onarımla birlikte açılmaz.

Bu modül ağa çıkmaz: model çağrısı, taslak güncelleme ve silme enjekte edilir
(discover.py bağlar). Çekirdek dosya sonundaki __main__ ile kendini test eder:
`python scripts/repair.py`.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

import audit_drafts

MEMO_KEY = "draft_repair"

# q= her zaman ilk parametre değil; hedef URL-encode'lu gelir.
WRAP_RE = re.compile(
    r"https?://(?:www\.)?google\.[a-z.]+/url\?(?:[^\s<>\"']*?&)?q=([^&\s<>\"']+)[^\s<>\"']*", re.I)
# Düz metin parçasında link "ornek.com <http://ornek.com>" diye iki kez geçer.
ANGLE_RE = re.compile(r"(\S+)\s*<(https?://[^<>\s]+)>")


def _bare(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.strip(), flags=re.I).rstrip("/").lower()


def unwrap_links(body: str) -> str:
    """Gmail link sarmalayıcısını söker; aynı linkin köşeli parantezli tekrarını atar."""
    out = WRAP_RE.sub(lambda m: unquote(m.group(1)), body or "")
    return ANGLE_RE.sub(
        lambda m: m.group(1) if _bare(m.group(1)) == _bare(m.group(2)) else m.group(0), out)


def same_claims(a: str, b: str, strip_urls) -> bool:
    """Link açma iddiaları değiştirmedi mi. Değiştirmediyse eski denetim kararı yeni
    gövdeye taşınabilir; değiştirdiyse taşınmaz, taslak yeniden denetlenir."""
    def norm(t: str) -> str:
        return re.sub(r"[<>\s]+", " ", strip_urls(t)).strip().lower()
    return norm(a) == norm(b)


def run(results: list, drafts: dict, state: dict, cache: dict, *, today: str,
        repair_fn, update_fn, delete_fn, key_fn, strip_urls,
        max_llm: int = 10, allow_delete: bool = False) -> dict:
    """Denetim kararlarını uygular. `results` (audit çıktısı) YERİNDE güncellenir:
    onarılan ✅ olur, silinen listeden çıkar.

    drafts:    id → parse_draft çıktısı (to/subject/body)
    repair_fn: (govde, sorunlar, firma_domaini) → (yeni_govde | None, sebep, sayilir)
    update_fn: (id, to, konu, govde) → bool
    delete_fn: (id) → bool
    key_fn:    (govde) → audit cache anahtarı
    max_llm:   bu run'da en fazla kaç taslağa onarım LLM'i çağrılsın

    Onarılamayan taslak state[MEMO_KEY]'e gövde hash'iyle yazılır: silme kapalıyken aynı
    taslağa her run yeniden LLM harcanmaz. Gövde değişirse (kullanıcı elle düzeltti) kayıt
    geçersizdir ve taslak yeniden denenir.
    """
    S, D, C, P = (audit_drafts.SAFE, audit_drafts.DUPLICATE, audit_drafts.CONTENT,
                  audit_drafts.PENDING)
    memo = state.setdefault(MEMO_KEY, {})
    st = {"unwrapped": [], "repaired": [], "deleted": [], "failed": [], "waiting": 0}
    used = 0

    def delete(r: dict, why: str) -> bool:
        if not (allow_delete and delete_fn(r["id"])):
            return False
        st["deleted"].append({**r, "why": why})
        cache.pop(r["id"], None)
        memo.pop(r["id"], None)
        return True

    for r in list(results):
        d = drafts.get(r["id"])
        if not d:
            continue
        if r["verdict"] == D:
            delete(r, "mükerrer")
            continue
        if r["verdict"] not in (S, C) or not d.get("body") or not d.get("to"):
            continue
        body = unwrap_links(d["body"])

        if r["verdict"] == S:
            if body == d["body"] or not update_fn(r["id"], d["to"], d["subject"], body):
                continue
            if same_claims(d["body"], body, strip_urls) and r["id"] in cache:
                cache[r["id"]] = {**cache[r["id"]], "hash": key_fn(body)}
            else:                          # emin değiliz → bu run gönderilmez, yeniden denetlenir
                cache.pop(r["id"], None)
                r.update(verdict=P, reasons=["linkler düzeltildi, sonraki run'da yeniden denetlenecek"])
            d["body"] = body
            st["unwrapped"].append(r)
            continue

        # Yanlış dilde yazılmış taslak çevrilmez: konu da yanlış dilde ve onarım yalnızca
        # gövdeyi değiştirir. Silinir; firma havuza döner ve doğru dilde yeniden yazılır.
        dil = [x for x in r["reasons"] if str(x).startswith(audit_drafts.LANG_PREFIX)]
        if dil:
            if not delete(r, str(dil[0])):
                st["failed"].append({**r, "why": str(dil[0])})
            continue
        rec = memo.get(r["id"])
        if rec and rec.get("hash") == audit_drafts.body_hash(d["body"]):
            if not delete(r, "onarılamadı: " + str(rec.get("sebep", ""))):
                st["failed"].append({**r, "why": str(rec.get("sebep", ""))})
            continue
        if used >= max_llm:
            st["waiting"] += 1
            continue
        used += 1
        fixed, why, counts = repair_fn(body, r["reasons"], r.get("domain") or "")
        if fixed:
            fixed = unwrap_links(fixed)
            if update_fn(r["id"], d["to"], d["subject"], fixed):
                d["body"] = fixed
                cache[r["id"]] = {"hash": key_fn(fixed), "verdict": S, "reasons": []}
                r.update(verdict=S, reasons=[])
                memo.pop(r["id"], None)
                st["repaired"].append(r)
            else:
                st["waiting"] += 1         # taslak yazılamadı → dokunulmadı, sonra yine dene
            continue
        if not counts:                     # altyapı hatası → içerik hakkında hüküm yok
            st["waiting"] += 1
            continue
        memo[r["id"]] = {"hash": audit_drafts.body_hash(d["body"]), "sebep": why[:200], "tarih": today}
        if not delete(r, "onarılamadı: " + why):
            st["failed"].append({**r, "why": why})

    gone = {x["id"] for x in st["deleted"]}
    results[:] = [r for r in results if r["id"] not in gone]
    return st


def summary_lines(st: dict, allow_delete: bool) -> list:
    """Rapor bloğu. Hiçbir şey yapılmadıysa boş liste (rapora gürültü eklenmez)."""
    if not any(st[k] for k in ("unwrapped", "repaired", "deleted", "failed", "waiting")):
        return []
    lines = [f"🔧 Otomatik onarım: {len(st['repaired'])} onarıldı · {len(st['unwrapped'])} link "
             f"düzeltildi · {len(st['deleted'])} silindi · {len(st['failed'])} onarılamadı · "
             f"{st['waiting']} sırada"]

    def block(title: str, items: list, reason: bool = False) -> None:
        if not items:
            return
        lines.append(f"  {title} ({len(items)}):")
        for v in items[:20]:
            tail = f"  — {str(v.get('why', ''))[:140]}" if reason and v.get("why") else ""
            lines.append(f"    • {v['company']} ({v['to']}){tail}")
        if len(items) > 20:
            lines.append(f"    … ve {len(items) - 20} taslak daha")

    block("✅ onarıldı, yarın gönderim kuyruğunda", st["repaired"])
    block("🗑 silindi (geri alınamaz)", st["deleted"], reason=True)
    block("⚠ onarılamadı" + ("" if allow_delete else " (elle düzelt ya da AUTO_REPAIR_DELETE=1 ile sildir)"),
          st["failed"], reason=True)
    return lines


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    import drafting

    w = ("Kod: https://www.google.com/url?q=http://github.com/aday&source=gmail"
         "&ust=1790242035715000&sa=E bakabilirsiniz.")
    assert unwrap_links(w) == "Kod: http://github.com/aday bakabilirsiniz.", unwrap_links(w)
    enc = "x https://www.google.com/url?sa=E&q=https%3A%2F%2Fa.io%2Fp%3Fk%3D1&ust=17 y"
    assert unwrap_links(enc) == "x https://a.io/p?k=1 y", unwrap_links(enc)
    dup = "github.com/aday <https://www.google.com/url?q=http://github.com/aday&sa=E>"
    assert unwrap_links(dup) == "github.com/aday", unwrap_links(dup)
    assert unwrap_links("Profil <https://b.io/x>") == "Profil <https://b.io/x>"   # farklı metin kalır
    assert unwrap_links("düz metin, link yok") == "düz metin, link yok"
    assert same_claims(w, unwrap_links(w), drafting.strip_urls)
    assert same_claims(dup, unwrap_links(dup), drafting.strip_urls)
    assert not same_claims("a 5 yıl", "a 6 yıl", drafting.strip_urls)

    S, D, C, P = audit_drafts.SAFE, audit_drafts.DUPLICATE, audit_drafts.CONTENT, audit_drafts.PENDING

    def setup():
        ids = ["safe", "wrapped", "fix", "bad", "infra", "dup", "late"]
        verdicts = [S, S, C, C, C, D, C]
        bodies = ["temiz gövde", w, "onarılır gövde", "onarılmaz gövde", "altyapı gövde",
                  "mükerrer gövde", "sıradaki gövde"]
        drafts = {i: {"id": i, "to": f"hi@{i}.io", "subject": "Konu", "body": b}
                  for i, b in zip(ids, bodies)}
        results = [{"id": i, "company": i, "to": f"hi@{i}.io", "domain": f"{i}.io",
                    "subject": "Konu", "verdict": v, "reasons": ["sorun"] if v == C else []}
                   for i, v in zip(ids, verdicts)]
        cache = {"wrapped": {"hash": "eski", "verdict": S, "reasons": []},
                 "bad": {"hash": "x", "verdict": C, "reasons": ["sorun"]}}
        return results, drafts, cache

    def repair_fn(body, _problems, _allow=""):
        if "onarılır" in body:
            return "onarılmış gövde", "", True
        if "altyapı" in body:
            return None, "model cevap vermedi", False
        return None, "hâlâ uydurma", True

    def go(allow_delete, state=None, max_llm=3):
        results, drafts, cache = setup()
        log = {"upd": [], "del": []}
        st = run(results, drafts, state if state is not None else {}, cache, today="2026-10-02",
                 repair_fn=repair_fn,
                 update_fn=lambda i, to, subj, body: log["upd"].append((i, body)) or True,
                 delete_fn=lambda i: log["del"].append(i) or True,
                 key_fn=lambda b: "k:" + audit_drafts.body_hash(b),
                 strip_urls=drafting.strip_urls, max_llm=max_llm, allow_delete=allow_delete)
        return st, results, drafts, cache, log

    # --- silme KAPALI: hiçbir taslak silinmez
    state: dict = {}
    st, results, drafts, cache, log = go(False, state)
    by = {r["id"]: r["verdict"] for r in results}
    assert log["del"] == [] and len(results) == 7
    assert by["fix"] == S and drafts["fix"]["body"] == "onarılmış gövde"
    assert cache["fix"] == {"hash": "k:" + audit_drafts.body_hash("onarılmış gövde"),
                            "verdict": S, "reasons": []}
    # link açılan ✅ taslak ✅ kalır, kararı yeni gövdeye taşınır
    assert by["wrapped"] == S and "google.com/url" not in drafts["wrapped"]["body"]
    assert cache["wrapped"]["hash"].startswith("k:")
    assert ("safe", "temiz gövde") not in log["upd"] and len(log["upd"]) == 2   # wrapped + fix
    assert [x["id"] for x in st["failed"]] == ["bad"] and by["bad"] == C
    assert by["dup"] == D and by["infra"] == C
    assert st["waiting"] == 2                       # infra (altyapı) + late (kota doldu)
    assert set(state[MEMO_KEY]) == {"bad"}          # altyapı hatası onarılamaz diye YAZILMAZ

    # --- ikinci run, silme hâlâ kapalı: onarılamayan için LLM yeniden çağrılmaz
    calls = {"n": 0}
    orig = repair_fn
    def counting(body, problems, allow=""):
        calls["n"] += 1
        return orig(body, problems, allow)
    results, drafts, cache = setup()
    run(results, drafts, state, cache, today="2026-10-03", repair_fn=counting,
        update_fn=lambda *a: True, delete_fn=lambda i: True, key_fn=lambda b: b,
        strip_urls=drafting.strip_urls, max_llm=10, allow_delete=False)
    assert calls["n"] == 3, calls["n"]              # fix, infra, late; bad atlandı

    # --- silme AÇIK: mükerrer + onarılamayan silinir, listeden çıkar
    st, results, drafts, cache, log = go(True, {}, max_llm=10)
    assert sorted(log["del"]) == ["bad", "dup", "late"], log["del"]
    assert {r["id"] for r in results} == {"safe", "wrapped", "fix", "infra"}
    assert "bad" not in cache
    assert {x["why"].split(":")[0] for x in st["deleted"]} == {"mükerrer", "onarılamadı"}

    # --- taslak yazılamazsa karar değişmez
    results, drafts, cache = setup()
    st = run(results, drafts, {}, cache, today="t", repair_fn=repair_fn,
             update_fn=lambda *a: False, delete_fn=lambda i: False, key_fn=lambda b: b,
             strip_urls=drafting.strip_urls, max_llm=10, allow_delete=True)
    by = {r["id"]: r["verdict"] for r in results}
    assert by["fix"] == C and drafts["fix"]["body"] == "onarılır gövde" and len(results) == 7
    assert st["repaired"] == [] and st["deleted"] == []

    assert summary_lines({"unwrapped": [], "repaired": [], "deleted": [], "failed": [],
                          "waiting": 0}, False) == []
    st, *_ = go(True, {}, max_llm=10)
    lines = summary_lines(st, True)
    assert "Otomatik onarım" in lines[0] and any("silindi" in ln for ln in lines)

    # --- yanlış dil: onarım LLM'i çağrılmaz; silme açıksa silinir, kapalıysa başarısız sayılır
    dil_sebep = audit_drafts.LANG_PREFIX + " mail Türkçe yazılmış, şirkete İngilizce yazılmalı"
    for acik in (True, False):
        calls["n"] = 0
        silinen: list = []
        res = [{"id": "tr", "to": "hi@x.io", "domain": "x.io", "subject": "Konu",
                "verdict": C, "reasons": [dil_sebep]}]
        st = run(res, {"tr": {"id": "tr", "to": "hi@x.io", "subject": "Konu", "body": "Merhaba"}},
                 {}, {}, today="t", repair_fn=counting, update_fn=lambda *a: True,
                 delete_fn=lambda i: silinen.append(i) or True, key_fn=lambda b: b,
                 strip_urls=drafting.strip_urls, max_llm=10, allow_delete=acik)
        assert calls["n"] == 0
        assert (silinen, len(st["deleted"]), len(st["failed"])) == \
            ((["tr"], 1, 0) if acik else ([], 0, 1)), (acik, silinen, st)
    print("repair self-test: OK")
