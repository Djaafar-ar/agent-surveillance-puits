"""Rule engine tests on the simulated field (no network).

Each scenario runs over several random seeds. The field model is the same
one the OPC UA simulator uses.
"""

import ast
import glob
import os

import pytest

from agent import config
from agent.engine import Engine
from simulator.feed import Feed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS = [1, 2, 3, 4, 5]
WARMUP = 60          # minutes: the agent learns normal values (learn_minutes: 30)


class Out:
    def __init__(self):
        self.sms_log, self.j, self.mails = [], [], []

    def journal(self, t, l, x): self.j.append((t, l, x))
    def sms(self, i, text, a, k): self.sms_log.append({"idx": i, "alarm": a.id, "code": a.code, "kind": k, "text": text, "t": a.last_sms if k != "clear" else None})
    def alarm_email(self, a, upgraded): self.mails.append(("alarm", a.code))
    def clear_email(self, a): self.mails.append(("clear", a.code))


@pytest.fixture(scope="module")
def cfg():
    c = config.load(os.path.join(ROOT, "config", "config.sim.yaml"))
    return c


def run(cfg, seed, actions, minutes, overrides=None, start=170):
    """actions: list of (minute, 'well'|'group'|'sat', id, incident or None)"""
    if overrides:
        cfg.thresholds.update(overrides)
    f, o = Feed(seed, start), Out()
    e = Engine(cfg, o)
    for m in range(minutes):
        for at, kind, ent, inc in actions:
            if at == m:
                {"well": f.field.set_well, "group": f.field.set_group, "sat": f.field.set_satellite}[kind](ent, inc)
        t, r = f.step()
        e.step(t, r, True)
    if overrides:
        cfg.thresholds.update(config.DEFAULT_THRESHOLDS)
    return e, o


def roots(e):
    return [a for a in e.alarms if a.parent is None]


@pytest.mark.parametrize("seed", SEEDS)
def test_no_false_alarm_24h(cfg, seed):
    e, o = run(cfg, seed, [], 1440)
    assert e.alarms == [], [(a.entity, a.code) for a in e.alarms]
    assert o.sms_log == []


def test_no_false_alarm_most_sensitive(cfg):
    e, _ = run(cfg, 7, [], 1440, {"pressure_drop_pct": 15, "pressure_high_pct": 30, "rtu_timeout_min": 3})
    assert e.alarms == [], [(a.entity, a.code) for a in e.alarms]


CASES = [
    ("chute", "PUITS-03", "P_CHUTE", "urgence", 20, True),
    ("chute", "PUITS-01", "P_CHUTE", "urgence", 20, True),
    ("ligne", "PUITS-07", "LIGNE", "urgence", 12, True),
    ("duse", "PUITS-07", "DUSE", "urgence", 15, True),
    ("orifice", "PUITS-10", "GL_ECART", "avert", 20, False),
    ("commande", "PUITS-03", "SDV", "avert", 4, False),
    ("porte", "PUITS-08", "INTRUS", "urgence", 2, True),
    ("gl", "PUITS-02", "GL_ARRET", "urgence", 6, True),
    ("eau", "PUITS-04", "EAU_ARRET", "urgence", 6, True),
    ("ann", "PUITS-11", "ANN", "urgence", 8, True),
    ("instab", "PUITS-09", "INSTAB", "avert", 20, False),
    ("capteur", "PUITS-10", "CAPTEUR", "avert", 3, False),
    ("batterie", "PUITS-12", "BATT", "avert", 30, False),
    ("rtu", "PUITS-06", "RTU", "urgence", 8, True),
    ("sdv", "PUITS-05", "SDV", "urgence", 2, True),
    ("feu", "PUITS-07", "FEU", "urgence", 2, True),
    ("intrusion", "PUITS-02", "INTRUS", "urgence", 2, True),
    ("carte", "PUITS-08", "CARTE", "avert", 2, False),
]


@pytest.mark.parametrize("seed", SEEDS[:3])
@pytest.mark.parametrize("inc,well,code,sev,delay,sms", CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
def test_incident(cfg, seed, inc, well, code, sev, delay, sms):
    e, o = run(cfg, seed, [(WARMUP, "well", well, inc), (WARMUP + 60, "well", well, None)], WARMUP + 60 + 150)
    main = [a for a in e.alarms if a.code == code and a.entity == well]
    assert len(main) == 1, f"{code} raised {len(main)} times"
    a = main[0]
    t_start = e.started + (WARMUP + 1) * 60
    assert (a.t0 - t_start) / 60 <= delay, f"detected after {(a.t0 - t_start) / 60:.0f} min"
    assert a.parent is None and a.severity == sev
    assert bool([s for s in o.sms_log if s["alarm"] == a.id and s["kind"] == "alarm"]) == sms
    others = [x for x in e.alarms if x.entity != well]
    assert not others, [(x.entity, x.code) for x in others]
    still = [x for x in e.alarms if x.t_end is None]
    assert not still, f"still active after the incident was removed: {[(x.entity, x.code) for x in still]}"
    if inc != "batterie":   # flat battery ends in a radio loss: second root alarm by design
        assert len(roots(e)) == 1, [(x.entity, x.code) for x in roots(e)]


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_repeater_failure_one_alarm_one_sms(cfg, seed):
    e, o = run(cfg, seed, [(WARMUP, "group", "MAN-B", "rep"), (WARMUP + 30, "group", "MAN-B", None)], WARMUP + 60)
    r = roots(e)
    assert [(a.entity, a.code) for a in r] == [("Répéteur R2", "REP")]
    assert sum(1 for a in e.alarms if a.code == "RTU" and a.parent is r[0]) == 4
    assert len([s for s in o.sms_log if s["kind"] == "alarm"]) == 1


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_gas_lift_network(cfg, seed):
    e, _ = run(cfg, seed, [(WARMUP, "group", "MAN-A", "glnet")], WARMUP + 60)
    assert [(a.entity, a.code) for a in roots(e)] == [("Manifold MAN-A", "GLNET")]
    assert {a.entity for a in e.alarms if a.parent} >= {"PUITS-01", "PUITS-02"}


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_flowline_restriction(cfg, seed):
    e, _ = run(cfg, seed, [(WARMUP, "group", "MAN-C", "collecte")], WARMUP + 45)
    assert [(a.entity, a.code) for a in roots(e)] == [("Manifold MAN-C", "MAN_HAUTE")]


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_safety_valve_restart_no_nuisance(cfg, seed):
    e, _ = run(cfg, seed, [(WARMUP, "well", "PUITS-05", "sdv"), (WARMUP + 60, "well", "PUITS-05", None)], WARMUP + 60 + 150)
    assert [a.code for a in roots(e)] == ["SDV"], [(a.entity, a.code) for a in roots(e)]


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_io_card_groups_its_transmitters(cfg, seed):
    """Real case seen on a SCADA screen: I/O card in fault and 3 pressures reading negative = 1 alarm."""
    e, o = run(cfg, seed, [(WARMUP, "well", "PUITS-09", "carte"), (WARMUP + 30, "well", "PUITS-09", None)], WARMUP + 80)
    r = roots(e)
    assert [(a.entity, a.code) for a in r] == [("PUITS-09", "CARTE")], [(a.entity, a.code) for a in r]
    kids = sorted(a.code for a in e.alarms if a.parent is r[0])
    assert kids == ["CAPTEUR_ANN", "CAPTEUR_ANN_7", "CAPTEUR_GL_INJ_P"], kids
    assert o.sms_log == []


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_satellite_station_one_alarm(cfg, seed):
    e, o = run(cfg, seed, [(WARMUP, "sat", "SAT-1", "satellite"), (WARMUP + 40, "sat", "SAT-1", None)], WARMUP + 120)
    r = roots(e)
    assert [(a.entity, a.code) for a in r] == [("Satellite SAT-1", "SAT_HAUTE")], [(a.entity, a.code) for a in r]
    assert {a.entity for a in e.alarms if a.code == "MAN_HAUTE"} == {"Manifold MAN-A", "Manifold MAN-B"}
    assert len([s for s in o.sms_log if s["kind"] == "alarm"]) == 1


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_well_on_test_no_alarm(cfg, seed):
    e, _ = run(cfg, seed, [(WARMUP, "well", "PUITS-11", "test"), (WARMUP + 90, "well", "PUITS-11", None)], WARMUP + 200)
    assert e.alarms == [], [(a.entity, a.code) for a in e.alarms]


def test_flowline_restriction_vs_manifold(cfg):
    """Same symptom on the wellhead, different cause: the line pressures tell them apart."""
    e, _ = run(cfg, 1, [(WARMUP, "well", "PUITS-07", "ligne")], WARMUP + 40)
    assert [a.code for a in roots(e)] == ["LIGNE"]
    e, _ = run(cfg, 1, [(WARMUP, "group", "MAN-B", "collecte")], WARMUP + 40)
    assert [a.code for a in roots(e)] == ["MAN_HAUTE"], [(a.entity, a.code) for a in roots(e)]
    assert not [a for a in e.alarms if a.code in ("LIGNE", "DUSE")]


def test_solar_fault_warns_long_before_radio_loss(cfg):
    e, _ = run(cfg, 3, [(30, "well", "PUITS-03", "solaire")], 900, start=8 * 60)
    sol = [a for a in e.alarms if a.code == "SOLAIRE"]
    rtu = [a for a in e.alarms if a.code == "RTU"]
    assert [a.entity for a in sol] == ["PUITS-03"], [(a.entity, a.code) for a in e.alarms]
    assert rtu and (rtu[0].t0 - sol[0].t0) / 3600 > 4, "the solar warning should come hours before the RTU stops"
    assert next(a for a in e.alarms if a.code == "BATT").parent is sol[0]


def test_no_false_alarm_daytime(cfg):
    e, _ = run(cfg, 9, [], 1440, start=6 * 60)
    assert e.alarms == [], [(a.entity, a.code) for a in e.alarms]


def test_fire_pages_everyone(cfg):
    e, o = run(cfg, 1, [(WARMUP, "well", "PUITS-07", "feu")], WARMUP + 3)
    assert sorted(s["idx"] for s in o.sms_log if s["code"] == "FEU") == [0, 1, 2]


def test_escalation_and_ack(cfg):
    e, o = run(cfg, 1, [(WARMUP, "well", "PUITS-03", "sdv")], WARMUP + 35)
    a = next(x for x in e.alarms if x.code == "SDV")
    seq = [(s["idx"], s["kind"]) for s in o.sms_log if s["alarm"] == a.id]
    assert seq == [(0, "alarm"), (1, "escal"), (2, "escal")]
    delays = [round((s["t"] - a.t0) / 60) for s in o.sms_log if s["alarm"] == a.id]
    assert delays == [0, 10, 20], delays
    assert a.exhausted
    # acknowledgement stops escalation
    e2, o2 = run(cfg, 1, [(WARMUP, "well", "PUITS-03", "sdv")], WARMUP + 3)
    a2 = next(x for x in e2.alarms if x.code == "SDV")
    assert e2.ack(a2.id, 0, "test")
    assert not e2.ack(a2.id, 0, "test"), "second ack must be refused"


def test_sms_reply_ok_acknowledges(cfg):
    e, o = run(cfg, 1, [(WARMUP, "well", "PUITS-03", "sdv")], WARMUP + 3)
    assert e.ack_from_phone("+213 600 000 002", "OK") == [], "contact 2 was not paged yet"
    assert e.ack_from_phone("00213600000001", "ok") != []
    a = next(x for x in e.alarms if x.code == "SDV")
    assert a.ack[0] == 0 and a.ack[2] == "SMS"
    assert e.ack_from_phone("+213600000001", "bonjour") == []


def test_scada_link_loss_one_alarm(cfg):
    f, o = Feed(1), Out()
    e = Engine(cfg, o)
    for m in range(WARMUP + 30):
        t, r = f.step()
        down = WARMUP <= m < WARMUP + 15
        e.step(t, {} if down else r, not down, "timeout" if down else "")
    r = roots(e)
    assert [a.code for a in r] == ["SCADA_LINK"], [(a.entity, a.code) for a in r]
    assert all(a.parent is r[0] for a in e.alarms if a.code == "RTU")
    assert not [a for a in e.alarms if a.t_end is None]


def test_learning_before_alarming(cfg):
    """Without configured normal values, no pressure alarm during the learning period."""
    e, _ = run(cfg, 1, [(5, "well", "PUITS-03", "chute")], 25)
    assert not [a for a in e.alarms if a.code == "P_CHUTE"]


def test_texts_have_no_missing_placeholders(cfg):
    for lang in ("fr", "en"):
        cfg.language = lang
        e, o = run(cfg, 2, [(WARMUP, "group", "MAN-B", "rep"), (WARMUP, "well", "PUITS-11", "ann"), (WARMUP, "well", "PUITS-12", "batterie"),
                            (WARMUP, "group", "MAN-A", "glnet"), (WARMUP, "well", "PUITS-04", "eau"), (WARMUP, "well", "PUITS-09", "carte"),
                            (WARMUP, "well", "PUITS-03", "duse"), (WARMUP, "well", "PUITS-10", "orifice"), (WARMUP, "sat", "SAT-2", None),
                            (WARMUP + 5, "group", "MAN-C", "collecte"), (WARMUP, "well", "PUITS-01", "commande")], WARMUP + 90)
        for a in e.alarms:
            for txt in (a.title, a.value, a.diag, a.action):
                assert "{" not in txt and "None" not in txt and "nan" not in txt.lower(), (lang, a.code, txt)
        for s in o.sms_log:
            assert "{" not in s["text"]
    cfg.language = "fr"


def test_connectors_are_read_only():
    """No connector source contains an OPC UA write or a Modbus write function code."""
    forbidden = ("write_value", "write_attribute", "write_register", "write_coil", "write_registers", "write_coils",
                 "set_value", "call_method")
    for path in glob.glob(os.path.join(ROOT, "agent", "**", "*.py"), recursive=True):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                pytest.fail(f"{os.path.relpath(path, ROOT)}:{node.lineno} uses {node.attr}")
