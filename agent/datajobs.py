"""Upload data to the running agent from the dashboard (supervisor), with no command line:

  1. upload the historian export (+ the tag list and the logbook, if there are any)
  2. the agent proposes which tag is which signal; the supervisor corrects it
  3. import: cleaning and data-quality report
  4. then either or both:
       - backtest: what the agent would have said on that period (report)
       - load into the history: the long-term analysis uses those months at once

Long jobs run in the background (the backtest in a separate process, so that the
surveillance never slows down); the page follows their progress.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid

import yaml

from . import importer

MAX_UPLOAD = 1024 ** 3      # 1 GB


class DataJobs:
    def __init__(self, app):
        self.app = app
        self.base = os.path.dirname(os.path.abspath(app.cfg.storage["path"]))
        self.up = os.path.join(self.base, "uploads")
        self.rep = os.path.join(self.base, "reports")
        self.jobs: dict = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ files
    def save_upload(self, name: str, stream, length: int) -> dict:
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError("file empty or larger than 1 GB")
        os.makedirs(self.up, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(name or "fichier.csv"))[-80:]
        fid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + "-" + safe
        path = os.path.join(self.up, fid)
        left = length
        with open(path, "wb") as f:
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(path)
            raise ValueError("upload interrupted")
        return {"id": fid, "name": name, "size": length}

    def _file(self, fid):
        if not fid:
            return None
        p = os.path.join(self.up, os.path.basename(fid))
        if not os.path.exists(p):
            raise ValueError(f"file not found: {fid}")
        return p

    # ------------------------------------------------------------------ jobs
    def _new(self, kind, fn, *args):
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "status": "running", "t0": time.time(), "progress": "", "result": None, "error": ""}
        with self.lock:
            self.jobs[jid] = job

        def go():
            try:
                job["result"] = fn(job, *args)
                job["status"] = "done"
            except Exception as e:  # noqa: BLE001
                job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
            job["t1"] = time.time()
        threading.Thread(target=go, daemon=True, name=f"job-{kind}").start()
        return job

    def get(self, jid):
        return self.jobs.get(jid)

    def list(self):
        return sorted(self.jobs.values(), key=lambda j: -j["t0"])[:20]

    def guess(self, data, tags=None):
        def fn(job, d, t):
            out = os.path.join(self.rep, "import-" + job["id"])
            r = importer.run(self.app.cfg, d, out, taglist_path=t, guess_only=True)
            m = importer.load_mapping(r["mapping"])
            rows = [{"entity": e, "signal": s, "tag": v["tag"], "unit": v.get("unit", "bar")} for e in sorted(m) for s, v in sorted(m[e].items())]
            return {"rows": rows, "unknown": r["unknown"], "info": r["info"]}
        return self._new("guess", fn, self._file(data), self._file(tags))

    def run_import(self, data, rows, events=None):
        mapping = {}
        known = {w.id for w in self.app.cfg.wells} | {m.id for m in self.app.cfg.manifolds}
        for r in rows:
            if r.get("tag") and r.get("entity") in known:
                mapping.setdefault(r["entity"], {})[r["signal"]] = {"tag": r["tag"], "unit": r.get("unit") or "bar"}
        if not mapping:
            raise ValueError("no tag mapped to a configured well or manifold")

        def fn(job, d, ev):
            out = os.path.join(self.rep, "import-" + job["id"])
            os.makedirs(out, exist_ok=True)
            mp = os.path.join(out, "mapping.yaml")
            with open(mp, "w", encoding="utf-8") as f:
                yaml.safe_dump({"tags": mapping}, f, allow_unicode=True)
            job["progress"] = "nettoyage"
            r = importer.run(self.app.cfg, d, out, mp, ev)
            return {"summary": importer.summary_text(r), "dir": os.path.basename(out), "export": r["export"], "events": r.get("events"),
                    "quality": f"/reports/{os.path.basename(out)}/quality.html", "event_counts": r.get("event_counts"),
                    "review": len(r.get("review", []))}
        return self._new("import", fn, self._file(data), self._file(events))

    def _import_result(self, jid):
        j = self.get(jid)
        if not j or j["kind"] != "import" or j["status"] != "done":
            raise ValueError("run the import first")
        return j["result"]

    def backtest(self, import_job):
        r = self._import_result(import_job)

        def fn(job, r):
            out = os.path.join(self.rep, "backtest-" + job["id"])
            cmd = [sys.executable, "-m", "agent", "backtest", "--config", self.app.cfg.path, "--csv", r["export"], "--out", out]
            if r.get("events"):
                cmd += ["--events", r["events"]]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            lines = []
            for line in p.stdout:
                lines.append(line.rstrip())
                if re.match(r"^\s+\d\d/\d{4}", line):
                    job["progress"] = line.strip()
            p.wait()
            if p.returncode:
                raise RuntimeError("\n".join(lines[-8:]))
            text = "\n".join(lines)
            summary = "\n".join(x for x in lines if x.startswith(("Problèmes", "Alarmes", "Constats", "Real problems", "Alarms", "Long-term")))
            return {"report": f"/reports/{os.path.basename(out)}/report.html", "summary": summary, "text": text}
        return self._new("backtest", fn, r)

    def load_history(self, import_job):
        r = self._import_result(import_job)

        def fn(job, r):
            res = importer.seed_history(self.app.store, self.app.logbook, r["export"], r.get("events"), self.app.cfg)
            self.app._analysis_cache = None
            return res
        return self._new("history", fn, r)

    def report_path(self, rel):
        """Safe path of a report file under data/reports."""
        p = os.path.abspath(os.path.join(self.rep, rel))
        if not p.startswith(os.path.abspath(self.rep) + os.sep) or not os.path.isfile(p):
            return None
        return p
