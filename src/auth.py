"""
Tek-kullanıcılı auth (sıfır bağımlılık).
- Parola PBKDF2-HMAC-SHA256 ile hash'lenir (stdlib hashlib), asla düz metin saklanmaz.
- Session token'ları secrets ile üretilir, cookie'de tutulur.
- Parola hash'i app_config'te; session'lar bellekte (yeniden başlatınca tekrar giriş).
Her kurulum kendi sahibinindir. Ürün genel ama instance tek kişilik.
"""
import hashlib
import hmac
import os
import secrets
import time

sessions = {}  # sid -> son_erisim (epoch)
SESSION_TTL = 60 * 60 * 24 * 7  # 7 gün
ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def new_session() -> str:
    sid = secrets.token_urlsafe(32)
    sessions[sid] = time.time()
    return sid


def valid_session(sid: str) -> bool:
    if not sid or sid not in sessions:
        return False
    if time.time() - sessions[sid] > SESSION_TTL:
        sessions.pop(sid, None)
        return False
    sessions[sid] = time.time()  # kayan pencere
    return True


def end_session(sid: str):
    sessions.pop(sid, None)


def parse_cookie(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out
