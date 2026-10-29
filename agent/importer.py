"""Import a site's historian export and event log into the agent's format, with
a data-quality report, before a backtest (or to seed the history).

    # 1. propose a mapping from the tag list (or from the tag names alone)
    python -m agent import --config config/config.yaml --data historien_export.csv --tags liste_tags.csv --guess
    #    -> data/import/mapping.yaml, to check and correct by hand
    # 2. import: clean, convert, resample, classify the events
    python -m agent import --config config/config.yaml --data historien_export.csv --events journal.csv \\
                           --mapping data/import/mapping.yaml
    #    -> data/import/export.csv, events.csv, quality.html
    # 3. backtest
    python -m agent backtest --config config/config.yaml --csv data/import/export.csv --events data/import/events.csv

What it copes with (all of it is common in real exports):
  * long format (Tag;Time;Value;Quality, one row per value) or wide format
    (one column per tag); any of ; , tab as separator; decimal comma;
    dd/mm/yyyy or ISO dates; a title block before the header; UTF-8 or
    Windows (cp1252) encoding
  * values archived only when they change (irregular times): resampled on a
    regular grid by holding the last value, up to a maximum age
  * bad quality and text values ("Comm Failure", "I/O Timeout", "Bad Input"):
    the signal is missing until the next good value
  * sentinel values and spikes (9999, -32768, values outside the physical range)
  * stuck transmitters (the same value repeated for a day or more)
  * units: psi, kPa, MPa, kg/cm2 converted to bar; °F to °C
  * an event log typed by hand: the type (shut-in, reopening, failure,
    intervention, test) is recognised from the words, the well from its number;
    lines that cannot be classified are listed for a person to check

Nothing is guessed silently: every cleaning step is counted in the report.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import re
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta

import yaml

log = logging.getLogger("agent.import")

UNITS = {"bar": 1.0, "barg": 1.0, "bars": 1.0, "psi": 1 / 14.5038, "psig": 1 / 14.5038, "kpa": 0.01, "mpa": 10.0,
         "kg/cm2": 0.980665, "kgf/cm2": 0.980665, "°c": 1.0, "degc": 1.0, "c": 1.0, "v": 1.0, "m3/j": 1.0, "m3/d": 1.0, "%": 1.0}
RANGE = {"whp": (-1, 700), "line_p": (-1, 700), "arrival_p": (-1, 700), "ann": (-1, 700), "ann_7": (-1, 700), "p_man": (-1, 700),
         "p_gl": (-1, 700), "p_test": (-1, 700), "gl_inj_p": (-1, 700), "wht": (-30, 200), "battery": (5, 40),
         "cabinet_temp": (-30, 100), "oil_rate": (0, 5000), "gl_flow": (0, 1e6), "water_flow": (0, 1000)}
SENTINELS = {9999.0, -9999.0, 99999.0, -32768.0, 32767.0, 65535.0}

# words in tag descriptions / names -> signal (checked in this order)
GUESS = [
    ("ann_7", [r"annulaire\s*7", r"annulus\s*7", r"\b7\s*\"", r"PT-?202"]),
    ("ann", [r"annulaire", r"annulus", r"casing", r"\bCSG\b", r"PT-?201"]),
    ("p_man", [r"collecteur", r"manifold", r"\bheader\b", r"\bP_?MAN\b"]),
    ("line_p", [r"aval\s*duse", r"\bligne\b", r"downstream", r"flow\s*line", r"PT-?102", r"P_?LIGNE"]),
    ("whp", [r"pression\s*t[êe]te", r"wellhead\s*pressure", r"\bWHP\b", r"PT-?101", r"P_?TETE"]),
    ("wht", [r"temp[ée]rature\s*t[êe]te", r"wellhead\s*temp", r"\bWHT\b", r"TT-?101"]),
    ("battery", [r"batterie", r"battery", r"\bUBAT\b", r"\bVBAT\b", r"EI-?001"]),
    ("oil_rate", [r"d[ée]bit\s*huile", r"oil\s*rate"]),
    ("gl_flow", [r"d[ée]bit\s*gas\s*lift", r"gas\s*lift\s*(rate|flow)", r"\bFT-?301"]),
]
EV_RULES = [  # (type, patterns) checked in this order: "réouverture après réparation" is a reopening,
    # "purge annulaire et test de remontée" is an intervention, not a well test
    ("test_end", [r"fin\s+d['’ ]?\s*essai", r"end\s+of\s+test"]),
    ("reopen", [r"r[ée]ouverture", r"remise?\s+en\s+(production|service)", r"red[ée]marrage", r"\breopen", r"back\s+on\s+line", r"^ouverture"]),
    ("shut_in", [r"fermeture", r"\bferm[ée]", r"\barr[êe]t\b", r"shut", r"mise\s+en\s+s[ée]curit[ée]"]),
    ("failure", [r"\bpanne", r"d[ée]faut", r"\bfuite", r"\bcasse", r"d[ée]faillan", r"failure", r"\bleak"]),
    ("intervention", [r"lavage", r"changement", r"remplacement", r"[ée]talonnage", r"\bpurge", r"nettoyage", r"r[ée]paration",
                      r"intervention", r"\bwash", r"replace", r"calibrat"]),
    ("test", [r"\bessai", r"jaugeage", r"\btest\b"]),
    ("routine", [r"routine", r"relev[ée]", r"contr[ôo]le", r"[ée]chantillon", r"visite", r"\bronde", r"inspection"]),
]


# ---------------------------------------------------------------------- reading
def read_table(path):
    """(header, rows) from a CSV export of any usual shape."""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    best = None
    for i, line in enumerate(lines[:50]):
        for d in (";", "\t", ","):
            n = len(line.split(d))
            if n >= 2 and i + 1 < len(lines) and len(lines[i + 1].split(d)) == n:
                if best is None or n > best[2]:
                    best = (i, d, n)
        if best and best[0] == i:
            break
    if best is None:
        raise ValueError(f"{path}: no table found")
    i, d, _ = best
    rd = csv.reader(io.StringIO("\n".join(lines[i:])), delimiter=d)
    head = [h.strip() for h in next(rd)]
    return head, [r for r in rd if any(c.strip() for c in r)], {"encoding": enc, "delimiter": d, "skipped_lines": i}


def parse_time(s: str) -> datetime:
    s = s.strip().replace("T", " ").rstrip("Z")
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    if "." in s:     # fractional seconds
        return parse_time(s.split(".")[0])
    raise ValueError(f"unrecognised date '{s}'")


def parse_value(s: str):
    s = (s or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:            # 1.234,56 or 1,234.56
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None                          # "Comm Failure", "Bad Input"...


def _col(head, names):
    low = [h.lower() for h in head]
    for n in names:
        for i, h in enumerate(low):
            if h == n or h.startswith(n):
                return i
    return None


def read_export(path):
    """{tag: [(time, value or None, good)]} and format info."""
    head, rows, info = read_table(path)
    ct = _col(head, ["tag", "nom", "name", "point", "variable", "repère", "repere"])
    ch = _col(head, ["horodatage", "timestamp", "date", "time", "heure"])
    cv = _col(head, ["valeur", "value", "val"])
    cq = _col(head, ["qualité", "qualite", "quality", "statut", "status", "etat", "état"])
    data = defaultdict(list)
    if ct is not None and ch is not None and cv is not None:
        info["format"] = "long"
        for r in rows:
            if len(r) <= max(ct, ch, cv):
                continue
            v = parse_value(r[cv])
            q = r[cq].strip().lower() if cq is not None and cq < len(r) else ""
            good = v is not None and (not q or q in ("good", "bon", "ok", "g", "192", "0", "valid", "valide"))
            data[r[ct].strip()].append((parse_time(r[ch]), v, good))
    else:
        info["format"] = "wide"
        ch = 0 if ch is None else ch
        for r in rows:
            t = parse_time(r[ch])
            for i, h in enumerate(head):
                if i == ch or i >= len(r):
                    continue
                v = parse_value(r[i])
                if r[i].strip() == "":
                    continue
                data[h].append((t, v, v is not None))
    for k in data:
        data[k].sort(key=lambda x: x[0])
    return dict(data), info


def read_taglist(path):
    """{tag: (description, unit)} from the instrumentation tag list, if there is one."""
    if not path:
        return {}
    head, rows, _ = read_table(path)
    ct = _col(head, ["tag", "nom", "name", "repère", "repere", "point"]) or 0
    cd = _col(head, ["description", "libellé", "libelle", "désignation", "designation", "desc"])
    cu = _col(head, ["unité", "unite", "unit", "eng"])
    out = {}
    for r in rows:
        if ct < len(r) and r[ct].strip():
            out[r[ct].strip()] = (r[cd].strip() if cd is not None and cd < len(r) else "",
                                  r[cu].strip() if cu is not None and cu < len(r) else "")
    return out


# ---------------------------------------------------------------------- mapping
def _number(s):
    m = re.findall(r"\d+", s)
    return int(m[-1]) if m else None


def entity_of(text: str, cfg) -> str | None:
    """Well / manifold / repeater id from a name written by a person or in a tag."""
    t = text.strip()
    ids = [w.id for w in cfg.wells] + [m.id for m in cfg.manifolds]
    reps = sorted({w.repeater for w in cfg.wells})
    for i in ids + reps:
        if i.lower() == t.lower():
            return i
    tl = t.lower()
    m = re.search(r"(r[ée]p[ée]teur|repeater)\s*-?\s*([a-z0-9]+)", tl)
    if m:
        cand = m.group(2).upper()
        for r in reps:
            if r.upper() in (cand, "R" + cand):
                return r
    m = re.search(r"(manifold|collecteur)\s*-?\s*([a-z])\b", tl) or re.match(r"^m-?([a-z])\b", tl) or re.match(r"^m([a-z])-", tl)
    if m:
        letter = m.group(m.lastindex).upper()
        for mm in cfg.manifolds:
            if mm.id.upper().endswith(letter):
                return mm.id
    n = None
    m = re.search(r"(puits|well|^p)\s*-?\s*0*(\d{1,3})", tl)
    if m:
        n = int(m.group(2))
    if n is not None:
        for w in cfg.wells:
            if _number(w.id) == n:
                return w.id
    return None


def guess_mapping(tags, taglist, cfg):
    """Draft mapping {entity: {signal: {tag, unit}}} and the tags left aside."""
    out, unknown = {}, []
    for tag in sorted(tags):
        desc, unit = taglist.get(tag, ("", ""))
        text = f"{desc} {tag}"
        sig = next((s for s, pats in GUESS if any(re.search(p, text, re.I) for p in pats)), None)
        ent = entity_of(desc, cfg) if desc else None
        if ent is None:
            ent = entity_of(tag.split("-")[0].split("_")[0], cfg) or entity_of(tag, cfg)
        if sig == "p_man" or (ent and ent.startswith(tuple(m.id for m in cfg.manifolds))):
            if ent not in {m.id for m in cfg.manifolds}:
                ent = entity_of(desc or tag, cfg)
            sig = "p_man" if sig in (None, "p_man", "whp") else sig
        if not sig or not ent:
            unknown.append(tag)
            continue
        out.setdefault(ent, {})[sig] = {"tag": tag, "unit": unit or "bar"}
    return out, unknown


def load_mapping(path):
    with open(path, encoding="utf-8") as f:
        m = yaml.safe_load(f) or {}
    out = {}
    for ent, sigs in (m.get("tags") or m).items():
        for sig, v in (sigs or {}).items():
            out.setdefault(ent, {})[sig] = v if isinstance(v, dict) else {"tag": str(v), "unit": "bar"}
    return out


def write_mapping(path, mapping, unknown):
    lines = ["# Correspondance tags du site -> signaux de l'agent. PROPOSITION AUTOMATIQUE : à vérifier.",
             "# unit : bar, psi, kPa, MPa, kg/cm2, °C, V. Supprimer une ligne fausse plutôt que de la laisser.", "tags:"]
    for ent in sorted(mapping):
        lines.append(f"  {ent}:")
        for sig, v in sorted(mapping[ent].items()):
            lines.append(f"    {sig}: {{tag: \"{v['tag']}\", unit: \"{v['unit']}\"}}")
    if unknown:
        lines.append("# Tags non reconnus (ignorés) :")
        lines += [f"#   {t}" for t in unknown]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------- cleaning
def clean_series(pts, sig, unit):
    """Convert, then drop sentinels, out-of-range values, spikes and frozen runs.
    Returns (points [(t, v or None)], stats). None marks the start of a bad period."""
    f = UNITS.get((unit or "bar").strip().lower())
    st = {"points": len(pts), "bad_quality": 0, "sentinel": 0, "out_of_range": 0, "spike": 0, "frozen_points": 0,
          "duplicates": 0, "unit": unit or "bar", "unknown_unit": f is None}
    if f is None:
        f = 1.0
    seen, uniq = set(), []
    for p in pts:
        k = (p[0], p[1])
        if k in seen:
            st["duplicates"] += 1
            continue
        seen.add(k)
        uniq.append(p)
    lo, hi = RANGE.get(sig, (-1e9, 1e9))
    out = []
    for t, v, good in uniq:
        if not good or v is None:
            st["bad_quality"] += 1
            out.append((t, None))
            continue
        if v in SENTINELS:
            st["sentinel"] += 1
            continue
        if unit and unit.strip().lower() in ("°f", "degf", "f"):
            v = (v - 32) / 1.8
        else:
            v = v * f
        if not lo <= v <= hi:
            st["out_of_range"] += 1
            continue
        out.append((t, v))
    # spikes: far from both neighbours, which agree with each other
    vals = [v for _, v in out if v is not None]
    if len(vals) > 20:
        diffs = [abs(b - a) for a, b in zip(vals, vals[1:])]
        mad = statistics.median(diffs) or 0.01
        thr = max(12 * mad, 0.05 * (statistics.median([abs(v) for v in vals]) or 1), 0.5)
        keep = []
        for i, (t, v) in enumerate(out):
            if v is not None and 0 < i < len(out) - 1 and out[i - 1][1] is not None and out[i + 1][1] is not None:
                a, b = out[i - 1][1], out[i + 1][1]
                if abs(v - a) > thr and abs(v - b) > thr and abs(a - b) < thr:
                    st["spike"] += 1
                    continue
            keep.append((t, v))
        out = keep
    # stuck transmitter: the very same value archived again and again for 24 h or more
    res, i = [], 0
    while i < len(out):
        j = i
        while j + 1 < len(out) and out[j + 1][1] is not None and out[i][1] is not None and out[j + 1][1] == out[i][1]:
            j += 1
        if out[i][1] is not None and j - i >= 12 and (out[j][0] - out[i][0]) >= timedelta(hours=24):
            st["frozen_points"] += j - i + 1
            st.setdefault("frozen_periods", []).append((out[i][0].isoformat(" ", "minutes"), out[j][0].isoformat(" ", "minutes")))
            res.append((out[i][0], None))
        else:
            res.extend(out[i:j + 1])
        i = j + 1
    return res, st


def resample(pts, grid, max_age, lag=timedelta(minutes=5)):
    """Value at each grid time: last good value if recent enough and not followed by a bad marker.
    Archive times lag the measurement by seconds to minutes (03:00:32 is the 03:00 value): values
    archived up to `lag` after a grid time belong to it."""
    ts = [t for t, _ in pts]
    out = []
    for g in grid:
        k = bisect_right(ts, g + lag) - 1
        if k < 0:
            out.append(None)
            continue
        t, v = pts[k]
        out.append(v if v is not None and g - t <= max_age else None)
    return out


# ---------------------------------------------------------------------- events
def classify(text: str) -> str | None:
    t = text.lower()
    for typ, pats in EV_RULES:
        if any(re.search(p, t) for p in pats):
            return typ
    return None


def read_events(path, cfg):
    head, rows, info = read_table(path)
    cd = _col(head, ["date", "jour", "datetime", "horodatage"])
    ch = _col(head, ["heure", "hour", "time"])
    ce = _col(head, ["puits", "équipement", "equipement", "equipment", "well", "ouvrage", "installation"])
    cw = _col(head, ["evènement", "événement", "evenement", "event", "opération", "operation", "nature", "type"])
    co = _col(head, ["observation", "commentaire", "remarque", "description", "comment", "détail", "detail"])
    out, review = [], []
    for r in rows:
        try:
            ds = r[cd].strip()
            if ch is not None and ch != cd and ch < len(r) and r[ch].strip():
                ds += " " + r[ch].strip()
            t = parse_time(ds)
        except (ValueError, IndexError, TypeError):
            review.append(("date", r))
            continue
        what = r[cw].strip() if cw is not None and cw < len(r) else ""
        obs = r[co].strip() if co is not None and co < len(r) else ""
        eq_txt = r[ce].strip() if ce is not None and ce < len(r) else ""
        eq = entity_of(eq_txt, cfg) or entity_of(f"{what} {obs}", cfg)
        typ = classify(what) or classify(obs)
        text = what + (f" - {obs}" if obs and obs.lower() not in what.lower() else "")
        if eq is None or typ is None:
            review.append(("équipement" if eq is None else "type", r))
            if eq is None:
                continue
            typ = "planned"
        if typ in ("routine", "test_end"):
            typ = "planned"
        out.append((t, eq, typ, text))
    out.sort()
    return out, review, info


# ---------------------------------------------------------------------- main
def run(cfg, data_path, out_dir, mapping_path=None, events_path=None, taglist_path=None, interval=3600, max_age_h=6.0,
        guess_only=False):
    os.makedirs(out_dir, exist_ok=True)
    data, info = read_export(data_path)
    taglist = read_taglist(taglist_path)
    if guess_only or not mapping_path:
        mapping, unknown = guess_mapping(data.keys(), taglist, cfg)
        mp = os.path.join(out_dir, "mapping.yaml")
        write_mapping(mp, mapping, unknown)
        if guess_only:
            return {"mapping": mp, "mapped": sum(len(v) for v in mapping.values()), "unknown": unknown, "info": info}
    else:
        mapping = load_mapping(mapping_path)
        unknown = [t for t in data if t not in {v["tag"] for s in mapping.values() for v in s.values()}]
    t0 = min(p[0][0] for p in data.values() if p)
    t1 = max(p[-1][0] for p in data.values() if p)
    start = t0.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=interval)
    grid = []
    g = start
    while g <= t1:
        grid.append(g)
        g += timedelta(seconds=interval)
    cols, quality = [], []
    max_age = timedelta(hours=max_age_h)
    known_ids = {w.id for w in cfg.wells} | {m.id for m in cfg.manifolds}
    for ent in sorted(mapping):
        if ent not in known_ids:
            quality.append({"entity": ent, "signal": "-", "tag": "-", "error": "équipement absent de la configuration"})
            continue
        for sig, v in sorted(mapping[ent].items()):
            pts = data.get(v["tag"])
            if not pts:
                quality.append({"entity": ent, "signal": sig, "tag": v["tag"], "error": "tag absent de l'export"})
                continue
            clean, st = clean_series(pts, sig, v.get("unit"))
            vals = resample(clean, grid, max_age)
            st.update(entity=ent, signal=sig, tag=v["tag"], coverage=round(100 * sum(x is not None for x in vals) / max(1, len(vals)), 1))
            quality.append(st)
            cols.append((f"{ent}.{sig}", vals))
    exp = os.path.join(out_dir, "export.csv")
    with open(exp, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["timestamp"] + [c for c, _ in cols])
        for i, g in enumerate(grid):
            wr.writerow([g.strftime("%Y-%m-%d %H:%M:%S")] + ["" if v[i] is None else f"{v[i]:.3f}" for _, v in cols])
    res = {"export": exp, "info": info, "grid": len(grid), "interval_s": interval, "start": grid[0].isoformat(" ") if grid else None,
           "end": grid[-1].isoformat(" ") if grid else None, "quality": quality, "unknown_tags": unknown}
    if events_path:
        evs, review, einfo = read_events(events_path, cfg)
        ep = os.path.join(out_dir, "events.csv")
        with open(ep, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["datetime", "equipment", "type", "description"])
            for t, eq, typ, text in evs:
                wr.writerow([t.strftime("%Y-%m-%d %H:%M"), eq, typ, text])
        counts = defaultdict(int)
        for e in evs:
            counts[e[2]] += 1
        res.update(events=ep, event_counts=dict(counts), review=[(why, ";".join(r)) for why, r in review])
    with open(os.path.join(out_dir, "quality.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(out_dir, "quality.html"), "w", encoding="utf-8") as f:
        f.write(quality_html(res, cfg))
    return res


def summary_text(res) -> str:
    q = [x for x in res["quality"] if "error" not in x]
    lines = [f"Format {res['info'].get('format')} ({res['info'].get('encoding')}, séparateur '{res['info'].get('delimiter')}'), "
             f"{len(q)} signaux importés, grille de {res['interval_s']} s du {res['start']} au {res['end']}."]
    tot = lambda k: sum(x.get(k, 0) for x in q)
    lines.append(f"Nettoyage : {tot('bad_quality')} valeurs de mauvaise qualité, {tot('sentinel')} valeurs sentinelles, "
                 f"{tot('out_of_range')} hors plage, {tot('spike')} pics, {tot('frozen_points')} valeurs figées, {tot('duplicates')} doublons.")
    low = [f"{x['entity']}.{x['signal']} ({x['coverage']} %)" for x in q if x["coverage"] < 90]
    if low:
        lines.append("Couverture faible : " + ", ".join(low))
    for x in q:
        for a, b in x.get("frozen_periods", []):
            lines.append(f"Transmetteur figé : {x['tag']} ({x['entity']}.{x['signal']}) du {a} au {b}")
    conv = sorted({f"{x['tag']} {x['unit']}→bar" for x in q if x["unit"].lower() not in ("bar", "barg", "°c", "v", "")})
    if conv:
        lines.append(f"Unités converties : {len(conv)} tags ({', '.join(conv[:4])}{'…' if len(conv) > 4 else ''})")
    err = [x for x in res["quality"] if "error" in x]
    for x in err:
        lines.append(f"PROBLÈME : {x['entity']}.{x['signal']} {x['tag']} : {x['error']}")
    if res.get("unknown_tags"):
        lines.append(f"Tags non utilisés : {len(res['unknown_tags'])}")
    if "event_counts" in res:
        lines.append("Événements : " + ", ".join(f"{k} {v}" for k, v in sorted(res["event_counts"].items())))
        if res["review"]:
            lines.append(f"Lignes du journal à vérifier à la main : {len(res['review'])}")
    return "\n".join(lines)


def quality_html(res, cfg) -> str:
    e = html.escape
    rows = []
    for x in res["quality"]:
        if "error" in x:
            rows.append(f"<tr class=bad><td>{e(x['entity'])}</td><td>{e(x['signal'])}</td><td>{e(x['tag'])}</td><td colspan=8>{e(x['error'])}</td></tr>")
            continue
        fz = "; ".join(f"{a} → {b}" for a, b in x.get("frozen_periods", []))
        rows.append(f"<tr class='{'warn' if x['coverage'] < 90 or fz else ''}'><td>{e(x['entity'])}</td><td>{e(x['signal'])}</td><td>{e(x['tag'])}</td>"
                    f"<td>{e(x['unit'])}</td><td class=n>{x['points']}</td><td class=n>{x['coverage']} %</td><td class=n>{x['bad_quality']}</td>"
                    f"<td class=n>{x['sentinel'] + x['out_of_range']}</td><td class=n>{x['spike']}</td><td class=n>{x['duplicates']}</td><td>{e(fz)}</td></tr>")
    rev = "".join(f"<li><b>{e(w)}</b> : {e(r)}</li>" for w, r in res.get("review", [])[:200])
    summ = e(summary_text(res)).replace("\n", "<br>")
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qualité des données importées</title><style>
body{{font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:0;background:#f4f3ef;color:#1d1d1b}} main{{max-width:1200px;margin:0 auto;padding:24px 16px}}
table{{width:100%;border-collapse:collapse;background:#fff}} th,td{{padding:6px 8px;border-bottom:1px solid #e5e3dc;text-align:left;vertical-align:top}}
th{{background:#faf9f6;font-size:12px}} td.n{{text-align:right;font-variant-numeric:tabular-nums}} tr.warn td{{background:#fdf6e3}} tr.bad td{{background:#fbe4e4}}
.box{{background:#fff;border-left:4px solid #8a8577;padding:10px 14px;margin:14px 0}} .wrap{{overflow-x:auto}}
</style></head><body><main><h1>Qualité des données importées</h1><p>{e(cfg.site_name)} · {e(os.path.basename(res['export']))}</p>
<div class=box>{summ}</div>
<h2>Par signal</h2><div class=wrap><table><tr><th>Équipement</th><th>Signal</th><th>Tag</th><th>Unité</th><th>Points</th><th>Couverture</th>
<th>Mauvaise qualité</th><th>Sentinelles / hors plage</th><th>Pics</th><th>Doublons</th><th>Figé</th></tr>{''.join(rows)}</table></div>
{f"<h2>Lignes du journal à vérifier</h2><ul>{rev}</ul>" if rev else ""}
<p style="color:#66645e">Chaque valeur retirée est comptée ici ; aucune n'est inventée. Les trous restent des trous.</p></main></body></html>"""
