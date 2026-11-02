#!/usr/bin/env python3
"""Actual quantum-channel delivery feeding shared Q2NS/native execution."""
import argparse
import json
import os
from pathlib import Path
from runtime import NS3_DIR
from runtime import HybridValidationFailure, session_metrics, save_report
from provisioned_config import normalize_config
from provisioned_core import (
    ProvisionedCore,
    ProvisionedCoordinator,
    ProvisionedSwapAdapter,
    ProvisionedTeleportAdapter,
)
from provisioned_participant import ProvisionedParticipant
from provisioned_validation import validate_report
from state_validation import cross_validate_provisioned as cross_validate
from gate_noise import split_config, install_gate_noise


class ProvisionedManager:
    def __init__(self, scenario, binary=None):
        scenario, probability = split_config(scenario)
        self.config = normalize_config(scenario)
        self.config["gate_depolar_probability"] = probability
        if binary is None:
            found = [
                p
                for p in (NS3_DIR / "build/contrib/cosim").rglob("*cosim-provisioned*")
                if p.is_file() and os.access(str(p), os.X_OK)
            ]
            if len(found) != 1:
                raise RuntimeError("Build ./ns3 build cosim-provisioned")
            binary = found[0]
        self.binary = Path(binary)

    def run(self, references=None, *, capture_states=True):
        core = ProvisionedCore(self.config, capture_states)
        install_gate_noise(core)
        for slot, s in enumerate(self.config["sessions"]):
            core.adapters[s["session_id"]] = (
                ProvisionedSwapAdapter if s["protocol"] == "swap" else ProvisionedTeleportAdapter
            )(core, s, slot)
        p = ProvisionedParticipant(self.binary, self.config, core.roots)
        coordinator = ProvisionedCoordinator(core, p)
        try:
            snapshot = coordinator.run()
            p.finish()
        finally:
            p.close()
        report = dict(
            schema_version=4,
            architecture="provisioned-native-v4",
            model="Full-Sync",
            nodes=p.nodes,
            config=self.config,
            execution_core="ProvisionedCore(QuantumExecutionManager)",
            coordinator="ProvisionedCoordinator(CrossSimulatorEventCoordinator)",
            ns3_binary=str(self.binary),
            ns3_time_ns=p.now_ns,
            time_unit="ns",
            snapshot=snapshot,
            events=core.events,
            ns3_events=coordinator.rows,
            bridge_steps=coordinator.steps,
            native_instructions=core.instruction_events,
            instruction_state_recording=capture_states,
            q2ns_status=p.q2ns_status,
        )
        try:
            report["validation"] = validate_report(report)
            report["cross_validation"] = cross_validate(report, references)
            report["metrics"] = session_metrics(report)
            expected = report["validation"]["expected_timing"]["sessions"]
            for row in report["metrics"]["sessions"]:
                pred = expected[str(row["session_id"])]
                row.update(
                    resource_ready_ns=pred["resource_ready_ns"],
                    eligible_ns=pred["eligible_ns"],
                    resource_wait_ns=pred["resource_wait_ns"],
                )
            report["batch_completion_ns"] = max(
                r["completion_ns"] for r in report["metrics"]["sessions"]
            )
        except (ValueError, RuntimeError, KeyError) as error:
            report["validation"] = dict(passed=False, error=str(error))
            raise HybridValidationFailure(report, error) from error
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ns3-binary", type=Path)
    args = parser.parse_args()
    failure = None
    try:
        report = ProvisionedManager(json.loads(args.scenario.read_text()), args.ns3_binary).run()
    except HybridValidationFailure as error:
        report = error.report
        failure = error
    save_report(args.output, report)
    if failure:
        raise failure
    print(
        "Quantum provisioning: {} sessions; state error {:.3g}; PASS".format(
            len(report["config"]["sessions"]),
            report["cross_validation"]["max_density_matrix_error"],
        )
    )


if __name__ == "__main__":
    main()
