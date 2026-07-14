"""Minimal Gmail OAuth + API client using only the Python standard library."""
import base64
import json
import secrets
import time
from email.message import EmailMessage
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.readonly"
_states = {}


def authorization_url(client_id, redirect_uri):
    state = secrets.token_urlsafe(24)
    _states[state] = time.time()
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": SCOPES, "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state}
    return AUTH_URL + "?" + urlencode(params), state


def valid_state(state):
    created = _states.pop(state, 0)
    return bool(created and time.time() - created < 600)


def _post_form(url, data):
    req = Request(url, data=urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=20) as r:
            return json.load(r)
    except HTTPError as e:
        raise RuntimeError(json.loads(e.read().decode()).get("error_description", str(e))) from e


def exchange_code(code, client_id, client_secret, redirect_uri):
    return _post_form(TOKEN_URL, {"code": code, "client_id": client_id, "client_secret": client_secret,
                                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"})


def access_token(config, save_tokens):
    token = config.get("access_token", "")
    if token and float(config.get("token_expires_at", 0)) > time.time() + 60:
        return token
    refresh = config.get("refresh_token", "")
    if not refresh:
        raise RuntimeError("Gmail bağlı değil.")
    data = _post_form(TOKEN_URL, {"refresh_token": refresh, "client_id": config["client_id"],
                                  "client_secret": config["client_secret"], "grant_type": "refresh_token"})
    data.setdefault("refresh_token", refresh)
    save_tokens(data)
    return data["access_token"]


def _api(method, path, token, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(API + path, data=body, method=method,
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=25) as r:
            return json.load(r)
    except HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Gmail API hatası ({e.code}): {detail[:300]}") from e


def create_draft(to, subject, body, token):
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    return _api("POST", "/drafts", token, {"message": {"raw": raw}})


def find_replies(email, token):
    query = urlencode({"q": f"from:({email}) newer_than:30d", "maxResults": 10})
    return _api("GET", "/messages?" + query, token).get("messages", [])
