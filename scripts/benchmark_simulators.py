#!/usr/bin/env python3
"""Cold-start cost with engine-specific imports, including validation and output.

Configuration preparation is outside the timed subprocess. In particular the
Q2NS worker does not import NetSquid just to parse shared configuration. Each
worker writes one compressed report after running its native execution path.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "experiments")]


def worker(task_path, output):
    from runtime import save_report

    task = json.loads(task_path.read_text())
    name = task["engine"]
    if name == "Q2NS-only":
        from standalone_q2ns import batch_config, parse_output, validate_native
        import tempfile

        # The parent already validated the same noise-free configuration used
        # by the other engines. This worker only runs native Q2NS state updates.
        c = task["normalized"]
        if (
            any(c["memory_noise"][k] for k in ("T1_ns", "T2_ns"))
            or c["gate_depolar_probability"]
            or any(v["depolar_rate_hz"] for v in c["quantum_links"].values())
        ):
            raise ValueError("native Q2NS cost requires noise off")
        with tempfile.TemporaryDirectory(prefix="qucl-q2ns-cost-") as temp:
            cfg, trace = Path(temp) / "config.txt", Path(temp) / "trace.txt"
            cfg.write_text(batch_config(c))
            subprocess.run(
                [task["binary"], "--nativeConfig=" + str(cfg), "--nativeOutput=" + str(trace)],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=120,
            )
            report = parse_output(trace.read_text(), c)
        report["validation"], report["metrics"] = validate_native(report)
        if "netsquid" in sys.modules:
            raise RuntimeError("Q2NS-only benchmark unexpectedly imported NetSquid")
    elif name == "NetSquid-only":
        from standalone_netsquid import run_netsquid

        report = run_netsquid(task["config"], task["delays"], reference=False)
    elif name == "QuCl":
        from run_chained import ChainedManager

        report = ChainedManager(task["config"], task["binary"]).run(reference=False)
    else:
        raise ValueError("unknown engine")
    report["benchmark"] = dict(
        netsquid_imported="netsquid" in sys.modules, independent_reference=False
    )
    save_report(output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.output)
        return
    if args.campaign is None or args.output.exists() or args.repeats < 1:
        parser.error(
            "provide a completed campaign, a new output directory and positive repeats"
        )
    # Imports needed by configuration construction happen only in this untimed parent.
    from run_simulator_comparison import noise_off, ENGINES
    from run_evaluation import workload, csv_file
    from run_chained import ChainedManager
    from runtime import save_report
    import copy
    import hashlib

    s = json.loads((args.campaign / "summary.json").read_text())
    if not s["passed"]:
        raise RuntimeError("completed calibration/evaluation required")
    calibration = json.loads((args.campaign / "calibration.json").read_text())
    records = []
    args.output.mkdir(parents=True)
    for count in (1, 4, 8, 16, 32):
        for load in (0.0, 0.75):
            plan = copy.deepcopy(s["plan"])
            plan["sessions"] = count
            bsm = (
                plan["native_instructions"]["cnot_ns"]
                + plan["native_instructions"]["h_ns"]
                + 2 * plan["native_instructions"]["measure_ns"]
            )
            plan["background_end_ns"] = max(
                plan["background_end_ns"], plan["first_start_ns"] + count * bsm + 2000000
            )
            config = noise_off(
                workload(
                    plan,
                    plan["request_intervals_ns"][0],
                    load,
                    plan["test_traffic_seeds"][0],
                    True,
                )
            )
            manager = ChainedManager(config)
            for repeat in range(args.repeats):
                for engine in ENGINES[repeat % 3 :] + ENGINES[: repeat % 3]:
                    key = "{}-n-{}-load-{}-repeat-{}".format(engine, count, load, repeat)
                    task, output = args.output / (key + "-input.json"), args.output / (
                        key + ".json.gz"
                    )
                    save_report(
                        task,
                        dict(
                            engine=engine,
                            config=config,
                            normalized=manager.config,
                            binary=str(manager.binary),
                            delays=calibration[str(load)]["delays"],
                        ),
                    )
                    start = time.perf_counter()
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
                    wall = time.perf_counter() - start
                    records.append(
                        dict(
                            engine=engine,
                            workflows=count,
                            load=load,
                            repeat=repeat,
                            wall_seconds=wall,
                            seconds_per_workflow=wall / count,
                        )
                    )
            print("completed cost n={} load={}".format(count, load), flush=True)
    csv_file(args.output / "runtime.csv", records)
    save_report(
        args.output / "summary.json",
        dict(
            passed=True,
            runs=len(records),
            repeats=args.repeats,
            records=records,
            python=sys.version,
            platform=platform.platform(),
            cpu_affinity=sorted(os.sched_getaffinity(0)),
            binary_sha256=hashlib.sha256(manager.binary.read_bytes()).hexdigest(),
            worker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            scope="Noise-off; cold engine-specific process startup, native execution, validation and report I/O. Shared config preparation, reference enumeration and build excluded. Q2NS worker imports no NetSquid. Not isolated IPC overhead or equivalent-work speedup.",
        ),
    )


if __name__ == "__main__":
    main()
