"""CSV replay connector.

Replays a historian export so the rules can be checked on real past data,
without any network access to the SCADA. Expected format (one row per
timestamp, one column per tag):

    timestamp,PUITS-01.WHP,PUITS-01.ANN,MAN-A.P_MAN,...
    2026-05-19 02:50:00,22.7,1.2,11.8,...

In the YAML, each signal's tag is the column name. An empty cell means "no
value received" (lost RTU). The replay advances one row per agent cycle and
provides the clock, so a month of data can be replayed in minutes.
"""

from __future__ import annotations

import csv
from datetime import datetime

from .base import Connector, Sample


def _parse_ts(s: str) -> float:
    s = s.strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp '{s}'")


def _val(s: str):
    s = s.strip()
    if s == "":
        return None
    if s.lower() in ("true", "on", "yes"):
        return True
    if s.lower() in ("false", "off", "no"):
        return False
    return float(s.replace(",", "."))


class CsvConnector(Connector):
    name = "csv"

    def _connect(self):
        with open(self.c["file"], encoding="utf-8-sig", newline="") as f:
            rd = csv.reader(f, delimiter=self.c.get("delimiter", ","))
            head = next(rd)
            self.rows = [r for r in rd if r]
        self.col = {h.strip(): i for i, h in enumerate(head)}
        missing = [f"{e}.{s} -> {t}" for e, sigs in self.points().items() for s, t in sigs.items() if t not in self.col]
        if missing and not self.c.get("allow_missing"):
            raise ValueError("columns not found in CSV: " + ", ".join(missing[:5]))
        # allow_missing (backtest): signals absent from the export are simply not read
        self._pts = {e: {s: t for s, t in sigs.items() if t in self.col} for e, sigs in super().points().items()}
        if not any(self._pts.values()):
            raise ValueError("no configured signal found in the CSV header")
        self.i = 0
        self._clock = None
        self._last: dict[str, float] = {}

    def _read(self):
        if self.i >= len(self.rows):
            return {}
        row = self.rows[self.i]
        self.i += 1
        t = _parse_ts(row[0])
        self._clock = t
        res = {}
        for ent, sigs in self.points().items():
            for sig, col in sigs.items():
                v = _val(row[self.col[col]]) if self.col[col] < len(row) else None
                if v is None:
                    # keep the last value with its old timestamp: looks stale, like a real SCADA
                    if col in self._last:
                        res.setdefault(ent, {})[sig] = Sample(self._last[col][0], self._last[col][1], True)
                    continue
                self._last[col] = (v, t)
                res.setdefault(ent, {})[sig] = Sample(v, t, True)
        return res

    def points(self):
        return getattr(self, "_pts", None) or super().points()

    def now(self):
        return self._clock

    @property
    def finished(self):
        return hasattr(self, "rows") and self.i >= len(self.rows)
