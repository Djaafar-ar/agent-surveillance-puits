"""Well tests: which wells are overdue, a proposed schedule, and a check of each
result before it becomes the reference rate.

  running     detected from the test signal (on_test) or from the logbook status
              "on test"; start and end times are recorded automatically
  overdue     last test older than welltests.interval_days (30 by default)
  schedule    one test at a time per manifold (one test separator each), most
              overdue first, wells with a long-term finding (decline, deposits,
              step drop) moved up because their rate is probably wrong
  validation  a new result is compared with the previous validated test and with
              the wellhead pressure: a big change in oil with no change in
              pressure is suspicious ("retest advised"); the supervisor then
              validates or rejects it. Only validated tests become the reference
              rate used by the production report.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS well_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, well TEXT, manifold TEXT, start_t REAL, end_t REAL,
  status TEXT, oil_m3d REAL, water_m3d REAL, gas_m3d REAL, whp REAL, author TEXT, note TEXT, verdict TEXT, checked_by TEXT,
  checked_t REAL);
CREATE INDEX IF NOT EXISTS well_tests_w ON well_tests (well, start_t);
"""
COLS = ("id", "well", "manifold", "start_t", "end_t", "status", "oil_m3d", "water_m3d", "gas_m3d", "whp", "author", "note",
        "verdict", "checked_by", "checked_t")
# status: running -> ended (waiting for the result) -> to_validate -> validated | rejected
PRIORITY_KINDS = ("fast_decline", "decline", "salt_deposits", "step_drop", "choke_trend")


class WellTests:
    def __init__(self, cfg, store, engine=None, analysis=None):
        self.cfg, self.store, self.engine = cfg, store, engine
        self.analysis = analysis              # callable returning the current findings (dicts)
        self.c = cfg.welltests
        self.L = cfg.language
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def _q(self, sql, args=()):
        with self.store.lock:
            return [dict(zip(COLS, r)) for r in self.store.db.execute(sql, args)]

    def _x(self, sql, args=()):
        with self.store.lock:
            cur = self.store.db.execute(sql, args)
            self.store.db.commit()
            return cur.lastrowid

    def get(self, tid):
        r = self._q("SELECT * FROM well_tests WHERE id = ?", (tid,))
        return r[0] if r else None

    def list(self, well=None, limit=200):
        if well:
            return self._q("SELECT * FROM well_tests WHERE well = ? ORDER BY start_t DESC LIMIT ?", (well, limit))
        return self._q("SELECT * FROM well_tests ORDER BY start_t DESC LIMIT ?", (limit,))

    def running(self):
        return self._q("SELECT * FROM well_tests WHERE status = 'running' ORDER BY start_t")

    # ------------------------------------------------------------------ detection
    def step(self, now):
        if not self.engine:
            return
        run = {r["well"]: r for r in self.running()}
        for w in self.engine.wells.values():
            on = self.engine.on_test(w)
            if on and w.id not in run:
                self._x("INSERT INTO well_tests (well, manifold, start_t, status) VALUES (?,?,?, 'running')", (w.id, w.cfg.manifold, now))
            elif not on and w.id in run and w.fresh:
                self._x("UPDATE well_tests SET end_t = ?, status = 'ended' WHERE id = ?", (now, run[w.id]["id"]))

    # ------------------------------------------------------------------ results
    def record(self, well, oil_m3d, water_m3d=None, gas_m3d=None, whp=None, author="", note="", start_t=None, end_t=None, tid=None):
        """Enter a test result (from the test separator or the laboratory). Returns the test with its verdict."""
        if well not in {w.id for w in self.cfg.wells}:
            raise ValueError(f"unknown well '{well}'")
        oil = float(oil_m3d)
        if oil < 0:
            raise ValueError("oil rate must be positive")
        if tid is None:
            cand = self._q("SELECT * FROM well_tests WHERE well = ? AND status IN ('ended', 'running') ORDER BY start_t DESC LIMIT 1", (well,))
            tid = cand[0]["id"] if cand else None
        now = self.engine.now if self.engine else datetime.now().timestamp()
        if whp is None and self.engine:
            whp = self.engine.wells[well].cur.get("whp")
        if tid is None:
            m = next(w.manifold for w in self.cfg.wells if w.id == well)
            tid = self._x("INSERT INTO well_tests (well, manifold, start_t, end_t, status) VALUES (?,?,?,?, 'ended')",
                          (well, m, start_t or now - self.c["duration_hours"] * 3600, end_t or now))
        verdict = self._verdict(well, oil, water_m3d, whp, tid)
        self._x("UPDATE well_tests SET oil_m3d=?, water_m3d=?, gas_m3d=?, whp=?, author=?, note=?, verdict=?, status='to_validate', "
                "end_t = COALESCE(end_t, ?) WHERE id = ?",
                (oil, None if water_m3d in (None, "") else float(water_m3d), None if gas_m3d in (None, "") else float(gas_m3d),
                 None if whp is None else float(whp), author, note, verdict, now, tid))
        return self.get(tid)

    def _verdict(self, well, oil, water, whp, tid):
        fr = self.L == "fr"
        prev = self._q("SELECT * FROM well_tests WHERE well = ? AND status = 'validated' AND id != ? ORDER BY start_t DESC LIMIT 1", (well, tid))
        if not prev or not prev[0]["oil_m3d"]:
            return "Premier essai validé de ce puits : pas de comparaison possible." if fr else "First validated test of this well: nothing to compare."
        p = prev[0]
        d_oil = (oil - p["oil_m3d"]) / p["oil_m3d"] * 100
        d_whp = ((whp - p["whp"]) / p["whp"] * 100) if whp and p["whp"] else None
        lim = self.c["max_change_pct"]
        txt = (f"Huile {oil:.1f} m³/j contre {p['oil_m3d']:.1f} au dernier essai validé ({d_oil:+.0f} %)" if fr
               else f"Oil {oil:.1f} m3/d vs {p['oil_m3d']:.1f} at the last validated test ({d_oil:+.0f} %)")
        if d_whp is not None:
            txt += (f", pression tête {d_whp:+.0f} %" if fr else f", wellhead pressure {d_whp:+.0f} %")
        if abs(d_oil) <= lim:
            return txt + (". Cohérent." if fr else ". Consistent.")
        if d_whp is not None and abs(d_whp) < 5:
            return txt + (". Variation forte sans changement de pression tête : essai à refaire avant validation (séparateur, durée de stabilisation, comptage)."
                          if fr else ". Large change with no change in wellhead pressure: retest before validating (separator, stabilisation time, metering).")
        if d_whp is not None and (d_whp < 0) == (d_oil < 0):
            return txt + (". Variation forte mais dans le même sens que la pression tête : probablement réelle, à confirmer par l'ingénieur."
                          if fr else ". Large change in the same direction as the wellhead pressure: probably real, engineer to confirm.")
        return txt + (". Variation forte à expliquer avant validation." if fr else ". Large change to be explained before validating.")

    def check(self, tid, ok: bool, by: str):
        t = self.get(tid)
        if not t or t["status"] != "to_validate":
            raise ValueError("this test has no result waiting for validation")
        now = self.engine.now if self.engine else datetime.now().timestamp()
        self._x("UPDATE well_tests SET status = ?, checked_by = ?, checked_t = ? WHERE id = ?", ("validated" if ok else "rejected", by, now, tid))
        return self.get(tid)

    def reference(self, well):
        r = self._q("SELECT * FROM well_tests WHERE well = ? AND status = 'validated' AND oil_m3d IS NOT NULL ORDER BY start_t DESC LIMIT 1", (well,))
        return r[0]["oil_m3d"] if r else None

    # ------------------------------------------------------------------ planning
    def last_tests(self):
        rows = self._q("SELECT * FROM well_tests WHERE id IN (SELECT MAX(id) FROM well_tests GROUP BY well)")
        return {r["well"]: r for r in rows}

    def overdue(self, now=None):
        now = now or (self.engine.now if self.engine else datetime.now().timestamp())
        last = self.last_tests()
        out = []
        for w in self.cfg.wells:
            r = last.get(w.id)
            t = (r["end_t"] or r["start_t"]) if r else None
            age = None if t is None else (now - t) / 86400
            if age is None or age > self.c["interval_days"]:
                out.append({"well": w.id, "manifold": w.manifold, "last": t, "days": None if age is None else round(age),
                            "overdue_days": None if age is None else round(age - self.c["interval_days"])})
        return sorted(out, key=lambda x: -(x["days"] if x["days"] is not None else 10**6))

    def schedule(self, days=14, now=None):
        """Proposed test plan: one test at a time per manifold, most urgent first."""
        now = now or (self.engine.now if self.engine else datetime.now().timestamp())
        fr = self.L == "fr"
        last = self.last_tests()
        findings = {}
        try:
            for f in (self.analysis() if self.analysis else []):
                if f["kind"] in PRIORITY_KINDS:
                    findings.setdefault(f["entity"], f["title"])
        except Exception:  # noqa: BLE001
            pass
        running = {r["well"] for r in self.running()}
        cand = []
        for w in self.cfg.wells:
            if w.id in running:
                continue
            if self.engine and self.engine.wells[w.id].manual in ("shut", "work"):
                continue
            r = last.get(w.id)
            t = (r["end_t"] or r["start_t"]) if r else None
            age = 10**4 if t is None else (now - t) / 86400
            due_in = self.c["interval_days"] - age
            if w.id in findings:
                reason = (f"constat : {findings[w.id]}" if fr else f"finding: {findings[w.id]}")
                score = 1000 + age
            elif t is None:
                reason, score = ("jamais testé" if fr else "never tested"), 900 + 0
            elif due_in <= days:
                reason = (f"dernier essai il y a {age:.0f} jours" if fr else f"last test {age:.0f} days ago")
                score = age
            else:
                continue
            cand.append((score, w, reason))
        cand.sort(key=lambda x: -x[0])
        per_day = max(1, int(24 // max(1, self.c["duration_hours"])))
        slots: dict = {}
        out = []
        d0 = datetime.fromtimestamp(now).date() + timedelta(days=1)
        for score, w, reason in cand:
            m = w.manifold or w.id
            n = slots.get(m, 0)
            day = d0 + timedelta(days=n // per_day)
            if (day - d0).days >= days:
                continue
            slots[m] = n + 1
            hour = int(self.c["start_hour"]) + (n % per_day) * int(self.c["duration_hours"])
            out.append({"well": w.id, "manifold": m, "date": f"{day:%d/%m/%Y} {hour % 24:02d}:00", "reason": reason})
        return sorted(out, key=lambda x: datetime.strptime(x["date"], "%d/%m/%Y %H:%M"))
