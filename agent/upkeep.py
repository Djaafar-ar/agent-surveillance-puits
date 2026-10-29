"""Keeping the agent healthy when it runs for months without anyone looking at it.

  heartbeat    data/heartbeat.json rewritten every cycle (at most every 30 s):
               `python -m agent status` reads it and exits with an error if the
               agent has stopped, for the site's own monitoring.
  restart      alarms that were open when the agent stopped are resumed when they
               are detected again (same start time, acknowledgement and escalation,
               no new SMS); those that cleared meanwhile are closed.
  backup       a copy of the database every day in data/backup (7 kept).
  retention    journal, messages and alarm history older than 2 years deleted
               (the daily history used by the long-term analysis is kept).
  disk         warning e-mail if free disk space falls under 1 GB.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime

log = logging.getLogger("agent.upkeep")


class Upkeep:
    def __init__(self, app):
        self.app = app
        self.c = app.cfg.maintenance
        self.dir = os.path.dirname(os.path.abspath(app.cfg.storage["path"]))
        self.hb = os.path.join(self.dir, "heartbeat.json")
        self._hb_t = 0.0
        self._day = None
        self._resume_until = None
        self._bl_t = 0.0
        self.bl_path = os.path.join(self.dir, "baselines.json")

    # ------------------------------------------------------------------ restart
    def load_resume(self, now: float, window_h: float = 12):
        if self.load_baselines():
            log.info("normal values learned before the restart reloaded")
        rows = self.app.store.open_alarms_before(now - window_h * 3600)
        self.app.engine.resume = {(r["entity"], r["code"]): r for r in rows}
        self._resume_until = now + 30 * 60
        stale = self.app.store.open_alarms_before(0)
        for r in stale:
            if (r["entity"], r["code"]) not in self.app.engine.resume:
                self.app.store.close_alarm_row(r["run"], r["id"], now)
        if rows:
            log.info("%d alarm(s) open before the restart, waiting to see if they are still there", len(rows))

    def save_baselines(self):
        tmp = self.bl_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"t": time.time(), "data": self.app.engine.export_baselines()}, f)
            os.replace(tmp, self.bl_path)
        except OSError as e:
            log.warning("baselines: %s", e)

    def load_baselines(self):
        """Learned normal values saved before the restart (if recent enough to still be valid)."""
        if not os.path.exists(self.bl_path):
            return False
        try:
            with open(self.bl_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return False
        if time.time() - d.get("t", 0) > self.app.cfg.baseline_hours * 3600:
            return False
        self.app.engine.import_baselines(d.get("data"))
        return True

    def _end_resume(self, now):
        left = self.app.engine.resume
        for r in list(left.values()):
            self.app.store.close_alarm_row(r["run"], r["id"], now)
        if left:
            self.app.out.journal("info", None, (f"{len(left)} alarme(s) ouverte(s) avant le redémarrage ne sont plus présentes : fermées."
                                                if self.app.cfg.language == "fr" else
                                                f"{len(left)} alarm(s) open before the restart are no longer present: closed."))
        self.app.engine.resume = {}
        self._resume_until = None

    # ------------------------------------------------------------------ cycle
    def step(self, now: float):
        if self._resume_until is not None and now >= self._resume_until:
            self._end_resume(now)
        wall = time.time()
        if wall - self._bl_t >= 600:
            self._bl_t = wall
            self.save_baselines()
        if wall - self._hb_t >= 30:
            self._hb_t = wall
            self.heartbeat(now)
        d = datetime.now().date()
        if d != self._day:
            first = self._day is None
            self._day = d
            if not first:
                self.daily()

    def heartbeat(self, now):
        eng = self.app.engine
        with eng.lock:
            active = sum(1 for a in eng.alarms if a.parent is None and a.t_end is None)
        data = {"wall_time": time.time(), "data_time": now, "cycles": self.app.cycles, "version": getattr(self.app, "version", ""),
                "connector": self.app.conn.name, "connected": self.app.conn.connected, "last_error": self.app.conn.last_error,
                "active_alarms": active, "poll_seconds": self.app.cfg.poll_seconds, "pid": os.getpid()}
        tmp = self.hb + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.hb)
        except OSError as e:
            log.warning("heartbeat: %s", e)

    def daily(self):
        for job in (self.backup, self.retention, self.disk):
            try:
                job()
            except Exception:  # noqa: BLE001
                log.exception("upkeep %s failed", job.__name__)

    def backup(self):
        bdir = os.path.join(self.dir, "backup")
        os.makedirs(bdir, exist_ok=True)
        dst = os.path.join(bdir, f"agent-{datetime.now():%Y%m%d}.db")
        st = self.app.store
        with st.lock:
            out = sqlite3.connect(dst)
            try:
                st.db.backup(out)
            finally:
                out.close()
        files = sorted(glob.glob(os.path.join(bdir, "agent-*.db")))
        for f in files[:-int(self.c["backup_keep_days"])]:
            os.remove(f)
        return dst

    def retention(self):
        lim = time.time() - float(self.c["retention_days"]) * 86400
        st = self.app.store
        with st.lock:
            for tbl, col in (("journal", "t"), ("messages", "t"), ("alarms", "t0")):
                st.db.execute(f"DELETE FROM {tbl} WHERE {col} < ?", (lim,))
            st.db.commit()

    def disk(self):
        free = shutil.disk_usage(self.dir).free / 1e9
        if free < float(self.c["min_free_gb"]):
            fr = self.app.cfg.language == "fr"
            msg = (f"Espace disque faible sur le PC de l'agent : {free:.2f} Go libres. L'historique risque de ne plus être enregistré."
                   if fr else f"Low disk space on the agent PC: {free:.2f} GB free. History may stop being recorded.")
            self.app.out.journal("info", None, msg)
            self.app.out.email(self.app.cfg.email.get("report_to", []), msg.split(":")[0], msg)
        return free


def status(cfg) -> tuple[int, str]:
    """For `python -m agent status`: (exit code, text)."""
    hb = os.path.join(os.path.dirname(os.path.abspath(cfg.storage["path"])), "heartbeat.json")
    if not os.path.exists(hb):
        return 2, "No heartbeat file: the agent has never run with this configuration."
    with open(hb, encoding="utf-8") as f:
        d = json.load(f)
    age = time.time() - d["wall_time"]
    ok = age < 3 * d.get("poll_seconds", 60) + 60
    txt = (f"{'RUNNING' if ok else 'STOPPED?'}  last heartbeat {age:.0f} s ago, {d['cycles']} cycles, "
           f"connector {d['connector']} {'connected' if d['connected'] else 'NOT connected: ' + str(d['last_error'])}, "
           f"{d['active_alarms']} active alarm(s), data time {datetime.fromtimestamp(d['data_time']):%d/%m/%Y %H:%M}")
    return (0 if ok and d["connected"] else 1), txt
