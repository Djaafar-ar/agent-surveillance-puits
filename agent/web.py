"""Dashboard and small JSON API (Python standard library only).

GET  /                 dashboard
GET  /api/state        current state (wells, alarms, journal, messages, KPIs)
POST /api/ack          {"alarm_id": 12, "idx": 0}  acknowledge from the dashboard
POST /api/report       {"kind": "daily" | "handover"}  send a report now
POST /api/sms-in       incoming SMS webhook (Android SMS Gateway): "OK" acknowledges

The dashboard listens on 127.0.0.1 by default. To open it to the control-room
network, set web.host to 0.0.0.0 AND a password (environment variable named
by web.password_env); the agent refuses to start otherwise.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import secret

log = logging.getLogger("agent.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def serve(app):
    cfg = app.cfg
    pw = secret(cfg.web.get("password_env"))
    hook_token = secret(cfg.web.get("webhook_token_env", "SMS_WEBHOOK_TOKEN"))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            if not pw:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, p = base64.b64decode(h[6:]).decode().split(":", 1)
                    if hmac.compare_digest(p, pw):
                        return True
                except Exception:  # noqa: BLE001
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="agent"')
            self.end_headers()
            return False

        def _json(self, code, obj):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 100_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            try:
                self._get()
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s", self.path)
                self._json(500, {"error": str(e)})

        def _get(self):
            if not self._auth():
                return
            if self.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "dashboard.html"), "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/api/state":
                self._json(200, app.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            try:
                if self.path.startswith("/api/sms-in"):
                    if hook_token and not hmac.compare_digest(self.path.partition("token=")[2], hook_token):
                        return self._json(403, {"error": "bad token"})
                    b = self._body()
                    p = b.get("payload", b)
                    phone = str(p.get("phoneNumber") or p.get("from") or "")
                    text = str(p.get("message") or p.get("text") or "")
                    done = app.engine.ack_from_phone(phone, text)
                    app.out.journal("info", None, f"SMS reçu de {phone} : « {text} »" + (f" → OK {done}" if done else ""))
                    return self._json(200, {"acknowledged": done})
                if not self._auth():
                    return
                if self.headers.get("X-Requested-With") != "agent":   # blocks cross-site form posts
                    return self._json(403, {"error": "forbidden"})
                b = self._body()
                if self.path == "/api/ack":
                    ok = app.engine.ack(int(b["alarm_id"]), int(b.get("idx", 0)), "dashboard")
                    return self._json(200 if ok else 409, {"ok": ok})
                if self.path == "/api/report":
                    app.send_report(b.get("kind", "daily"))
                    return self._json(200, {"ok": True})
                self._json(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                self._json(400, {"error": str(e)})

    srv = ThreadingHTTPServer((cfg.web["host"], int(cfg.web["port"])), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on http://%s:%s", cfg.web["host"], cfg.web["port"])
    return srv
