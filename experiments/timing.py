"""Independent ready-gated schedules for the evaluation variants."""

from session_config import result_fifo
from provisioned_config import readiness, expected_timing


def eligible_order(config):
    return sorted(
        enumerate(config["sessions"]),
        key=lambda item: (
            max(item[1]["session_start_ns"], readiness(config, item[1])),
            readiness(config, item[1]) >= item[1]["session_start_ns"],
            item[0],
        ),
    )


def variant_timing(config, model, delays=None):
    if model == "Full-Sync":
        return expected_timing(config)
    if model not in ("Fixed-Dc", "No-Dq-R"):
        raise ValueError("unknown provisioned model")
    if model == "Fixed-Dc" and (
        not isinstance(delays, dict)
        or set(delays) != {"result_ns"}
        or type(delays["result_ns"]) is not int
        or delays["result_ns"] < 0
    ):
        raise ValueError("Fixed-Dc requires only a nonnegative integer result_ns")
    sessions, controls, r_free = {}, [], 0
    for slot, session in eligible_order(config):
        ready = readiness(config, session)
        eligible = max(session["session_start_ns"], ready)
        start = eligible if model == "No-Dq-R" else max(eligible, r_free)
        r_free = start + config["bsm_duration_ns"]
        sessions[str(session["session_id"])] = dict(
            session_start_ns=session["session_start_ns"],
            resource_ready_ns=ready,
            eligible_ns=eligible,
            resource_wait_ns=eligible - session["session_start_ns"],
            bsm_arrival_ns=eligible,
            bsm_start_ns=start,
            bsm_completion_ns=r_free,
            bsm_wait_ns=start - eligible,
        )
        controls.append(
            dict(
                packet_id=slot + 1,
                session_id=session["session_id"],
                app_tx_ns=r_free,
                payload_bytes=(
                    2 if session["protocol"] == "teleport" else config["result_payload_bytes"]
                ),
            )
        )
    if model == "Fixed-Dc":
        packets = [
            dict(p, app_rx_ns=p["app_tx_ns"] + delays["result_ns"], queue_wait_ns=None)
            for p in controls
        ]
    else:
        packets = result_fifo(config, controls)
    b_free = 0
    for packet in packets:
        if not packet["session_id"]:
            continue
        start = max(b_free, packet["app_rx_ns"])
        b_free = start + config["correction_duration_ns"]
        sessions[str(packet["session_id"])].update(
            correction_arrival_ns=packet["app_rx_ns"],
            correction_start_ns=start,
            correction_completion_ns=b_free,
            correction_wait_ns=start - packet["app_rx_ns"],
        )
    drain = max(p["app_rx_ns"] for p in packets)
    return dict(
        sessions=sessions,
        packets=packets,
        batch_completion_ns=b_free,
        traffic_drain_completion_ns=drain,
        simulation_completion_ns=max(b_free, drain),
    )


def decoupled(config, classical):
    """Retain readiness; add eligible R FIFO wait to No-Dq-R classical delay.

    Result packet phases are not regenerated after the wait is composed.
    No quantum state or service-feasibility prediction is defined.
    """
    measured = {row["session_id"]: row for row in classical["metrics"]["sessions"]}
    rows, free = [], 0
    for _, session in eligible_order(config):
        at = session["session_start_ns"]
        ready = readiness(config, session)
        eligible = max(at, ready)
        start = max(eligible, free)
        free = start + config["bsm_duration_ns"]
        delay = measured[session["session_id"]]["result_delay_ns"]
        latency = free - at + delay + config["correction_duration_ns"]
        rows.append(
            dict(
                session_id=session["session_id"],
                transaction_latency_ns=latency,
                completion_ns=at + latency,
                session_start_ns=at,
                resource_ready_ns=ready,
                resource_wait_ns=eligible - at,
                quantum_wait_ns=start - eligible,
                result_delay_ns=delay,
                usable_fidelity=None,
                source="C=provisioned No-Dq-R result delay; Q=resource-eligible R FIFO",
            )
        )
    return rows
