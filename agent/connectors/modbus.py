"""Modbus TCP connector (read-only function codes only: 1, 2, 3, 4).

Used when the RTUs or a SCADA front-end expose Modbus TCP. Each signal is
described by:

    {host: 10.0.0.21, port: 502, unit: 1, kind: holding, address: 100,
     type: float32, word_order: big, scale: 0.1, offset: 0}

kind: holding (FC3) | input (FC4) | coil (FC1) | discrete (FC2)
type: int16 | uint16 | int32 | uint32 | float32 | bool

Modbus has no timestamps: a value read successfully is stamped with the read
time, and a failed read (timeout, exception) marks the whole RTU as not
communicating, which is how a lost radio link shows up.
"""

from __future__ import annotations

import struct
import time

from pymodbus.client import ModbusTcpClient

from .base import Connector, Sample

READ_ONLY_KINDS = ("holding", "input", "coil", "discrete")


def _decode(regs: list[int], typ: str, word_order: str) -> float:
    if typ == "int16":
        return struct.unpack(">h", struct.pack(">H", regs[0]))[0]
    if typ == "uint16":
        return regs[0]
    ws = regs[:2] if word_order == "big" else regs[1::-1]
    raw = struct.pack(">HH", *ws)
    return struct.unpack({"int32": ">i", "uint32": ">I", "float32": ">f"}[typ], raw)[0]


class ModbusConnector(Connector):
    name = "modbus"

    def __init__(self, cfg, conn_cfg):
        super().__init__(cfg, conn_cfg)
        self.clients: dict[tuple, ModbusTcpClient] = {}
        for ent, sigs in self.points().items():
            for sig, p in sigs.items():
                if not isinstance(p, dict) or p.get("kind", "holding") not in READ_ONLY_KINDS:
                    raise ValueError(f"{ent}.{sig}: Modbus point must be a mapping with kind in {READ_ONLY_KINDS}")

    def _client(self, host, port):
        key = (host, port)
        c = self.clients.get(key)
        if c is None:
            c = ModbusTcpClient(host, port=port, timeout=float(self.c.get("timeout_s", 3)), retries=1)
            self.clients[key] = c
        if not c.connected:
            c.connect()
        return c

    def _connect(self):
        # Connections are opened lazily per RTU in _read: one dead RTU must not
        # stop the others from being read.
        pass

    @staticmethod
    def _call(fn, address, count, unit):
        try:
            return fn(address, count=count, device_id=unit)
        except TypeError:  # pymodbus < 3.10
            return fn(address, count=count, slave=unit)

    def _read_point(self, p) -> float | bool:
        host = p.get("host", self.c.get("host"))
        port = int(p.get("port", self.c.get("port", 502)))
        unit = int(p.get("unit", self.c.get("unit", 1)))
        kind = p.get("kind", "holding")
        typ = p.get("type", "bool" if kind in ("coil", "discrete") else "uint16")
        c = self._client(host, port)
        addr = int(p["address"])
        if kind in ("coil", "discrete"):
            fn = c.read_coils if kind == "coil" else c.read_discrete_inputs
            r = self._call(fn, addr, 1, unit)
            if r.isError():
                raise IOError(str(r))
            v = bool(r.bits[0])
            return (not v) if p.get("invert") else v
        n = 1 if typ in ("int16", "uint16", "bool") else 2
        fn = c.read_holding_registers if kind == "holding" else c.read_input_registers
        r = self._call(fn, addr, n, unit)
        if r.isError():
            raise IOError(str(r))
        if typ == "bool":
            v = bool(r.registers[0] & (1 << int(p.get("bit", 0))))
            return (not v) if p.get("invert") else v
        return _decode(r.registers, typ, p.get("word_order", "big")) * float(p.get("scale", 1)) + float(p.get("offset", 0))

    def _read(self):
        res = {}
        any_ok = False
        dead_rtus: set = set()
        for ent, sigs in self.points().items():
            for sig, p in sigs.items():
                rtu = (p.get("host", self.c.get("host")), p.get("unit", self.c.get("unit", 1)))
                if rtu in dead_rtus:
                    continue
                try:
                    v = self._read_point(p)
                    res.setdefault(ent, {})[sig] = Sample(v, time.time(), True)
                    any_ok = True
                except Exception:  # noqa: BLE001
                    dead_rtus.add(rtu)  # skip the other points of this RTU this cycle
        if not any_ok and res == {}:
            raise IOError("no Modbus point could be read")
        return res

    def _close(self):
        for c in self.clients.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
