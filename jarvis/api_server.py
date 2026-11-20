"""Tiny HTTP API so your phone (the future Android app) can talk to Jarvis.

Every route except /health needs the header:  Authorization: Bearer <token>
The token lives in data/api_token.txt (run `python run.py --pair` to see it).
Plain HTTP is fine on your home Wi-Fi; for use away from home put the PC and
phone on Tailscale instead of opening a port on your router.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, tools

log = logging.getLogger("jarvis.api")
MAX_BODY = 64 * 1024


def get_health_status() -> dict:
    """Return health status and version information for the Jarvis service."""
    return {
        "ok": True,
        "name": "jarvis",
        "version": __version__,
        "status": "healthy",
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = f"Jarvis/{__version__}"

    def log_message(self, fmt, *args):  # route access logs to our logger
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- helpers -----------------------------------------------------
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        return hmac.compare_digest(token.encode(), self.server.token.encode())

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---- routes ------------------------------------------------------
    def do_GET(self):
        if self.path == "/health":
            return self._send(200, get_health_status())
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        if self.path == "/tasks":
            return self._send(200, {"tasks": self.server.tasks.list(include_done=True)})
        if self.path == "/status":
            return self._send(200, {"status": tools.call_tool("system_status", {}),
                                    "summary": self.server.tasks.summary()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        body = self._body()
        if self.path == "/command":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._send(400, {"error": "missing 'text'"})
            approve = bool(body.get("auto_confirm", False))
            reply = self.server.brain.handle(text, confirm=lambda _q: approve)
            return self._send(200, {"reply": reply})
        if self.path == "/tasks":
            try:
                task = self.server.tasks.add(
                    str(body.get("title", "")), str(body.get("due", "")),
                    str(body.get("priority", "normal")), bool(body.get("remind", False)))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(201, {"task": task})
        m = re.fullmatch(r"/tasks/(\d+)/complete", self.path)
        if m:
            try:
                return self._send(200, {"task": self.server.tasks.complete(m.group(1))})
            except LookupError as e:
                return self._send(404, {"error": str(e)})
        self._send(404, {"error": "not found"})


def create_server(brain, tasks, host: str, port: int, token: str) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("Refusing to start the API without a token.")
    server = ThreadingHTTPServer((host, port), _Handler)
    server.brain, server.tasks, server.token = brain, tasks, token
    return server


def start_in_thread(brain, tasks, host: str, port: int, token: str) -> ThreadingHTTPServer:
    server = create_server(brain, tasks, host, port, token)
    threading.Thread(target=server.serve_forever, name="jarvis-api", daemon=True).start()
    log.info("API listening on %s:%s", host, port)
    return server
