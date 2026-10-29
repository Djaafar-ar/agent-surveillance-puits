"""Ask the agent: questions in plain language, answered from the agent's own data.

    "Qu'est-ce qui s'est passé sur le puits 7 cette nuit ?"
    "Quels puits sont fermés et pourquoi ?"   "Production différée hier ?"
    "Quels essais sont en retard ?"           "Ordres de travail en retard ?"

Three modes (assistant.mode in the configuration):

  off     no assistant
  rules   no AI at all: the question is matched to a few known requests (well,
          alarms, production, tests, work orders, handover, priorities) and the
          answer is built from the data. Predictable, nothing to install.
  local   the same facts are collected, then an AI model RUNNING ON THE SITE
          (Ollama, on the agent PC or a server of the site network) writes the
          answer. The configuration refuses any address outside the site network:
          well data never leaves the site. If the model does not answer, the
          rules answer is given instead.

In every mode the assistant only reads. It cannot acknowledge, change a status
or send anything, and it never sends a command to the SCADA.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger("agent.assistant")

KW = {
    "help": ("aide", "help", "que peux", "what can"),
    "handover": ("relève", "releve", "handover", "quart", "shift"),
    "production": ("production", "différée", "differee", "deferred", "huile", "oil", "m3", "m³"),
    "tests": ("essai", "test"),
    "workorders": ("ordre", "ot ", "ot?", "travaux", "work order", "intervention", "job"),
    "priorities": ("priorit", "analyse", "tendance", "prévision", "prevision", "forecast", "trend", "long terme", "semaine prochaine"),
    "shut": ("fermé", "ferme", "shut", "arrêt", "arret", "hors production"),
    "alarms": ("alarme", "alarm", "problème", "probleme", "problem", "panne", "incident", "nuit", "night", "hier", "yesterday",
               "aujourd", "today", "passé", "happened"),
}
HELP = {"fr": "Je réponds à partir des données de l'agent. Exemples : « puits 7 cette nuit », « alarmes en cours », "
              "« quels puits sont fermés », « production différée hier », « essais en retard », « ordres de travail », "
              "« priorités de la semaine », « relève ».",
        "en": "I answer from the agent's data. Examples: \"well 7 last night\", \"active alarms\", \"which wells are shut in\", "
              "\"deferred production yesterday\", \"overdue tests\", \"work orders\", \"this week's priorities\", \"handover\"."}

SYSTEM = {"fr": ("Tu es l'assistant de la salle de contrôle d'un champ pétrolier. Réponds en français, en quelques phrases, "
                 "UNIQUEMENT à partir des FAITS fournis. Donne les chiffres et les heures exacts des faits. Si les faits ne "
                 "permettent pas de répondre, dis-le. N'invente rien. Ne propose jamais d'envoyer une commande au SCADA."),
          "en": ("You are the control-room assistant of an oil field. Answer in English, in a few sentences, ONLY from the "
                 "FACTS given. Use the exact figures and times from the facts. If the facts do not answer the question, say "
                 "so. Do not make anything up. Never suggest sending a command to the SCADA.")}


VAL_LAB = {"fr": {"whp": ("pression tête", "Barg"), "line_p": ("pression ligne", "Barg"), "ann": ("annulaire", "Barg"),
                  "oil_rate": ("huile", "m³/j"), "battery": ("batterie RTU", "V")},
           "en": {"whp": ("wellhead pressure", "barg"), "line_p": ("line pressure", "barg"), "ann": ("annulus", "barg"),
                  "oil_rate": ("oil", "m3/d"), "battery": ("RTU battery", "V")}}
TYPE_LAB = {"fr": {"gas_lift": "gas lift", "natural": "éruptif", "natural_water": "éruptif avec eau de dessalage"},
            "en": {"gas_lift": "gas lift", "natural": "natural flow", "natural_water": "natural flow with desalting water"}}
WO_LAB = {"fr": {"open": "ouvert", "acknowledged": "pris en charge", "done": "fait, en vérification", "verified": "vérifié",
                 "reopened": "rouvert", "cancelled": "annulé"},
          "en": {"open": "open", "acknowledged": "taken", "done": "done, being checked", "verified": "verified",
                 "reopened": "reopened", "cancelled": "cancelled"}}


class Assistant:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.L = app.cfg.language
        self.c = app.cfg.assistant

    # ------------------------------------------------------------------ parsing
    def wells_in(self, q: str) -> list[str]:
        ql = q.lower()
        found = [w.id for w in self.cfg.wells if w.id.lower() in ql]
        for m in re.finditer(r"(?:puits|well|p)\s*-?\s*0*(\d{1,3})\b", ql):
            n = int(m.group(1))
            for w in self.cfg.wells:
                digits = re.findall(r"\d+", w.id)
                if digits and int(digits[-1]) == n and w.id not in found:
                    found.append(w.id)
        return found

    def window(self, q: str) -> tuple[float, float, str]:
        now = self.app.engine.now
        d = datetime.fromtimestamp(now)
        ql = q.lower()
        fr = self.L == "fr"
        if any(k in ql for k in ("cette nuit", "last night", "tonight", "nuit", "night")):
            start = datetime.combine(d.date() - timedelta(days=1 if d.hour < 18 else 0), datetime.min.time()).replace(hour=18)
            end = start + timedelta(hours=12)
            return start.timestamp(), min(end.timestamp(), now), ("cette nuit" if fr else "last night")
        if any(k in ql for k in ("hier", "yesterday")):
            s = datetime.combine(d.date() - timedelta(days=1), datetime.min.time())
            return s.timestamp(), (s + timedelta(days=1)).timestamp(), ("hier" if fr else "yesterday")
        if any(k in ql for k in ("semaine", "week", "7 jours", "7 days")):
            return now - 7 * 86400, now, ("sur 7 jours" if fr else "over 7 days")
        if any(k in ql for k in ("aujourd", "today")):
            return datetime.combine(d.date(), datetime.min.time()).timestamp(), now, ("aujourd'hui" if fr else "today")
        return now - 24 * 3600, now, ("sur 24 h" if fr else "over 24 h")

    def intent(self, q: str) -> str:
        ql = " " + q.lower() + " "
        for k in ("help", "handover", "production", "tests", "workorders", "priorities", "shut"):
            if any(w in ql for w in KW[k]):
                return k
        return "alarms"

    # ------------------------------------------------------------------ facts
    def facts(self, q: str) -> tuple[str, list[str], str]:
        """(intent, fact lines, period label)."""
        it = self.intent(q)
        wells = self.wells_in(q)
        t0, t1, per = self.window(q)
        fn = getattr(self, "_f_" + it)
        lines = []
        for wid in wells:
            lines += self._f_well(wid, t0, t1, per)
        if not wells or it not in ("alarms",):
            lines += fn(t0, t1, per, wells)
        return it, lines, per

    def _fmt_t(self, t):
        return datetime.fromtimestamp(t).strftime("%d/%m %H:%M")

    def _f_help(self, *a):
        return [HELP[self.L]]

    def _f_well(self, wid, t0, t1, per):
        fr = self.L == "fr"
        e = self.app.engine
        w = e.wells[wid]
        c = w.cur
        out = []
        st = w.manual or ("shut" if c.get("sdv_closed") else "test" if c.get("on_test") else "production")
        from .logbook import STATUS_LAB
        lab = VAL_LAB[self.L]
        vals = ", ".join(f"{lab[k][0]} {c[k]:.1f} {lab[k][1]}" for k in lab if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool))
        out.append(f"{wid} ({TYPE_LAB[self.L].get(w.cfg.type, w.cfg.type)}, {w.cfg.manifold}) : " + (f"{STATUS_LAB['fr'][st].lower()}" if fr else f"{STATUS_LAB['en'][st].lower()}")
                   + (f", valeurs actuelles {vals}" if fr else f", current values {vals}") if vals else
                   f"{wid} : " + ("pas de données reçues" if fr else "no data received"))
        if not w.fresh:
            out.append(f"{wid} : " + (f"RTU muette depuis {e.age_min(w):.0f} min" if fr else f"RTU silent for {e.age_min(w):.0f} min"))
        with e.lock:
            al = [a for a in e.alarms if a.entity == wid and (a.t_end is None or a.t_end >= t0) and a.t0 < t1]
        for a in al:
            end = (self._fmt_t(a.t_end) if a.t_end else ("en cours" if fr else "ongoing"))
            ack = (f", acquittée par {self.cfg.roster[a.ack[0]].name}" if fr else f", acknowledged by {self.cfg.roster[a.ack[0]].name}") if a.ack else ""
            out.append(f"{wid} alarme {a.title} ({a.value}) de {self._fmt_t(a.t0)} à {end}{ack}. Diagnostic : {a.diag}" if fr else
                       f"{wid} alarm {a.title} ({a.value}) from {self._fmt_t(a.t0)} to {end}{ack}. Diagnosis: {a.diag}")
        if not al:
            out.append(f"{wid} : " + (f"aucune alarme {per}" if fr else f"no alarm {per}"))
        lb = getattr(self.app, "logbook", None)
        if lb:
            for x in lb.entries(t0, t1, equipment=wid, limit=10):
                out.append(f"{wid} main courante {self._fmt_t(x['t'])} ({x['author']}) : {x['kind']} {x['status'] or ''} {x['text']}" if fr
                           else f"{wid} logbook {self._fmt_t(x['t'])} ({x['author']}): {x['kind']} {x['status'] or ''} {x['text']}")
        wo = getattr(self.app, "workorders", None)
        if wo:
            for o in wo.list(statuses=("open", "acknowledged", "reopened", "done")):
                if o["entity"] == wid:
                    out.append(f"{wid} ordre de travail #{o['id']} {WO_LAB['fr'][o['status']]} : {o['title']} (équipe {o['team']})" if fr else
                               f"{wid} work order #{o['id']} {WO_LAB['en'][o['status']]}: {o['title']} (team {o['team']})")
        for f in self._findings():
            if f["entity"] == wid:
                out.append(f"{wid} analyse long terme : {f['title']}. {f['evidence']}" if fr else f"{wid} long-term analysis: {f['title']}. {f['evidence']}")
        wt = getattr(self.app, "welltests", None)
        if wt:
            t = wt.list(wid, 1)
            if t:
                t = t[0]
                out.append(f"{wid} dernier essai {self._fmt_t(t['start_t'])} : {t['status']}" + (f", huile {t['oil_m3d']:.1f} m³/j" if t["oil_m3d"] else "")
                           if fr else f"{wid} last test {self._fmt_t(t['start_t'])}: {t['status']}" + (f", oil {t['oil_m3d']:.1f} m3/d" if t["oil_m3d"] else ""))
        return out

    def _findings(self):
        try:
            return self.app.analysis()
        except Exception:  # noqa: BLE001
            return []

    def _f_alarms(self, t0, t1, per, wells):
        fr = self.L == "fr"
        e = self.app.engine
        with e.lock:
            active = [a for a in e.alarms if a.parent is None and a.t_end is None]
            during = [a for a in e.alarms if a.parent is None and t0 <= a.t0 < t1]
        out = [(f"{len(active)} alarme(s) en cours, {len(during)} alarme(s) déclenchée(s) {per}." if fr
                else f"{len(active)} active alarm(s), {len(during)} alarm(s) raised {per}.")]
        for a in active:
            out.append((f"En cours : {a.entity} {a.title} ({a.value}) depuis {self._fmt_t(a.t0)}" if fr else
                        f"Active: {a.entity} {a.title} ({a.value}) since {self._fmt_t(a.t0)}") + ("" if a.ack else (", non acquittée" if fr else ", not acknowledged")))
        for a in during:
            if a.t_end:
                out.append(f"{self._fmt_t(a.t0)}-{self._fmt_t(a.t_end)} {a.entity} : {a.title} ({a.value})")
        return out

    def _f_shut(self, t0, t1, per, wells):
        fr = self.L == "fr"
        from .logbook import STATUS_LAB
        out = []
        lb = getattr(self.app, "logbook", None)
        for w in self.app.engine.wells.values():
            st = w.manual or ("shut" if w.cur.get("sdv_closed") else "test" if w.cur.get("on_test") else None)
            if st:
                c = lb.cause(w.id) if lb else ""
                out.append(f"{w.id} : {STATUS_LAB[self.L][st]}" + (f" ({c})" if c else (" (cause non écrite)" if fr and st == "shut" else
                                                                                        " (no reason written)" if st == "shut" else "")))
        return out or [("Tous les puits sont en production." if fr else "All wells are producing.")]

    def _f_production(self, t0, t1, per, wells):
        fr = self.L == "fr"
        pr = getattr(self.app, "production", None)
        if not pr:
            return []
        d = datetime.fromtimestamp(t0).date() if per in ("hier", "yesterday") else datetime.fromtimestamp(self.app.engine.now).date()
        r = pr.day(d)
        out = [(f"Production du {d:%d/%m/%Y} : huile {r['oil_m3']:.1f} m³, différée {r['defer_m3']:.1f} m³, {r['no_data_h']:.1f} heures-puits sans données."
                if fr else f"Production on {d:%d/%m/%Y}: oil {r['oil_m3']:.1f} m3, deferred {r['defer_m3']:.1f} m3, {r['no_data_h']:.1f} well-hours without data.")]
        for x in r["wells"]:
            if x["defer_m3"] > 0.05:
                out.append(f"{x['well']} : {x['defer_m3']:.1f} m³ " + ("différés" if fr else "deferred") + f" ({'; '.join(x['causes'])})")
        for c, m3, ws in pr.causes(d - timedelta(days=29), d)[:5]:
            out.append((f"Sur 30 jours : {m3:.1f} m³ différés pour « {c} » ({', '.join(ws)})" if fr else
                        f"Over 30 days: {m3:.1f} m3 deferred for \"{c}\" ({', '.join(ws)})"))
        return out

    def _f_tests(self, t0, t1, per, wells):
        fr = self.L == "fr"
        wt = getattr(self.app, "welltests", None)
        if not wt:
            return []
        out = []
        for r in wt.running():
            out.append((f"En essai : {r['well']} depuis {self._fmt_t(r['start_t'])}" if fr else f"On test: {r['well']} since {self._fmt_t(r['start_t'])}"))
        od = wt.overdue()
        out.append((f"{len(od)} puits en retard d'essai : " if fr else f"{len(od)} wells overdue for a test: ")
                   + ", ".join(f"{o['well']} ({'jamais' if fr else 'never'} )" if o["days"] is None else f"{o['well']} ({o['days']} j)" for o in od))
        for s in wt.schedule(7)[:6]:
            out.append((f"Proposé : {s['well']} le {s['date']} sur {s['manifold']} ({s['reason']})" if fr else
                        f"Proposed: {s['well']} on {s['date']} on {s['manifold']} ({s['reason']})"))
        for r in wt.list(limit=50):
            if r["status"] == "to_validate":
                out.append((f"À valider : essai {r['well']} : {r['verdict']}" if fr else f"To validate: test {r['well']}: {r['verdict']}"))
        return out

    def _f_workorders(self, t0, t1, per, wells):
        fr = self.L == "fr"
        wo = getattr(self.app, "workorders", None)
        if not wo:
            return []
        now = self.app.engine.now
        rows = wo.list(statuses=("open", "acknowledged", "reopened", "done"))
        out = [(f"{len(rows)} ordre(s) de travail non clos." if fr else f"{len(rows)} work order(s) not closed.")]
        for o in rows[:15]:
            late = o["status"] != "done" and o["due_t"] < now
            out.append(f"#{o['id']} {o['entity']} {o['title']} → {o['team']}, {WO_LAB[self.L][o['status']]}" + ((" EN RETARD" if fr else " OVERDUE") if late else ""))
        return out

    def _f_priorities(self, t0, t1, per, wells):
        fr = self.L == "fr"
        out = []
        for f in self._findings()[:8]:
            due = f" ({'échéance' if fr else 'due'} {f['due_date']})" if f.get("due_date") else ""
            out.append(f"[{f['severity_label']}] {f['entity']} : {f['title']}{due}. {f['action']} ({f['team_label']})")
        return out or [("Aucun constat de l'analyse long terme." if fr else "No long-term finding.")]

    def _f_handover(self, t0, t1, per, wells):
        from .logbook import handover
        return [handover(self.app)[1]]

    # ------------------------------------------------------------------ answer
    def answer(self, q: str) -> dict:
        q = (q or "").strip()[:500]
        if not q:
            return {"answer": HELP[self.L], "mode": "rules", "facts": []}
        if self.c["mode"] == "off":
            return {"answer": "", "mode": "off", "facts": []}
        it, lines, per = self.facts(q)
        rules = "\n".join(lines) if lines else HELP[self.L]
        if self.c["mode"] != "local" or it in ("help", "handover"):
            return {"answer": rules, "mode": "rules", "facts": lines}
        try:
            text = self._llm(q, lines)
            return {"answer": text, "mode": "local", "facts": lines}
        except Exception as e:  # noqa: BLE001
            log.warning("local model did not answer: %s", e)
            note = "(Modèle local indisponible : réponse sans IA.)\n" if self.L == "fr" else "(Local model unavailable: answer without AI.)\n"
            return {"answer": note + rules, "mode": "rules", "facts": lines, "error": str(e)}

    def _llm(self, q, lines):
        now = datetime.fromtimestamp(self.app.engine.now).strftime("%d/%m/%Y %H:%M")
        facts = "\n".join(f"- {x}" for x in lines) or "- (aucun)"
        msg = [{"role": "system", "content": SYSTEM[self.L]},
               {"role": "user", "content": f"Heure actuelle : {now}\nFAITS :\n{facts}\n\nQUESTION : {q}"}]
        req = urllib.request.Request(self.c["url"].rstrip("/") + "/api/chat",
                                     data=json.dumps({"model": self.c["model"], "messages": msg, "stream": False,
                                                      "options": {"temperature": 0.1}}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(self.c["timeout_s"])) as r:  # noqa: S310 - site-local address checked in config
            data = json.loads(r.read().decode())
        text = (data.get("message") or {}).get("content", "").strip()
        if not text:
            raise ValueError("empty answer")
        return text
