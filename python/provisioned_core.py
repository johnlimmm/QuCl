"""Resource delivery gates the unchanged native shared R/B execution core."""

from native_core import NativeExecutionCore, NativeHybridFederation
from native_adapters import NativeSwapAdapter, NativeTeleportAdapter
from protocol_adapters import STATES
from netsquid.qubits import qubitapi as qapi
from quantum_network_backend import QuantumNetworkBackend


class ProvisionedCore(NativeExecutionCore):
    def __init__(self, config, capture_states=True):
        super().__init__(config, capture_states)
        self.pending_ready = []
        self.quantum_network = QuantumNetworkBackend(self)

    def snapshot(self):
        value = super().snapshot()
        value["quantum_network"] = self.quantum_network.snapshot()
        if self.pending_ready:
            raise RuntimeError("resource notification not delivered")
        return value


class ProvisionedAdapter:
    def __init__(self, core, session, slot):
        self.core, self.session, self.sid = core, session, session["session_id"]
        self.slot = slot
        self.local = [("R", 2 * slot), ("R", 2 * slot + 1)]
        self.bits = None
        self.checkpoints = {}
        self.correction_count = 0
        self.ready_ns = None
        self.survivors = [("A", slot), ("B", slot)] if self.protocol == "swap" else [("B", slot)]
        if self.protocol == "teleport":
            self.target = STATES[session["input_state"]]
            q = qapi.create_qubits(1)[0]
            qapi.assign_qstate([q], self.target)
            self.put(q, self.local[0])
            core.register(self.sid, "input", "qubit", [self.local[0]])
            core.resources[(self.sid, "input")]["ready_ns"] = 0
            core.emit(
                "INPUT_CREATED",
                self.sid,
                core.roots[self.sid],
                resource_handle="input",
                created_ns=0,
            )
            core.quantum_network.pair(self, "epr", self.survivors[0], self.local[1])
        else:
            for handle, remote, local in zip(self.inputs, self.survivors, self.local):
                core.quantum_network.pair(self, handle, remote, local)

    def resource_available(self, cause):
        if not all(self.core.resources[(self.sid, h)]["state"] == "AVAILABLE" for h in self.inputs):
            return
        if self.ready_ns is not None:
            raise RuntimeError("duplicate resources-ready")
        c = self.core
        self.ready_ns = c.now_ns
        self.original_output = tuple(self.qubits(self.survivors))
        self.checkpoints["resources_ready"] = self.input_states()
        event = c.emit(
            "RESOURCES_READY",
            self.sid,
            cause,
            protocol=self.protocol,
            resource_handles=self.inputs,
            ready_ns=self.ready_ns,
            notification="ideal_zero_delay",
        )
        c.pending_ready.append(dict(session_id=self.sid, cause=event))
        if c.wakeup is None:
            raise RuntimeError("delivery has no federation receiver")
        c.wakeup(c.now_ns)

    def receive(self, row):
        if self.ready_ns is None:
            raise RuntimeError("quantum request before resource readiness")
        return super().receive(row)

    def snapshot(self):
        value = super().snapshot()
        value["resource_ready_ns"] = self.ready_ns
        return value


class ProvisionedSwapAdapter(ProvisionedAdapter, NativeSwapAdapter):
    pass


class ProvisionedTeleportAdapter(ProvisionedAdapter, NativeTeleportAdapter):
    pass


class ProvisionedFederation(NativeHybridFederation):
    def step(self, event):
        # Same native calendar and callback-driven execution; readiness is an
        # additional domain event, never an invented BSM completion.
        c, p = self.core, self.participant
        c.sync_time()
        at = c.now_ns
        self.pending.remove(at)
        if len(self.steps) >= 100000:
            raise RuntimeError("provisioned step limit")
        c.round = len(self.steps)
        c.phase = "NS3"
        completed = c.completed
        c.completed = []
        ready = c.pending_ready
        c.pending_ready = []
        ready.sort(key=lambda r: c.adapters[r["session_id"]].slot)
        rows = p.advance(at)
        self.rows.extend(rows)
        c.import_rows(rows)
        c.phase = "INPUT"
        for row in rows:
            if row["event_type"] in ("BSM_REQUEST", "CORRECTION_REQUEST"):
                c.adapters[row["session_id"]].receive(row)
        c.phase = "DISPATCH"
        c.dispatch()
        p.notify_ready(at, ready)
        p.inject(at, completed)
        if c.now_ns != p.now_ns:
            raise RuntimeError("provisioned clocks differ")
        self.steps.append(
            dict(
                time_ns=at,
                ns3_next_ns=p.next_time,
                quantum_next_ns=c.horizon(),
                completed=len(completed),
                resources_ready=len(ready),
                inputs=len(rows),
            )
        )
        self.schedule(p.next_time)
