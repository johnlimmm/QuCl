#!/usr/bin/env python3
"""Reevaluate Full Sync and simplifications over complete Swap--Teleport workflows."""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor
import hashlib
import itertools
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time

from run_evaluation import (
    MODULE,
    workload,
    validation,
    validate_plan,
    environment,
    hashes as base_hashes,
    read_report,
    csv_file,
    timing_signature,
)
from chain_models import run_chain_model, REFERENCES, seed_reference_cache
from chain_analysis import model_rows, calibration_entry, frozen_delays, validate_decoupled
from analysis import compare, summarize
from runtime import save_report
from reference_client import close_workers

MODELS = ("Full-Sync", "No-Dq-R", "Fixed-Dc", "Decoupled")


def hashes():
    values = base_hashes()
    for name in ("chain_models.py", "chain_analysis.py", "run_chain_evaluation.py"):
        p = MODULE / "experiments" / name
        values[str(p.relative_to(MODULE))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return values


def extend_campaign(source, out, plan, sources, env):
    """Preserve verified three-model reports and add only the missing native model."""
    base = read_report(source / "summary.json")
    if (
        not base["passed"]
        or base["comparison_scope"] != "complete-swap-teleport-chain"
        or base.get("state_models")
        or out.exists()
    ):
        raise ValueError(
            "extension requires a completed three-state-model campaign and a new directory"
        )
    if {k: v for k, v in base["plan"].items() if k != "notes"} != {
        k: v for k, v in plan.items() if k != "notes"
    } or base["environment"] != env:
        raise ValueError("extension requires the same physical workload, software and binaries")
    for name, sha in dict(base["report_sha256"], **base["data_sha256"]).items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != sha:
            raise ValueError("changed parent campaign file: " + name)
    if read_report(source / "validation/summary.json") != base["validation"]:
        raise ValueError("changed parent validation summary")
    out.mkdir(parents=True)
    # These immutable raw reports are read only; summaries and comparisons are new files.
    for name in base["report_sha256"]:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(str(source / name), str(target))
    shutil.copy2(source / "validation/summary.json", out / "validation/summary.json")
    save_report(out / "plan.json", plan)
    save_report(out / "execution-source.json", sources)
    save_report(out / "environment.json", env)
    save_report(out / "parent-summary.json", base)
    save_report(
        out / "extension.json",
        dict(
            parent=str(source.resolve()),
            parent_summary_sha256=hashlib.sha256(
                (source / "summary.json").read_bytes()
            ).hexdigest(),
            retained_evaluation_runs=base["evaluation_runs"],
            retained_calibration_runs=base["calibration_runs"],
            retained_validation_runs=base["validation"]["noiseless_runs"]
            + base["validation"]["noisy_runs"],
            new_model="Decoupled",
            retained_models=["Full-Sync", "No-Dq-R", "Fixed-Dc"],
            changed_source_files=[
                name for name, sha in sources.items() if base["source_sha256"].get(name) != sha
            ],
            method="Hash-verified parent raw executions retained; only Decoupled native state execution is added.",
        ),
    )


def execute_case(task):
    plan, out, load, interval, phase, delays, resume = task
    key = "load-{}-interval-{}-phase-{}".format(load, interval, phase)
    metadata = dict(
        classical_load=load,
        request_interval_ns=interval,
        traffic_seed=phase,
        quantum_seed=plan["quantum_seed"],
    )
    errors, chains, signatures = [], [], {}
    maximum, reused = 0.0, 0
    try:
        for index, state in enumerate(plan["input_states"]):
            cfg = workload(plan, interval, load, phase, True)
            for c in cfg["chains"]:
                c["input_state"] = state
            reports, predictions = {}, {}
            for model in MODELS:
                path = out / "chains" / key / ("input-" + str(index)) / (model + ".json.gz")
                if resume and path.exists():
                    r = read_report(path)
                    reused += 1
                else:
                    if model == "Decoupled":
                        for baseline in reports.values():
                            seed_reference_cache(baseline)
                    own_delays = (
                        frozen_delays(reports["No-Dq-R"]) if model == "Decoupled" else delays
                    )
                    r = run_chain_model(cfg, model, own_delays)
                    if model == "Decoupled":
                        r["decoupled_validation"] = validate_decoupled(r, reports["No-Dq-R"])
                    save_report(path, r)
                if not r["validation"]["passed"] or not r["cross_validation"]["passed"]:
                    raise ValueError("failed complete-chain model")
                if any(c["input_state"] != state for c in r["config"]["chains"]):
                    raise ValueError("wrong input-state report")
                signature = timing_signature(r)
                if model in signatures and signatures[model] != signature:
                    raise ValueError("input-dependent operation/packet timing")
                signatures[model] = signature
                maximum = max(maximum, r["cross_validation"]["max_density_matrix_error"])
                reports[model] = r
                predictions[model] = model_rows(r, plan["fidelity_min"], plan["deadline_ns"])
            validate_decoupled(reports["Decoupled"], reports["No-Dq-R"])
            for model in ("No-Dq-R", "Fixed-Dc", "Decoupled"):
                errors.extend(
                    compare(
                        predictions["Full-Sync"],
                        predictions[model],
                        dict(metadata, input_state=state, model=model),
                        plan["deadline_ns"],
                    )
                )
            for m in predictions["Full-Sync"]:
                chains.append(
                    dict(
                        metadata,
                        input_state=state,
                        chain_id=m["chain_id"],
                        latency_ns=m["latency_ns"],
                        observed_fidelity=m["output_fidelity"],
                        expected_fidelity=m["expected_fidelity"],
                        **m["delays"]
                    )
                )
            save_report(
                out / "chains" / key / ("input-" + str(index)) / "comparison.json",
                dict(metadata, input_state=state, models=predictions),
            )
        return dict(
            chains=chains,
            errors=errors,
            max_density_matrix_error=maximum,
            runs=len(MODELS) * len(plan["input_states"]),
            reused=reused,
        )
    except Exception as error:
        if hasattr(error, "report"):
            save_report(out / ("failed-" + key + ".json.gz"), error.report)
        raise
    finally:
        close_workers()
        REFERENCES.clear()


def evaluate(plan, out, workers=1, resume=False, extend_from=None):
    validate_plan(plan)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be 1..8")
    sources, env = hashes(), environment()
    if extend_from is not None:
        extend_campaign(extend_from, out, plan, sources, env)
        resume = True
    if out.exists():
        if not resume or (out / "summary.json").exists():
            raise ValueError("choose a new result directory")
        if (
            read_report(out / "plan.json") != plan
            or read_report(out / "execution-source.json") != sources
            or read_report(out / "environment.json") != env
        ):
            raise ValueError("resume requires unchanged plan, source and environment")
    else:
        out.mkdir(parents=True)
        save_report(out / "plan.json", plan)
        save_report(out / "execution-source.json", sources)
        save_report(out / "environment.json", env)
    started = time.monotonic()
    val_path = out / "validation/summary.json"
    val = (
        read_report(val_path)
        if resume and val_path.exists()
        else validation(plan, val_path.parent)
    )
    if not val["passed"]:
        raise ValueError("failed six-state validation")
    print("Six-state validation PASS", flush=True)
    maximum = val["max_density_matrix_error"]
    calibration, calibration_runs = {}, 0
    for load in plan["classical_loads"]:
        reports = []
        for seed in (
            plan["calibration_traffic_seeds"] if load else plan["calibration_traffic_seeds"][:1]
        ):
            cfg = workload(plan, plan["calibration_interval_ns"], load, seed, True)
            path = out / "calibration" / ("load-{}-{}.json.gz".format(load, seed))
            if resume and path.exists():
                r = read_report(path)
            else:
                r = run_chain_model(cfg, enumerate_branches=False)
                save_report(path, r)
            if not r["validation"]["passed"] or not r["cross_validation"]["passed"]:
                raise ValueError("failed calibration")
            if any(
                any(
                    m["delays"][k]
                    for k in (
                        "swap_R_wait_ns",
                        "teleport_A_wait_ns",
                        "swap_B_wait_ns",
                        "teleport_B_wait_ns",
                    )
                )
                for m in r["metrics"]["chains"]
            ):
                raise ValueError("calibration has processor contention")
            if load:
                flow = cfg["background"]["result"]
                last = flow["start_ns"] + (flow["count"] - 1) * flow["interval_ns"]
                if r["metrics"]["batch_completion_ns"] >= last:
                    raise ValueError("calibration must finish while background is active")
            maximum = max(maximum, r["cross_validation"]["max_density_matrix_error"])
            reports.append(r)
            calibration_runs += 1
        calibration[str(load)] = calibration_entry(load, reports)
    save_report(
        out / "calibration.json",
        dict(seeds=plan["calibration_traffic_seeds"], by_load=calibration),
    )
    print("Three-path calibration PASS", flush=True)
    tasks = [
        (plan, out, load, interval, seed, calibration[str(load)]["delays"], resume)
        for load, interval in itertools.product(
            plan["classical_loads"], plan["request_intervals_ns"]
        )
        for seed in (plan["test_traffic_seeds"] if load else plan["test_traffic_seeds"][:1])
    ]
    chains, errors = [], []
    runs = reused = cells = 0
    pool = None
    try:
        if workers == 1:
            results = map(execute_case, tasks)
        else:
            pool = ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn")
            )
            results = pool.map(execute_case, tasks)
        for r in results:
            chains.extend(r["chains"])
            errors.extend(r["errors"])
            maximum = max(maximum, r["max_density_matrix_error"])
            runs += r["runs"]
            reused += r["reused"]
            cells += 1
            print(
                "{} cells / {} complete-chain runs / {:.1f}s / error {:.3g}".format(
                    cells, runs, time.monotonic() - started, maximum
                ),
                flush=True,
            )
    finally:
        if pool:
            pool.shutdown(wait=True)
        close_workers()
    csv_file(out / "chains.csv", chains)
    csv_file(out / "errors.csv", errors)
    if hashes() != sources:
        raise RuntimeError("execution source changed during experiment")
    summary = dict(
        passed=True,
        comparison_scope="complete-swap-teleport-chain",
        profile=plan["profile"],
        plan=plan,
        environment=env,
        source_sha256=sources,
        validation=val,
        paired_cases=len(tasks),
        calibration_runs=calibration_runs,
        evaluation_runs=runs,
        state_models=list(MODELS),
        chain_runs=runs // len(MODELS),
        chain_transactions=len(chains),
        transactions_per_ablation_model=len(chains),
        workers=workers,
        max_density_matrix_error=maximum,
        input_timing_equivalence_passed=True,
        ablation_groups=summarize(errors, plan["bootstrap_samples"]),
        elapsed_seconds=time.monotonic() - started,
        resumption=dict(
            resumed=resume, reused_evaluation_runs=reused, new_evaluation_runs=runs - reused
        ),
        statistics="Paired final-output branch expectations; absolute errors averaged over six inputs and workflows inside traffic phases; 95% phase-cluster bootstrap conditional on three-path calibration.",
    )
    if (out / "extension.json").exists():
        summary["extension"] = read_report(out / "extension.json")
        parent = read_report(out / "parent-summary.json")
        identity = lambda g: (g["classical_load"], g["request_interval_ns"], g["model"])
        previous = {identity(g): g for g in parent["ablation_groups"]}
        for group in summary["ablation_groups"]:
            old = previous[identity(group)]
            fields = (
                ("latency_bias_ns", "latency_mae_ns")
                if group["model"] == "Decoupled"
                else tuple(old)
            )
            if any(group[field] != old[field] for field in fields):
                raise RuntimeError(
                    "extension changed an existing timing or baseline-state result"
                )
        summary["extension"]["previous_metrics_unchanged"] = True
    summary["report_sha256"] = {
        str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in out.rglob("*.json.gz")
    }
    summary["data_sha256"] = {
        n: hashlib.sha256((out / n).read_bytes()).hexdigest()
        for n in ("chains.csv", "errors.csv", "calibration.json", "plan.json")
    }
    save_report(out / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--extend-from",
        type=Path,
        help="retain a verified three-model campaign and run the missing Decoupled model",
    )
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.quick:
        plan["classical_loads"] = [0.0, 0.75]
        plan["test_traffic_seeds"] = plan["test_traffic_seeds"][:2]
        plan["calibration_traffic_seeds"] = plan["calibration_traffic_seeds"][:1]
    try:
        result = evaluate(plan, args.output_dir, args.workers, args.resume, args.extend_from)
    finally:
        close_workers()
    print(
        "PASS: {} complete-chain runs ({} newly executed); max state error {:.3g}".format(
            result["evaluation_runs"],
            result["resumption"]["new_evaluation_runs"],
            result["max_density_matrix_error"],
        )
    )
