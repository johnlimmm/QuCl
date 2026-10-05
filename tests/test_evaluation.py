"""Ablation semantics, statistical units and campaign input contracts."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
sys.path.insert(0, str(MODULE / "experiments"))
from analysis import estimate, model_rows, compare, calibration_entry, chain_cells, DELAY_FIELDS
from models import run_model
from run_evaluation import workload, validate_plan, evaluate, execute_case
from timing import decoupled
from provisioned_validation import validate_report
from run_provisioned import ProvisionedManager
from runtime import save_report


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (MODULE / "results").mkdir(exist_ok=True)
        cls.plan = json.loads((MODULE / "experiments/profile.json").read_text())
        cls.config = workload(cls.plan, 13703, 0.75, 1000)
        cls.reports = {
            model: run_model(cls.config, model, {}, dict(result_ns=108800))
            for model in ("Full-Sync", "No-Dq-R", "Fixed-Dc")
        }
        cls.zero = {
            model: run_model(
                workload(cls.plan, 13703, 0.0, 1000), model, {}, dict(result_ns=108800)
            )
            for model in ("Full-Sync", "No-Dq-R", "Fixed-Dc")
        }

    def test_full_sync_is_production_execution(self):
        production = ProvisionedManager(self.config).run()
        full = self.reports["Full-Sync"]
        self.assertEqual(production["snapshot"], full["snapshot"])
        self.assertEqual(production["ns3_events"], full["ns3_events"])
        self.assertEqual(production["metrics"], full["metrics"])

    def test_models_use_native_state_reference(self):
        for report in self.reports.values():
            self.assertTrue(report["validation"]["passed"])
            self.assertLess(report["cross_validation"]["max_density_matrix_error"], 1e-12)
            for row in report["metrics"]["sessions"]:
                self.assertEqual(row["correction_wait_ns"], 0)

    def test_no_r_wait_regenerates_actual_packets(self):
        full = self.reports["Full-Sync"]["metrics"]["sessions"]
        relaxed = self.reports["No-Dq-R"]["metrics"]["sessions"]
        self.assertTrue(any(r["quantum_wait_ns"] > 0 for r in full))
        self.assertTrue(all(r["quantum_wait_ns"] == 0 for r in relaxed))
        self.assertTrue(
            any(a["result_delay_ns"] != b["result_delay_ns"] for a, b in zip(full, relaxed))
        )
        report = self.reports["No-Dq-R"]
        self.assertEqual(report["q2ns_status"]["native_qubit_count"], 0)
        for row in relaxed:
            tx = [
                e
                for e in report["ns3_events"]
                if e["event_type"] == "RESULT_TX" and e["session_id"] == row["session_id"]
            ]
            self.assertEqual(tx[0]["time_ns"], row["bsm_completion_ns"])

    def test_relaxed_capacity_must_be_explicit(self):
        report = self.reports["No-Dq-R"]
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            validate_report(report, report["validation"]["expected_timing"])

    def test_fixed_delay_has_shared_fifo_without_packet_claim(self):
        report = self.reports["Fixed-Dc"]
        self.assertNotIn("ns3_events", report)
        self.assertNotIn("q2ns_status", report)
        self.assertTrue(all(r["result_delay_ns"] == 108800 for r in report["metrics"]["sessions"]))
        self.assertTrue(any(r["quantum_wait_ns"] for r in report["metrics"]["sessions"]))

    def test_decoupled_error_is_changed_result_delay(self):
        report = self.reports["Full-Sync"]
        relaxed = self.reports["No-Dq-R"]
        rows = decoupled(report["config"], relaxed)
        for full, nodq, predicted in zip(
            report["metrics"]["sessions"], relaxed["metrics"]["sessions"], rows
        ):
            self.assertEqual(
                predicted["transaction_latency_ns"] - full["transaction_latency_ns"],
                nodq["result_delay_ns"] - full["result_delay_ns"],
            )
            self.assertIsNone(predicted["usable_fidelity"])

    def test_zero_load_controls(self):
        full = self.zero["Full-Sync"]
        for a, b in zip(full["metrics"]["sessions"], self.zero["Fixed-Dc"]["metrics"]["sessions"]):
            # A virtual fixed delay has no measured packet queue occupancy.
            self.assertEqual(
                {k: v for k, v in a.items() if k != "result_queue_ns"},
                {k: v for k, v in b.items() if k != "result_queue_ns"},
            )
        rows = decoupled(full["config"], self.zero["No-Dq-R"])
        self.assertEqual(
            [r["transaction_latency_ns"] for r in rows],
            [r["transaction_latency_ns"] for r in full["metrics"]["sessions"]],
        )

    def test_b_contention_rejected(self):
        config = copy.deepcopy(self.config)
        config["native_instructions"].update(x_ns=125000, z_ns=125000)
        with self.assertRaisesRegex(ValueError, "B wait"):
            run_model(config, "No-Dq-R", {})

    def test_packet_corruption_rejected(self):
        report = copy.deepcopy(self.reports["No-Dq-R"])
        next(e for e in report["ns3_events"] if e["event_type"] == "RESULT_RX")["time_ns"] += 1
        with self.assertRaisesRegex(RuntimeError, "packet timing"):
            validate_report(report, report["validation"]["expected_timing"], r_capacity=4)

    def test_branch_expectation_separate_from_sample(self):
        report = self.reports["Full-Sync"]
        for row in model_rows(report, 0.5, 5000000):
            ref = report["cross_validation"]["sessions"][str(row["session_id"])]["reference"]
            expectation = sum(
                b["probability"] * b["checkpoints"]["usable"]["fidelity"]
                for b in ref["branches"].values()
            )
            self.assertAlmostEqual(expectation, row["expected_fidelity"], places=14)
        full = model_rows(report, 0.5, 5000000)
        predicted = decoupled(report["config"], self.reports["No-Dq-R"])
        self.assertTrue(
            all(r["delta_expected_fidelity"] is None for r in compare(full, predicted, {}, 5000000))
        )

    def test_bootstrap_resamples_phases_not_transactions(self):
        rows = [dict(traffic_seed=1, value=v) for v in (0.0, 2.0)]
        rows += [dict(traffic_seed=2, value=v) for v in (2.0, 4.0)]
        first = estimate(rows, "value", 1000)
        self.assertEqual(first, estimate(rows * 6, "value", 1000))
        self.assertEqual(first["traffic_clusters"], 2)
        self.assertEqual(first["mean"], 2.0)
        self.assertIsNone(estimate(rows[:2], "value", 1000)["ci95_cluster_bootstrap"])

    def test_balanced_inputs_required(self):
        plan = dict(self.plan, classical_loads=[0.0], request_intervals_ns=[54810], sessions=1)
        rows = [
            dict(
                {k: 0 for k in DELAY_FIELDS},
                classical_load=0.0,
                request_interval_ns=54810,
                traffic_seed=1000,
                chain_id=1,
                input_state=s,
                latency_ns=0,
                expected_fidelity=0.7,
            )
            for s in plan["input_states"]
        ]
        self.assertEqual(chain_cells(rows, plan)[0]["traffic_phases"], 1)
        with self.assertRaisesRegex(ValueError, "missing"):
            chain_cells(rows[:-1], plan)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            chain_cells(rows + rows[:1], plan)

    def test_invalid_plan_and_calibration_leakage(self):
        validate_plan(copy.deepcopy(self.plan))
        for field, value in [
            ("test_traffic_seeds", [100]),
            ("classical_loads", [0.0, 1.0]),
            ("request_intervals_ns", [13703, 27405, 54000]),
        ]:
            plan = copy.deepcopy(self.plan)
            plan[field] = value
            with self.assertRaises(ValueError):
                validate_plan(plan)
        cal = calibration_entry(0.0, [self.zero["Full-Sync"]])
        self.assertEqual(cal["delays"], dict(result_ns=108800))

    def test_completed_run_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory(dir=str(MODULE / "results")) as directory:
            path = Path(directory)
            save_report(path / "summary.json", {"sentinel": True})
            before = (path / "summary.json").read_bytes()
            for resume in (True, False):
                with self.assertRaisesRegex(ValueError, "new result directory"):
                    evaluate(copy.deepcopy(self.plan), path, resume)
            self.assertEqual(before, (path / "summary.json").read_bytes())

    def test_spawned_condition_matches_sequential(self):
        plan = copy.deepcopy(self.plan)
        plan["sessions"] = 1
        with tempfile.TemporaryDirectory(dir=str(MODULE / "results")) as directory:
            root = Path(directory)
            task = (plan, root / "sequential", 0.75, 13703, 1000, dict(result_ns=108800), False)
            sequential = execute_case(task)
            task = (plan, root / "spawned") + task[2:]
            with ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                spawned = pool.submit(execute_case, task).result(timeout=120)
            self.assertEqual(sequential, spawned)


if __name__ == "__main__":
    unittest.main()
