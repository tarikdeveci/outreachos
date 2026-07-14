"""
İş Arama Otomasyon Dashboard — self-hostable ürün (sıfır bağımlılık çekirdek).
Çalıştır:  python src/app.py   ->  http://localhost:8787
  /        splash (public tanıtım)
  /login   giriş (ilk kurulumda parola belirleme)
  /app     dashboard (auth gerekli)
DB birincil (tracker.db). Her düzenleme audit_log'a yazılır ve CSV'ye senkronlanır.
AI özellikleri BYOK: kullanıcı kendi Anthropic anahtarını Ayarlar'dan bağlar.
"""
import json
import os
import sys
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import (  # noqa: E402
    get_conn, init_schema, now_iso, gmail_draft_url,
    STATUSES, KANBAN_COLUMNS, CSV_PATH, DB_PATH,
)
import auth  # noqa: E402
import ai  # noqa: E402

PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")  # Docker/deploy'da 0.0.0.0
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

PUBLIC_API = {"/api/session", "/api/setup", "/api/login", "/api/logout"}


def ensure_db():
    if not os.path.exists(DB_PATH):
        print("[app] tracker.db yok — önce 'python src/migrate.py' çalıştırın.")
        sys.exit(1)
    conn = get_conn()
    init_schema(conn)
    # Deploy kolaylığı: DASHBOARD_PASSWORD env'i varsa parolayı ondan (yeniden) kur.
    env_pw = os.environ.get("DASHBOARD_PASSWORD")
    if env_pw:
        conn.execute(
            "INSERT INTO app_config (anahtar, deger) VALUES ('app_password_hash', ?) "
            "ON CONFLICT(anahtar) DO UPDATE SET deger=excluded.deger",
            (auth.hash_password(env_pw),))
        conn.commit()
    conn.close()


def cfg_get(conn, key, default=None):
    r = conn.execute("SELECT deger FROM app_config WHERE anahtar=?", (key,)).fetchone()
    return r["deger"] if r else default


def cfg_set(conn, key, value):
    conn.execute(
        "INSERT INTO app_config (anahtar, deger) VALUES (?,?) ON CONFLICT(anahtar) DO UPDATE SET deger=excluded.deger",
        (key, value))


def row_to_company(r):
    d = dict(r)
    d["draft_url"] = gmail_draft_url(d.get("draft_id"))
    d["is_personal"] = bool(d.get("is_personal"))
    d["is_elenen"] = str(d.get("durum", "")).startswith("ELENEN")
    return d


def sync_csv():
    conn = get_conn()
    rows = conn.execute(
        "SELECT tarih, firma, sektor, kanal, proje_eslesme, durum_raw, durum, eposta_veya_link "
        "FROM companies ORDER BY id").fetchall()
    conn.close()
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tarih", "firma", "sektor", "kanal", "proje", "durum", "eposta_veya_link"])
        for r in rows:
            w.writerow([r["tarih"], r["firma"], r["sektor"], r["kanal"],
                        r["proje_eslesme"], r["durum_raw"] or r["durum"], r["eposta_veya_link"]])


def write_audit(conn, company_id, alan, eski, yeni, aktor="user"):
    conn.execute(
        "INSERT INTO audit_log (company_id, alan, eski_deger, yeni_deger, aktor, zaman) VALUES (?,?,?,?,?,?)",
        (company_id, alan, str(eski) if eski is not None else None,
         str(yeni) if yeni is not None else None, aktor, now_iso()))


def mask_key(k):
    if not k:
        return ""
    return (k[:6] + "…" + k[-4:]) if len(k) > 12 else "••••"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ---------- helpers ----------
    def _json(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, filename):
        fp = os.path.join(STATIC_DIR, filename)
        if not os.path.abspath(fp).startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(fp):
            self.send_error(404); return
        types = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}
        ext = os.path.splitext(fp)[1]
        ctype = types.get(ext, "text/plain") + ("; charset=utf-8" if ext in (".html", ".css", ".js", ".svg") else "")
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _sid(self):
        return auth.parse_cookie(self.headers.get("Cookie", "")).get("sid")

    def _authed(self):
        return auth.valid_session(self._sid())

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html", "/splash"):
            return self._static("splash.html")
        if p == "/login":
            return self._static("login.html")
        if p == "/app":
            return self._static("index.html")
        if p.endswith((".css", ".js", ".svg", ".png", ".ico", ".woff2")):
            return self._static(p.lstrip("/"))
        if p.startswith("/api/"):
            if p not in PUBLIC_API and not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            return self._get_api(p, parse_qs(u.query))
        return self.send_error(404)

    def _get_api(self, p, q):
        if p == "/api/session":
            return self.api_session()
        if p == "/api/meta":
            return self._json({"statuses": STATUSES, "kanban_columns": KANBAN_COLUMNS})
        if p == "/api/companies":
            return self.api_companies(q)
        if p.startswith("/api/company/"):
            return self.api_company_detail(int(p.rsplit("/", 1)[1]))
        if p == "/api/stats":
            return self.api_stats()
        if p == "/api/profile":
            return self.api_profile()
        if p == "/api/settings":
            return self.api_get_settings()
        return self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p not in PUBLIC_API and not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        if p == "/api/setup":
            return self.api_setup()
        if p == "/api/login":
            return self.api_login()
        if p == "/api/logout":
            return self.api_logout()
        if p == "/api/settings":
            return self.api_set_settings()
        if p == "/api/companies":
            return self.api_create_company()
        if p.startswith("/api/company/") and p.endswith("/draft"):
            return self.api_ai_draft(int(p.split("/")[3]))
        if p.startswith("/api/company/") and p.endswith("/match"):
            return self.api_ai_match(int(p.split("/")[3]))
        return self.send_error(404)

    def do_PATCH(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        if u.path.startswith("/api/company/"):
            return self.api_update_company(int(u.path.rsplit("/", 1)[1]))
        self.send_error(404)

    # ---------- auth endpoints ----------
    def api_session(self):
        conn = get_conn()
        configured = cfg_get(conn, "app_password_hash") is not None
        ai_key = cfg_get(conn, "anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
        conn.close()
        self._json({
            "configured": configured,
            "authed": self._authed(),
            "ai_available": ai.available(),
            "ai_connected": bool(ai_key),
        })

    def api_setup(self):
        data = self._body_json()
        pw = (data.get("password") or "").strip()
        conn = get_conn()
        if cfg_get(conn, "app_password_hash") is not None:
            conn.close()
            return self._json({"error": "zaten kurulu"}, 409)
        if len(pw) < 6:
            conn.close()
            return self._json({"error": "parola en az 6 karakter olmalı"}, 400)
        cfg_set(conn, "app_password_hash", auth.hash_password(pw))
        conn.commit(); conn.close()
        sid = auth.new_session()
        self._set_session_cookie(sid)

    def api_login(self):
        data = self._body_json()
        pw = data.get("password") or ""
        conn = get_conn()
        stored = cfg_get(conn, "app_password_hash")
        conn.close()
        if not stored or not auth.verify_password(pw, stored):
            return self._json({"error": "parola hatalı"}, 401)
        sid = auth.new_session()
        self._set_session_cookie(sid)

    def _set_session_cookie(self, sid):
        cookie = f"sid={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_TTL}"
        self._json({"ok": True}, 200, extra_headers=[("Set-Cookie", cookie)])

    def api_logout(self):
        auth.end_session(self._sid())
        self._json({"ok": True}, 200, extra_headers=[("Set-Cookie", "sid=; Path=/; Max-Age=0")])

    # ---------- settings / BYOK ----------
    def api_get_settings(self):
        conn = get_conn()
        profile_row = conn.execute("SELECT data_json FROM profile WHERE id=1").fetchone()
        profile = json.loads(profile_row["data_json"]) if profile_row else {}
        settings = {
            "profile": profile,
            "anthropic_key_masked": mask_key(cfg_get(conn, "anthropic_api_key")),
            "anthropic_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "search_provider": cfg_get(conn, "search_provider", ""),
            "search_key_masked": mask_key(cfg_get(conn, "search_api_key")),
            "daily_caps_enabled": (cfg_get(conn, "daily_caps_enabled", "true") == "true"),
            "ai_available": ai.available(),
        }
        conn.close()
        self._json(settings)

    def api_set_settings(self):
        data = self._body_json()
        conn = get_conn()
        if "profile" in data and isinstance(data["profile"], dict):
            conn.execute(
                "INSERT INTO profile (id, data_json) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
                (json.dumps(data["profile"], ensure_ascii=False),))
        for key in ("anthropic_api_key", "search_api_key", "search_provider"):
            if data.get(key):  # boş gönderilirse mevcut değeri koru
                cfg_set(conn, key, data[key].strip())
        if "daily_caps_enabled" in data:
            cfg_set(conn, "daily_caps_enabled", "true" if data["daily_caps_enabled"] else "false")
        conn.commit(); conn.close()
        self._json({"ok": True})

    # ---------- AI (BYOK) ----------
    def _company_and_profile(self, cid):
        conn = get_conn()
        r = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        prof = conn.execute("SELECT data_json FROM profile WHERE id=1").fetchone()
        key = cfg_get(conn, "anthropic_api_key") or ""
        conn.close()
        company = dict(r) if r else None
        profile = json.loads(prof["data_json"]) if prof else {}
        return company, profile, key

    def api_ai_draft(self, cid):
        company, profile, key = self._company_and_profile(cid)
        if not company:
            return self.send_error(404)
        if company.get("is_personal"):
            return self._json({"ok": False, "error": "Kişisel/tanıdık şirketi — otomatik taslak üretilmez, manuel karar verin."}, 400)
        result = ai.generate_draft(company, profile, key)
        self._json(result)

    def api_ai_match(self, cid):
        company, profile, key = self._company_and_profile(cid)
        if not company:
            return self.send_error(404)
        self._json(ai.match_project(company, profile, key))

    # ---------- data endpoints ----------
    def api_companies(self, q):
        conn = get_conn()
        where, params = [], []
        if q.get("durum"):
            where.append("durum=?"); params.append(q["durum"][0])
        if q.get("kanal"):
            where.append("kanal=?"); params.append(q["kanal"][0])
        if q.get("proje"):
            where.append("proje_eslesme LIKE ?"); params.append(f"%{q['proje'][0]}%")
        if q.get("sektor"):
            where.append("sektor LIKE ?"); params.append(f"%{q['sektor'][0]}%")
        if q.get("q"):
            where.append("(firma LIKE ? OR sektor LIKE ? OR notlar LIKE ?)")
            t = f"%{q['q'][0]}%"; params += [t, t, t]
        sql = "SELECT * FROM companies"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY firma COLLATE NOCASE"
        rows = [row_to_company(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
        self._json(rows)

    def api_create_company(self):
        data = self._body_json()
        firma = (data.get("firma") or "").strip()
        if not firma:
            return self._json({"error": "firma zorunlu"}, 400)
        durum = data.get("durum") or "ARASTIRILMADI"
        if durum not in STATUSES:
            durum = "ARASTIRILMADI"
        tarih = (data.get("tarih") or now_iso()[:10]).strip()
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO companies
               (firma, sektor, kanal, proje_eslesme, durum, durum_raw, eposta_veya_link,
                tarih, is_personal, notlar, son_guncelleme)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (firma, data.get("sektor", ""), data.get("kanal", ""), data.get("proje_eslesme", ""),
             durum, durum, data.get("eposta_veya_link", ""), tarih,
             1 if data.get("is_personal") else 0, data.get("notlar", ""), now_iso()))
        cid = cur.lastrowid
        write_audit(conn, cid, "olusturuldu", None, firma)
        conn.commit()
        r = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        conn.close()
        sync_csv()
        self._json(row_to_company(r), 201)

    def api_company_detail(self, cid):
        conn = get_conn()
        r = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if not r:
            conn.close(); return self.send_error(404)
        company = row_to_company(r)
        audit = [dict(a) for a in conn.execute(
            "SELECT * FROM audit_log WHERE company_id=? ORDER BY id DESC", (cid,)).fetchall()]
        conn.close()
        self._json({"company": company, "audit": audit})

    def api_update_company(self, cid):
        data = self._body_json()
        allowed = {"durum", "notlar", "is_personal", "proje_eslesme",
                   "eposta_veya_link", "draft_id", "last_reply_seen", "sektor"}
        conn = get_conn()
        cur = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if not cur:
            conn.close(); return self.send_error(404)
        old = dict(cur)
        for k, v in data.items():
            if k not in allowed:
                continue
            if k == "is_personal":
                v = 1 if v else 0
            if str(old.get(k)) == str(v):
                continue
            write_audit(conn, cid, k, old.get(k), v)
            conn.execute(f"UPDATE companies SET {k}=? WHERE id=?", (v, cid))
            if k == "durum":
                conn.execute("UPDATE companies SET durum_raw=? WHERE id=?", (v, cid))
        conn.execute("UPDATE companies SET son_guncelleme=? WHERE id=?", (now_iso(), cid))
        conn.commit()
        r = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        conn.close()
        sync_csv()
        self._json(row_to_company(r))

    def api_stats(self):
        conn = get_conn()
        total = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
        by_status = {r["durum"]: r["n"] for r in conn.execute(
            "SELECT durum, COUNT(*) n FROM companies GROUP BY durum").fetchall()}
        latest = conn.execute("SELECT MAX(tarih) t FROM companies").fetchone()["t"]
        today_new = conn.execute(
            "SELECT COUNT(*) c FROM companies WHERE tarih=? AND durum IN ('TASLAK','FORM_DOLDURULDU','ATS_DIGEST','ADAY')",
            (latest,)).fetchone()["c"]
        today_drafts = conn.execute(
            "SELECT COUNT(*) c FROM companies WHERE tarih=? AND durum='TASLAK'", (latest,)).fetchone()["c"]
        daily = [dict(d) for d in conn.execute("SELECT * FROM daily_stats ORDER BY tarih").fetchall()]
        caps_row = cfg_get(conn, "daily_caps")
        caps = json.loads(caps_row) if caps_row else {}
        caps_en = cfg_get(conn, "daily_caps_enabled", "true")
        aday = by_status.get("ADAY", 0); taslak = by_status.get("TASLAK", 0)
        elenen = sum(v for k, v in by_status.items() if k.startswith("ELENEN"))
        conn.close()
        self._json({
            "total": total, "today_new": today_new, "today_drafts": today_drafts,
            "by_status": by_status, "daily": daily, "daily_caps": caps,
            "daily_caps_enabled": (caps_en == "true"),
            "latest_date": latest, "aday": aday, "taslak": taslak, "elenen": elenen,
        })

    def api_profile(self):
        conn = get_conn()
        r = conn.execute("SELECT data_json FROM profile WHERE id=1").fetchone()
        conn.close()
        self._json(json.loads(r["data_json"]) if r else {})


def main():
    ensure_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[app] İş Arama Dashboard  ->  http://localhost:{PORT}")
    print(f"[app] AI (anthropic SDK): {'kurulu' if ai.available() else 'KURULU DEĞİL (pip install anthropic)'}")
    print("[app] Durdurmak için Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] kapatıldı.")


if __name__ == "__main__":
    main()
