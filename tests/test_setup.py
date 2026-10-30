"""Installation without editing files: the setup wizard (OPC UA discovery, tag
recognition, configuration, accounts, start) and data upload from the dashboard."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
import yaml

from agent import config, wizard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, url, body=None, auth=None):
    h = {"Content-Type": "application/json", "X-Requested-With": "agent"}
    if auth:
        import base64
        h["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, ConnectionError):
        return 0, {}


def wait(pred, timeout, step=0.3):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


@pytest.fixture(scope="module")
def scada():
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "simulator.sim_server", "--port", str(port), "--control-port", str(free_port())],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"opc.tcp://127.0.0.1:{port}/agent-sim/"
    assert wait(lambda: wizard.test_opcua(url, timeout=2)["ok"], 30), "simulator did not start"
    yield url
    p.terminate()
    p.wait()


def test_discovery_recognises_every_tag(scada):
    from simulator.sim_server import SIG_NODE
    v = wizard.discover_opcua(scada)
    p = wizard.propose(v)
    assert [w["id"] for w in p["wells"]] == [f"PUITS-{i:02d}" for i in range(1, 13)]
    assert [m["id"] for m in p["manifolds"]] == ["MAN-A", "MAN-B", "MAN-C"]
    wrong = [(e["id"], s, n) for e in p["wells"] + p["manifolds"] for s, n in e["tags"].items() if n != f"ns=2;s={e['id']}.{SIG_NODE[s]}"]
    assert not wrong
    assert len([1 for e in p["wells"] + p["manifolds"] for _ in e["tags"]]) == 330


def test_flat_scada_naming():
    flat = [(f"ns=2;s=P{i:02d}_{n}", ["Champ", f"P{i:02d}_{n}"], f"P{i:02d}_{n}") for i in range(1, 12)
            for n in ("PRESSION_TETE", "TEMP_TETE", "PRESSION_LIGNE", "PR_ANN_9_5_8", "PR_ANN_7", "TENSION_BATTERIE", "SDV_FERMEE", "ETAT_RTU")]
    flat += [("ns=2;s=MA_PR_COLLECTEUR_PRODUCTION", ["Champ", "MA"], "PR_COLLECTEUR_PRODUCTION")]
    p = wizard.propose(flat)
    assert len(p["wells"]) == 11
    t = p["wells"][0]["tags"]
    assert t == {"whp": "ns=2;s=P01_PRESSION_TETE", "wht": "ns=2;s=P01_TEMP_TETE", "line_p": "ns=2;s=P01_PRESSION_LIGNE",
                 "ann": "ns=2;s=P01_PR_ANN_9_5_8", "ann_7": "ns=2;s=P01_PR_ANN_7", "battery": "ns=2;s=P01_TENSION_BATTERIE",
                 "sdv_closed": "ns=2;s=P01_SDV_FERMEE", "comm_ok": "ns=2;s=P01_ETAT_RTU"}


def _form(url, port):
    p = wizard.propose(wizard.discover_opcua(url))
    return {"site_name": "Champ test", "language": "fr", "poll_seconds": 60, "opcua_url": url,
            "opcua_user": "lecture", "opcua_password": "secret-scada-123",
            "manifolds": [{"id": m["id"], "repeater": f"R{i + 1}", "tags": m["tags"]} for i, m in enumerate(p["manifolds"])],
            "wells": [{"id": w["id"], "type": "gas_lift" if "gl_flow" in w["tags"] else "natural", "manifold": "MAN-" + "ABC"[i // 4],
                       "tags": w["tags"], "enabled": True} for i, w in enumerate(p["wells"])],
            "roster": [{"name": "K. Benali", "role": "Technicien", "phone": "+213600000001"}],
            "alarm_to": "salle@exemple.dz", "report_to": "chef@exemple.dz", "teams": {"telecom": "radio@exemple.dz"},
            "smtp_password": "", "web_host": "127.0.0.1", "web_port": port}


def test_build_validate_save(scada, tmp_path):
    form = _form(scada, 8099)
    text, secrets = wizard.build(form)
    assert "secret-scada-123" not in text and secrets["SCADA_PASSWORD"] == "secret-scada-123"
    cfg, err = wizard.validate(text, secrets, str(tmp_path / "config.yaml"))
    assert not err and len(cfg.wells) == 12 and cfg.wells[4].repeater == "R2"
    wizard.save(str(tmp_path / "config.yaml"), text, secrets)
    env = open(tmp_path / "secrets.env", encoding="utf-8").read()
    assert "SCADA_PASSWORD=secret-scada-123" in env
    assert "secret-scada-123" not in open(tmp_path / "config.yaml", encoding="utf-8").read()
    cfg2 = config.load(str(tmp_path / "config.yaml"))
    assert cfg2.analysis["teams"]["telecom"] == ["radio@exemple.dz"]
    form2 = wizard.current_form(cfg2)
    assert form2["opcua_url"] == scada and len(form2["wells"]) == 12
    form["wells"][0]["tags"]["whp"] = ""
    _, err = wizard.validate(wizard.build(form)[0], secrets, str(tmp_path / "config.yaml"))
    assert "whp" in err                                    # a well without wellhead pressure is refused


def test_first_start_runs_the_wizard_then_the_surveillance(scada, tmp_path):
    port = free_port()
    cpath = tmp_path / "conf" / "config.yaml"
    p = subprocess.Popen([sys.executable, "-m", "agent", "run", "--config", str(cpath), "--port", str(port)], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        assert wait(lambda: http("GET", base + "/api/setup/state")[0] == 200, 30), "wizard not served"
        assert http("GET", base + "/api/setup/state")[1]["configured"] is False
        code, r = http("POST", base + "/api/setup/test-opcua", {"form": {"opcua_url": scada}})
        assert code == 200 and r["ok"]
        code, r = http("POST", base + "/api/setup/discover", {"form": {"opcua_url": scada}})
        assert len(r["wells"]) == 12
        form = _form(scada, port)
        form.pop("opcua_user"), form.pop("opcua_password")
        code, r = http("POST", base + "/api/setup/read-tags", {"form": form})
        assert code == 200 and r["ok"] and all(t["quality"] == "good" for t in r["tags"]), r.get("error")
        code, r = http("POST", base + "/api/setup/test-sms", {"form": form, "to": "+213600000001"})
        assert r["ok"] and r["outbox"]
        code, r = http("POST", base + "/api/setup/save", {"form": form, "admin": {"name": "A. Mansouri", "password": "superviseur-2026"}})
        assert code == 200, r
        # the surveillance starts on the same port, with the account just created
        assert wait(lambda: http("GET", base + "/api/me", auth=("A. Mansouri", "superviseur-2026"))[0] == 200, 60)
        assert http("GET", base + "/api/state")[0] == 401
        st = wait(lambda: (lambda s: s if s[1].get("cycles", 0) >= 1 else None)(http("GET", base + "/api/state", auth=("A. Mansouri", "superviseur-2026"))), 60)
        assert st and len(st[1]["wells"]) == 12
        # settings page for supervisors
        code, s = http("GET", base + "/api/setup/state", auth=("A. Mansouri", "superviseur-2026"))
        assert code == 200 and s["configured"] and s["has_users"]
    finally:
        p.terminate()
        p.wait(10)


def test_upload_import_history_and_backtest_from_the_dashboard(tmp_path):
    from agent.app import App
    from agent.web import serve
    from simulator.site_export import generate
    generate(str(tmp_path / "site"), 120)
    raw = yaml.safe_load(open(os.path.join(ROOT, "config", "config.sim.yaml"), encoding="utf-8"))
    raw.pop("clock", None)
    port = free_port()
    raw["web"] = {"host": "127.0.0.1", "port": port}
    raw["storage"] = {"path": str(tmp_path / "data" / "agent.db")}
    cp = tmp_path / "config.yaml"
    cp.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    app = App(config.load(str(cp)))
    srv = serve(app)
    base = f"http://127.0.0.1:{port}"
    try:
        ids = {}
        for k, f in (("data", "historien_export.csv"), ("tags", "liste_tags.csv"), ("events", "journal_exploitation.csv")):
            body = open(tmp_path / "site" / f, "rb").read()
            req = urllib.request.Request(f"{base}/api/data/upload?name={f}", data=body, method="POST",
                                         headers={"X-Requested-With": "agent", "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req) as r:
                ids[k] = json.loads(r.read())["id"]

        def done(job):
            return wait(lambda: (lambda j: j if j["status"] in ("done", "error") else None)(http("POST", base + "/api/data/job", {"job": job})[1]), 300, 1)
        j = done(http("POST", base + "/api/data/guess", {"data": ids["data"], "tags": ids["tags"]})[1]["id"])
        assert j["status"] == "done" and len(j["result"]["rows"]) == 63
        rows = j["result"]["rows"]
        j = done(http("POST", base + "/api/data/import", {"data": ids["data"], "events": ids["events"], "rows": rows})[1]["id"])
        assert j["status"] == "done", j["error"]
        imp = j["id"]
        code, q = 0, None
        with urllib.request.urlopen(base + j["result"]["quality"]) as r:
            assert b"Qualit" in r.read()
        h = done(http("POST", base + "/api/data/history", {"job": imp})[1]["id"])
        assert h["status"] == "done" and h["result"]["days"] >= 119
        assert app.store.series("PUITS-03", "whp", 400) and len(app.store.series("PUITS-03", "whp", 400)) >= 119
        b = done(http("POST", base + "/api/data/backtest", {"job": imp})[1]["id"])
        assert b["status"] == "done", b["error"]
        with urllib.request.urlopen(base + b["result"]["report"]) as r:
            assert b"Rapport de backtest" in r.read()
        assert http("GET", base + "/reports/../config.yaml")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
