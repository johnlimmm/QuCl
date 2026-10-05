"""Lossless, pre-generated EPR delivery; ideal zero-delay readiness knowledge."""

import copy
import math
from native_config import normalize_config as normalize_native
from session_config import result_fifo
from limits import integer, MAX_TIME_NS

DEFAULT_LINKS = {
    "RA": dict(delay_ns=800000, depolar_rate_hz=0),
    "RB": dict(delay_ns=1200000, depolar_rate_hz=0),
}


def readiness(config, session):
    links = ["RA", "RB"] if session["protocol"] == "swap" else ["RB"]
    return max(config["quantum_links"][link]["delay_ns"] for link in links)


def normalize_config(raw):
    raw = copy.deepcopy(raw)
    if not isinstance(raw, dict):
        raise ValueError("provisioned scenario must be an object")
    links = raw.pop("quantum_links", {})
    if not isinstance(links, dict) or set(links) - set(DEFAULT_LINKS):
        raise ValueError("unknown quantum link")
    config = normalize_native(raw)
    config["quantum_links"] = copy.deepcopy(DEFAULT_LINKS)
    for name, given in links.items():
        if not isinstance(given, dict) or set(given) - {"delay_ns", "depolar_rate_hz"}:
            raise ValueError(
                "quantum links support fixed delay and native depolarization only; loss is unsupported"
            )
        config["quantum_links"][name].update(given)
    for link in config["quantum_links"].values():
        integer(link["delay_ns"], "quantum channel delay", maximum=MAX_TIME_NS)
        rate = link["depolar_rate_hz"]
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1e12:
            raise ValueError("invalid depolarization rate in Hz")
    if expected_timing(config)["simulation_completion_ns"] > MAX_TIME_NS:
        raise ValueError("provisioning time overflow")
    return config


def expected_timing(config):
    """Independent oracle: ready-gated FIFO, packet FIFO, then B FIFO.

    At ties, already-ready session-start events precede newly delivered readiness
    notifications; each batch uses config order, as specified by the IPC adapter.
    """
    ordered = sorted(
        enumerate(config["sessions"]),
        key=lambda it: (
            max(it[1]["session_start_ns"], readiness(config, it[1])),
            readiness(config, it[1]) >= it[1]["session_start_ns"],
            it[0],
        ),
    )
    sessions = {}
    controls = []
    free = 0
    for slot, s in ordered:
        ready = readiness(config, s)
        eligible = max(s["session_start_ns"], ready)
        start = max(free, eligible)
        free = start + config["bsm_duration_ns"]
        sessions[str(s["session_id"])] = dict(
            session_start_ns=s["session_start_ns"],
            resource_ready_ns=ready,
            eligible_ns=eligible,
            resource_wait_ns=eligible - s["session_start_ns"],
            bsm_arrival_ns=eligible,
            bsm_start_ns=start,
            bsm_completion_ns=free,
            bsm_wait_ns=start - eligible,
        )
        controls.append(
            dict(
                packet_id=slot + 1,
                session_id=s["session_id"],
                app_tx_ns=free,
                payload_bytes=2 if s["protocol"] == "teleport" else config["result_payload_bytes"],
            )
        )
    packets = result_fifo(config, controls)
    free = 0
    for p in packets:
        if not p["session_id"]:
            continue
        start = max(free, p["app_rx_ns"])
        free = start + config["correction_duration_ns"]
        sessions[str(p["session_id"])].update(
            correction_arrival_ns=p["app_rx_ns"],
            correction_start_ns=start,
            correction_completion_ns=free,
            correction_wait_ns=start - p["app_rx_ns"],
        )
    drain = max(p["app_rx_ns"] for p in packets)
    batch = max(t["correction_completion_ns"] for t in sessions.values())
    return dict(
        sessions=sessions,
        packets=packets,
        batch_completion_ns=batch,
        traffic_drain_completion_ns=drain,
        simulation_completion_ns=max(batch, drain),
    )
