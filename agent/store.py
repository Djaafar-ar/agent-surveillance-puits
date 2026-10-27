"""SQLite storage: alarm history, journal, messages sent, and daily well averages
(used by the long-term decline analysis). One file, no server to install."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (id INTEGER, run TEXT, entity TEXT, kind TEXT, code TEXT, severity TEXT,
  t0 REAL, t_end REAL, title TEXT, value TEXT, diag TEXT, action TEXT, parent INTEGER, ack_idx INTEGER,
  ack_t REAL, ack_via TEXT, level INTEGER, notified TEXT, PRIMARY KEY (run, id));
CREATE TABLE IF NOT EXISTS journal (t REAL, type TEXT, label TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS messages (t REAL, channel TEXT, recipient TEXT, subject TEXT, body TEXT, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS daily (day TEXT, well TEXT, whp REAL, wht REAL, n INTEGER, shut INTEGER, PRIMARY KEY (day, well));
CREATE INDEX IF NOT EXISTS journal_t ON journal (t);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.run = datetime.now().strftime("%Y%m%d-%H%M%S")
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

    # ---- daily averages for the decline analysis --------------------------
    def accumulate(self, t: float, well: str, whp, wht, shut: bool):
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        k = (day, well)
        a = self._acc.setdefault(k, [0.0, 0.0, 0, 0, 0])
        if whp is not None and not shut:
            a[0] += whp
            a[2] += 1
            if wht is not None:
                a[1] += wht
                a[4] += 1
        if shut:
            a[3] = 1
        # flush days that are over
        for (d, w) in [x for x in self._acc if x[0] != day]:
            self._flush(d, w)

    def _flush(self, d, w):
        a = self._acc.pop((d, w))
        if a[2]:
            self._x("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                    (d, w, a[0] / a[2], a[1] / a[4] if a[4] else None, a[2], a[3]))

    def daily(self, well: str, days: int = 400):
        with self.lock:
            return list(self.db.execute("SELECT day, whp, wht, n, shut FROM daily WHERE well=? ORDER BY day DESC LIMIT ?",
                                        (well, days)))[::-1]
