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
        subj, body = reports.daily_report(self.engine, self.store, self.cfg, 24 if kind == "daily" else 8)
        self.out.email(self.cfg.email.get("report_to", []), subj, body)
        self.out.journal("info", None, subj)

    def cycle(self):
        readings = self.conn.read()
        self.process(self.conn.now() or time.time(), readings)

    def process(self, now, readings):
        self.engine.step(now, readings, bool(readings), self.conn.last_error)
        self.cycles += 1
        for w in self.engine.wells.values():
            if w.fresh:
                self.store.accumulate(now, w.id, w.cur.get("whp") if not w.whp_bad else None, w.cur.get("wht"),
                                      bool(w.cur.get("sdv_closed")))
        d = datetime.fromtimestamp(now)
        if d.hour == self.cfg.daily_report_hour and self.last_report_day != d.date():
            if self.last_report_day is not None or self.cycles > 1:
                self.send_report("daily")
            self.last_report_day = d.date()
        return now

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
        self.conn.close()
