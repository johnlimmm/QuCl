"""Native gate durations and derived BSM/correction durations."""

import copy
from session_config import normalize_config as normalize_sessions
from limits import integer, MAX_TIME_NS

# Illustrative durations, not hardware calibration. Measurements are serial.
DEFAULT_INSTRUCTIONS = dict(
    cnot_ns=600000, h_ns=200000, measure_ns=400000, x_ns=250000, z_ns=250000
)


def normalize_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("native scenario must be an object")
    raw = copy.deepcopy(raw)
    given = raw.pop("native_instructions", {})
    if not isinstance(given, dict) or set(given) - set(DEFAULT_INSTRUCTIONS):
        raise ValueError("invalid native instruction durations")
    durations = dict(DEFAULT_INSTRUCTIONS, **given)
    for name, value in durations.items():
        integer(value, name, 1, MAX_TIME_NS)
    totals = dict(
        bsm_duration_ns=durations["cnot_ns"] + durations["h_ns"] + 2 * durations["measure_ns"],
        correction_duration_ns=durations["x_ns"] + durations["z_ns"],
    )
    for name, value in totals.items():
        if name in raw and raw[name] != value:
            raise ValueError(name + " must equal the native instruction sum")
        raw[name] = value
    config = normalize_sessions(raw)
    config["native_instructions"] = durations
    return config
