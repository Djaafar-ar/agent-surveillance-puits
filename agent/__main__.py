"""Command line.

    python -m agent run        --config config/config.yaml    start the surveillance
    python -m agent check      --config config/config.yaml    read every tag once and print it (commissioning)
    python -m agent browse     --config config/config.yaml    list the OPC UA nodes to find tag addresses
    python -m agent test-sms   --config config/config.yaml --to +213...
    python -m agent test-email --config config/config.yaml --to someone@site.dz
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent", description="Well surveillance agent (read-only)")
    ap.add_argument("command", choices=["run", "check", "browse", "test-sms", "test-email"])
    ap.add_argument("--config", default=os.environ.get("AGENT_CONFIG", "config/config.yaml"))
    ap.add_argument("--to", help="recipient for test-sms / test-email")
    ap.add_argument("--node", help="browse: start node (default Objects)")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    try:
        cfg = config.load(a.config)
    except (OSError, config.ConfigError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    if a.command == "run":
        from .app import App
        App(cfg).run(web=not a.no_web)
        return 0

    if a.command in ("check", "browse"):
        from .connectors.base import make_connector
        conn = make_connector(cfg)
        if a.command == "browse":
            if conn.name != "opcua":
                print("browse is only available for OPC UA")
                return 2
            for d, nid, name, val in conn.browse(a.node, a.depth):
                print(f"{'  ' * d}{name:<28} {nid:<40} {val}")
            conn.close()
            return 0
        r = conn.read()
        if not r:
            print(f"NO DATA: {conn.last_error}")
            return 1
        now = conn.now() or time.time()
        bad = 0
        print(f"{'entity':<12} {'signal':<12} {'value':>12}  {'quality':<8} {'age':>8}  address")
        for ent, sigs in conn.points().items():
            for sig, addr in sigs.items():
                s = r.get(ent, {}).get(sig)
                if s is None:
                    bad += 1
                    print(f"{ent:<12} {sig:<12} {'-':>12}  {'MISSING':<8} {'':>8}  {addr}")
                    continue
                v = f"{s.value:.2f}" if isinstance(s.value, float) else str(s.value)
                age = now - s.ts
                ok = s.good and age < 3 * cfg.poll_seconds
                bad += not ok
                print(f"{ent:<12} {sig:<12} {v:>12}  {'good' if s.good else 'BAD':<8} {age:>7.0f}s  {addr}")
        conn.close()
        print(f"\n{bad} problem(s)" if bad else "\nAll tags readable.")
        return 1 if bad else 0

    from .notify import Dispatcher
    d = Dispatcher(cfg, None)
    if a.command == "test-sms":
        to = a.to or cfg.roster[0].phone
        d.q.put(("sms", to, "Test SMS - agent de surveillance des puits", None))
    else:
        to = [a.to] if a.to else cfg.email.get("alarm_to", [])
        d.email(to, "Test e-mail - agent de surveillance des puits", "Si vous lisez ce message, l'envoi d'e-mails fonctionne.")
    d.flush(60)
    print("Sent (check the recipient). Delivery errors, if any, are printed above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
