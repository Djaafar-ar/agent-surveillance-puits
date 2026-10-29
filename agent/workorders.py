"""Work orders: from "the agent saw a problem" to "the problem is really fixed".

  open        created automatically from a real-time alarm that lasts (a failed
              transmitter, a silent RTU, a plugging choke...) or from a long-term
              finding (battery ageing, annulus build-up...), or by hand. Sent to the
              team that should act, with the evidence and the proposed action.
  reminder    while it is not done past its due time, the team and the supervisor
              are reminded (every 24 h by default).
  done        the team says it is done (dashboard). For a well, this is written in
              the logbook as an intervention: the agent relearns the well.
  verified    the agent checks in the data: the alarm did not come back within
              24 h, or the long-term finding is gone 14 days later.
  reopened    the problem is still there: the order is reopened and everyone told.
  cancelled   by the supervisor, with a reason.

The agent proposes and follows up; people decide and do the work.
"""

from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("agent.workorders")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, created_t REAL, created_by TEXT,
  source TEXT, entity TEXT, code TEXT, team TEXT, priority INTEGER, title TEXT, detail TEXT, action TEXT, due_t REAL,
  status TEXT, done_t REAL, done_by TEXT, done_note TEXT, verify_until REAL, closed_t REAL, result TEXT,
  last_remind_t REAL, reminders INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS work_orders_k ON work_orders (key, status);
"""
COLS = ("id", "key", "created_t", "created_by", "source", "entity", "code", "team", "priority", "title", "detail", "action",
        "due_t", "status", "done_t", "done_by", "done_note", "verify_until", "closed_t", "result", "last_remind_t", "reminders")
OPEN = ("open", "acknowledged", "reopened")

ALARM_TEAM = {"CARTE": "instrumentation", "RTU": "telecom", "REP": "telecom", "SCADA_LINK": "telecom",
              "BATT": "maintenance", "SOLAIRE": "maintenance", "TEMP_COFFRET": "maintenance",
              "DUSE": "production", "LIGNE": "production", "P_CHUTE": "production", "P_HAUTE": "production",
              "INSTAB": "production", "GL_ARRET": "production", "GL_ECART": "production", "EAU_ARRET": "production",
              "MAN_HAUTE": "production", "SAT_HAUTE": "production", "GLNET": "production",
              "ANN": "hse", "ANN7": "hse", "FEU": "hse", "INTRUS": "hse"}

T = {"fr": {"new": "Ordre de travail #{id} : {entity} - {title}", "remind": "RAPPEL ordre de travail #{id} en retard : {entity} - {title}",
            "reopen": "ROUVERT ordre de travail #{id} : {entity} - {title}", "verified": "Vérifié : ordre #{id} {entity}, le problème n'est pas revenu.",
            "still_alarm": "L'alarme « {title} » est revenue après l'intervention.",
            "still_finding": "Le constat est toujours présent {d} jours après l'intervention.",
            "ok_alarm": "Pas d'alarme pendant {h} h après l'intervention.", "ok_finding": "Le constat a disparu {d} jours après l'intervention.",
            "body": "{entity}\nÉquipe : {team}\nPriorité : {prio}\nÀ faire avant : {due}\n\nConstat\n{detail}\n\nAction proposée\n{action}\n\n"
                    "Quand c'est fait, le marquer « fait » sur le tableau de bord de l'agent : il vérifiera dans les données que le problème a disparu.\n"
                    "Message automatique de l'agent de surveillance (lecture seule).",
            "prio": {3: "urgente", 2: "normale", 1: "basse"}},
     "en": {"new": "Work order #{id}: {entity} - {title}", "remind": "REMINDER overdue work order #{id}: {entity} - {title}",
            "reopen": "REOPENED work order #{id}: {entity} - {title}", "verified": "Verified: order #{id} {entity}, the problem did not come back.",
            "still_alarm": "The alarm \"{title}\" came back after the intervention.",
            "still_finding": "The finding is still there {d} days after the intervention.",
            "ok_alarm": "No alarm for {h} h after the intervention.", "ok_finding": "The finding disappeared {d} days after the intervention.",
            "body": "{entity}\nTeam: {team}\nPriority: {prio}\nDue: {due}\n\nFinding\n{detail}\n\nProposed action\n{action}\n\n"
                    "When it is done, mark it \"done\" on the agent dashboard: it will check in the data that the problem is gone.\n"
                    "Automatic message from the surveillance agent (read-only).",
            "prio": {3: "urgent", 2: "normal", 1: "low"}}}


class WorkOrders:
    def __init__(self, cfg, store, engine, out=None, logbook=None, analysis=None):
        self.cfg, self.store, self.engine, self.out, self.logbook = cfg, store, engine, out, logbook
        self.analysis = analysis          # callable(force: bool) -> list of finding dicts
        self.c = cfg.workorders
        self.L = cfg.language
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    # ------------------------------------------------------------------ db
    def _q(self, sql, args=()):
        with self.store.lock:
            return [dict(zip(COLS, r)) for r in self.store.db.execute(sql, args)]

    def _x(self, sql, args=()):
        with self.store.lock:
            cur = self.store.db.execute(sql, args)
            self.store.db.commit()
            return cur.lastrowid

    def get(self, oid):
        r = self._q("SELECT * FROM work_orders WHERE id = ?", (oid,))
        return r[0] if r else None

    def list(self, statuses=None, limit=300):
        if statuses:
            return self._q(f"SELECT * FROM work_orders WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY priority DESC, created_t DESC LIMIT ?",
                           (*statuses, limit))
        return self._q("SELECT * FROM work_orders ORDER BY created_t DESC LIMIT ?", (limit,))

    def _now(self):
        return self.engine.now if self.engine else datetime.now().timestamp()

    # ------------------------------------------------------------------ create
    def create(self, entity, title, team, detail="", action="", priority=2, due_hours=72.0, source="manual", code="", by="agent", key=None):
        from .analysis import TEAMS
        if team not in TEAMS:
            raise ValueError(f"team must be one of {', '.join(TEAMS)}")
        if not (title or "").strip() or not (entity or "").strip():
            raise ValueError("equipment and title are required")
        now = self._now()
        key = key or f"{source}:{entity}:{code or title}"
        oid = self._x("INSERT INTO work_orders (key, created_t, created_by, source, entity, code, team, priority, title, detail, action, "
                      "due_t, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                      (key, now, by, source, entity, code, team, int(priority), title.strip(), detail, action, now + due_hours * 3600))
        o = self.get(oid)
        self._notify(o, "new")
        if self.out:
            self.out.journal("info", entity, self._t("new", o))
        return o

    def _t(self, k, o, **kw):
        return T[self.L][k].format(**{**o, **kw})

    def _notify(self, o, kind, extra=""):
        if not self.out or not self.c.get("notify_teams", True):
            return
        teams = (self.cfg.analysis or {}).get("teams", {})
        to = list(teams.get(o["team"], []))
        if kind in ("remind", "reopen"):
            to += [x for x in self.cfg.email.get("report_to", []) if x not in to]
        if not to:
            return
        from .analysis import TEAM_LAB
        L = T[self.L]
        body = L["body"].format(entity=o["entity"], team=TEAM_LAB[self.L][o["team"]], prio=L["prio"].get(o["priority"], ""),
                                due=datetime.fromtimestamp(o["due_t"]).strftime("%d/%m/%Y %H:%M"), detail=o["detail"] or "-",
                                action=o["action"] or "-")
        if extra:
            body = extra + "\n\n" + body
        self.out.email(to, self._t(kind, o), body)

    def _has(self, key, within_s=None):
        rows = self._q("SELECT * FROM work_orders WHERE key = ? ORDER BY id DESC LIMIT 1", (key,))
        if not rows:
            return False
        o = rows[0]
        if o["status"] in OPEN + ("done",):
            return True
        # verified or cancelled recently: do not recreate at once
        return within_s is not None and o["closed_t"] and self._now() - o["closed_t"] < within_s

    def from_alarms(self):
        if not self.c.get("enabled", True):
            return
        lim = self.c["from_alarms_after_min"] * 60
        now = self._now()
        with self.engine.lock:
            roots = [a for a in self.engine.alarms if a.parent is None and a.t_end is None and now - a.t0 >= lim]
        for a in roots:
            code = "CAPTEUR" if a.code.startswith("CAPTEUR") else a.code
            team = "instrumentation" if code == "CAPTEUR" else ALARM_TEAM.get(code)
            if not team:
                continue
            key = f"alarm:{a.entity}:{a.code}"
            if self._has(key, within_s=6 * 3600):
                continue
            urgent = a.severity == "urgence"
            self.create(a.entity, a.title, team, detail=f"{a.value}. {a.diag}".strip(), action=a.action,
                        priority=3 if urgent else 2, due_hours=4 if urgent else 24, source="alarm", code=a.code, key=key)

    def from_findings(self, findings):
        if not self.c.get("enabled", True):
            return
        for f in findings:
            if f["severity"] < self.c["from_analysis_min_severity"]:
                continue
            key = f"analysis:{f['entity']}:{f['kind']}"
            if self._has(key, within_s=self.c["verify_days_analysis"] * 86400):
                continue
            due = f.get("due_days")
            due_h = max(24.0, min(due * 24 * 0.7, 30 * 24)) if due is not None else (48 if f["severity"] == 3 else 14 * 24)
            self.create(f["entity"], f["title"], f["team"], detail=f["evidence"], action=f["action"], priority=f["severity"],
                        due_hours=due_h, source="analysis", code=f["kind"], key=key)

    # ------------------------------------------------------------------ people
    def acknowledge(self, oid, by):
        o = self.get(oid)
        if not o or o["status"] not in ("open", "reopened"):
            raise ValueError("work order not open")
        self._x("UPDATE work_orders SET status = 'acknowledged', result = COALESCE(result, '') || ? WHERE id = ?",
                (f"[{datetime.fromtimestamp(self._now()):%d/%m %H:%M}] pris en charge par {by}\n", oid))
        return self.get(oid)

    def done(self, oid, by, note=""):
        o = self.get(oid)
        if not o or o["status"] not in OPEN:
            raise ValueError("work order not open")
        now = self._now()
        verify = self.c["verify_hours"] * 3600 if o["source"] != "analysis" else self.c["verify_days_analysis"] * 86400
        self._x("UPDATE work_orders SET status = 'done', done_t = ?, done_by = ?, done_note = ?, verify_until = ? WHERE id = ?",
                (now, by, note, now + verify, oid))
        if self.logbook:
            text = f"OT #{oid} : {o['title']}" + (f" - {note}" if note else "")
            wells = {w.id for w in self.cfg.wells}
            if o["entity"] in wells:
                self.logbook.intervention(o["entity"], text, by)
            else:
                self.logbook.add("intervention", text, by, o["entity"])
        return self.get(oid)

    def cancel(self, oid, by, reason):
        o = self.get(oid)
        if not o or o["status"] not in OPEN + ("done",):
            raise ValueError("work order already closed")
        if not (reason or "").strip():
            raise ValueError("give a reason")
        self._x("UPDATE work_orders SET status = 'cancelled', closed_t = ?, result = COALESCE(result, '') || ? WHERE id = ?",
                (self._now(), f"Annulé par {by} : {reason}\n" if self.L == "fr" else f"Cancelled by {by}: {reason}\n", oid))
        return self.get(oid)

    # ------------------------------------------------------------------ follow-up
    def step(self):
        now = self._now()
        if not self.c.get("enabled", True):
            return
        # reminders
        rem = self.c["remind_hours"] * 3600
        for o in self.list(statuses=OPEN):
            if o["due_t"] < now and (o["last_remind_t"] is None or now - o["last_remind_t"] >= rem):
                self._x("UPDATE work_orders SET last_remind_t = ?, reminders = reminders + 1 WHERE id = ?", (now, o["id"]))
                self._notify(o, "remind")
        # verification of what was declared done
        for o in self.list(statuses=("done",)):
            if o["source"] == "alarm":
                with self.engine.lock:
                    back = [a for a in self.engine.alarms if a.t_end is None and a.entity == o["entity"] and a.code == o["code"]
                            and a.t0 >= o["done_t"]]
                if back:
                    self._reopen(o, self._t("still_alarm", o))
                elif now >= o["verify_until"]:
                    # still active since before the intervention?
                    with self.engine.lock:
                        still = [a for a in self.engine.alarms if a.t_end is None and a.entity == o["entity"] and a.code == o["code"]]
                    if still:
                        self._reopen(o, self._t("still_alarm", o))
                    else:
                        self._verify(o, self._t("ok_alarm", o, h=self.c["verify_hours"]))
            elif now >= o["verify_until"]:
                if o["source"] == "analysis" and self.analysis:
                    try:
                        found = self.analysis(True)
                    except Exception:  # noqa: BLE001
                        log.exception("analysis during verification")
                        continue
                    d = round((now - o["done_t"]) / 86400)
                    if any(f["entity"] == o["entity"] and f["kind"] == o["code"] for f in found):
                        self._reopen(o, self._t("still_finding", o, d=d))
                    else:
                        self._verify(o, self._t("ok_finding", o, d=d))
                else:
                    self._verify(o, "")

    def _verify(self, o, why):
        self._x("UPDATE work_orders SET status = 'verified', closed_t = ?, result = COALESCE(result, '') || ? WHERE id = ?",
                (self._now(), (why + "\n") if why else "", o["id"]))
        if self.out:
            self.out.journal("info", o["entity"], self._t("verified", o))

    def _reopen(self, o, why):
        self._x("UPDATE work_orders SET status = 'reopened', due_t = ?, result = COALESCE(result, '') || ?, verify_until = NULL WHERE id = ?",
                (self._now() + 24 * 3600, why + "\n", o["id"]))
        o = self.get(o["id"])
        self._notify(o, "reopen", why)
        if self.out:
            self.out.journal("info", o["entity"], self._t("reopen", o) + " " + why)

    def summary(self):
        rows = self.list()
        now = self._now()
        return {"open": sum(o["status"] in OPEN for o in rows), "overdue": sum(o["status"] in OPEN and o["due_t"] < now for o in rows),
                "done_waiting_check": sum(o["status"] == "done" for o in rows), "verified": sum(o["status"] == "verified" for o in rows),
                "reopened": sum(o["status"] == "reopened" for o in rows)}
