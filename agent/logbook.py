"""Electronic shift logbook (main courante) and automatic shift handover.

What the operator used to write in the paper logbook goes here, and the agent
uses it:

  note          free text ("vent de sable, tournée reportée")
  status        a well is shut in, on test, under intervention, or back in production.
                On sites without a valve or test signal this is how the agent knows;
                the reason is kept and becomes the cause of the deferred production.
  intervention  wash, choke change, battery replaced, transmitter recalibrated...
                The agent relearns the well's normal values, closes the alarms the
                intervention explains, and the long-term trends restart from that day.
  handover      the shift handover written by the agent at each shift change.

Every entry keeps its author (logged-in user) and time.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger("agent.logbook")

SCHEMA = """
CREATE TABLE IF NOT EXISTS logbook (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, author TEXT, kind TEXT,
  equipment TEXT, status TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS logbook_t ON logbook (t);
CREATE INDEX IF NOT EXISTS logbook_e ON logbook (equipment, t);
"""
KINDS = ("note", "status", "intervention", "handover", "system")
STATUSES = ("production", "shut", "test", "work")

STATUS_LAB = {"fr": {"production": "En production", "shut": "Fermé", "test": "En essai", "work": "En intervention"},
              "en": {"production": "Producing", "shut": "Shut in", "test": "On test", "work": "Under intervention"}}


class Logbook:
    def __init__(self, cfg, store, engine=None, out=None):
        self.cfg, self.store, self.engine, self.out = cfg, store, engine, out
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def _now(self, t=None):
        if t is not None:
            return t
        return self.engine.now if self.engine else datetime.now().timestamp()

    # ------------------------------------------------------------------ write
    def add(self, kind: str, text: str, author: str = "", equipment: str | None = None, status: str | None = None, t=None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown logbook entry kind '{kind}'")
        text = (text or "").strip()[:4000]
        with self.store.lock:
            cur = self.store.db.execute("INSERT INTO logbook (t, author, kind, equipment, status, text) VALUES (?,?,?,?,?,?)",
                                        (self._now(t), author, kind, equipment, status, text))
            self.store.db.commit()
            return cur.lastrowid

    def note(self, text, author="", equipment=None, t=None):
        if not (text or "").strip():
            raise ValueError("empty note")
        return self.add("note", text, author, equipment, t=t)

    def set_status(self, well: str, status: str, author: str = "", text: str = "", t=None) -> int:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if well not in {w.id for w in self.cfg.wells}:
            raise ValueError(f"unknown well '{well}'")
        i = self.add("status", text, author, well, status, t)
        if self.engine:
            self.engine.set_status(well, status)
        return i

    def intervention(self, equipment: str, text: str, author: str = "", t=None) -> int:
        if not (text or "").strip():
            raise ValueError("describe the intervention")
        i = self.add("intervention", text, author, equipment, t=t)
        if self.engine and equipment in self.engine.wells:
            self.engine.relearn(equipment, text)
        return i

    # ------------------------------------------------------------------ read
    def entries(self, since=None, until=None, equipment=None, kinds=None, limit=500):
        q, a = "SELECT id, t, author, kind, equipment, status, text FROM logbook WHERE 1=1", []
        if since is not None:
            q += " AND t >= ?"
            a.append(since)
        if until is not None:
            q += " AND t < ?"
            a.append(until)
        if equipment:
            q += " AND equipment = ?"
            a.append(equipment)
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            a += list(kinds)
        q += " ORDER BY t DESC, id DESC LIMIT ?"
        a.append(limit)
        with self.store.lock:
            rows = list(self.store.db.execute(q, a))
        return [dict(zip(("id", "t", "author", "kind", "equipment", "status", "text"), r)) for r in rows]

    def statuses(self) -> dict:
        """Current status written for each well: {well: entry} (wells never written are absent)."""
        with self.store.lock:
            rows = list(self.store.db.execute(
                "SELECT l.id, l.t, l.author, l.kind, l.equipment, l.status, l.text FROM logbook l "
                "JOIN (SELECT equipment, MAX(id) AS m FROM logbook WHERE kind='status' GROUP BY equipment) x ON l.id = x.m"))
        return {r[4]: dict(zip(("id", "t", "author", "kind", "equipment", "status", "text"), r)) for r in rows}

    def interventions(self) -> dict:
        """{equipment: [date, ...]} for the long-term analysis."""
        out: dict = {}
        with self.store.lock:
            rows = list(self.store.db.execute(
                "SELECT equipment, t FROM logbook WHERE kind='intervention' OR (kind='status' AND status='work') ORDER BY t"))
        for e, t in rows:
            if e:
                out.setdefault(e, []).append(datetime.fromtimestamp(t).date())
        return out

    def restore(self):
        """At start-up: give the engine back the statuses written before the restart."""
        if not self.engine:
            return
        for well, e in self.statuses().items():
            if well in self.engine.wells and e["status"] != "production":
                self.engine.wells[well].manual = e["status"]

    def cause(self, well: str) -> str:
        """Reason written for the current shut-in, if any."""
        e = self.statuses().get(well)
        return e["text"] if e and e["status"] in ("shut", "work") else ""


# ---------------------------------------------------------------------- shifts
def shift_changes(cfg) -> list[tuple[int, int]]:
    out = []
    for s in (cfg.shifts or {}).get("changes", ["06:00", "18:00"]):
        h, m = str(s).split(":")
        out.append((int(h), int(m)))
    return sorted(out)


def shift_bounds(cfg, t: float) -> tuple[float, float]:
    """Start and end of the shift that contains time t."""
    d = datetime.fromtimestamp(t)
    ch = shift_changes(cfg)
    cands = []
    for k in (-1, 0, 1):
        day = d.date() + timedelta(days=k)
        cands += [datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m) for h, m in ch]
    cands.sort()
    start = max(c for c in cands if c <= d)
    end = min(c for c in cands if c > d)
    return start.timestamp(), end.timestamp()


def handover(app, t_end: float | None = None, author: str = "agent") -> tuple[str, str]:
    """Shift handover: what the next shift must know. Returns (subject, body)."""
    cfg, eng, L = app.cfg, app.engine, app.cfg.language
    fr = L == "fr"
    t_end = t_end or eng.now
    t0, _ = shift_bounds(cfg, t_end - 1)
    f = lambda t: datetime.fromtimestamp(t).strftime("%H:%M")
    fd = lambda t: datetime.fromtimestamp(t).strftime("%d/%m %H:%M")
    lines = []
    subj = (f"Relève de quart {cfg.site_name} - {datetime.fromtimestamp(t_end):%d/%m/%Y %H:%M}" if fr
            else f"Shift handover {cfg.site_name} - {datetime.fromtimestamp(t_end):%d/%m/%Y %H:%M}")
    lines.append((f"Quart du {fd(t0)} au {fd(t_end)}." if fr else f"Shift from {fd(t0)} to {fd(t_end)}."))
    lines.append("")
    with eng.lock:
        active = [a for a in eng.alarms if a.parent is None and a.t_end is None]
        during = [a for a in eng.alarms if a.parent is None and t0 <= a.t0 < t_end]
        not_prod = [(w.id, w.manual or ("shut" if w.cur.get("sdv_closed") else "test" if w.cur.get("on_test") else None))
                    for w in eng.wells.values()]
    # 1. to do first
    todo = []
    for a in sorted(active, key=lambda a: (a.severity != "urgence", a.t0)):
        todo.append(f"- [{'URGENCE' if a.severity == 'urgence' else 'AVERT.'}] {a.entity} : {a.title}, {a.value}"
                    + ((" (non acquittée)" if fr else " (not acknowledged)") if not a.ack else ""))
    lb = getattr(app, "logbook", None)
    for wid, st in not_prod:
        if st in ("shut", "work") and lb and not lb.cause(wid):
            todo.append(f"- {wid} : " + ("fermé sans cause écrite dans la main courante, à préciser" if fr
                                        else "shut in with no reason written in the logbook, to be specified"))
    wo = getattr(app, "workorders", None)
    if wo:
        for o in wo.list(statuses=("open", "acknowledged", "reopened")):
            if o["due_t"] and o["due_t"] < t_end:
                todo.append(f"- OT #{o['id']} {o['entity']} : {o['title']} " + ("(en retard)" if fr else "(overdue)"))
    lines.append("À FAIRE / EN COURS" if fr else "TO DO / ONGOING")
    lines += todo or ["- " + ("rien en attente" if fr else "nothing pending")]
    lines.append("")
    # 2. wells not producing
    lines.append("PUITS HORS PRODUCTION" if fr else "WELLS NOT PRODUCING")
    np_ = [(w, s) for w, s in not_prod if s]
    for wid, st in np_:
        c = lb.cause(wid) if lb else ""
        lines.append(f"- {wid} : {STATUS_LAB[L][st]}" + (f" ({c})" if c else ""))
    if not np_:
        lines.append("- " + ("aucun" if fr else "none"))
    lines.append("")
    # 3. what happened
    lines.append("PENDANT LE QUART" if fr else "DURING THE SHIFT")
    for a in sorted(during, key=lambda a: a.t0):
        end = f"{f(a.t_end)}" if a.t_end else ("en cours" if fr else "ongoing")
        ack = f", OK {cfg.roster[a.ack[0]].name}" if a.ack else ""
        lines.append(f"- {f(a.t0)}-{end} {a.entity} : {a.title}{ack}")
    if lb:
        for e in reversed(lb.entries(t0, t_end, kinds=("note", "status", "intervention"))):
            what = {"note": "", "status": STATUS_LAB[L].get(e["status"], "") + " ", "intervention": ("Intervention " if fr else "Intervention ")}[e["kind"]]
            lines.append(f"- {f(e['t'])} {e['equipment'] or ''} {what}{e['text']} ({e['author'] or '-'})".replace("  ", " "))
    if not during and not (lb and lb.entries(t0, t_end, kinds=("note", "status", "intervention"))):
        lines.append("- " + ("quart calme, aucune alarme" if fr else "quiet shift, no alarm"))
    lines.append("")
    # 4. tests and work orders
    wt = getattr(app, "welltests", None)
    if wt:
        run = wt.running()
        nxt = wt.schedule(days=2)[:4]
        if run or nxt:
            lines.append("ESSAIS DE PUITS" if fr else "WELL TESTS")
            for r in run:
                lines.append(f"- {r['well']} " + (f"en essai depuis {fd(r['start_t'])}" if fr else f"on test since {fd(r['start_t'])}"))
            for s in nxt:
                lines.append(f"- {s['well']} " + (f"prévu le {s['date']} sur {s['manifold']}" if fr else f"planned {s['date']} on {s['manifold']}"))
            lines.append("")
    if wo:
        opened = [o for o in wo.list() if t0 <= o["created_t"] < t_end]
        done = [o for o in wo.list() if o["done_t"] and t0 <= o["done_t"] < t_end]
        if opened or done:
            lines.append("ORDRES DE TRAVAIL" if fr else "WORK ORDERS")
            for o in opened:
                lines.append(f"- " + (f"ouvert #{o['id']} {o['entity']} : {o['title']} → {o['team']}" if fr else
                                      f"opened #{o['id']} {o['entity']}: {o['title']} → {o['team']}"))
            for o in done:
                lines.append(f"- " + (f"terminé #{o['id']} {o['entity']} par {o['done_by']}" if fr else f"done #{o['id']} {o['entity']} by {o['done_by']}"))
            lines.append("")
    pr = getattr(app, "production", None)
    if pr:
        d = pr.shift_deferred(t0, t_end)
        if d:
            lines.append("PRODUCTION DIFFÉRÉE PENDANT LE QUART" if fr else "DEFERRED PRODUCTION DURING THE SHIFT")
            for well, m3, cause in d:
                lines.append(f"- {well} : {m3:.1f} m³ ({cause})")
            lines.append("")
    lines.append("Relève préparée automatiquement par l'agent de surveillance (lecture seule). À compléter par le chef de quart."
                 if fr else "Handover prepared automatically by the surveillance agent (read-only). To be completed by the shift supervisor.")
    return subj, "\n".join(lines)
