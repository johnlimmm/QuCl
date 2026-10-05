"""Protocol resource identities; the execution core owns FIFO scheduling and reservations."""

import copy
import numpy as np
import netsquid as ns
from netsquid.qubits import qubitapi as qapi, ketstates
from quantum_state import encode_state

STATES = {
    "0": np.array([1, 0], complex),
    "1": np.array([0, 1], complex),
    "+": np.array([1, 1], complex) / np.sqrt(2),
    "-": np.array([1, -1], complex) / np.sqrt(2),
    "+i": np.array([1, 1j], complex) / np.sqrt(2),
    "-i": np.array([1, -1j], complex) / np.sqrt(2),
}


class ProtocolAdapter:
    """Shared checkpoint and resource helpers; subclasses define each protocol resource layout."""

    def __init__(self, core, session, slot):
        self.core, self.session, self.sid = core, session, session["session_id"]
        self.slot = slot
        self.local = [("R", 2 * slot), ("R", 2 * slot + 1)]
        self.bits = None
        self.checkpoints = {}
        self.correction_count = 0
        self.create()
        self.original_output = tuple(self.qubits(self.survivors))
        self.checkpoints["initial"] = self.input_states()
        core.emit(
            "RESOURCES_READY",
            self.sid,
            core.roots[self.sid],
            protocol=self.protocol,
            resource_handles=self.inputs,
            created_ns=0,
        )

    def qubits(self, positions):
        return [self.core.peek(p) for p in positions]

    def put(self, qubit, location):
        self.core.devices[location[0]].put(qubit, positions=location[1])

    def pair(self, remote, local):
        a, b = qapi.create_qubits(2)
        qapi.operate(a, ns.H)
        qapi.operate([a, b], ns.CNOT)
        self.put(a, remote)
        self.put(b, local)

    def receive(self, row):
        op = "BSM" if row["event_type"] == "BSM_REQUEST" else "CORRECTION"
        if row["session_id"] != self.sid:
            raise ValueError("adapter session mismatch")
        bits = None
        if op == "CORRECTION":
            bits = [row["m1"], row["m2"]]
            if self.bits is None or bits != self.bits:
                raise ValueError("correction packet/result mismatch")
            if "packet_arrival" not in self.checkpoints:
                self.checkpoints["packet_arrival"] = self.output_state()
        elif "arrival" not in self.checkpoints:
            self.checkpoints["arrival"] = self.input_states()
        return self.core.submit(
            self,
            op,
            "R" if op == "BSM" else "B",
            self.inputs if op == "BSM" else ["output"],
            self.local if op == "BSM" else [("B", self.slot)],
            self.core.config["bsm_duration_ns" if op == "BSM" else "correction_duration_ns"],
            row["event_id"],
            bits,
        )

    def on_start(self, request):
        name = "bsm_start" if request["operation"] == "BSM" else "correction_start"
        self.checkpoints[name] = (
            self.input_states() if request["operation"] == "BSM" else self.output_state()
        )

    def on_complete(self, request):
        """Native adapters must read the completed QuantumProgram result."""
        raise NotImplementedError("Use a native protocol adapter")

    def output_state(self, corrected=False):
        target = self.target
        if not corrected:
            x, z = np.array([[0, 1], [1, 0]]), np.diag([1, -1])
            m1, m2 = self.bits
            correction = (z if m1 else np.eye(2)) @ (x if m2 else np.eye(2))
            inverse = correction.conj().T
            target = (
                np.kron(np.eye(2), inverse) if self.output_kind == "pair" else inverse
            ) @ target
        return encode_state(
            self.qubits(self.survivors), self.output_order, self.core.now_ns, target
        )

    def snapshot(self):
        return dict(
            protocol=self.protocol,
            session_id=self.sid,
            measurement_bits=self.bits,
            correction_count=self.correction_count,
            checkpoints=copy.deepcopy(self.checkpoints),
            logical_handles=self.logical_handles,
            input_created_ns=self.session.get("input_created_ns"),
            epr_created_ns=self.session["epr_created_ns"],
            input_state=self.session.get("input_state"),
            output=dict(
                kind=self.output_kind,
                locations=self.survivors,
                handle="output",
                state=self.core.resources.get((self.sid, "output"), {}).get("state"),
            ),
        )


class SwapAdapter(ProtocolAdapter):
    protocol = "swap"
    inputs = ["left_pair", "right_pair"]
    output_kind = "pair"
    output_order = ["A", "B"]
    target = ketstates.b00
    logical_handles = {"left_pair": 101, "right_pair": 102, "output": 103}

    def create(self):
        self.survivors = [("A", self.slot), ("B", self.slot)]
        for name, remote, local in zip(self.inputs, self.survivors, self.local):
            self.pair(remote, local)
            self.core.register(self.sid, name, "pair", [remote, local])

    def input_states(self):
        a, r1, r2, b = self.qubits([self.survivors[0]] + self.local + [self.survivors[1]])
        return dict(
            AR=encode_state([a, r1], ["A", "R_AR"], self.core.now_ns),
            RB=encode_state([r2, b], ["R_RB", "B"], self.core.now_ns),
        )


class TeleportAdapter(ProtocolAdapter):
    protocol = "teleport"
    inputs = ["input", "epr"]
    output_kind = "qubit"
    output_order = ["B"]
    logical_handles = {"input": 201, "epr": 202, "output": 203}

    def create(self):
        self.target = STATES[self.session["input_state"]]
        q = qapi.create_qubits(1)[0]
        qapi.assign_qstate([q], self.target)
        self.put(q, self.local[0])
        self.survivors = [("B", self.slot)]
        self.pair(self.survivors[0], self.local[1])
        self.core.register(self.sid, "input", "qubit", [self.local[0]])
        self.core.register(self.sid, "epr", "pair", [self.local[1], self.survivors[0]])

    def input_states(self):
        psi, a, b = self.qubits(self.local + self.survivors)
        return dict(
            input=encode_state([psi], ["input"], self.core.now_ns, self.target),
            epr=encode_state([a, b], ["Alice_EPR", "B"], self.core.now_ns),
        )
