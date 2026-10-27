"""Common types for SCADA connectors.

A connector only READS. None of the connector classes has a method that
writes to the SCADA, the historian or an RTU: this is the main safety
property of the agent and it is checked by tests/test_engine.py.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("agent.connector")


@dataclass
class Sample:
    value: float | bool | None
    ts: float          # source timestamp, epoch seconds (time the RTU/SCADA produced the value)
    good: bool = True  # OPC UA StatusCode good / Modbus read succeeded


class Connector:
    """Base class. Subclasses implement _connect, _read and _close."""

    name = "base"

    def __init__(self, cfg, conn_cfg: dict):
        self.cfg = cfg
        self.c = conn_cfg
        self.connected = False
        self.last_error = ""
        self.last_ok = 0.0
        self._next_retry = 0.0

    # Mapping entity id -> {signal: address}
    def points(self) -> dict[str, dict]:
        pts = {w.id: dict(w.tags) for w in self.cfg.wells}
        pts.update({m.id: dict(m.tags) for m in self.cfg.manifolds})
        return pts

    def read(self) -> dict[str, dict[str, Sample]]:
        """Read every configured point. Never raises: on failure returns {} and
        marks the connector disconnected, then retries with a back-off."""
        now = time.time()
        if not self.connected:
            if now < self._next_retry:
                return {}
            try:
                self._connect()
                self.connected = True
                self.last_error = ""
                log.info("%s: connected", self.name)
            except Exception as e:  # noqa: BLE001
                self._fail(f"connection failed: {e}")
                return {}
        try:
            out = self._read()
            self.last_ok = time.time()
            return out
        except Exception as e:  # noqa: BLE001
            self._fail(f"read failed: {e}")
            try:
                self._close()
            except Exception:  # noqa: BLE001
                pass
            return {}

    def now(self) -> float | None:
        """Optional clock provided by the data source (simulation only)."""
        return None

    def close(self):
        try:
            self._close()
        finally:
            self.connected = False

    def _fail(self, msg: str):
        if msg != self.last_error:
            log.warning("%s: %s", self.name, msg)
        self.last_error = msg
        self.connected = False
        self._next_retry = time.time() + 10

    # to implement
    def _connect(self): raise NotImplementedError
    def _read(self) -> dict[str, dict[str, Sample]]: raise NotImplementedError
    def _close(self): pass


def make_connector(cfg) -> Connector:
    t = cfg.connector["type"]
    if t == "opcua":
        from .opcua import OpcUaConnector
        return OpcUaConnector(cfg, cfg.connector)
    if t == "modbus":
        from .modbus import ModbusConnector
        return ModbusConnector(cfg, cfg.connector)
    if t == "csv":
        from .csvreplay import CsvConnector
        return CsvConnector(cfg, cfg.connector)
    raise ValueError(t)
