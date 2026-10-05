"""Optional native gate noise must preserve timing and match an independent circuit."""

import json
from pathlib import Path
import sys
import unittest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
from gate_noise import split_config
from run_chained import ChainedManager
from run_provisioned import ProvisionedManager


class GateNoiseTests(unittest.TestCase):
    def test_chained_gate_noise_changes_state_not_packet_timing(self):
        raw = json.loads((MODULE / "scenarios/chain.json").read_text())
        clean = ChainedManager(raw).run()
        noisy = ChainedManager(dict(raw, gate_depolar_probability=0.01)).run(
            enumerate_branches=True
        )
        schedule = lambda r: [
            (e["event_type"], e["time_ns"], e["session_id"]) for e in r["ns3_events"]
        ]
        self.assertEqual(schedule(clean), schedule(noisy))
        self.assertEqual(
            clean["metrics"]["chains"][0]["completion_ns"],
            noisy["metrics"]["chains"][0]["completion_ns"],
        )
        self.assertLess(noisy["cross_validation"]["max_density_matrix_error"], 1e-10)
        self.assertNotAlmostEqual(
            clean["metrics"]["chains"][0]["output_fidelity"],
            noisy["metrics"]["chains"][0]["output_fidelity"],
            places=8,
        )

    def test_mixed_native_noise_matches_reference(self):
        raw = json.loads((MODULE / "scenarios/mixed.json").read_text())
        raw["gate_depolar_probability"] = 0.03
        report = ProvisionedManager(raw).run()
        self.assertLess(report["cross_validation"]["max_density_matrix_error"], 1e-12)
        self.assertEqual(report["q2ns_status"]["corrections_applied"], 4)

    def test_invalid_noise_rejected_before_simulation(self):
        for raw in (
            [],
            None,
            {"gate_depolar_probability": -0.01},
            {"gate_depolar_probability": 1.1},
            {"gate_depolar_probability": True},
            {"gate_depolar_probability": float("nan")},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                split_config(raw)


if __name__ == "__main__":
    unittest.main()
