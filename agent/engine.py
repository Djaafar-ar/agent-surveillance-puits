"""Alarm engine.

Every cycle the engine receives the latest readings, decides which alarms
are active, links consequences to their cause (ISA-18.2 "designed
suppression"), escalates unacknowledged emergencies along the on-call roster
and asks the dispatcher to send SMS and e-mails.

Detection is deterministic: fixed rules on thresholds, rates of change,
topology (which well is on which manifold and repeater) and time windows.
No language model ever decides whether something is an emergency.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .config import INTRUSION_SIGNALS
from .connectors.base import Sample
from .i18n import SIGLAB, fmt

log = logging.getLogger("agent.engine")
SEV = {"avert": 1, "urgence": 2}
ANALOG_SENSORS = ("ann", "ann_7", "gl_inj_p", "line_p", "arrival_p")   # besides whp, checked for impossible values
LEARNED = ("whp", "ann", "ann_7", "gl_flow", "water_flow", "wht", "line_p", "arrival_p", "oil_rate", "dp_choke", "dp_line")


def f1(v, d=1):
    return f"{v:.{d}f}"


def hhmm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def mean(a):
    a = list(a)
    return sum(a) / len(a) if a else 0.0


def std(a):
    a = list(a)
    if not a:
        return 0.0
    m = mean(a)
    return math.sqrt(mean((x - m) ** 2 for x in a))


@dataclass
class Alarm:
    id: int
    entity: str
    kind: str            # well | manifold | repeater | link
    code: str
    severity: str
    t0: float
    title: str = ""
    value: str = ""
    sms: str = ""
    diag: str = ""
    action: str = ""
    parent: "Alarm | None" = None
    children: list = field(default_factory=list)
    ack: tuple | None = None          # (contact index, time, via)
    level: int = 0
    last_sms: float | None = None
    notified: list = field(default_factory=list)
    exhausted: bool = False
    t_end: float | None = None
    snapshot: dict = field(default_factory=dict)

    def root(self):
        a = self
        while a.parent is not None:
            a = a.parent
        return a

    def to_dict(self):
        return {"id": self.id, "entity": self.entity, "kind": self.kind, "code": self.code, "severity": self.severity,
                "t0": self.t0, "t_end": self.t_end, "title": self.title, "value": self.value, "diag": self.diag,
                "action": self.action, "parent": self.parent.id if self.parent else None,
                "root_title": self.root().title, "children": [c.id for c in self.children],
                "ack": {"idx": self.ack[0], "t": self.ack[1], "via": self.ack[2]} if self.ack else None,
                "level": self.level, "notified": sorted(set(self.notified)), "exhausted": self.exhausted}


class Baseline:
    """Normal value of a signal: fixed from the configuration, or learned as the
    median of the recent samples taken while the equipment had no alarm."""

    def __init__(self, fixed: float | None, hours: int, learn_minutes: int):
        self.fixed = fixed
        self.hours = hours
        self.learn_s = learn_minutes * 60
        self.buf: deque = deque()
        self._last_t = -1e18
        self._cache = None

    def add(self, t: float, v: float):
        if self.fixed is not None or v is None:
            return
        if t - self._last_t < 60:       # one sample per minute is enough
            return
        self._last_t = t
        self.buf.append((t, float(v)))
        while self.buf and t - self.buf[0][0] > self.hours * 3600:
            self.buf.popleft()
        self._cache = None

    @property
    def ready(self) -> bool:
        if self.fixed is not None:
            return True
        return len(self.buf) >= 10 and self.buf[-1][0] - self.buf[0][0] >= self.learn_s

    @property
    def value(self) -> float | None:
        if self.fixed is not None:
            return self.fixed
        if not self.ready:
            return None
        if self._cache is None:
            self._cache = statistics.median(v for _, v in self.buf)
        return self._cache


class Entity:
    def __init__(self, kind, eid, label):
        self.kind, self.id, self.label = kind, eid, label
        self.alarms: dict[str, Alarm] = {}
        self.ok: dict[str, int] = {}
        self.cur: dict = {}
        self.cur_ts: dict = {}
        self.last_update = None
        self.fresh = False


class Engine:
    def __init__(self, cfg, dispatcher, store=None):
        self.cfg, self.out, self.store = cfg, dispatcher, store
        self.lang = cfg.language
        self.th = cfg.thresholds
        self.spm = cfg.samples_per_minute
        self.lock = threading.RLock()
        self.alarm_seq = 0
        self.alarms: list[Alarm] = []
        self.now = time.time()
        self.started = None
        self.link = Entity("link", "SCADA", "SCADA")
        self.link_last_ok = None
        self.link_err = ""
        self.wells: dict[str, Entity] = {}
        self.mans: dict[str, Entity] = {}
        self.reps: dict[str, Entity] = {}
        self.sats: dict[str, Entity] = {}
        self.resume: dict = {}          # alarms open when the agent stopped: (entity, code) -> row
        H = cfg.baseline_hours
        L = cfg.learn_minutes
        maxlen = int(240 * self.spm) + 5
        for m in cfg.manifolds:
            e = Entity("manifold", m.id, ("Manifold " + m.id))
            e.cfg = m
            e.hist = {"p_man": deque(maxlen=maxlen), "p_gl": deque(maxlen=maxlen)}
            e.base = {s: Baseline(m.normal.get(s), H, L) for s in ("p_man", "p_gl")}
            self.mans[m.id] = e
        for w in cfg.wells:
            e = Entity("well", w.id, w.id)
            e.cfg = w
            e.hist = {s: deque(maxlen=maxlen) for s in ("whp", "ann", "battery", "wht", "dp_choke", "dp_line")}
            e.base = {s: Baseline(w.normal.get(s), H, L) for s in LEARNED}
            e.defer_m3 = 0.0
            e.cnt = {}
            e.last_cmd = None
            e.manual = None           # status written in the logbook: shut | test | work (None = production)
            self.wells[w.id] = e
            if w.repeater not in self.reps:
                self.reps[w.repeater] = Entity("repeater", w.repeater, ("Répéteur " if self.lang == "fr" else "Repeater ") + w.repeater)
        self.rep_wells = {r: [w for w in self.wells.values() if w.cfg.repeater == r] for r in self.reps}
        for sid in cfg.satellites:
            e = Entity("satellite", sid, ("Satellite " if self.lang == "fr" else "Satellite station ") + sid)
            e.members = [m for m in self.mans.values() if m.cfg.satellite == sid]
            self.sats[sid] = e

    # ------------------------------------------------------------------ cycle
    def n(self, minutes: float) -> int:
        return max(1, int(round(minutes * self.spm)))

    def step(self, now: float, readings: dict[str, dict[str, Sample]], link_ok: bool, link_err: str = ""):
        with self.lock:
            self.now = now
            if self.started is None:
                self.started = now
            dt = 0 if not hasattr(self, "_prev") else max(0.0, now - self._prev)
            self._prev = now
            self.link_ok_now = link_ok
            if link_ok:
                self.link_last_ok = now
            self.link_err = link_err
            self._update(now, readings)
            self._eval_link(now)
            for r in self.reps.values():
                self._eval_rep(r)
            for st in self.sats.values():
                self._eval_sat(st)
            for m in self.mans.values():
                self._eval_man(m)
            for w in self.wells.values():
                self._eval_well(w)
                if (w.fresh and w.cur.get("sdv_closed") is True) or w.manual in ("shut", "work"):
                    rate = w.base["oil_rate"].value if w.base["oil_rate"].ready else w.cfg.potential_m3d
                    if rate:
                        w.defer_m3 += rate * dt / 86400
            self._escalate(now)

    # ------------------------------------------------------------------ logbook
    def on_test(self, w) -> bool:
        return bool(w.cur.get("on_test")) or w.manual == "test"

    def is_shut(self, w) -> bool:
        return bool(w.cur.get("sdv_closed")) or w.manual in ("shut", "work")

    def set_status(self, wid: str, status: str):
        """Well status written by an operator (sites without a valve or test signal):
        production | shut | test | work. Back to production starts the restart grace."""
        with self.lock:
            w = self.wells[wid]
            prev, w.manual = w.manual, (None if status in (None, "", "production") else status)
            if prev in ("shut", "work") and w.manual is None:
                w.grace_until = self.now + self.th["restart_grace_min"] * 60
            if prev == "test" and w.manual is None:
                w.test_until = self.now + self.th["test_grace_min"] * 60

    # ------------------------------------------------------------------ restart
    def export_baselines(self) -> dict:
        """Learned normal values, kept on disk so that a restart in the middle of an
        incident does not learn the incident as normal."""
        out = {}
        with self.lock:
            for kind, ents in (("well", self.wells), ("manifold", self.mans)):
                for e in ents.values():
                    for sig, b in e.base.items():
                        if b.fixed is None and b.buf:
                            pts = list(b.buf)
                            step = max(1, len(pts) // 120)
                            out.setdefault(e.id, {})[sig] = [(round(t), round(v, 4)) for t, v in pts[::step]] + ([pts[-1]] if (len(pts) - 1) % step else [])
        return out

    def import_baselines(self, data: dict):
        with self.lock:
            for eid, sigs in (data or {}).items():
                e = self.wells.get(eid) or self.mans.get(eid)
                if e is None:
                    continue
                for sig, pts in sigs.items():
                    b = e.base.get(sig)
                    if b is not None and b.fixed is None:
                        b.buf.clear()
                        b.buf.extend((float(t), float(v)) for t, v in pts)
                        b._last_t = b.buf[-1][0] if b.buf else -1e18
                        b._cache = None

    RELEARN = ("DUSE", "LIGNE", "P_CHUTE", "P_HAUTE", "INSTAB", "GL_ECART", "EAU_ARRET")

    def relearn(self, wid: str, note: str = "") -> list[str]:
        """After an intervention (wash, choke change, repair) the old normal values are no
        longer valid: forget them, close the alarms the intervention explains, learn again."""
        closed = []
        with self.lock:
            w = self.wells.get(wid)
            if w is None:
                return closed
            for b in w.base.values():
                if b.fixed is None:
                    b.buf.clear()
                    b._cache = None
            for code in self.RELEARN:
                a = w.alarms.get(code)
                if a is not None:
                    self._clear(w, a)
                    closed.append(code)
            self.out.journal("info", wid, fmt(self.lang, "relearn") + (f" ({note})" if note else ""))
        return closed

    def _fresh(self, s: Sample | None) -> bool:
        stale = max(2.5 * self.cfg.poll_seconds, 150)
        return s is not None and s.good and s.value is not None and (self.now - s.ts) <= stale

    def _update(self, now, readings):
        for m in self.mans.values():
            r = readings.get(m.id, {})
            m.fresh = self._fresh(r.get("p_man")) or self._fresh(r.get("p_gl"))
            if self._fresh(r.get("p_test")):
                m.cur["p_test"] = r["p_test"].value
            for s in ("p_man", "p_gl"):
                if self._fresh(r.get(s)):
                    m.cur[s] = r[s].value
                    m.hist[s].append(r[s].value)
                    if not m.alarms:
                        m.base[s].add(now, r[s].value)
            if m.fresh:
                m.last_update = now
        for w in self.wells.values():
            r = readings.get(w.id, {})
            comm = r.get("comm_ok")
            comm_ok = comm is None or not comm.good or bool(comm.value) or comm.value is None
            whp = r.get("whp")
            # The RTU is "talking" if its pressure sample is recent, whatever its quality
            talking = whp is not None and (now - whp.ts) <= max(2.5 * self.cfg.poll_seconds, 150)
            w.fresh = comm_ok and talking
            w.whp_bad = w.fresh and (not whp.good or whp.value is None)
            if not w.fresh:
                continue
            w.last_update = min(now, whp.ts) if whp.ts else now
            for s, smp in r.items():
                if smp is not None and smp.value is not None and (now - smp.ts) <= max(2.5 * self.cfg.poll_seconds, 150):
                    w.cur[s] = smp.value
                    w.cur_ts[s] = smp.ts
            if w.whp_bad:
                w.cur["whp"] = whp.value if whp.value is not None else float("nan")
            v = w.cur.get("whp")
            w.hist["whp"].append(v if v is not None and not w.whp_bad else -1.0)
            # pressure lost across the choke and along the flowline (needs the line pressure)
            M = self.mans.get(w.cfg.manifold)
            lp = w.cur.get("line_p") if "line_p" in r else None
            down = w.cur.get("arrival_p") if "arrival_p" in r else (M.cur.get("p_man") if M else None)
            if lp is not None and v is not None and not w.whp_bad and lp >= 0 and v >= 0:
                w.cur["dp_choke"] = v - lp
                w.hist["dp_choke"].append(v - lp)
                if down is not None and down >= 0:
                    w.cur["dp_line"] = lp - down
                    w.hist["dp_line"].append(lp - down)
            for s in ("ann", "battery", "wht"):
                if s in w.cur:
                    w.hist[s].append(w.cur[s])
            if w.cur.get("remote_cmd"):
                w.last_cmd = now
            if self.on_test(w):
                w.test_until = now + self.th["test_grace_min"] * 60
            quiet = (not w.alarms and not (M.alarms if M else {}) and not self.on_test(w) and not w.manual
                     and now >= getattr(w, "grace_until", 0) and now >= getattr(w, "test_until", 0))
            if quiet and not w.whp_bad:
                for s in LEARNED:
                    val = w.cur.get(s)
                    if isinstance(val, (int, float)) and not isinstance(val, bool) and (val >= 0 or s.startswith("dp_")):
                        w.base[s].add(now, val)

    def age_min(self, e: Entity) -> float:
        ref = e.last_update if e.last_update is not None else (self.started if self.started is not None else self.now)
        return (self.now - ref) / 60.0

    # ------------------------------------------------------------------ rules
    def rule(self, T: Entity, code, active, sev, clear, need=3, ctx=None):
        a = T.alarms.get(code)
        if active:
            T.ok[code] = 0
            if a is None:
                self._new(T, code, sev, ctx)
            elif SEV[sev] > SEV[a.severity]:
                self._upgrade(T, a, sev, ctx)
            elif code == "REP" and ctx and len(ctx) != a.snapshot.get("n"):
                i = self.info(T, code, ctx)
                a.value = i["value"]
                a.snapshot["n"] = len(ctx)
        elif a is not None:
            if clear:
                T.ok[code] = T.ok.get(code, 0) + 1
                if T.ok[code] >= need:
                    self._clear(T, a)
            else:
                T.ok[code] = 0

    def _eval_link(self, now):
        ref = self.link_last_ok if self.link_last_ok is not None else self.started
        age = (now - ref) / 60
        lim = self.th["rtu_timeout_min"]
        self.rule(self.link, "SCADA_LINK", age >= lim, "urgence", age == 0, need=2)

    def _eval_rep(self, R: Entity):
        ws = self.rep_wells[R.id]
        if len(ws) < 2 or self.link.alarms or not self.link_ok_now:
            return      # without SCADA data nothing can be said about the radio network
        silent = [w for w in ws if self.age_min(w) >= self.th["rtu_timeout_min"]]
        self.rule(R, "REP", len(silent) >= 2, "urgence", len(silent) == 0, 3, silent)

    def _man_high(self, M: Entity) -> bool:
        pm = M.base["p_man"].value
        if not (M.fresh and pm and len(M.hist["p_man"])):
            return False
        return mean(list(M.hist["p_man"])[-self.n(3):]) > (1 + self.th["manifold_high_pct"] / 100) * pm

    def _eval_sat(self, S: Entity):
        """Several manifolds of the same satellite station rising together: the
        problem is at or after the station, not in each manifold."""
        if len(S.members) < 2:
            return
        high = [m for m in S.members if self._man_high(m)]
        self.rule(S, "SAT_HAUTE", len(high) >= 2, "urgence", len(high) == 0, 3, high)

    def _eval_man(self, M: Entity):
        if not M.fresh:
            return
        pm, pg = M.base["p_man"].value, M.base["p_gl"].value
        if pm and len(M.hist["p_man"]):
            recent = mean(list(M.hist["p_man"])[-self.n(3):])
            self.rule(M, "MAN_HAUTE", recent > (1 + self.th["manifold_high_pct"] / 100) * pm, "urgence", recent < 1.1 * pm)
        if pg and "p_gl" in M.cur:
            self.rule(M, "GLNET", M.cur["p_gl"] < (1 - self.th["gl_network_low_pct"] / 100) * pg, "urgence", M.cur["p_gl"] > 0.85 * pg)

    def _eval_well(self, w: Entity):
        th, c, cfg = self.th, w.cur, w.cfg
        age = self.age_min(w)
        fresh = w.fresh
        if "battery" in cfg.tags:
            b = c.get("battery")
            self.rule(w, "BATT", fresh and b is not None and b < th["battery_low_v"], "avert",
                      fresh and b is not None and b > th["battery_ok_v"])
        urg = max(th["rtu_urgent_min"], th["rtu_timeout_min"] + 10)
        if self.link_ok_now or self.link.alarms:   # SCADA link down: wait for SCADA_LINK, then group under it
            self.rule(w, "RTU", age >= th["rtu_timeout_min"], "urgence" if age >= urg else "avert", fresh)
        if not fresh:
            return
        # the I/O card first, so that the transmitters it makes unreliable are grouped under it
        if "io_fault" in cfg.tags and "io_fault" in c:
            v = bool(c["io_fault"])
            self.rule(w, "CARTE", v, "avert", not v)
        p = c.get("whp")
        bad = w.whp_bad or p is None or (isinstance(p, float) and math.isnan(p)) or p < th["sensor_min_barg"] or p > th["sensor_max_barg"]
        self.rule(w, "CAPTEUR", bad, "avert", not bad and p > 0.5, 6)
        faulty = set()
        for sig in ANALOG_SENSORS:
            if sig in cfg.tags and isinstance(c.get(sig), (int, float)):
                b_ = c[sig] < th["sensor_min_barg"] or c[sig] > th["sensor_max_barg"]
                if b_:
                    faulty.add(sig)
                self.rule(w, "CAPTEUR_" + sig.upper(), b_, "avert", not b_ and c[sig] >= 0, 6)
        if w.alarms.get("SDV") is None:
            cmd = w.last_cmd is not None and self.now - w.last_cmd <= th["command_window_min"] * 60
        else:
            cmd = bool(w.alarms["SDV"].snapshot.get("cmd"))
        for code, sig in (("SDV", "sdv_closed"), ("FEU", "fire")):
            if sig in cfg.tags and sig in c:
                v = bool(c[sig])
                self.rule(w, code, v, "avert" if (code == "SDV" and cmd) else "urgence", not v)
        contacts = [s_ for s_ in INTRUSION_SIGNALS if s_ in cfg.tags and c.get(s_) is True]
        if any(s_ in cfg.tags for s_ in INTRUSION_SIGNALS):
            self.rule(w, "INTRUS", bool(contacts), "urgence", not contacts, 3, contacts)
        for code, sig in (("ANN", "ann"), ("ANN7", "ann_7")):
            val = c.get(sig)
            if val is None or sig not in cfg.tags or sig in faulty or (code == "ANN7" and cfg.type == "gas_lift"):
                continue      # in gas lift wells the 7" annulus carries the injection gas: high pressure is normal
            warn = th["annulus_warn_barg"]
            maasp = cfg.maasp
            sev = "urgence" if maasp and val > th["annulus_urgent_pct_maasp"] / 100 * maasp else "avert"
            self.rule(w, code, val > warn, sev, val < 0.85 * warn)
        if cfg.type == "gas_lift" and "gl_flow" in c:
            sp = c.get("gl_setpoint")
            qn = sp if isinstance(sp, (int, float)) and not isinstance(sp, bool) and sp > 0 else (w.base["gl_flow"].value if w.base["gl_flow"].ready else None)
            if qn:
                q = c["gl_flow"]
                self.rule(w, "GL_ARRET", q < th["gl_low_pct"] / 100 * qn, "urgence", q > 0.8 * qn)
                valve = c.get("gl_valve_pos", 100.0 if "gl_valve_pos" not in cfg.tags else None)
                low = valve is not None and valve >= 90 and th["gl_low_pct"] / 100 * qn <= q < th["gl_below_setpoint_pct"] / 100 * qn
                w.cnt["gl"] = w.cnt.get("gl", 0) + 1 if low else 0
                self.rule(w, "GL_ECART", w.cnt["gl"] >= self.n(10), "avert", q > 0.85 * qn)
        if "solar_current" in cfg.tags and isinstance(c.get("solar_current"), (int, float)):
            hour = datetime.fromtimestamp(self.now).hour
            sc = c["solar_current"]
            if 10 <= hour < 15 and sc < 0.1:
                w.cnt["sol"] = w.cnt.get("sol", 0) + 1
            elif sc > 0.5:
                w.cnt["sol"] = 0
            self.rule(w, "SOLAIRE", w.cnt.get("sol", 0) >= self.n(th["solar_fault_min"]), "avert", sc > 0.5)
        if "cabinet_temp" in cfg.tags and isinstance(c.get("cabinet_temp"), (int, float)):
            tc = c["cabinet_temp"]
            self.rule(w, "TEMP_COFFRET", tc > th["cabinet_temp_max"], "avert", tc < th["cabinet_temp_max"] - 5)
        if cfg.type == "natural_water" and "water_flow" in c and w.base["water_flow"].ready:
            qn = w.base["water_flow"].value
            a = w.alarms.get("EAU_ARRET")
            sev = "urgence" if a and self.now - a.t0 >= th["water_urgent_min"] * 60 else "avert"
            self.rule(w, "EAU_ARRET", c["water_flow"] < th["water_low_pct"] / 100 * qn, sev, c["water_flow"] > 0.8 * qn)
        h = list(w.hist["whp"])
        if w.alarms.get("CAPTEUR") or any(v < 0 for v in h[-self.n(21):]) or not w.base["whp"].ready:
            return
        pn = w.base["whp"].value
        k = max(1, int(round(self.spm)))
        recent = mean(h[-self.n(5):])
        past = mean(h[-self.n(40):-self.n(30)]) if len(h) >= self.n(40) else pn
        mins = h[::-1][::k][::-1][-21:]          # one value per minute, last 21 minutes
        diffs = [b - a for a, b in zip(mins, mins[1:])]
        flips, last = 0, 0
        for dv in diffs:
            if abs(dv) < 1.2:
                continue
            sg = 1 if dv > 0 else -1
            if last and sg != last:
                flips += 1
            last = sg
        w20 = h[-self.n(20):]
        w.metrics = {"recent": recent, "past": past, "dstd": std(diffs), "flips": flips, "amp": (max(w20) - min(w20)) / 2}
        d, s = th["pressure_drop_pct"] / 100, th["pressure_high_pct"] / 100
        # After a shut-in the well restarts (unloading): low or unstable pressure is
        # expected for a while and must not raise new alarms.
        if w.manual in ("shut", "work"):
            return          # shut in or under intervention (logbook): pressures are not production pressures
        grace = self.now < max(getattr(w, "grace_until", 0), getattr(w, "test_until", 0)) or self.on_test(w)
        shut = bool(c.get("sdv_closed")) or w.alarms.get("SDV") is not None
        # choke plugging / flowline restriction, from the pressure lost across the choke and along the line
        if "line_p" not in faulty and len(w.hist["dp_choke"]) >= self.n(5):
            for code, sig, pct in (("DUSE", "dp_choke", th["choke_dp_rise_pct"]), ("LIGNE", "dp_line", th["line_dp_rise_pct"])):
                bl = w.base[sig]
                if not bl.ready or len(w.hist[sig]) < self.n(5):
                    continue
                rc = mean(list(w.hist[sig])[-self.n(5):])
                base_ = max(bl.value, 0.5)
                M = self.mans.get(cfg.manifold)
                upstream_ok = not (M and M.alarms.get("MAN_HAUTE"))
                act = not grace and not shut and upstream_ok and rc > base_ * (1 + pct / 100) and rc - base_ > 3
                self.rule(w, code, act, "urgence", rc < base_ * (1 + pct * 0.4 / 100) or grace or shut)
        self.rule(w, "P_CHUTE", not grace and ((recent < (1 - d) * past and recent < 0.9 * pn) or recent < (0.9 - d) * pn),
                  "urgence", recent >= (1 - d * 0.75) * pn or grace)
        self.rule(w, "P_HAUTE", not grace and recent > (1 + s) * pn, "urgence", recent < (1 + s * 0.4) * pn or grace)
        self.rule(w, "INSTAB", not grace and w.metrics["dstd"] > 2 and flips >= 4, "avert",
                  w.metrics["dstd"] < 1 or flips < 2 or grace)

    def parent_of(self, T: Entity, code: str):
        if code == "RTU" and self.link.alarms.get("SCADA_LINK"):
            return self.link.alarms["SCADA_LINK"]
        if T.kind == "manifold" and code == "MAN_HAUTE":
            S = self.sats.get(T.cfg.satellite)
            return S.alarms.get("SAT_HAUTE") if S else None
        if T.kind != "well":
            return None
        M = self.mans.get(T.cfg.manifold)
        R = self.reps.get(T.cfg.repeater)
        ma = M.alarms if M else {}
        if code == "RTU":
            return R.alarms.get("REP") if R else None
        if code == "GL_ARRET":
            return T.alarms.get("SDV") or ma.get("GLNET")
        if code == "EAU_ARRET":
            return T.alarms.get("SDV")
        if code == "P_HAUTE":
            return T.alarms.get("SDV") or T.alarms.get("DUSE") or T.alarms.get("LIGNE") or ma.get("MAN_HAUTE")
        if code == "CAPTEUR" or code.startswith("CAPTEUR_"):
            return T.alarms.get("CARTE")
        if code == "GL_ECART":
            return T.alarms.get("SDV") or ma.get("GLNET") or T.alarms.get("GL_ARRET")
        if code == "BATT":
            return T.alarms.get("SOLAIRE")
        if code == "P_CHUTE":
            return T.alarms.get("SDV") or T.alarms.get("GL_ARRET") or ma.get("GLNET")
        if code == "SDV":
            return T.alarms.get("FEU")
        return None

    # ------------------------------------------------------------------ texts
    def info(self, T: Entity, code: str, ctx=None) -> dict:
        L = self.lang
        if code == "SCADA_LINK":
            ref = self.link_last_ok if self.link_last_ok is not None else self.started
            return fmt(L, code, age=int((self.now - ref) / 60), err=self.link_err or "?")
        if code == "REP":
            ws = ctx if ctx is not None else [w for w in self.rep_wells[T.id] if self.age_min(w) >= self.th["rtu_timeout_min"]]
            ids = ", ".join(w.id for w in ws)
            t0 = min((w.last_update or self.now) for w in ws) if ws else self.now
            return fmt(L, code, n=len(ws), ids=ids, id=T.id, t0=hhmm(t0))
        if code == "SAT_HAUTE":
            ms = ctx if ctx is not None else [m for m in T.members if self._man_high(m)]
            return fmt(L, code, id=T.id, n=len(ms), ids=", ".join(m.id for m in ms))
        if code in ("MAN_HAUTE", "GLNET"):
            sig = "p_man" if code == "MAN_HAUTE" else "p_gl"
            pn = T.base[sig].value or 0
            p = mean(list(T.hist[sig])[-self.n(3):]) if T.hist[sig] else 0
            gl = ", ".join(w.id for w in self.wells.values() if w.cfg.manifold == T.id and w.cfg.type == "gas_lift")
            return fmt(L, code, id=T.id, p=f1(p), pn=f1(pn), pct=round((p / pn - 1) * 100) if pn else 0, ids=gl or "-")
        w, c, cfg = T, T.cur, T.cfg
        M = self.mans.get(cfg.manifold)
        ma = M.alarms if M else {}
        pn = w.base["whp"].value or 0
        m = getattr(w, "metrics", None) or {"recent": c.get("whp", 0) or 0, "amp": 0}
        p = m["recent"]
        if code == "P_CHUTE":
            pct = round((1 - p / pn) * 100) if pn else 0
            gl = w.alarms.get("GL_ARRET") or ma.get("GLNET")
            if gl:
                return fmt(L, "P_CHUTE_gl", p=f1(p), pn=f1(pn), pct=pct, root=gl.root().title)
            if cfg.type == "natural_water" and w.alarms.get("EAU_ARRET"):
                return fmt(L, "P_CHUTE_eau", p=f1(p), pn=f1(pn), pct=pct)
            tn = w.base["wht"].value
            tdrop = fmt(L, "tdrop") if tn and c.get("wht") is not None and c["wht"] < tn - 1 else ""
            ctx_ = fmt(L, "ctx_gl" if cfg.type == "gas_lift" else "ctx_man")
            return fmt(L, code, p=f1(p), pn=f1(pn), pct=pct, ctx=ctx_, tdrop=tdrop)
        if code == "P_HAUTE":
            pct = round((p / pn - 1) * 100) if pn else 0
            if ma.get("MAN_HAUTE"):
                return fmt(L, "P_HAUTE_man", p=f1(p), pn=f1(pn), pct=pct, man=cfg.manifold)
            return fmt(L, code, p=f1(p), pn=f1(pn), pct=pct)
        if code == "GL_ARRET":
            q = c.get("gl_flow", 0)
            sp = c.get("gl_setpoint")
            qn = sp if isinstance(sp, (int, float)) and sp and not isinstance(sp, bool) else (w.base["gl_flow"].value or 0)
            key = "GL_ARRET_net" if ma.get("GLNET") else code
            return fmt(L, key, q=f1(q, 0), qn=f1(qn, 0), man=cfg.manifold)
        if code == "EAU_ARRET":
            return fmt(L, code, q=f1(c.get("water_flow", 0)), qn=f1(w.base["water_flow"].value or 0), m=int(self.th["water_urgent_min"]))
        if code == "ANN":
            ann = c.get("ann", 0)
            maasp = cfg.maasp or 0
            return fmt(L, code, p=f1(ann), pn=f1(w.base["ann"].value or 0), th=f1(self.th["annulus_warn_barg"]),
                       pct=round(ann / maasp * 100) if maasp else "?", maasp=f1(maasp, 0))
        if code == "SDV":
            a = w.alarms.get("SDV")
            cmd = (a.snapshot.get("cmd") if a else (w.last_cmd is not None and self.now - w.last_cmd <= self.th["command_window_min"] * 60))
            return fmt(L, "SDV_cmd" if cmd else "SDV", id=w.id, t=hhmm(w.last_cmd) if w.last_cmd else "?")
        if code in ("FEU", "CARTE"):
            return fmt(L, code, id=w.id)
        if code.startswith("CAPTEUR_"):
            sig = code[8:].lower()
            v = c.get(sig)
            return fmt(L, "SENSOR", sig=SIGLAB[L].get(sig, sig), p="?" if v is None else f1(v, 2), id=w.id)
        if code in ("DUSE", "LIGNE"):
            dc, dl = c.get("dp_choke", 0), c.get("dp_line", 0)
            return fmt(L, code, p=f1(c.get("whp", 0)), pn=f1(pn), lp=f1(c.get("line_p", 0)), lpn=f1(w.base["line_p"].value or 0),
                       dc=f1(dc), dcn=f1(w.base["dp_choke"].value or 0), dl=f1(dl), dln=f1(w.base["dp_line"].value or 0),
                       q=f1(c.get("oil_rate", 0)), qn=f1(w.base["oil_rate"].value or 0))
        if code == "ANN7":
            v = c.get("ann_7", 0)
            maasp = cfg.maasp or 0
            return fmt(L, code, p=f1(v), pn=f1(w.base["ann_7"].value or 0), th=f1(self.th["annulus_warn_barg"]),
                       pct=round(v / maasp * 100) if maasp else "?", maasp=f1(maasp, 0))
        if code == "GL_ECART":
            sp = c.get("gl_setpoint") or w.base["gl_flow"].value or 0
            return fmt(L, code, q=f1(c.get("gl_flow", 0), 0), sp=f1(sp, 0), pos=f1(c.get("gl_valve_pos", 100), 0),
                       pct=round(c.get("gl_flow", 0) / sp * 100) if sp else "?")
        if code == "SOLAIRE":
            return fmt(L, code, i=f1(c.get("solar_current", 0), 2), v=f1(c.get("solar_voltage", 0)), b=f1(c.get("battery", 0), 2),
                       m=int(self.th["solar_fault_min"]))
        if code == "TEMP_COFFRET":
            return fmt(L, code, t=f1(c.get("cabinet_temp", 0)), th=f1(self.th["cabinet_temp_max"], 0))
        if code == "INTRUS":
            ct = ctx if ctx is not None else [s_ for s_ in INTRUSION_SIGNALS if c.get(s_) is True]
            names = ", ".join(SIGLAB[L].get(s_, s_) for s_ in ct) or SIGLAB[L]["intrusion"]
            return fmt(L, code, id=w.id, t=hhmm(self.now), what=names)
        if code == "INSTAB":
            return fmt(L, code, amp=f1(m.get("amp", 0)))
        if code == "CAPTEUR":
            v = c.get("whp")
            return fmt(L, code, p="?" if v is None or (isinstance(v, float) and math.isnan(v)) else f1(v, 2))
        if code == "BATT":
            hb = list(w.hist["battery"])
            k = min(self.n(10), len(hb) - 1)
            rate = (hb[-1 - k] - hb[-1]) / (k / self.spm) if k > 0 else 0
            v = c.get("battery", 0)
            eta = ""
            rate_s = ""
            if rate > 0.02:
                mins = max(1, round((v - 22) / rate))
                eta = fmt(L, "eta", t=hhmm(self.now + mins * 60))
                rate_s = f", -{f1(rate, 2)} V/min"
            return fmt(L, code, v=f1(v, 2), rate=rate_s, eta=eta)
        if code == "RTU":
            age = int(self.age_min(w))
            if self.link.alarms.get("SCADA_LINK"):
                return fmt(L, "RTU_link", age=age)
            R = self.reps.get(cfg.repeater)
            if R and R.alarms.get("REP"):
                return fmt(L, "RTU_rep", age=age, rep=cfg.repeater)
            b = c.get("battery")
            bat = fmt(L, "bat_cause", v=f1(b)) if b is not None and b < self.th["battery_low_v"] else ""
            return fmt(L, code, id=w.id, age=age, bat=bat)
        return {"title": code, "value": "", "sms": "", "diag": "", "action": ""}

    # ------------------------------------------------------------------ actions
    def _new(self, T: Entity, code, sev, ctx):
        i = self.info(T, code, ctx)
        self.alarm_seq += 1
        a = Alarm(id=self.alarm_seq, entity=T.label, kind=T.kind, code=code, severity=sev, t0=self.now, **i)
        a.parent = self.parent_of(T, code)
        a.snapshot = dict(T.cur) if T.kind == "well" else {}
        if code == "REP":
            a.snapshot["n"] = len(ctx or [])
        if code == "SDV" and sev == "avert":
            a.snapshot["cmd"] = True
        T.alarms[code] = a
        self.alarms.append(a)
        r = self.resume.pop((a.entity, code), None) if self.resume else None
        if r is not None:
            # same alarm as before the agent restarted: keep its start, acknowledgement and
            # escalation state, and do not page anyone again
            a.t0, a.level, a.notified = r["t0"], r["level"] or 0, list(r["notified"])
            a.ack = (r["ack_idx"], r["ack_t"], r["ack_via"]) if r["ack_t"] else None
            a.last_sms = self.now if a.notified else None
            self._persist(a)
            if self.store:
                self.store.drop_alarm_row(r["run"], r["id"])   # the new row carries it on
            if a.parent is not None:
                a.parent.children.append(a)
            self.out.journal("info", a.entity, f"{a.title}: {fmt(self.lang, 'resumed')}")
            return
        if code == "CARTE":       # transmitter faults already raised on this well: same cause
            self._adopt(a, [x for k_, x in T.alarms.items() if k_ == "CAPTEUR" or k_.startswith("CAPTEUR_")])
        if code == "SAT_HAUTE":   # manifold alarms already raised: same cause
            self._adopt(a, [m.alarms["MAN_HAUTE"] for m in T.members if "MAN_HAUTE" in m.alarms])
        self._persist(a)
        if a.parent is not None:
            a.parent.children.append(a)
            q = ("« ", " »") if self.lang == "fr" else ("\"", "\"")
            self.out.journal("group", a.entity, f"{a.title}: {a.value}. → {q[0]}{a.parent.title}{q[1]} ({a.parent.entity}).")
            return
        self.out.journal("detect", a.entity, f"{a.title}: {a.value}.")
        if code == "FEU":
            for idx in range(len(self.cfg.roster)):
                self._sms(a, idx, "alarm")
            a.level = len(self.cfg.roster) - 1
        elif sev == "urgence" or self.cfg.sms_warnings:
            self._sms(a, 0, "alarm")
        self.out.alarm_email(a, upgraded=False)

    def _adopt(self, parent: Alarm, alarms):
        for x in alarms:
            if x is parent or x.parent is not None:
                continue
            x.parent = parent
            parent.children.append(x)
            self._persist(x)
            self.out.journal("group", x.entity, f"{x.title} → {parent.title} ({parent.entity}).")

    def _upgrade(self, T, a: Alarm, sev, ctx):
        i = self.info(T, a.code, ctx)
        a.severity = sev
        for k, v in i.items():
            setattr(a, k, v)
        self._persist(a)
        if a.parent is not None:
            return
        self.out.journal("escal", a.entity, f"{a.title} → {sev} ({a.value}).")
        if not a.ack:
            self._sms(a, a.level, "alarm")
        self.out.alarm_email(a, upgraded=True)

    def _clear(self, T, a: Alarm):
        del T.alarms[a.code]
        T.ok.pop(a.code, None)
        a.t_end = self.now
        if a.code == "SDV":
            T.grace_until = self.now + self.th["restart_grace_min"] * 60
        self._persist(a)
        self.out.journal("clear", a.entity, f"{a.title}: {fmt(self.lang, 'back_normal')}")
        if a.parent is not None:
            return
        for idx in sorted(set(a.notified)):
            self._sms(a, idx, "clear")
        self.out.clear_email(a)

    def _sms(self, a: Alarm, idx: int, kind: str):
        L = self.lang
        t = hhmm(self.now)
        c = " : " if L == "fr" else ": "
        if kind == "alarm":
            text = f"{fmt(L, a.severity)} {a.entity} {t}\n{a.title}{c}{a.value}.\n{a.sms}"
            if a.severity == "urgence":
                text += "\n" + fmt(L, "reply_ok")
        elif kind == "escal":
            text = (f"{fmt(L, 'escal')} {a.entity}\n{fmt(L, 'not_acked_since', m=int((self.now - a.t0) / 60))}\n"
                    f"{a.title}{c}{a.value}.\n{fmt(L, 'reply_ok')}")
        else:
            text = f"{fmt(L, 'end')} {a.entity} {t}\n{a.title}{c}{fmt(L, 'back_normal')}"
        if kind != "clear":
            a.notified.append(idx)
            a.last_sms = self.now
        self.out.sms(idx, text, a, kind)

    def _escalate(self, now):
        esc = self.cfg.escalation_min * 60
        for a in self.alarms:
            if a.parent or a.t_end is not None or a.ack or a.severity != "urgence" or a.last_sms is None:
                continue
            if now - a.last_sms < esc:
                continue
            if a.level < len(self.cfg.roster) - 1:
                a.level += 1
                c = self.cfg.roster[a.level]
                self.out.journal("escal", a.entity, f"→ {c.name} ({c.role}).")
                self._sms(a, a.level, "escal")
            elif not a.exhausted:
                a.exhausted = True
                self.out.journal("escal", a.entity, fmt(self.lang, "roster_done"))

    def ack(self, alarm_id: int, idx: int, via: str) -> bool:
        with self.lock:
            a = next((x for x in self.alarms if x.id == alarm_id), None)
            if a is None or a.ack or a.t_end is not None:
                return False
            a = a.root()
            if a.ack:
                return False
            a.ack = (idx, self.now, via)
            self._persist(a)
            self.out.journal("ack", a.entity, f"OK {self.cfg.roster[idx].name} ({via}), {int((self.now - a.t0) / 60)} min.")
            return True

    def ack_from_phone(self, phone: str, text: str) -> list[int]:
        """An SMS reply 'OK' from a roster member acknowledges every open emergency
        that was sent to that person and is not yet acknowledged."""
        digits = "".join(ch for ch in phone if ch.isdigit())[-9:]
        idx = next((i for i, c in enumerate(self.cfg.roster) if digits and "".join(ch for ch in c.phone if ch.isdigit()).endswith(digits)), None)
        if idx is None or text.strip().upper().rstrip(".!") not in ("OK", "D'ACCORD", "ACK"):
            return []
        done = []
        with self.lock:
            for a in self.alarms:
                if a.parent is None and a.t_end is None and not a.ack and idx in a.notified and self.ack(a.id, idx, "SMS"):
                    done.append(a.id)
        return done

    def _persist(self, a):
        if self.store:
            self.store.save_alarm(a)

    # ------------------------------------------------------------------ views
    def perf(self, since: float | None = None) -> dict:
        with self.lock:
            since = since if since is not None else self.now - 86400
            span = max(self.now - max(since, self.started or self.now), 60)
            al = [a for a in self.alarms if a.t0 >= since]
            roots = [a for a in al if a.parent is None]
            nb = max(1, math.ceil(span / 600))
            b_raw, b_vis = [0] * nb, [0] * nb
            t_start = self.now - span
            for a in al:
                k = min(nb - 1, max(0, int((a.t0 - t_start) // 600)))
                b_raw[k] += 1
                if a.parent is None:
                    b_vis[k] += 1
            acked = [a for a in roots if a.ack]
            return {"span_min": span / 60, "raw": len(al), "roots": len(roots),
                    "rate_vis": len(roots) / (span / 600), "rate_raw": len(al) / (span / 600),
                    "peak_vis": max(b_vis), "peak_raw": max(b_raw),
                    "flood_vis": sum(1 for v in b_vis if v > 10) / nb * 100,
                    "urg_pct": (sum(1 for a in roots if a.severity == "urgence") / len(roots) * 100) if roots else 0,
                    "ack_avg_min": mean((a.ack[1] - a.t0) / 60 for a in acked) if acked else None,
                    "reduction": (len(al) - len(roots)) / len(al) * 100 if al else 0,
                    "buckets_raw": b_raw, "buckets_vis": b_vis}

    def state(self) -> dict:
        with self.lock:
            def well_view(w):
                return {"id": w.id, "type": w.cfg.type, "manifold": w.cfg.manifold, "repeater": w.cfg.repeater,
                        "values": {k: v for k, v in w.cur.items()}, "age_min": round(self.age_min(w), 1),
                        "learning": not w.base["whp"].ready,
                        "normal": {k: b.value for k, b in w.base.items() if b.value is not None},
                        "alarms": [a.to_dict() for a in w.alarms.values()],
                        "state": "urgence" if any(a.severity == "urgence" for a in w.alarms.values()) else ("avert" if w.alarms else "normal"),
                        "defer_m3": round(w.defer_m3, 2),
                        "hist": [round(v, 2) for v in list(w.hist["whp"])[-int(90 * self.spm)::max(1, int(round(self.spm)))]]}
            return {
                "now": self.now, "site": self.cfg.site_name, "language": self.lang,
                "link": {"ok": bool(self.link_last_ok and self.now - self.link_last_ok < 3 * self.cfg.poll_seconds),
                         "last_ok": self.link_last_ok, "error": self.link_err},
                "satellites": [{"id": st.id, "zone": self.cfg.satellites.get(st.id, ""), "manifolds": [m.id for m in st.members],
                                "alarms": [a.to_dict() for a in st.alarms.values()]} for st in self.sats.values()],
                "manifolds": [{"id": m.id, "repeater": m.cfg.repeater, "satellite": m.cfg.satellite, "values": dict(m.cur),
                               "alarms": [a.to_dict() for a in list(m.alarms.values())
                                          + list(self.reps.get(m.cfg.repeater, Entity("", "", "")).alarms.values())]}
                              for m in self.mans.values()],
                "wells": [well_view(w) for w in self.wells.values()],
                "active": [a.to_dict() for a in self.alarms if a.t_end is None and a.parent is None],
                "link_alarms": [a.to_dict() for a in self.link.alarms.values()],
                "recent": [a.to_dict() for a in self.alarms[-60:]][::-1],
                "roster": [{"name": c.name, "role": c.role} for c in self.cfg.roster],
                "perf": self.perf(),
                "defer_m3": round(sum(w.defer_m3 for w in self.wells.values()), 2),
            }
