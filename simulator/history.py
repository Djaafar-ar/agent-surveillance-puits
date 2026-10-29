"""Generates months of daily history for the simulated field, with known
long-term problems planted in it, so that the long-term analysis can be
demonstrated and tested without waiting a year.

    python -m simulator.history --config config/config.sim.yaml --days 365

THIS IS SIMULATED DATA. Planted cases (what the analysis should find):
    PUITS-03  fast decline, accelerating over the last 75 days
    PUITS-04  salt deposits: sawtooth with washes
    PUITS-08  wellhead pressure transmitter drifting (pressure falls, temperature flat)
    PUITS-12  sudden pressure drop with no operation
    PUITS-01  choke progressively plugging (pressure lost across the choke rising)
    PUITS-11  9 5/8 annulus pressure slowly building up
    PUITS-06  RTU battery ageing (night-time minimum falling)
    PUITS-10  solar panel producing less (sand)
    PUITS-05  unreliable RTU radio link
    Repeater R2  failing repeatedly
    PUITS-09  instability alarm coming back again and again
Wells 02, 07 and 11 (pressure) are healthy, normal slow decline.
"""

from __future__ import annotations

import argparse
import math
import random
from datetime import date, datetime, timedelta

from agent import config as config_mod
from agent.store import Store

from .field_model import CHOKE_FRAC, MANIFOLDS, WELLS

DECL = {"PUITS-01": -0.35, "PUITS-02": -0.3, "PUITS-03": -0.6, "PUITS-04": -0.3, "PUITS-05": -0.25, "PUITS-06": -0.2,
        "PUITS-07": -0.45, "PUITS-08": -0.3, "PUITS-09": -0.3, "PUITS-10": -0.4, "PUITS-11": -0.1, "PUITS-12": -0.45}


def generate(store: Store, days: int = 365, end: date | None = None, seed: int = 7):
    r = random.Random(seed)
    g = lambda: (r.random() + r.random() + r.random() - 1.5) * 2
    end = end or (date.today() - timedelta(days=1))
    start = end - timedelta(days=days - 1)
    rows = []
    man_p = {m["id"]: m["p_man"] for m in MANIFOLDS}
    for i in range(days):
        d = start + timedelta(days=i)
        for m in MANIFOLDS:
            rows.append((d.isoformat(), m["id"], "p_man", m["p_man"] + g() * 0.1, m["p_man"] - 0.5, m["p_man"] + 0.5, 1440, 0))
            rows.append((d.isoformat(), m["id"], "p_gl", m["p_gl"] + g() * 0.5, m["p_gl"] - 2, m["p_gl"] + 2, 1440, 0))
    for wi, wd in enumerate(WELLS):
        wid = wd["id"]
        lvl, salt, drift = 0.0, 0.0, 0.0
        raw = []
        shut_days = set()
        for _ in range(2 + r.randrange(2)):
            s0 = r.randrange(20, days - 90) if days > 110 else r.randrange(days)
            for k in range(1 + r.randrange(3)):
                shut_days.add(s0 + k)
        washes = [days - 270, days - 160, days - 47] if wid == "PUITS-04" else []
        for k in range(days):
            rate = DECL[wid]
            if wid == "PUITS-03" and k >= days - 75:
                rate = -2.2
            lvl += rate / 30.4
            if wid == "PUITS-12" and k == days - 115:
                lvl -= 3.5
            if washes:
                salt = 0.0 if k in washes else max(salt - 0.12, -3.5)
            if wid == "PUITS-08" and k >= days - 45:
                drift -= 0.065
            raw.append((lvl + salt, lvl + salt + drift))
        end_real, end_meas = raw[-1]
        for k in range(days):
            d = (start + timedelta(days=k)).isoformat()
            real, meas = raw[k]
            p = wd["p"] + (meas - end_meas) + g() * 0.25
            t = wd["t"] + 0.25 * (real - end_real) + g() * 0.15
            shut = k in shut_days
            if shut:
                p += 8
            pman = man_p[wd["man"]]
            frac = CHOKE_FRAC
            if wid == "PUITS-01" and k >= days - 40:          # choke plugging over the last 40 days
                frac = CHOKE_FRAC + 0.25 * (k - (days - 40)) / 40
                p += 2.5 * (k - (days - 40)) / 40
            dp = frac * (p - pman) + g() * 0.15
            line = p - dp
            ann = wd["ann"] + g() * 0.05
            if wid == "PUITS-11" and k >= days - 60:          # annulus build-up
                ann += 0.065 * (k - (days - 60))
            ann7 = (wd.get("ann7") or 0) + g() * 0.05
            bat_min = 25.9 + g() * 0.05
            if wid == "PUITS-06" and k >= days - 45:           # battery ageing
                bat_min -= 0.04 * (k - (days - 45))
            sol = 4.2 + g() * 0.1
            if wid == "PUITS-10" and k >= days - 30:           # sand on the panel
                sol *= 1 - 0.45 * (k - (days - 30)) / 30
            online = 1.0
            if wid == "PUITS-05" and k >= days - 30 and r.random() < 0.3:
                online = 0.8
            oil = 0 if shut else max(0.0, wd["qo"] * (1 + 0.03 * (real - end_real)) + g() * 0.5)
            defer = wd["qo"] if shut else 0.0
            for sig, mean_, mn, mx in (("whp", p, p - 1, p + 1), ("wht", t, t - 0.3, t + 0.3), ("line_p", line, line - 0.5, line + 0.5),
                                       ("dp_choke", dp, dp - 0.4, dp + 0.4), ("ann", ann, ann - 0.1, ann + 0.1),
                                       ("ann_7", ann7, ann7 - 0.1, ann7 + 0.1), ("oil_rate", oil, oil * 0.95, oil * 1.05),
                                       ("battery", bat_min + 1.0, bat_min, 27.7), ("solar_current", sol * 0.4, 0.0, sol),
                                       ("cabinet_temp", 34, 22, 48 + g()), ("online", online, 0, 1),
                                       ("shut", 1.0 if shut else 0.0, 0, 1), ("test", 0.0, 0, 0)):
                rows.append((d, wid, sig, mean_, mn, mx, 1440, 0.0))
            rows.append((d, wid, "defer_m3", defer / 1440, 0, defer, 1440, defer))
    store.put_daily(rows)
    # alarm history for the radio and bad-actor analyses
    def alarm(i, ent, code, title, day_offset, hour):
        t0 = datetime.combine(end - timedelta(days=day_offset), datetime.min.time()).timestamp() + hour * 3600
        store.db.execute("INSERT OR REPLACE INTO alarms (id, run, entity, kind, code, severity, t0, t_end, title, value, diag, action, parent, notified) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (i, "history", ent, "well", code, "urgence", t0, t0 + 1800, title, "", "", "", None, "[]"))
    n = 0
    for k in (3, 9, 14, 20, 26, 28):
        n += 1; alarm(n, "PUITS-05", "RTU", "Perte de communication RTU", k, 2)
    for k in (5, 17, 24):
        n += 1; alarm(n, "Répéteur R2", "REP", "Panne probable du répéteur radio", k, 3)
    for k in range(1, 28, 3):
        n += 1; alarm(n, "PUITS-09", "INSTAB", "Écoulement instable", k, 4)
    store.db.commit()
    return start, end


def main():
    ap = argparse.ArgumentParser(description="Fill the agent database with simulated daily history (demonstration only)")
    ap.add_argument("--config", default="config/config.sim.yaml")
    ap.add_argument("--days", type=int, default=365)
    a = ap.parse_args()
    cfg = config_mod.load(a.config)
    st = Store(cfg.storage["path"])
    s, e = generate(st, a.days)
    print(f"Simulated history written: {s} to {e}, {len(cfg.wells)} wells -> {cfg.storage['path']}")


if __name__ == "__main__":
    main()
