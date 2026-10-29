"""Command line.

    python -m agent run        --config config/config.yaml    start the surveillance
    python -m agent check      --config config/config.yaml    read every tag once and print it (commissioning)
    python -m agent browse     --config config/config.yaml    list the OPC UA nodes to find tag addresses
    python -m agent test-sms   --config config/config.yaml --to +213...
    python -m agent test-email --config config/config.yaml --to someone@site.dz
    python -m agent status     --config config/config.yaml    is the agent running? (exit code 0 = yes)
    python -m agent add-user   --config config/config.yaml --name "K. Benali" --role operator|supervisor|viewer
    python -m agent del-user   --config config/config.yaml --name "K. Benali"
    python -m agent backtest   --config config/config.yaml --csv export.csv --events events.csv [--out data/backtest]
                               replay a historian export and measure what the agent would have caught
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
    ap.add_argument("command", choices=["run", "check", "browse", "test-sms", "test-email", "backtest", "status", "add-user", "del-user"])
    ap.add_argument("--config", default=os.environ.get("AGENT_CONFIG", "config/config.yaml"))
    ap.add_argument("--to", help="recipient for test-sms / test-email")
    ap.add_argument("--node", help="browse: start node (default Objects)")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument("--name", help="add-user / del-user: person's name")
    ap.add_argument("--role", choices=["viewer", "operator", "supervisor"], default="operator")
    ap.add_argument("--csv", help="backtest: historian export (CSV)")
    ap.add_argument("--events", help="backtest: event log (CSV: datetime, equipment, type, description)")
    ap.add_argument("--out", default="data/backtest", help="backtest: output folder")
    ap.add_argument("--pattern", default="{id}.{signal}", help="backtest: column name pattern")
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

    if a.command == "status":
        from .upkeep import status
        code, txt = status(cfg)
        print(txt)
        return code

    if a.command in ("add-user", "del-user"):
        from .users import Users
        import getpass
        if not a.name:
            print("--name is required", file=sys.stderr)
            return 2
        u = Users(cfg)
        try:
            if a.command == "del-user":
                u.delete(a.name)
                print(f"Deleted {a.name}")
                return 0
            p1 = os.environ.get("AGENT_NEW_PASSWORD") or getpass.getpass(f"Password for {a.name}: ")
            p2 = os.environ.get("AGENT_NEW_PASSWORD") or getpass.getpass("Again: ")
            if p1 != p2:
                print("Passwords do not match", file=sys.stderr)
                return 2
            u.add(a.name, a.role, p1)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        print(f"{a.name} ({a.role}) saved in {u.path}")
        return 0

    if a.command == "backtest":
        if not a.csv:
            print("backtest needs --csv export.csv", file=sys.stderr)
            return 2
        from .backtest import Backtest
        logging.getLogger("agent").setLevel(logging.WARNING)
        bt = Backtest(cfg, a.csv, a.events, a.out, a.pattern)
        bt.run(progress=lambda d: d.day == 1 and print(f"  {d:%m/%Y}", flush=True))
        path = bt.write()
        print("\n" + bt.text())
        print(f"Report: {os.path.abspath(path)}")
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
