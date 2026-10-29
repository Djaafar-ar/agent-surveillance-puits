"""Backtest: replay a historian export through the agent and compare what it
would have said with what really happened (the site's event log).

    python -m agent backtest --config config/config.yaml --csv export.csv --events events.csv --out data/backtest

Nothing is sent: no SMS, no e-mail. The result is a proof report:

  * for every real problem in the event log (shut-in, failure, intervention):
    was there a warning before it, how many days before, and what did it say;
  * warnings that were NOT followed by any logged event (false alarms, or
    problems nobody wrote down: to be checked with the team);
  * the alarm load an operator would have had (alarms per day, SMS).

Input formats
-------------
export.csv   timestamp, then one column per signal. Default column names are
             "<id>.<signal>" (PUITS-01.whp, MAN-A.p_man), change with --pattern.
             Missing columns are allowed: the rules that need them are skipped.
             Any regular interval works (1 min to 1 h); with hourly averages,
             events shorter than an hour are smoothed out.
events.csv   datetime, equipment, type, description
             type = shut_in | failure | intervention   (problems the agent should see)
                    test | reopen | planned             (routine: not counted)
             French words are accepted too (fermeture, panne, essai, réouverture...).
             The log is also replayed into the agent's logbook at the time each
             event happened, as operators would have written it: shut-ins and
             reopenings set the well status, interventions make the agent relearn
             the well. Nothing written after an event is known before it.
"""

from __future__ import annotations

import csv
import html
import json
import logging
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config as config_mod
from .connectors.csvreplay import _parse_ts

log = logging.getLogger("agent.backtest")

PROBLEM = {"shut_in", "failure", "intervention"}
TYPE_ALIASES = {"fermeture": "shut_in", "arret": "shut_in", "arrêt": "shut_in", "panne": "failure", "defaut": "failure",
                "défaut": "failure", "intervention": "intervention", "travaux": "intervention", "essai": "test",
                "reopen": "reopen", "réouverture": "reopen", "reouverture": "reopen", "ouverture": "reopen", "redémarrage": "reopen",
                "test": "test", "planifie": "planned", "planifié": "planned", "planned": "planned",
                "shut_in": "shut_in", "failure": "failure", "shut-in": "shut_in"}


class Recorder:
    """Takes the place of the SMS / e-mail dispatcher: counts, sends nothing."""

    def __init__(self, cfg):
        self.cfg, self.engine = cfg, None
        self.journal_mem: list = []
        self.sent_sms = 0
        self.pages: dict = {}      # alarm (first page) | escal (nobody acknowledges in a backtest) | clear
        self.emails = 0

    def journal(self, typ, label, text):
        pass

    def sms(self, idx, text, alarm, kind):
        self.sent_sms += 1
        self.pages[kind] = self.pages.get(kind, 0) + 1

    def alarm_email(self, a, upgraded):
        self.emails += 1

    def clear_email(self, a):
        pass

    def email(self, to, subject, body):
        self.emails += 1

    def flush(self, timeout=10):
        pass


@dataclass
class Warning_:
    t: float
    entity: str
    source: str           # "alarm" or "analysis"
    code: str
    title: str
    detail: str = ""
    team: str = ""
    t_last: float | None = None     # last time it was still active


@dataclass
class Event:
    t: float
    equipment: str
    type: str
    text: str
    first: Warning_ | None = None
    lead_h: float | None = None
    result: str = "missed"            # ahead | at_time | missed | followup (same problem as the event before)
    followup: object = None
    n_warn: int = 0
    related: list = field(default_factory=list)


def read_events(path):
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        head = [h.strip().lower() for h in next(rd)]
        for row in rd:
            if not row or not row[0].strip():
                continue
            d = dict(zip(head, row))
            typ = TYPE_ALIASES.get((d.get("type") or "").strip().lower(), "planned")
            out.append(Event(_parse_ts(d.get("datetime") or d.get("date") or row[0]), (d.get("equipment") or d.get("well") or "").strip(),
                             typ, (d.get("description") or "").strip()))
    out.sort(key=lambda e: e.t)
    return out


def _interval(csv_path):
    ts = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        next(rd)
        for row in rd:
            if row:
                ts.append(_parse_ts(row[0]))
            if len(ts) > 50:
                break
    if len(ts) < 3:
        raise ValueError("the export has fewer than 3 rows")
    return max(1, int(round(statistics.median(b - a for a, b in zip(ts, ts[1:])))))


class Backtest:
    def __init__(self, cfg, csv_path, events_path=None, out_dir="data/backtest", pattern="{id}.{signal}",
                 lookback_days=90, analysis_every_days=1):
        self.cfg = cfg
        self.csv_path, self.events_path, self.out_dir = csv_path, events_path, out_dir
        self.lookback = lookback_days * 86400
        self.every = analysis_every_days
        os.makedirs(out_dir, exist_ok=True)
        db = os.path.join(out_dir, "backtest.db")
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(db + ext):
                os.remove(db + ext)
        cfg.storage = {"path": db}
        cfg.clock = {}
        self.interval = _interval(csv_path)
        cfg.poll_seconds = self.interval          # rules count samples at the export's own rate
        for w in cfg.wells:
            w.tags = {s: pattern.format(id=w.id, signal=s) for s in config_mod.WELL_SIGNALS}
        for m in cfg.manifolds:
            m.tags = {s: pattern.format(id=m.id, signal=s) for s in config_mod.MANIFOLD_SIGNALS}
        cfg.connector = {"type": "csv", "file": csv_path, "allow_missing": True}
        cfg.daily_report_hour = -1                 # no scheduled reports during the replay
        cfg.analysis = dict(cfg.analysis, weekly_report_day=-1)

    # ------------------------------------------------------------------ replay
    def run(self, progress=None):
        from .analysis import Analyst
        from .app import App
        t0 = time.time()
        rec = Recorder(self.cfg)
        app = App(self.cfg, dispatcher=rec)
        self.app = app
        conn = app.conn
        events = read_events(self.events_path) if self.events_path else []
        todo = sorted([(e.t, i, e) for i, e in enumerate(events)] +
                      [(e.t + self.cfg.welltests["duration_hours"] * 3600, -1 - i, None) for i, e in enumerate(events) if e.type == "test"],
                      key=lambda x: (x[0], x[1]))
        test_end = {e.t + self.cfg.welltests["duration_hours"] * 3600: e for e in events if e.type == "test"}
        wells = {w.id for w in self.cfg.wells}
        episodes: list = []          # one warning each time a finding appears (again)
        active: dict = {}            # (entity, kind) -> last day it was reported
        last_day = None
        last_analysis = None
        self.t_first = self.t_last = None
        rows = 0
        while True:
            r = conn.read()
            if not r and conn.last_error:
                raise RuntimeError(conn.last_error)
            if conn.finished and not r:
                break
            now = conn.now()
            # the logbook as it would have been written: every event up to now
            while todo and todo[0][0] <= now:
                t_ev, k, e = todo.pop(0)
                self._apply(app, e, t_ev, test_end, wells)
            self.t_first = self.t_first or now
            self.t_last = now
            app.process(now, r)
            rows += 1
            day = datetime.fromtimestamp(now).date()
            if day != last_day:
                if last_day is not None and (last_analysis is None or (day - last_analysis).days >= self.every):
                    last_analysis = day
                    seen_at = datetime.combine(day, datetime.min.time()).timestamp()
                    # what the site knew that day: the logbook written so far
                    known = app.logbook.interventions()
                    for f in Analyst(self.cfg, app.store, app.engine, interventions=known).run(end=last_day):
                        k = (f.entity, f.kind)
                        li = max(known.get(f.entity, [None]) or [None], key=lambda d: d or datetime.min.date())
                        renewed = li is not None and k in active and active[k][1].t < datetime.combine(li, datetime.min.time()).timestamp()
                        if k not in active or (last_day - active[k][0]).days > 30 or renewed:
                            ep = Warning_(seen_at, f.entity, "analysis", f.kind, f.title, f.evidence, f.team)
                            episodes.append(ep)
                            active[k] = [last_day, ep]
                        active[k][0] = last_day
                        active[k][1].t_last = seen_at
                    if progress:
                        progress(day)
                last_day = day
            if conn.finished:
                break
        app.store.flush_all()
        app.production.flush()
        self.workorders = app.workorders.list()
        self.rows = rows
        self.sms = rec.sent_sms
        self.pages = rec.pages
        alarms = [a for a in app.engine.alarms if a.parent is None]
        self.alarms = [Warning_(a.t0, a.entity, "alarm", a.code, a.title, a.value, t_last=a.t_end or self.t_last) for a in alarms]
        self.findings = sorted(episodes, key=lambda w: w.t)
        self.warnings = sorted(self.alarms + self.findings, key=lambda w: w.t)
        self.events = events
        self._score()
        self.seconds = time.time() - t0
        return self

    def _apply(self, app, e, t, test_end, wells):
        lb = app.logbook
        if e is None:                       # end of a well test
            e = test_end.get(t)
            if e and e.equipment in wells and app.engine.wells[e.equipment].manual == "test":
                lb.set_status(e.equipment, "production", "journal", "fin d'essai", t=t)
            return
        if e.type == "shut_in" and e.equipment in wells:
            lb.set_status(e.equipment, "shut", "journal", e.text, t=e.t)
        elif e.type == "reopen" and e.equipment in wells:
            lb.set_status(e.equipment, "production", "journal", e.text, t=e.t)
        elif e.type == "test" and e.equipment in wells and not app.engine.wells[e.equipment].manual:
            lb.set_status(e.equipment, "test", "journal", e.text, t=e.t)
        elif e.type == "intervention":
            if e.equipment in wells:
                lb.intervention(e.equipment, e.text, "journal", t=e.t)
            else:
                lb.add("intervention", e.text, "journal", e.equipment, t=e.t)
        else:
            lb.note(e.text or e.type, "journal", e.equipment, t=e.t)

    # ------------------------------------------------------------------ scoring
    def _equip_ids(self, entity: str) -> set:
        """Ids an alarm or finding entity refers to ('Répéteur R2' -> R2)."""
        return {entity, entity.split()[-1]}

    def _related(self, equipment: str) -> set:
        ids = {equipment}
        w = next((w for w in self.cfg.wells if w.id == equipment), None)
        if w:
            ids |= {w.manifold, w.repeater}
            m = next((m for m in self.cfg.manifolds if m.id == w.manifold), None)
            if m and m.satellite:
                ids.add(m.satellite)
        else:   # repeater, manifold or satellite: its wells' RTU alarms count too
            ids |= {w.id for w in self.cfg.wells if equipment in (w.repeater, w.manifold)}
        return ids

    def _score(self):
        problems = []
        last: dict = {}
        for e in self.events:
            if e.type not in PROBLEM:
                continue
            # a shut-in a few hours after a failure on the same equipment is the same problem
            p0 = last.get(e.equipment)
            if p0 is not None and e.t - p0.t <= 6 * 3600:
                e.result, e.followup = "followup", p0
                continue
            problems.append(e)
            last[e.equipment] = e
        self.problems = problems
        prev: dict = {}
        for e in problems:
            rel = self._related(e.equipment)
            since = prev.get(e.equipment, -1e18) + 86400
            # a warning counts if it appeared after the previous problem on this equipment and was
            # still active in the lookback window (a finding can stay on for months before the event)
            # a long-term finding can stay on for months before the event; a real-time alarm only
            # counts if it was still active within 2 days of the event (an RTU loss 2 months
            # earlier did not announce this repeater failure)
            cands = [w for w in self.warnings if self._equip_ids(w.entity) & rel and since <= w.t <= e.t + 6 * 3600
                     and (w.t_last or w.t) >= e.t - (self.lookback if w.source == "analysis" else 2 * 86400)]
            e.related = cands
            e.n_warn = len(cands)
            if cands:
                e.first = cands[0]
                e.lead_h = (e.t - cands[0].t) / 3600
                e.result = "ahead" if e.lead_h >= 24 else "at_time"
            prev[e.equipment] = e.t
        # warnings followed by no logged problem
        self.unconfirmed = []
        for w in self.warnings:
            ids = self._equip_ids(w.entity)
            hit = any(self._related(e.equipment) & ids and w.t - 86400 <= e.t <= max(w.t + self.lookback, (w.t_last or w.t) + 7 * 86400)
                      for e in problems)
            planned = any(self._related(e.equipment) & ids and abs(w.t - e.t) <= 12 * 3600
                          for e in self.events if e.type in ("test", "planned"))
            if not hit and not planned:
                self.unconfirmed.append(w)
        days = max(1.0, ((self.t_last or 0) - (self.t_first or 0)) / 86400)
        self.summary = {
            "days": round(days, 1), "rows": self.rows, "interval_s": self.interval,
            "problems": len(problems),
            "ahead": sum(e.result == "ahead" for e in problems),
            "at_time": sum(e.result == "at_time" for e in problems),
            "missed": sum(e.result == "missed" for e in problems),
            "median_lead_days": round(statistics.median([e.lead_h / 24 for e in problems if e.result == "ahead"]), 1)
            if any(e.result == "ahead" for e in problems) else None,
            "alarms": len(self.alarms), "alarms_per_day": round(len(self.alarms) / days, 2),
            "findings": len(self.findings), "sms": self.sms, "sms_pages": self.pages.get("alarm", 0),
            "sms_escalations": self.pages.get("escal", 0),
            "unconfirmed_by_type": dict(sorted(Counter(w.title for w in self.unconfirmed).items(), key=lambda x: -x[1])),
            "unconfirmed": len(self.unconfirmed),
            "workorders": len(self.workorders),
            "start": datetime.fromtimestamp(self.t_first).isoformat(" ", "minutes") if self.t_first else None,
            "end": datetime.fromtimestamp(self.t_last).isoformat(" ", "minutes") if self.t_last else None,
        }

    # ------------------------------------------------------------------ reports
    def write(self):
        L = TXT[self.cfg.language]
        with open(os.path.join(self.out_dir, "report.html"), "w", encoding="utf-8") as f:
            f.write(self._html(L))
        with open(os.path.join(self.out_dir, "report.txt"), "w", encoding="utf-8") as f:
            f.write(self.text())
        with open(os.path.join(self.out_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump({"summary": self.summary,
                       "events": [{"t": datetime.fromtimestamp(e.t).isoformat(" ", "minutes"), "equipment": e.equipment, "type": e.type,
                                   "description": e.text, "result": e.result, "lead_days": None if e.lead_h is None else round(e.lead_h / 24, 1),
                                   "first_warning": None if not e.first else {"t": datetime.fromtimestamp(e.first.t).isoformat(" ", "minutes"),
                                                                             "entity": e.first.entity, "source": e.first.source,
                                                                             "code": e.first.code, "title": e.first.title}}
                                  for e in self.events if e.type in PROBLEM and e.result != "followup"],
                       "unconfirmed": [{"t": datetime.fromtimestamp(w.t).isoformat(" ", "minutes"), "entity": w.entity, "source": w.source,
                                        "code": w.code, "title": w.title} for w in self.unconfirmed]},
                      f, ensure_ascii=False, indent=1)
        return os.path.join(self.out_dir, "report.html")

    def _lead(self, e, L):
        if e.result == "ahead":
            d = e.lead_h / 24
            return L["ahead"].format(d=f"{d:.0f}" if d >= 2 else f"{d:.1f}")
        if e.result == "at_time":
            return L["at_time"]
        return L["missed"]

    def text(self):
        L = TXT[self.cfg.language]
        s = self.summary
        out = [L["title"], "=" * len(L["title"]), L["period"].format(**s), "",
               L["sum1"].format(**s), L["sum2"].format(**s), L["sum3"].format(**s), ""]
        for e in self.events:
            if e.type not in PROBLEM or e.result == "followup":
                continue
            out.append(f"{datetime.fromtimestamp(e.t):%d/%m/%Y %H:%M}  {e.equipment:<10} {e.text}")
            out.append(f"    -> {self._lead(e, L)}" + (f" : {e.first.title} ({e.first.entity}, {datetime.fromtimestamp(e.first.t):%d/%m/%Y})" if e.first else ""))
        out += ["", L["unconf"].format(n=len(self.unconfirmed))]
        if self.unconfirmed:
            out.append("  " + " ; ".join(f"{t} : {n}" for t, n in self.summary["unconfirmed_by_type"].items()))
        for w in self.unconfirmed[:30]:
            out.append(f"  {datetime.fromtimestamp(w.t):%d/%m/%Y %H:%M}  {w.entity:<12} {w.title}")
        out += ["", L["note"]]
        return "\n".join(out) + "\n"

    def _html(self, L):
        s = self.summary
        esc = html.escape
        rows = []
        for e in self.events:
            if e.type not in PROBLEM or e.result == "followup":
                continue
            cls = {"ahead": "ok", "at_time": "mid", "missed": "bad"}[e.result]
            fw = ""
            if e.first:
                fw = (f"<b>{esc(e.first.title)}</b><br><span class=m>{esc(e.first.entity)} · "
                      f"{datetime.fromtimestamp(e.first.t):%d/%m/%Y %H:%M} · {L['src_' + e.first.source]}</span>")
                if e.first.detail:
                    d = e.first.detail
                    if len(d) > 420:
                        d = d[:420].rsplit(" ", 1)[0] + "…"
                    fw += f"<br><span class=m>{esc(d)}</span>"
                more, seen = [], {e.first.title}
                for w in e.related[1:]:
                    if w.title not in seen:
                        seen.add(w.title)
                        more.append(f"{w.title} ({datetime.fromtimestamp(w.t):%d/%m})")
                if more:
                    fw += f"<br><span class=m>{esc(L['then'])} : {esc(' ; '.join(more[:4]))}</span>"
            rows.append(f"<tr><td>{datetime.fromtimestamp(e.t):%d/%m/%Y %H:%M}</td><td><b>{esc(e.equipment)}</b></td>"
                        f"<td>{esc(L['type_' + e.type])}<br><span class=m>{esc(e.text)}</span></td>"
                        f"<td><span class='tag {cls}'>{esc(self._lead(e, L))}</span></td><td>{fw}</td></tr>")
        unc = "".join(f"<tr><td>{datetime.fromtimestamp(w.t):%d/%m/%Y %H:%M}</td><td>{esc(w.entity)}</td>"
                      f"<td>{esc(w.title)}</td><td>{L['src_' + w.source]}</td></tr>" for w in self.unconfirmed[:200])
        return f"""<!doctype html><html lang="{self.cfg.language}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(L['title'])}</title>
<style>
body{{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f4f3ef;color:#1d1d1b}}
main{{max-width:1100px;margin:0 auto;padding:28px 16px 60px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:18px;margin:34px 0 10px}}
.m{{color:#66645e;font-size:13px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:22px 0}}
.k{{background:#fff;border:1px solid #e1dfd8;border-radius:10px;padding:14px 16px}}
.k b{{display:block;font-size:28px;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1dfd8;border-radius:10px;overflow:hidden}}
th,td{{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid #eceae4;font-size:14px}}
th{{background:#faf9f6;font-weight:600;font-size:13px;color:#4a4843}}
.tag{{display:inline-block;padding:2px 8px;border-radius:99px;font-size:13px;font-weight:600;white-space:nowrap}}
.ok{{background:#dff0e3;color:#1e6b35}} .mid{{background:#fdf0d2;color:#7a5600}} .bad{{background:#f8dcdc;color:#9b1c1c}}
.wrap{{overflow-x:auto}} .note{{background:#fff;border-left:4px solid #b9b5aa;padding:12px 16px;margin-top:30px;font-size:14px}}
</style></head><body><main>
<h1>{esc(L['title'])}</h1>
<p class=m>{esc(self.cfg.site_name)} · {esc(L['period'].format(**s))}</p>
<div class=kpis>
<div class=k><b>{s['ahead']} / {s['problems']}</b>{esc(L['k_ahead'])}</div>
<div class=k><b>{s['median_lead_days'] if s['median_lead_days'] is not None else '-'}</b>{esc(L['k_lead'])}</div>
<div class=k><b>{s['at_time']}</b>{esc(L['k_at'])}</div>
<div class=k><b>{s['missed']}</b>{esc(L['k_missed'])}</div>
<div class=k><b>{s['alarms_per_day']}</b>{esc(L['k_rate'])}</div>
<div class=k><b>{s['sms_pages']}</b>{esc(L['k_pages'])}</div>
<div class=k><b>{s['unconfirmed']}</b>{esc(L['k_unc'])}</div>
</div>
<h2>{esc(L['h_events'])}</h2>
<div class=wrap><table><tr><th>{L['c_date']}</th><th>{L['c_eq']}</th><th>{L['c_ev']}</th><th>{L['c_res']}</th><th>{L['c_first']}</th></tr>
{''.join(rows) or f"<tr><td colspan=5>{esc(L['no_events'])}</td></tr>"}</table></div>
<h2>{esc(L['unconf'].format(n=len(self.unconfirmed)))}</h2>
<p class=m>{esc(L['unconf_help'])}</p>
<p>{esc(" ; ".join(f"{t} : {n}" for t, n in s["unconfirmed_by_type"].items()))}</p>
<div class=wrap><table><tr><th>{L['c_date']}</th><th>{L['c_eq']}</th><th>{L['c_warn']}</th><th>{L['c_src']}</th></tr>{unc}</table></div>
<div class=note>{esc(L['note'])}</div>
</main></body></html>"""


TXT = {
    "fr": dict(
        title="Rapport de backtest de l'agent de surveillance",
        period="Données du {start} au {end} ({days} jours, une valeur toutes les {interval_s} s)",
        sum1="Problèmes réels dans le journal : {problems}. Prévenus à l'avance : {ahead} (délai médian {median_lead_days} jours). "
             "Détectés au moment : {at_time}. Non vus : {missed}.",
        sum2="Alarmes présentées à l'opérateur : {alarms} ({alarms_per_day} par jour). Alarmes qui auraient réveillé l'astreinte par SMS : "
             "{sms_pages} (plus {sms_escalations} SMS d'escalade, car personne n'acquitte pendant un backtest).",
        sum3="Constats de l'analyse long terme : {findings}. Avertissements non suivis d'un événement enregistré : {unconfirmed}. "
             "Ordres de travail qui auraient été ouverts : {workorders}.",
        then="ensuite", ahead="Prévenu {d} jours avant", at_time="Détecté au moment", missed="Non vu",
        unconf="Avertissements sans événement enregistré ({n})",
        unconf_help="Fausse alarme, ou problème réel que personne n'a noté dans le journal : à revoir avec l'équipe.",
        note="Un avertissement compte pour un événement s'il concerne le même équipement (ou son manifold, son répéteur, sa station) "
             "dans les 90 jours qui précèdent. Les résultats dépendent de la qualité du journal des événements. Aucun SMS ni e-mail "
             "n'a été envoyé pendant ce backtest.",
        k_ahead="problèmes prévenus à l'avance", k_lead="jours d'avance (médiane)", k_at="détectés au moment",
        k_missed="non vus", k_rate="alarmes par jour", k_pages="SMS d'astreinte sur la période", k_unc="avertissements sans événement",
        h_events="Chaque problème réel, et ce que l'agent aurait dit", c_date="Date", c_eq="Équipement", c_ev="Événement",
        c_res="Résultat", c_first="Premier avertissement", c_warn="Avertissement", c_src="Source",
        src_alarm="alarme temps réel", src_analysis="analyse long terme", no_events="Aucun journal d'événements fourni.",
        type_shut_in="Fermeture", type_failure="Panne", type_intervention="Intervention", type_test="Essai", type_planned="Planifié"),
    "en": dict(
        title="Surveillance agent backtest report",
        period="Data from {start} to {end} ({days} days, one value every {interval_s} s)",
        sum1="Real problems in the event log: {problems}. Warned ahead: {ahead} (median lead {median_lead_days} days). "
             "Detected as it happened: {at_time}. Missed: {missed}.",
        sum2="Alarms presented to the operator: {alarms} ({alarms_per_day} per day). Alarms that would have paged the on-call "
             "engineer: {sms_pages} (plus {sms_escalations} escalation SMS, since nobody acknowledges during a backtest).",
        sum3="Long-term analysis findings: {findings}. Warnings not followed by a logged event: {unconfirmed}. "
             "Work orders that would have been opened: {workorders}.",
        then="then", ahead="Warned {d} days ahead", at_time="Detected as it happened", missed="Missed",
        unconf="Warnings with no logged event ({n})",
        unconf_help="False alarm, or a real problem nobody wrote in the log: to review with the team.",
        note="A warning counts for an event if it concerns the same equipment (or its manifold, repeater or station) within the 90 "
             "days before. Results depend on the quality of the event log. No SMS or e-mail was sent during this backtest.",
        k_ahead="problems warned ahead", k_lead="days of warning (median)", k_at="detected as it happened",
        k_missed="missed", k_rate="alarms per day", k_pages="on-call pages over the period", k_unc="warnings with no event",
        h_events="Each real problem, and what the agent would have said", c_date="Date", c_eq="Equipment", c_ev="Event",
        c_res="Result", c_first="First warning", c_warn="Warning", c_src="Source",
        src_alarm="real-time alarm", src_analysis="long-term analysis", no_events="No event log provided.",
        type_shut_in="Shut-in", type_failure="Failure", type_intervention="Intervention", type_test="Test", type_planned="Planned"),
}
