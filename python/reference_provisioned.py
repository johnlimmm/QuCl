#!/usr/bin/env python3
"""Independent NetSquid-only timed circuit, enumerating four measurement branches.

No co-sim, adapter, scheduler, or production QuantumProgram imports. A reference
measurement projects the specified outcome and records its Born probability;
all gate durations and memory evolution run on native QuantumProcessors.
"""
import json
import sys
import numpy as np
import netsquid as ns
from netsquid.components import QuantumMemory, QuantumProcessor, PhysicalInstruction, QuantumChannel
from netsquid.nodes import Network, Node
from netsquid.components.qprogram import QuantumProgram
from netsquid.components.instructions import (
    Instruction,
    INSTR_CNOT,
    INSTR_H,
    INSTR_X,
    INSTR_Z,
    IGate,
)
from netsquid.components.models.qerrormodels import T1T2NoiseModel, DepolarNoiseModel
from netsquid.protocols import Protocol
from netsquid.qubits import qubitapi as qapi


def run_branch(spec, bits):
    ns.sim_reset()
    ns.set_qstate_formalism(ns.QFormalism.DM)
    tele = spec["protocol"] == "teleport"
    d = spec["native_instructions"]
    noise = spec["memory_noise"]
    phi = np.array([1, 0, 0, 1], complex) / np.sqrt(2)
    states = {"0": [1, 0], "1": [0, 1], "+": [1, 1], "-": [1, -1], "+i": [1, 1j], "-i": [1, -1j]}
    target = np.array(states[spec["input_state"]], complex) if tele else phi.copy()
    target /= np.linalg.norm(target)
    qs = qapi.create_qubits(3 if tele else 4)
    if tele:
        qapi.assign_qstate(qs[0], target)
    for a, b in [(1, 2)] if tele else [(0, 1), (2, 3)]:
        qapi.operate(qs[a], ns.H)
        qapi.operate([qs[a], qs[b]], ns.CNOT)
    result = dict(probability=1.0, instructions=[], checkpoints={}, timing={})

    class Projection(Instruction):
        @property
        def name(self):
            return "reference_postselection"

        @property
        def num_positions(self):
            return 1

        def execute(self, quantum_memory, positions, outcome, **kwargs):
            q = quantum_memory.peek(positions)[0]
            probability = float(np.real(qapi.reduced_dm([q])[outcome, outcome]))
            result["probability"] *= probability
            if probability > 0:
                i = next(i for i, v in enumerate(qs) if v is q)
                op = np.array([[1.0]], complex)
                for j in range(len(qs)):
                    op = np.kron(op, np.diag([1 - outcome, outcome]) if i == j else np.eye(2))
                rho = qapi.reduced_dm(qs)
                qapi.assign_qstate(qs, op @ rho @ op.conj().T / probability)
            return [outcome]

    class Observe(Instruction):
        @property
        def name(self):
            return "reference_observe"

        @property
        def num_positions(self):
            return -1

        def execute(self, quantum_memory, positions, callback, **kwargs):
            callback()

    observe = Observe()
    projection = Projection()
    ix = IGate("ref_idle_x", ns.I)
    iz = IGate("ref_idle_z", ns.I)

    def physical(op, duration):
        error = T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"])
        if op in (INSTR_CNOT, INSTR_H, INSTR_X, INSTR_Z):
            error = error + DepolarNoiseModel(
                depolar_rate=spec["gate_depolar_probability"], time_independent=True
            )
        return PhysicalInstruction(
            op,
            duration=duration,
            parallel=False,
            quantum_noise_model=error,
            apply_q_noise_after=False,
        )

    r = QuantumProcessor(
        "reference_R",
        num_positions=2,
        phys_instructions=[
            physical(INSTR_CNOT, d["cnot_ns"]),
            physical(INSTR_H, d["h_ns"]),
            physical(projection, d["measure_ns"]),
        ],
    )
    b = QuantumProcessor(
        "reference_B",
        num_positions=1,
        phys_instructions=[
            physical(op, d[key])
            for op, key in [(INSTR_X, "x_ns"), (ix, "x_ns"), (INSTR_Z, "z_ns"), (iz, "z_ns")]
        ],
    )
    a = None if tele else QuantumMemory("reference_A", num_positions=1)
    for dev in (r, b):
        dev.add_physical_instruction(PhysicalInstruction(observe, duration=0, parallel=False))
    for dev in [r, b] + ([] if tele else [a]):
        for pos in dev.mem_positions:
            pos.models["noise_model"] = T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"])
    r.put(qs[:2] if tele else qs[1:3])
    survivors = [qs[-1]] if tele else [qs[0], qs[-1]]

    def now():
        return int(ns.sim_time())

    def touch_all():
        r.peek([0, 1])
        b.peek(0)
        if a is not None:
            a.peek(0)

    def state(qubits, order, ref=None):
        rho = qapi.reduced_dm(qubits)
        value = dict(
            sim_time_ns=now(),
            density_matrix=dict(qubit_order=order, real=rho.real.tolist(), imag=rho.imag.tolist()),
        )
        if ref is not None:
            value["fidelity"] = float(qapi.fidelity(qubits, ref, squared=True))
        return value

    def inputs():
        touch_all()
        return (
            dict(input=state(qs[:1], ["input"], target), epr=state(qs[1:], ["Alice_EPR", "B"], phi))
            if tele
            else dict(AR=state(qs[:2], ["A", "R_AR"], phi), RB=state(qs[2:], ["R_RB", "B"], phi))
        )

    def output(corrected=False):
        b.peek(0)
        if a is not None:
            a.peek(0)
        ref = target
        if not corrected:
            z, x = bits
            pauli = (np.diag([1, -1]) if z else np.eye(2)) @ (
                np.array([[0, 1], [1, 0]]) if x else np.eye(2)
            )
            inverse = pauli.conj().T
            ref = (inverse if tele else np.kron(np.eye(2), inverse)) @ ref
        return state(survivors, ["B"] if tele else ["A", "B"], ref)

    def record(op, index, name, start):
        if op == "BSM":
            touch_all()
            checkpoint = state(
                qs, ["input", "Alice_EPR", "B"] if tele else ["A", "R_AR", "R_RB", "B"]
            )
        else:
            checkpoint = output(True)
        result["instructions"].append(
            dict(
                operation=op,
                index=index,
                instruction=name,
                start_ns=start,
                completion_ns=now(),
                state=checkpoint,
            )
        )

    class Circuit(QuantumProgram):
        def __init__(self, operation):
            super().__init__(num_qubits=2 if operation == "BSM" else 1, parallel=False)
            self.operation = operation

        def program(self):
            if self.operation == "BSM":
                operations = [
                    ("CNOT", INSTR_CNOT, [0, 1], {}),
                    ("H", INSTR_H, [0], {}),
                    ("MEASURE_0", projection, [0], dict(outcome=bits[0])),
                    ("MEASURE_1", projection, [1], dict(outcome=bits[1])),
                ]
            else:
                operations = [
                    ("X" if bits[1] else "IDLE_X", INSTR_X if bits[1] else ix, [0], {}),
                    ("Z" if bits[0] else "IDLE_Z", INSTR_Z if bits[0] else iz, [0], {}),
                ]
            for i, (name, op, positions, params) in enumerate(operations):
                start = now()
                self.apply(op, positions, physical=True, **params)
                yield self.run(parallel=False)
                self.apply(
                    observe,
                    [0, 1] if self.operation == "BSM" else [0],
                    physical=True,
                    callback=lambda i=i, n=name, t=start: record(self.operation, i, n, t),
                )
                yield self.run(parallel=False)

    class Transaction(Protocol):
        def run(self):
            if spec["bsm_start_ns"] > ns.sim_time():
                yield self.await_timer(spec["bsm_start_ns"] - ns.sim_time())
            result["timing"]["bsm_start_ns"] = now()
            result["checkpoints"]["bsm_start"] = inputs()
            yield r.execute_program(Circuit("BSM"))
            result["timing"]["bsm_completion_ns"] = now()
            for q in r.pop([0, 1]):
                qapi.discard(q)
            result["checkpoints"]["frame"] = output()
            if spec["correction_arrival_ns"] > ns.sim_time():
                yield self.await_timer(spec["correction_arrival_ns"] - ns.sim_time())
            result["timing"]["correction_arrival_ns"] = now()
            result["checkpoints"]["packet_arrival"] = output()
            if spec["correction_start_ns"] > ns.sim_time():
                yield self.await_timer(spec["correction_start_ns"] - ns.sim_time())
            result["timing"]["correction_start_ns"] = now()
            result["checkpoints"]["correction_start"] = output()
            yield b.execute_program(Circuit("CORRECTION"))
            result["timing"]["correction_completion_ns"] = now()
            result["checkpoints"]["usable"] = output(True)

    # Independent network construction: no production backend/adapters imported.
    network = Network("reference_network")
    nodes = {"R": Node("reference_R_node", qmemory=r), "B": Node("reference_B_node", qmemory=b)}
    if a is not None:
        nodes["A"] = Node("reference_A_node", qmemory=a)
    network.add_nodes(list(nodes.values()))
    pending = {"RB"} if tele else {"RA", "RB"}
    result["deliveries"] = {}
    protocol = Transaction()

    def delivered(link, remote_index, local_index, node, message):
        assert len(message.items) == 1 and message.items[0] is qs[remote_index]
        node.qmemory.put(message.items[0], positions=0)
        r.peek(0 if link == "RA" else 1)
        pair = (
            [qs[remote_index], qs[local_index]]
            if link == "RA"
            else [qs[local_index], qs[remote_index]]
        )
        result["deliveries"][link] = state(
            pair, ["A", "R_AR"] if link == "RA" else ["R_RB", "B"], phi
        )
        pending.remove(link)
        if not pending:
            result["resource_ready_ns"] = now()
            result["checkpoints"]["resources_ready"] = inputs()
            protocol.start()

    for link, remote_index, local_index, dest in ([] if tele else [("RA", 0, 1, "A")]) + [
        ("RB", 2 if tele else 3, 1 if tele else 2, "B")
    ]:
        cfg = spec["quantum_links"][link]
        channel = QuantumChannel(
            "reference_" + link,
            delay=cfg["delay_ns"],
            models={
                "quantum_noise_model": DepolarNoiseModel(
                    depolar_rate=cfg["depolar_rate_hz"], time_independent=False
                )
            },
        )
        tx, rx = network.add_connection(nodes["R"], nodes[dest], channel_to=channel, label=link)
        node = nodes[dest]
        node.ports[rx].bind_input_handler(
            lambda msg, l=link, ri=remote_index, li=local_index, n=node: delivered(
                l, ri, li, n, msg
            )
        )
        nodes["R"].ports[tx].tx_output(qs[remote_index])
    ns.sim_run()
    return result


def run_reference(spec):
    return dict(
        model="independent-provisioned-native-postselection",
        spec=spec,
        branches={str(z) + str(x): run_branch(spec, (z, x)) for z in (0, 1) for x in (0, 1)},
    )


if __name__ == "__main__":
    json.dump(run_reference(json.load(sys.stdin)), sys.stdout, allow_nan=False)
    sys.stdout.write("\n")
