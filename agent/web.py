"""Dashboard and JSON API (Python standard library only).

GET  /                       dashboard
GET  /api/me                 who is logged in, and the role
GET  /api/state              current state (wells, alarms, journal, messages, KPIs)
GET  /api/analysis           long-term analysis: prioritised findings
GET  /api/logbook            logbook entries (?equipment=PUITS-07&days=7)
GET  /api/handover           preview of the shift handover
GET  /api/production         production of a day (?day=2026-10-28) and deferred production by cause
GET  /api/production.csv     export (?from=2026-10-01&to=2026-10-28)
GET  /api/tests              running, overdue, proposed schedule, recent results
GET  /api/workorders         work orders
POST /api/ask                {"q": "puits 7 cette nuit ?"}                       viewer
POST /api/ack                {"alarm_id": 12, "idx": 0}                          operator
POST /api/logbook            {"kind": "note|status|intervention", "equipment", "status", "text"}   operator
POST /api/tests/result       {"well", "oil_m3d", "water_m3d", "gas_m3d", "note"}  operator
POST /api/tests/check        {"id": 3, "ok": true}                               supervisor
POST /api/workorders/ack     {"id": 5}                                           operator
POST /api/workorders/done    {"id": 5, "note": "..."}                            operator
POST /api/workorders/create  {"entity", "title", "team", "detail", "action", "priority", "due_hours"}  supervisor
POST /api/workorders/cancel  {"id": 5, "reason": "..."}                          supervisor
POST /api/report             {"kind": "daily|handover|weekly|production"}        supervisor
POST /api/sms-in             incoming SMS webhook (Android SMS Gateway): "OK" acknowledges

The dashboard listens on 127.0.0.1 by default. To open it to the control-room
network, set web.host to 0.0.0.0 and create accounts (python -m agent add-user)
or a shared password; the agent refuses to start otherwise. Every change is
signed with the name of the logged-in person.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import secret
from .users import allowed

log = logging.getLogger("agent.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")
LOCAL = {"fr": "poste local", "en": "local desk"}


class Denied(Exception):
    pass


class SetupAPI:
    """The setup wizard's API, used before any configuration exists (local PC only)
    and afterwards by supervisors to change settings."""

    def __init__(self, config_path, cfg=None, on_saved=None):
        self.path = config_path
        self.cfg = cfg
        self.on_saved = on_saved

    def state(self):
        from . import wizard
        from .users import Users
        users = Users(self.cfg) if self.cfg else None
        return {"configured": self.cfg is not None, "form": wizard.current_form(self.cfg),
                "has_users": bool(users), "config_path": os.path.abspath(self.path)}

    def _cfg_from(self, b):
        from . import wizard
        text, secrets = wizard.build(b["form"])
        if b.get("yaml"):
            text = b["yaml"]
        cfg, err = wizard.validate(text, secrets, self.path)
        if err:
            raise ValueError(err)
        return cfg, text, secrets

    def post(self, p, b):
        from . import wizard
        f = b.get("form", {})
        if p == "/api/setup/test-opcua":
            return wizard.test_opcua(f.get("opcua_url", ""), f.get("opcua_user", ""), f.get("opcua_password", ""), f.get("opcua_security", ""))
        if p == "/api/setup/discover":
            v = wizard.discover_opcua(f.get("opcua_url", ""), f.get("opcua_user", ""), f.get("opcua_password", ""), f.get("opcua_security", ""))
            r = wizard.propose(v)
            r["n_variables"] = len(v)
            return r
        if p == "/api/setup/preview":
            from . import wizard as w_
            text, secrets = w_.build(f)
            cfg, err = w_.validate(b.get("yaml") or text, secrets, self.path)
            return {"yaml": text, "error": err, "wells": len(cfg.wells) if cfg else 0}
        if p == "/api/setup/read-tags":
            cfg, _, _ = self._cfg_from(b)
            return wizard.read_once(cfg)
        if p in ("/api/setup/test-sms", "/api/setup/test-email"):
            cfg, _, _ = self._cfg_from(b)
            from .notify import Dispatcher
            d = Dispatcher(cfg, None)
            to = str(b.get("to", "")).strip()
            if not to:
                raise ValueError("recipient required")
            try:
                if p.endswith("sms"):
                    d.smsc.send(to, "Test SMS - agent de surveillance des puits")
                else:
                    d._send_email([to], "Test e-mail - agent de surveillance des puits", "Si vous lisez ce message, l'envoi d'e-mails fonctionne.")
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            where = d.outbox if (cfg.sms.get("type") == "console" and p.endswith("sms")) or (cfg.email.get("type") == "console" and p.endswith("email")) else ""
            return {"ok": True, "outbox": where}
        if p == "/api/setup/save":
            cfg, text, secrets = self._cfg_from(b)
            from .users import Users
            u = Users(cfg)
            adm = b.get("admin") or {}
            if adm.get("name") and adm.get("password"):
                u.add(adm["name"], "supervisor", adm["password"])
            if not u and cfg.web["host"] not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("create a supervisor account before opening the dashboard to the network")
            wizard.save(self.path, text, secrets)
            if self.on_saved:
                threading.Timer(1.0, self.on_saved).start()
            return {"ok": True, "path": os.path.abspath(self.path)}
        raise ValueError("not found")


def serve_setup(config_path, port=8080, host="127.0.0.1"):
    """Wizard alone, before the agent has a configuration. Returns when it is saved."""
    done = threading.Event()
    api = SetupAPI(config_path, None, done.set)

    class S(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj, default=str, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/setup", "/index.html"):
                with open(os.path.join(STATIC, "setup.html"), "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif p == "/api/setup/state":
                self._json(200, api.state())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            try:
                if self.headers.get("X-Requested-With") != "agent":
                    return self._json(403, {"error": "forbidden"})
                n = int(self.headers.get("Content-Length", "0") or 0)
                b = json.loads(self.rfile.read(n) or b"{}")
                self._json(200, api.post(urlparse(self.path).path, b))
            except Exception as e:  # noqa: BLE001
                self._json(400, {"error": str(e)})

    srv = ThreadingHTTPServer((host, port), S)
    threading.Thread(target=srv.serve_forever, daemon=True, name="setup").start()
    log.warning("No configuration yet: open http://%s:%s/setup on this PC to set the agent up", host, port)
    print(f"\n  Setup: open http://{host}:{port}/setup in a browser on this PC\n", flush=True)
    try:
        while not done.wait(1):
            pass
    finally:
        srv.shutdown()
        srv.server_close()


def serve(app):
    cfg = app.cfg
    pw = secret(cfg.web.get("password_env"))
    from .datajobs import DataJobs
    app.data = getattr(app, "data", None) or DataJobs(app)
    setup = SetupAPI(cfg.path, cfg, app.request_restart)
    hook_token = secret(cfg.web.get("webhook_token_env", "SMS_WEBHOOK_TOKEN"))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ---------------------------------------------------------- auth
        def _who(self):
            """(name, role) or None after sending 401."""
            h = self.headers.get("Authorization", "")
            name = p = None
            if h.startswith("Basic "):
                try:
                    name, p = base64.b64decode(h[6:]).decode().split(":", 1)
                except Exception:  # noqa: BLE001
                    pass
            if app.users:
                role = app.users.check(name or "", p or "") if name else None
                if role:
                    return name, role
            elif not pw:
                return LOCAL[cfg.language], "supervisor"
            elif p is not None and hmac.compare_digest(p, pw):
                return (name or LOCAL[cfg.language]), "supervisor"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="agent"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        def _need(self, role, need):
            if not allowed(role, need):
                raise Denied(need)

        def _json(self, code, obj):
            data = json.dumps(obj, default=str, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _raw(self, code, data: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 100_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        # ---------------------------------------------------------- GET
        def do_GET(self):
            try:
                self._get()
            except Denied as e:
                self._json(403, {"error": f"role {e} required"})
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s", self.path)
                self._json(500, {"error": str(e)})

        def _get(self):
            who = self._who()
            if not who:
                return
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            p = u.path
            if p == "/setup":
                self._need(who[1], "supervisor")
                with open(os.path.join(STATIC, "setup.html"), "rb") as f:
                    return self._raw(200, f.read(), "text/html; charset=utf-8", {"X-Frame-Options": "DENY"})
            if p == "/api/setup/state":
                self._need(who[1], "supervisor")
                return self._json(200, setup.state())
            if p.startswith("/reports/"):
                f = app.data.report_path(p[len("/reports/"):])
                if not f:
                    return self._json(404, {"error": "not found"})
                with open(f, "rb") as fh:
                    return self._raw(200, fh.read(), "text/html; charset=utf-8" if f.endswith(".html") else "text/plain; charset=utf-8",
                                     {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})
            if p == "/api/data/jobs":
                self._need(who[1], "supervisor")
                return self._json(200, {"jobs": app.data.list()})
            if p in ("/", "/index.html"):
                with open(os.path.join(STATIC, "dashboard.html"), "rb") as f:
                    data = f.read()
                return self._raw(200, data, "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
                    "X-Frame-Options": "DENY"})
            if p == "/api/me":
                return self._json(200, {"name": who[0], "role": who[1], "multi_user": bool(app.users),
                                        "assistant": cfg.assistant["mode"], "language": cfg.language})
            if p == "/api/state":
                return self._json(200, app.snapshot())
            if p == "/api/analysis":
                return self._json(200, {"findings": app.analysis()})
            if p == "/api/logbook":
                days = float(q.get("days", 2))
                since = app.engine.now - days * 86400
                return self._json(200, {"entries": app.logbook.entries(since, None, q.get("equipment") or None, limit=400),
                                        "statuses": app.logbook.statuses()})
            if p == "/api/handover":
                from .logbook import handover
                s, b = handover(app)
                return self._json(200, {"subject": s, "body": b})
            if p == "/api/production":
                d = date.fromisoformat(q["day"]) if q.get("day") else datetime.fromtimestamp(app.engine.now).date()
                r = app.production.day(d)
                r["causes_30d"] = [{"cause": c, "m3": m, "wells": w} for c, m, w in app.production.causes(d - timedelta(days=29), d)]
                return self._json(200, r)
            if p == "/api/production.csv":
                today = datetime.fromtimestamp(app.engine.now).date()
                d1 = date.fromisoformat(q["to"]) if q.get("to") else today
                d0 = date.fromisoformat(q["from"]) if q.get("from") else d1 - timedelta(days=30)
                return self._raw(200, app.production.csv(d0, d1).encode("utf-8-sig"), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="production_{d0}_{d1}.csv"'})
            if p == "/api/tests":
                wt = app.welltests
                return self._json(200, {"running": wt.running(), "overdue": wt.overdue(), "schedule": wt.schedule(14),
                                        "recent": wt.list(limit=60), "interval_days": cfg.welltests["interval_days"]})
            if p == "/api/workorders":
                return self._json(200, {"orders": app.workorders.list(), "summary": app.workorders.summary()})
            self._json(404, {"error": "not found"})

        # ---------------------------------------------------------- POST
        def do_POST(self):
            try:
                if self.path.startswith("/api/sms-in"):
                    if hook_token and not hmac.compare_digest(self.path.partition("token=")[2], hook_token):
                        return self._json(403, {"error": "bad token"})
                    b = self._body()
                    pl = b.get("payload", b)
                    phone = str(pl.get("phoneNumber") or pl.get("from") or "")
                    text = str(pl.get("message") or pl.get("text") or "")
                    done = app.engine.ack_from_phone(phone, text)
                    app.out.journal("info", None, f"SMS reçu de {phone} : « {text} »" + (f" → OK {done}" if done else ""))
                    return self._json(200, {"acknowledged": done})
                who = self._who()
                if not who:
                    return
                if self.headers.get("X-Requested-With") != "agent":   # blocks cross-site form posts
                    return self._json(403, {"error": "forbidden"})
                name, role = who
                u = urlparse(self.path)
                p = u.path
                if p == "/api/data/upload":
                    self._need(role, "supervisor")
                    nm = parse_qs(u.query).get("name", ["fichier.csv"])[-1]
                    return self._json(200, app.data.save_upload(nm, self.rfile, int(self.headers.get("Content-Length", "0") or 0)))
                b = self._body()
                if p.startswith("/api/setup/"):
                    self._need(role, "supervisor")
                    return self._json(200, setup.post(p, b))
                if p.startswith("/api/data/"):
                    self._need(role, "supervisor")
                    d = app.data
                    if p == "/api/data/guess":
                        return self._json(200, d.guess(b["data"], b.get("tags")))
                    if p == "/api/data/import":
                        return self._json(200, d.run_import(b["data"], b.get("rows", []), b.get("events")))
                    if p == "/api/data/backtest":
                        return self._json(200, d.backtest(b["job"]))
                    if p == "/api/data/history":
                        return self._json(200, d.load_history(b["job"]))
                    if p == "/api/data/job":
                        return self._json(200, d.get(b["job"]) or {"status": "unknown"})
                if p == "/api/ask":
                    self._need(role, "viewer")
                    return self._json(200, app.assistant.answer(str(b.get("q", ""))))
                if p == "/api/ack":
                    self._need(role, "operator")
                    ok = app.engine.ack(int(b["alarm_id"]), int(b.get("idx", 0)), f"dashboard ({name})")
                    return self._json(200 if ok else 409, {"ok": ok})
                if p == "/api/logbook":
                    self._need(role, "operator")
                    kind, eq, text = b.get("kind", "note"), (b.get("equipment") or None), str(b.get("text", ""))
                    if kind == "note":
                        i = app.logbook.note(text, name, eq)
                    elif kind == "status":
                        i = app.logbook.set_status(eq, str(b.get("status")), name, text)
                    elif kind == "intervention":
                        i = app.logbook.intervention(eq, text, name)
                    else:
                        raise ValueError("kind: note, status or intervention")
                    return self._json(200, {"ok": True, "id": i})
                if p == "/api/tests/result":
                    self._need(role, "operator")
                    t = app.welltests.record(b["well"], b["oil_m3d"], b.get("water_m3d"), b.get("gas_m3d"), b.get("whp"), name, str(b.get("note", "")))
                    return self._json(200, t)
                if p == "/api/tests/check":
                    self._need(role, "supervisor")
                    return self._json(200, app.welltests.check(int(b["id"]), bool(b.get("ok")), name))
                if p == "/api/workorders/ack":
                    self._need(role, "operator")
                    return self._json(200, app.workorders.acknowledge(int(b["id"]), name))
                if p == "/api/workorders/done":
                    self._need(role, "operator")
                    return self._json(200, app.workorders.done(int(b["id"]), name, str(b.get("note", ""))))
                if p == "/api/workorders/create":
                    self._need(role, "supervisor")
                    return self._json(200, app.workorders.create(str(b.get("entity", "")), str(b.get("title", "")), str(b.get("team", "")),
                                                                 str(b.get("detail", "")), str(b.get("action", "")),
                                                                 int(b.get("priority", 2)), float(b.get("due_hours", 72)), by=name))
                if p == "/api/workorders/cancel":
                    self._need(role, "supervisor")
                    return self._json(200, app.workorders.cancel(int(b["id"]), name, str(b.get("reason", ""))))
                if p == "/api/report":
                    self._need(role, "supervisor")
                    app.send_report(b.get("kind", "daily"))
                    return self._json(200, {"ok": True})
                self._json(404, {"error": "not found"})
            except Denied as e:
                self._json(403, {"error": f"role {e} required"})
            except Exception as e:  # noqa: BLE001
                self._json(400, {"error": str(e)})

    srv = ThreadingHTTPServer((cfg.web["host"], int(cfg.web["port"])), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on http://%s:%s", cfg.web["host"], cfg.web["port"])
    return srv
