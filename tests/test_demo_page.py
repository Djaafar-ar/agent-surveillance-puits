#!/usr/bin/env python3
"""
Automated tests for the well surveillance agent (index.html).

The tests open the page in a headless browser, freeze the real-time clock and
drive the simulation minute by minute. Math.random is replaced by a seeded
generator, so every run is reproducible, and each scenario is repeated over
several seeds to catch behaviour that only fails "sometimes".

Nothing in index.html is modified: at load time the test harness injects a
small hook at the end of the script that exposes the internal state.

Install once:
    pip install playwright
    python -m playwright install chromium

Run:
    python tests/test_agent.py               # all tests, 3 seeds
    python tests/test_agent.py --seeds 10    # more seeds
    python tests/test_agent.py -k feu        # only tests whose name contains "feu"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "index.html")

# ---------------------------------------------------------------------------
# Hook injected at the end of the app's IIFE. It wraps sendSms to record every
# SMS and exposes a read-only view of alarms, wells and settings.
# ---------------------------------------------------------------------------
HOOK = r"""
running = false; clearInterval(timer);
const __sms = [];
const __sendSms = sendSms;
sendSms = function(a, idx, kind){ __sms.push({t:simMin, alarm:a.id, label:a.label, code:a.code, idx, kind, sev:a.severity}); return __sendSms(a, idx, kind); };
const __al = a => ({id:a.id, label:a.label, kind:a.kind, code:a.code, severity:a.severity, t0:a.t0, tEnd:a.tEnd,
  parent:a.parent ? a.parent.id : null, root: rootOf(a).id, children:a.children.map(c => c.id), level:a.level,
  ack:a.ack ? a.ack.idx : null, notified:[...a.notified], exhausted:a.exhausted, title:a.title});
window.__T = {
  run(n){ for(let i=0;i<n;i++) tick(); return simMin; },
  runUntil(js, max){ const f = new Function('T', 'return ' + js); for(let i=0;i<max;i++){ tick(); if(f(window.__T)) return i+1; } return -1; },
  now(){ return simMin; },
  well(id, k){ setWellInc(id, k); },
  group(man, k){ setGroupInc(man, k); },
  reset(){ resetAll(); },
  ack(id, idx){ const a = allAlarms.find(x => x.id === id); ack(a, idx); },
  alarms(){ return allAlarms.map(__al); },
  active(){ return allAlarms.filter(a => a.tEnd === null).map(__al); },
  sms(){ return __sms.slice(); },
  smsCount(){ return smsCount; },
  mails(){ return mails.map(m => ({t:m.t, subject:m.subject, kind:m.kind, to:m.to, body:m.body})); },
  perf(){ const P = alarmPerf(); return {raw:P.raw, roots:P.roots, rateVis:P.rateVis, rateRaw:P.rateRaw, peakVis:P.peakVis, peakRaw:P.peakRaw, reduction:P.reduction, urgPct:P.urgPct, ackAvg:P.ackAvg}; },
  wells(){ return wells.map(w => ({id:w.id, type:w.type, man:w.man, rep:w.rep, p:w.cur.p, ann:w.cur.ann, bat:w.cur.bat, q:w.cur.q, qe:w.cur.qe, qo:w.cur.qo,
    lastUpdate:w.lastUpdate, alarms:Object.keys(w.alarms), defer:w.stats.defer, online:w.stats.online, total:w.stats.total})); },
  decl(){ return Object.fromEntries(Object.entries(declRes).map(([k,R]) => [k, {cls:R.cls, level:R.level, s60:R.s60, forecast:R.forecast, pNow:R.pNow, pMin:R.pMin}])); },
  settings(){ return JSON.parse(JSON.stringify(S)); },
  set(o){ Object.assign(S, o); },
  startDemo(){ startDemo(); running = false; clearInterval(timer); },
  demoOn(){ return demo !== null; },
  daily(){ sendDailyReport(false); },
  handover(){ sendHandoverReport(); },
  declReport(){ sendDeclineReport(); },
  perfReport(){ sendPerfReport(); },
  tab(id){ showTab(id); },
};
"""

SEEDED_RANDOM = """
(() => { let s = %d >>> 0;
  Math.random = function(){ s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
})();
"""


def patched_html():
    src = open(PAGE, encoding="utf-8").read()
    marker = "\n})();\n</script>"
    if marker not in src:
        raise RuntimeError("Could not find the end of the main script to inject the test hook.")
    return src.replace(marker, "\n" + HOOK + marker, 1)


# ---------------------------------------------------------------------------
# Minimal test runner
# ---------------------------------------------------------------------------
class Fail(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Ctx:
    def __init__(self, browser, html, seed, url=None):
        self.browser, self.html, self.seed, self.url = browser, html, seed, url
        self.errors = []
        self.page = None

    def open(self, keep_storage=False):
        if self.page and not keep_storage:
            self.page.context.close()
        if not self.page or not keep_storage:
            ctx = self.browser.new_context(viewport={"width": 1440, "height": 900})
            ctx.add_init_script(SEEDED_RANDOM % self.seed)
            html = self.html
            ctx.route("**/index.html", lambda r: r.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
            ctx.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)))
            pg.on("console", lambda m: self.errors.append("console.error: " + m.text)
                  if m.type == "error" and "ERR_FAILED" not in m.text and "Failed to load resource" not in m.text else None)
            self.page = pg
        self.page.goto(self.url or "http://agent.test/index.html")
        self.page.wait_for_function("window.__T !== undefined")
        return self

    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def T(self, call, *args):
        return self.page.evaluate(f"(a) => window.__T.{call}(...a)", list(args))

    def close(self):
        if self.page:
            self.page.context.close()


TESTS = []


def test(name, seeds=True):
    def deco(fn):
        TESTS.append((name, fn, seeds))
        return fn
    return deco


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def roots(alarms):
    return [a for a in alarms if a["parent"] is None]


def wait_for(c, code, label, max_min):
    """Run until an alarm with this code appears on this label; return detection delay in minutes."""
    return c.T("runUntil", f"T.alarms().some(a => a.code === '{code}' && a.label === '{label}')", max_min)


def assert_clean_after_restore(c, max_min=40):
    n = c.T("runUntil", "T.active().length === 0", max_min)
    act = c.T("active")
    check(n != -1, f"alarms still active {max_min} min after the incident was removed: "
          + ", ".join(f"{a['label']}/{a['code']}" for a in act))


def no_unexpected_roots(c, allowed_labels, allowed_codes=None):
    bad = [a for a in roots(c.T("alarms")) if a["label"] not in allowed_labels or (allowed_codes and a["code"] not in allowed_codes)]
    check(not bad, "unexpected alarms presented to the operator: " + ", ".join(f"{a['label']}/{a['code']}" for a in bad))


# ---------------------------------------------------------------------------
# 1. Page and normal operation
# ---------------------------------------------------------------------------
@test("page loads without JavaScript errors", seeds=False)
def t_load(c):
    c.open()
    check(c.js("document.querySelectorAll('#wellList .well').length") == 12, "12 well rows expected")
    check(c.js("document.querySelectorAll('[role=tab]').length") == 5, "5 tabs expected")
    for tab in ["decl", "perf", "mail", "set", "rt"]:
        c.T("tab", tab)
    check(not c.errors, "errors: " + "; ".join(c.errors))


@test("24 h of normal operation: zero false alarm")
def t_normal(c):
    c.open()
    c.T("run", 1440)
    al = c.T("alarms")
    check(not al, f"{len(al)} false alarm(s): " + ", ".join(f"{a['label']}/{a['code']} at {a['t0']} min" for a in al[:8]))
    check(c.T("smsCount") == 0, "SMS sent without any incident")
    radio = sum(w["online"] for w in c.T("wells")) / sum(w["total"] for w in c.T("wells"))
    check(radio == 1.0, f"radio availability should be 100 % without incident, got {radio:.3%}")
    defer = sum(w["defer"] for w in c.T("wells"))
    check(defer < 0.5, f"deferred production without incident should be ~0, got {defer:.2f} m3")


@test("24 h with the most sensitive thresholds: zero false alarm")
def t_normal_sensitive(c):
    c.open()
    c.T("set", {"thChute": 15, "thSurp": 30, "thRtu": 3, "thAnn": 5})
    c.T("run", 1440)
    al = c.T("alarms")
    check(not al, f"{len(al)} false alarm(s): " + ", ".join(f"{a['label']}/{a['code']} at {a['t0']} min" for a in al[:8]))


@test("least sensitive thresholds: incidents are still detected")
def t_insensitive(c):
    c.open()
    c.T("set", {"thChute": 30, "thSurp": 50, "thRtu": 10})
    c.T("run", 30)
    for inc, wid, code in [("chute", "PUITS-03", "P_CHUTE"), ("surp", "PUITS-07", "P_HAUTE"), ("rtu", "PUITS-06", "RTU")]:
        c.T("well", wid, inc)
        n = wait_for(c, code, wid, 40)
        check(n != -1, f"{code} on {wid} not detected with the least sensitive thresholds")


@test("annulus threshold setting changes the detection")
def t_ann_setting(c):
    times = {}
    for th in (5, 6.9, 10):
        c.open(); c.T("set", {"thAnn": th}); c.T("run", 30)
        c.T("well", "PUITS-11", "ann")
        times[th] = wait_for(c, "ANN", "PUITS-11", 40)
    check(times[5] < times[6.9] < times[10], f"detection delay by setting {times}: the annulus threshold setting should move the detection")


@test("daily report sent once at 07:00, then again next day")
def t_daily(c):
    c.open()
    c.T("run", 249)  # start is 02:50 -> 06:59
    check(not [m for m in c.T("mails") if "journalier" in m["subject"]], "daily report sent before 07:00")
    c.T("run", 1)
    d = [m for m in c.T("mails") if "journalier" in m["subject"]]
    check(len(d) == 1, f"exactly one daily report expected at 07:00, got {len(d)}")
    c.T("run", 1440)
    d = [m for m in c.T("mails") if "journalier" in m["subject"]]
    check(len(d) == 2, f"two daily reports expected after 24 h, got {len(d)}")


# ---------------------------------------------------------------------------
# 2. Single-well incidents: right diagnosis, right severity, nothing else
# ---------------------------------------------------------------------------
WELL_CASES = [
    # incident, well, expected root code, final severity, max detection delay (min), SMS expected
    ("chute",     "PUITS-03", "P_CHUTE",   "urgence", 25, True),
    ("chute",     "PUITS-01", "P_CHUTE",   "urgence", 25, True),
    ("surp",      "PUITS-07", "P_HAUTE",   "urgence", 20, True),
    ("gl",        "PUITS-02", "GL_ARRET",  "urgence", 8,  True),
    ("eau",       "PUITS-04", "EAU_ARRET", "urgence", 45, True),
    ("ann",       "PUITS-11", "ANN",       "urgence", 25, True),
    ("instab",    "PUITS-09", "INSTAB",    "avert",   25, False),
    ("capteur",   "PUITS-10", "CAPTEUR",   "avert",   5,  False),
    ("batterie",  "PUITS-12", "BATT",      "avert",   30, False),
    ("rtu",       "PUITS-06", "RTU",       "urgence", 20, True),
    ("sdv",       "PUITS-05", "SDV",       "urgence", 5,  True),
    ("feu",       "PUITS-07", "FEU",       "urgence", 5,  True),
    ("intrusion", "PUITS-02", "INTRUS",    "urgence", 5,  True),
    ("carte",     "PUITS-08", "CARTE",     "avert",   5,  False),
]


def make_well_test(inc, wid, code, sev, delay, sms):
    def fn(c):
        c.open()
        c.T("run", 30)
        c.T("well", wid, inc)
        n = wait_for(c, code, wid, delay)
        check(n != -1, f"{code} not detected within {delay} min")
        c.T("run", 45)  # let the incident develop (upgrades, consequences)
        al = c.T("alarms")
        main = [a for a in al if a["code"] == code and a["label"] == wid]
        check(main and main[0]["parent"] is None, f"{code} should be a root alarm")
        check(main[0]["severity"] == sev, f"{code} severity {main[0]['severity']}, expected {sev}")
        check(len(main) == 1, f"{code} raised {len(main)} times during one incident (alarm flapping)")
        sent = [s for s in c.T("sms") if s["kind"] == "alarm" and s["alarm"] == main[0]["id"]]
        check(bool(sent) == sms, f"SMS for {code}: expected {sms}, got {len(sent)}")
        others = [a for a in al if a["label"] != wid]
        check(not others, "alarms on other equipment: " + ", ".join(f"{a['label']}/{a['code']}" for a in others))
        c.T("well", wid, None)
        assert_clean_after_restore(c)
        c.__dict__["delay"] = n
    return fn


for inc, wid, code, sev, delay, sms in WELL_CASES:
    test(f"incident {inc} on {wid} -> {code} ({sev})")(make_well_test(inc, wid, code, sev, delay, sms))


@test("gas lift stop: pressure drop is grouped under the gas lift alarm")
def t_gl_group(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-01", "gl")
    c.T("run", 70)
    al = [a for a in c.T("alarms") if a["label"] == "PUITS-01"]
    r = [a for a in al if a["parent"] is None]
    check([a["code"] for a in r] == ["GL_ARRET"], f"one root alarm GL_ARRET expected, got {[a['code'] for a in r]}")
    ch = [a for a in al if a["code"] == "P_CHUTE"]
    check(ch, "the pressure drop caused by the gas lift stop should be detected")
    check(ch[0]["parent"] is not None, "P_CHUTE should be grouped under GL_ARRET")


@test("pressure sensor failure does not raise pressure alarms")
def t_sensor(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-01", "capteur")
    c.T("run", 60)
    codes = {a["code"] for a in c.T("alarms") if a["label"] == "PUITS-01"}
    check(codes == {"CAPTEUR"}, f"only CAPTEUR expected, got {sorted(codes)}")


@test("safety valve closure: consequences grouped, deferred production counted")
def t_sdv(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-05", "sdv")
    c.T("run", 60)
    al = [a for a in c.T("alarms") if a["label"] == "PUITS-05"]
    r = [a["code"] for a in al if a["parent"] is None]
    check(r == ["SDV"], f"only SDV should be presented, got {r}")
    w = [x for x in c.T("wells") if x["id"] == "PUITS-05"][0]
    expected = 64 * 60 / 1440  # qo 64 m3/d during ~60 min
    check(abs(w["defer"] - expected) < 0.5 * expected, f"deferred production {w['defer']:.2f} m3, expected about {expected:.2f}")


@test("fire: the whole on-call roster is paged at once")
def t_fire(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-07", "feu")
    c.T("run", 2)
    s = [x for x in c.T("sms") if x["code"] == "FEU" and x["kind"] == "alarm"]
    check(sorted(x["idx"] for x in s) == [0, 1, 2], f"3 SMS to contacts 0,1,2 expected, got {[x['idx'] for x in s]}")
    check(len({x["t"] for x in s}) == 1, "the 3 SMS should leave in the same minute")


@test("water stop is a warning first, emergency after 30 min")
def t_eau(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-08", "eau")
    n = wait_for(c, "EAU_ARRET", "PUITS-08", 10)
    check(n != -1, "EAU_ARRET not detected")
    a = [x for x in c.T("alarms") if x["code"] == "EAU_ARRET"][0]
    check(a["severity"] == "avert", "should start as a warning")
    check(not [s for s in c.T("sms") if s["code"] == "EAU_ARRET"], "no SMS while it is only a warning")
    c.T("run", 32)
    a = [x for x in c.T("alarms") if x["code"] == "EAU_ARRET"][0]
    check(a["severity"] == "urgence", "should become an emergency after 30 min")
    check([s for s in c.T("sms") if s["code"] == "EAU_ARRET"], "SMS expected when it becomes an emergency")


@test("battery failure: warning before the radio is lost")
def t_batt(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-12", "batterie")
    c.T("run", 90)
    al = [a for a in c.T("alarms") if a["label"] == "PUITS-12"]
    b = [a for a in al if a["code"] == "BATT"]
    r = [a for a in al if a["code"] == "RTU"]
    check(b, "BATT warning expected")
    check(r, "the flat battery should end in a radio loss (RTU alarm)")
    lead = r[0]["t0"] - b[0]["t0"]
    check(lead >= 10, f"battery warning only {lead} min before the radio loss, at least 10 min expected")
    # The RTU loss stays a separate alarm on purpose: it is an emergency (the well is no longer
    # monitored) and must not be hidden under a lower-priority warning. Its diagnosis must name the battery.
    diag = c.js("() => [...document.querySelectorAll('#mailList .m-body')].map(e => e.textContent).join('\\n')")
    check("batterie vide" in diag, "the RTU loss diagnosis should name the flat battery as the probable cause")
    c.__dict__["delay"] = lead


# ---------------------------------------------------------------------------
# 3. Group incidents: one cause -> one alarm
# ---------------------------------------------------------------------------
@test("repeater failure: 4 silent wells -> one alarm, one SMS")
def t_rep(c):
    c.open(); c.T("run", 30)
    c.T("group", "MAN-B", "rep")
    c.T("run", 30)
    al = c.T("alarms")
    r = roots(al)
    check([(a["label"], a["code"]) for a in r] == [("Répéteur R2", "REP")], f"only REP R2 should be presented, got {[(a['label'], a['code']) for a in r]}")
    rtu = [a for a in al if a["code"] == "RTU"]
    check(len(rtu) == 4 and all(a["parent"] for a in rtu), f"4 RTU alarms grouped under REP expected, got {len(rtu)}")
    s = [x for x in c.T("sms") if x["kind"] == "alarm"]
    check(len(s) == 1, f"exactly one SMS on detection expected, got {len(s)}")
    c.T("group", "MAN-B", None)
    assert_clean_after_restore(c)


@test("gas lift network failure: one alarm for the manifold")
def t_glnet(c):
    c.open(); c.T("run", 30)
    c.T("group", "MAN-A", "glnet")
    c.T("run", 60)
    r = roots(c.T("alarms"))
    check([(a["label"], a["code"]) for a in r] == [("Manifold MAN-A", "GLNET")],
          f"only GLNET MAN-A should be presented, got {[(a['label'], a['code']) for a in r]}")
    ch = [a for a in c.T("alarms") if a["parent"] is not None]
    check({a["label"] for a in ch} >= {"PUITS-01", "PUITS-02"}, "the gas lift wells PUITS-01/02 should have grouped alarms")
    c.T("group", "MAN-A", None)
    assert_clean_after_restore(c, 60)


@test("flowline restriction: well overpressures grouped under the manifold")
def t_collecte(c):
    c.open(); c.T("run", 30)
    c.T("group", "MAN-C", "collecte")
    c.T("run", 45)
    r = roots(c.T("alarms"))
    check([(a["label"], a["code"]) for a in r] == [("Manifold MAN-C", "MAN_HAUTE")],
          f"only MAN_HAUTE MAN-C should be presented, got {[(a['label'], a['code']) for a in r]}")
    c.T("group", "MAN-C", None)
    assert_clean_after_restore(c, 60)


@test("worst case: flowline restriction + repeater failure -> 2 alarms only")
def t_worst(c):
    c.open(); c.T("run", 30)
    c.T("group", "MAN-C", "collecte"); c.T("group", "MAN-A", "rep")
    c.T("run", 40)
    p = c.T("perf")
    r = roots(c.T("alarms"))
    check(len(r) == 2, f"2 alarms presented expected, got {len(r)}: {[(a['label'], a['code']) for a in r]}")
    check(p["raw"] >= 6, f"several raw detections expected, got {p['raw']}")


# ---------------------------------------------------------------------------
# 4. Escalation and acknowledgement
# ---------------------------------------------------------------------------
@test("escalation: no answer -> contact 2 after 10 min, contact 3 after 20 min")
def t_escal(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-03", "sdv")
    c.T("run", 2)
    a = [x for x in c.T("alarms") if x["code"] == "SDV"][0]
    t0 = a["t0"]
    c.T("run", 35)
    s = [x for x in c.T("sms") if x["alarm"] == a["id"] and x["kind"] in ("alarm", "escal")]
    seq = [(x["idx"], x["t"] - t0) for x in s]
    check([i for i, _ in seq] == [0, 1, 2], f"contacts 0 -> 1 -> 2 expected, got {seq}")
    check(seq[1][1] == 10 and seq[2][1] == 20, f"escalation at +10 and +20 min expected, got {seq}")
    a = [x for x in c.T("alarms") if x["id"] == a["id"]][0]
    check(a["exhausted"], "the alarm should be marked as having exhausted the roster")


@test("acknowledgement stops escalation")
def t_ack(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-03", "sdv")
    c.T("run", 3)
    a = [x for x in c.T("alarms") if x["code"] == "SDV"][0]
    c.T("ack", a["id"], 0)
    c.T("run", 40)
    s = [x for x in c.T("sms") if x["alarm"] == a["id"] and x["kind"] == "escal"]
    check(not s, f"no escalation expected after OK, got {len(s)} escalation SMS")


@test("end-of-alarm SMS goes only to the people who need it")
def t_clear_sms(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-03", "sdv")
    c.T("run", 3)
    a = [x for x in c.T("alarms") if x["code"] == "SDV"][0]
    c.T("ack", a["id"], 0)
    c.T("run", 5)
    c.T("well", "PUITS-03", None)
    c.T("run", 20)
    s = [x for x in c.T("sms") if x["alarm"] == a["id"] and x["kind"] == "clear"]
    check(len(s) == 1, f"acknowledged by contact 1 before any escalation: 1 end-of-alarm SMS expected, got {len(s)}")


@test("escalation delay setting is respected (5 min)")
def t_esc_setting(c):
    c.open(); c.T("set", {"escMin": 5}); c.T("run", 30)
    c.T("well", "PUITS-03", "sdv")
    c.T("run", 2)
    a = [x for x in c.T("alarms") if x["code"] == "SDV"][0]
    c.T("run", 12)
    s = [x["t"] - a["t0"] for x in c.T("sms") if x["alarm"] == a["id"] and x["kind"] == "escal"]
    check(s[:2] == [5, 10], f"escalations at +5 and +10 min expected, got {s}")


# ---------------------------------------------------------------------------
# 5. Settings, reports, decline, guided demo
# ---------------------------------------------------------------------------
@test("settings: invalid e-mail rejected, valid settings survive a reload", seeds=False)
def t_settings(c):
    c.open()
    c.T("tab", "set")
    c.page.fill("#fReportTo", "pas-une-adresse")
    c.page.click("#saveSet")
    check("invalide" in c.page.inner_text("#setStatus").lower(), "invalid address should be refused")
    c.page.fill("#fReportTo", "ryan@exemple.dz")
    c.page.select_option("#fEsc", index=0)
    esc = c.page.eval_on_selector("#fEsc", "e => +e.value")
    c.page.click("#saveSet")
    c.open(keep_storage=True)
    st = c.T("settings")
    check(st["reportTo"] == "ryan@exemple.dz" and st["escMin"] == esc, f"settings not persisted: {st['reportTo']}, {st['escMin']}")


@test("contact names are escaped (no HTML injection)", seeds=False)
def t_xss(c):
    c.open()
    c.T("tab", "set")
    c.page.fill("#rosterEdit input[data-r='0'][data-f='name']", "<img src=x onerror=\"window.__pwn=1\">")
    c.page.click("#saveSet")
    c.T("tab", "rt")
    c.T("well", "PUITS-03", "sdv"); c.T("run", 3)
    check(not c.js("window.__pwn === 1"), "script executed from a contact name")
    check(not c.js("!!document.querySelector('#smsList img, #roster img')"), "HTML from a contact name was rendered")


@test("reports: daily, handover, decline and alarm review are complete", seeds=False)
def t_reports(c):
    c.open(); c.T("run", 30)
    c.T("well", "PUITS-03", "sdv"); c.T("run", 20)
    c.T("daily"); c.T("handover"); c.T("declReport"); c.T("perfReport")
    m = c.T("mails")
    subj = " | ".join(x["subject"] for x in m)
    for key in ["journalier", "Relève", "déclin", "Bilan"]:
        check(key.lower() in subj.lower(), f"report '{key}' missing")
    daily = [x for x in m if "journalier" in x["subject"]][0]["body"]
    check("PUITS-03" in daily and "Vanne de sécurité" in daily, "daily report should list the SDV alarm on PUITS-03")
    check("undefined" not in json.dumps(m) and "NaN" not in json.dumps(m), "a report contains 'undefined' or 'NaN'")


@test("decline analysis finds the planted cases", seeds=False)
def t_decline(c):
    c.open()
    d = c.T("decl")
    expect = {"PUITS-08": "Dérive capteur probable", "PUITS-04": "Dépôts de sel récurrents",
              "PUITS-03": "Déclin rapide", "PUITS-07": "Déclin rapide", "PUITS-12": "Chute brutale"}
    wrong = {k: d[k]["cls"] for k, v in expect.items() if d[k]["cls"] != v}
    check(not wrong, f"wrong diagnosis: {wrong}")
    check(d["PUITS-06"]["cls"] in ("Stable", "Déclin lent"), f"PUITS-06 (choke change only) should not show a decline, got {d['PUITS-06']['cls']}")


@test("decline chart: legend matches what is drawn", seeds=False)
def t_decl_legend(c):
    c.open(); c.T("tab", "decl")
    problems = []
    for row in c.page.query_selector_all("#declBody tr"):
        row.click()
        legend = c.page.inner_text("#panel-decl .legend").lower()
        has_reg = c.js("!!document.querySelector('#declChart .s-reg')")
        has_fc = c.js("!!document.querySelector('#declChart .s-fc')")
        wid = row.get_attribute("data-id")
        if "tendance calculée" in legend and not has_reg:
            problems.append(f"{wid}: legend shows 'Tendance calculée' but no trend line is drawn")
        if "prévision" in legend and not has_fc:
            problems.append(f"{wid}: legend shows 'Prévision' but no forecast is drawn")
    check(not problems, "; ".join(problems[:6]) + (f" (+{len(problems)-6} more)" if len(problems) > 6 else ""))


@test("guided demo runs to the end, alarms stay under EEMUA targets")
def t_demo(c):
    c.open()
    c.T("startDemo")
    c.T("run", 240)
    check(not c.T("demoOn"), "demo did not finish")
    p = c.T("perf")
    check(p["rateVis"] <= 1, f"alarm rate {p['rateVis']:.2f} per 10 min, target <= 1")
    check(p["peakVis"] <= 10, f"peak {p['peakVis']} in 10 min, flood threshold 10")
    check(p["roots"] < p["raw"], "grouping should reduce the number of alarms")
    subj = " ".join(m["subject"] for m in c.T("mails"))
    check("journalier" in subj and "Relève" in subj and "Bilan" in subj, "the 3 end-of-demo reports should be sent")
    check(not c.T("active"), "all alarms should be cleared at the end of the demo")
    c.__dict__["demo_perf"] = p
    s = c.T("sms")
    c.__dict__["demo_sms"] = {k: sum(1 for x in s if x["kind"] == k) for k in ("alarm", "escal", "clear")}
    check(not c.errors, "errors: " + "; ".join(c.errors))


# ---------------------------------------------------------------------------
# 6. Real SMS path through the Python gateway (test mode, nothing is sent)
# ---------------------------------------------------------------------------
@test("SMS gateway: page -> passerelle-sms.py receives emergency SMS", seeds=False)
def t_gateway(c):
    port = 8797
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "passerelle-sms.py"), "--test", "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/etat", timeout=1); break
            except Exception:
                time.sleep(0.1)
        c.url = f"http://127.0.0.1:{port}/index.html"
        c.open()
        c.T("set", {"smsReal": True, "smsUrl": f"http://127.0.0.1:{port}/sms",
                    "roster": [{"name": "A", "role": "Tech", "tel": "+213600000001"},
                               {"name": "B", "role": "Chef", "tel": "+213600000002"},
                               {"name": "C", "role": "Sup", "tel": "+213600000003"}]})
        c.T("run", 5)
        c.T("well", "PUITS-07", "feu")
        c.T("run", 2)
        c.page.wait_for_timeout(1500)
    finally:
        proc.terminate()
        out = proc.communicate(timeout=5)[0]
    got = re.findall(r"MODE TEST - SMS non envoye vers (\+\d+)", out)
    check(sorted(got) == ["+213600000001", "+213600000002", "+213600000003"], f"gateway received {got}")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3, help="random seeds per scenario")
    ap.add_argument("-k", default="", help="only run tests whose name contains this text")
    ap.add_argument("--json", default="", help="write results to this JSON file")
    args = ap.parse_args()

    html = patched_html()
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, fn, use_seeds in TESTS:
            if args.k and args.k.lower() not in name.lower():
                continue
            seeds = list(range(1, args.seeds + 1)) if use_seeds else [1]
            fails, extra = [], {}
            t = time.time()
            for s in seeds:
                c = Ctx(browser, html, s)
                try:
                    fn(c)
                    if c.errors and fn is not t_load:
                        fails.append((s, "JS errors: " + "; ".join(c.errors[:3])))
                    for k in ("delay", "demo_perf", "demo_sms"):
                        if k in c.__dict__:
                            extra.setdefault(k, []).append(c.__dict__[k])
                except Fail as e:
                    fails.append((s, str(e)))
                except Exception as e:
                    fails.append((s, "CRASH " + "".join(traceback.format_exception_only(type(e), e)).strip()))
                finally:
                    c.close()
            ok = not fails
            mark = "PASS" if ok else "FAIL"
            info = f"  detection {min(extra['delay'])}-{max(extra['delay'])} min" if "delay" in extra else ""
            if "demo_sms" in extra:
                pf = extra["demo_perf"][0]
                info = f"  {pf['raw']} detections -> {pf['roots']} alarms, SMS {extra['demo_sms'][0]}"
            print(f"[{mark}] {name}  ({len(seeds)} seed{'s' if len(seeds) > 1 else ''}, {time.time() - t:.1f}s){info}")
            for s, msg in fails:
                print(f"        seed {s}: {msg}")
            results.append({"name": name, "ok": ok, "seeds": seeds, "fails": fails, "extra": extra})
        browser.close()

    n_ok = sum(r["ok"] for r in results)
    print(f"\n{n_ok}/{len(results)} tests passed")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == "__main__":
    main()
