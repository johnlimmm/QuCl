"""Final teleported-state metrics and independent complete-workflow timing replay."""

from collections import deque
import heapq
from analysis import rounded_mean

PATHS = ("swap_packet_ns", "ready_packet_ns", "teleport_packet_ns")


def frozen_delays(classical):
    """Keep each No-R workflow's paths; never replace them with cell means."""
    return {
        str(m["chain_id"]): {k: m["delays"][k] for k in PATHS}
        for m in classical["metrics"]["chains"]
    }


def validate_decoupled(report, classical):
    """Native replay must reproduce the independent frozen-path schedule exactly."""
    if report.get("model") != "Decoupled" or report["frozen_delays"] != frozen_delays(
        classical
    ):
        raise ValueError("Decoupled path provenance mismatch")
    prediction = decoupled(report["config"], classical)
    actual = {(r["session_id"], r["operation"]): r for r in report["snapshot"]["requests"]}
    for chain, row in zip(report["config"]["chains"], prediction):
        for (sid, op), expected in zip(
            (
                (chain["swap_session_id"], "BSM"),
                (chain["swap_session_id"], "CORRECTION"),
                (chain["teleport_session_id"], "BSM"),
                (chain["teleport_session_id"], "CORRECTION"),
            ),
            row["requests"],
        ):
            if any(
                actual[sid, op][k] != expected[k]
                for k in ("arrival_ns", "start_ns", "completion_ns")
            ):
                raise ValueError("Decoupled native/timing replay mismatch")
    return dict(passed=True, requests=4 * len(prediction), max_timing_error_ns=0)


def model_rows(report, fmin, deadline):
    rows = []
    for m in report["metrics"]["chains"]:
        ref = report["cross_validation"]["chains"][str(m["chain_id"])]["reference"]
        values = [
            (b["probability"], b["stages"][1]["checkpoints"]["usable"]["fidelity"])
            for b in ref["branches"].values()
        ]
        if len(values) != 16 or abs(sum(p for p, _ in values) - 1) > 1e-10:
            raise ValueError("16 normalized joint branches required")
        deadline_ok = m["latency_ns"] <= deadline
        rows.append(
            dict(
                m,
                session_id=m["chain_id"],
                transaction_latency_ns=m["latency_ns"],
                usable_fidelity=m["output_fidelity"],
                expected_fidelity=sum(p * f for p, f in values),
                deadline_ok=deadline_ok,
                feasible=deadline_ok and m["output_fidelity"] >= fmin,
                success_probability=(
                    sum(p for p, f in values if f >= fmin) if deadline_ok else 0.0
                ),
            )
        )
    return rows


def calibration_entry(load, reports):
    raw = [{k: m["delays"][k] for k in PATHS} for r in reports for m in r["metrics"]["chains"]]
    return dict(
        classical_load=load,
        delays={k: rounded_mean([r[k] for r in raw]) for k in PATHS},
        observations=raw,
        runs=len(reports),
        method="separate Full Sync chain runs; per-path mean rounded to nearest ns",
    )


def decoupled(config, classical):
    """Replay shared R/A/B scheduling with per-workflow No-R path delays frozen.

    Complete the entire dependency graph. Never regenerate a packet after a
    shifted operation. This is a timing-only prediction; no state is inferred
    from a scalar latency or from another model's quantum state.
    """
    observed = {m["chain_id"]: m["delays"] for m in classical["metrics"]["chains"]}
    chains = {c["chain_id"]: c for c in config["chains"]}
    ready = max(q["delay_ns"] for q in config["quantum_links"].values())
    pending, sequence = [], 0

    def schedule(at, kind, cid, stage):
        nonlocal sequence
        heapq.heappush(pending, (at, sequence, kind, cid, stage))
        sequence += 1

    # Stages: R BSM, first B correction, A BSM, final B correction.
    nodes = ("R", "B", "A", "B")
    durations = (
        config["bsm_duration_ns"],
        config["correction_duration_ns"],
        config["bsm_duration_ns"],
        config["correction_duration_ns"],
    )
    queues = {n: deque() for n in ("R", "A", "B")}
    running = {n: None for n in queues}
    requests = {}
    for chain in config["chains"]:
        schedule(max(ready, chain["session_start_ns"]), "arrival", chain["chain_id"], 0)
    while pending:
        at = pending[0][0]
        while pending and pending[0][0] == at:
            _, _, kind, cid, stage = heapq.heappop(pending)
            node = nodes[stage]
            if kind == "arrival":
                queues[node].append((cid, stage))
                requests[cid, stage] = dict(arrival_ns=at)
            else:
                if running[node] != (cid, stage):
                    raise ValueError("unexpected timing replay completion")
                running[node] = None
                requests[cid, stage]["completion_ns"] = at
                if stage < 3:
                    schedule(at + observed[cid][PATHS[stage]], "arrival", cid, stage + 1)
        for node in ("R", "A", "B"):
            if running[node] is None and queues[node]:
                cid, stage = queues[node].popleft()
                running[node] = (cid, stage)
                requests[cid, stage]["start_ns"] = at
                schedule(at + durations[stage], "complete", cid, stage)
    result = []
    for chain in config["chains"]:
        cid = chain["chain_id"]
        own = [requests[cid, i] for i in range(4)]
        completion = own[-1]["completion_ns"]
        result.append(
            dict(
                chain_id=cid,
                session_id=cid,
                session_start_ns=chain["session_start_ns"],
                transaction_latency_ns=completion - chain["session_start_ns"],
                completion_ns=completion,
                usable_fidelity=None,
                expected_fidelity=None,
                frozen_path_delays=observed[cid],
                requests=own,
                source="shared R/A/B FIFO replay; per-chain No-R classical delays held fixed",
            )
        )
    return result
