#!/usr/bin/env python3
"""
Aday şirket havuzunu tazeler — YERELDE (Türkiye IP'sinden) çalıştır.

    python scripts/refresh_pool.py

Neden ayrı bir script: Türk startup dizinleri (KWORKS, Teknopark İstanbul,
Endeavor, Ege Teknopark, Bilkent Cyberpark, EU-Startups) GitHub Actions
sunucu IP'lerini engelliyor — CI'da hepsi "sayfa çekilemedi" dönüyor, senin
makinenden ise sorunsuz açılıyor. Bu yüzden havuz burada toplanıp
`candidate_pool.json` olarak repoya yazılıyor; günlük CI run'ı o dosyadan
besleniyor ve dizinleri kendisi çekmeye çalışmıyor.

Havuz tükendiğinde (günlük özette "0 yeni aday" görürsen) tekrar çalıştır.
Ayda bir yeterli olur.
"""
import html
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover as d  # noqa: E402

POOL_PATH = os.path.join(d.DATA_DIR, "candidate_pool.json")

# Y Combinator'ın tüm şirket verisi tek JSON olarak açık ve günlük güncelleniyor.
# 6000+ şirket; HTML kazımaya göre kıyaslanamaz derecede iyi çünkü lokasyon,
# ekip büyüklüğü, sektör ve "şu an işe alıyor mu" bilgisi hazır geliyor —
# ülkeyi alan adından tahmin etmeye de gerek kalmıyor.
YC_API = "https://yc-oss.github.io/api/companies/all.json"

# Hacker News "Ask HN: Who is hiring?" — her ay yayınlanıyor, her başlıkta
# 400-500 yorum ve ~250 benzersiz şirket domaini. 12 ay taranınca binlerce eder.
# Bu şirketler tanım gereği İŞE ALIM YAPIYOR, yani soğuk mail için en sıcak liste.
HN_ARA = ("https://hn.algolia.com/api/v1/search_by_date?"
          "query=%22Ask%20HN%3A%20Who%20is%20hiring%22&tags=story&hitsPerPage=24")
HN_ITEM = "https://hn.algolia.com/api/v1/items/{}"


def _ulke(loc: str) -> str:
    """'Berlin, Germany' -> 'Germany'. YC lokasyonu 'şehir, bölge, ülke' formatında."""
    if not loc:
        return "??"
    return loc.split(";")[0].split(",")[-1].strip() or "??"


def yc_havuzu(pool: dict) -> int:
    """YC'nin açık şirket verisinden aday ekler.

    Ülke filtresi UYGULANMIYOR — amaç havuzu olabildiğince büyütmek.
    Ülke bilgisi yine de kaydediliyor; daraltmak istenirse tarama anında
    COUNTRIES ile yapılır, havuzu baştan kısıtlamaya gerek yok.
    """
    try:
        req = d.Request(YC_API, headers={"User-Agent": d.UA})
        with d.urlopen(req, timeout=120) as r:
            veri = json.load(r)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! YC verisi alınamadı: {e}")
        return 0

    eklendi = 0
    for c in veri:
        if c.get("status") != "Active" or not c.get("website"):
            continue
        dom = d.domain_of(c["website"])
        if not dom or d.is_noise(dom) or dom in pool:
            continue
        pool[dom] = {
            "domain": dom,
            "kaynak": f"YC {c.get('batch', '')}".strip(),
            "ulke": _ulke(c.get("all_locations", "")),
            "sehir": (c.get("all_locations") or "").split(",")[0].strip(),
            "sektor": c.get("industry", ""),
            "ekip": c.get("team_size"),
            "ise_aliyor": bool(c.get("isHiring")),
            "aciklama": (c.get("one_liner") or "")[:200],
            "eklendi": date.today().isoformat(),
        }
        eklendi += 1
    return eklendi


def hn_havuzu(pool: dict, ay_sayisi: int = 14) -> int:
    """HN 'Who is hiring' başlıklarının yorumlarından şirket domaini toplar."""
    try:
        req = d.Request(HN_ARA, headers={"User-Agent": d.UA})
        with d.urlopen(req, timeout=60) as r:
            hits = json.load(r).get("hits", [])
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! HN listesi alınamadı: {e}")
        return 0

    basliklar = [h for h in hits if "who is hiring?" in (h.get("title") or "").lower()]
    eklendi = 0
    for h in basliklar[:ay_sayisi]:
        try:
            req = d.Request(HN_ITEM.format(h["objectID"]), headers={"User-Agent": d.UA})
            with d.urlopen(req, timeout=90) as r:
                agac = json.load(r)
        except Exception:                                        # noqa: BLE001
            continue
        metinler: list[str] = []

        def gez(n: dict) -> None:
            for c in n.get("children", []):
                if c.get("text"):
                    metinler.append(c["text"])
                gez(c)

        gez(agac)
        ay = (h.get("title") or "").split("(")[-1].rstrip(")")
        for txt in metinler:
            s = html.unescape(txt)
            for m in re.findall(r"https?://(?:www\.)?([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", s):
                dom = m.lower().rstrip(".")
                if not dom or d.is_noise(dom) or dom in pool:
                    continue
                pool[dom] = {"domain": dom, "kaynak": f"HN Who is hiring ({ay})",
                             "ulke": "??", "ise_aliyor": True,
                             "eklendi": date.today().isoformat()}
                eklendi += 1
    return eklendi

# Sayfalanabilen dizinler: tek sayfa yerine bitene kadar gez.
# İTÜ Çekirdek tek sayfada 10 şirket gösteriyor ama 19 sayfası var (198 şirket) —
# sadece ilk sayfayı almak havuzun %95'ini kaçırıyordu.
PAGINATED = [
    ("https://www.itucekirdek.com/girisimler?page={}", "İTÜ Çekirdek (sayfalı)"),
]


def harvest_paginated(tmpl: str, label: str, pool: dict, max_pages: int = 40) -> int:
    """Yeni sonuç gelmeyi kesene kadar sayfaları gez (3 boş sayfa = son)."""
    added, empty = 0, 0
    for p in range(1, max_pages + 1):
        fresh: dict[str, dict] = {}
        d.harvest_links(tmpl.format(p), label, fresh)
        new = [dom for dom in fresh if dom not in pool]
        for dom in new:
            pool[dom] = {"domain": dom, "kaynak": f"{label} s{p}",
                         "ulke": d.country_of(dom),
                         "eklendi": date.today().isoformat()}
        added += len(new)
        if new:
            empty = 0
        else:
            empty += 1
            if empty >= 3:
                break
    return added


def main() -> int:
    pool: dict[str, dict] = {}
    if os.path.exists(POOL_PATH):
        with open(POOL_PATH, encoding="utf-8") as f:
            pool = {x["domain"]: x for x in json.load(f).get("candidates", [])}
    before = len(pool)

    print(f"Mevcut havuz: {before} domain\n")

    n = yc_havuzu(pool)
    print(f"  {'Y Combinator (açık API)':28s} {n:5d} yeni")
    n = hn_havuzu(pool)
    print(f"  {'HN Who is hiring (14 ay)':28s} {n:5d} yeni")

    for tmpl, label in PAGINATED:
        n = harvest_paginated(tmpl, label, pool)
        print(f"  {label:28s} {n:4d} yeni (sayfalar gezildi)")

    for url, label in d.SEED_DIRECTORIES:
        fresh: dict[str, dict] = {}
        n = d.harvest_links(url, label, fresh)
        new = [dom for dom in fresh if dom not in pool]
        for dom in new:
            pool[dom] = {"domain": dom, "kaynak": label, "ulke": d.country_of(dom),
                         "eklendi": date.today().isoformat()}
        print(f"  {label:28s} {n:4d} link, {len(new):4d} yeni")

    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump({"guncellendi": date.today().isoformat(),
                   "candidates": sorted(pool.values(), key=lambda x: x["domain"])},
                  f, ensure_ascii=False, indent=2)

    print(f"\nHavuz: {before} → {len(pool)} domain ({len(pool) - before} yeni)")
    print(f"Yazıldı: {POOL_PATH}")
    print("\nŞimdi commit'le:")
    print('  git add candidate_pool.json && git commit -m "havuz tazelendi" && git push')
    return 0


if __name__ == "__main__":
    sys.exit(main())
