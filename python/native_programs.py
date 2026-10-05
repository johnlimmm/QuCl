"""Real QuantumPrograms. Each yield completes one native physical instruction."""

import netsquid as ns
from netsquid.components import PhysicalInstruction
from netsquid.components.qprogram import QuantumProgram
from netsquid.components.models.qerrormodels import T1T2NoiseModel
from netsquid.components.instructions import (
    Instruction,
    INSTR_CNOT,
    INSTR_H,
    INSTR_MEASURE,
    INSTR_X,
    INSTR_Z,
    IGate,
)

IDLE_X = IGate("IDLE_X", ns.I)
IDLE_Z = IGate("IDLE_Z", ns.I)


class Checkpoint(Instruction):
    """Zero-time observation inside the processor's permitted memory access."""

    @property
    def name(self):
        return "QUCL_CHECKPOINT"

    @property
    def num_positions(self):
        return -1

    def execute(self, quantum_memory, positions, callback, **kwargs):
        callback()


CHECKPOINT = Checkpoint()


def physical_instructions(d, noise):
    # Native processors exclude active instruction time from idle memory noise.
    # Use the same storage model during each physical instruction, before its
    # ideal state action. gate_noise.py adds optional per-operand depolarization.
    return [
        PhysicalInstruction(
            op,
            duration=d[key],
            parallel=False,
            quantum_noise_model=T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"]),
            apply_q_noise_after=False,
        )
        for op, key in (
            (INSTR_CNOT, "cnot_ns"),
            (INSTR_H, "h_ns"),
            (INSTR_MEASURE, "measure_ns"),
            (INSTR_X, "x_ns"),
            (IDLE_X, "x_ns"),
            (INSTR_Z, "z_ns"),
            (IDLE_Z, "z_ns"),
        )
    ] + [PhysicalInstruction(CHECKPOINT, duration=0, parallel=False)]


class NativeOperation(QuantumProgram):
    def __init__(self, operation, bits, started, finished, observe=True):
        super().__init__(num_qubits=2 if operation == "BSM" else 1, parallel=False)
        self.operation, self.bits = operation, bits
        self.started, self.finished = started, finished
        self.observe = observe

    def program(self):
        if self.operation == "BSM":
            steps = [
                ("CNOT", INSTR_CNOT, [0, 1], None),
                ("H", INSTR_H, [0], None),
                ("MEASURE_0", INSTR_MEASURE, [0], "m1"),
                ("MEASURE_1", INSTR_MEASURE, [1], "m2"),
            ]
        else:
            z, x = self.bits
            steps = [
                ("X" if x else "IDLE_X", INSTR_X if x else IDLE_X, [0], None),
                ("Z" if z else "IDLE_Z", INSTR_Z if z else IDLE_Z, [0], None),
            ]
        for index, (name, instruction, positions, output_key) in enumerate(steps):
            self.started(index, name)
            self.apply(instruction, positions, output_key=output_key, physical=True)
            yield self.run(parallel=False)
            if self.observe:
                self.apply(
                    CHECKPOINT,
                    [0, 1] if self.operation == "BSM" else [0],
                    physical=True,
                    callback=lambda i=index, n=name: self.finished(i, n),
                )
                yield self.run(parallel=False)
            else:
                self.finished(index, name)
