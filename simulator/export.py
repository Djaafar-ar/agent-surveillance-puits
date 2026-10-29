"""Generates a simulated HISTORIAN EXPORT (one value per hour) and the matching
EVENT LOG, in the format a site would provide, to demonstrate the backtest:

    python -m simulator.export --days 365 --out data/backtest

writes data/backtest/export.csv and data/backtest/events.csv.

THIS IS SIMULATED DATA. It only has the signals most historians keep
(wellhead pressure and temperature, line pressure, annulus, RTU battery,
manifold pressure), not the full set the live agent reads. Planted cases:

    PUITS-03  decline accelerating, the well dies and is shut in       (warning weeks ahead expected)
    PUITS-11  annulus pressure building up, bled down by the team       (warning weeks ahead expected)
    PUITS-06  RTU battery ageing, RTU goes dark at night, replaced     (warning weeks ahead expected)
    PUITS-01  choke slowly plugging, then plugged, choke changed       (warning ahead expected)
    PUITS-08  pressure transmitter drifting, recalibrated              (warning ahead expected)
    PUITS-04  salt deposits, washed three times                        (warnings once the pattern is learned)
    PUITS-09  sudden leak, emergency shut-in                           (seen when it happens, no warning possible)
    R2        radio repeater failure for 3 hours                       (seen when it happens)
    PUITS-12  safety valve defect found on a site round                (not visible in the data: missed)
Plus routine well tests, which are not problems.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
from datetime import date, datetime, timedelta

from .field_model import MANIFOLDS, WELLS

DECL = {"PUITS-01": -0.35, "PUITS-02": -0.3, "PUITS-03": -0.6, "PUITS-04": -0.3, "PUITS-05": -0.25, "PUITS-06": -0.2,
        "PUITS-07": -0.45, "PUITS-08": -0.3, "PUITS-09": -0.3, "PUITS-10": -0.4, "PUITS-11": -0.1, "PUITS-12": -0.45}
SIGNALS = ("whp", "wht", "line_p", "ann", "battery")
REPEATER = {"MAN-A": "R1", "MAN-B": "R2", "MAN-C": "R3"}


def generate(out_dir: str, days: int = 365, end: date | None = None, seed: int = 11):
    """Returns (export_path, events_path)."""
    r = random.Random(seed)
    N = days
    end = end or (date.today() - timedelta(days=1))
    t_start = datetime.combine(end - timedelta(days=N - 1), datetime.min.time())
    os.makedirs(out_dir, exist_ok=True)
    ts = lambda k, h, m=0: t_start + timedelta(days=k, hours=h, minutes=m)

    events = []

    def ev(k, h, m, who, typ, text):
        events.append((ts(k, h, m), who, typ, text))

    # ---- scripted events (k = day index from the start) -----------------------
    d03 = N - 4                      # PUITS-03 dies
    ev(d03, 8, 0, "PUITS-03", "shut_in", "Puits fermé : pression insuffisante pour débiter dans le manifold")
    a11 = N - 35
    ev(a11, 10, 30, "PUITS-11", "intervention", "Purge annulaire 9 5/8 et test de remontée")
    b06 = N - 60
    ev(b06, 11, 0, "PUITS-06", "intervention", "Remplacement de la batterie de la RTU (RTU coupée la nuit)")
    c01 = N - 173
    ev(c01, 10, 0, "PUITS-01", "intervention", "Changement de duse (duse bouchée)")
    s08 = N - 10
    ev(s08, 9, 30, "PUITS-08", "intervention", "Réétalonnage du transmetteur de pression tête")
    washes = [N - 270, N - 160, N - 47]
    for k in washes:
        ev(k, 9, 0, "PUITS-04", "intervention", "Lavage du puits (dépôts de sel)")
    l09 = N - 245
    ev(l09, 3, 40, "PUITS-09", "failure", "Fuite sur la tête de puits")
    ev(l09, 3, 45, "PUITS-09", "shut_in", "Fermeture d'urgence : fuite sur la tête de puits")
    ev(l09 + 5, 14, 0, "PUITS-09", "reopen", "Réouverture après réparation")
    rep = N - 200
    ev(rep, 2, 10, "R2", "failure", "Panne du répéteur radio R2")
    ev(N - 130, 10, 0, "PUITS-12", "failure", "Défaut de la vanne de sécurité trouvé lors de la tournée")
    for i, w in enumerate(WELLS):
        for k in range(12 + 3 * i, N - 2, 40):
            ev(k, 9, 0, w["id"], "test", "Essai de puits")

    # ---- hourly values --------------------------------------------------------
    head = ["timestamp"] + [f"{w['id']}.{s}" for w in WELLS for s in SIGNALS] + [f"{m['id']}.p_man" for m in MANIFOLDS]
    man = {m["id"]: m for m in MANIFOLDS}
    state = {w["id"]: {"lvl": 0.0, "salt": 0.0} for w in WELLS}
    path = os.path.join(out_dir, "export.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(head)
        for k in range(N):
            for h in range(24):
                row = [ts(k, h).strftime("%Y-%m-%d %H:%M:%S")]
                day_wave = math.sin(2 * math.pi * (h - 9) / 24)          # warmest mid-afternoon
                pman = {mid: m["p_man"] + 0.1 * day_wave + r.gauss(0, 0.08) for mid, m in man.items()}
                for w in WELLS:
                    wid, st = w["id"], state[w["id"]]
                    if h == 0:
                        rate = DECL[wid]
                        if wid == "PUITS-03" and k >= N - 75:
                            rate = -2.2 - 3.0 * max(0, k - (N - 25)) / 25      # accelerating at the end
                        st["lvl"] += rate / 30.4
                        if wid == "PUITS-04":
                            st["salt"] = max(st["salt"] - 0.035, -3.5)
                    if wid == "PUITS-04" and k in washes and h == 12:
                        st["salt"] = 0.0                                   # washed in the morning, flowing again at noon
                    p = w["p"] + st["lvl"] + st["salt"] + 0.15 * day_wave + r.gauss(0, 0.07)
                    t = w["t"] + 1.4 * day_wave + 0.35 * (st["lvl"] + st["salt"]) + r.gauss(0, 0.15)
                    pm = pman[w["man"]]
                    line = pm + 2.0 + r.gauss(0, 0.05)
                    ann = w["ann"] + 0.12 * day_wave + r.gauss(0, 0.03)
                    sun = 9 <= h <= 17
                    night_h = (h - 18) % 24 if not sun else 0             # hours since sunset
                    bat = 27.3 + r.gauss(0, 0.03) if sun else 26.7 - 0.09 * night_h + r.gauss(0, 0.03)
                    dark = False
                    # -- planted cases
                    if wid == "PUITS-03" and k >= d03 and (k > d03 or h >= 8):
                        p, line, t = 36.0 + r.gauss(0, 0.05), pm + r.gauss(0, 0.03), 22 + 1.4 * day_wave
                    if wid == "PUITS-11" and N - 115 <= k < a11 + (h >= 11):
                        ann += 0.07 * (k - (N - 115) + h / 24)
                    if wid == "PUITS-06" and N - 165 <= k < b06 + (h >= 11) and not sun:
                        age = (k - (N - 165)) / 105                       # 0 -> 1 when replaced
                        bat -= 2.6 * age ** 1.6 * night_h / 15
                        dark = bat < 23.0                                  # RTU switches off
                    if wid == "PUITS-01":
                        if c01 - 42 <= k < c01:
                            f_ = (k - (c01 - 42) + h / 24) / 42
                            p += 2.0 * f_
                            line -= 1.2 * f_
                        if k == c01 - 1 and h >= 6 or k == c01 and h < 10:
                            p += 3.0
                            line -= 0.6
                    if wid == "PUITS-08" and N - 50 <= k < s08 + (h >= 10):
                        p -= 2.0 * (k - (N - 50) + h / 24) / 30.4         # transmitter drift: temperature unchanged
                    if wid == "PUITS-09" and l09 <= k <= l09 + 5:
                        if k == l09 and h == 3:
                            p = pm + 1.0
                        elif (k == l09 and h >= 4) or (l09 < k < l09 + 5) or (k == l09 + 5 and h < 14):
                            p, line = 0.4 + r.gauss(0, 0.05), pm + r.gauss(0, 0.03)   # shut in, wellhead bled down
                    if REPEATER[w["man"]] == "R2" and k == rep and 2 <= h < 5:
                        dark = True
                    if dark:
                        row += [""] * len(SIGNALS)
                    else:
                        row += [f"{p:.2f}", f"{t:.2f}", f"{line:.2f}", f"{ann:.2f}", f"{bat:.2f}"]
                row += [f"{pman[m['id']]:.2f}" for m in MANIFOLDS]
                wr.writerow(row)

    events.sort()
    ep = os.path.join(out_dir, "events.csv")
    with open(ep, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["datetime", "equipment", "type", "description"])
        for t, who, typ, text in events:
            wr.writerow([t.strftime("%Y-%m-%d %H:%M"), who, typ, text])
    return path, ep


def main():
    ap = argparse.ArgumentParser(description="Simulated historian export + event log (demonstration only)")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="data/backtest")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    p, e = generate(a.out, a.days, seed=a.seed)
    print(f"Simulated export written: {p}\nEvent log: {e}")


if __name__ == "__main__":
    main()
