"""Main loop: read the SCADA, run the engine, and do the office work around it
(logbook, production, well tests, work orders, reports, handovers)."""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta

from . import reports
from .connectors.base import make_connector
from .engine import Engine
from .notify import Dispatcher
from .store import Store

log = logging.getLogger("agent")
VERSION = "2.0.0"


class App:
    def __init__(self, cfg, dispatcher=None):
        from .assistant import Assistant
        from .logbook import Logbook
        from .production import Production
        from .upkeep import Upkeep
        from .users import Users
        from .welltests import WellTests
        from .workorders import WorkOrders
        self.cfg = cfg
        self.version = VERSION
        self.store = Store(cfg.storage["path"])
        self.out = dispatcher or Dispatcher(cfg, self.store)
        self.engine = Engine(cfg, self.out, self.store)
        self.out.engine = self.engine
        self.conn = make_connector(cfg)
        self.logbook = Logbook(cfg, self.store, self.engine, self.out)
        self.logbook.restore()
        self.welltests = WellTests(cfg, self.store, self.engine, analysis=lambda: self.analysis())
        self.production = Production(cfg, self.store, self.engine, self.logbook, self.welltests)
        self.workorders = WorkOrders(cfg, self.store, self.engine, self.out, self.logbook,
                                     analysis=lambda force=False: self.analysis(0 if force else 600))
        self.assistant = Assistant(self)
        self.users = Users(cfg)
        self.upkeep = Upkeep(self)
        self.stop = threading.Event()
        self.last_report_day = None
        self.last_shift = None
        self._next_wo = 0.0
        self._next_findings = 0.0
        self.cycles = 0
        self.srv = None
        self.restart = False

    def request_restart(self):
        """Settings changed from the dashboard: stop cleanly, the launcher starts again."""
        self.restart = True
        self.stop.set()

    def snapshot(self):
        s = self.engine.state()
        s["journal"] = self.out.journal_mem[-120:][::-1]
        s["messages"] = self.store.recent_messages(40)
        s["connector"] = {"type": self.conn.name, "connected": self.conn.connected, "error": self.conn.last_error}
        s["version"] = VERSION
        s["cycles"] = self.cycles
        st = self.logbook.statuses()
        for w in s.get("wells", []):
            e = st.get(w["id"])
            w["manual"] = self.engine.wells[w["id"]].manual
            w["status_note"] = e["text"] if e and e["status"] != "production" else ""
        s["workorders"] = self.workorders.summary()
        s["assistant"] = self.cfg.assistant["mode"]
        return s

    # ------------------------------------------------------------------ reports
    def send_report(self, kind="daily"):
        if kind == "weekly":
            return self.send_weekly()
        if kind == "production":
            d = datetime.fromtimestamp(self.engine.now).date() - timedelta(days=1)
            subj, body = self.production.report(d)
        elif kind == "handover":
            from .logbook import handover
            subj, body = handover(self)
            self.logbook.add("handover", body, "agent")
            to = self.cfg.shifts.get("handover_to") or self.cfg.email.get("report_to", [])
            self.out.email(to, subj, body)
            self.out.journal("info", None, subj)
            return
        else:
            subj, body = reports.daily_report(self.engine, self.store, self.cfg, 24)
        self.out.email(self.cfg.email.get("report_to", []), subj, body)
        self.out.journal("info", None, subj)

    def send_weekly(self):
        """Weekly analysis to the supervisor; work lists to each team if enabled."""
        subj, body, per_team, found = reports.weekly_report(self.engine, self.store, self.cfg, self.logbook.interventions())
        self.out.email(self.cfg.email.get("report_to", []), subj, body)
        self.out.journal("info", None, subj)
        a = self.cfg.analysis
        if a.get("dispatch_to_teams"):
            for team, (ts, tb) in per_team.items():
                to = a.get("teams", {}).get(team, [])
                if to:
                    self.out.email(to, ts, tb)
                    self.out.journal("info", None, ts)
        self._analysis_cache = (time.time(), found)
        return found

    def analysis(self, max_age=600):
        cache = getattr(self, "_analysis_cache", None)
        if cache is None or time.time() - cache[0] > max_age:
            from .analysis import Analyst
            self._analysis_cache = (time.time(), Analyst(self.cfg, self.store, self.engine, self.logbook.interventions()).run())
        return [f.to_dict(self.cfg.language) for f in self._analysis_cache[1]]

    # ------------------------------------------------------------------ cycle
    def cycle(self):
        readings = self.conn.read()
        self.process(self.conn.now() or time.time(), readings)

    def process(self, now, readings):
        if self.cycles == 0:
            self.upkeep.load_resume(now)
        self.engine.step(now, readings, bool(readings), self.conn.last_error)
        self.cycles += 1
        self.record_daily(now)
        for job in (self.production.step, self.welltests.step, self.upkeep.step):
            try:
                job(now)
            except Exception:  # noqa: BLE001 - office work must never stop the surveillance
                log.exception("%s failed", getattr(job, "__qualname__", job))
        if now >= self._next_wo:
            self._next_wo = now + 300
            try:
                self.workorders.from_alarms()
                self.workorders.step()
            except Exception:  # noqa: BLE001
                log.exception("work orders failed")
        d = datetime.fromtimestamp(now)
        if now >= self._next_findings and self.cycles > 1:
            self._next_findings = now + 3600
            try:
                self.workorders.from_findings(self.analysis())
            except Exception:  # noqa: BLE001
                log.exception("work orders from findings failed")
        a = self.cfg.analysis
        if (d.weekday() == int(a["weekly_report_day"]) and d.hour == int(a["weekly_report_hour"])
                and getattr(self, "last_weekly", None) != d.date() and self.cycles > 1):
            self.last_weekly = d.date()
            self._safe(self.send_weekly)
        if d.hour == self.cfg.daily_report_hour and self.last_report_day != d.date():
            if self.last_report_day is not None or self.cycles > 1:
                self._safe(self.send_report, "daily")
                self._safe(self.send_report, "production")
            self.last_report_day = d.date()
        from .logbook import shift_bounds
        start, _ = shift_bounds(self.cfg, now)
        if self.last_shift is not None and start != self.last_shift and self.cycles > 1:
            self._safe(self.send_report, "handover")
        self.last_shift = start
        return now

    def _safe(self, fn, *a):
        try:
            return fn(*a)
        except Exception:  # noqa: BLE001
            log.exception("%s failed", fn.__name__)

    DAILY_WELL = ("whp", "wht", "line_p", "arrival_p", "ann", "ann_7", "oil_rate", "gl_flow", "gl_setpoint", "gl_valve_pos",
                  "water_flow", "battery", "solar_current", "cabinet_temp", "dp_choke", "dp_line")

    def record_daily(self, now):
        """Daily min / mean / max of every signal, for the long-term analysis."""
        smin = self.cfg.thresholds["sensor_min_barg"]
        with self.engine.lock:
            for w in self.engine.wells.values():
                vals = {"online": 1.0 if w.fresh else 0.0}
                if w.fresh:
                    for s_ in self.DAILY_WELL:
                        v = w.cur.get(s_)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            if s_ in ("whp", "line_p", "arrival_p", "ann", "ann_7") and (v < smin or (s_ == "whp" and w.whp_bad)):
                                continue    # failed transmitter: keep it out of the history
                            vals[s_] = v
                    vals["shut"] = 1.0 if self.engine.is_shut(w) else 0.0
                    vals["test"] = 1.0 if self.engine.on_test(w) else 0.0
                d = w.defer_m3 - getattr(w, "_defer_rec", 0.0)
                w._defer_rec = w.defer_m3
                vals["defer_m3"] = d
                self.store.accumulate(now, w.id, vals)
            for m in self.engine.mans.values():
                if m.fresh:
                    self.store.accumulate(now, m.id, {k: v for k, v in m.cur.items() if isinstance(v, (int, float))})

    def run(self, web=True):
        if web:
            from .web import serve
            self.srv = serve(self)
        self.out.journal("info", None, f"Agent {VERSION} démarré : {len(self.cfg.wells)} puits, "
                         f"{len(self.cfg.manifolds)} manifolds, connecteur {self.conn.name} (lecture seule).")
        try:
            signal.signal(signal.SIGTERM, lambda *a: self.stop.set())
        except ValueError:
            pass
        sim_clock = bool((self.cfg.clock or {}).get("opcua_node"))
        last, last_rt, rate = None, time.time(), 1.0
        while not self.stop.is_set():
            if self.conn.name == "csv":
                self.cycle()
                if self.conn.finished:
                    self.send_report("daily")
                    self.out.flush()
                    log.info("CSV replay finished")
                    break
                continue
            if sim_clock:
                # simulation: step each time the simulator clock has advanced by poll_seconds.
                # If the simulator stops answering, keep the clock running at the last observed
                # speed so that the loss of the SCADA link is still detected.
                readings = self.conn.read()
                now = self.conn.now() if readings else None
                rt = time.time()
                if now is not None:
                    if last is not None and now > last and rt > last_rt:
                        rate = (now - last) / (rt - last_rt)
                else:
                    if last is None:
                        now = rt
                        rate = 1.0
                    else:
                        now = last + (rt - last_rt) * rate
                if last is not None and now - last < self.cfg.poll_seconds - 0.5:
                    self.stop.wait(0.1)
                    continue
                last, last_rt = now, rt
                try:
                    self.process(now, readings)
                except Exception:  # noqa: BLE001
                    log.exception("cycle failed")
                continue
            t0 = time.time()
            try:
                self.cycle()
            except Exception:  # noqa: BLE001  - never let one bad cycle stop the surveillance
                log.exception("cycle failed")
            self.stop.wait(max(1.0, self.cfg.poll_seconds - (time.time() - t0)))
        self.store.flush_all()      # keep today's partial daily history
        self.production.flush()
        self.upkeep.save_baselines()
        self.conn.close()
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
