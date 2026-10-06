#!/usr/bin/env python3
"""Q2NS Swap -> actual pair handoff -> A-to-B Q2NS Teleportation."""
import argparse
import json
import os
from pathlib import Path
from runtime import NS3_DIR
from runtime import HybridValidationFailure
from runtime import save_report
from gate_noise import split_config, install_gate_noise
from chained_config import normalize_config
from chained_core import ChainedCore, ChainedSwapAdapter, ChainedTeleportAdapter, ChainedCoordinator
from chained_participant import ChainedParticipant


class ChainedManager:
    def __init__(self, scenario, binary=None):
        scenario, probability = split_config(scenario)
        self.config = normalize_config(scenario)
        self.config["gate_depolar_probability"] = probability
        if binary is None:
            found = [
                p
                for p in (NS3_DIR / "build/contrib/cosim").rglob("*cosim-chained*")
                if p.is_file() and os.access(str(p), os.X_OK)
            ]
            if len(found) != 1:
                raise RuntimeError("Build ./ns3 build cosim-chained")
            binary = found[0]
        self.binary = Path(binary)

    def run(self, reference=True, enumerate_branches=False):
        from chained_validation import validate_report, metrics
        from state_validation import cross_validate_chained as cross_validate

        core = ChainedCore(self.config)
        install_gate_noise(core)
        for slot, chain in enumerate(self.config["chains"]):
            swap, tele = self.config["sessions"][2 * slot : 2 * slot + 2]
            core.adapters[swap["session_id"]] = ChainedSwapAdapter(core, swap, slot)
            core.adapters[tele["session_id"]] = ChainedTeleportAdapter(core, tele, slot)
        p = ChainedParticipant(self.binary, self.config, core.roots)
        coordinator = ChainedCoordinator(core, p)
        execution_error = None
        try:
            snapshot = coordinator.run()
            p.finish()
        except (RuntimeError, ValueError) as error:
            execution_error = error
            snapshot = core.snapshot()
        finally:
            p.close()
        report = dict(
            schema_version=5,
            architecture="swapping-assisted-teleportation-v5",
            config=self.config,
            nodes=p.nodes,
            ns3_time_ns=p.now_ns,
            time_unit="ns",
            snapshot=snapshot,
            events=core.events,
            ns3_events=coordinator.rows,
            bridge_steps=coordinator.steps,
            native_instructions=core.instruction_events,
            q2ns_status=getattr(p, "q2ns_status", None),
        )
        try:
            if execution_error:
                raise execution_error
            report["validation"] = validate_report(report)
            report["metrics"] = metrics(report)
            if reference:
                report["cross_validation"] = cross_validate(report, enumerate_branches)
        except (ValueError, RuntimeError, KeyError) as error:
            report["validation"] = dict(passed=False, error=str(error))
            raise HybridValidationFailure(report, error) from error
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ns3-binary", type=Path)
    parser.add_argument("--enumerate-branches", action="store_true")
    args = parser.parse_args()
    failure = None
    try:
        report = ChainedManager(json.loads(args.scenario.read_text()), args.ns3_binary).run(
            enumerate_branches=args.enumerate_branches
        )
    except HybridValidationFailure as error:
        report = error.report
        failure = error
    save_report(args.output, report)
    if failure:
        raise failure
    print(
        "Chained Swap -> Teleport: {} chains; error {:.3g}; PASS".format(
            len(report["metrics"]["chains"]), report["cross_validation"]["max_density_matrix_error"]
        )
    )


if __name__ == "__main__":
    main()
