"""Simulated oil field, structured like a real radio-telemetry field (names invented).

    zone Z-NORD
      satellite station SAT-1  <- manifolds MAN-A, MAN-B
      satellite station SAT-2  <- manifold  MAN-C
    radio: one repeater per manifold group (R1, R2, R3)

Each well has the signals of a typical SCADA well screen: wellhead pressure and
temperature, line pressure after the choke, arrival pressure at the manifold,
annulus 7" and 9 5/8, gas lift rate / set-point / valve position, desalting
water, calculated oil rate, RTU power (battery, solar panel), cabinet
temperature, safety valve, fire, intrusion contacts, I/O cards, operator
command, well-test flag.

Used only for demonstrations and tests. One call to step() advances the field
by one minute. The agent is never told which incident is active.
"""

from __future__ import annotations

import math
import random

SATELLITES = {"SAT-1": ["MAN-A", "MAN-B"], "SAT-2": ["MAN-C"]}
MANIFOLDS = [
    {"id": "MAN-A", "rep": "R1", "sat": "SAT-1", "p_man": 11.8, "p_gl": 169.1},
    {"id": "MAN-B", "rep": "R2", "sat": "SAT-1", "p_man": 9.6, "p_gl": 152.3},
    {"id": "MAN-C", "rep": "R3", "sat": "SAT-2", "p_man": 12.9, "p_gl": 161.0},
]
WELLS = [
    {"id": "PUITS-01", "man": "MAN-A", "type": "gas_lift", "p": 22.8, "t": 35.9, "ann": 1.2, "ann7": 0, "q": 20000, "qo": 46, "maasp": 48},
    {"id": "PUITS-02", "man": "MAN-A", "type": "gas_lift", "p": 19.6, "t": 39.7, "ann": 0.8, "ann7": 0, "q": 22000, "qo": 38, "maasp": 48},
    {"id": "PUITS-03", "man": "MAN-A", "type": "natural", "p": 27.4, "t": 41.8, "ann": 2.1, "ann7": 1.4, "qo": 72, "maasp": 55},
    {"id": "PUITS-04", "man": "MAN-A", "type": "natural_water", "p": 26.1, "t": 38.4, "ann": 1.5, "ann7": 2.2, "qe": 32, "qo": 58, "maasp": 52},
    {"id": "PUITS-05", "man": "MAN-B", "type": "gas_lift", "p": 18.4, "t": 41.2, "ann": 0.6, "ann7": 0, "q": 40000, "qo": 64, "maasp": 45},
    {"id": "PUITS-06", "man": "MAN-B", "type": "gas_lift", "p": 17.9, "t": 37.5, "ann": 1.9, "ann7": 0, "q": 30000, "qo": 41, "maasp": 45},
    {"id": "PUITS-07", "man": "MAN-B", "type": "natural", "p": 24.9, "t": 44.1, "ann": 1.1, "ann7": 0.9, "qo": 55, "maasp": 52},
    {"id": "PUITS-08", "man": "MAN-B", "type": "natural_water", "p": 21.3, "t": 40.2, "ann": 2.4, "ann7": 1.8, "qe": 28, "qo": 33, "maasp": 50},
    {"id": "PUITS-09", "man": "MAN-C", "type": "gas_lift", "p": 24.3, "t": 33.5, "ann": 0.9, "ann7": 0, "q": 15000, "qo": 29, "maasp": 47},
    {"id": "PUITS-10", "man": "MAN-C", "type": "gas_lift", "p": 20.8, "t": 36.2, "ann": 1.3, "ann7": 0, "q": 25000, "qo": 44, "maasp": 47},
    {"id": "PUITS-11", "man": "MAN-C", "type": "natural", "p": 29.2, "t": 42.7, "ann": 1.7, "ann7": 2.6, "qo": 81, "maasp": 58},
    {"id": "PUITS-12", "man": "MAN-C", "type": "natural_water", "p": 23.6, "t": 39.1, "ann": 0.7, "ann7": 1.1, "qe": 36, "qo": 49, "maasp": 50},
]
WELL_INCIDENTS = ["chute", "duse", "ligne", "surp", "gl", "orifice", "eau", "ann", "instab", "capteur", "batterie", "solaire",
                  "rtu", "sdv", "commande", "feu", "intrusion", "porte", "carte", "test"]
GROUP_INCIDENTS = ["rep", "glnet", "collecte"]
SAT_INCIDENTS = ["satellite"]
CHOKE_FRAC = 0.62     # share of (wellhead - manifold) pressure lost across the choke
ANALOG_ON_CARD = ("ann", "ann_7", "gl_inj_p")   # transmitters wired to the I/O card that fails in the "carte" incident


def clamp01(x):
    return min(max(x, 0.0), 1.0)


class Field:
    def __init__(self, seed: int = 1, start_minute_of_day: int = 2 * 60 + 50):
        self.r = random.Random(seed)
        self.m = 0
        self.mod0 = start_minute_of_day
        self.mans = {}
        for d in MANIFOLDS:
            self.mans[d["id"]] = {"def": d, "rep": d["rep"], "sat": d["sat"], "base": {"p_man": d["p_man"], "p_gl": d["p_gl"]},
                                  "trend": {"p_man": d["p_man"], "p_gl": d["p_gl"]}, "cur": {"p_man": d["p_man"], "p_gl": d["p_gl"], "p_test": d["p_man"] * 0.85},
                                  "incident": None, "inc_t": 0}
        self.rep_down = {d["rep"]: False for d in MANIFOLDS}
        self.sat_incident = {s: None for s in SATELLITES}
        self.sat_t = {s: 0 for s in SATELLITES}
        self.wells = {}
        for i, d in enumerate(WELLS):
            M = self.mans[d["man"]]
            line = d_line = M["base"]["p_man"] + (1 - CHOKE_FRAC) * (d["p"] - M["base"]["p_man"])
            base = {"p": d["p"], "t": d["t"], "ann": d["ann"], "ann7": d["ann7"], "q": d.get("q", 0), "qe": d.get("qe", 0),
                    "pinj": M["base"]["p_gl"] - 2.1 - i * 0.1 if d["type"] == "gas_lift" else 0, "bat": 27.2, "qo": d["qo"], "line": line}
            self.wells[d["id"]] = {"def": d, "type": d["type"], "man": d["man"], "rep": M["rep"], "base": base,
                                   "trend": dict(base), "cur": dict(base, sol_i=0.0, sol_v=0.0, bat_i=1.5, temp_c=26.0, arr=M["base"]["p_man"]),
                                   "phase": i * 1.7, "period": 7 + (i % 5) * 1.4, "incident": None, "inc_t": 0, "starve": 0,
                                   "sdv": True, "fire": False, "io": True, "online": True, "contacts": {}, "remote": False, "on_test": False}
        for _ in range(60):
            self.step()
        self.m = 0

    def noise(self):
        r = self.r.random
        return (r() + r() + r() - 1.5) * 2

    def hour(self):
        return ((self.mod0 + self.m) // 60) % 24 + ((self.mod0 + self.m) % 60) / 60

    def sun(self):
        h = self.hour()
        return max(0.0, math.sin(math.pi * (h - 6.5) / 13)) if 6.5 < h < 19.5 else 0.0

    # ---- incident control --------------------------------------------------
    def set_well(self, wid, k):
        w = self.wells[wid]
        if k == "surp":
            k = "ligne"
        if k in ("gl", "orifice") and w["type"] != "gas_lift":
            raise ValueError("gas lift incident on a non gas lift well")
        if k == "eau" and w["type"] != "natural_water":
            raise ValueError("water incident on a well without desalting water")
        w["incident"] = k or None
        w["inc_t"] = self.m

    def set_group(self, man, k):
        M = self.mans[man]
        if not k:
            M["incident"] = None
            self.rep_down[M["rep"]] = False
        elif k == "rep":
            self.rep_down[M["rep"]] = True
        else:
            M["incident"] = k
            M["inc_t"] = self.m

    def set_satellite(self, sat, k):
        self.sat_incident[sat] = k or None
        self.sat_t[sat] = self.m

    def reset(self):
        for wid in self.wells:
            self.set_well(wid, None)
        for man in self.mans:
            self.set_group(man, None)
        for s in SATELLITES:
            self.set_satellite(s, None)

    def comm_down(self, w):
        return w["incident"] == "rtu" or self.rep_down[w["rep"]] or w["trend"]["bat"] < 22

    # ---- physics, one minute -------------------------------------------------
    def step(self):
        self.m += 1
        m = self.m
        sun = self.sun()
        for M in self.mans.values():
            e = m - M["inc_t"] if M["incident"] else 0
            pm, pg = M["base"]["p_man"], M["base"]["p_gl"]
            if M["incident"] == "collecte":
                pm *= 1 + 0.95 * clamp01(e / 12)
            s = M["sat"]
            if self.sat_incident[s] == "satellite":
                pm *= 1 + 0.8 * clamp01((m - self.sat_t[s]) / 10)
            if M["incident"] == "glnet":
                pg *= 1 - 0.7 * clamp01(e / 2)
            M["trend"]["p_man"] += (pm - M["trend"]["p_man"]) * 0.45
            M["trend"]["p_gl"] += (pg - M["trend"]["p_gl"]) * 0.5
            M["cur"]["p_man"] = M["trend"]["p_man"] + self.noise() * 0.08
            M["cur"]["p_gl"] = M["trend"]["p_gl"] + self.noise() * 0.4
            M["cur"]["p_test"] = M["trend"]["p_man"] * 0.85 + self.noise() * 0.08
        for w in self.wells.values():
            M, b, k = self.mans[w["man"]], w["base"], w["incident"]
            e = m - w["inc_t"] if k else 0
            sun_w = sun
            dman = M["trend"]["p_man"] - M["base"]["p_man"]
            t = {"p": b["p"], "t": b["t"], "ann": b["ann"], "ann7": b["ann7"], "q": b["q"], "qe": b["qe"], "pinj": b["pinj"],
                 "bat": 27.6 if sun_w > 0.15 else 26.6, "qo": b["qo"]}
            if w["type"] == "gas_lift":
                net = M["trend"]["p_gl"] / M["base"]["p_gl"]
                t["q"] = b["q"] * clamp01((net - 0.45) / 0.35)
                t["pinj"] = b["pinj"] * net
                t["ann7"] = t["pinj"] * 0.62            # gas lift wells inject down the 7" annulus
                if k == "gl":
                    t["q"] *= 1 - clamp01(e / 3)
                    t["pinj"] *= 1 - 0.6 * clamp01(e / 10)
                if k == "orifice":
                    t["q"] *= 1 - 0.42 * clamp01(e / 8)
                if w["cur"]["q"] < 0.3 * b["q"]:
                    w["starve"] += 1
                else:
                    w["starve"] = max(0, w["starve"] - 2)
                t["p"] *= 1 - 0.4 * clamp01((w["starve"] - 8) / 35)
                t["t"] -= 3 * clamp01((w["starve"] - 8) / 35)
            if k == "eau":
                t["qe"] = b["qe"] * (1 - clamp01(e / 2))
            if k == "chute":
                t["p"] *= 1 - 0.45 * clamp01(e / 20)
                t["t"] -= 4 * clamp01(e / 20)
            choke = CHOKE_FRAC
            line_extra = 0.0
            if k == "duse":                     # choke plugging: more pressure lost across the choke
                choke = CHOKE_FRAC + (0.93 - CHOKE_FRAC) * clamp01(e / 15)
                t["p"] *= 1 + 0.45 * clamp01(e / 15)
                t["t"] -= 2 * clamp01(e / 15)
            if k == "ligne":                    # restriction in the flowline, between choke and manifold
                t["p"] *= 1 + 0.6 * clamp01(e / 15)
                line_extra = 0.75
            if k == "ann":
                t["ann"] = b["ann"] + (w["def"]["maasp"] * 0.82 - b["ann"]) * clamp01(e / 18)
            if k == "batterie":
                t["bat"] = 27.2 - 6.5 * clamp01(e / 40)
            if k == "solaire":                  # no charging: the battery slowly runs down
                sun_w = 0.0
                t["bat"] = 26.6 - 4.8 * clamp01(e / 600)
            if k == "test":                     # well routed to the test separator
                t["p"] -= 0.15 * (M["base"]["p_man"]) + 0.8
            w["on_test"] = k == "test"
            w["remote"] = k == "commande" and e <= 3
            w["sdv"] = not (k == "sdv" or (k == "commande" and e >= 2))
            w["fire"] = k == "feu"
            w["io"] = k != "carte"
            w["contacts"] = {"pit_cover_open": k == "intrusion", "presence_5m": k == "intrusion",
                             "cabinet_door_open": k == "porte", "shelter_door_open": False, "concertina_alarm": False}
            if not w["sdv"]:
                t["p"] = b["p"] * (1 + 0.55 * clamp01(e / 12))
                t["q"] = 0
                t["qe"] = 0
                t["qo"] = 0
            t["p"] += dman
            pman_now = M["trend"]["p_man"] * (0.85 if w["on_test"] else 1.0)
            if w["sdv"]:
                t["line"] = pman_now + (1 - choke) * (t["p"] - pman_now) + line_extra * max(0.0, t["p"] - b["p"] - dman)
            else:
                t["line"] = pman_now
            for key in t:
                w["trend"][key] += (t[key] - w["trend"].get(key, t[key])) * 0.35
            w["online"] = not self.comm_down(w)
            if not w["online"]:
                continue
            tr = w["trend"]
            amp = 0.8 + 5.5 * clamp01(e / 10) if k == "instab" else 0.8
            per = 6 if k == "instab" else w["period"]
            c = w["cur"]
            wave = amp * math.sin(2 * math.pi * (m + w["phase"] * 3) / per)
            c["p"] = max(0.0, tr["p"] + wave + self.noise() * 0.25)
            c["line"] = max(0.0, tr["line"] + wave * (1 - CHOKE_FRAC) + self.noise() * 0.15)
            c["arr"] = M["cur"]["p_man"] + 0.25 + self.noise() * 0.06
            if k == "capteur":
                c["p"] = -0.18 + self.noise() * 0.004
            c["t"] = tr["t"] + self.noise() * 0.12
            c["ann"] = max(0.0, tr["ann"] + self.noise() * 0.06)
            c["ann7"] = max(0.0, tr["ann7"] + self.noise() * 0.06)
            c["q"] = max(0.0, tr["q"] + (self.noise() * 90 if tr["q"] > 500 else 0)) if w["type"] == "gas_lift" else 0
            c["qe"] = max(0.0, tr["qe"] + (self.noise() * 0.4 if tr["qe"] > 1 else 0)) if w["type"] == "natural_water" else 0
            c["pinj"] = tr["pinj"] + self.noise() * 0.5 if w["type"] == "gas_lift" else 0
            c["bat"] = tr["bat"] + self.noise() * 0.03
            # oil rate estimated from the flow through the choke
            pflow = M["base"]["p_man"] + 2
            f = clamp01((tr["p"] - pflow) / (b["p"] - pflow)) if w["sdv"] else 0.0
            if w["type"] == "gas_lift":
                f *= 0.35 + 0.65 * clamp01(tr["q"] / b["q"])
            if k == "duse":
                f *= 1 - 0.6 * clamp01(e / 15)
            if k == "ligne":
                f *= 1 - 0.5 * clamp01(e / 15)
            c["qo"] = max(0.0, b["qo"] * min(f, 1.05) + self.noise() * 0.3)
            c["sol_i"] = max(0.0, 4.2 * sun_w + self.noise() * 0.05) if sun_w > 0 else 0.0
            c["sol_v"] = 27.1 + self.noise() * 0.1 if self.sun() > 0.02 else 1.5
            c["bat_i"] = 1.6 - c["sol_i"] * 0.8 + self.noise() * 0.05
            c["temp_c"] = 24 + 22 * self.sun() + self.noise() * 0.3
            if k == "carte":                    # failed I/O card: the transmitters on it read impossible values
                c["ann"] = -0.3 + self.noise() * 0.004
                c["ann7"] = -0.2 + self.noise() * 0.004
                c["pinj"] = -0.25 + self.noise() * 0.004

    def values(self):
        """Current values per entity, as a SCADA would show them.
        Returns {entity: {signal: value}}, and the set of wells whose RTU is silent."""
        out, silent = {}, set()
        for wid, w in self.wells.items():
            c = w["cur"]
            v = {"whp": c["p"], "wht": c["t"], "line_p": c["line"], "arrival_p": c["arr"], "ann": c["ann"],
                 "battery": c["bat"], "battery_current": c["bat_i"], "solar_current": c["sol_i"], "solar_voltage": c["sol_v"],
                 "cabinet_temp": c["temp_c"], "oil_rate": c["qo"],
                 "sdv_closed": not w["sdv"], "fire": w["fire"], "intrusion": False, "io_fault": not w["io"],
                 "remote_cmd": w["remote"], "on_test": w["on_test"], "comm_ok": w["online"]}
            v.update(w["contacts"])
            if w["type"] != "gas_lift" or True:
                v["ann_7"] = c["ann7"]
            if w["type"] == "gas_lift":
                v["gl_flow"] = c["q"]
                v["gl_inj_p"] = c["pinj"]
                v["gl_setpoint"] = float(w["base"]["q"])
                v["gl_valve_pos"] = 100.0
                v["gl_mode_auto"] = True
            if w["type"] == "natural_water":
                v["water_flow"] = c["qe"]
            out[wid] = v
            if not w["online"]:
                silent.add(wid)
        for mid, M in self.mans.items():
            out[mid] = {"p_man": M["cur"]["p_man"], "p_gl": M["cur"]["p_gl"], "p_test": M["cur"]["p_test"]}
        return out, silent
