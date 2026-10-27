"""Field simulator for demonstrations and tests: an OPC UA server that behaves
like the SCADA of a small oil field (12 wells, 3 manifolds, 3 radio repeaters).

    python -m simulator.sim_server                 # 1 simulated minute every 2 s
    python -m simulator.sim_server --speed 60      # real time x60
    python -m simulator.sim_server --scenario demo # scripted incidents, for a video

Incident control page: http://127.0.0.1:8090
The agent connects to opc.tcp://127.0.0.1:4840/agent-sim/ exactly as it would
connect to a real SCADA; it is never told which incident is active.
THIS IS A SIMULATION. It is not part of the agent installed on a site.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asyncua import Server, ua

from .field_model import GROUP_INCIDENTS, SAT_INCIDENTS, SATELLITES, WELL_INCIDENTS, Field

SIG_NODE = {"whp": "WHP", "wht": "WHT", "line_p": "LINE_P", "arrival_p": "ARRIVAL_P", "ann": "ANN", "ann_7": "ANN_7",
            "oil_rate": "OIL_RATE", "gl_flow": "GL_FLOW", "gl_inj_p": "GL_INJ_P", "gl_setpoint": "GL_SETPOINT",
            "gl_valve_pos": "GL_VALVE_POS", "gl_mode_auto": "GL_MODE_AUTO", "water_flow": "WATER_FLOW", "on_test": "ON_TEST",
            "battery": "BATTERY", "battery_current": "BATTERY_I", "solar_current": "SOLAR_I", "solar_voltage": "SOLAR_V",
            "cabinet_temp": "CABINET_T", "io_fault": "IO_FAULT", "comm_ok": "COMM_OK", "sdv_closed": "SDV_CLOSED", "fire": "FIRE",
            "remote_cmd": "REMOTE_CMD", "intrusion": "INTRUSION", "pit_cover_open": "PIT_COVER", "cabinet_door_open": "CABINET_DOOR",
            "shelter_door_open": "SHELTER_DOOR", "concertina_alarm": "CONCERTINA", "presence_5m": "PRESENCE_5M",
            "p_man": "P_MAN", "p_test": "P_TEST", "p_gl": "P_GL"}

# Scripted sequence for the video. Minutes after start; the agent first needs
# about 30 simulated minutes to learn the normal values of each well.
DEMO = [
    (35, "well", "PUITS-01", "chute", "Chute de pression tête sur PUITS-01"),
    (60, "well", "PUITS-01", None, "PUITS-01 rétabli"),
    (62, "group", "MAN-B", "rep", "Panne du répéteur radio R2 (4 puits muets)"),
    (85, "group", "MAN-B", None, "Répéteur R2 rétabli"),
    (88, "group", "MAN-A", "glnet", "Panne du réseau gas lift MAN-A"),
    (130, "group", "MAN-A", None, "Réseau gas lift rétabli"),
    (132, "well", "PUITS-11", "ann", "Pression annulaire en hausse sur PUITS-11"),
    (155, "well", "PUITS-11", None, "PUITS-11 rétabli"),
    (157, "well", "PUITS-07", "feu", "Détection feu sur PUITS-07"),
    (170, "well", "PUITS-07", None, "Fin détection feu"),
    (172, "group", "MAN-C", "collecte", "Restriction de la ligne de collecte MAN-C"),
    (195, "group", "MAN-C", None, "Ligne de collecte rétablie"),
    (198, "well", "PUITS-09", "carte", "Défaut carte E/S sur PUITS-09 (3 transmetteurs faux)"),
    (215, "well", "PUITS-09", None, "Carte E/S PUITS-09 réparée"),
    (217, "well", "PUITS-07", "duse", "Duse de PUITS-07 qui se bouche"),
    (240, "well", "PUITS-07", None, "Duse de PUITS-07 nettoyée"),
]


class Sim:
    def __init__(self, seed, speed, start):
        self.field = Field(seed, start.hour * 60 + start.minute)
        self.speed = speed
        self.t = start
        self.lock = threading.Lock()
        self.events: list = []
        self.minute = 0

    def well(self, wid, k):
        with self.lock:
            self.field.set_well(wid, k)
            self.events.append((self.t.strftime("%H:%M"), f"{wid}: {k or 'rétabli'}"))

    def group(self, man, k):
        with self.lock:
            self.field.set_group(man, k)
            self.events.append((self.t.strftime("%H:%M"), f"{man}: {k or 'rétabli'}"))

    def satellite(self, sat, k):
        with self.lock:
            self.field.set_satellite(sat, k)
            self.events.append((self.t.strftime("%H:%M"), f"{sat}: {k or 'rétabli'}"))

    def reset(self):
        with self.lock:
            self.field.reset()
            self.events.append((self.t.strftime("%H:%M"), "tout rétabli"))


async def run(args):
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    start = datetime.now().replace(hour=2, minute=50, second=0, microsecond=0) if args.night else datetime.now().replace(second=0, microsecond=0)
    sim = Sim(args.seed, args.speed, start)
    srv = Server()
    await srv.init()
    srv.set_endpoint(f"opc.tcp://{args.host}:{args.port}/agent-sim/")
    srv.set_server_name("Simulateur de champ (démonstration)")
    srv.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    idx = await srv.register_namespace("urn:agent-surveillance-puits:simulator")
    objs = srv.nodes.objects
    site = await objs.add_object(ua.NodeId("Site", idx), "Site")
    clock = await site.add_variable(ua.NodeId("Site.SimTime", idx), "SimTime", sim.t.astimezone(timezone.utc).replace(tzinfo=None))
    nodes = {}
    vals, _ = sim.field.values()
    for ent, sigs in vals.items():
        o = await objs.add_object(ua.NodeId(ent, idx), ent)
        for s, v in sigs.items():
            nid = ua.NodeId(f"{ent}.{SIG_NODE[s]}", idx)
            init = ua.Variant(bool(v), ua.VariantType.Boolean) if isinstance(v, bool) else ua.Variant(float(v), ua.VariantType.Double)
            nodes[(ent, s)] = await o.add_variable(nid, SIG_NODE[s], init)
    print(f"OPC UA simulator on opc.tcp://{args.host}:{args.port}/agent-sim/  (namespace {idx})")
    print(f"Incident control page: http://127.0.0.1:{args.control_port}")
    threading.Thread(target=control_server, args=(sim, args.control_port), daemon=True).start()

    script = list(DEMO) if args.scenario == "demo" else []
    async with srv:
        while True:
            with sim.lock:
                sim.field.step()
                sim.minute += 1
                sim.t += timedelta(minutes=1)
                while script and script[0][0] <= sim.minute:
                    _, kind, ent, k, label = script.pop(0)
                    {"well": sim.field.set_well, "group": sim.field.set_group, "sat": sim.field.set_satellite}[kind](ent, k)
                    sim.events.append((sim.t.strftime("%H:%M"), label))
                    print(f"[{sim.t:%H:%M}] scenario: {label}")
                vals, silent = sim.field.values()
            ts = (datetime.now(timezone.utc) if args.wallclock else sim.t.astimezone(timezone.utc)).replace(tzinfo=None)
            for (ent, s), node in nodes.items():
                if ent in silent and s != "comm_ok":
                    continue      # lost RTU: the SCADA keeps the last value with its old timestamp
                v = vals[ent][s]
                var = ua.Variant(bool(v), ua.VariantType.Boolean) if isinstance(v, bool) else ua.Variant(float(v), ua.VariantType.Double)
                await node.write_value(ua.DataValue(var, SourceTimestamp=ts, ServerTimestamp=ts))
            await clock.write_value(ua.DataValue(ua.Variant(ts, ua.VariantType.DateTime), SourceTimestamp=ts, ServerTimestamp=ts))
            await asyncio.sleep(60 / sim.speed)


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Simulateur de champ</title><style>
body{font:15px system-ui,sans-serif;margin:0;background:#20242a;color:#e8e8e8}main{max-width:900px;margin:auto;padding:16px}
h1{font-size:1.3rem;margin:0 0 4px}.warn{background:#5a1d1d;padding:8px 12px;border-radius:6px}
button,select{font:inherit;padding:6px 10px;margin:3px;border-radius:6px;border:1px solid #555;background:#2d333b;color:#eee;cursor:pointer}
fieldset{border:1px solid #444;border-radius:8px;margin:12px 0}ul{font-size:.9rem;color:#bbb}
</style></head><body><main><h1>Simulateur de champ pétrolier</h1>
<p class="warn">SIMULATION pour démonstration. Ce n'est pas un SCADA réel. L'agent lit ce simulateur en OPC UA comme il lirait un vrai SCADA, sans savoir quel incident est déclenché.</p>
<p id="t"></p>
<fieldset><legend>Incident sur un puits</legend><select id="w"></select><div id="wi"></div></fieldset>
<fieldset><legend>Incident sur un manifold / répéteur</legend><select id="m"><option>MAN-A</option><option>MAN-B</option><option>MAN-C</option></select><div id="gi"></div></fieldset>
<fieldset><legend>Incident sur une station satellite</legend><select id="s">%SATS%</select><button data-s="satellite">satellite</button><button data-s="">rétablir</button></fieldset>
<button id="reset">Tout rétablir</button><h2 style="font-size:1rem">Événements simulés</h2><ul id="ev"></ul>
<script>
const W=%WELLS%, WI=%WI%, GI=%GI%;
const $=id=>document.getElementById(id);
$('w').innerHTML=W.map(w=>`<option>${w}</option>`).join('');
$('wi').innerHTML=WI.map(k=>`<button data-w="${k}">${k}</button>`).join('')+'<button data-w="">rétablir</button>';
$('gi').innerHTML=GI.map(k=>`<button data-g="${k}">${k}</button>`).join('')+'<button data-g="">rétablir</button>';
const post=(p,b)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json()).then(s=>{if(s.error)alert(s.error);load()});
document.addEventListener('click',e=>{const b=e.target;
 if(b.dataset.w!==undefined)post('/well',{id:$('w').value,k:b.dataset.w||null});
 if(b.dataset.g!==undefined)post('/group',{id:$('m').value,k:b.dataset.g||null});
 if(b.dataset.s!==undefined)post('/satellite',{id:$('s').value,k:b.dataset.s||null});
 if(b.id==='reset')post('/reset',{});});
function load(){fetch('/state').then(r=>r.json()).then(s=>{$('t').textContent='Heure simulée '+s.time+' · vitesse x'+s.speed;
 $('ev').innerHTML=s.events.slice(-20).reverse().map(e=>`<li>${e[0]} ${e[1]}</li>`).join('')})}
load();setInterval(load,2000);
</script></main></body></html>"""


def control_server(sim: Sim, port: int):
    page = (PAGE.replace("%WELLS%", json.dumps(list(sim.field.wells)))
            .replace("%WI%", json.dumps(WELL_INCIDENTS)).replace("%GI%", json.dumps(GROUP_INCIDENTS))
            .replace("%SATS%", "".join(f"<option>{x}</option>" for x in SATELLITES))).encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, o):
            d = json.dumps(o).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers()
            self.wfile.write(d)

        def do_GET(self):
            if self.path == "/state":
                with sim.lock:
                    act = {w: x["incident"] for w, x in sim.field.wells.items() if x["incident"]}
                    act.update({m: x["incident"] for m, x in sim.field.mans.items() if x["incident"]})
                    act.update({r: "rep" for r, d in sim.field.rep_down.items() if d})
                    act.update({x: k for x, k in sim.field.sat_incident.items() if k})
                    return self._json(200, {"time": sim.t.strftime("%d/%m %H:%M"), "minute": sim.minute, "speed": sim.speed,
                                            "events": sim.events, "active": act})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            try:
                b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
                if self.path == "/well":
                    sim.well(b["id"], b.get("k"))
                elif self.path == "/group":
                    sim.group(b["id"], b.get("k"))
                elif self.path == "/satellite":
                    sim.satellite(b["id"], b.get("k"))
                elif self.path == "/reset":
                    sim.reset()
                self._json(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                self._json(400, {"error": str(e)})

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="OPC UA oil field simulator (demonstration only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4840)
    ap.add_argument("--control-port", type=int, default=8090)
    ap.add_argument("--speed", type=float, default=30, help="simulated minutes per real minute (default 30: 1 min every 2 s)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scenario", choices=["none", "demo"], default="none")
    ap.add_argument("--night", action="store_true", help="start the simulated clock at 02:50")
    ap.add_argument("--wallclock", action="store_true", help="stamp values with the real time (to test the agent without clock_node)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
