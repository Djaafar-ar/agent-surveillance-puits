"""Production accounting: the daily production report and the deferred
production by cause, which are usually typed by hand every morning.

Every cycle, each well is in one of four states:

  flowing   producing; oil from the oil-rate signal if there is one, otherwise
            from the reference rate (last validated well test, else the learned
            rate, else the potential written in the configuration)
  test      on test: producing into the test separator, not deferred
  shut      shut in or under intervention: the reference rate is deferred, with
            its cause (written in the logbook, or "safety valve closed, cause to
            be specified" when only the valve signal says so)
  no_data   the RTU is not answering: production unknown, counted apart

Figures are estimates unless the well has a flow meter: the report says so.
"""

from __future__ import annotations

import csv
import io
import threading
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS prod (day TEXT, well TEXT, state TEXT, cause TEXT, hours REAL, oil_m3 REAL, defer_m3 REAL,
  measured_h REAL, PRIMARY KEY (day, well, state, cause));
"""
STATES = ("flowing", "test", "shut", "no_data")
LAB = {"fr": {"flowing": "en production", "test": "en essai", "shut": "fermé", "no_data": "sans données",
              "cause_sdv": "vanne de sécurité fermée, cause à préciser", "cause_shut": "fermé, cause à préciser",
              "cause_work": "intervention"},
       "en": {"flowing": "producing", "test": "on test", "shut": "shut in", "no_data": "no data",
              "cause_sdv": "safety valve closed, cause to be specified", "cause_shut": "shut in, cause to be specified",
              "cause_work": "intervention"}}


class Production:
    def __init__(self, cfg, store, engine, logbook=None, welltests=None):
        self.cfg, self.store, self.engine, self.logbook, self.welltests = cfg, store, engine, logbook, welltests
        self.L = cfg.language
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()
        self._acc: dict = {}          # (day, well, state, cause) -> [hours, oil, defer, measured_h]
        self._hourly: dict = {}       # (hour start, well, cause) -> deferred m3, for the shift handover
        self._prev = None
        self._last_flush = 0.0
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ rates
    def ref_rate(self, w) -> tuple[float | None, str]:
        """Reference oil rate (m3/d) and where it comes from."""
        if self.welltests:
            t = self.welltests.reference(w.id)
            if t is not None:
                return t, "test"
        b = w.base.get("oil_rate")
        if b is not None and b.ready and b.value:
            return b.value, "learned"
        if w.cfg.potential_m3d:
            return w.cfg.potential_m3d, "potential"
        return None, "none"

    def state_of(self, w) -> tuple[str, str]:
        L = LAB[self.L]
        if w.manual in ("shut", "work"):
            c = self.logbook.cause(w.id) if self.logbook else ""
            return "shut", c or (L["cause_work"] if w.manual == "work" else L["cause_shut"])
        if not w.fresh:
            return "no_data", ""
        if w.cur.get("sdv_closed") is True:
            return "shut", L["cause_sdv"]
        if self.engine.on_test(w):
            return "test", ""
        return "flowing", ""

    # ------------------------------------------------------------------ step
    def step(self, now: float):
        dt = 0.0 if self._prev is None else now - self._prev
        self._prev = now
        if dt <= 0:
            return
        dt = min(dt, 3 * self.cfg.poll_seconds)      # a gap in the readings is not production
        h = dt / 3600
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        hour = int(now // 3600 * 3600)
        with self.engine.lock, self.lock:
            for w in self.engine.wells.values():
                st, cause = self.state_of(w)
                ref, _ = self.ref_rate(w)
                oil = defer = meas = 0.0
                if st in ("flowing", "test"):
                    q = w.cur.get("oil_rate") if "oil_rate" in w.cfg.tags else None
                    if isinstance(q, (int, float)) and not isinstance(q, bool) and q >= 0:
                        oil, meas = q * dt / 86400, h
                    elif ref:
                        oil = ref * dt / 86400
                elif st == "shut" and ref:
                    defer = ref * dt / 86400
                    k = (hour, w.id, cause)
                    self._hourly[k] = self._hourly.get(k, 0.0) + defer
                a = self._acc.setdefault((day, w.id, st, cause), [0.0, 0.0, 0.0, 0.0])
                a[0] += h
                a[1] += oil
                a[2] += defer
                a[3] += meas
            old = [k for k in self._hourly if k[0] < now - 3 * 86400]
            for k in old:
                del self._hourly[k]
        if now - self._last_flush > 600 or any(k[0] != day for k in self._acc):
            self.flush()
            self._last_flush = now

    def flush(self):
        with self.lock:
            rows, self._acc = self._acc, {}
        if not rows:
            return
        with self.store.lock:
            for (day, well, st, cause), (hh, oil, de, me) in rows.items():
                self.store.db.execute(
                    "INSERT INTO prod VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day, well, state, cause) DO UPDATE SET "
                    "hours = hours + excluded.hours, oil_m3 = oil_m3 + excluded.oil_m3, defer_m3 = defer_m3 + excluded.defer_m3, "
                    "measured_h = measured_h + excluded.measured_h", (day, well, st, cause, hh, oil, de, me))
            self.store.db.commit()

    # ------------------------------------------------------------------ read
    def _rows(self, d0: str, d1: str):
        self.flush()
        with self.store.lock:
            return list(self.store.db.execute(
                "SELECT day, well, state, cause, hours, oil_m3, defer_m3, measured_h FROM prod WHERE day >= ? AND day <= ? ORDER BY day, well",
                (d0, d1)))

    def day(self, d: date) -> dict:
        """Per-well figures for one day."""
        ds = d.isoformat()
        wells = {w.id: {"well": w.id, "manifold": w.manifold, "flowing_h": 0.0, "test_h": 0.0, "shut_h": 0.0, "no_data_h": 0.0,
                        "oil_m3": 0.0, "defer_m3": 0.0, "measured": False, "causes": {}} for w in self.cfg.wells}
        for day, well, st, cause, hh, oil, de, me in self._rows(ds, ds):
            r = wells.setdefault(well, {"well": well, "manifold": "", "flowing_h": 0.0, "test_h": 0.0, "shut_h": 0.0,
                                        "no_data_h": 0.0, "oil_m3": 0.0, "defer_m3": 0.0, "measured": False, "causes": {}})
            r[st + "_h"] += hh
            r["oil_m3"] += oil
            r["defer_m3"] += de
            r["measured"] = r["measured"] or me > 0.5 * max(hh, 1e-9)
            if de:
                r["causes"][cause] = r["causes"].get(cause, 0.0) + de
        return {"day": ds, "wells": list(wells.values()),
                "oil_m3": sum(r["oil_m3"] for r in wells.values()), "defer_m3": sum(r["defer_m3"] for r in wells.values()),
                "no_data_h": sum(r["no_data_h"] for r in wells.values())}

    def causes(self, d0: date, d1: date) -> list[tuple[str, float, list[str]]]:
        """Deferred production by cause (Pareto), with the wells concerned."""
        agg: dict = {}
        for day, well, st, cause, hh, oil, de, me in self._rows(d0.isoformat(), d1.isoformat()):
            if de > 0:
                a = agg.setdefault(cause, [0.0, set()])
                a[0] += de
                a[1].add(well)
        return sorted(((c, v[0], sorted(v[1])) for c, v in agg.items()), key=lambda x: -x[1])

    def shift_deferred(self, t0: float, t1: float) -> list[tuple[str, float, str]]:
        out: dict = {}
        with self.lock:
            for (hr, well, cause), m3 in self._hourly.items():
                if t0 - 3600 < hr < t1:
                    out[(well, cause)] = out.get((well, cause), 0.0) + m3
        return sorted(((w, m, c) for (w, c), m in out.items() if m >= 0.05), key=lambda x: -x[1])

    def csv(self, d0: date, d1: date) -> str:
        buf = io.StringIO()
        wr = csv.writer(buf, delimiter=";")
        wr.writerow(["day", "well", "state", "cause", "hours", "oil_m3", "deferred_m3", "measured_hours"])
        for r in self._rows(d0.isoformat(), d1.isoformat()):
            wr.writerow([r[0], r[1], r[2], r[3], f"{r[4]:.2f}", f"{r[5]:.2f}", f"{r[6]:.2f}", f"{r[7]:.2f}"])
        return buf.getvalue()

    # ------------------------------------------------------------------ report
    def report(self, d: date) -> tuple[str, str]:
        """Daily production report (the one typed every morning), as e-mail text."""
        fr = self.L == "fr"
        r = self.day(d)
        price = self.cfg.oil_price_usd
        subj = (f"Rapport de production {self.cfg.site_name} - {d:%d/%m/%Y}" if fr
                else f"Production report {self.cfg.site_name} - {d:%d/%m/%Y}")
        head = (f"{'Puits':<10} {'Prod. h':>7} {'Essai h':>7} {'Fermé h':>7} {'Sans données h':>14} {'Huile m³':>9} {'Différée m³':>11}  Cause"
                if fr else f"{'Well':<10} {'Prod h':>7} {'Test h':>7} {'Shut h':>7} {'No data h':>14} {'Oil m3':>9} {'Deferred m3':>11}  Cause")
        lines = [subj, "", head]
        est = False
        for w in r["wells"]:
            est = est or not w["measured"]
            cause = "; ".join(f"{c}" for c in w["causes"]) if w["causes"] else ""
            lines.append(f"{w['well']:<10} {w['flowing_h']:>7.1f} {w['test_h']:>7.1f} {w['shut_h']:>7.1f} {w['no_data_h']:>14.1f} "
                         f"{w['oil_m3']:>9.1f}{'' if w['measured'] else '*'}{w['defer_m3']:>11.1f}  {cause}")
        lines += ["", (f"Total huile : {r['oil_m3']:.1f} m³ ; production différée : {r['defer_m3']:.1f} m³ "
                       f"(environ {r['defer_m3'] * 6.29 * price:,.0f} USD à {price:.0f} USD/baril)." if fr else
                       f"Total oil: {r['oil_m3']:.1f} m3; deferred production: {r['defer_m3']:.1f} m3 "
                       f"(about {r['defer_m3'] * 6.29 * price:,.0f} USD at {price:.0f} USD/bbl).").replace(",", " ")]
        if r["no_data_h"] > 0.5:
            lines.append((f"Attention : {r['no_data_h']:.1f} heures-puits sans données (RTU muettes), production non comptée."
                          if fr else f"Note: {r['no_data_h']:.1f} well-hours without data (silent RTUs), production not counted."))
        cz = self.causes(d - timedelta(days=29), d)
        if cz:
            lines += ["", "PRODUCTION DIFFÉRÉE SUR 30 JOURS, PAR CAUSE" if fr else "DEFERRED PRODUCTION OVER 30 DAYS, BY CAUSE"]
            for c, m3, ws in cz[:8]:
                lines.append(f"- {m3:8.1f} m³  {c}  ({', '.join(ws)})")
        if est:
            lines += ["", "* estimation à partir du débit de référence (dernier essai validé, sinon débit appris ou potentiel) : "
                      "le puits n'a pas de débitmètre." if fr else
                      "* estimated from the reference rate (last validated test, else learned rate or potential): no flow meter."]
        return subj, "\n".join(lines)
