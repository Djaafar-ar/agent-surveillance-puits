"""SQLite storage: alarm history, journal, messages sent, and daily well averages
(used by the long-term decline analysis). One file, no server to install."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (id INTEGER, run TEXT, entity TEXT, kind TEXT, code TEXT, severity TEXT,
  t0 REAL, t_end REAL, title TEXT, value TEXT, diag TEXT, action TEXT, parent INTEGER, ack_idx INTEGER,
  ack_t REAL, ack_via TEXT, level INTEGER, notified TEXT, PRIMARY KEY (run, id));
CREATE TABLE IF NOT EXISTS journal (t REAL, type TEXT, label TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS messages (t REAL, channel TEXT, recipient TEXT, subject TEXT, body TEXT, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS daily_sig (day TEXT, entity TEXT, signal TEXT, mean REAL, min REAL, max REAL, n INTEGER, total REAL,
  PRIMARY KEY (day, entity, signal));
CREATE INDEX IF NOT EXISTS daily_sig_e ON daily_sig (entity, signal, day);
CREATE INDEX IF NOT EXISTS journal_t ON journal (t);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.run = datetime.now().strftime("%Y%m%d-%H%M%S-%f")   # one id per start of the agent
        self._acc: dict = {}

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def save_alarm(self, a):
        self._x("INSERT OR REPLACE INTO alarms VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, self.run, a.entity, a.kind, a.code, a.severity, a.t0, a.t_end, a.title, a.value, a.diag, a.action,
                 a.parent.id if a.parent else None, a.ack[0] if a.ack else None, a.ack[1] if a.ack else None,
                 a.ack[2] if a.ack else None, a.level, json.dumps(sorted(set(a.notified)))))

    def journal(self, t, typ, label, text):
        self._x("INSERT INTO journal VALUES (?,?,?,?)", (t, typ, label, text))

    def message(self, t, channel, recipient, subject, body, status, error=""):
        self._x("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (t, channel, recipient, subject, body, status, error))

    def recent_journal(self, n=150):
        with self.lock:
            return [dict(zip(("t", "type", "label", "text"), r)) for r in
                    self.db.execute("SELECT t,type,label,text FROM journal ORDER BY rowid DESC LIMIT ?", (n,))]

    def recent_messages(self, n=60):
        with self.lock:
            return [dict(zip(("t", "channel", "recipient", "subject", "status", "error"), r)) for r in
                    self.db.execute("SELECT t,channel,recipient,subject,status,error FROM messages ORDER BY rowid DESC LIMIT ?", (n,))]

    # ---- daily history (one row per day, entity and signal) -------------------
    # This is what the long-term analysis works on: months or years of history
    # take a few thousand rows per well, not millions of raw samples.
    def accumulate(self, t: float, entity: str, values: dict):
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        for sig, v in values.items():
            if v is None or isinstance(v, str):
                continue
            v = float(v)
            if v != v:          # NaN
                continue
            a = self._acc.get((day, entity, sig))
            if a is None:
                self._acc[(day, entity, sig)] = [v, v, v, 1]
            else:
                a[0] += v
                a[1] = min(a[1], v)
                a[2] = max(a[2], v)
                a[3] += 1
        old = [k for k in self._acc if k[0] != day]
        if old:
            self._flush(old)

    def flush_all(self):
        self._flush(list(self._acc))

    def _flush(self, keys):
        rows = []
        for k in keys:
            a = self._acc.pop(k)
            rows.append((k[0], k[1], k[2], a[0] / a[3], a[1], a[2], a[3], a[0]))
        self.put_daily(rows, merge=True)

    def put_daily(self, rows, merge=False):
        """rows: (day, entity, signal, mean, min, max, n, total).
        merge=True adds to a row already saved for that day (agent restarted during the day)."""
        with self.lock:
            if merge:
                out = []
                for d, e, sg, mean, mn, mx, n, tot in rows:
                    old = self.db.execute("SELECT mean, min, max, n, total FROM daily_sig WHERE day=? AND entity=? AND signal=?",
                                          (d, e, sg)).fetchone()
                    if old and old[3]:
                        m0, mn0, mx0, n0, t0 = old
                        mean = (m0 * n0 + mean * n) / (n0 + n)
                        mn, mx, n, tot = min(mn, mn0), max(mx, mx0), n + n0, (tot or 0) + (t0 or 0)
                    out.append((d, e, sg, mean, mn, mx, n, tot))
                rows = out
            self.db.executemany("INSERT OR REPLACE INTO daily_sig VALUES (?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def series(self, entity: str, signal: str, days: int = 400, stat: str = "mean", full_days=False):
        """[(date, value)] for the last `days` days with data, oldest first.
        full_days=True drops days with less than half the usual number of readings
        (agent stopped part of the day), which would distort daily min/max trends."""
        col = {"mean": "mean", "min": "min", "max": "max", "total": "total", "n": "n"}[stat]
        with self.lock:
            rows = list(self.db.execute(f"SELECT day, {col}, n FROM daily_sig WHERE entity=? AND signal=? ORDER BY day DESC LIMIT ?",
                                        (entity, signal, days)))
        if full_days and rows:
            ns = sorted(r[2] or 0 for r in rows)
            typ = ns[len(ns) // 2]
            rows = [r for r in rows if (r[2] or 0) >= 0.5 * typ]
        return [(date.fromisoformat(d), v) for d, v, _ in rows[::-1]]

    def last_day(self):
        with self.lock:
            r = self.db.execute("SELECT MAX(day) FROM daily_sig").fetchone()
        return datetime.strptime(r[0], "%Y-%m-%d").date() if r and r[0] else None

    def open_alarms_before(self, since: float):
        """Alarms left open by previous runs (agent stopped or crashed) since a time."""
        with self.lock:
            rows = list(self.db.execute(
                "SELECT run, id, entity, code, severity, t0, ack_idx, ack_t, ack_via, level, notified FROM alarms "
                "WHERE run != ? AND t_end IS NULL AND t0 >= ?", (self.run, since)))
        out = []
        for r in rows:
            d = dict(zip(("run", "id", "entity", "code", "severity", "t0", "ack_idx", "ack_t", "ack_via", "level", "notified"), r))
            d["notified"] = json.loads(d["notified"] or "[]")
            out.append(d)
        return out

    def drop_alarm_row(self, run, aid):
        self._x("DELETE FROM alarms WHERE run = ? AND id = ?", (run, aid))

    def close_alarm_row(self, run, aid, t_end):
        self._x("UPDATE alarms SET t_end = ? WHERE run = ? AND id = ?", (t_end, run, aid))

    def alarm_history(self, since: float):
        """Alarms of every run since a timestamp: (entity, code, severity, t0, t_end, parent, ack_t, title)."""
        with self.lock:
            return list(self.db.execute("SELECT entity, code, severity, t0, t_end, parent, ack_t, title FROM alarms WHERE t0 >= ? ORDER BY t0",
                                        (since,)))
