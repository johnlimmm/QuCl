#!/usr/bin/env python3
"""Run chained workflows over a result-link delay grid and save JSON reports plus CSV."""
import argparse
import copy
import csv
import json
from pathlib import Path
import sys

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
from run_chained import ChainedManager
from runtime import HybridValidationFailure, save_report


def run(scenario, output, delays, seeds):
    if not delays or not seeds or len(set(delays)) != len(delays) or len(set(seeds)) != len(seeds):
        raise ValueError("delay and seed grids must be nonempty and contain no duplicates")
    if any(type(d) is not int or d < 0 for d in delays):
        raise ValueError("delays must be nonnegative integer ns")
    # Normalize every case before creating an output directory or running anything.
    cases = []
    for delay in delays:
        for seed in seeds:
            config = copy.deepcopy(scenario)
            config["seed"] = seed
            link = config.setdefault("result_link", {"rate_bps": 10000000, "delay_ns": 200000})
            link["delay_ns"] = delay
            manager = ChainedManager(config)
            cases.append((delay, seed, manager))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    fields = [
        "result_delay_ns",
        "seed",
        "chain_id",
        "latency_ns",
        "completion_ns",
        "output_fidelity",
        "max_density_matrix_error",
        "report",
    ]
    with (output / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for delay, seed, manager in cases:
            name = "delay-{}-seed-{}.json.gz".format(delay, seed)
            try:
                report = manager.run()
            except HybridValidationFailure as error:
                save_report(output / name, error.report)
                (output / "failure.json").write_text(
                    json.dumps({"report": name, "error": str(error)}, indent=2) + "\n"
                )
                raise
            save_report(output / name, report)
            for row in report["metrics"]["chains"]:
                writer.writerow(
                    dict(
                        result_delay_ns=delay,
                        seed=seed,
                        chain_id=row["chain_id"],
                        latency_ns=row["latency_ns"],
                        completion_ns=row["completion_ns"],
                        output_fidelity=row["output_fidelity"],
                        max_density_matrix_error=report["cross_validation"][
                            "max_density_matrix_error"
                        ],
                        report=name,
                    )
                )
            stream.flush()
            print("delay={} ns, seed={}: PASS".format(delay, seed), flush=True)
    (output / "complete.json").write_text(
        json.dumps(
            {"passed": True, "runs": len(cases), "delays_ns": delays, "seeds": seeds}, indent=2
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--delays-ns", nargs="+", type=int, default=[100000, 500000, 1000000])
    parser.add_argument("--seeds", nargs="+", type=int, default=[7, 11])
    args = parser.parse_args()
    run(json.loads(args.scenario.read_text()), args.output_dir, args.delays_ns, args.seeds)


if __name__ == "__main__":
    main()
