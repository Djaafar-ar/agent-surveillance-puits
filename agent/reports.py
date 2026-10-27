"""Daily report, shift handover and long-term pressure decline.

The daily report also works as a heartbeat: if the on-call team does not
receive it at the usual hour, the agent itself is down.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime

from .config import secret
from .engine import mean

log = logging.getLogger("agent.reports")


def _linreg(xs, ys):
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    s = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    return s


def decline(store, well) -> dict | None:
    """Slopes in bar per month over 30 and 60 days, from daily averages,
    excluding days with a shut-in. Needs at least 21 days of history."""
    rows = [r for r in store.daily(well, 400) if not r[4] and r[1] is not None]
    if len(rows) < 21:
        return None
    days = [datetime.strptime(r[0], "%Y-%m-%d").toordinal() for r in rows]
    out = {"n_days": len(rows), "p_now": mean(r[1] for r in rows[-5:])}
    for win in (30, 60):
        sel = [(d, r) for d, r in zip(days, rows) if d >= days[-1] - win]
        if len(sel) >= 10:
            out[f"s{win}"] = _linreg([d for d, _ in sel], [r[1] for _, r in sel]) * 30.4
            ts = [(d, r[2]) for d, r in sel if r[2] is not None]
            out[f"t{win}"] = _linreg([d for d, _ in ts], [t for _, t in ts]) * 30.4 if len(ts) >= 10 else None
    s60, t60 = out.get("s60"), out.get("t60")
    if s60 is None:
        out["cls"] = "?"
    elif s60 < -0.8 and t60 is not None and abs(t60) < 0.12:
        out["cls"] = "sensor_drift"
    elif s60 < -1.5:
        out["cls"] = "fast"
    elif s60 < -0.6:
        out["cls"] = "marked"
    elif s60 < -0.15:
        out["cls"] = "slow"
    else:
        out["cls"] = "stable"
    return out


CLS = {"fr": {"sensor_drift": "dérive capteur probable (pression en baisse, température stable)", "fast": "déclin rapide",
              "marked": "déclin marqué", "slow": "déclin lent", "stable": "stable", "?": "historique insuffisant"},
       "en": {"sensor_drift": "probable sensor drift (pressure falling, temperature flat)", "fast": "fast decline",
              "marked": "marked decline", "slow": "slow decline", "stable": "stable", "?": "not enough history"}}


def daily_report(engine, store, cfg, hours=24) -> tuple[str, str]:
    L = cfg.language
    fr = L == "fr"
    now = engine.now
    since = now - hours * 3600
    P = engine.perf(since)
    with engine.lock:
        al = [a for a in engine.alarms if a.t0 >= since or a.t_end is None]
        roots = [a for a in al if a.parent is None]
        wells = list(engine.wells.values())
    d = datetime.fromtimestamp(now)
    lines = []
    if fr:
        lines += [f"Site : {cfg.site_name}", f"Période : {datetime.fromtimestamp(since):%d/%m/%Y %H:%M} au {d:%d/%m/%Y %H:%M}", "",
                  "Synthèse",
                  f"{len(wells)} puits surveillés. {sum(1 for w in wells if w.alarms)} en alarme en ce moment.",
                  f"{len(roots)} alarme(s) présentées à l'opérateur pour {P['raw']} détections ({P['reduction']:.0f} % regroupées).",
                  f"Taux moyen {P['rate_vis']:.2f} alarme(s) / 10 min (cible EEMUA 191 : 1). Pic {P['peak_vis']} / 10 min (inondation au-delà de 10).",
                  f"Délai moyen d'acquittement : {P['ack_avg_min']:.0f} min." if P["ack_avg_min"] is not None else "Aucune alarme acquittée.",
                  f"Production différée (puits fermés) : {sum(w.defer_m3 for w in wells):.1f} m³.", "", "Alarmes"]
    else:
        lines += [f"Site: {cfg.site_name}", f"Period: {datetime.fromtimestamp(since):%d/%m/%Y %H:%M} to {d:%d/%m/%Y %H:%M}", "",
                  "Summary",
                  f"{len(wells)} wells monitored. {sum(1 for w in wells if w.alarms)} in alarm now.",
                  f"{len(roots)} alarm(s) shown to the operator for {P['raw']} detections ({P['reduction']:.0f} % grouped).",
                  f"Average rate {P['rate_vis']:.2f} alarm(s) / 10 min (EEMUA 191 target: 1). Peak {P['peak_vis']} / 10 min (flood above 10).",
                  f"Mean time to acknowledge: {P['ack_avg_min']:.0f} min." if P["ack_avg_min"] is not None else "No alarm acknowledged.",
                  f"Deferred production (shut-in wells): {sum(w.defer_m3 for w in wells):.1f} m³.", "", "Alarms"]
    if not roots:
        lines.append("-")
    for a in roots:
        st = (f"{'acquittée par' if fr else 'acknowledged by'} {cfg.roster[a.ack[0]].name}" if a.ack else
              ("non acquittée" if fr else "not acknowledged") if a.severity == "urgence" else ("e-mail seulement" if fr else "e-mail only"))
        end = (f"{'terminée à' if fr else 'cleared at'} {datetime.fromtimestamp(a.t_end):%H:%M}" if a.t_end else ("toujours active" if fr else "still active"))
        grp = f", {len(a.children)} {'regroupée(s)' if fr else 'grouped'}" if a.children else ""
        lines.append(f"- {datetime.fromtimestamp(a.t0):%H:%M} {a.entity} : {a.title} ({st}{grp}, {end})")
    lines += ["", "Actions recommandées" if fr else "Recommended actions"]
    acts = [f"- {a.entity} : {a.action}" for a in roots if a.t_end is None]
    lines += acts or ["-"]
    lines += ["", "Tendances long terme (60 jours)" if fr else "Long-term trends (60 days)"]
    for w in wells:
        r = decline(store, w.id) if store else None
        if r:
            s = f"{r['s60']:+.1f} bar/{'mois' if fr else 'month'}" if r.get("s60") is not None else ""
            lines.append(f"- {w.id} : {CLS[L][r['cls']]} {s}")
    if lines[-1].startswith("Tendances") or lines[-1].startswith("Long-term"):
        lines.append("Historique de moins de 21 jours : analyse disponible plus tard." if fr else "Less than 21 days of history: analysis available later.")
    lines += ["", "État par puits" if fr else "Well status"]
    for w in wells:
        stt = ", ".join(a.title for a in w.alarms.values()) or ("normal")
        p = w.cur.get("whp")
        lines.append(f"- {w.id} ({w.cfg.manifold}) : {stt}" + (f", {p:.1f} Barg" if isinstance(p, float) else ""))
    lines += ["", ("Rapport automatique. Si vous ne recevez pas ce rapport à l'heure habituelle, l'agent est arrêté."
                   if fr else "Automatic report. If you do not receive it at the usual time, the agent is down.")]
    body = "\n".join(lines)
    summary = llm_summary(cfg, body)
    if summary:
        body = ("Synthèse\n" if fr else "Summary\n") + summary + "\n\n" + body
    subject = (f"Rapport journalier {cfg.site_name} du {d:%d/%m/%Y}" if fr else f"Daily report {cfg.site_name} {d:%d/%m/%Y}")
    if hours != 24:
        subject = (f"Relève de quart {cfg.site_name} {d:%d/%m/%Y %H:%M}" if fr else f"Shift handover {cfg.site_name} {d:%d/%m/%Y %H:%M}")
    return subject, body


def llm_summary(cfg, body: str) -> str:
    """Optional: a short narrative written by a language model, on top of the
    rule-based report. OpenAI-compatible API, e.g. a local Ollama server
    (url: http://localhost:11434/v1/chat/completions). Off by default; the
    report is complete without it, and failures are ignored."""
    llm = cfg.llm or {}
    if not llm.get("enabled"):
        return ""
    prompt = ("Tu es ingénieur production. Résume ce rapport de surveillance en 100 mots, texte brut, "
              "les trois points importants puis la priorité du jour." if cfg.language == "fr" else
              "You are a production engineer. Summarise this surveillance report in 100 words, plain text, "
              "the three key points then today's priority.")
    req = {"model": llm.get("model", "llama3.1"), "messages": [{"role": "system", "content": prompt},
                                                             {"role": "user", "content": body[:12000]}], "temperature": 0.2}
    headers = {"Content-Type": "application/json"}
    key = secret(llm.get("api_key_env"))
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        r = urllib.request.urlopen(urllib.request.Request(llm["url"], data=json.dumps(req).encode(), headers=headers), timeout=60)
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        log.warning("LLM summary skipped: %s", e)
        return ""
