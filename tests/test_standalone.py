"""Actual standalone execution, model boundaries and unchanged coupled behavior."""

import copy
import json
from pathlib import Path
import sys
import unittest

MODULE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE / "python"), str(MODULE / "experiments")]
from standalone_netsquid import run_netsquid, unloaded_delays
from standalone_q2ns import run_q2ns, validate_native
from chain_models import run_chain_model
from run_chained import ChainedManager
from run_evaluation import workload


class StandaloneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads((MODULE / "experiments/chain-profile.json").read_text())
        cls.cfg = workload(cls.plan, 13703, 0.75, 1000, True)
        cls.native_cfg = copy.deepcopy(cls.cfg)
        cls.native_cfg["memory_noise"].update(T1_ns=0, T2_ns=0)
        cls.native_cfg["gate_depolar_probability"] = 0
        cls.q2ns = run_q2ns(cls.native_cfg)
        cls.ns = run_netsquid(cls.cfg, reference=True, enumerate_branches=True)

    def test_native_channels_match_fixed_delay_model(self):
        fixed = run_chain_model(self.cfg, "Fixed-Dc", self.ns["fixed_delays"])
        self.assertEqual(self.ns["metrics"], fixed["metrics"])
        self.assertEqual(self.ns["snapshot"]["sessions"], fixed["snapshot"]["sessions"])
        self.assertEqual(
            [
                {k: v for k, v in r.items() if k != "cause"}
                for r in self.ns["snapshot"]["requests"]
            ],
            [
                {k: v for k, v in r.items() if k != "cause"}
                for r in fixed["snapshot"]["requests"]
            ],
        )
        self.assertLess(self.ns["cross_validation"]["max_density_matrix_error"], 1e-12)

    def test_native_channel_hops_and_payload_gating(self):
        self.assertNotIn("bridge_steps", self.ns)
        self.assertNotIn("ns3_events", self.ns)
        self.assertEqual(self.ns["ipc_messages"], 0)
        hops = self.ns["classical_channel_events"]
        self.assertEqual(len(hops), 10 * len(self.cfg["chains"]))
        for tx in [r for r in hops if r["event"] == "TX"]:
            # Native routes have one delivery per path/hop/session.
            match = [
                r
                for r in hops
                if r["event"] == "RX"
                and all(r[k] == tx[k] for k in ("path", "hop", "session_id"))
            ]
            self.assertEqual(len(match), 1)
            self.assertEqual(match[0]["time_ns"] - tx["time_ns"], tx["delay_ns"])

    def test_zero_delay_and_equal_time_native_events(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["background"]["result"]["count"] = 0
        for link in cfg["quantum_links"].values():
            link["delay_ns"] = 0
        for ch in cfg["chains"]:
            ch["session_start_ns"] = 0
        delays = dict(swap_packet_ns=0, ready_packet_ns=0, teleport_packet_ns=0)
        a = run_netsquid(cfg, delays)
        b = run_chain_model(cfg, "Fixed-Dc", delays, enumerate_branches=False)
        self.assertEqual(a["metrics"], b["metrics"])
        self.assertEqual(a["snapshot"]["sessions"], b["snapshot"]["sessions"])

    def test_native_q2ns_actual_state_and_instant_execution(self):
        self.assertEqual(self.q2ns["quantum_state_owner"], "Q2NS-QStateDM")
        self.assertGreater(self.q2ns["status"]["native_states"], 0)
        self.assertEqual(self.q2ns["ipc_messages"], 0)
        self.assertNotIn("snapshot", self.q2ns)  # no fabricated NetSquid snapshot
        self.assertLess(self.q2ns["validation"]["max_density_matrix_error"], 1e-12)

    def test_native_all_inputs_and_joint_branches(self):
        cfg = json.loads((MODULE / "scenarios/chain-noiseless.json").read_text())
        branches = {ch["input_state"]: set() for ch in cfg["chains"]}
        for seed in range(128):
            cfg["seed"] = seed
            r = run_q2ns(cfg)
            for m in r["metrics"].values():
                branches[m["input_state"]].add(m["joint_branch"])
                self.assertAlmostEqual(m["output_fidelity"], 1, places=12)
            if all(len(v) == 16 for v in branches.values()):
                break
        self.assertTrue(all(len(v) == 16 for v in branches.values()), branches)

    def test_q2ns_rejects_unimplemented_noise(self):
        for field in ("memory", "gate", "channel"):
            cfg = copy.deepcopy(self.native_cfg)
            if field == "memory":
                cfg["memory_noise"].update(T1_ns=20000000, T2_ns=10000000)
            elif field == "gate":
                cfg["gate_depolar_probability"] = 0.01
            else:
                cfg["quantum_links"]["RA"]["depolar_rate_hz"] = 1
            with self.assertRaisesRegex(ValueError, "noise-off"):
                run_q2ns(cfg)

    def test_native_zero_start_waits_for_delivery(self):
        cfg = copy.deepcopy(self.native_cfg)
        for ch in cfg["chains"]:
            ch["session_start_ns"] = 0
        r = run_q2ns(cfg)
        self.assertTrue(r["validation"]["passed"])
        self.assertTrue(
            all(
                e["time_ns"] == 1200000
                for e in r["ns3_events"]
                if e["event_type"] == "BSM_REQUEST" and e["node_id"] == 1
            )
        )

    def test_no_contention_still_has_intrinsic_timing_difference(self):
        cfg = json.loads((MODULE / "scenarios/chain-noiseless.json").read_text())
        cfg["chains"] = cfg["chains"][:1]
        native, full = run_q2ns(cfg), ChainedManager(cfg).run()
        difference = (
            full["metrics"]["chains"][0]["latency_ns"] - native["metrics"]["1"]["latency_ns"]
        )
        self.assertEqual(
            difference,
            2 * (full["config"]["bsm_duration_ns"] + full["config"]["correction_duration_ns"]),
        )

    def test_native_corrupted_handoff_is_rejected(self):
        r = copy.deepcopy(self.q2ns)
        next(e for e in r["native_resources"] if e["event"] == "HANDOFF")["qubit_ids"][0] += 100
        with self.assertRaisesRegex(RuntimeError, "physical pair"):
            validate_native(r)

    def test_native_corrupted_packet_is_rejected(self):
        r = copy.deepcopy(self.q2ns)
        next(e for e in r["ns3_events"] if e["event_type"] == "PHY_RX")["time_ns"] += 1
        with self.assertRaisesRegex(RuntimeError, "packet hop timing"):
            validate_native(r)

    def test_native_overflow_fails(self):
        cfg = copy.deepcopy(self.native_cfg)
        cfg["queue_packets"] = 1
        cfg["background"]["result"].update(start_ns=1900000, interval_ns=1, count=10)
        with self.assertRaisesRegex(RuntimeError, "packet drop|incomplete native chain"):
            run_q2ns(cfg)


if __name__ == "__main__":
    unittest.main()
