"""Main loop: read the SCADA, run the engine, schedule reports."""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime

from . import reports
from .connectors.base import make_connector
from .engine import Engine
from .notify import Dispatcher
from .store import Store

log = logging.getLogger("agent")
VERSION = "1.0.0"


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.storage["path"])
        self.out = Dispatcher(cfg, self.store)
        self.engine = Engine(cfg, self.out, self.store)
        self.out.engine = self.engine
        self.conn = make_connector(cfg)
        self.stop = threading.Event()
        self.last_report_day = None
        self.cycles = 0
        self.srv = None

    def snapshot(self):
        s = self.engine.state()
        s["journal"] = self.out.journal_mem[-120:][::-1]
        s["messages"] = self.store.recent_messages(40)
        s["connector"] = {"type": self.conn.name, "connected": self.conn.connected, "error": self.conn.last_error}
        s["version"] = VERSION
        s["cycles"] = self.cycles
        return s

    def send_report(self, kind="daily"):
        if kind == "weekly":
            return self.send_weekly()
        subj, body = reports.daily_report(self.engine, self.store, self.cfg, 24 if kind == "daily" else 8)
        self.out.email(self.cfg.email.get("report_to", []), subj, body)
        self.out.journal("info", None, subj)

    def send_weekly(self):
        """Weekly analysis to the supervisor; work lists to each team if enabled."""
        subj, body, per_team, found = reports.weekly_report(self.engine, self.store, self.cfg)
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
            self._analysis_cache = (time.time(), Analyst(self.cfg, self.store, self.engine).run())
        return [f.to_dict(self.cfg.language) for f in self._analysis_cache[1]]

    def cycle(self):
        readings = self.conn.read()
        self.process(self.conn.now() or time.time(), readings)

    def process(self, now, readings):
        self.engine.step(now, readings, bool(readings), self.conn.last_error)
        self.cycles += 1
        self.record_daily(now)
        d = datetime.fromtimestamp(now)
        a = self.cfg.analysis
        if (d.weekday() == int(a["weekly_report_day"]) and d.hour == int(a["weekly_report_hour"])
                and getattr(self, "last_weekly", None) != d.date() and self.cycles > 1):
            self.last_weekly = d.date()
            try:
                self.send_weekly()
            except Exception:  # noqa: BLE001
                log.exception("weekly report failed")
        if d.hour == self.cfg.daily_report_hour and self.last_report_day != d.date():
            if self.last_report_day is not None or self.cycles > 1:
                self.send_report("daily")
            self.last_report_day = d.date()
        return now

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
                    vals["shut"] = 1.0 if w.cur.get("sdv_closed") else 0.0
                    vals["test"] = 1.0 if w.cur.get("on_test") else 0.0
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
                self.process(now, readings)
                continue
            t0 = time.time()
            try:
                self.cycle()
            except Exception:  # noqa: BLE001  - never let one bad cycle stop the surveillance
                log.exception("cycle failed")
            self.stop.wait(max(1.0, self.cfg.poll_seconds - (time.time() - t0)))
        self.store.flush_all()      # keep today's partial daily history
        self.conn.close()
