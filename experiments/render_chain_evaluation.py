#!/usr/bin/env python3
"""Audit and plot complete-chain latency, fidelity and paired model errors."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MaxNLocator, ScalarFormatter
import numpy as np

from render_evaluation import (
    read,
    digest,
    require,
    write_csv,
    style,
    legend,
    bars,
    fidelity_plot,
    save_figure,
    COLORS,
    EDGES,
    HATCHES,
    MODELS,
    LABELS,
    interval_text,
)
from analysis import chain_cells, compare, summarize, DELAY_FIELDS
from chain_analysis import model_rows, decoupled, calibration_entry, validate_decoupled


def check_csv(path, expected):
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == len(expected), "CSV row count: " + path.name)
    for actual, truth in zip(rows, expected):
        require(set(actual) == set(truth), "CSV columns")
        for key, value in truth.items():
            if value is None:
                require(actual[key] == "", "undefined CSV field")
            elif isinstance(value, str):
                require(actual[key] == value, "CSV identity: " + key)
            else:
                require(abs(float(actual[key]) - value) < 1e-12, "CSV metric: " + key)


def audit(source):
    s = read(source / "summary.json")
    require(
        s["passed"] and s["comparison_scope"] == "complete-swap-teleport-chain",
        "complete-chain campaign required",
    )
    plan = s["plan"]
    for name, sha in s["data_sha256"].items():
        require(digest(source / name) == sha, "modified data: " + name)
    for name, sha in s["report_sha256"].items():
        require(digest(source / name) == sha, "modified report: " + name)
    if "extension" in s:
        parent = read(source / "parent-summary.json")
        require(
            digest(source / "parent-summary.json") == s["extension"]["parent_summary_sha256"],
            "parent summary identity",
        )
        for name, sha in parent["report_sha256"].items():
            require(s["report_sha256"].get(name) == sha, "retained report identity: " + name)
    full_rows, errors, comparison_rows, execution_changes = [], [], [], []
    signatures = {}
    controls = dict(
        workflows=0,
        max_native_state_error=0.0,
        decoupled_workflows=0,
        max_decoupled_state_error=0.0,
        decoupled_reports=0,
        decoupled_timing_requests=0,
        decoupled_reference_cache_hits=0,
        decoupled_max_reference_error=0.0,
    )
    # Use the runner's order, not lexical file ordering, to verify exported CSVs.
    for load in plan["classical_loads"]:
        for interval in plan["request_intervals_ns"]:
            for phase in plan["test_traffic_seeds"] if load else plan["test_traffic_seeds"][:1]:
                metadata = dict(
                    classical_load=load,
                    request_interval_ns=interval,
                    traffic_seed=phase,
                    quantum_seed=plan["quantum_seed"],
                )
                folder = (
                    source
                    / "chains"
                    / ("load-{}-interval-{}-phase-{}".format(load, interval, phase))
                )
                for index, state in enumerate(plan["input_states"]):
                    root = folder / ("input-" + str(index))
                    case = read(root / "comparison.json")
                    reports, predictions = {}, {}
                    for model in s.get("state_models", ("Full-Sync", "No-Dq-R", "Fixed-Dc")):
                        report = read(root / (model + ".json.gz"))
                        require(
                            report["validation"]["passed"]
                            and report["cross_validation"]["passed"],
                            "invalid chain model",
                        )
                        require(
                            all(c["input_state"] == state for c in report["config"]["chains"]),
                            "input state",
                        )
                        predictions[model] = model_rows(
                            report, plan["fidelity_min"], plan["deadline_ns"]
                        )
                        signature = [
                            (m["chain_id"], m["latency_ns"], sorted(m["delays"].items()))
                            for m in report["metrics"]["chains"]
                        ]
                        key = (load, interval, phase, model)
                        require(
                            key not in signatures or signatures[key] == signature,
                            "input-dependent timing",
                        )
                        signatures[key] = signature
                        reports[model] = report
                    if all(
                        m["delays"]["swap_R_wait_ns"] == 0
                        for m in reports["Full-Sync"]["metrics"]["chains"]
                    ):
                        for chain in reports["Full-Sync"]["config"]["chains"]:
                            sid = str(chain["teleport_session_id"])
                            full = reports["Full-Sync"]["snapshot"]["sessions"][sid]
                            no_r = reports["No-Dq-R"]["snapshot"]["sessions"][sid]
                            require(
                                full["measurement_bits"] == no_r["measurement_bits"],
                                "no-R-wait control outcome",
                            )
                            a = full["checkpoints"]["usable"]["density_matrix"]
                            b = no_r["checkpoints"]["usable"]["density_matrix"]
                            require(a["qubit_order"] == b["qubit_order"], "control state order")
                            error = max(
                                abs(x - y)
                                for part in ("real", "imag")
                                for ra, rb in zip(a[part], b[part])
                                for x, y in zip(ra, rb)
                            )
                            require(error < 1e-12, "no-R-wait final native state control")
                            controls["workflows"] += 1
                            controls["max_native_state_error"] = max(
                                controls["max_native_state_error"], error
                            )
                    if "Decoupled" in reports:
                        timing = validate_decoupled(reports["Decoupled"], reports["No-Dq-R"])
                        controls["decoupled_reports"] += 1
                        controls["decoupled_timing_requests"] += timing["requests"]
                        controls["decoupled_reference_cache_hits"] += (
                            reports["Decoupled"]
                            .get("reference_cache", {})
                            .get("exact_specification_hits", 0)
                        )
                        controls["decoupled_max_reference_error"] = max(
                            controls["decoupled_max_reference_error"],
                            reports["Decoupled"]["cross_validation"][
                                "max_density_matrix_error"
                            ],
                        )
                        if not load or all(
                            m["delays"]["swap_R_wait_ns"] == 0
                            for m in reports["Full-Sync"]["metrics"]["chains"]
                        ):
                            for chain in reports["Full-Sync"]["config"]["chains"]:
                                sid = str(chain["teleport_session_id"])
                                full = reports["Full-Sync"]["snapshot"]["sessions"][sid]
                                replay = reports["Decoupled"]["snapshot"]["sessions"][sid]
                                require(
                                    full["measurement_bits"] == replay["measurement_bits"],
                                    "Decoupled control outcome",
                                )
                                a = full["checkpoints"]["usable"]["density_matrix"]
                                b = replay["checkpoints"]["usable"]["density_matrix"]
                                require(
                                    a["qubit_order"] == b["qubit_order"],
                                    "Decoupled control order",
                                )
                                error = max(
                                    abs(x - y)
                                    for part in ("real", "imag")
                                    for ra, rb in zip(a[part], b[part])
                                    for x, y in zip(ra, rb)
                                )
                                require(error < 1e-12, "Decoupled native state control")
                                controls["decoupled_workflows"] += 1
                                controls["max_decoupled_state_error"] = max(
                                    controls["max_decoupled_state_error"], error
                                )
                    else:
                        predictions["Decoupled"] = decoupled(
                            reports["Full-Sync"]["config"], reports["No-Dq-R"]
                        )
                    require(
                        predictions == case["models"],
                        "comparison does not match native reports",
                    )
                    for model in MODELS:
                        errors.extend(
                            compare(
                                predictions["Full-Sync"],
                                predictions[model],
                                dict(metadata, input_state=state, model=model),
                                plan["deadline_ns"],
                            )
                        )
                    for m in predictions["Full-Sync"]:
                        full_rows.append(
                            dict(
                                metadata,
                                input_state=state,
                                chain_id=m["chain_id"],
                                latency_ns=m["latency_ns"],
                                observed_fidelity=m["output_fidelity"],
                                expected_fidelity=m["expected_fidelity"],
                                **m["delays"],
                            )
                        )
                    full_by_id = {m["session_id"]: m for m in predictions["Full-Sync"]}
                    for model in (m for m in MODELS if m in reports):
                        for m in predictions[model]:
                            base = full_by_id[m["session_id"]]
                            changes = {
                                k: m["delays"][k] - base["delays"][k] for k in DELAY_FIELDS
                            }
                            delta = m["latency_ns"] - base["latency_ns"]
                            require(
                                sum(changes.values()) == delta, "paired latency decomposition"
                            )
                            execution_changes.append(
                                dict(
                                    metadata,
                                    input_state=state,
                                    model=model,
                                    chain_id=m["session_id"],
                                    delta_latency_ns=delta,
                                    **{"delta_" + k: v for k, v in changes.items()},
                                )
                            )
                    for model, rows in predictions.items():
                        for m in rows:
                            comparison_rows.append(
                                dict(
                                    metadata,
                                    input_state=state,
                                    model=model,
                                    chain_id=m["session_id"],
                                    latency_ns=m["transaction_latency_ns"],
                                    expected_fidelity=m.get("expected_fidelity"),
                                )
                            )
    check_csv(source / "chains.csv", full_rows)
    check_csv(source / "errors.csv", errors)
    require(len(full_rows) == s["chain_transactions"], "complete workflow count")
    require(
        summarize(errors, plan["bootstrap_samples"]) == s["ablation_groups"],
        "paired error aggregates",
    )
    calibration = read(source / "calibration.json")
    for load in plan["classical_loads"]:
        reports = [
            read(source / "calibration" / ("load-{}-{}.json.gz".format(load, seed)))
            for seed in (
                plan["calibration_traffic_seeds"]
                if load
                else plan["calibration_traffic_seeds"][:1]
            )
        ]
        require(
            calibration_entry(load, reports) == calibration["by_load"][str(load)],
            "three-path calibration",
        )
    return s, chain_cells(full_rows, plan), comparison_rows, execution_changes, controls


def model_plot(summary, metric, ylabel, name, output, book):
    """Show the 0.5× start interval; retain all interval results in the tables."""
    p = summary["plan"]
    intervals = [
        interval
        for interval, factor in zip(p["request_intervals_ns"], p["interval_factors"])
        if factor == 0.5
    ]
    require(len(intervals) == 1, "Fig. 5 requires one 0.5× workflow-start interval")
    interval = intervals[0]
    loads = p["classical_loads"]
    models = (
        MODELS
        if metric == "latency_mae_ns" or "Decoupled" in summary.get("state_models", [])
        else MODELS[:2]
    )
    scale = 1000 if metric == "latency_mae_ns" else 1
    fig, ax = plt.subplots(figsize=(4.7, 3.0))
    fig.subplots_adjust(left=0.19, right=0.97, bottom=0.215, top=0.81)
    upper = max(
        (g[metric]["ci95_cluster_bootstrap"] or [g[metric]["mean"]] * 2)[1] / scale
        for g in summary["ablation_groups"]
        if g["model"] in models and g["request_interval_ns"] == interval
    )
    positions = np.arange(len(loads)) * 1.15
    width = 0.245
    ax.grid(axis="y", color="#E7EAEE", linewidth=0.65)
    ax.tick_params(axis="x", length=0, pad=5)
    for index, model in enumerate(models):
        groups = [
            next(
                g
                for g in summary["ablation_groups"]
                if g["classical_load"] == load
                and g["request_interval_ns"] == interval
                and g["model"] == model
            )
            for load in loads
        ]
        estimates = [g[metric] for g in groups]
        y = [e["mean"] / scale for e in estimates]
        x = positions + (index - (len(models) - 1) / 2) * width
        ax.bar(
            x,
            y,
            width * 0.9,
            label=LABELS[MODELS.index(model)],
            color=COLORS[index],
            edgecolor=EDGES[index],
            linewidth=0.8,
            hatch=HATCHES[index],
            zorder=3,
        )
        for at, value, e in zip(x, y, estimates):
            ci = e["ci95_cluster_bootstrap"]
            if ci is not None:
                ax.errorbar(
                    at,
                    value,
                    yerr=[[max(0, value - ci[0] / scale)], [max(0, ci[1] / scale - value)]],
                    fmt="none",
                    ecolor="#414B55",
                    capsize=2.5,
                    elinewidth=0.8,
                    zorder=5,
                )
            if abs(value) < 1e-12:
                ax.plot(
                    [at - width * 0.45, at + width * 0.45],
                    [0, 0],
                    color=EDGES[index],
                    lw=1.6,
                    clip_on=False,
                    zorder=6,
                )
    ax.set_ylim(0, upper * 1.15 if upper else 1)
    ax.set_xticks(positions, [f"{load:.2f}" for load in loads])
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
    if metric != "latency_mae_ns":
        ax.yaxis.set_major_locator(MaxNLocator(nbins=3, steps=[1, 2, 5, 10]))
        ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
    ax.set_xlabel("Background load", labelpad=7)
    ax.set_ylabel(ylabel, labelpad=8)
    legend(fig, ax)
    save_figure(fig, output, name, book)


def render(source, output):
    require(not output.exists(), "choose a new output directory")
    summary, cells, predictions, execution_changes, controls = audit(source)
    output.mkdir(parents=True)
    (output / "figures").mkdir()
    style()
    p = summary["plan"]
    with PdfPages(output / "FIGURES.pdf") as book:
        series = []
        for interval, ratio in zip(p["request_intervals_ns"], p["interval_factors"]):
            values = []
            for load in p["classical_loads"]:
                c = next(c for c in cells if c["load"] == load and c["interval_ns"] == interval)
                values.append(
                    tuple(
                        None if c[k] is None else c[k] / 1000
                        for k in (
                            "extra_latency_ns",
                            "extra_latency_ns_low",
                            "extra_latency_ns_high",
                        )
                    )
                )
            series.append((f"{ratio:g}× interval", values))
        bars(
            series,
            "Additional latency (μs)",
            p["classical_loads"],
            output,
            "fig3_chain_latency",
            book,
        )
        fidelity_plot(summary, cells, output, book)
        model_plot(summary, "latency_mae_ns", "Latency MAE (μs)", "fig5_a", output, book)
        model_plot(summary, "expected_fidelity_mae", "Fidelity MAE", "fig5_b", output, book)
    write_csv(output / "chain_cells.csv", cells)
    write_csv(output / "model_predictions.csv", predictions)
    write_csv(output / "execution_changes.csv", execution_changes)
    errors = []
    lines = [
        "# Complete Swap--Teleport evaluation",
        "",
        "## Execution and validation",
        "",
        f"{summary['paired_cases']} traffic conditions; {summary['evaluation_runs']:,} complete-chain simulation runs and {summary['calibration_runs']} independent calibration runs.",
        f"Each model evaluates {summary['chain_transactions']:,} workflows across six inputs. Maximum native/reference density-matrix error: {summary['max_density_matrix_error']:.3g}.",
        f"Separate validation: {summary['validation']['noiseless_runs']} noiseless batches / {summary['validation']['noiseless_transactions']} workflows, all 16 joint outcomes for each input, and {summary['validation']['noisy_runs']} noisy runs.",
        (
            "This artifact extends the completed three-model campaign: its existing executions are hash-verified and retained, and the missing Decoupled native executions are added."
            if "extension" in summary
            else "Completed executions from the same campaign may be retained after an interruption; no reports from an earlier campaign are used."
        ),
        "",
        "## Figures and interpretation",
        "",
        "All curves and model errors cover the complete workflow and its final Bob output.",
        "Fig. 3: Full Sync latency. Fig. 4: Full Sync final teleportation fidelity.",
        "Fig. 5(a): complete-workflow latency MAE. Fig. 5(b): final-output fidelity MAE.",
        "Fig. 5 shows only the 0.5× workflow-start interval, where R waiting occurs. The 1× and 2× controls remain in the result tables and CSVs.",
        "The multiplier refers to workflow-start spacing relative to the 27.405-us R BSM service time; every BSM executes fully.",
        "This controls R waiting, its boundary and a no-R-wait condition; A/B and packet queues remain enabled.",
        (
            "Decoupled holds each workflow's No-R path delays fixed and executes the complete native quantum circuit; all three simplifications have final-state errors in Fig. 5(b)."
            if "Decoupled" in summary.get("state_models", [])
            else "This historical campaign has timing-only Decoupled predictions; that model is omitted from Fig. 5(b)."
        ),
        "Errors pair the same input, workflow and traffic phase. MAE takes absolute errors before averaging six inputs and four workflows inside each phase.",
        "Whiskers are 95% traffic-phase bootstrap intervals, conditional on independent three-path calibration. Zero load is deterministic.",
        "",
        "## Full Sync",
        "",
        "| Start interval | Background load | Absolute latency (us) | Final teleportation fidelity |",
        "|---:|---:|---:|---:|",
    ]
    for c in cells:
        ratio = p["interval_factors"][p["request_intervals_ns"].index(c["interval_ns"])]
        lines.append(
            f"| {ratio:g}× | {c['load']:.2f} | {c['latency_ns'] / 1000:.3f} | {c['expected_fidelity']:.6f} |"
        )
    lines += [
        "",
        "## Complete-workflow prediction errors",
        "",
        "| Interval | Load | Model | Latency MAE (us) [CI] | Fidelity MAE [CI] | Fidelity bias [CI] |",
        "|---:|---:|---|---:|---:|---:|",
    ]
    for g in summary["ablation_groups"]:
        row = {k: g[k] for k in ("request_interval_ns", "classical_load", "model")}
        for field in ("latency_mae_ns", "expected_fidelity_mae", "expected_fidelity_bias"):
            e = g[field]
            row[field] = e["mean"] if e else None
            row[field + "_low"], row[field + "_high"] = (
                e["ci95_cluster_bootstrap"] if e else None
            ) or [None, None]
        errors.append(row)
        ratio = p["interval_factors"][p["request_intervals_ns"].index(g["request_interval_ns"])]
        lines.append(
            "| {:g}× | {} | {} | {} | {} | {} |".format(
                ratio,
                g["classical_load"],
                LABELS[MODELS.index(g["model"])],
                interval_text(g["latency_mae_ns"], scale=1000),
                (
                    interval_text(g["expected_fidelity_mae"], digits=5)
                    if g["expected_fidelity_mae"]
                    else "not defined"
                ),
                (
                    interval_text(g["expected_fidelity_bias"], digits=5)
                    if g["expected_fidelity_bias"]
                    else "not defined"
                ),
            )
        )
    write_csv(output / "model_error_cells.csv", errors)
    write_csv(output / "six_state_noise.csv", summary["validation"]["noisy_cases"])
    zero = next(
        g
        for g in summary["ablation_groups"]
        if g["model"] == "No-Dq-R"
        and g["classical_load"] == 0
        and g["request_interval_ns"] == p["request_intervals_ns"][0]
    )
    shifts = [
        r
        for r in execution_changes
        if r["model"] == "No-Dq-R"
        and r["classical_load"] == 0
        and r["request_interval_ns"] == p["request_intervals_ns"][0]
    ]
    shift = lambda k: sum(r[k] for r in shifts) / len(shifts) / 1000
    lines += [
        "",
        "## Waiting can move between processors",
        "",
        f"At zero background load and the shortest start interval, removing R contention changes mean R waiting by {shift('delta_swap_R_wait_ns'):+.3f} us and mean A waiting by {shift('delta_teleport_A_wait_ns'):+.3f} us.",
        f"The complete-workflow latency MAE is {zero['latency_mae_ns']['mean'] / 1000:.3f} us, while final fidelity bias is {zero['expected_fidelity_bias']['mean']:+.6f} and fidelity MAE is {zero['expected_fidelity_mae']['mean']:.6f}.",
        "Final completion time alone therefore need not capture the change in intermediate measurement times and quantum storage history.",
        "The paired timing changes, including all three classical paths and both B corrections, are retained in execution_changes.csv.",
        "",
        "## Scope",
        "",
        "These are finite four-workflow batches with periodic background traffic and fixed calibration. Start intervals control R contention relative to a known service time; they are not measured application rates.",
        "Fixed-delay calibration uses a sparser workload. Its errors reflect both possible mean-delay differences between calibration and test batches and the variation removed by constant transport delays.",
        "All resources originate at t=0. Different start intervals also change pre-request storage age. Compare loads within an interval, or pair models at the same starts, to preserve that control.",
        "The state metrics use native NetSquid fidelity in each measurement branch. Exact branch expectations are distinct from a sampled trajectory's output fidelity.",
    ]
    if "extension" in summary:
        lines += [
            "",
            "## Campaign extension",
            "",
            "The hash-verified Full Sync, No R contention and Fixed classical delay reports are retained unchanged from the completed parent campaign. The missing Decoupled native state executions are added separately; no previous model's quantum state is substituted.",
            f"Retained evaluation runs: {summary['extension']['retained_evaluation_runs']:,}; added Decoupled runs: {summary['evaluation_runs'] - summary['extension']['retained_evaluation_runs']:,}. Calibration and the original execution validation are retained.",
            "Decoupled's native operation times are checked against its independent FIFO timing replay. Its instruction and protocol states are cross-validated with an independent NetSquid circuit, including all 16 joint branches.",
        ]
    if (source / "regression.json").exists():
        regression = read(source / "regression.json")
        require(regression["passed"], "regression suite")
        lines += [
            "",
            "## Regression",
            "",
            f"{regression['python_tests']} Python tests and {regression['native_test_cases']} Q2NS native test cases passed.",
        ]
    (output / "RESULTS.md").write_text("\n".join(lines) + "\n")
    if "Decoupled" in summary.get("state_models", []):
        (output / "decoupled-validation.json").write_text(
            json.dumps(
                dict(
                    passed=True,
                    native_runs=controls["decoupled_reports"],
                    native_workflows=summary["plan"]["sessions"]
                    * controls["decoupled_reports"],
                    timing_requests=controls["decoupled_timing_requests"],
                    max_timing_error_ns=0,
                    max_density_matrix_error=controls["decoupled_max_reference_error"],
                    full_sync_state_controls=controls["decoupled_workflows"],
                    max_control_state_error=controls["max_decoupled_state_error"],
                    exact_specification_reference_hits=controls[
                        "decoupled_reference_cache_hits"
                    ],
                    quantum_execution="Fresh native execution for every Decoupled run; all instruction/protocol checkpoints compared.",
                ),
                indent=2,
            )
            + "\n"
        )
    provenance = dict(
        passed=True,
        scope=summary["comparison_scope"],
        source=str(source.resolve()),
        summary_sha256=digest(source / "summary.json"),
        raw_reports_checked=len(summary["report_sha256"]),
        fig5_interval_factor=0.5,
        no_r_wait_controls=controls,
        supplementary_sha256={
            name: digest(source / name)
            for name in (
                "regression.json",
                "execution-history.json",
                "full-sync-equivalence.json",
                "reference-cache-equivalence.json",
                "extension.json",
                "parent-summary.json",
            )
            if (source / name).exists()
        },
        source_sha256={
            str(Path(__file__).parent / n): digest(Path(__file__).parent / n)
            for n in (
                "render_chain_evaluation.py",
                "render_evaluation.py",
                "analysis.py",
                "chain_analysis.py",
            )
        },
        python=sys.version,
        matplotlib=matplotlib.__version__,
        numpy=np.__version__,
        files={str(p.relative_to(output)): digest(p) for p in output.rglob("*") if p.is_file()},
    )
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(
        "PASS: audited complete-chain reports and rendered Figures 3, 4, 5(a), 5(b) in "
        + str(output)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    render(args.run, args.output_dir)
