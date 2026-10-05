#!/usr/bin/env python3
"""Reproduce the six-input chain campaign and three swapping-stage ablations."""
import argparse
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import copy
import csv
import gzip
import hashlib
import itertools
import json
import math
import platform
from pathlib import Path
import random
import sys
import time

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
from run_chained import ChainedManager
from models import run_model, model_rows
from state_validation import cross_validate_chained

from run_provisioned import save_report
from packet_timing import tx_duration_ns
from analysis import calibration_entry
from timing import decoupled
from analysis import compare, summarize
from reference_client import close_workers
from limits import integer
from runtime import NS3_DIR


class Cache(OrderedDict):
    def __setitem__(self, key, value):
        if key not in self and len(self) >= 128:
            self.popitem(last=False)
        super().__setitem__(key, value)


def bsm_duration(plan):
    d = plan["native_instructions"]
    return d["cnot_ns"] + d["h_ns"] + 2 * d["measure_ns"]


def hashes():
    paths = list((MODULE / "python").glob("*.py"))
    paths += [
        MODULE / "experiments" / name
        for name in ("run_evaluation.py", "models.py", "timing.py", "analysis.py")
    ]
    paths += list((MODULE / "examples").glob("*.cc")) + list((MODULE / "model").glob("*"))
    paths += list((MODULE / "integrations").glob("*.patch"))
    return {str(p.relative_to(MODULE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def environment():
    import netsquid
    import numpy

    binaries = {}
    for name in ("cosim-chained", "cosim-provisioned"):
        found = [
            p
            for p in (NS3_DIR / "build/contrib/cosim").rglob("*" + name + "*")
            if p.is_file() and p.stat().st_mode & 0o111
        ]
        if len(found) != 1:
            raise RuntimeError("Build ./ns3 build " + name)
        binaries[name] = dict(
            path=str(found[0]), sha256=hashlib.sha256(found[0].read_bytes()).hexdigest()
        )
    return dict(
        python=sys.version,
        executable=sys.executable,
        platform=platform.platform(),
        netsquid=netsquid.__version__,
        numpy=numpy.__version__,
        binaries=binaries,
    )


def workload(plan, interval, load, traffic, chain=False):
    cfg = {
        k: copy.deepcopy(plan[k])
        for k in (
            "native_instructions",
            "memory_noise",
            "quantum_links",
            "gate_depolar_probability",
        )
    }
    cfg.update(
        name="evaluation-chained" if chain else "evaluation-swapping",
        seed=plan["quantum_seed"],
        result_link=copy.deepcopy(plan["classical_link"]),
    )
    serial = tx_duration_ns(plan["background_payload_bytes"], cfg["result_link"]["rate_bps"])
    period = round(serial / load) if load else 1000000
    phase = random.Random(traffic).randrange(period)
    count = max(0, (plan["background_end_ns"] - phase) // period + 1) if load else 0
    cfg["background"] = dict(
        result=dict(
            start_ns=phase,
            interval_ns=period,
            count=count,
            payload_bytes=plan["background_payload_bytes"],
        )
    )
    if chain:
        cfg["access_link"] = copy.deepcopy(plan["classical_link"])
        cfg["chains"] = [
            dict(
                chain_id=i + 1,
                session_start_ns=plan["first_start_ns"] + i * interval,
                input_state="+i",
            )
            for i in range(plan["sessions"])
        ]
    else:
        cfg["sessions"] = [
            dict(
                session_id=i + 1,
                protocol="swap",
                session_start_ns=plan["first_start_ns"] + i * interval,
            )
            for i in range(plan["sessions"])
        ]
    return cfg


def csv_file(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def validation(plan, out):
    states = ["0", "1", "+", "-", "+i", "-i"]
    coverage = {s: set() for s in states}
    maximum = 0.0
    minimum = 1.0
    runs = 0
    checked = 0
    records = []
    base = workload(plan, bsm_duration(plan) * 2, 0, 1000, True)
    base["memory_noise"].update(T1_ns=0, T2_ns=0)
    base["gate_depolar_probability"] = 0
    base["chains"] = [
        dict(chain_id=i + 1, session_start_ns=plan["first_start_ns"] + i * 1000000, input_state=s)
        for i, s in enumerate(states)
    ]
    for seed in range(256):
        base["seed"] = seed
        report = ChainedManager(base).run(reference=False)
        runs += 1
        new = []
        for chain in report["config"]["chains"]:
            stage = report["snapshot"]["sessions"]
            state = chain["input_state"]
            bits = "".join(
                str(b)
                for sid in (chain["swap_session_id"], chain["teleport_session_id"])
                for b in stage[str(sid)]["measurement_bits"]
            )
            fidelity = stage[str(chain["teleport_session_id"])]["checkpoints"]["usable"]["fidelity"]
            minimum = min(minimum, fidelity)
            if abs(fidelity - 1) > 1e-12:
                raise RuntimeError("noiseless fidelity")
            if bits not in coverage[state]:
                new.append(chain)
                coverage[state].add(bits)
        if new:
            view = dict(report, config=dict(report["config"], chains=new))
            report["cross_validation"] = cross_validate_chained(view)
            maximum = max(maximum, report["cross_validation"]["max_density_matrix_error"])
            checked += len(new)
        save_report(out / ("coverage/seed-{}.json.gz".format(seed)), report)
        if all(len(v) == 16 for v in coverage.values()):
            break
    if not all(len(v) == 16 for v in coverage.values()):
        raise RuntimeError("incomplete joint branch coverage")
    for state, delay in itertools.product(states, [100000, 500000]):
        cfg = workload(plan, 2 * bsm_duration(plan), 0, 1000, True)
        cfg["chains"] = [
            dict(chain_id=1, session_start_ns=plan["first_start_ns"], input_state=state)
        ]
        cfg["access_link"]["delay_ns"] = delay
        report = ChainedManager(cfg).run(enumerate_branches=True)
        ref = report["cross_validation"]
        maximum = max(maximum, ref["max_density_matrix_error"])
        path = "noise/{}-{}.json.gz".format(states.index(state), delay)
        save_report(out / path, report)
        records.append(
            dict(
                input_state=state,
                access_delay_ns=delay,
                report=path,
                expected_fidelity=ref["chains"]["1"]["reference"]["expected_fidelity"],
                latency_ns=report["metrics"]["chains"][0]["latency_ns"],
            )
        )
    result = dict(
        passed=True,
        noiseless_runs=runs,
        noiseless_transactions=runs * 6,
        distinct_input_branches_checked=checked,
        branches={s: sorted(v) for s, v in coverage.items()},
        minimum_noiseless_fidelity=minimum,
        noisy_runs=len(records),
        noisy_cases=records,
        max_density_matrix_error=maximum,
    )
    save_report(out / "summary.json", result)
    return result


def read_report(path):
    with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as stream:
        return json.load(stream)


def validate_plan(plan):
    template = json.loads((MODULE / "experiments/profile.json").read_text())
    if not isinstance(plan, dict) or set(plan) != set(template):
        raise ValueError("plan fields must match experiments/profile.json")
    if plan["input_states"] != ["0", "1", "+", "-", "+i", "-i"]:
        raise ValueError("six distinct, equally weighted input states required")
    if plan["memory_noise"] != dict(model="T1T2NoiseModel", T1_ns=20000000, T2_ns=10000000):
        raise ValueError("registered memory profile is T1=20 ms, T2=10 ms")
    if set(plan["test_traffic_seeds"]) & set(plan["calibration_traffic_seeds"]):
        raise ValueError("seed overlap")
    for key in ("test_traffic_seeds", "calibration_traffic_seeds", "request_intervals_ns"):
        values = plan[key]
        if not isinstance(values, list) or not values or len(set(values)) != len(values):
            raise ValueError("empty or duplicate " + key)
        for value in values:
            integer(value, key, 1 if key == "request_intervals_ns" else 0, 2**32 - 1)
    loads = plan["classical_loads"]
    if (
        not isinstance(loads, list)
        or not loads
        or len(set(loads)) != len(loads)
        or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v < 1 for v in loads)
        or loads != sorted(loads)
        or loads[0] != 0
    ):
        raise ValueError("loads must be sorted, unique, in [0,1), and include zero")
    # Use canonical float names so report paths do not depend on JSON 0 versus 0.0.
    plan["classical_loads"] = [float(v) for v in loads]
    for key in (
        "sessions",
        "first_start_ns",
        "background_payload_bytes",
        "background_end_ns",
        "calibration_interval_ns",
        "bootstrap_samples",
        "deadline_ns",
    ):
        integer(plan[key], key, 1)
    integer(plan["sessions"], "sessions", 1, 64)
    integer(plan["quantum_seed"], "quantum_seed", 0, 2**32 - 1)
    factors = plan["interval_factors"]
    if len(factors) != len(plan["request_intervals_ns"]) or any(
        type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in factors
    ):
        raise ValueError("invalid interval factors")
    if plan["request_intervals_ns"] != [
        int(math.floor(v * bsm_duration(plan) + 0.5)) for v in factors
    ]:
        raise ValueError("intervals must match rounded BSM-duration multiples")
    if plan["request_intervals_ns"] != sorted(plan["request_intervals_ns"]) or factors[-1] < 1:
        raise ValueError("include an unloaded noncontending interval")
    if type(plan["fidelity_min"]) not in (int, float) or not 0 <= plan["fidelity_min"] <= 1:
        raise ValueError("invalid fidelity threshold")
    if plan["first_start_ns"] < max(v["delay_ns"] for v in plan["quantum_links"].values()):
        raise ValueError("controlled requests must start after resource delivery")
    # Production normalization validates all gate, link, memory and traffic fields.
    # No processes are started by manager construction.
    for load, interval in itertools.product(loads, plan["request_intervals_ns"]):
        ChainedManager(workload(plan, interval, load, plan["test_traffic_seeds"][0], True))


def timing_signature(report):
    # Quantum branches may differ by input, but fixed service slots and packet
    # sizes make timing input-independent. Check before averaging six states.
    return [
        (m["chain_id"], m["latency_ns"], sorted(m["delays"].items()))
        for m in report["metrics"]["chains"]
    ]


def execute_case(task):
    """One independent traffic condition; never shares native simulator state."""
    plan, out, load, interval, traffic, delays, resume = task
    cache = Cache()
    errors, chains = [], []
    run_count = reused = 0
    maximum = 0.0
    try:
        key = "load-{}-interval-{}-phase-{}".format(load, interval, traffic)
        metadata = dict(
            classical_load=load,
            request_interval_ns=interval,
            traffic_seed=traffic,
            quantum_seed=plan["quantum_seed"],
        )
        signature = None
        for state_index, state in enumerate(plan["input_states"]):
            cfg = workload(plan, interval, load, traffic, True)
            for chain in cfg["chains"]:
                chain["input_state"] = state
            path = out / "chains" / key / ("input-{}.json.gz".format(state_index))
            if resume and path.exists():
                report = read_report(path)
                reused += 1
            else:
                report = ChainedManager(cfg).run(enumerate_branches=True)
                save_report(path, report)
            if not report["validation"]["passed"] or not report["cross_validation"]["passed"]:
                raise ValueError("failed chain")
            if any(c["input_state"] != state for c in report["config"]["chains"]):
                raise ValueError("wrong input-state report")
            current_signature = timing_signature(report)
            if signature is not None and current_signature != signature:
                raise ValueError("input-dependent timing; cannot pool six states")
            signature = current_signature
            run_count += 1
            maximum = max(maximum, report["cross_validation"]["max_density_matrix_error"])
            for m in report["metrics"]["chains"]:
                ref = report["cross_validation"]["chains"][str(m["chain_id"])]["reference"]
                chains.append(
                    dict(
                        metadata,
                        input_state=state,
                        chain_id=m["chain_id"],
                        latency_ns=m["latency_ns"],
                        expected_fidelity=ref["expected_fidelity"],
                        observed_fidelity=m["output_fidelity"],
                        **m["delays"],
                    )
                )
        rows = {}
        reports = {}
        cfg = workload(plan, interval, load, traffic)
        for model in ("Full-Sync", "No-Dq-R", "Fixed-Dc"):
            path = out / "ablations" / key / (model + ".json.gz")
            if resume and path.exists():
                r = read_report(path)
                reused += 1
            else:
                r = run_model(cfg, model, cache, delays)
                save_report(path, r)
            if not r["validation"]["passed"] or not r["cross_validation"]["passed"]:
                raise ValueError("failed model")
            run_count += 1
            maximum = max(maximum, r["cross_validation"]["max_density_matrix_error"])
            reports[model] = r
            rows[model] = model_rows(r, plan["fidelity_min"], plan["deadline_ns"])
        rows["Decoupled"] = decoupled(reports["Full-Sync"]["config"], reports["No-Dq-R"])
        for model in ("No-Dq-R", "Fixed-Dc", "Decoupled"):
            errors.extend(
                compare(
                    rows["Full-Sync"],
                    rows[model],
                    dict(metadata, model=model),
                    plan["deadline_ns"],
                )
            )
        case = dict(metadata, models=rows)
        save_report(out / "ablations" / key / "comparison.json", case)

        return dict(
            errors=errors,
            chains=chains,
            case=case,
            runs=run_count,
            reused=reused,
            max_density_matrix_error=maximum,
        )
    except Exception as error:
        if hasattr(error, "report"):
            save_report(
                out / ("failed-case-{}-{}-{}.json.gz".format(load, interval, traffic)), error.report
            )
        raise
    finally:
        close_workers()


def evaluate(plan, out, resume=False, workers=1):
    integer(workers, "workers", 1, 8)
    validate_plan(plan)
    old_sources = None
    reused = 0
    if out.exists():
        if not resume or (out / "summary.json").exists():
            raise ValueError("choose a new result directory")
        if read_report(out / "plan.json") != plan:
            raise ValueError("resume plan mismatch")
        old_sources = read_report(out / "execution-source.json")
        current = hashes()
        if current != old_sources:
            raise ValueError("execution source changed; cannot resume")
    elif resume:
        raise ValueError("no run to resume")
    if set(plan["test_traffic_seeds"]) & set(plan["calibration_traffic_seeds"]):
        raise ValueError("seed overlap")
    env = environment()
    if resume and read_report(out / "environment.json") != env:
        raise ValueError("environment or participant binary changed; cannot resume")
    out.mkdir(parents=True, exist_ok=resume)
    start = time.monotonic()
    sources = hashes()
    save_report(out / "plan.json", plan)
    save_report(out / "execution-source.json", sources)
    save_report(out / "environment.json", env)
    val = (
        read_report(out / "validation/summary.json")
        if resume and (out / "validation/summary.json").exists()
        else validation(plan, out / "validation")
    )
    if not val["passed"]:
        raise ValueError("failed validation")
    print("Six-state validation PASS" + (" (retained validated run)" if resume else ""), flush=True)
    maximum = val["max_density_matrix_error"]
    cache = Cache()
    calibration = {}
    cal_runs = 0
    for load in plan["classical_loads"]:
        records = []
        for seed in (
            plan["calibration_traffic_seeds"] if load else plan["calibration_traffic_seeds"][:1]
        ):
            cfg = workload(plan, plan["calibration_interval_ns"], load, seed)
            path = out / ("calibration/load-{}-{}.json.gz".format(load, seed))
            report = (
                read_report(path)
                if resume and path.exists()
                else run_model(cfg, "Full-Sync", cache)
            )
            if not report["validation"]["passed"] or not report["cross_validation"]["passed"]:
                raise ValueError("failed calibration")
            records.append(report)
            cal_runs += 1
            if load:
                traffic = cfg["background"]["result"]
                last_background = (
                    traffic["start_ns"] + (traffic["count"] - 1) * traffic["interval_ns"]
                )
                if report["batch_completion_ns"] >= last_background:
                    raise RuntimeError(
                        "calibration must complete while background traffic is active"
                    )
            if not path.exists():
                save_report(path, report)
            maximum = max(maximum, report["cross_validation"]["max_density_matrix_error"])
        calibration[str(load)] = calibration_entry(load, records)
    save_report(
        out / "calibration.json", dict(seeds=plan["calibration_traffic_seeds"], by_load=calibration)
    )
    errors = []
    chains = []
    cases = []
    run_count = 0
    tasks = [
        (plan, out, load, interval, traffic, calibration[str(load)]["delays"], resume)
        for load, interval in itertools.product(
            plan["classical_loads"], plan["request_intervals_ns"]
        )
        for traffic in (plan["test_traffic_seeds"] if load else plan["test_traffic_seeds"][:1])
    ]
    pool = None
    try:
        if workers == 1:
            results = map(execute_case, tasks)
        else:
            # Spawn, never fork an initialized NetSquid simulator. Each condition
            # owns separate ns-3 participants and isolated reference workers.
            pool = ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn")
            )
            results = pool.map(execute_case, tasks)
        for result in results:
            errors.extend(result["errors"])
            chains.extend(result["chains"])
            cases.append(result["case"])
            run_count += result["runs"]
            reused += result["reused"]
            maximum = max(maximum, result["max_density_matrix_error"])
            print(
                "{} cells / {} runs / {:.1f}s / error {:.2g}".format(
                    len(cases), run_count, time.monotonic() - start, maximum
                ),
                flush=True,
            )
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
        close_workers()
    csv_file(out / "chains.csv", chains)
    csv_file(out / "errors.csv", errors)
    if hashes() != sources:
        raise RuntimeError("execution source changed during experiment")
    summary = dict(
        passed=True,
        profile=plan["profile"],
        plan=plan,
        source_sha256=sources,
        validation=val,
        environment=env,
        workers=workers,
        calibration_runs=cal_runs,
        paired_cases=len(cases),
        chain_runs=len(cases) * len(plan["input_states"]),
        evaluation_runs=run_count,
        chain_transactions=len(chains),
        transactions_per_ablation_model=len(cases) * plan["sessions"],
        max_density_matrix_error=maximum,
        ablation_groups=summarize(errors, plan["bootstrap_samples"]),
        elapsed_seconds=time.monotonic() - start,
        elapsed_scope="current invocation only; includes retained-report audits on resume",
        input_timing_equivalence_passed=True,
        statistics="Exact branch-weighted quantum expectation, equally averaged over six input states and four sessions inside each traffic phase; traffic-phase cluster bootstrap conditional on fixed calibration; zero-load phase not repeated.",
        scope="Literature-informed composite abstract processor: Liao/Iqbal gates, Bugalho 20/10 ms memory applied uniformly; fixed topology, ideal source/readout, lossless delivery, finite four-session batches.",
    )
    summary["resumption"] = dict(
        resumed=resume,
        reused_evaluation_runs=reused,
        new_evaluation_runs=run_count - reused,
        source_changes=(
            {}
            if old_sources is None
            else {
                k: dict(before=v, after=sources[k])
                for k, v in old_sources.items()
                if sources[k] != v
            }
        ),
        reason="Resume only byte-identical plan and execution sources." if resume else None,
    )
    summary["report_sha256"] = {
        str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in out.rglob("*.json.gz")
    }
    summary["data_sha256"] = {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in ("chains.csv", "errors.csv", "calibration.json", "plan.json")
    }
    save_report(out / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="retain validated reports; require identical physics/circuit sources",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="two loads and two traffic phases; a smoke campaign, not the full grid",
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="independent condition processes (1 to 8)"
    )
    args = parser.parse_args()
    # Reject completed/non-resumable output before writing even a failure marker.
    if args.output_dir.exists() and (
        not args.resume or (args.output_dir / "summary.json").exists()
    ):
        parser.error("choose a new result directory")
    plan = json.loads(args.plan.read_text())
    if args.quick:
        plan["classical_loads"] = [0.0, 0.75]
        plan["test_traffic_seeds"] = plan["test_traffic_seeds"][:2]
        plan["calibration_traffic_seeds"] = plan["calibration_traffic_seeds"][:1]
    try:
        result = evaluate(plan, args.output_dir, args.resume, args.workers)
    except Exception as error:
        # Input/resume rejection must not alter an existing run.
        if args.output_dir.exists() and not isinstance(error, FileExistsError):
            if hasattr(error, "report"):
                save_report(args.output_dir / "failure-report.json.gz", error.report)
            if not args.resume:
                save_report(args.output_dir / "failure.json", dict(passed=False, error=str(error)))
        raise
    finally:
        close_workers()
    print(
        "PASS: {} evaluation runs; maximum state error {:.3g}".format(
            result["evaluation_runs"], result["max_density_matrix_error"]
        )
    )
