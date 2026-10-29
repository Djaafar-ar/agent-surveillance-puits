"""Configuration loading and validation.

The configuration is a YAML file (see config/config.example.yaml). Secrets
(SCADA password, SMTP password, SMS gateway password, dashboard password) are
never written in the YAML: the YAML names an environment variable, and the
value is read from the environment or from a `secrets.env` file placed next
to the configuration file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

WELL_TYPES = ("gas_lift", "natural", "natural_water")

# Signals the agent understands. Every signal is optional except whp.
WELL_SIGNALS = {
    # process
    "whp": "Wellhead pressure (Barg)",
    "wht": "Wellhead temperature (degC)",
    "line_p": "Line pressure, downstream of the choke (Barg)",
    "arrival_p": "Arrival pressure of the well's line at the manifold (Barg)",
    "ann": "Annulus 9 5/8 pressure (Barg)",
    "ann_7": "Annulus 7\" pressure (Barg)",
    "oil_rate": "Calculated oil rate (m3/d)",
    "gl_flow": "Gas lift injection rate (Sm3/d)",
    "gl_inj_p": "Gas lift injection pressure (Barg)",
    "gl_setpoint": "Gas lift rate set-point (Sm3/d)",
    "gl_valve_pos": "Gas lift valve position (%)",
    "gl_mode_auto": "Gas lift valve in automatic mode (bool)",
    "water_flow": "Desalting water rate (m3/d)",
    "on_test": "Well routed to the test line (bool)",
    # RTU and power
    "battery": "RTU battery voltage (V)",
    "battery_current": "RTU battery current (A)",
    "solar_current": "Solar panel current (A)",
    "solar_voltage": "Solar panel voltage (V)",
    "cabinet_temp": "RTU cabinet temperature (degC)",
    "io_fault": "RTU I/O card fault (bool)",
    "comm_ok": "RTU communication healthy, as seen by the SCADA (bool)",
    # safety and security
    "sdv_closed": "Safety valve closed (bool)",
    "fire": "Fire detector in alarm (bool)",
    "remote_cmd": "Remote command / operator activation in progress (bool)",
    "intrusion": "Intrusion contact, generic (bool)",
    "pit_cover_open": "Battery pit cover open (bool)",
    "cabinet_door_open": "RTU cabinet door open (bool)",
    "shelter_door_open": "Shelter door open (bool)",
    "concertina_alarm": "Concertina (perimeter wire) alarm (bool)",
    "presence_5m": "Presence detected within 5 m (bool)",
}
INTRUSION_SIGNALS = ("intrusion", "pit_cover_open", "cabinet_door_open", "shelter_door_open", "concertina_alarm", "presence_5m")
MANIFOLD_SIGNALS = {
    "p_man": "Production header pressure (Barg)",
    "p_test": "Test header pressure (Barg)",
    "p_gl": "Gas lift network pressure (Barg)",
}

DEFAULT_THRESHOLDS = {
    "pressure_drop_pct": 20,      # P_CHUTE
    "pressure_high_pct": 40,      # P_HAUTE
    "rtu_timeout_min": 5,         # RTU comm loss warning
    "rtu_urgent_min": 15,         # RTU comm loss becomes an emergency
    "annulus_warn_barg": 6.9,     # API RP 90 diagnostic threshold (100 psi)
    "annulus_urgent_pct_maasp": 70,
    "battery_low_v": 24.0,
    "battery_ok_v": 25.0,
    "gl_low_pct": 30,             # gas lift stopped below 30 % of normal
    "water_low_pct": 30,
    "water_urgent_min": 30,
    "manifold_high_pct": 35,
    "gl_network_low_pct": 40,
    "sensor_min_barg": -0.05,     # below this the pressure transmitter is considered failed
    "sensor_max_barg": 690.0,
    "restart_grace_min": 90,      # no pressure alarm while a well restarts after a shut-in
    "choke_dp_rise_pct": 60,      # choke plugging: pressure lost across the choke rises by more than this
    "line_dp_rise_pct": 60,       # flowline restriction: pressure lost along the line rises by more than this
    "gl_below_setpoint_pct": 75,  # gas lift below this share of the set-point with the valve open
    "solar_fault_min": 90,        # no solar current for this long in full daylight (10:00-15:00)
    "cabinet_temp_max": 65,
    "command_window_min": 15,     # a safety valve closing this soon after a remote command is "commanded"
    "test_grace_min": 60,         # no pressure alarm while a well is on test, and this long after
}


class ConfigError(ValueError):
    pass


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def secret(name: str | None) -> str:
    """Read a secret from the environment; empty string if not set."""
    return os.environ.get(name, "") if name else ""


@dataclass
class Contact:
    name: str
    role: str
    phone: str


@dataclass
class Manifold:
    id: str
    repeater: str
    tags: dict[str, Any]
    normal: dict[str, float] = field(default_factory=dict)
    satellite: str = ""


@dataclass
class Well:
    id: str
    type: str
    manifold: str
    repeater: str
    tags: dict[str, Any]
    maasp: float | None = None
    potential_m3d: float | None = None
    normal: dict[str, float] = field(default_factory=dict)


@dataclass
class Config:
    path: str
    site_name: str
    language: str
    poll_seconds: int
    clock: dict
    connector: dict
    thresholds: dict
    escalation_min: int
    sms_warnings: bool
    daily_report_hour: int
    learn_minutes: int
    baseline_hours: int
    roster: list[Contact]
    sms: dict
    email: dict
    web: dict
    storage: dict
    llm: dict
    manifolds: list[Manifold]
    wells: list[Well]
    oil_price_usd: float = 70.0
    satellites: dict = field(default_factory=dict)    # id -> zone
    analysis: dict = field(default_factory=dict)

    @property
    def samples_per_minute(self) -> float:
        return 60.0 / self.poll_seconds


def _expand_tags(entity_id: str, explicit: dict | None, template: dict | None, signals: dict) -> dict:
    tags: dict[str, Any] = {}
    for sig in signals:
        if template and sig in template and template[sig] is not None:
            t = template[sig]
            tags[sig] = t.format(id=entity_id) if isinstance(t, str) else _format_nested(t, entity_id)
    for sig, t in (explicit or {}).items():
        if sig not in signals:
            raise ConfigError(f"{entity_id}: unknown signal '{sig}'. Known: {', '.join(signals)}")
        if t is None:
            tags.pop(sig, None)
        else:
            tags[sig] = t
    return tags


def _format_nested(t: Any, entity_id: str) -> Any:
    if isinstance(t, dict):
        return {k: _format_nested(v, entity_id) for k, v in t.items()}
    if isinstance(t, str):
        return t.format(id=entity_id)
    return t


def _analysis(a: dict) -> dict:
    out = {"weekly_report_day": 6, "weekly_report_hour": 8, "dispatch_to_teams": False, "teams": {}}
    out.update(a)
    from .analysis import TEAMS
    for t in out["teams"]:
        if t not in TEAMS:
            raise ConfigError(f"analysis.teams: unknown team '{t}'. Known: {', '.join(TEAMS)}")
    if not 0 <= int(out["weekly_report_day"]) <= 6:
        raise ConfigError("analysis.weekly_report_day: 0 (Monday) to 6 (Sunday)")
    return out


def load(path: str) -> Config:
    path = os.path.abspath(path)
    _load_env_file(os.path.join(os.path.dirname(path), "secrets.env"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    site = raw.get("site", {})
    conn = raw.get("connector") or {}
    if conn.get("type") not in ("opcua", "modbus", "csv"):
        raise ConfigError("connector.type must be opcua, modbus or csv")

    th = dict(DEFAULT_THRESHOLDS)
    for k, v in (raw.get("thresholds") or {}).items():
        if k not in DEFAULT_THRESHOLDS:
            raise ConfigError(f"unknown threshold '{k}'. Known: {', '.join(DEFAULT_THRESHOLDS)}")
        th[k] = float(v)

    roster = [Contact(str(c["name"]), str(c.get("role", "")), str(c.get("phone", ""))) for c in raw.get("roster") or []]
    if not roster:
        raise ConfigError("roster: at least one on-call contact is required")

    wt = conn.get("well_tags") or {}
    mt = conn.get("manifold_tags") or {}
    sats = {str(s["id"]): str(s.get("zone", "")) for s in raw.get("satellites") or []}
    manifolds = []
    for m in raw.get("manifolds") or []:
        sat = str(m.get("satellite", "") or "")
        if sat and sat not in sats:
            raise ConfigError(f"{m['id']}: satellite '{sat}' is not defined in satellites")
        manifolds.append(Manifold(id=str(m["id"]), repeater=str(m.get("repeater", m["id"])),
                                  tags=_expand_tags(str(m["id"]), m.get("tags"), mt, MANIFOLD_SIGNALS),
                                  normal={k: float(v) for k, v in (m.get("normal") or {}).items()}, satellite=sat))
    man_by_id = {m.id: m for m in manifolds}

    wells = []
    for w in raw.get("wells") or []:
        wid = str(w["id"])
        wtype = w.get("type", "natural")
        if wtype not in WELL_TYPES:
            raise ConfigError(f"{wid}: type must be one of {', '.join(WELL_TYPES)}")
        man = str(w.get("manifold", ""))
        if man and man not in man_by_id:
            raise ConfigError(f"{wid}: manifold '{man}' is not defined")
        tags = _expand_tags(wid, w.get("tags"), wt, WELL_SIGNALS)
        if wtype != "gas_lift":
            for k_ in ("gl_flow", "gl_inj_p", "gl_setpoint", "gl_valve_pos", "gl_mode_auto"):
                tags.pop(k_, None)
        if wtype != "natural_water":
            tags.pop("water_flow", None)
        if "whp" not in tags:
            raise ConfigError(f"{wid}: the wellhead pressure tag (whp) is required")
        rep = str(w.get("repeater") or (man_by_id[man].repeater if man else wid))
        wells.append(Well(id=wid, type=wtype, manifold=man, repeater=rep, tags=tags,
                          maasp=float(w["maasp"]) if w.get("maasp") is not None else None,
                          potential_m3d=float(w["potential_m3d"]) if w.get("potential_m3d") is not None else None,
                          normal={k: float(v) for k, v in (w.get("normal") or {}).items()}))
    if not wells:
        raise ConfigError("wells: at least one well is required")
    ids = [w.id for w in wells] + [m.id for m in manifolds]
    if len(ids) != len(set(ids)):
        raise ConfigError("well and manifold ids must be unique")

    web = {"host": "127.0.0.1", "port": 8080, "password_env": "DASHBOARD_PASSWORD"}
    web.update(raw.get("web") or {})
    if web["host"] not in ("127.0.0.1", "localhost", "::1") and not secret(web.get("password_env")):
        raise ConfigError("web.host is reachable from the network: set a dashboard password "
                          f"in the environment variable {web.get('password_env')}")

    poll = int(raw.get("poll_seconds", 60))
    if not 5 <= poll <= 300:
        raise ConfigError("poll_seconds must be between 5 and 300")
    lang = site.get("language", "fr")
    if lang not in ("fr", "en"):
        raise ConfigError("site.language must be fr or en")

    storage = {"path": "data/agent.db"}
    storage.update(raw.get("storage") or {})
    if not os.path.isabs(storage["path"]):
        storage["path"] = os.path.join(os.path.dirname(path), storage["path"])

    return Config(
        path=path, site_name=str(site.get("name", "Site")), language=lang, poll_seconds=poll,
        clock=raw.get("clock") or {"type": "system"}, connector=conn, thresholds=th,
        escalation_min=int(raw.get("escalation_min", 10)), sms_warnings=bool(raw.get("sms_warnings", False)),
        daily_report_hour=int(raw.get("daily_report_hour", 7)),
        learn_minutes=int(raw.get("learn_minutes", 120)), baseline_hours=int(raw.get("baseline_hours", 12)),
        roster=roster, sms=raw.get("sms") or {"type": "console"}, email=raw.get("email") or {"type": "console"},
        web=web, storage=storage, llm=raw.get("llm") or {"enabled": False},
        manifolds=manifolds, wells=wells, oil_price_usd=float(raw.get("oil_price_usd", 70)), satellites=sats,
        analysis=_analysis(raw.get("analysis") or {}))
