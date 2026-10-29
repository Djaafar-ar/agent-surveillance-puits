"""Long-term analysis: what a production engineer does with weeks and months of data.

The real-time engine answers "what is wrong right now?". This module answers
"what is slowly going wrong, and what should we plan?". It reads the daily
history (one row per day, well and signal), looks for known patterns, predicts
when a limit will be reached, and turns everything into a short list of
priorities, each with its evidence, the recommended action and the team that
should act.

Patterns
  decline         pressure decline per well (30 / 60 / 365 days), zone or well-specific,
                  date when the well will no longer flow into its manifold
  sensor_drift    pressure falling while temperature stays flat: transmitter drift, not a real decline
  salt_deposits   sawtooth: pressure recovers sharply after each wash then falls again
  step_drop       sudden drop not explained by an operation
  choke_trend     pressure lost across the choke rising (plugging) or falling (erosion)
  annulus_buildup annulus pressure creeping up: date it will reach the diagnostic threshold
  battery_ageing  night-time battery voltage falling week after week: replace before failure
  solar_degraded  solar panel current falling (sand, dirt, failing panel)
  cabinet_hot     RTU cabinet repeatedly above its limit
  rtu_unreliable  radio availability below target, repeated RTU losses
  repeater_unreliable  repeater failing again and again
  bad_actor       alarm that keeps coming back (threshold to review or cause to fix)

Everything here is deterministic and explainable; a language model may only be
used afterwards to write a summary (see reports.llm_summary).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

TEAMS = ("production", "instrumentation", "telecom", "maintenance", "hse")
TEAM_LAB = {"fr": {"production": "Production", "instrumentation": "Instrumentation", "telecom": "Télécom / radio",
                   "maintenance": "Maintenance électrique", "hse": "Intégrité / HSE"},
            "en": {"production": "Production", "instrumentation": "Instrumentation", "telecom": "Telecom / radio",
                   "maintenance": "Electrical maintenance", "hse": "Well integrity / HSE"}}
SEV_LAB = {"fr": {3: "URGENT", 2: "À planifier", 1: "À surveiller"}, "en": {3: "URGENT", 2: "Plan", 1: "Watch"}}


@dataclass
class Finding:
    entity: str
    kind: str
    severity: int              # 3 urgent, 2 plan, 1 watch
    team: str
    title: str
    evidence: str
    action: str
    due_days: float | None = None
    data: dict = field(default_factory=dict)
    ref: date | None = None    # last complete day of data; deadlines count from it

    @property
    def score(self):
        s = self.severity * 100
        if self.due_days is not None:
            s += max(0.0, 90 - self.due_days)
        return s

    def to_dict(self, lang="fr"):
        return {"entity": self.entity, "kind": self.kind, "severity": self.severity, "severity_label": SEV_LAB[lang][self.severity],
                "team": self.team, "team_label": TEAM_LAB[lang][self.team], "title": self.title, "evidence": self.evidence,
                "action": self.action, "due_days": None if self.due_days is None else round(self.due_days),
                "due_date": None if self.due_days is None else ((self.ref or date.today()) + timedelta(days=self.due_days)).isoformat()}


def _ord(d):
    return d.toordinal()


def slope_per_day(pts):
    """Least-squares slope of [(date, value)], per day."""
    if len(pts) < 2:
        return 0.0
    xs = [_ord(d) for d, _ in pts]
    ys = [v for _, v in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def window(pts, end, days):
    return [(d, v) for d, v in pts if end - timedelta(days=days) < d <= end]


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def f1(v, d=1):
    return f"{v:.{d}f}".replace(".", ",")


T = {
"fr": {
  "fast_decline": ("Déclin rapide de la pression tête", "Programmer un essai de puits et étudier l'activation (gas lift) ou une stimulation."),
  "decline": ("Déclin marqué de la pression tête", "Suivre au prochain essai de puits."),
  "zone": "Déclin de zone : les puits du manifold {m} baissent au même rythme (médiane {med} bar/mois). Cause probable : réservoir.",
  "well_specific": "Déclin propre au puits : les autres puits du manifold {m} baissent de {med} bar/mois seulement. Cause probable dans le puits (dépôts, venue d'eau, tubing).",
  "decline_ev": "{s60} bar/mois sur 60 jours ({s365} bar/mois sur l'année). Pression actuelle {p} Barg ; débit d'huile {oil}. ",
  "forecast": "Au rythme actuel, le puits n'aura plus assez de pression pour débiter dans le manifold ({pmin} Barg) vers le {date} (dans {m} mois).",
  "sensor_drift": ("Dérive probable du transmetteur de pression tête", "Vérifier et réétalonner le transmetteur avant de conclure à un déclin."),
  "sensor_drift_ev": "La pression baisse de {s60} bar/mois depuis 60 jours mais la température tête reste stable ({t60} °C/mois). Une vraie baisse d'écoulement ferait aussi baisser la température.",
  "salt_deposits": ("Dépôts de sel récurrents", "Rapprocher les lavages ou augmenter le débit d'eau de dessalage."),
  "salt_ev": "{n} remontées brutales de pression sur l'année (lavages), puis une baisse d'environ {drop} bar en {gap} jours à chaque fois. La cause est le sel, pas le réservoir.",
  "step_drop": ("Chute brutale de pression sans opération enregistrée", "Programmer un essai de puits pour trouver ce qui a changé (dépôt, venue d'eau, tubing)."),
  "step_ev": "Chute de {size} bar autour du {d}, sans fermeture enregistrée ce jour-là.",
  "choke_plug": ("Duse qui s'encrasse progressivement", "Planifier le nettoyage ou le remplacement de la duse."),
  "choke_erosion": ("Duse probablement érodée", "Contrôler la duse : une duse érodée laisse passer plus de débit que prévu (sable, risque pour la ligne)."),
  "choke_ev": "La perte de charge à travers la duse est passée de {a} à {b} bar en un mois ({pct} %).",
  "annulus_buildup": ("Pression annulaire {ann} en montée lente", "Planifier un test de purge et remontée avant que le seuil soit atteint."),
  "annulus_ev": "Maximum journalier passé de {a} à {b} Barg en 60 jours (+{rate} bar/jour). Au rythme actuel, le seuil de diagnostic de {th} Barg sera atteint vers le {date}.",
  "battery_ageing": ("Batterie de la RTU en fin de vie", "Remplacer la batterie avant la date prévue."),
  "battery_ev": "La tension minimale de nuit baisse de {rate} V/semaine (dernière nuit {v} V). Au rythme actuel, la RTU ne passera plus la nuit vers le {date}.",
  "solar_degraded": ("Panneau solaire moins productif", "Nettoyer le panneau (sable) et vérifier son câblage."),
  "solar_ev": "Courant maximal du panneau passé de {a} A à {b} A en un mois ({pct} %).",
  "cabinet_hot": ("Coffret RTU trop chaud", "Améliorer l'aération ou l'ombrage du coffret."),
  "cabinet_ev": "Température du coffret au-dessus de {th} °C pendant {n} des 14 derniers jours (max {mx} °C).",
  "rtu_unreliable": ("Liaison radio de la RTU peu fiable", "Vérifier antenne, radio, alimentation et câbles de la RTU."),
  "rtu_ev": "Disponibilité radio {av} % sur 30 jours (cible 99 %), {n} pertes de communication.",
  "repeater_unreliable": ("Répéteur radio qui tombe en panne à répétition", "Inspecter le répéteur (alimentation, batterie, antenne) et envisager un secours."),
  "repeater_ev": "{n} pannes en 30 jours ; chaque panne coupe la surveillance de {w} puits.",
  "bad_actor": ("Alarme qui revient sans cesse", "Revoir le seuil de cette alarme ou traiter la cause de fond."),
  "bad_actor_ev": "« {title} » : {n} fois en 30 jours. Les alarmes qui reviennent font perdre confiance dans toutes les autres (EEMUA 191).",
  "sensors_rep": ("Transmetteurs à réparer", "Intervention instrumentation."),
},
"en": {
  "fast_decline": ("Fast wellhead pressure decline", "Plan a well test and study artificial lift or stimulation."),
  "decline": ("Marked wellhead pressure decline", "Follow up at the next well test."),
  "zone": "Field-wide decline: the wells of manifold {m} fall at the same rate (median {med} bar/month). Probable cause: reservoir.",
  "well_specific": "Well-specific decline: the other wells of manifold {m} fall by only {med} bar/month. Probable cause in the well (deposits, water, tubing).",
  "decline_ev": "{s60} bar/month over 60 days ({s365} bar/month over the year). Current pressure {p} Barg; oil rate {oil}. ",
  "forecast": "At the current rate the well will no longer have enough pressure to flow into the manifold ({pmin} Barg) around {date} (in {m} months).",
  "sensor_drift": ("Probable wellhead pressure transmitter drift", "Check and recalibrate the transmitter before concluding to a decline."),
  "sensor_drift_ev": "Pressure falls by {s60} bar/month over 60 days but wellhead temperature stays flat ({t60} °C/month). A real drop in flow would also lower the temperature.",
  "salt_deposits": ("Recurrent salt deposits", "Wash more often or increase the desalting water rate."),
  "salt_ev": "{n} sharp pressure recoveries over the year (washes), then a fall of about {drop} bar in {gap} days each time. The cause is salt, not the reservoir.",
  "step_drop": ("Sudden pressure drop with no recorded operation", "Plan a well test to find what changed (deposit, water, tubing)."),
  "step_ev": "Drop of {size} bar around {d}, with no shut-in recorded that day.",
  "choke_plug": ("Choke progressively plugging", "Plan cleaning or replacement of the choke."),
  "choke_erosion": ("Choke probably eroded", "Inspect the choke: an eroded choke lets more flow through than intended (sand, risk for the line)."),
  "choke_ev": "Pressure drop across the choke went from {a} to {b} bar in a month ({pct} %).",
  "annulus_buildup": ("Annulus {ann} pressure slowly building up", "Plan a bleed-down / build-up test before the threshold is reached."),
  "annulus_ev": "Daily maximum went from {a} to {b} Barg in 60 days (+{rate} bar/day). At this rate the {th} Barg diagnostic threshold will be reached around {date}.",
  "battery_ageing": ("RTU battery reaching end of life", "Replace the battery before the predicted date."),
  "battery_ev": "Night-time minimum voltage falls by {rate} V/week (last night {v} V). At this rate the RTU will not last the night around {date}.",
  "solar_degraded": ("Solar panel producing less", "Clean the panel (sand) and check its wiring."),
  "solar_ev": "Panel peak current went from {a} A to {b} A in a month ({pct} %).",
  "cabinet_hot": ("RTU cabinet too hot", "Improve cabinet ventilation or shading."),
  "cabinet_ev": "Cabinet temperature above {th} °C on {n} of the last 14 days (max {mx} °C).",
  "rtu_unreliable": ("Unreliable RTU radio link", "Check the RTU antenna, radio, power supply and cables."),
  "rtu_ev": "Radio availability {av} % over 30 days (target 99 %), {n} communication losses.",
  "repeater_unreliable": ("Radio repeater failing repeatedly", "Inspect the repeater (power, battery, antenna) and consider a backup."),
  "repeater_ev": "{n} failures in 30 days; each failure stops the monitoring of {w} wells.",
  "bad_actor": ("Alarm that keeps coming back", "Review this alarm's threshold or fix the underlying cause."),
  "bad_actor_ev": "\"{title}\": {n} times in 30 days. Recurring alarms make operators trust all the others less (EEMUA 191).",
  "sensors_rep": ("Transmitters to repair", "Instrumentation job."),
},
}


class Analyst:
    def __init__(self, cfg, store, engine=None):
        self.cfg, self.store, self.engine = cfg, store, engine
        self.L = cfg.language
        self.t = T[self.L]

    # ------------------------------------------------------------------ helpers
    def _valid(self, wid, sig, days=400, stat="mean"):
        """Daily values excluding days the well was shut in, on test or mostly offline."""
        pts = self.store.series(wid, sig, days, stat, full_days=True)
        bad = set()
        for s_, lim in (("shut", 0.2), ("test", 0.2)):
            bad |= {d for d, v in self.store.series(wid, s_, days, full_days=True) if v is not None and v > lim}
        bad |= {d for d, v in self.store.series(wid, "online", days, full_days=True) if v is not None and v < 0.5}
        return [(d, v) for d, v in pts if d not in bad and v is not None]

    def _date(self, days_ahead, end):
        return (end + timedelta(days=days_ahead)).strftime("%d/%m/%Y")

    # ------------------------------------------------------------------ main
    def run(self, end: date | None = None) -> list[Finding]:
        # complete days only: today's partial aggregates stay in memory until midnight
        end = end or self.store.last_day() or date.today()
        out: list[Finding] = []
        declines = {}
        for w in self.cfg.wells:
            res = self._decline(w, end)
            if res:
                declines[w.id] = res
        out += self._decline_findings(declines, end)
        for w in self.cfg.wells:
            out += self._choke(w, end)
            out += self._annulus(w, end)
            out += self._power(w, end)
            out += self._radio(w, end)
        out += self._repeaters(end)
        out += self._bad_actors(end)
        out.sort(key=lambda f: -f.score)
        for f in out:
            f.ref = end
        return out

    # ------------------------------------------------------------------ decline
    def _decline(self, w, end):
        pts = self._valid(w.id, "whp")
        if len(pts) < 21:
            return None
        p30, p60, p365 = window(pts, end, 30), window(pts, end, 60), window(pts, end, 365)
        if len(p60) < 10:
            return None
        # sawtooth (washes): sharp one-day recoveries
        jumps = []
        for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
            if (d1 - d0).days <= 2 and v1 - v0 > 2.0:
                jumps.append((d1, v1 - v0))
        # fit the slope only after the last jump inside each window, so washes do not hide the trend
        def fit(win, days):
            last = [d for d, _ in jumps if end - timedelta(days=days) < d <= end]
            seg = [(d, v) for d, v in win if not last or d >= max(last)]
            return slope_per_day(seg) * 30.4 if len(seg) >= 8 else slope_per_day(win) * 30.4
        s60, s30 = fit(p60, 60), fit(p30, 30) if len(p30) >= 8 else 0.0
        s365 = slope_per_day(p365) * 30.4
        t60pts = window(self._valid(w.id, "wht"), end, 60)
        t60 = slope_per_day(t60pts) * 30.4 if len(t60pts) >= 10 else None
        # step drop: largest fall between consecutive weeks, away from jumps
        step = None
        for i in range(7, len(pts) - 7):
            d = pts[i][0]
            if any(abs((d - j).days) <= 10 for j, _ in jumps):
                continue
            before = [v for dd, v in pts[i - 7:i] if (d - dd).days <= 9]
            after = [v for dd, v in pts[i + 1:i + 8] if (dd - d).days <= 9]
            if len(before) < 5 or len(after) < 5:
                continue
            drop = mean(before) - mean(after)
            if drop > 2.5 and (step is None or drop > step[1]):
                step = (d, drop)
        man = self.store.series(w.manifold, "p_man", 14, full_days=True) if w.manifold else []
        pmin = (mean(v for _, v in man) if man else 0) + 3
        oil = self._valid(w.id, "oil_rate")
        oil60 = window(oil, end, 60)
        oil_trend = (slope_per_day(oil60) * 30.4 / mean(v for _, v in oil60) * 100) if len(oil60) >= 10 and mean(v for _, v in oil60) > 0 else None
        return {"s30": s30, "s60": s60, "s365": s365, "t60": t60, "jumps": jumps, "step": step, "pnow": mean(v for _, v in pts[-5:]),
                "pmin": pmin, "oil_now": mean(v for _, v in oil[-5:]) if oil else None, "oil_trend": oil_trend, "type": w.type, "man": w.manifold}

    def _decline_findings(self, D, end):
        t, out = self.t, []
        # manifold median of "ordinary" declines, to tell field-wide from well-specific decline
        med = {}
        for m in self.cfg.manifolds:
            vals = [r["s60"] for wid, r in D.items() if r["man"] == m.id and len(r["jumps"]) < 2 and not self._is_drift(r)]
            med[m.id] = statistics.median(vals) if vals else 0.0
        for wid, r in D.items():
            if self._is_drift(r):
                title, action = t["sensor_drift"]
                out.append(Finding(wid, "sensor_drift", 2, "instrumentation", title,
                                   t["sensor_drift_ev"].format(s60=f1(-r["s60"]), t60=f1(r["t60"], 2)), action))
                continue
            if len(r["jumps"]) >= 2:
                js = [d for d, _ in r["jumps"]]
                gap = mean((b - a).days for a, b in zip(js, js[1:]))
                drop = mean(sz for _, sz in r["jumps"])
                title, action = t["salt_deposits"]
                out.append(Finding(wid, "salt_deposits", 2, "production", title,
                                   t["salt_ev"].format(n=len(js), drop=f1(drop), gap=round(gap)), action))
                continue
            if r["step"]:
                title, action = t["step_drop"]
                out.append(Finding(wid, "step_drop", 2, "production", title,
                                   t["step_ev"].format(size=f1(r["step"][1]), d=r["step"][0].strftime("%d/%m/%Y")), action))
            s60 = r["s60"]
            if s60 < -0.6:
                months = (r["pnow"] - r["pmin"]) / -s60 if r["pnow"] > r["pmin"] else 0.0
                fast = s60 < -1.5
                sev = 3 if fast and months < 6 else 2 if fast else 1
                kind = "fast_decline" if fast else "decline"
                title, action = t[kind]
                oil = "-" if r["oil_now"] is None else (f"{f1(r['oil_now'])} m³/j" if self.L == "fr" else f"{f1(r['oil_now'])} m³/d")
                if r["oil_trend"] is not None:
                    oil += f" ({'+' if r['oil_trend'] >= 0 else ''}{f1(r['oil_trend'], 0)} %/{'mois' if self.L == 'fr' else 'month'})"
                ev = t["decline_ev"].format(s60=f1(s60), s365=f1(r["s365"]), p=f1(r["pnow"]), oil=oil)
                m = r["man"]
                if m:
                    ev += (t["well_specific"] if s60 < 2 * med.get(m, 0) - 0.3 else t["zone"]).format(m=m, med=f1(-med.get(m, 0))) + " "
                due = None
                if months < 36:
                    due = months * 30.4
                    ev += t["forecast"].format(pmin=f1(r["pmin"]), date=self._date(due, end), m=max(1, round(months)))
                out.append(Finding(wid, kind, sev, "production", title, ev.strip(), action, due, {"s60": s60}))
        return out

    @staticmethod
    def _is_drift(r):
        return r["s60"] < -0.8 and r["t60"] is not None and abs(r["t60"]) < 0.12

    # ------------------------------------------------------------------ choke
    def _choke(self, w, end):
        pts = self._valid(w.id, "dp_choke")
        recent, before = window(pts, end, 7), [(d, v) for d, v in window(pts, end, 45) if d <= end - timedelta(days=30)]
        if len(recent) < 4 or len(before) < 4:
            return []
        a, b = mean(v for _, v in before), mean(v for _, v in recent)
        if a <= 0.5:
            return []
        pct = (b - a) / a * 100
        if pct > 30 and b - a > 1.5:
            kind, key = "choke_trend", "choke_plug"
        elif pct < -30 and a - b > 1.5:
            kind, key = "choke_trend", "choke_erosion"
        else:
            return []
        title, action = self.t[key]
        return [Finding(w.id, kind, 2, "production", title, self.t["choke_ev"].format(a=f1(a), b=f1(b), pct=f"{pct:+.0f}"), action)]

    # ------------------------------------------------------------------ annulus
    def _annulus(self, w, end):
        out = []
        th = self.cfg.thresholds["annulus_warn_barg"]
        for sig, lab in (("ann", "9 5/8"), ("ann_7", '7"')):
            if sig not in w.tags or (sig == "ann_7" and w.type == "gas_lift"):
                continue
            pts = window(self._valid(w.id, sig, 90, "max"), end, 60)
            if len(pts) < 20:
                continue
            rate = slope_per_day(pts)
            last = mean(v for _, v in pts[-3:])
            if rate > 0.02 and last < th:
                days = (th - last) / rate
                if days <= 120:
                    title, action = self.t["annulus_buildup"]
                    out.append(Finding(w.id, "annulus_buildup", 3 if days < 30 else 2, "hse", title.format(ann=lab),
                                       self.t["annulus_ev"].format(a=f1(pts[0][1]), b=f1(last), rate=f1(rate, 2), th=f1(th),
                                                                   date=self._date(days, end)), action, days))
        return out

    # ------------------------------------------------------------------ power
    def _power(self, w, end):
        out = []
        bat = window(self.store.series(w.id, "battery", 60, "min", full_days=True), end, 45)
        if len(bat) >= 14:
            rate = slope_per_day(bat)
            last = bat[-1][1]
            limit = 23.0
            days = (last - limit) / -rate if rate < -0.005 else None
            if (days is not None and days <= 60) or last < 24.0:
                days = days if days is not None else 0.0
                title, action = self.t["battery_ageing"]
                out.append(Finding(w.id, "battery_ageing", 3 if days < 14 else 2, "maintenance", title,
                                   self.t["battery_ev"].format(rate=f1(-rate * 7, 2), v=f1(last, 2), date=self._date(days, end)),
                                   action, days))
        sol = self.store.series(w.id, "solar_current", 60, "max", full_days=True)
        recent, before = window(sol, end, 7), [(d, v) for d, v in window(sol, end, 45) if d <= end - timedelta(days=30)]
        if len(recent) >= 4 and len(before) >= 4:
            a, b = mean(v for _, v in before), mean(v for _, v in recent)
            if a > 0.5 and (b - a) / a < -0.3:
                title, action = self.t["solar_degraded"]
                out.append(Finding(w.id, "solar_degraded", 1 if b > 0.5 * a else 2, "maintenance", title,
                                   self.t["solar_ev"].format(a=f1(a), b=f1(b), pct=f"{(b - a) / a * 100:+.0f}"), action))
        temp = window(self.store.series(w.id, "cabinet_temp", 20, "max", full_days=True), end, 14)
        th = self.cfg.thresholds["cabinet_temp_max"] - 5
        hot = [v for _, v in temp if v > th]
        if len(hot) >= 5:
            title, action = self.t["cabinet_hot"]
            out.append(Finding(w.id, "cabinet_hot", 1, "maintenance", title,
                               self.t["cabinet_ev"].format(th=f1(th, 0), n=len(hot), mx=f1(max(hot), 0)), action))
        return out

    # ------------------------------------------------------------------ radio
    def _alarms(self, end, days=30):
        since = datetime.combine(end - timedelta(days=days), datetime.min.time()).timestamp()
        until = datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp()
        return [r for r in self.store.alarm_history(since) if r[3] < until]

    def _radio(self, w, end):
        on = window(self.store.series(w.id, "online", 40, full_days=True), end, 30)
        if len(on) < 10:
            return []
        av = mean(v for _, v in on) * 100
        n = sum(1 for r in self._alarms(end) if r[0] == w.id and r[1] == "RTU")
        if av < 97 or n >= 4:
            title, action = self.t["rtu_unreliable"]
            return [Finding(w.id, "rtu_unreliable", 2, "telecom", title, self.t["rtu_ev"].format(av=f1(av), n=n), action)]
        return []

    def _repeaters(self, end):
        out = []
        counts = {}
        for r in self._alarms(end):
            if r[1] == "REP" and r[5] is None:
                counts[r[0]] = counts.get(r[0], 0) + 1
        for ent, n in counts.items():
            if n >= 2:
                rid = ent.split()[-1]
                nw = sum(1 for w in self.cfg.wells if w.repeater == rid)
                title, action = self.t["repeater_unreliable"]
                out.append(Finding(ent, "repeater_unreliable", 2, "telecom", title, self.t["repeater_ev"].format(n=n, w=nw), action))
        return out

    def _bad_actors(self, end):
        counts, titles = {}, {}
        for ent, code, sev, t0, t_end, parent, ack_t, title in self._alarms(end):
            if parent is not None or code in ("REP", "RTU", "SCADA_LINK"):
                continue
            counts[(ent, code)] = counts.get((ent, code), 0) + 1
            titles[(ent, code)] = title
        out = []
        for (ent, code), n in sorted(counts.items(), key=lambda x: -x[1])[:5]:
            if n >= 5:
                title, action = self.t["bad_actor"]
                team = "instrumentation" if code.startswith("CAPTEUR") or code == "CARTE" else "production"
                out.append(Finding(ent, "bad_actor", 1, team, title, self.t["bad_actor_ev"].format(title=titles[(ent, code)] or code, n=n), action))
        return out
