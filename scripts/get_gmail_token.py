#!/usr/bin/env python3
"""
Gmail refresh token'ı bir kez almak için — YERELDE çalıştır.

    python scripts/get_gmail_token.py

Argüman yok, her şeyi sorar. Tarayıcı açılır, Google hesabınla izin verirsin,
token otomatik olarak GitHub secret'a yazılır. Token ekranda GÖRÜNMEZ ve
komut geçmişine düşmez.

Scope: gmail.compose + gmail.readonly + gmail.send — taslak açar, gelen kutusunu
okur ve YALNIZCA kendi adresine günlük rapor yollar. Şirketlere otomatik gönderim
kodda engelli (send yalnızca self-report'ta; hedef adres bağlı hesapla eşleşmezse red).
"""
import os
import http.server
import json
import secrets
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from urllib.request import Request, urlopen

SCOPE = ("https://www.googleapis.com/auth/gmail.compose "
         "https://www.googleapis.com/auth/gmail.send "
         "https://www.googleapis.com/auth/gmail.readonly")
# Refresh token'ın secret olarak yazılacağı private veri reposu ("kullanici/repo").
# Bu dosya public kod reposunda durduğu için kimsenin repo adı burada sabitlenmez;
# herkes kendi fork'unda OUTREACHOS_DATA_REPO ile kendi verisini gösterir.
REPO = os.environ.get("OUTREACHOS_DATA_REPO", "")

_result: dict = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                     # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # Tarayıcı /favicon.ico gibi alakasız istekler de atıyor —
        # bunlara takılıp gerçek callback'i kaçırmamak için görmezden gel.
        if "code" not in q and "error" not in q:
            self.send_response(204)
            self.end_headers()
            return
        _result.update({k: v[0] for k, v in q.items()})
        ok = "code" in _result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("<h2>Tamam, bu sekmeyi kapatabilirsin.</h2>" if ok else
               f"<h2>Hata: {_result.get('error', 'bilinmeyen')}</h2>")
        self.wfile.write(f"<html><body style='font-family:sans-serif'>{msg}</body></html>".encode())

    def log_message(self, *a):                            # sessiz
        pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    print(__doc__)
    cid = input("Google OAuth CLIENT ID  : ").strip()
    secret = input("Google OAuth CLIENT SECRET: ").strip()
    if not (cid and secret):
        print("! Boş bırakılamaz.")
        return 1

    port = free_port()
    redirect = f"http://localhost:{port}"
    state = secrets.token_urlsafe(16)

    # serve_forever: tek istekte durmaz, gerçek callback gelene kadar dinler
    srv = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    auth = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state})

    print("\nTarayıcı açılıyor — Google hesabınla izin ver.")
    print("(Açılmazsa şu adresi elle yapıştır:)\n" + auth + "\n")
    try:
        webbrowser.open(auth)
    except Exception:
        pass

    print("İzin bekleniyor (3 dk)...")
    for _ in range(180):
        if _result:
            break
        threading.Event().wait(1)
    srv.shutdown()

    if not _result:
        print("\n! Zaman aşımı — tarayıcıdan localhost'a hiç istek gelmedi.")
        print("  En sık sebep: OAuth client tipi 'Web application' olarak seçilmiş.")
        print("  Gerekli tip: 'Desktop app' — sadece o tip rastgele localhost portuna izin verir.")
        print("  Console → Credentials → Create credentials → OAuth client ID → Desktop app")
        return 1
    if "code" not in _result:
        print(f"\n! Google hata döndü: {_result.get('error')}")
        if _result.get("error") == "access_denied":
            print("  → Audience sayfasında Publishing status 'Testing' olmalı ve")
            print("    giriş yaptığın adres 'Test users' listesinde bulunmalı.")
        return 1
    if _result.get("state") != state:
        print("\n! state uyuşmadı — güvenlik kontrolü başarısız, tekrar dene.")
        return 1

    req = Request("https://oauth2.googleapis.com/token",
                  data=urllib.parse.urlencode({
                      "code": _result["code"], "client_id": cid, "client_secret": secret,
                      "redirect_uri": redirect, "grant_type": "authorization_code"}).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r:
        data = json.load(r)

    token = data.get("refresh_token")
    if not token:
        print("\n! refresh_token gelmedi. Google Cloud Console'da uygulamanın 'Testing' modunda "
              "olduğundan emin ol, sonra tekrar dene.")
        return 1

    if not REPO:
        print()
        print("Refresh token alindi, ama hangi repoya yazilacagi belli degil.")
        print("  OUTREACHOS_DATA_REPO tanimli degil (or. OUTREACHOS_DATA_REPO=kullanici/veri-repo).")
        print("  Elle ayarla - komut token'i sorar, ekrana yazmaz:")
        print("  gh secret set GMAIL_REFRESH_TOKEN --repo <kullanici>/<veri-repo>")
        return 1

    print("\n✓ Refresh token alındı. GitHub secret'a yazılıyor...")
    try:
        p = subprocess.run(["gh", "secret", "set", "GMAIL_REFRESH_TOKEN", "--repo", REPO],
                           input=token, text=True, capture_output=True, timeout=60)
        if p.returncode == 0:
            print(f"✓ GMAIL_REFRESH_TOKEN → {REPO} ayarlandı.\n")
            print("Sıradaki adım:")
            print(f'  gh workflow run "Günlük iş arama taraması" --repo {REPO} -f dry_run=true')
            return 0
        print(f"! gh hatası: {p.stderr.strip()}")
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"! gh çalıştırılamadı: {e}")

    print("\nElle ayarlamak için (token ekrana yazdırılmıyor, aşağıdaki komut sorar):")
    print(f"  gh secret set GMAIL_REFRESH_TOKEN --repo {REPO}")
    print("  ...ve istendiğinde token'ı yapıştır. Token bu oturumda tekrar gösterilmeyecek,")
    print("  kaybedersen scripti baştan çalıştır.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
