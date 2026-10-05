#!/usr/bin/env python3
"""Independent native two-stage circuit with 16 possible joint BSM branches.

No production core, adapter, program, noise helper or scheduler is imported.
Only operation timestamps, initial state and physical parameters are inputs.
"""
import itertools
import json
import sys
import numpy as np
import netsquid as ns
from netsquid.components import QuantumProcessor, PhysicalInstruction, QuantumChannel
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
    d = spec["native_instructions"]
    noise = spec["memory_noise"]
    target = np.array(
        {"0": [1, 0], "1": [0, 1], "+": [1, 1], "-": [1, -1], "+i": [1, 1j], "-i": [1, -1j]}[
            spec["input_state"]
        ],
        complex,
    )
    target /= np.linalg.norm(target)
    phi = np.array([1, 0, 0, 1], complex) / np.sqrt(2)
    psi, a, r1, r2, b = qapi.create_qubits(5)
    active = [psi, a, r1, r2, b]
    qapi.assign_qstate(psi, target)
    for x, y in ((a, r1), (r2, b)):
        qapi.operate(x, ns.H)
        qapi.operate([x, y], ns.CNOT)
    result = dict(
        probability=1.0, stages=[dict(timing={}, checkpoints={}, instructions=[]) for _ in range(2)]
    )

    class Projection(Instruction):
        @property
        def name(self):
            return "chain_reference_projection"

        @property
        def num_positions(self):
            return 1

        def execute(self, memory, positions, outcome, **kwargs):
            q = memory.peek(positions)[0]
            probability = float(np.real(qapi.reduced_dm([q])[outcome, outcome]))
            result["probability"] *= probability
            if probability > 0:
                op = np.array([[1.0]], complex)
                for v in active:
                    op = np.kron(op, np.diag([1 - outcome, outcome]) if v is q else np.eye(2))
                rho = qapi.reduced_dm(active)
                qapi.assign_qstate(active, op @ rho @ op.conj().T / probability)
            return [outcome]

    class Observe(Instruction):
        @property
        def name(self):
            return "chain_reference_observe"

        @property
        def num_positions(self):
            return -1

        def execute(self, memory, positions, callback, **kwargs):
            callback()

    projection = Projection()
    observe = Observe()
    ix = IGate("chain_ref_ix", ns.I)
    iz = IGate("chain_ref_iz", ns.I)

    def physical(op, t):
        error = T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"])
        if op in (INSTR_CNOT, INSTR_H, INSTR_X, INSTR_Z):
            error = error + DepolarNoiseModel(
                depolar_rate=spec["gate_depolar_probability"], time_independent=True
            )
        return PhysicalInstruction(
            op, duration=t, parallel=False, quantum_noise_model=error, apply_q_noise_after=False
        )

    def processor(name, n):
        dev = QuantumProcessor(
            name,
            num_positions=n,
            phys_instructions=[
                physical(INSTR_CNOT, d["cnot_ns"]),
                physical(INSTR_H, d["h_ns"]),
                physical(projection, d["measure_ns"]),
                physical(INSTR_X, d["x_ns"]),
                physical(ix, d["x_ns"]),
                physical(INSTR_Z, d["z_ns"]),
                physical(iz, d["z_ns"]),
                PhysicalInstruction(observe, duration=0, parallel=False),
            ],
        )
        for pos in dev.mem_positions:
            pos.models["noise_model"] = T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"])
        return dev

    A, R, B = processor("reference_A", 2), processor("reference_R", 2), processor("reference_B", 1)
    A.put(psi, positions=0)
    R.put([r1, r2])

    def now():
        return int(ns.sim_time())

    def state(qs, order, ref=None):
        dm = qapi.reduced_dm(qs)
        value = dict(
            sim_time_ns=now(),
            density_matrix=dict(qubit_order=order, real=dm.real.tolist(), imag=dm.imag.tolist()),
        )
        if ref is not None:
            value["fidelity"] = float(qapi.fidelity(qs, ref, squared=True))
        return value

    def inputs(stage):
        if stage == 0:
            A.peek(1)
            R.peek([0, 1])
            B.peek(0)
            return dict(
                AR=state([a, r1], ["A", "R_AR"], phi), RB=state([r2, b], ["R_RB", "B"], phi)
            )
        A.peek([0, 1])
        B.peek(0)
        return dict(
            input=state([psi], ["input"], target), epr=state([a, b], ["Alice_EPR", "B"], phi)
        )

    def output(stage, corrected=False):
        B.peek(0)
        if stage == 0:
            A.peek(1)
        ref = phi if stage == 0 else target
        if not corrected:
            z, x = bits[stage]
            pauli = (np.diag([1, -1]) if z else np.eye(2)) @ (
                np.array([[0, 1], [1, 0]]) if x else np.eye(2)
            )
            ref = (np.kron(np.eye(2), pauli.conj().T) if stage == 0 else pauli.conj().T) @ ref
        return state([a, b] if stage == 0 else [b], ["A", "B"] if stage == 0 else ["B"], ref)

    def record(stage, operation, index, name, start):
        if operation == "BSM":
            inputs(stage)
            value = state(
                [a, r1, r2, b] if stage == 0 else [psi, a, b],
                ["A", "R_AR", "R_RB", "B"] if stage == 0 else ["input", "Alice_EPR", "B"],
            )
        else:
            value = output(stage, True)
        result["stages"][stage]["instructions"].append(
            dict(
                operation=operation,
                index=index,
                instruction=name,
                start_ns=start,
                completion_ns=now(),
                state=value,
            )
        )

    class Circuit(QuantumProgram):
        def __init__(self, stage, operation):
            super().__init__(num_qubits=2 if operation == "BSM" else 1, parallel=False)
            self.stage, self.operation = stage, operation

        def program(self):
            z, x = bits[self.stage]
            ops = (
                [
                    ("CNOT", INSTR_CNOT, [0, 1], {}),
                    ("H", INSTR_H, [0], {}),
                    ("MEASURE_0", projection, [0], dict(outcome=z)),
                    ("MEASURE_1", projection, [1], dict(outcome=x)),
                ]
                if self.operation == "BSM"
                else [
                    ("X" if x else "IDLE_X", INSTR_X if x else ix, [0], {}),
                    ("Z" if z else "IDLE_Z", INSTR_Z if z else iz, [0], {}),
                ]
            )
            for i, (name, op, pos, kw) in enumerate(ops):
                start = now()
                self.apply(op, pos, physical=True, **kw)
                yield self.run(parallel=False)
                self.apply(
                    observe,
                    [0, 1] if self.operation == "BSM" else [0],
                    physical=True,
                    callback=lambda i=i, n=name, t=start: record(
                        self.stage, self.operation, i, n, t
                    ),
                )
                yield self.run(parallel=False)

    class Transaction(Protocol):
        def run(self):
            for stage, dev in enumerate((R, A)):
                timing = spec["stages"][stage]
                out = result["stages"][stage]
                for field in ("arrival_ns", "start_ns"):
                    at = timing["bsm"][field]
                    if at > now():
                        yield self.await_timer(at - now())
                    if now() != at:
                        raise ValueError("reference BSM timestamp precedes resource/previous stage")
                    out["timing"].setdefault("bsm", {})[field] = now()
                out["checkpoints"]["bsm_start"] = inputs(stage)
                yield dev.execute_program(Circuit(stage, "BSM"), qubit_mapping=[0, 1])
                out["timing"]["bsm"]["completion_ns"] = now()
                for q in dev.pop([0, 1]):
                    active.remove(q)
                    qapi.discard(q)
                out["checkpoints"]["frame"] = output(stage)
                for field, name in (
                    ("arrival_ns", "packet_arrival"),
                    ("start_ns", "correction_start"),
                ):
                    at = timing["correction"][field]
                    if at > now():
                        yield self.await_timer(at - now())
                    if now() != at:
                        raise ValueError("reference correction timestamp precedes BSM/arrival")
                    out["timing"].setdefault("correction", {})[field] = now()
                    out["checkpoints"][name] = output(stage)
                yield B.execute_program(Circuit(stage, "CORRECTION"), qubit_mapping=[0])
                out["timing"]["correction"]["completion_ns"] = now()
                out["checkpoints"]["usable"] = output(stage, True)

    network = Network("chain_reference_network")
    nodes = {n: Node("reference_" + n, qmemory=dev) for n, dev in [("A", A), ("R", R), ("B", B)]}
    network.add_nodes(list(nodes.values()))
    pending = {"RA", "RB"}
    protocol = Transaction()

    def delivered(link, q, node, position, message):
        if message.items != [q]:
            raise RuntimeError("reference delivery identity")
        node.qmemory.put(q, positions=position)
        pending.remove(link)
        if not pending:
            protocol.start()

    for link, q, destination, pos in [("RA", a, "A", 1), ("RB", b, "B", 0)]:
        cfg = spec["quantum_links"][link]
        channel = QuantumChannel(
            "ref_" + link,
            delay=cfg["delay_ns"],
            models={
                "quantum_noise_model": DepolarNoiseModel(
                    depolar_rate=cfg["depolar_rate_hz"], time_independent=False
                )
            },
        )
        tx, rx = network.add_connection(
            nodes["R"], nodes[destination], channel_to=channel, label=link
        )
        node = nodes[destination]
        node.ports[rx].bind_input_handler(
            lambda msg, l=link, q=q, n=node, p=pos: delivered(l, q, n, p, msg)
        )
        nodes["R"].ports[tx].tx_output(q)
    ns.sim_run()
    return result


def run_reference(spec):
    observed_key = "".join(str(v) for pair in spec["bits"] for v in pair)
    if not spec.get("enumerate_branches"):
        return dict(
            model="independent-native-chained-postselection",
            observed=run_branch(spec, spec["bits"]),
        )
    branches = {}
    for values in itertools.product((0, 1), repeat=4):
        branches["".join(map(str, values))] = run_branch(spec, [values[:2], values[2:]])
    return dict(
        model="independent-native-chained-postselection",
        observed=branches[observed_key],
        branches=branches,
        expected_fidelity=sum(
            b["probability"] * b["stages"][1]["checkpoints"]["usable"]["fidelity"]
            for b in branches.values()
        ),
    )


if __name__ == "__main__":
    json.dump(run_reference(json.load(sys.stdin)), sys.stdout, allow_nan=False)
