#!/usr/bin/env python3
"""Run actual standalone simulators and QuCl over complete Swap--Teleport chains.

Use --pilot for a reduced first campaign. The full profile retains all original
traffic phases and loads. Q2NS-only is included only in the common noise-off
comparison. Noisy QuCl/NetSquid comparisons share their complete quantum model.
"""
import argparse
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

MODULE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE / "python"), str(MODULE / "experiments")]
from run_evaluation import workload, validate_plan, csv_file
from chain_analysis import calibration_entry
from analysis import estimate
from run_chained import ChainedManager
from standalone_netsquid import run_netsquid
from standalone_q2ns import run_q2ns
from runtime import save_report, NS3_DIR

ENGINES = ("QuCl", "NetSquid-only", "Q2NS-only")


def noise_off(raw):
    raw = copy.deepcopy(raw)
    raw["memory_noise"].update(T1_ns=0, T2_ns=0)
    raw["gate_depolar_probability"] = 0
    for q in raw["quantum_links"].values():
        q["depolar_rate_hz"] = 0
    return raw


def run(engine, config, delays, reference=True):
    if engine == "Q2NS-only":
        return run_q2ns(config)
    if engine == "NetSquid-only":
        return run_netsquid(config, delays, reference=reference, enumerate_branches=reference)
    if engine == "QuCl":
        return ChainedManager(config).run(reference=reference, enumerate_branches=reference)
    raise ValueError("unknown engine")


def observations(report):
    if report.get("model") == "Q2NS-only":
        return [
            dict(chain_id=int(cid), **m, expected_fidelity=m["output_fidelity"])
            for cid, m in report["metrics"].items()
        ]
    refs = report.get("cross_validation", {}).get("chains", {})
    result = []
    for m in report["metrics"]["chains"]:
        result.append(
            dict(
                chain_id=m["chain_id"],
                latency_ns=m["latency_ns"],
                completion_ns=m["completion_ns"],
                output_fidelity=m["output_fidelity"],
                expected_fidelity=(
                    refs[str(m["chain_id"])]["reference"]["expected_fidelity"]
                    if refs
                    else m["output_fidelity"]
                ),
            )
        )
    return result


def sources():
    files = [
        p
        for folder in ("python", "experiments", "examples", "model")
        for p in (MODULE / folder).rglob("*")
        if p.is_file() and p.suffix in (".py", ".cc", ".h")
    ]
    return {
        str(p.relative_to(MODULE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }


def evaluate(plan, out, pilot=False):
    validate_plan(plan)
    plan = copy.deepcopy(plan)
    if pilot:
        plan["classical_loads"] = [0.0, 0.75]
        plan["test_traffic_seeds"] = plan["test_traffic_seeds"][:4]
    if out.exists():
        raise ValueError("use a new output directory")
    out.mkdir(parents=True)
    stamp = sources()
    save_report(out / "plan.json", plan)
    calibration, calibration_runs = {}, 0
    for load in plan["classical_loads"]:
        reports = []
        for phase in (
            plan["calibration_traffic_seeds"] if load else plan["calibration_traffic_seeds"][:1]
        ):
            cfg = workload(plan, plan["calibration_interval_ns"], load, phase, True)
            report = ChainedManager(cfg).run(reference=False)
            for m in report["metrics"]["chains"]:
                if any(v for k, v in m["delays"].items() if k.endswith("_wait_ns")):
                    raise RuntimeError("calibration processor/resource contention")
                if load and m["completion_ns"] > plan["background_end_ns"]:
                    raise RuntimeError("calibration outlasts background traffic")
            save_report(
                out / "calibration" / ("load-{}-seed-{}.json.gz".format(load, phase)), report
            )
            reports.append(report)
            calibration_runs += 1
        calibration[str(load)] = calibration_entry(load, reports)
    save_report(out / "calibration.json", calibration)
    records, maximum, evaluated = [], 0.0, 0
    for load in plan["classical_loads"]:
        phases = plan["test_traffic_seeds"] if load else plan["test_traffic_seeds"][:1]
        delays = calibration[str(load)]["delays"]
        for interval, phase in itertools.product(plan["request_intervals_ns"], phases):
            for state_index, state in enumerate(plan["input_states"]):
                config = workload(plan, interval, load, phase, True)
                for ch in config["chains"]:
                    ch["input_state"] = state
                for mode in ("noiseless", "noisy"):
                    engines = ENGINES if mode == "noiseless" else ENGINES[:2]
                    cfg = noise_off(config) if mode == "noiseless" else config
                    full = None
                    for engine in engines:
                        report = run(engine, cfg, delays, reference=mode == "noisy")
                        path = (
                            "runs/{}/{}/load-{}-interval-{}-phase-{}-input-{}.json.gz".format(
                                mode, engine, load, interval, phase, state_index
                            )
                        )
                        save_report(out / path, report)
                        evaluated += 1
                        maximum = max(
                            maximum,
                            report.get("cross_validation", report["validation"]).get(
                                "max_density_matrix_error", 0
                            ),
                        )
                        obs = observations(report)
                        if mode == "noiseless" and any(
                            abs(m["output_fidelity"] - 1) > 1e-12 for m in obs
                        ):
                            raise RuntimeError("standalone noiseless output mismatch")
                        if full is None:
                            full = {m["chain_id"]: m for m in obs}
                        for m in obs:
                            base = full[m["chain_id"]]
                            records.append(
                                dict(
                                    mode=mode,
                                    engine=engine,
                                    load=load,
                                    interval_ns=interval,
                                    traffic_seed=phase,
                                    input_state=state,
                                    chain_id=m["chain_id"],
                                    latency_ns=m["latency_ns"],
                                    expected_fidelity=m["expected_fidelity"],
                                    sampled_fidelity=m["output_fidelity"],
                                    latency_bias_ns=m["latency_ns"] - base["latency_ns"],
                                    latency_error_ns=abs(m["latency_ns"] - base["latency_ns"]),
                                    fidelity_bias=m["expected_fidelity"]
                                    - base["expected_fidelity"],
                                    report=path,
                                )
                            )
            print(
                "completed load={} interval={} phase={}".format(load, interval, phase),
                flush=True,
            )
    groups = []
    for mode, engine, load, interval in sorted(
        {(r["mode"], r["engine"], r["load"], r["interval_ns"]) for r in records}
    ):
        own = [
            r
            for r in records
            if (r["mode"], r["engine"], r["load"], r["interval_ns"])
            == (mode, engine, load, interval)
        ]
        # Timing must be input-independent, including Q2NS's sampled measurement branches.
        for phase, chain in {(r["traffic_seed"], r["chain_id"]) for r in own}:
            matches = [r for r in own if r["traffic_seed"] == phase and r["chain_id"] == chain]
            if (
                len(matches) != len(plan["input_states"])
                or len({r["latency_ns"] for r in matches}) != 1
            ):
                raise RuntimeError("incomplete inputs or input-dependent timing")
        groups.append(
            dict(
                mode=mode,
                engine=engine,
                load=load,
                interval_ns=interval,
                traffic_phases=len({r["traffic_seed"] for r in own}),
                **{
                    k: estimate(own, k, plan["bootstrap_samples"])
                    for k in (
                        "latency_ns",
                        "expected_fidelity",
                        "latency_bias_ns",
                        "latency_error_ns",
                        "fidelity_bias",
                    )
                },
            )
        )
    csv_file(out / "observations.csv", records)
    if stamp != sources():
        raise RuntimeError("source changed during campaign")
    summary = dict(
        passed=True,
        pilot=pilot,
        plan=plan,
        calibration_runs=calibration_runs,
        simulation_runs=evaluated,
        max_density_matrix_error=maximum,
        groups=groups,
        source_sha256=stamp,
        scope="Complete chain. Three actual engines share a noise-off workload; only QuCl and NetSquid-only share the noisy quantum model. Q2NS timing is instantaneous.",
        statistics="Six inputs and four workflows averaged within traffic phase. 95% phase-cluster percentile bootstrap, conditional on fixed calibration. Zero load is deterministic.",
    )
    save_report(out / "summary.json", summary)
    return summary


def costs(plan, calibration, out, repeats):
    """Sequential fresh-process wall cost, including startup, validation and I/O.

    Different native workloads do different work. These measurements are not
    isolated IPC overhead, and Q2NS/noiseless cannot substitute for noisy QuCl.
    """
    rows = []
    # Interleave engines at each repeat to avoid one engine always running last.
    for count, load in itertools.product((1, 4, 8, 16, 32), (0.0, 0.75)):
        p = copy.deepcopy(plan)
        p["sessions"] = count
        p["background_end_ns"] = max(
            p["background_end_ns"], p["first_start_ns"] + count * 27405 + 2000000
        )
        config = noise_off(workload(p, 13703, load, p["test_traffic_seeds"][0], True))
        for repeat in range(repeats):
            order = ENGINES[repeat % 3 :] + ENGINES[: repeat % 3]
            for engine in order:
                key = "{}-load-{}-n-{}-repeat-{}".format(engine, load, count, repeat)
                task = out / (key + "-input.json")
                output = out / (key + ".json.gz")
                save_report(
                    task,
                    dict(engine=engine, config=config, delays=calibration[str(load)]["delays"]),
                )
                begin = time.perf_counter()
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(task),
                        "--output",
                        str(output),
                    ],
                    check=True,
                    timeout=180,
                )
                elapsed = time.perf_counter() - begin
                rows.append(
                    dict(
                        engine=engine,
                        load=load,
                        workflows=count,
                        repeat=repeat,
                        wall_seconds=elapsed,
                        seconds_per_workflow=elapsed / count,
                    )
                )
        print("cost load={} workflows={}".format(load, count), flush=True)
    csv_file(out / "runtime.csv", rows)
    save_report(
        out / "summary.json",
        dict(
            passed=True,
            repeats=repeats,
            runs=len(rows),
            records=rows,
            python=sys.version,
            platform=platform.platform(),
            cpu_affinity=sorted(os.sched_getaffinity(0)),
            scope="Noise-off complete chains; sequential fresh Python process + native engine startup, validation, trace/report generation and disk I/O; independent reference excluded. Not isolated IPC overhead or equivalent-work speedup.",
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", type=Path, default=MODULE / "experiments/chain-profile.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--runtime-repeats", type=int, default=0)
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker:
        task = json.loads(args.worker.read_text())
        save_report(
            args.output, run(task["engine"], task["config"], task["delays"], reference=False)
        )
        return
    if args.output.exists():
        parser.error("use a new output directory; existing results are never overwritten")
    if args.runtime_repeats < 0:
        parser.error("runtime repeats must be nonnegative")
    plan = json.loads(args.profile.read_text())
    try:
        summary = evaluate(plan, args.output, args.pilot)
        if args.runtime_repeats:
            costs(
                summary["plan"],
                json.loads((args.output / "calibration.json").read_text()),
                args.output / "runtime",
                args.runtime_repeats,
            )
    except BaseException as error:
        if args.output.exists() and not (args.output / "failure.json").exists():
            save_report(
                args.output / "failure.json", dict(error=str(error), type=type(error).__name__)
            )
        raise


if __name__ == "__main__":
    main()
