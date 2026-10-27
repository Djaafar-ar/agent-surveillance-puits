"""End-to-end tests: the real agent against a real OPC UA server (the field
simulator), a real Modbus TCP server, a CSV historian export, and the
dashboard HTTP API."""

import base64
import csv
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from agent import config
from agent.app import App

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, url, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return 0, {}


def wait(pred, timeout, step=0.2):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def write_cfg(tmp_path, changes):
    raw = yaml.safe_load(open(os.path.join(ROOT, "config", "config.sim.yaml"), encoding="utf-8"))
    for k, v in changes.items():
        if v is None:
            raw.pop(k, None)
        elif isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    raw["storage"] = {"path": str(tmp_path / "agent.db")}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return str(p)


@pytest.fixture
def sim(request):
    procs = []

    def start(*extra):
        port, ctl = free_port(), free_port()
        p = subprocess.Popen([sys.executable, "-m", "simulator.sim_server", "--port", str(port), "--control-port", str(ctl), *extra],
                             cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(p)
        ok = wait(lambda: _up(ctl), 20)
        assert ok, "simulator did not start"
        return p, port, ctl
    yield start
    for p in procs:
        p.terminate()
        p.wait(5)


def _up(ctl):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{ctl}/state", timeout=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def start_app(cfg_path):
    cfg = config.load(cfg_path)
    app = App(cfg)
    th = threading.Thread(target=app.run, daemon=True)
    th.start()
    return app


# --------------------------------------------------------------------------- OPC UA
def test_opcua_end_to_end(tmp_path, sim):
    p, port, ctl = sim("--speed", "300")
    web = free_port()
    cfg_path = write_cfg(tmp_path, {"connector": {"url": f"opc.tcp://127.0.0.1:{port}/agent-sim/"}, "web": {"port": web}})
    app = start_app(cfg_path)
    base = f"http://127.0.0.1:{web}"
    try:
        st = wait(lambda: (lambda s: s if s[1].get("cycles", 0) > 40 and not any(w["learning"] for w in s[1]["wells"]) else None)(http("GET", base + "/api/state")), 60)
        assert st, "agent did not learn the normal values"
        s = st[1]
        assert s["link"]["ok"] and s["active"] == []
        assert all(w["age_min"] < 2 for w in s["wells"]), "all RTUs should be online"
        # repeater failure
        http("POST", f"http://127.0.0.1:{ctl}/group", {"id": "MAN-B", "k": "rep"})
        got = wait(lambda: [a for a in http("GET", base + "/api/state")[1]["active"] if a["code"] == "REP"], 30)
        assert got, "repeater failure not detected over OPC UA"
        rep = got[0]
        assert len(rep["children"]) == 4
        # dashboard ack needs the anti-CSRF header
        assert http("POST", base + "/api/ack", {"alarm_id": rep["id"], "idx": 0})[0] == 403
        code, r = http("POST", base + "/api/ack", {"alarm_id": rep["id"], "idx": 0}, {"X-Requested-With": "agent"})
        assert code == 200 and r["ok"]
        # SMS reply "OK" through the Android gateway webhook, on another alarm
        http("POST", f"http://127.0.0.1:{ctl}/well", {"id": "PUITS-03", "k": "sdv"})
        sdv = wait(lambda: [a for a in http("GET", base + "/api/state")[1]["active"] if a["code"] == "SDV"], 30)
        assert sdv
        code, r = http("POST", base + "/api/sms-in", {"event": "sms:received", "payload": {"phoneNumber": "+213600000001", "message": "OK"}})
        assert code == 200 and sdv[0]["id"] in r["acknowledged"]
        # restore: everything clears
        http("POST", f"http://127.0.0.1:{ctl}/reset", {})
        assert wait(lambda: not http("GET", base + "/api/state")[1]["active"], 60), "alarms did not clear"
        # dashboard page is served
        with urllib.request.urlopen(base + "/") as r:
            assert b"Agent de surveillance" in r.read()
        app.out.flush()
        log = open(tmp_path / "outbox" / "sms.log", encoding="utf-8").read()
        assert "URGENCE Répéteur R2" in log and "FIN ALARME" in log
        assert glob.glob(str(tmp_path / "outbox" / "*.eml")), "alarm e-mails should be written"
    finally:
        app.stop.set()
        if app.srv:
            app.srv.shutdown()


def test_opcua_real_clock_and_link_loss(tmp_path, sim):
    """Real-time mode (no simulator clock): if the SCADA disappears, ONE alarm 'SCADA link lost'."""
    p, port, ctl = sim("--speed", "60", "--wallclock")
    web = free_port()
    cfg_path = write_cfg(tmp_path, {"clock": None, "poll_seconds": 5,
                                    "connector": {"url": f"opc.tcp://127.0.0.1:{port}/agent-sim/", "timeout_s": 2},
                                    "thresholds": {"rtu_timeout_min": 0.25}, "web": {"port": web}})
    app = start_app(cfg_path)
    base = f"http://127.0.0.1:{web}"
    try:
        assert wait(lambda: http("GET", base + "/api/state")[1].get("cycles", 0) >= 2, 30)
        s = http("GET", base + "/api/state")[1]
        assert s["link"]["ok"] and all(w["age_min"] < 1 for w in s["wells"])
        p.terminate()
        p.wait(5)
        got = wait(lambda: http("GET", base + "/api/state")[1]["link_alarms"], 60, 1)
        assert got and got[0]["code"] == "SCADA_LINK"
        s = http("GET", base + "/api/state")[1]
        assert [a["code"] for a in s["active"]] == ["SCADA_LINK"], [a["code"] for a in s["active"]]
    finally:
        app.stop.set()
        if app.srv:
            app.srv.shutdown()


def test_check_command(tmp_path, sim):
    p, port, ctl = sim("--speed", "60", "--wallclock")
    time.sleep(2)
    cfg_path = write_cfg(tmp_path, {"clock": None, "connector": {"url": f"opc.tcp://127.0.0.1:{port}/agent-sim/"}})
    r = subprocess.run([sys.executable, "-m", "agent", "check", "--config", cfg_path], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All tags readable" in r.stdout and "PUITS-12" in r.stdout


# --------------------------------------------------------------------------- Modbus
def test_modbus_connector(tmp_path):
    from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
    from pymodbus.server import StartTcpServer, ServerStop
    import struct
    regs = [0] * 20
    hi, lo = struct.unpack(">HH", struct.pack(">f", 22.75))
    regs[0], regs[1] = hi, lo                              # whp float32 @ input 0
    regs[10] = 2712                                        # battery 27.12 V, uint16 scale 0.01
    hr = [0] * 200
    hr[100] = struct.unpack(">H", struct.pack(">h", -5))[0]  # int16 -0.5 with scale 0.1
    dev = ModbusDeviceContext(di=ModbusSequentialDataBlock(1, [1, 0, 0, 0]), co=ModbusSequentialDataBlock(1, [0] * 10),
                              hr=ModbusSequentialDataBlock(1, hr), ir=ModbusSequentialDataBlock(1, regs))
    port = free_port()
    ctx = ModbusServerContext(devices={1: dev}, single=False)
    th = threading.Thread(target=StartTcpServer, kwargs={"context": ctx, "address": ("127.0.0.1", port)}, daemon=True)
    th.start()
    time.sleep(1)
    raw = {"site": {"name": "t"}, "connector": {"type": "modbus", "port": port, "timeout_s": 1},
           "roster": [{"name": "a", "phone": "+213600000001"}], "storage": {"path": str(tmp_path / "a.db")},
           "manifolds": [{"id": "M", "repeater": "R"}],
           "wells": [{"id": "W1", "type": "natural", "manifold": "M", "tags": {
               "whp": {"host": "127.0.0.1", "unit": 1, "kind": "input", "address": 0, "type": "float32"},
               "battery": {"host": "127.0.0.1", "unit": 1, "kind": "input", "address": 10, "scale": 0.01},
               "sdv_closed": {"host": "127.0.0.1", "unit": 1, "kind": "discrete", "address": 0},
               "fire": {"host": "127.0.0.1", "unit": 1, "kind": "coil", "address": 0, "invert": True},
               "ann": {"host": "127.0.0.1", "unit": 1, "kind": "holding", "address": 100, "type": "int16", "scale": 0.1}}}]}
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(raw))
    cfg = config.load(str(p))
    from agent.connectors.base import make_connector
    c = make_connector(cfg)
    r = c.read()
    w = r["W1"]
    assert abs(w["whp"].value - 22.75) < 1e-6
    assert abs(w["battery"].value - 27.12) < 1e-6
    assert w["sdv_closed"].value is True
    assert w["fire"].value is True            # coil 0 inverted
    assert abs(w["ann"].value + 0.5) < 1e-6
    c.close()
    ServerStop()
    time.sleep(0.5)
    c2 = make_connector(cfg)
    assert c2.read() == {} and c2.last_error, "a dead RTU must give no data and an error, not an exception"
    # write function codes are refused in the configuration
    raw["wells"][0]["tags"]["whp"]["kind"] = "write_holding"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        make_connector(config.load(str(p)))


# --------------------------------------------------------------------------- CSV replay
def test_csv_replay(tmp_path):
    from simulator.feed import Feed
    f = Feed(3)
    rows = []
    for m in range(240):
        if m == 90:
            f.field.set_well("PUITS-07", "ligne")
        if m == 150:
            f.field.set_well("PUITS-07", None)
        t, r = f.step()
        row = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))}
        for ent, sigs in r.items():
            for s, smp in sigs.items():
                fresh = smp.ts == t
                v = smp.value
                row[f"{ent}.{s}"] = "" if not fresh else (str(v).lower() if isinstance(v, bool) else f"{v:.3f}")
        rows.append(row)
    path = tmp_path / "export.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    raw = yaml.safe_load(open(os.path.join(ROOT, "config", "config.sim.yaml"), encoding="utf-8"))
    raw.pop("clock")
    raw["connector"] = {"type": "csv", "file": str(path), "well_tags": {s: "{id}." + s for s in config.WELL_SIGNALS},
                        "manifold_tags": {"p_man": "{id}.p_man", "p_gl": "{id}.p_gl"}}
    raw["storage"] = {"path": str(tmp_path / "r.db")}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    app = App(config.load(str(p)))
    app.run(web=False)
    roots = [(a.entity, a.code) for a in app.engine.alarms if a.parent is None]
    assert roots == [("PUITS-07", "LIGNE")], roots
    emls = glob.glob(str(tmp_path / "outbox" / "*.eml"))
    assert any("Rapport journalier" in open(e, encoding="utf-8", errors="ignore").read() or "Rapport_journalier" in open(e, encoding="utf-8", errors="ignore").read()
               or "=?utf-8?" in open(e, encoding="utf-8", errors="ignore").read() for e in emls)


# --------------------------------------------------------------------------- security
def test_dashboard_password_and_network_exposure(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(config.ConfigError):
        config.load(write_cfg(tmp_path, {"web": {"host": "0.0.0.0"}}))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    web = free_port()
    raw_cfg = write_cfg(tmp_path, {"web": {"port": web}, "connector": {"url": "opc.tcp://127.0.0.1:1/none/"}})
    app = start_app(raw_cfg)
    try:
        time.sleep(0.5)
        assert http("GET", f"http://127.0.0.1:{web}/api/state")[0] == 401
        tok = base64.b64encode(b"x:s3cret").decode()
        code, s = http("GET", f"http://127.0.0.1:{web}/api/state", headers={"Authorization": "Basic " + tok})
        assert code == 200 and s["connector"]["connected"] is False
    finally:
        app.stop.set()
        if app.srv:
            app.srv.shutdown()


def test_simulation_clock_link_loss(tmp_path, sim):
    """Simulation clock mode: if the simulator dies, the agent clock keeps running and
    the SCADA link alarm is raised (no frozen agent)."""
    p, port, ctl = sim("--speed", "300")
    web = free_port()
    app = start_app(write_cfg(tmp_path, {"connector": {"url": f"opc.tcp://127.0.0.1:{port}/agent-sim/", "timeout_s": 2},
                                         "web": {"port": web}}))
    base = f"http://127.0.0.1:{web}"
    try:
        assert wait(lambda: http("GET", base + "/api/state")[1].get("cycles", 0) > 20, 30)
        p.terminate()
        p.wait(5)
        got = wait(lambda: http("GET", base + "/api/state")[1].get("link_alarms"), 60, 0.5)
        assert got and got[0]["code"] == "SCADA_LINK"
        assert [a["code"] for a in http("GET", base + "/api/state")[1]["active"]] == ["SCADA_LINK"]
    finally:
        app.stop.set()
        if app.srv:
            app.srv.shutdown()
