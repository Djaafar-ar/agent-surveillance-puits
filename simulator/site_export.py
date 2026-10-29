"""Generates a SIMULATED site export that looks like what a real historian and a
real control room give you, with all the usual problems, to prepare the import
before the real data arrives:

    python -m simulator.site_export --out data/site

writes, in data/site:

  historien_export.csv     long format, as exported from a historian:
                           Tag;Horodatage;Valeur;Qualité, Windows encoding (cp1252),
                           semicolons, decimal commas, dates dd/mm/yyyy, a title
                           block before the header, values archived only when they
                           change (irregular times), "Comm Failure" / "I/O Timeout"
                           rows, sentinel spikes (9999, -32768), one stuck
                           transmitter, annulus pressures in psi, duplicated rows
  liste_tags.csv           the tag list (Tag;Description;Unité) that instrumentation
                           keeps, used to guess the mapping
  journal_exploitation.csv the control-room event log typed by hand in French
                           (Date;Heure;Puits;Evènement;Observations), with routine
                           entries that are not problems

THIS IS SIMULATED DATA (same planted problems as simulator/export.py). Tag names
are generic (P01-PT-101...), not those of any real field.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import datetime, timedelta

from . import export as clean_export
from .field_model import MANIFOLDS, WELLS

# signal -> (tag suffix, description, unit, deadband for archiving)
SIG = {"whp": ("PT-101", "Pression tête de puits", "bar", 0.08),
       "wht": ("TT-101", "Température tête de puits", "°C", 0.3),
       "line_p": ("PT-102", "Pression aval duse (ligne)", "bar", 0.08),
       "ann": ("PT-201", "Pression annulaire 9 5/8", "psi", 0.5),
       "battery": ("EI-001", "Tension batterie RTU", "V", 0.05)}
PSI = 14.5038


def tag_of(entity: str, sig: str) -> str:
    if entity.startswith("MAN-"):
        return f"M{entity[-1]}-PT-001"
    return f"P{entity.split('-')[1]}-{SIG[sig][0]}"


def generate(out_dir: str, days: int = 365, seed: int = 11):
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "_clean")
    exp, ev = clean_export.generate(tmp, days, seed=seed)
    r = random.Random(seed + 1)

    with open(exp, encoding="utf-8") as f:
        rd = csv.reader(f)
        head = next(rd)
        rows = list(rd)
    cols = head[1:]
    series = {c: [] for c in cols}
    for row in rows:
        t = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        for c, v in zip(cols, row[1:]):
            series[c].append((t, float(v) if v else None))

    out = []          # (tag, time, value text, quality)
    stuck = "PUITS-07.line_p"
    t_stuck0 = series[stuck][len(series[stuck]) // 3][0]
    for c, pts in series.items():
        ent, sig = c.split(".")
        tag = tag_of(ent, sig)
        db = SIG[sig][3] if sig in SIG else 0.08
        last_v, last_t, in_gap = None, None, False
        # a few random communication failures per tag (a few hours each)
        outages = []
        for _ in range(r.randint(0, 3)):
            s = r.randrange(len(pts) - 10)
            outages.append((s, s + r.randint(2, 7)))
        for i, (t, v) in enumerate(pts):
            ts = t + timedelta(seconds=r.randint(0, 150))            # archive time is never exactly on the hour
            if any(a <= i < b for a, b in outages):
                v = None
            if v is None:
                if not in_gap:
                    out.append((tag, ts, r.choice(["Comm Failure", "I/O Timeout", "No Data"]), "Bad"))
                    in_gap = True
                continue
            in_gap = False
            if sig == "ann":
                v = v * PSI
            if c == stuck and t_stuck0 <= t < t_stuck0 + timedelta(days=10):
                out.append((tag, ts, _fr(round(v if last_v is None else last_v, 2)), "Good"))     # transmitter frozen
                last_v = v if last_v is None else last_v
                continue
            if last_v is None or abs(v - last_v) > db or (t - last_t) >= timedelta(hours=4):
                out.append((tag, ts, _fr(round(v, 2)), "Good"))
                last_v, last_t = v, t
            if r.random() < 0.0004:                                 # garbage from the RTU
                out.append((tag, ts + timedelta(seconds=20), r.choice(["9999", "-32768", "0"]), "Good"))
    for _ in range(40):                                             # duplicated rows (export overlap)
        out.append(r.choice(out))
    out.sort(key=lambda x: (x[0], x[1]))

    path = os.path.join(out_dir, "historien_export.csv")
    t0, t1 = series[cols[0]][0][0], series[cols[0]][-1][0]
    with open(path, "w", newline="", encoding="cp1252") as f:
        f.write(f"Export historien - Site de démonstration (SIMULÉ)\r\nPériode : {t0:%d/%m/%Y} - {t1:%d/%m/%Y}\r\n\r\n")
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["Tag", "Horodatage", "Valeur", "Qualité"])
        for tag, ts, v, q in out:
            wr.writerow([tag, ts.strftime("%d/%m/%Y %H:%M:%S"), v, q])

    with open(os.path.join(out_dir, "liste_tags.csv"), "w", newline="", encoding="cp1252") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["Tag", "Description", "Unité"])
        for w in WELLS:
            for sig, (suf, desc, unit, _) in SIG.items():
                wr.writerow([tag_of(w["id"], sig), f"{desc} puits {w['id'].split('-')[1]}", unit])
        for m in MANIFOLDS:
            wr.writerow([tag_of(m["id"], "p_man"), f"Pression collecteur production manifold {m['id'][-1]}", "bar"])

    # the event log as typed in the control room
    TXT = {"shut_in": ["Fermeture du puits", "Puits fermé", "Arrêt puits"],
           "reopen": ["Réouverture du puits", "Remise en production"],
           "test": ["Mise en essai", "Essai sur séparateur"],
           "failure": ["Panne", "Défaut constaté"],
           "intervention": ["Intervention"]}
    jr = []
    with open(ev, encoding="utf-8") as f:
        for e in csv.DictReader(f):
            t = datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
            eq = e["equipment"]
            who = f"P{eq.split('-')[1]}" if eq.startswith("PUITS-") else ("Répéteur " + eq if eq.startswith("R") else eq)
            if r.random() < 0.3 and eq.startswith("PUITS-"):
                who = "Puits " + eq.split("-")[1]
            what = e["description"]
            if e["type"] == "intervention":
                head_ = what.split(" (")[0]
                jr.append((t, who, head_, what))
            elif e["type"] == "test":
                jr.append((t, who, r.choice(TXT["test"]), "séparateur de test du manifold"))
                jr.append((t + timedelta(hours=12), who, "Fin d'essai", "puits remis sur collecteur production"))
            else:
                jr.append((t, who, r.choice(TXT[e["type"]]), what))
    # routine entries that are not problems
    for k in range(0, days, 9):
        t = t0 + timedelta(days=k, hours=r.choice([8, 10, 14]))
        jr.append((t, f"P{r.randint(1, 12):02d}", r.choice(["Visite de routine", "Relevé compteur", "Contrôle HSE", "Prise d'échantillon"]),
                   r.choice(["RAS", "rien à signaler", "conforme", ""])))
    jr.sort()
    with open(os.path.join(out_dir, "journal_exploitation.csv"), "w", newline="", encoding="cp1252") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["Date", "Heure", "Puits", "Evènement", "Observations"])
        for t, who, what, obs in jr:
            wr.writerow([t.strftime("%d/%m/%Y"), t.strftime("%H:%M"), who, what, obs])
    for p in (exp, ev):
        os.remove(p)
    os.rmdir(tmp)
    return path


def _fr(v: float) -> str:
    return f"{v}".replace(".", ",")


def main():
    ap = argparse.ArgumentParser(description="Simulated site export (historian long format + tag list + logbook)")
    ap.add_argument("--out", default="data/site")
    ap.add_argument("--days", type=int, default=365)
    a = ap.parse_args()
    p = generate(a.out, a.days)
    print(f"Simulated site export written in {os.path.dirname(p)}")


if __name__ == "__main__":
    main()
