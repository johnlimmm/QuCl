"""Paired model errors and traffic-phase cluster bootstrap intervals."""

import random
from collections import defaultdict

DELAY_FIELDS = [
    "resource_wait_ns",
    "swap_R_wait_ns",
    "swap_bsm_ns",
    "swap_packet_ns",
    "swap_B_wait_ns",
    "swap_correction_ns",
    "ready_packet_ns",
    "teleport_A_wait_ns",
    "teleport_bsm_ns",
    "teleport_packet_ns",
    "teleport_B_wait_ns",
    "teleport_correction_ns",
]


def chain_cells(rows, plan):
    """Balanced six-input means; traffic phase is the unit of resampling."""
    cells = []
    expected_keys = set()
    actual = {}
    for row in rows:
        key = (
            row["classical_load"],
            row["request_interval_ns"],
            row["traffic_seed"],
            row["chain_id"],
            row["input_state"],
        )
        if key in actual:
            raise ValueError("duplicate chain observation")
        actual[key] = row
        if sum(row[k] for k in DELAY_FIELDS) != row["latency_ns"]:
            raise ValueError("incomplete latency decomposition")
    for load in plan["classical_loads"]:
        phases = plan["test_traffic_seeds"] if load else plan["test_traffic_seeds"][:1]
        for interval in plan["request_intervals_ns"]:
            own = []
            for phase in phases:
                for sid in range(1, plan["sessions"] + 1):
                    group = []
                    for state in plan["input_states"]:
                        key = (load, interval, phase, sid, state)
                        expected_keys.add(key)
                        if key not in actual:
                            raise ValueError("missing six-input observation")
                        group.append(actual[key])
                    if len({r["latency_ns"] for r in group}) != 1:
                        raise ValueError("input-dependent timing")
                    own.extend(group)
            result = dict(
                load=load, interval_ns=interval, transactions=len(own), traffic_phases=len(phases)
            )
            for field in ["latency_ns", "expected_fidelity"] + DELAY_FIELDS:
                e = estimate(own, field, plan["bootstrap_samples"])
                lo, hi = e["ci95_cluster_bootstrap"] or [None, None]
                result.update({field: e["mean"], field + "_low": lo, field + "_high": hi})
            cells.append(result)
    if set(actual) != expected_keys:
        raise ValueError("unexpected chain observations")
    baseline = next(
        c["latency_ns"]
        for c in cells
        if c["load"] == 0 and c["interval_ns"] == plan["request_intervals_ns"][-1]
    )
    for c in cells:
        c["baseline_latency_ns"] = baseline
        for suffix in ("", "_low", "_high"):
            value = c["latency_ns" + suffix]
            c["extra_latency_ns" + suffix] = None if value is None else value - baseline
    return cells


def model_rows(report, fmin, deadline):
    rows = []
    for metric in report["metrics"]["sessions"]:
        sid = str(metric["session_id"])
        row = dict(metric)
        ref = report["cross_validation"]["sessions"][sid]["reference"]
        row["expected_fidelity"] = sum(
            b["probability"] * b["checkpoints"]["usable"]["fidelity"]
            for b in ref["branches"].values()
            if b["probability"] > 0
        )
        row["deadline_ok"] = row["transaction_latency_ns"] <= deadline
        row["feasible"] = row["deadline_ok"] and row["usable_fidelity"] >= fmin
        row["success_probability"] = (
            sum(
                b["probability"]
                for b in ref["branches"].values()
                if b["probability"] > 0 and b["checkpoints"]["usable"]["fidelity"] >= fmin
            )
            if row["deadline_ok"]
            else 0.0
        )
        row["measurement_bits"] = report["snapshot"]["sessions"][sid]["measurement_bits"]
        rows.append(row)
    return rows


def compare(full, predicted, metadata, deadline):
    truth = {r["session_id"]: r for r in full}
    result = []
    for row in predicted:
        base = truth[row["session_id"]]
        actual = row["usable_fidelity"] is not None
        deadline_ok = row["transaction_latency_ns"] <= deadline
        result.append(
            dict(
                metadata,
                session_id=row["session_id"],
                delta_latency_ns=row["transaction_latency_ns"] - base["transaction_latency_ns"],
                delta_fidelity=row["usable_fidelity"] - base["usable_fidelity"] if actual else None,
                delta_expected_fidelity=(
                    row["expected_fidelity"] - base["expected_fidelity"] if actual else None
                ),
                delta_success_probability=(
                    row["success_probability"] - base["success_probability"] if actual else None
                ),
                false_feasible=int(row["feasible"] and not base["feasible"]) if actual else None,
                false_infeasible=int(not row["feasible"] and base["feasible"]) if actual else None,
                false_deadline_feasible=int(deadline_ok and not base["deadline_ok"]),
                false_deadline_infeasible=int(not deadline_ok and base["deadline_ok"]),
            )
        )
    return result


def estimate(rows, field, bootstrap_samples, absolute=False):
    # Average sessions and input states within each traffic phase first.
    # Only independent phase means are resampled.
    clusters = defaultdict(list)
    for row in rows:
        if row[field] is not None:
            clusters[row["traffic_seed"]].append(abs(row[field]) if absolute else row[field])
    if not clusters:
        return None
    means = [sum(v) / len(v) for _, v in sorted(clusters.items())]
    mean = sum(means) / len(means)
    interval = None
    if len(means) >= 2:
        rng = random.Random(7301)
        samples = sorted(
            sum(rng.choice(means) for _ in means) / len(means) for _ in range(bootstrap_samples)
        )
        interval = [
            samples[int(0.025 * (len(samples) - 1))],
            samples[int(0.975 * (len(samples) - 1))],
        ]
    return dict(mean=mean, traffic_clusters=len(means), ci95_cluster_bootstrap=interval)


def summarize(errors, samples):
    grouped = defaultdict(list)
    for row in errors:
        grouped[(row["classical_load"], row["request_interval_ns"], row["model"])].append(row)
    output = []
    for (load, spacing, model), rows in sorted(grouped.items()):
        output.append(
            dict(
                classical_load=load,
                request_interval_ns=spacing,
                model=model,
                transactions=len(rows),
                latency_bias_ns=estimate(rows, "delta_latency_ns", samples),
                latency_mae_ns=estimate(rows, "delta_latency_ns", samples, True),
                fidelity_bias=estimate(rows, "delta_fidelity", samples),
                expected_fidelity_bias=estimate(rows, "delta_expected_fidelity", samples),
                expected_fidelity_mae=estimate(rows, "delta_expected_fidelity", samples, True),
                success_probability_mae=estimate(rows, "delta_success_probability", samples, True),
                false_feasible_rate=estimate(rows, "false_feasible", samples),
                false_infeasible_rate=estimate(rows, "false_infeasible", samples),
                false_deadline_feasible_rate=estimate(rows, "false_deadline_feasible", samples),
                false_deadline_infeasible_rate=estimate(rows, "false_deadline_infeasible", samples),
            )
        )
    return output


def rounded_mean(values):
    # Round positive ns means to the nearest integer, with ties rounded up.
    return (2 * sum(values) + len(values)) // (2 * len(values))


def calibration_entry(load, records):
    delays = {
        side
        + "_ns": rounded_mean(
            [m[side + "_delay_ns"] for r in records for m in r["metrics"]["sessions"]]
        )
        for side in ("result",)
    }
    return dict(
        classical_load=load,
        delays=delays,
        rounding="nearest integer ns; half up",
        sample_count=sum(len(r["metrics"]["sessions"]) for r in records),
        measured_delays={
            side: [m[side + "_delay_ns"] for r in records for m in r["metrics"]["sessions"]]
            for side in ("result",)
        },
    )
