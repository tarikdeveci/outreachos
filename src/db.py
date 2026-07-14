"""
Ortak DB katmanı — SQLite şema + normalizasyon kuralları.
Bu iş arama otomasyon sisteminin tek doğruluk kaynağı: tracker.db
CSV (outreach_log.csv) insan-okunur append-only yedek olarak senkron tutulur.
"""
import sqlite3
import json
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "tracker.db")
CSV_PATH = os.path.join(BASE_DIR, "outreach_log.csv")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# --- Kanonik durum enum'u (prompt + gerçek veriden çıkarıldı) ---
STATUSES = [
    "TASLAK", "ATS_DIGEST", "ADAY", "FORM_DOLDURULDU",
    "ELENEN_SENIORITY", "ELENEN_LOKASYON", "ELENEN_SEKTOR", "ELENEN_UYGUNSUZ",
    "ELENEN_EPOSTA_BULUNAMADI", "ELENEN_KISISEL",
    "ARASTIRILMADI", "BEKLIYOR",
]

# Kanban'da gösterilecek aktif akış kolonları (elenenler ayrı sekmede)
KANBAN_COLUMNS = ["ARASTIRILMADI", "TASLAK", "ATS_DIGEST", "FORM_DOLDURULDU", "ADAY", "BEKLIYOR"]

# Gerçek CSV'deki serbest durum -> kanonik enum eşlemesi.
# Ham değer daima durum_raw'da saklanır, hiçbir bilgi kaybolmaz.
def normalize_status(raw: str):
    """(kanonik_durum, is_personal) döndürür."""
    if not raw:
        return "ARASTIRILMADI", 0
    r = raw.strip()
    up = r.upper()
    if up.startswith("ADAY"):
        return "ADAY", 0
    if up.startswith("FORM_DOLDURULDU") or up.startswith("FORM_GONDERILDI"):
        return "FORM_DOLDURULDU", 0
    if up.startswith("ATS_DIGEST"):
        return "ATS_DIGEST", 0
    if up == "TASLAK":
        return "TASLAK", 0
    if up == "ELENEN_KISISEL":
        return "ELENEN_KISISEL", 1
    if up in ("ELENEN_SENIORITY",):
        return "ELENEN_SENIORITY", 0
    if up in ("ELENEN_LOKASYON",):
        return "ELENEN_LOKASYON", 0
    if up in ("ELENEN_SEKTOR",):
        return "ELENEN_SEKTOR", 0
    if up in ("ELENEN_EPOSTA_BULUNAMADI",):
        return "ELENEN_EPOSTA_BULUNAMADI", 0
    if up in ("ELENEN_UYGUNSUZ", "ELENEN_BELIRSIZ", "ELENEN_SITE_YOK"):
        return "ELENEN_UYGUNSUZ", 0
    if up == "ARASTIRILMADI":
        return "ARASTIRILMADI", 0
    if up == "BEKLIYOR":
        return "BEKLIYOR", 0
    # Bilinmeyen bir elenme türü -> genel elenen
    if up.startswith("ELENEN"):
        return "ELENEN_UYGUNSUZ", 0
    return "ARASTIRILMADI", 0


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firma TEXT NOT NULL,
            sektor TEXT,
            kanal TEXT,
            proje_eslesme TEXT,
            durum TEXT NOT NULL DEFAULT 'ARASTIRILMADI',
            durum_raw TEXT,
            eposta_veya_link TEXT,
            tarih TEXT,
            draft_id TEXT,
            last_reply_seen TEXT,
            is_personal INTEGER NOT NULL DEFAULT 0,
            notlar TEXT DEFAULT '',
            son_guncelleme TEXT
        );

        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            data_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            tarih TEXT PRIMARY KEY,
            taslak_sayisi INTEGER NOT NULL DEFAULT 0,
            form_sayisi INTEGER NOT NULL DEFAULT 0,
            ats_sayisi INTEGER NOT NULL DEFAULT 0,
            toplam_temas INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            alan TEXT,
            eski_deger TEXT,
            yeni_deger TEXT,
            aktor TEXT DEFAULT 'user',
            zaman TEXT,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );

        CREATE TABLE IF NOT EXISTS app_config (
            anahtar TEXT PRIMARY KEY,
            deger TEXT
        );
        """
    )
    conn.commit()


def gmail_draft_url(draft_id: str):
    if not draft_id:
        return None
    return f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}"
