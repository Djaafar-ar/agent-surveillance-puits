"""OPC UA connector (read-only).

Most modern SCADA systems and historians expose an OPC UA server. The agent
connects as a client with a dedicated read-only account, reads the
configured nodes once per cycle and uses the OPC UA StatusCode and
SourceTimestamp to detect bad quality and stale data (lost RTU).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timezone

from asyncua import Client, ua

from ..config import secret
from .base import Connector, Sample


class OpcUaConnector(Connector):
    name = "opcua"

    def __init__(self, cfg, conn_cfg):
        super().__init__(cfg, conn_cfg)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="opcua-loop")
        self.thread.start()
        self.client: Client | None = None
        self._nodes: list = []      # (entity, signal, NodeId)
        self._clock_node = None
        self.timeout = float(conn_cfg.get("timeout_s", 10))

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout + 5)

    async def _aconnect(self):
        c = Client(url=self.c["url"], timeout=self.timeout)
        user = secret(self.c.get("username_env"))
        if user:
            c.set_user(user)
            c.set_password(secret(self.c.get("password_env")))
        sec = self.c.get("security")
        if sec:  # e.g. "Basic256Sha256,SignAndEncrypt,certs/agent-cert.der,certs/agent-key.pem"
            await c.set_security_string(sec)
        c.application_uri = self.c.get("application_uri", "urn:agent-surveillance-puits:client")
        await c.connect()
        self.client = c

    def _connect(self):
        self._run(self._aconnect())
        self._nodes = []
        for ent, sigs in self.points().items():
            for sig, addr in sigs.items():
                self._nodes.append((ent, sig, ua.NodeId.from_string(addr)))
        clk = (self.cfg.clock or {}).get("opcua_node")
        self._clock_node = ua.NodeId.from_string(clk) if clk else None

    async def _aread(self):
        ids = [n for _, _, n in self._nodes]
        if self._clock_node is not None:
            ids = ids + [self._clock_node]
        out = []
        for i in range(0, len(ids), 500):  # stay below server MaxNodesPerRead
            out += await self.client.read_attributes([self.client.get_node(x) for x in ids[i:i + 500]], ua.AttributeIds.Value)
        return out

    def _read(self):
        dvs = self._run(self._aread())
        res: dict[str, dict[str, Sample]] = {}
        for (ent, sig, _), dv in zip(self._nodes, dvs):
            ts = dv.SourceTimestamp or dv.ServerTimestamp
            t = ts.replace(tzinfo=timezone.utc).timestamp() if ts else 0.0
            good = dv.StatusCode is None or dv.StatusCode.is_good()
            v = dv.Value.Value if dv.Value is not None else None
            if isinstance(v, bool):
                pass
            elif isinstance(v, (int, float)):
                v = float(v)
            else:
                good = False
                v = None
            res.setdefault(ent, {})[sig] = Sample(v, t, good)
        if self._clock_node is not None:
            dv = dvs[-1]
            v = dv.Value.Value if dv.Value is not None else None
            self._clock = v.replace(tzinfo=timezone.utc).timestamp() if v is not None else None
        return res

    def now(self):
        return getattr(self, "_clock", None)

    def _close(self):
        if self.client is not None:
            try:
                self._run(self.client.disconnect())
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    # ---- commissioning helper -------------------------------------------
    def browse(self, start: str | None, depth: int = 3):
        """Yield (indent, nodeid, browse name, value) to help find tag addresses."""
        self._connect()
        self.connected = True

        async def walk(node, d, acc):
            try:
                children = await node.get_children()
            except Exception:  # noqa: BLE001
                return
            for ch in children:
                name = (await ch.read_browse_name()).Name
                cls = await ch.read_node_class()
                val = ""
                if cls == ua.NodeClass.Variable:
                    try:
                        val = repr(await ch.read_value())
                    except Exception:  # noqa: BLE001
                        val = "?"
                acc.append((d, ch.nodeid.to_string(), name, val))
                if d < depth and cls == ua.NodeClass.Object:
                    await walk(ch, d + 1, acc)

        async def go():
            root = self.client.get_node(start) if start else self.client.nodes.objects
            acc = []
            await walk(root, 0, acc)
            return acc
        return self._run(go())
