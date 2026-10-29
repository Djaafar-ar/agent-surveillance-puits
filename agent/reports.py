"""Daily report, shift handover and long-term pressure decline.

The daily report also works as a heartbeat: if the on-call team does not
receive it at the usual hour, the agent itself is down.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta

from .analysis import SEV_LAB, TEAM_LAB, TEAMS, Analyst
from .config import secret
from .engine import mean

log = logging.getLogger("agent.reports")


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
    lines += ["", "Analyse long terme : priorités" if fr else "Long-term analysis: priorities"]
    found = Analyst(cfg, store, engine).run() if store else []
    for f_ in found[:5]:
        lines.append(f"- [{SEV_LAB[L][f_.severity]}] {f_.entity} : {f_.title}. {f_.action}")
    if not found:
        lines.append("Rien à signaler, ou moins de 21 jours d'historique." if fr else "Nothing to report, or less than 21 days of history.")
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


def weekly_report(engine, store, cfg, interventions=None):
    """Weekly analysis report for the supervisor, and one work list per team.
    Returns (subject, body, {team: (subject, body)})."""
    L = cfg.language
    fr = L == "fr"
    an = Analyst(cfg, store, engine, interventions)
    found = an.run()
    end = store.last_day() or datetime.now().date()
    teams_cfg = (cfg.analysis or {}).get("teams", {})
    lines = []
    lines += [f"{'Site' if fr else 'Site'} : {cfg.site_name}",
              (f"Analyse de l'historique jusqu'au {end:%d/%m/%Y}." if fr else f"Analysis of the history up to {end:%d/%m/%Y}."), ""]
    urgent = [f for f in found if f.severity == 3]
    lines.append(("Résumé : " if fr else "Summary: ") + (
        f"{len(found)} point(s), dont {len(urgent)} urgent(s)." if fr else f"{len(found)} item(s), {len(urgent)} urgent."))
    lines += ["", "Priorités" if fr else "Priorities"]
    if not found:
        lines.append("Rien à signaler." if fr else "Nothing to report.")
    for i, f_ in enumerate(found[:12], 1):
        due = ""
        if f_.due_days is not None:
            due = (f" — échéance estimée : {(end + timedelta(days=f_.due_days)):%d/%m/%Y}" if fr
                   else f" — estimated deadline: {(end + timedelta(days=f_.due_days)):%d/%m/%Y}")
        dest = ", ".join(teams_cfg.get(f_.team, [])) or "-"
        lines += [f"{i}. [{SEV_LAB[L][f_.severity]}] {f_.entity} — {f_.title}{due}",
                  f"   {'Constat' if fr else 'Evidence'} : {f_.evidence}",
                  f"   {'Action proposée' if fr else 'Proposed action'} : {f_.action}",
                  f"   {'Équipe' if fr else 'Team'} : {TEAM_LAB[L][f_.team]} ({dest})", ""]
    # per team
    per_team = {}
    lines += ["Par équipe" if fr else "By team"]
    for t in TEAMS:
        items = [f for f in found if f.team == t]
        if not items:
            continue
        lines.append(f"- {TEAM_LAB[L][t]} : " + "; ".join(f"{f.entity} ({f.title.lower()})" for f in items))
        body = [(f"Liste de travail proposée par l'agent de surveillance pour l'équipe {TEAM_LAB[L][t]}, {cfg.site_name}." if fr
                 else f"Work list proposed by the surveillance agent for the {TEAM_LAB[L][t]} team, {cfg.site_name}."), ""]
        for f_ in items:
            body += [f"- {f_.entity} : {f_.title}", f"  {f_.evidence}", f"  {'Action' if fr else 'Action'} : {f_.action}", ""]
        body.append("Proposition automatique à valider par le superviseur." if fr else "Automatic proposal, to be validated by the supervisor.")
        per_team[t] = ((f"[Agent] Travaux proposés {TEAM_LAB[L][t]} — {cfg.site_name}" if fr
                        else f"[Agent] Proposed work {TEAM_LAB[L][t]} — {cfg.site_name}"), "\n".join(body))
    # alarm system over 7 days
    since = (datetime.combine(end - timedelta(days=7), datetime.min.time())).timestamp()
    hist = store.alarm_history(since)
    roots = [h for h in hist if h[5] is None]
    lines += ["", "Système d'alarmes (7 jours)" if fr else "Alarm system (7 days)",
              (f"{len(roots)} alarme(s) présentée(s) pour {len(hist)} détection(s), soit {len(roots) / (7 * 144):.2f} par 10 min (cible EEMUA 191 : moins de 1)."
               if fr else f"{len(roots)} alarm(s) shown for {len(hist)} detection(s), {len(roots) / (7 * 144):.2f} per 10 min (EEMUA 191 target: below 1).")]
    # deferred production
    lines += ["", "Production différée" if fr else "Deferred production"]
    tot7 = tot30 = 0.0
    top = []
    for w in cfg.wells:
        d7 = sum(v or 0 for d, v in store.series(w.id, "defer_m3", 7, "total"))
        d30 = sum(v or 0 for d, v in store.series(w.id, "defer_m3", 30, "total"))
        tot7 += d7
        tot30 += d30
        if d30 > 0.5:
            top.append((d30, w.id))
    lines.append((f"{tot7:.0f} m³ sur 7 jours, {tot30:.0f} m³ sur 30 jours, soit environ {tot30 * 6.2898 * cfg.oil_price_usd:,.0f} USD sur 30 jours."
                   if fr else f"{tot7:.0f} m³ over 7 days, {tot30:.0f} m³ over 30 days, about {tot30 * 6.2898 * cfg.oil_price_usd:,.0f} USD over 30 days.").replace(",", " "))
    for d30, wid in sorted(top, reverse=True)[:5]:
        lines.append(f"- {wid} : {d30:.0f} m³")
    lines += ["", ("L'agent propose, l'équipe décide : chaque point est une recommandation fondée sur les données, à valider sur le terrain. "
                   "Données lues en lecture seule ; aucune commande n'est envoyée au SCADA." if fr else
                   "The agent proposes, the team decides: each item is a data-based recommendation, to be checked in the field. "
                   "Data read only; no command is sent to the SCADA.")]
    body = "\n".join(lines)
    summary = llm_summary(cfg, body)
    if summary:
        body = ("Synthèse\n" if fr else "Summary\n") + summary + "\n\n" + body
    subject = (f"Rapport hebdomadaire d'analyse {cfg.site_name} — {len(urgent)} urgent(s)" if fr
               else f"Weekly analysis report {cfg.site_name} — {len(urgent)} urgent")
    return subject, body, per_team, found
