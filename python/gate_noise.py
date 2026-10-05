"""Optional per-operand depolarization, after storage noise and before a gate."""

import copy
import math
from netsquid.components import QuantumProcessor
from netsquid.components.instructions import INSTR_CNOT, INSTR_H, INSTR_X, INSTR_Z
from netsquid.components.models.qerrormodels import DepolarNoiseModel
from native_programs import physical_instructions


def split_config(raw):
    raw = copy.deepcopy(raw)
    if not isinstance(raw, dict):
        raise ValueError("scenario must be an object")
    p = raw.pop("gate_depolar_probability", 0.0)
    if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError("gate_depolar_probability must be finite in [0,1]")
    return raw, p


def install_gate_noise(core):
    """Replace physical-instruction definitions using the public native API."""
    devices = [d for d in core.devices.values() if isinstance(d, QuantumProcessor)]
    devices += getattr(core, "r_devices", [])
    p = core.config["gate_depolar_probability"]
    for device in devices:
        for instr in physical_instructions(
            core.config["native_instructions"], core.config["memory_noise"]
        ):
            if instr.instruction in (INSTR_CNOT, INSTR_H, INSTR_X, INSTR_Z):
                instr.quantum_noise_model = instr.quantum_noise_model + DepolarNoiseModel(
                    depolar_rate=p, time_independent=True
                )
            device.add_physical_instruction(instr)
