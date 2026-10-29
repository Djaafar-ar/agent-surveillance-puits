"""Import of a site export as it really comes out of a historian and a control room:
long format, French formats, Windows encoding, irregular archive, bad quality,
spikes, a stuck transmitter, psi, and a hand-typed event log. Then the backtest
on the imported data must give the same answers as on the clean data."""

import csv
import os

import pytest
import yaml

from agent import config, importer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    d = tmp_path_factory.mktemp("cfg")
    raw = yaml.safe_load(open(os.path.join(ROOT, "config", "config.sim.yaml"), encoding="utf-8"))
    raw.pop("clock", None)
    raw["storage"] = {"path": str(d / "agent.db")}
    p = d / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return config.load(str(p))


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    from simulator.site_export import generate
    d = tmp_path_factory.mktemp("site")
    generate(str(d), 365)
    return d


@pytest.fixture(scope="module")
def imported(cfg, site, tmp_path_factory):
    out = tmp_path_factory.mktemp("imp")
    g = importer.run(cfg, str(site / "historien_export.csv"), str(out), taglist_path=str(site / "liste_tags.csv"), guess_only=True)
    res = importer.run(cfg, str(site / "historien_export.csv"), str(out), g["mapping"], str(site / "journal_exploitation.csv"))
    return out, g, res


def test_parsing_helpers(cfg):
    assert importer.parse_value("12,5") == 12.5
    assert importer.parse_value("1.234,5") == 1234.5
    assert importer.parse_value("1,234.5") == 1234.5
    assert importer.parse_value("Comm Failure") is None
    assert importer.parse_time("26/02/2026 03:00:32").hour == 3
    assert importer.parse_time("2026-02-26T03:00:32Z").minute == 0
    assert importer.entity_of("Puits 07", cfg) == "PUITS-07"
    assert importer.entity_of("P11", cfg) == "PUITS-11"
    assert importer.entity_of("Répéteur R2", cfg) == "R2"
    assert importer.entity_of("MB-PT-001", cfg) == "MAN-B"
    assert importer.entity_of("Manifold C", cfg) == "MAN-C"
    assert importer.classify("Réouverture après réparation") == "reopen"
    assert importer.classify("Fermeture d'urgence : fuite") == "shut_in"
    assert importer.classify("Fuite sur la tête de puits") == "failure"
    assert importer.classify("Purge annulaire 9 5/8 et test de remontée") == "intervention"
    assert importer.classify("Mise en essai") == "test"
    assert importer.classify("Fin d'essai") == "test_end"
    assert importer.classify("Relevé compteur") == "routine"
    assert importer.classify("xyz") is None


def test_mapping_guess_from_tag_list(imported):
    out, g, res = imported
    assert g["mapped"] == 63 and not g["unknown"]
    m = importer.load_mapping(g["mapping"])
    assert m["PUITS-04"]["ann"] == {"tag": "P04-PT-201", "unit": "psi"}
    assert m["PUITS-04"]["whp"]["tag"] == "P04-PT-101"
    assert m["MAN-C"]["p_man"]["tag"] == "MC-PT-001"


def test_cleaning_and_quality_report(imported):
    out, g, res = imported
    assert res["info"]["format"] == "long" and res["info"]["encoding"] == "cp1252" and res["info"]["delimiter"] == ";"
    q = {f"{x['entity']}.{x['signal']}": x for x in res["quality"]}
    assert len(q) == 63
    assert sum(x["sentinel"] for x in q.values()) > 50
    assert sum(x["duplicates"] for x in q.values()) == 40
    assert [k for k, x in q.items() if x.get("frozen_periods")] == ["PUITS-07.line_p"]
    assert all(x["coverage"] > 85 for x in q.values())
    assert os.path.exists(out / "quality.html")
    # psi converted to bar, values back in the expected range
    with open(out / "export.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    ann = [float(r["PUITS-11.ann"]) for r in rows[:48] if r["PUITS-11.ann"]]
    assert 1.0 < min(ann) and max(ann) < 3.0
    # no sentinel left anywhere
    for r in rows[::50]:
        for k, v in r.items():
            if k != "timestamp" and v:
                assert -1 <= float(v) < 700, (k, v)


def test_events_classified(imported):
    out, g, res = imported
    with open(res["events"], encoding="utf-8") as f:
        ev = list(csv.DictReader(f))
    probs = [(e["equipment"], e["type"]) for e in ev if e["type"] in ("shut_in", "failure", "intervention", "reopen")]
    assert ("PUITS-11", "intervention") in probs and ("R2", "failure") in probs and ("PUITS-09", "reopen") in probs
    assert res["event_counts"]["intervention"] == 7 and res["event_counts"]["shut_in"] == 2
    assert not res["review"]


def test_backtest_on_imported_site_data(cfg, imported):
    from agent.backtest import Backtest
    out, g, res = imported
    bt = Backtest(cfg, res["export"], res["events"], str(out / "bt")).run()
    s = bt.summary
    r = {(e.equipment, e.type): e.result for e in bt.problems}
    assert s["problems"] == 11 and s["ahead"] >= 7, bt.text()
    assert r[("PUITS-09", "failure")] == "at_time"
    assert r[("R2", "failure")] == "at_time"
    assert r[("PUITS-12", "failure")] == "missed"
    assert s["sms_pages"] < 40, s
