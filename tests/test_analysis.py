"""Long-term analysis tests: a simulated year with known problems planted in it.
The analyst must find each of them, predict the right deadlines, name the right
team, and stay quiet about the healthy wells."""

import os
from datetime import timedelta

import pytest

from agent import config
from agent.analysis import Analyst
from agent.reports import weekly_report
from agent.store import Store
from simulator.history import generate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED = {   # entity -> (kind, team)
    "PUITS-03": ("fast_decline", "production"),
    "PUITS-04": ("salt_deposits", "production"),
    "PUITS-08": ("sensor_drift", "instrumentation"),
    "PUITS-12": ("step_drop", "production"),
    "PUITS-01": ("choke_trend", "production"),
    "PUITS-11": ("annulus_buildup", "hse"),
    "PUITS-06": ("battery_ageing", "maintenance"),
    "PUITS-10": ("solar_degraded", "maintenance"),
    "PUITS-05": ("rtu_unreliable", "telecom"),
    "Répéteur R2": ("repeater_unreliable", "telecom"),
    "PUITS-09": ("bad_actor", "production"),
}


@pytest.fixture(scope="module")
def cfg():
    return config.load(os.path.join(ROOT, "config", "config.sim.yaml"))


@pytest.fixture(scope="module", params=[3, 7, 11])
def year(cfg, tmp_path_factory, request):
    st = Store(str(tmp_path_factory.mktemp("h") / "a.db"))
    start, end = generate(st, 365, seed=request.param)
    return st, end, Analyst(cfg, st).run(end)


def test_every_planted_problem_is_found(year):
    _, _, found = year
    got = {(f.entity, f.kind, f.team) for f in found}
    missing = [(e, k, t) for e, (k, t) in EXPECTED.items() if (e, k, t) not in got]
    assert not missing, f"missed: {missing}; found: {sorted(got)}"


def test_no_false_findings(year):
    _, _, found = year
    extra = [(f.entity, f.kind) for f in found if EXPECTED.get(f.entity, (None,))[0] != f.kind]
    assert not extra, f"unexpected findings: {extra}"


def test_predictions(year):
    _, end, found = year
    by = {(f.entity, f.kind): f for f in found}
    ann = by[("PUITS-11", "annulus_buildup")]
    assert 10 <= ann.due_days <= 40 and ann.severity == 3, ann.due_days      # 6.9 Barg reached in ~3 weeks
    bat = by[("PUITS-06", "battery_ageing")]
    assert 10 <= bat.due_days <= 45, bat.due_days
    dec = by[("PUITS-03", "fast_decline")]
    assert 90 <= dec.due_days <= 300, dec.due_days                            # stops flowing in a few months
    assert "propre au puits" in dec.evidence                                  # other wells of MAN-A decline slowly
    # urgent items come first
    sev = [f.severity for f in found]
    assert sev == sorted(sev, reverse=True)


def test_sensor_drift_not_reported_as_decline(year):
    _, _, found = year
    assert not [f for f in found if f.entity == "PUITS-08" and "decline" in f.kind]


def test_short_history_says_nothing(cfg, tmp_path):
    st = Store(str(tmp_path / "s.db"))
    _, end = generate(st, 15)
    found = Analyst(cfg, st).run(end)
    assert not [f for f in found if f.kind in ("fast_decline", "decline", "sensor_drift", "salt_deposits", "step_drop")]


def test_weekly_report_and_team_lists(cfg, tmp_path):
    st = Store(str(tmp_path / "w.db"))
    generate(st, 365)
    subj, body, per_team, found = weekly_report(None, st, cfg)
    assert "urgent" in subj
    for e in ("PUITS-11", "PUITS-03", "PUITS-06"):
        assert e in body
    assert "Action proposée" in body and "Production différée" in body
    assert set(per_team) == {"production", "instrumentation", "telecom", "maintenance", "hse"}
    assert "PUITS-11" in per_team["hse"][1] and "PUITS-11" not in per_team["telecom"][1]
    assert "{" not in body


def test_weekly_report_english(cfg, tmp_path):
    cfg.language = "en"
    try:
        st = Store(str(tmp_path / "e.db"))
        generate(st, 365)
        subj, body, per_team, found = weekly_report(None, st, cfg)
        assert subj.startswith("Weekly analysis report") and "Proposed action" in body and "{" not in body
    finally:
        cfg.language = "fr"


def test_live_daily_history_is_recorded(cfg, tmp_path):
    """The running agent writes min/mean/max per day, which is what the analysis reads."""
    from agent.app import App
    from simulator.feed import Feed
    import yaml
    raw = yaml.safe_load(open(os.path.join(ROOT, "config", "config.sim.yaml"), encoding="utf-8"))
    raw["storage"] = {"path": str(tmp_path / "live.db")}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    app = App(config.load(str(p)))
    f = Feed(1, 23 * 60)                     # starts at 23:00 so the day changes during the run
    f.field.set_well("PUITS-05", None)
    for _ in range(120):
        t, r = f.step()
        app.process(t, r)
    rows = app.store.series("PUITS-05", "whp", 5)
    assert len(rows) == 1, rows               # the first (complete) day was flushed at midnight
    assert 15 < rows[0][1] < 25
    assert app.store.series("PUITS-05", "online", 5)[0][1] == 1.0
    assert app.store.series("MAN-B", "p_man", 5)


def test_partial_day_after_restart_does_not_change_findings(cfg, tmp_path):
    """The agent stopped after one hour of a new day: that hour is saved, merged on
    restart, and too short to count as a day in the trends."""
    st = Store(str(tmp_path / "p.db"))
    _, end = generate(st, 365)
    before = {(f.entity, f.kind, f.severity) for f in Analyst(cfg, st).run()}
    day = (end + timedelta(days=1)).isoformat()
    rows = [(day, w.id, s, v, v, v, 60, v * 60) for w in cfg.wells
            for s, v in (("whp", 20.0), ("ann", 1.0), ("battery", 27.0), ("online", 1.0), ("shut", 0.0), ("test", 0.0))]
    st.put_daily(rows, merge=True)
    st.put_daily(rows, merge=True)            # second short run the same day: merged, n = 120
    assert st.series("PUITS-01", "whp", 2, "n")[-1][1] == 120
    after = {(f.entity, f.kind, f.severity) for f in Analyst(cfg, st).run()}
    assert after == before
