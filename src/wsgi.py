"""WSGI adapter for hosts such as PythonAnywhere.

The application keeps its dependency-free ``BaseHTTPRequestHandler`` core;
this module translates a WSGI request to that handler and translates the
response back without starting a second HTTP server.
"""
from __future__ import annotations

import io
import os
import sys
from http import HTTPStatus

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app import Handler, ensure_db  # noqa: E402


class _Socket:
    def __init__(self, request: bytes):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode, buffering=None):
        return self.input if "r" in mode else self.output

    def sendall(self, data):
        self.output.write(data)


class _Server:
    server_name = "wsgi"
    server_port = 80


def _raw_request(environ) -> bytes:
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")
    target = path + (("?" + query) if query else "")
    body = environ["wsgi.input"].read()

    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
            headers[name] = str(value)
    if environ.get("CONTENT_TYPE"):
        headers["Content-Type"] = environ["CONTENT_TYPE"]
    headers["Content-Length"] = str(len(body))
    headers.setdefault("Host", environ.get("HTTP_HOST", "localhost"))
    headers["Connection"] = "close"

    head = [f"{method} {target} HTTP/1.1"]
    head.extend(f"{name}: {value}" for name, value in headers.items())
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def application(environ, start_response):
    sock = _Socket(_raw_request(environ))
    Handler(sock, (environ.get("REMOTE_ADDR", "127.0.0.1"), 0), _Server())
    raw = sock.output.getvalue()
    header_block, _, body = raw.partition(b"\r\n\r\n")
    lines = header_block.decode("latin-1").split("\r\n")
    status_code = int(lines[0].split(" ", 2)[1])
    reason = HTTPStatus(status_code).phrase
    response_headers = []
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            if name.lower() not in {"connection", "server", "date"}:
                response_headers.append((name, value.strip()))
    start_response(f"{status_code} {reason}", response_headers)
    return [body]


ensure_db()

