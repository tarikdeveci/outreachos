"""
Tek seferlik migration: outreach_log.csv + state.json  ->  tracker.db
GÜVENLİ / idempotent: mevcut CSV ve JSON'a DOKUNMAZ, sadece okur.
Tekrar çalıştırılırsa companies tablosunu firma bazında upsert eder,
kullanıcının eklediği notlar/durumlar korunur (--force ile sıfırdan kurar).
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import (  # noqa: E402
    get_conn, init_schema, normalize_status, now_iso,
    CSV_PATH, STATE_PATH, DB_PATH,
)


def load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_csv_rows():
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def norm_name(n):
    return (n or "").strip().lower()


def migrate(force=False):
    state = load_state()
    rows = load_csv_rows()

    # state.json: firma -> {draft_id, email, last_reply_seen, channel}
    contacted = {}
    for firma, meta in state.get("companies_already_contacted", {}).items():
        contacted[norm_name(firma)] = meta

    personal_names = set()
    for entry in state.get("excluded_companies_seed_personal", []):
        # "Patientdesk.ai (kurucu ...)" -> ilk kelime öbeği
        base = entry.split("(")[0].strip()
        personal_names.add(norm_name(base))

    conn = get_conn()
    init_schema(conn)
    cur = conn.cursor()

    if force:
        cur.executescript("DELETE FROM companies; DELETE FROM audit_log; DELETE FROM daily_stats;")

    inserted, updated = 0, 0
    daily = {}  # tarih -> {taslak, form, ats, toplam}

    for row in rows:
        firma = (row.get("firma") or "").strip()
        if not firma:
            continue
        durum_raw = (row.get("durum") or "").strip()
        durum, is_personal = normalize_status(durum_raw)

        key = norm_name(firma)
        meta = contacted.get(key, {})
        if key in personal_names:
            is_personal = 1

        draft_id = meta.get("draft_id")
        email = meta.get("email") or (row.get("eposta_veya_link") or "").strip()
        link_or_email = (row.get("eposta_veya_link") or "").strip()
        last_reply = meta.get("last_reply_seen")
        tarih = (row.get("tarih") or "").strip()

        # günlük istatistik toplama
        d = daily.setdefault(tarih, {"taslak": 0, "form": 0, "ats": 0, "toplam": 0})
        if durum == "TASLAK":
            d["taslak"] += 1
        elif durum == "FORM_DOLDURULDU":
            d["form"] += 1
        elif durum == "ATS_DIGEST":
            d["ats"] += 1
        if durum in ("TASLAK", "FORM_DOLDURULDU", "ATS_DIGEST", "ADAY"):
            d["toplam"] += 1

        existing = cur.execute(
            "SELECT id, notlar, durum FROM companies WHERE lower(firma)=?", (key,)
        ).fetchone()

        if existing and not force:
            # Kullanıcı notlarını/durumunu koru; sadece meta alanlarını güncelle
            cur.execute(
                """UPDATE companies SET sektor=?, kanal=?, proje_eslesme=?,
                       durum_raw=?, eposta_veya_link=?, tarih=?, draft_id=?,
                       last_reply_seen=?, is_personal=?, son_guncelleme=?
                   WHERE id=?""",
                (row.get("sektor"), row.get("kanal"), row.get("proje"),
                 durum_raw, link_or_email, tarih, draft_id, last_reply,
                 is_personal, now_iso(), existing["id"]),
            )
            updated += 1
        else:
            cur.execute(
                """INSERT INTO companies
                   (firma, sektor, kanal, proje_eslesme, durum, durum_raw,
                    eposta_veya_link, tarih, draft_id, last_reply_seen,
                    is_personal, notlar, son_guncelleme)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (firma, row.get("sektor"), row.get("kanal"), row.get("proje"),
                 durum, durum_raw, link_or_email, tarih, draft_id, last_reply,
                 is_personal, "", now_iso()),
            )
            inserted += 1

    # daily_stats
    for tarih, d in daily.items():
        if not tarih:
            continue
        cur.execute(
            """INSERT INTO daily_stats (tarih, taslak_sayisi, form_sayisi, ats_sayisi, toplam_temas)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tarih) DO UPDATE SET
                 taslak_sayisi=excluded.taslak_sayisi,
                 form_sayisi=excluded.form_sayisi,
                 ats_sayisi=excluded.ats_sayisi,
                 toplam_temas=excluded.toplam_temas""",
            (tarih, d["taslak"], d["form"], d["ats"], d["toplam"]),
        )

    # profile (olduğu gibi sakla)
    cur.execute(
        "INSERT INTO profile (id, data_json) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
        (json.dumps(state.get("profile", {}), ensure_ascii=False),),
    )

    # app_config: daily_caps + connectors + excluded listeleri
    config_pairs = {
        "daily_caps": json.dumps(state.get("daily_caps", {}), ensure_ascii=False),
        "daily_caps_enabled": "true",  # varsayılan AÇIK
        "excluded_sectors": json.dumps(state.get("excluded_sectors", []), ensure_ascii=False),
        "excluded_companies_seed": json.dumps(state.get("excluded_companies_seed", []), ensure_ascii=False),
        "excluded_companies_seed_personal": json.dumps(state.get("excluded_companies_seed_personal", []), ensure_ascii=False),
        "job_connectors": json.dumps(state.get("job_connectors", {}), ensure_ascii=False),
        "processed_posting_urls": json.dumps(state.get("processed_posting_urls", []), ensure_ascii=False),
        "total_drafts_created_lifetime": str(state.get("total_drafts_created_lifetime", 0)),
        "last_run_date": state.get("last_run_date", ""),
    }
    for k, v in config_pairs.items():
        cur.execute(
            "INSERT INTO app_config (anahtar, deger) VALUES (?,?) ON CONFLICT(anahtar) DO UPDATE SET deger=excluded.deger",
            (k, v),
        )

    conn.commit()

    total = cur.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    conn.close()
    print(f"[migrate] DB: {DB_PATH}")
    print(f"[migrate] {inserted} eklendi, {updated} güncellendi, toplam {total} şirket.")
    print(f"[migrate] {len(daily)} güne ait daily_stats yazıldı.")


if __name__ == "__main__":
    migrate(force="--force" in sys.argv)
