"""Version 2: the office work around the surveillance.

Logbook and well status, interventions, production and deferred production by
cause, work orders with closure check, well tests, shift handover, the
assistant, user roles, restart without paging again, upkeep, and the backtest."""

import base64
import glob
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from agent import config
from agent.app import App
from simulator.feed import Feed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_cfg(tmp_path, changes=None, name="config.yaml"):
    raw = yaml.safe_load(open(os.path.join(ROOT, "config", "config.sim.yaml"), encoding="utf-8"))
    raw.pop("clock", None)
    for k, v in (changes or {}).items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    raw["storage"] = {"path": str(tmp_path / "agent.db")}
    p = tmp_path / name
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return config.load(str(p))


def run(app, feed, minutes, each=None):
    for i in range(minutes):
        if each:
            each(i)
        t, r = feed.step()
        app.process(t, r)


def outbox(tmp_path):
    out = []
    for f in sorted(glob.glob(str(tmp_path / "outbox" / "*.eml"))):
        import email
        m = email.message_from_file(open(f, encoding="utf-8"))
        subj = str(email.header.make_header(email.header.decode_header(m["Subject"])))
        out.append((m["To"], subj, m.get_payload(decode=True).decode("utf-8", "ignore")))
    return out


def roots(app, wid=None):
    return [(a.entity, a.code) for a in app.engine.alarms if a.parent is None and a.t_end is None and (wid is None or a.entity == wid)]


# --------------------------------------------------------------------------- logbook
def test_logbook_status_cause_and_deferred_production(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(2)
    run(app, f, 60)
    app.logbook.set_status("PUITS-03", "shut", "K. Benali", "attente réparation de la duse")
    run(app, f, 120)
    assert not roots(app, "PUITS-03")                      # shut in: no pressure alarm on it
    d = datetime.fromtimestamp(app.engine.now).date()
    w = next(x for x in app.production.day(d)["wells"] if x["well"] == "PUITS-03")
    assert 1.9 < w["shut_h"] < 2.1
    assert abs(w["defer_m3"] - 72 * 2 / 24) < 1.5            # potential 72 m3/d during 2 h
    assert list(w["causes"]) == ["attente réparation de la duse"]
    s = app.snapshot()
    assert next(x for x in s["wells"] if x["id"] == "PUITS-03")["manual"] == "shut"
    from agent.logbook import handover
    subj, body = handover(app)
    assert "PUITS-03 : Fermé (attente réparation de la duse)" in body
    app.logbook.set_status("PUITS-03", "production", "K. Benali", "remis en production")
    run(app, f, 90)
    assert not roots(app, "PUITS-03")
    e = app.logbook.entries(equipment="PUITS-03")
    assert [x["status"] for x in e] == ["production", "shut"] and e[0]["author"] == "K. Benali"


def test_shut_in_without_reason_is_flagged(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(3)
    run(app, f, 30)
    app.logbook.set_status("PUITS-10", "shut", "S. Hamdi", "")
    run(app, f, 5)
    from agent.logbook import handover
    body = handover(app)[1]
    assert "PUITS-10 : fermé sans cause écrite" in body


def test_intervention_relearns_and_closes_the_alarm(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(4)
    run(app, f, 60)
    f.field.set_well("PUITS-07", "duse")
    run(app, f, 40)
    assert ("PUITS-07", "DUSE") in roots(app)
    f.field.set_well("PUITS-07", None)               # the choke is changed
    closed = app.engine.relearn("PUITS-07") if False else None
    app.logbook.intervention("PUITS-07", "Changement de duse 32/64", "K. Benali")
    assert ("PUITS-07", "DUSE") not in roots(app)
    run(app, f, 150)
    assert not roots(app, "PUITS-07")
    assert app.engine.wells["PUITS-07"].base["whp"].ready     # learned again
    assert "PUITS-07" in app.logbook.interventions()


# --------------------------------------------------------------------------- work orders
def test_work_orders_from_alarm_done_verified_and_reopened(tmp_path):
    app = App(make_cfg(tmp_path, {"workorders": {"verify_hours": 2}}))
    f = Feed(5)
    run(app, f, 60)
    f.field.set_well("PUITS-05", "capteur")
    f.field.set_well("PUITS-09", "capteur")
    run(app, f, 45)
    wo = {o["entity"]: o for o in app.workorders.list()}
    assert set(wo) >= {"PUITS-05", "PUITS-09"}
    assert wo["PUITS-05"]["team"] == "instrumentation" and wo["PUITS-05"]["status"] == "open"
    app.out.flush()
    mails = outbox(tmp_path)
    assert any("instrumentation@exemple.dz" in to and "Ordre de travail" in s and "PUITS-05" in s for to, s, b in mails)
    # PUITS-05 really repaired, PUITS-09 declared done but still faulty
    f.field.set_well("PUITS-05", None)
    app.workorders.done(wo["PUITS-05"]["id"], "M. Djebbar", "transmetteur remplacé")
    app.workorders.done(wo["PUITS-09"]["id"], "M. Djebbar", "câble resserré")
    assert "PUITS-05" in app.logbook.interventions()
    run(app, f, 150)
    st = {o["entity"]: o for o in app.workorders.list()}
    assert st["PUITS-05"]["status"] == "verified", st["PUITS-05"]
    assert st["PUITS-09"]["status"] == "reopened", st["PUITS-09"]
    app.out.flush()
    assert any("ROUVERT" in s and "PUITS-09" in s for to, s, b in outbox(tmp_path))
    # a reopened order is not duplicated
    assert sum(o["entity"] == "PUITS-09" for o in app.workorders.list()) == 1


def test_work_order_reminder_and_from_findings(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(6)
    run(app, f, 5)
    o = app.workorders.create("PUITS-02", "Inspecter le coffret", "maintenance", priority=1, due_hours=0.2, by="A. Mansouri")
    run(app, f, 30)
    assert app.workorders.get(o["id"])["reminders"] >= 1
    findings = [{"entity": "PUITS-11", "kind": "annulus_buildup", "severity": 3, "team": "hse", "title": "Pression annulaire en montée",
                 "evidence": "…", "action": "Test de purge", "due_days": 21}]
    app.workorders.from_findings(findings)
    app.workorders.from_findings(findings)               # not twice
    w = [x for x in app.workorders.list() if x["entity"] == "PUITS-11"]
    assert len(w) == 1 and w[0]["priority"] == 3 and w[0]["team"] == "hse"
    with pytest.raises(ValueError):
        app.workorders.cancel(w[0]["id"], "A. Mansouri", "")
    assert app.workorders.cancel(w[0]["id"], "A. Mansouri", "doublon")["status"] == "cancelled"


# --------------------------------------------------------------------------- well tests
def test_well_tests_detection_validation_and_schedule(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(7)
    run(app, f, 30)
    f.field.set_well("PUITS-02", "test")
    run(app, f, 60)
    assert [r["well"] for r in app.welltests.running()] == ["PUITS-02"]
    f.field.set_well("PUITS-02", None)
    run(app, f, 5)
    t = app.welltests.list("PUITS-02")[0]
    assert t["status"] == "ended" and t["end_t"] > t["start_t"]
    t = app.welltests.record("PUITS-02", 36.5, 4.0, None, None, "K. Benali")
    assert t["status"] == "to_validate" and "Premier essai" in t["verdict"]
    app.welltests.check(t["id"], True, "A. Mansouri")
    assert app.welltests.reference("PUITS-02") == 36.5
    assert app.production.ref_rate(app.engine.wells["PUITS-02"]) == (36.5, "test")
    t2 = app.welltests.record("PUITS-02", 24.0, author="K. Benali")       # -34 % with the same pressure
    assert "à refaire" in t2["verdict"]
    app.welltests.check(t2["id"], False, "A. Mansouri")
    assert app.welltests.reference("PUITS-02") == 36.5
    od = [o["well"] for o in app.welltests.overdue()]
    assert "PUITS-02" not in od and len(od) == 11
    plan = app.welltests.schedule(14)
    per = {}
    for p in plan:
        per.setdefault((p["manifold"], p["date"]), []).append(p["well"])
    assert all(len(v) == 1 for v in per.values())                       # one test separator per manifold
    assert "PUITS-02" not in [p["well"] for p in plan]


# --------------------------------------------------------------------------- handover
def test_handover_sent_at_shift_change(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(8, start_minute=5 * 60 + 20)
    run(app, f, 20)
    app.logbook.note("Vent de sable, tournée reportée", "S. Hamdi")
    f.field.set_well("PUITS-01", "chute")
    run(app, f, 40)                                     # passes 06:00
    app.out.flush()
    hand = [(s, b) for to, s, b in outbox(tmp_path) if s.startswith("Relève de quart")]
    assert len(hand) == 1
    body = hand[0][1]
    assert "Vent de sable" in body and "PUITS-01" in body and "À FAIRE" in body
    assert app.logbook.entries(kinds=("handover",))


# --------------------------------------------------------------------------- assistant
def test_assistant_rules(tmp_path):
    app = App(make_cfg(tmp_path))
    f = Feed(9)
    run(app, f, 60)
    f.field.set_well("PUITS-05", "capteur")
    run(app, f, 20)
    app.logbook.set_status("PUITS-12", "shut", "K. Benali", "fuite sur la ligne")
    a = app.assistant.answer("Qu'est-ce qui se passe sur le puits 5 ?")
    assert a["mode"] == "rules" and "PUITS-05" in a["answer"] and "pression tête" in a["answer"] and "Capteur de pression tête défaillant" in a["answer"]
    a = app.assistant.answer("Quels puits sont fermés ?")
    assert "PUITS-12 : Fermé (fuite sur la ligne)" in a["answer"]
    assert "ordre(s) de travail" in app.assistant.answer("ordres de travail en retard ?")["answer"]
    assert "Production du" in app.assistant.answer("production différée aujourd'hui")["answer"]
    assert "en retard d'essai" in app.assistant.answer("quels essais sont en retard")["answer"]
    assert "Exemples" in app.assistant.answer("aide")["answer"]
    assert app.assistant.wells_in("P7 et PUITS-11, well 3") == ["PUITS-11", "PUITS-07", "PUITS-03"]


class _FakeModel(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _FakeModel.seen.append(json.loads(self.rfile.read(n)))
        data = json.dumps({"message": {"content": "Le puits 5 a un transmetteur en défaut depuis 20 minutes."}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_assistant_local_model_and_fallback(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        app = App(make_cfg(tmp_path, {"assistant": {"mode": "local", "url": f"http://127.0.0.1:{srv.server_port}"}}))
        f = Feed(9)
        run(app, f, 60)
        f.field.set_well("PUITS-05", "capteur")
        run(app, f, 20)
        a = app.assistant.answer("puits 5 ?")
        assert a["mode"] == "local" and a["answer"].startswith("Le puits 5")
        sent = _FakeModel.seen[-1]["messages"][-1]["content"]
        assert "FAITS" in sent and "PUITS-05" in sent and "Capteur de pression tête défaillant" in sent
    finally:
        srv.shutdown()
    app.assistant.c["url"] = "http://127.0.0.1:9"            # nothing listening: answer without AI
    app.assistant.c["timeout_s"] = 2
    a = app.assistant.answer("puits 5 ?")
    assert a["mode"] == "rules" and "Modèle local indisponible" in a["answer"] and "PUITS-05" in a["answer"]


def test_assistant_refuses_remote_model(tmp_path):
    with pytest.raises(config.ConfigError, match="must be on the site network"):
        make_cfg(tmp_path, {"assistant": {"mode": "local", "url": "https://api.example.com"}})


# --------------------------------------------------------------------------- users and roles
def _http(method, url, body=None, user=None, pw=None, csrf=True):
    h = {"Content-Type": "application/json"}
    if csrf:
        h["X-Requested-With"] = "agent"
    if user:
        h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def test_user_roles_on_the_dashboard(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = make_cfg(tmp_path, {"web": {"host": "127.0.0.1", "port": port}})
    from agent.users import Users
    u = Users(cfg)
    u.add("Lecteur", "viewer", "lecture-123")
    u.add("K. Benali", "operator", "operateur-123")
    u.add("A. Mansouri", "supervisor", "superviseur-123")
    with pytest.raises(ValueError):
        u.add("court", "operator", "123")
    app = App(cfg)
    f = Feed(10)
    run(app, f, 10)
    from agent.web import serve
    srv = serve(app)
    base = f"http://127.0.0.1:{port}"
    try:
        assert _http("GET", base + "/api/state")[0] == 401
        assert _http("GET", base + "/api/state", user="K. Benali", pw="faux")[0] == 401
        code, me = _http("GET", base + "/api/me", user="Lecteur", pw="lecture-123")
        assert code == 200 and me["role"] == "viewer"
        assert _http("POST", base + "/api/logbook", {"kind": "note", "text": "x"}, "Lecteur", "lecture-123")[0] == 403
        assert _http("POST", base + "/api/ask", {"q": "alarmes"}, "Lecteur", "lecture-123")[0] == 200
        code, r = _http("POST", base + "/api/logbook", {"kind": "note", "text": "Ronde faite, RAS"}, "K. Benali", "operateur-123")
        assert code == 200
        assert app.logbook.entries()[0]["author"] == "K. Benali"
        assert _http("POST", base + "/api/logbook", {"kind": "note", "text": "x"}, "K. Benali", "operateur-123", csrf=False)[0] == 403
        assert _http("POST", base + "/api/report", {"kind": "daily"}, "K. Benali", "operateur-123")[0] == 403
        assert _http("POST", base + "/api/report", {"kind": "production"}, "A. Mansouri", "superviseur-123")[0] == 200
        code, o = _http("POST", base + "/api/workorders/create", {"entity": "PUITS-04", "title": "Contrôle duse", "team": "production"},
                        "A. Mansouri", "superviseur-123")
        assert code == 200 and o["created_by"] == "A. Mansouri"
        assert _http("POST", base + "/api/workorders/done", {"id": o["id"], "note": "ok"}, "K. Benali", "operateur-123")[0] == 200
        assert _http("POST", base + "/api/logbook", {"kind": "status", "equipment": "PUITS-99", "status": "shut", "text": ""},
                     "K. Benali", "operateur-123")[0] == 400
        for p in ("/api/production", "/api/tests", "/api/workorders", "/api/logbook", "/api/handover"):
            assert _http("GET", base + p, user="Lecteur", pw="lecture-123")[0] == 200, p
        req = urllib.request.Request(base + "/api/production.csv", headers={"Authorization": "Basic " + base64.b64encode(b"Lecteur:lecture-123").decode()})
        with urllib.request.urlopen(req) as r:
            assert r.read().decode("utf-8-sig").startswith("day;well;state")
    finally:
        srv.shutdown()
    stored = json.load(open(tmp_path / "users.json", encoding="utf-8"))
    assert "operateur-123" not in json.dumps(stored)


# --------------------------------------------------------------------------- 24/7
def test_restart_resumes_alarm_without_paging_again(tmp_path):
    cfg = make_cfg(tmp_path)
    app = App(cfg)
    f = Feed(11)
    run(app, f, 60)
    f.field.set_well("PUITS-01", "chute")
    run(app, f, 15)
    a = next(x for x in app.engine.alarms if x.code == "P_CHUTE")
    assert a.notified == [0]
    app.engine.ack(a.id, 0, "SMS")
    t0 = a.t0
    app.store.flush_all()
    app.upkeep.save_baselines()              # done every 10 min and when the agent stops
    sms_before = app.out.sent_sms
    # the agent stops and restarts 2 minutes later
    for _ in range(2):
        f.step()
    app2 = App(config.load(cfg.path))
    run(app2, f, 90)
    b = next(x for x in app2.engine.alarms if x.code == "P_CHUTE")
    assert b.t0 == t0 and b.ack is not None and b.ack[2] == "SMS"
    assert app2.out.sent_sms == 0, "the on-call technician must not be paged again"
    hist = [r for r in app2.store.alarm_history(0) if r[1] == "P_CHUTE"]
    assert len(hist) == 1
    assert sms_before == 1


def test_restart_closes_alarms_that_cleared_meanwhile(tmp_path):
    cfg = make_cfg(tmp_path)
    app = App(cfg)
    f = Feed(12)
    run(app, f, 60)
    f.field.set_well("PUITS-08", "capteur")
    run(app, f, 10)
    assert ("PUITS-08", "CAPTEUR") in roots(app)
    f.field.set_well("PUITS-08", None)
    app2 = App(config.load(cfg.path))
    run(app2, f, 40)
    assert not [r for r in app2.store.alarm_history(0) if r[1] == "CAPTEUR" and r[4] is None]


def test_upkeep_heartbeat_backup_retention(tmp_path):
    cfg = make_cfg(tmp_path)
    app = App(cfg)
    f = Feed(13)
    run(app, f, 3)
    from agent.upkeep import status
    code, txt = status(cfg)
    assert "last heartbeat" in txt
    p = app.upkeep.backup()
    assert os.path.exists(p) and os.path.getsize(p) > 0
    app.store.journal(time.time() - 800 * 86400, "info", None, "very old")
    app.upkeep.retention()
    assert not [j for j in app.store.recent_journal(500) if j["text"] == "very old"]
    assert app.upkeep.disk() > 0


# --------------------------------------------------------------------------- backtest
def test_backtest_proof_report(tmp_path):
    from agent.backtest import Backtest
    from simulator.export import generate
    exp, ev = generate(str(tmp_path / "bt"), 365)
    cfg = make_cfg(tmp_path)
    bt = Backtest(cfg, exp, ev, str(tmp_path / "bt" / "out")).run()
    path = bt.write()
    s = bt.summary
    res = {(e.equipment, e.text[:20]): e.result for e in bt.problems}
    assert s["problems"] == 11
    assert s["ahead"] >= 7, bt.text()
    assert res[("PUITS-12", "Défaut de la vanne d")] == "missed"           # not visible in the data: honest miss
    assert res[("PUITS-11", "Purge annulaire 9 5/")] == "ahead"
    assert res[("PUITS-06", "Remplacement de la b")] == "ahead"
    assert res[("PUITS-03", "Puits fermé : pressi")] == "ahead"
    assert res[("PUITS-09", "Fuite sur la tête de")] == "at_time"
    assert s["unconfirmed"] <= 4
    html = open(path, encoding="utf-8").read()
    assert "Rapport de backtest" in html and "Non vu" in html
    assert not glob.glob(str(tmp_path / "bt" / "out" / "outbox" / "*.eml")), "a backtest must not send anything"
