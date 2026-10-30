"""Setup wizard: configure the agent from the browser, without editing files.

    python -m agent setup --config config/config.yaml      (or simply "run" with no configuration yet)

then open http://127.0.0.1:8080/setup on the agent PC. The wizard:

  1. site name and language
  2. SCADA connection (OPC UA address, read-only account), tested on the spot
  3. explores the OPC UA server and RECOGNISES the wells, manifolds and their
     signals from the tag names (WHP, PRESSION_TETE, PT_TETE, ANN, BATTERIE...);
     the engineer checks the proposal, gives each well its type and manifold
  4. on-call roster, e-mail addresses, SMS and e-mail settings, with test buttons
  5. first supervisor account
  6. reads every tag once (value, quality, age), shows the YAML, saves
     config/config.yaml and config/secrets.env, and starts the surveillance

Passwords go to config/secrets.env only, never to the YAML. The wizard is only
served on the local PC (127.0.0.1) until a supervisor account exists, and after
that only to supervisors.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from datetime import datetime

import yaml

from . import config as config_mod

# variable name -> signal, most specific first
SIGNAL_PATTERNS = [
    ("ann_7", r"ANN(ULAIRE|ULUS)?_?7|PR_ANN_7|CSG_?7"),
    ("ann", r"\bANN(ULAIRE|ULUS)?\b|ANN_?9|PR_ANN|ANNULAIRE|CASING|\bCSG\b|PT_?201"),
    ("wht", r"\bWHT\b|TEMP(ERATURE)?_?TETE|(^|[^P])T_TETE|TT_?101|WELLHEAD_?T"),
    ("whp", r"\bWHP\b|PRESSION_?TETE|\bP_?TETE\b|PT_?TETE|PT_?101|WELLHEAD_?P|THP"),
    ("line_p", r"LINE_?P|PRESSION_?LIGNE|\bP_?LIGNE|AVAL|PT_?102|FLP"),
    ("arrival_p", r"ARRIV"),
    ("oil_rate", r"OIL_?RATE|DEBIT_?HUILE|QO\b"),
    ("gl_inj_p", r"GL_?INJ_?P|PR_?INJECTION|INJ(ECTION)?_?P"),
    ("gl_setpoint", r"SETPOINT|CONSIGNE"),
    ("gl_valve_pos", r"VALVE_?POS|POSITION_?VANNE"),
    ("gl_mode_auto", r"MODE_?AUTO"),
    ("gl_flow", r"GL_?FLOW|DEBIT_?GL|GAS_?LIFT|DEBIT_?INJ"),
    ("water_flow", r"WATER|DEBIT_?EAU"),
    ("on_test", r"ON_?TEST|EN_?ESSAI"),
    ("battery_current", r"BATT(ERY|ERIE)?_?I\b|COURANT_?BATT"),
    ("battery", r"BATT|UBAT|VBAT"),
    ("solar_current", r"SOLAR_?I\b|COURANT_?PANNEAU"),
    ("solar_voltage", r"SOLAR_?V\b|TENSION_?PANNEAU"),
    ("cabinet_temp", r"CABINET_?T\b|TEMP_?COFFRET"),
    ("io_fault", r"IO_?FAULT|CARTES?_?E_?S|ETAT_?CARTE"),
    ("comm_ok", r"COMM_?OK|ETAT_?RTU|RTU_?OK"),
    ("sdv_closed", r"SDV"),
    ("fire", r"\bFIRE\b|\bFEU\b|DETECTION_?FEU"),
    ("remote_cmd", r"REMOTE_?CMD|TELECOMMANDE"),
    ("pit_cover_open", r"PIT_?COVER|FOSSE|DALLE"),
    ("cabinet_door_open", r"CABINET_?DOOR|PORTE_?COFFRET"),
    ("shelter_door_open", r"SHELTER"),
    ("concertina_alarm", r"CONCERTINA"),
    ("presence_5m", r"PRESENCE|NIVEAU_?5M"),
    ("intrusion", r"INTRUSION"),
]
MANIFOLD_PATTERNS = [("p_test", r"P_?TEST|TEST"), ("p_gl", r"P_?GL|RESEAU_?GL|GAS_?LIFT"),
                     ("p_man", r"P_?MAN|PRODUCTION|COLLECTEUR|HEADER")]


def classify_var(name: str, manifold=False):
    n = name.upper()
    for sig, pat in (MANIFOLD_PATTERNS if manifold else SIGNAL_PATTERNS):
        if re.search(pat, n):
            return sig
    return None


# ---------------------------------------------------------------------- OPC UA
def discover_opcua(url, user="", password="", security="", max_nodes=6000, depth=7, timeout=10):
    """All variables of the server: [(nodeid, path, name)]."""
    import asyncio

    from asyncua import Client, ua

    async def go():
        c = Client(url=url, timeout=timeout)
        if user:
            c.set_user(user)
            c.set_password(password)
        if security:
            await c.set_security_string(security)
        await c.connect()
        out = []
        try:
            async def walk(node, path, d):
                if len(out) >= max_nodes or d > depth:
                    return
                try:
                    children = await node.get_children()
                except Exception:  # noqa: BLE001
                    return
                for ch in children:
                    if len(out) >= max_nodes:
                        return
                    name = (await ch.read_browse_name()).Name
                    if d == 0 and name == "Server":
                        continue
                    cls = await ch.read_node_class()
                    if cls == ua.NodeClass.Variable:
                        out.append((ch.nodeid.to_string(), path + [name], name))
                    elif cls == ua.NodeClass.Object:
                        await walk(ch, path + [name], d + 1)
            await walk(c.nodes.objects, [], 0)
        finally:
            await c.disconnect()
        return out
    return asyncio.run(go())


def test_opcua(url, user="", password="", security="", timeout=8):
    t = time.time()
    try:
        nodes = discover_opcua(url, user, password, security, max_nodes=50, depth=2, timeout=timeout)
        return {"ok": True, "ms": round((time.time() - t) * 1000), "sample": [p for _, p, _ in nodes[:8]]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def propose(variables):
    """Group variables into wells and manifolds from the tag names.
    Returns {"wells": [{id, tags}], "manifolds": [{id, tags}], "unused": [...]}."""
    groups: dict = {}
    for nid, path, name in variables:
        parent = "/".join(path[:-1])
        groups.setdefault(parent, []).append((nid, path, name))
    wells, mans, unused = [], [], []
    for parent, vs in sorted(groups.items()):
        # flat folder: split "P01_WHP" into a prefix and a signal
        if len(vs) > 40:
            sub: dict = {}
            for nid, path, name in vs:
                m = re.match(r"^([A-Za-z]*[-_ ]?\d+[A-Za-z]?)[-_. ](.+)$", name)
                if m:
                    sub.setdefault(m.group(1), []).append((nid, path[:-1] + [m.group(1), m.group(2)], m.group(2)))
                else:
                    unused.append(nid)
            items = [("/".join(p[:-1]), v) for k, v in sub.items() for p in [v[0][1]]]
        else:
            items = [(parent, vs)]
        for par, vs2 in items:
            eid = par.split("/")[-1] if par else ""
            wt = {}
            for nid, path, name in vs2:
                s = classify_var(name)
                if s and s not in wt:
                    wt[s] = nid
            if "whp" in wt:
                wells.append({"id": eid, "tags": wt, "path": par})
                continue
            mt = {}
            for nid, path, name in vs2:
                s = classify_var(name, manifold=True)
                if s and s not in mt:
                    mt[s] = nid
            if "p_man" in mt or ("p_gl" in mt and re.search(r"MAN|COLL|HEADER", eid, re.I)):
                mans.append({"id": eid, "tags": mt, "path": par})
            else:
                unused += [nid for nid, _, _ in vs2]
    return {"wells": wells, "manifolds": mans, "unused": unused}


# ---------------------------------------------------------------------- config
def build(form: dict) -> tuple[str, dict]:
    """Form from the wizard -> (YAML text, secrets)."""
    s = form
    secrets = {}
    site = {"name": s.get("site_name") or "Site", "language": s.get("language", "fr")}
    conn = {"type": "opcua", "url": s["opcua_url"], "timeout_s": 10}
    if s.get("opcua_user"):
        conn["username_env"], conn["password_env"] = "SCADA_USER", "SCADA_PASSWORD"
        secrets["SCADA_USER"] = s["opcua_user"]
        if s.get("opcua_password"):
            secrets["SCADA_PASSWORD"] = s["opcua_password"]
    if s.get("opcua_security"):
        conn["security"] = s["opcua_security"]
    mans, wells = [], []
    for m in s.get("manifolds", []):
        d = {"id": m["id"], "repeater": m.get("repeater") or ("R-" + m["id"])}
        if m.get("satellite"):
            d["satellite"] = m["satellite"]
        d["tags"] = {k: v for k, v in (m.get("tags") or {}).items() if v}
        mans.append(d)
    for w in s.get("wells", []):
        if not w.get("enabled", True):
            continue
        d = {"id": w["id"], "type": w.get("type", "natural"), "manifold": w.get("manifold") or None}
        if w.get("repeater"):
            d["repeater"] = w["repeater"]
        for k in ("maasp", "potential_m3d"):
            if w.get(k) not in (None, ""):
                d[k] = float(w[k])
        d["tags"] = {k: v for k, v in (w.get("tags") or {}).items() if v}
        if not d["manifold"]:
            d.pop("manifold")
        wells.append(d)
    roster = [{"name": c["name"], "role": c.get("role", ""), "phone": c["phone"]} for c in s.get("roster", []) if c.get("name") and c.get("phone")]
    sms = {"type": s.get("sms_type", "console")}
    if sms["type"] == "gsm_modem":
        sms.update(port=s.get("sms_port", "COM3"), baudrate=int(s.get("sms_baud", 115200)))
        if s.get("sim_pin"):
            sms["pin_env"] = "SIM_PIN"
            secrets["SIM_PIN"] = s["sim_pin"]
    elif sms["type"] == "android_gateway":
        sms.update(url=s.get("sms_url", ""), username_env="SMS_GATEWAY_USER", password_env="SMS_GATEWAY_PASSWORD")
        secrets["SMS_GATEWAY_USER"] = s.get("sms_user", "")
        if s.get("sms_password"):
            secrets["SMS_GATEWAY_PASSWORD"] = s["sms_password"]
    email = {"type": s.get("email_type", "console"), "alarm_to": _list(s.get("alarm_to")), "report_to": _list(s.get("report_to"))}
    if email["type"] == "smtp":
        email.update(smtp_host=s.get("smtp_host", ""), smtp_port=int(s.get("smtp_port") or 587), starttls=True,
                     user_env="SMTP_USER", password_env="SMTP_PASSWORD", **{"from": s.get("smtp_from") or s.get("smtp_user", "")})
        secrets["SMTP_USER"] = s.get("smtp_user", "")
        if s.get("smtp_password"):
            secrets["SMTP_PASSWORD"] = s["smtp_password"]
    teams = {k: _list(v) for k, v in (s.get("teams") or {}).items() if _list(v)}
    cfg = {"site": site, "poll_seconds": int(s.get("poll_seconds") or 60), "connector": conn,
           "learn_minutes": 120, "baseline_hours": 12, "escalation_min": int(s.get("escalation_min") or 10),
           "daily_report_hour": int(s.get("daily_report_hour") or 7), "roster": roster, "sms": sms, "email": email,
           "analysis": {"teams": teams, "dispatch_to_teams": bool(s.get("dispatch_to_teams"))},
           "shifts": {"changes": _list(s.get("shift_changes")) or ["06:00", "18:00"]},
           "web": {"host": s.get("web_host") or "127.0.0.1", "port": int(s.get("web_port") or 8080)},
           "storage": {"path": "../data/agent.db"}, "manifolds": mans, "wells": wells}
    if s.get("satellites"):
        cfg["satellites"] = [{"id": x} for x in _list(s["satellites"])]
    head = (f"# Well surveillance agent - written by the setup wizard on {datetime.now():%d/%m/%Y %H:%M}\n"
            "# Passwords are in config/secrets.env, not here. Thresholds: see config.example.yaml.\n")
    return head + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=140), secrets


def _list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;\n]", str(v)) if x.strip()]


def validate(yaml_text: str, secrets: dict, config_path: str):
    """Load the configuration as the agent would. Returns (cfg or None, error)."""
    d = os.path.dirname(os.path.abspath(config_path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".yaml", dir=d)
    os.close(fd)
    old = {k: os.environ.get(k) for k in secrets}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        for k, v in secrets.items():
            if v:
                os.environ[k] = v
        return config_mod.load(tmp), ""
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    finally:
        os.remove(tmp)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def read_once(cfg):
    """Every tag read once: [{entity, signal, address, value, quality, age_s}]."""
    from .connectors.base import make_connector
    conn = make_connector(cfg)
    try:
        r = conn.read()
        now = conn.now() or time.time()
        out = []
        for ent, sigs in conn.points().items():
            for sig, addr in sigs.items():
                s = r.get(ent, {}).get(sig)
                out.append({"entity": ent, "signal": sig, "address": str(addr),
                            "value": None if s is None else (s.value if isinstance(s.value, (bool, int, float, str)) else str(s.value)),
                            "quality": "MISSING" if s is None else ("good" if s.good else "BAD"),
                            "age_s": None if s is None else round(now - s.ts)})
        return {"ok": bool(r), "error": conn.last_error, "tags": out}
    finally:
        conn.close()


def save(config_path: str, yaml_text: str, secrets: dict):
    """Write config.yaml (previous one kept as .bak) and merge secrets.env."""
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    if os.path.exists(config_path):
        shutil.copy2(config_path, config_path + f".{datetime.now():%Y%m%d-%H%M%S}.bak")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    sp = os.path.join(os.path.dirname(os.path.abspath(config_path)), "secrets.env")
    cur = {}
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    cur[k.strip()] = v
    cur.update({k: v for k, v in secrets.items() if v})
    with open(sp, "w", encoding="utf-8") as f:
        f.write("# Passwords of the agent. Keep this file readable only by the service account.\n")
        for k, v in cur.items():
            f.write(f"{k}={v}\n")
        f.flush()
    for k, v in secrets.items():
        if v:
            os.environ[k] = v
    try:
        os.chmod(sp, 0o600)
    except OSError:
        pass


def current_form(cfg) -> dict:
    """Wizard form pre-filled from an existing configuration (to change settings later)."""
    if cfg is None:
        return {}
    c = cfg.connector
    teams = (cfg.analysis or {}).get("teams", {})
    return {"site_name": cfg.site_name, "language": cfg.language, "poll_seconds": cfg.poll_seconds,
            "opcua_url": c.get("url", ""), "opcua_user": config_mod.secret(c.get("username_env")),
            "opcua_security": c.get("security", ""),
            "wells": [{"id": w.id, "type": w.type, "manifold": w.manifold, "repeater": w.repeater, "maasp": w.maasp,
                       "potential_m3d": w.potential_m3d, "tags": dict(w.tags), "enabled": True} for w in cfg.wells],
            "manifolds": [{"id": m.id, "repeater": m.repeater, "satellite": m.satellite, "tags": dict(m.tags)} for m in cfg.manifolds],
            "roster": [{"name": r.name, "role": r.role, "phone": r.phone} for r in cfg.roster],
            "sms_type": cfg.sms.get("type", "console"), "sms_port": cfg.sms.get("port", ""), "sms_url": cfg.sms.get("url", ""),
            "email_type": cfg.email.get("type", "console"), "alarm_to": ", ".join(cfg.email.get("alarm_to", [])),
            "report_to": ", ".join(cfg.email.get("report_to", [])), "smtp_host": cfg.email.get("smtp_host", ""),
            "smtp_port": cfg.email.get("smtp_port", 587), "smtp_from": cfg.email.get("from", ""),
            "teams": {k: ", ".join(v) for k, v in teams.items()}, "shift_changes": ", ".join(cfg.shifts.get("changes", [])),
            "escalation_min": cfg.escalation_min, "daily_report_hour": cfg.daily_report_hour,
            "web_host": cfg.web.get("host"), "web_port": cfg.web.get("port"), "satellites": ", ".join(cfg.satellites)}
