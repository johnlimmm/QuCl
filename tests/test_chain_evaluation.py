"""Complete-chain ablation boundaries, downstream contention and final-state metrics."""

import copy
import json
from pathlib import Path
import sys
import unittest

MODULE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE / "python"), str(MODULE / "experiments")]
from run_evaluation import workload
from chain_models import run_chain_model
from chain_analysis import (
    model_rows,
    decoupled,
    calibration_entry,
    PATHS,
    frozen_delays,
    validate_decoupled,
)
from chained_validation import validate_report
from run_chained import ChainedManager
from analysis import compare, estimate


class ChainEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads((MODULE / "experiments/profile.json").read_text())
        cls.config = workload(cls.plan, 13703, 0.75, 1000, True)
        cls.delays = dict(
            swap_packet_ns=108800, ready_packet_ns=207360, teleport_packet_ns=205120
        )
        cls.reports = {
            m: run_chain_model(cls.config, m, cls.delays)
            for m in ("Full-Sync", "No-Dq-R", "Fixed-Dc")
        }
        cls.zero = {
            m: run_chain_model(workload(cls.plan, 13703, 0.0, 1000, True), m, cls.delays)
            for m in ("Full-Sync", "No-Dq-R", "Fixed-Dc")
        }
        for config, reports in (
            (cls.config, cls.reports),
            (workload(cls.plan, 13703, 0.0, 1000, True), cls.zero),
        ):
            reports["Decoupled"] = run_chain_model(
                config, "Decoupled", frozen_delays(reports["No-Dq-R"])
            )

    def test_full_is_production_chain(self):
        r = ChainedManager(self.config).run(enumerate_branches=True)
        for field in ("snapshot", "metrics", "ns3_events", "cross_validation"):
            self.assertEqual(r[field], self.reports["Full-Sync"][field])

    def test_same_pair_and_native_final_state_in_every_model(self):
        for r in self.reports.values():
            self.assertTrue(r["validation"]["passed"])
            self.assertLess(r["cross_validation"]["max_density_matrix_error"], 1e-12)
            self.assertEqual(len(r["snapshot"]["handoffs"]), 4)
            self.assertTrue(all(h["same_qubit_objects"] for h in r["snapshot"]["handoffs"]))
            self.assertEqual(len(r["snapshot"]["quantum_network"]["deliveries"]), 8)
            self.assertTrue(
                all(
                    len(v["reference"]["branches"]) == 16
                    for v in r["cross_validation"]["chains"].values()
                )
            )

    def test_no_r_relaxes_only_r_and_keeps_downstream_fifo(self):
        r = self.zero["No-Dq-R"]
        rows = r["metrics"]["chains"]
        self.assertTrue(all(m["delays"]["swap_R_wait_ns"] == 0 for m in rows))
        self.assertTrue(any(m["delays"]["teleport_A_wait_ns"] > 0 for m in rows))
        with self.assertRaisesRegex(RuntimeError, "processor FIFO R"):
            validate_report(r)
        self.assertTrue(validate_report(r, r_capacity=4)["passed"])
        self.assertEqual(r["q2ns_status"]["native_qubit_count"], 0)

    def test_no_r_regenerates_entire_native_packet_path(self):
        r = self.reports["No-Dq-R"]
        rows = r["ns3_events"]
        for chain in r["config"]["chains"]:
            for sid in (chain["swap_session_id"], chain["teleport_session_id"]):
                req = next(
                    q
                    for q in r["snapshot"]["requests"]
                    if q["session_id"] == sid and q["operation"] == "BSM"
                )
                tx = next(
                    e for e in rows if e["session_id"] == sid and e["event_type"] == "RESULT_TX"
                )
                self.assertEqual(tx["time_ns"], req["completion_ns"])
        full = self.reports["Full-Sync"]["metrics"]["chains"]
        self.assertTrue(
            any(
                a["delays"][k] != b["delays"][k]
                for a, b in zip(full, r["metrics"]["chains"])
                for k in PATHS
            )
        )

    def test_fixed_uses_three_delays_and_no_packet_claim(self):
        r = self.reports["Fixed-Dc"]
        self.assertNotIn("ns3_events", r)
        self.assertNotIn("q2ns_status", r)
        for m in r["metrics"]["chains"]:
            self.assertEqual({k: m["delays"][k] for k in PATHS}, self.delays)
        self.assertTrue(any(m["delays"]["swap_R_wait_ns"] for m in r["metrics"]["chains"]))
        broken = copy.deepcopy(r)
        broken["fixed_delays"]["ready_packet_ns"] += 1
        with self.assertRaisesRegex(RuntimeError, "fixed path delay"):
            validate_report(broken, network=False)

    def test_final_teleport_expectation_not_swap_fidelity(self):
        for r in self.reports.values():
            for row in model_rows(r, 0.5, 5000000):
                ref = r["cross_validation"]["chains"][str(row["chain_id"])]["reference"]
                expected = sum(
                    b["probability"] * b["stages"][1]["checkpoints"]["usable"]["fidelity"]
                    for b in ref["branches"].values()
                )
                self.assertAlmostEqual(row["expected_fidelity"], expected, places=14)
                self.assertEqual(row["usable_fidelity"], row["output_fidelity"])
            altered = copy.deepcopy(r)
            for m in altered["metrics"]["chains"]:
                m["swap_fidelity"] = -100.0
            self.assertEqual(
                [m["expected_fidelity"] for m in model_rows(r, 0.5, 5000000)],
                [m["expected_fidelity"] for m in model_rows(altered, 0.5, 5000000)],
            )

    def test_zero_load_fixed_reproduces_full_final_states(self):
        a, b = self.zero["Full-Sync"], self.zero["Fixed-Dc"]
        self.assertEqual(a["metrics"], b["metrics"])
        self.assertEqual(a["snapshot"]["sessions"], b["snapshot"]["sessions"])
        self.assertEqual(calibration_entry(0.0, [a])["delays"], self.delays)

    def test_decoupled_native_matches_independent_timing_and_full_zero_load_state(self):
        full, nodq = self.zero["Full-Sync"], self.zero["No-Dq-R"]
        rows = decoupled(full["config"], nodq)
        self.assertEqual(
            [r["transaction_latency_ns"] for r in rows],
            [m["latency_ns"] for m in full["metrics"]["chains"]],
        )
        for r in rows:
            self.assertEqual(len(r["requests"]), 4)
            self.assertIsNone(r["usable_fidelity"])
        native = self.zero["Decoupled"]
        self.assertTrue(validate_decoupled(native, nodq)["passed"])
        self.assertEqual(native["snapshot"]["sessions"], full["snapshot"]["sessions"])
        errors = compare(
            model_rows(full, 0.5, 5000000), model_rows(native, 0.5, 5000000), {}, 5000000
        )
        self.assertTrue(all(abs(r["delta_expected_fidelity"]) < 1e-12 for r in errors))

    def test_decoupled_preserves_per_workflow_paths_and_measures_new_state(self):
        report, nodq = self.reports["Decoupled"], self.reports["No-Dq-R"]
        self.assertTrue(validate_decoupled(report, nodq)["passed"])
        self.assertNotIn("ns3_events", report)
        self.assertNotIn("q2ns_status", report)
        self.assertEqual(report["frozen_delays"], frozen_delays(nodq))
        self.assertTrue(
            any(m["delays"]["swap_R_wait_ns"] > 0 for m in report["metrics"]["chains"])
        )
        self.assertTrue(
            any(
                abs(a["expected_fidelity"] - b["expected_fidelity"]) > 1e-8
                for a, b in zip(
                    model_rows(report, 0.5, 5000000), model_rows(nodq, 0.5, 5000000)
                )
            )
        )

    def test_decoupled_rejects_changed_path_provenance_and_operation_times(self):
        report = copy.deepcopy(self.reports["Decoupled"])
        report["frozen_delays"]["1"]["ready_packet_ns"] += 1
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate_decoupled(report, self.reports["No-Dq-R"])
        with self.assertRaisesRegex(RuntimeError, "fixed path delay"):
            validate_report(report, network=False)
        report = copy.deepcopy(self.reports["Decoupled"])
        report["snapshot"]["requests"][0]["completion_ns"] += 1
        with self.assertRaisesRegex(ValueError, "timing replay"):
            validate_decoupled(report, self.reports["No-Dq-R"])

    def test_decoupled_rejects_invalid_workflow_delays(self):
        for delays in (
            {},
            {"1": dict(self.delays)},
            {str(i): dict(self.delays, swap_packet_ns=-1) for i in range(1, 5)},
        ):
            with self.assertRaises(ValueError):
                run_chain_model(self.config, "Decoupled", delays)

    def test_decoupled_reference_memoization_still_executes_native_states(self):
        from unittest.mock import patch
        from chain_models import REFERENCES, seed_reference_cache

        REFERENCES.clear()
        full = self.zero["Full-Sync"]
        seed_reference_cache(full)
        config = workload(self.plan, 13703, 0.0, 1000, True)
        with patch(
            "state_validation.run_reference",
            side_effect=AssertionError("reference was not reused"),
        ):
            report = run_chain_model(config, "Decoupled", frozen_delays(self.zero["No-Dq-R"]))
        self.assertEqual(report["snapshot"]["sessions"], full["snapshot"]["sessions"])
        self.assertEqual(report["reference_cache"]["exact_specification_hits"], 4)
        self.assertEqual(len(report["native_instructions"]), 48)

    def test_one_x_interval_removes_only_r_wait(self):
        c = workload(self.plan, 27405, 0.75, 1000, True)
        full = run_chain_model(c, enumerate_branches=False)
        nodq = run_chain_model(c, "No-Dq-R", enumerate_branches=False)
        replay = run_chain_model(c, "Decoupled", frozen_delays(nodq), enumerate_branches=False)
        self.assertTrue(validate_decoupled(replay, nodq)["passed"])
        self.assertEqual(full["metrics"], replay["metrics"])
        self.assertEqual(full["snapshot"]["sessions"], replay["snapshot"]["sessions"])
        self.assertEqual(full["metrics"], nodq["metrics"])
        self.assertTrue(
            all(r["delays"]["swap_R_wait_ns"] == 0 for r in full["metrics"]["chains"])
        )

    def test_corrupted_handoff_and_ready_delivery_rejected(self):
        for mutate, message in [
            (
                lambda r: r["snapshot"]["handoffs"][0].update(same_qubit_objects=False),
                "actual pair handoff",
            ),
            (
                lambda r: next(
                    e for e in r["ns3_events"] if e["event_type"] == "PAIR_READY_RX"
                ).update(time_ns=0),
                "application receive timing",
            ),
        ]:
            r = copy.deepcopy(self.reports["No-Dq-R"])
            mutate(r)
            with self.assertRaisesRegex(RuntimeError, message):
                validate_report(r, r_capacity=4)

    def test_downstream_b_contention_is_retained(self):
        cfg = copy.deepcopy(self.config)
        cfg["native_instructions"].update(x_ns=125000, z_ns=125000)
        for model in ("No-Dq-R", "Fixed-Dc"):
            r = run_chain_model(cfg, model, self.delays, enumerate_branches=False)
            self.assertTrue(
                any(
                    m["delays"]["swap_B_wait_ns"] or m["delays"]["teleport_B_wait_ns"]
                    for m in r["metrics"]["chains"]
                )
            )
            self.assertTrue(r["validation"]["passed"])
            self.assertLess(r["cross_validation"]["max_density_matrix_error"], 1e-12)

    def test_reference_cache_never_skips_native_state_validation(self):
        from unittest.mock import patch
        from state_validation import cross_validate_chained
        from chain_models import ReferenceCache

        cache = ReferenceCache()
        report = self.reports["Full-Sync"]
        expected = cross_validate_chained(report, True, cache)
        with patch("state_validation.run_reference", side_effect=AssertionError("cache miss")):
            self.assertEqual(cross_validate_chained(report, True, cache), expected)
            altered = copy.deepcopy(report)
            state = next(iter(altered["snapshot"]["sessions"].values()))
            state["checkpoints"]["usable"]["density_matrix"]["real"][0][0] += 0.1
            with self.assertRaisesRegex(RuntimeError, "reference state mismatch"):
                cross_validate_chained(altered, True, cache)
        altered = copy.deepcopy(report)
        altered["config"]["memory_noise"]["T2_ns"] = 5000000
        with patch(
            "state_validation.run_reference", side_effect=ValueError("new reference required")
        ):
            with self.assertRaisesRegex(ValueError, "new reference required"):
                cross_validate_chained(altered, True, cache)

    def test_six_inputs_are_not_six_independent_traffic_samples(self):
        rows = [
            dict(traffic_seed=seed, value=v)
            for seed, v in ((1000, -0.01), (1001, 0.02))
            for _ in range(24)
        ]
        e = estimate(rows, "value", 1000, absolute=True)
        self.assertEqual(e["traffic_clusters"], 2)
        self.assertAlmostEqual(e["mean"], 0.015)

    def test_decoupled_noiseless_six_inputs_all_reference_branches(self):
        for state in ("0", "1", "+", "-", "+i", "-i"):
            cfg = copy.deepcopy(self.config)
            cfg["memory_noise"].update(T1_ns=0, T2_ns=0)
            cfg["gate_depolar_probability"] = 0
            for chain in cfg["chains"]:
                chain["input_state"] = state
            report = run_chain_model(cfg, "Decoupled", frozen_delays(self.reports["No-Dq-R"]))
            self.assertTrue(validate_decoupled(report, self.reports["No-Dq-R"])["passed"])
            for ref in report["cross_validation"]["chains"].values():
                for branch in ref["reference"]["branches"].values():
                    self.assertAlmostEqual(
                        branch["stages"][1]["checkpoints"]["usable"]["fidelity"], 1.0, places=12
                    )


if __name__ == "__main__":
    unittest.main()
