"""Turns the field model into SCADA-like samples (used by tests).

Silent RTUs keep their last value with its old timestamp, and their
COMM_OK flag goes false: this is what a real SCADA shows for a lost RTU.
"""

from datetime import datetime

from agent.connectors.base import Sample

from .field_model import Field


class Feed:
    def __init__(self, seed=1, start_minute=2 * 60 + 50):
        self.field = Field(seed, start_minute)
        midnight = datetime(2026, 5, 19).timestamp()
        self.t = midnight + start_minute * 60   # clock consistent with the field's time of day
        self.last: dict = {}

    def step(self):
        self.field.step()
        self.t += 60
        vals, silent = self.field.values()
        out = {}
        for ent, sigs in vals.items():
            d = {}
            for s, v in sigs.items():
                if ent in silent and s != "comm_ok":
                    old = self.last.get((ent, s))
                    if old is not None:
                        d[s] = old
                    continue
                smp = Sample(v, self.t, True)
                self.last[(ent, s)] = smp
                d[s] = smp
            out[ent] = d
        return self.t, out
