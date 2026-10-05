"""Native delivery causality, readiness-gated FIFO, storage/channel noise separation."""

import copy
import json
import math
from pathlib import Path
import sys
import unittest
import numpy as np

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
from run_provisioned import ProvisionedManager, save_report
from runtime import HybridValidationFailure
from provisioned_config import normalize_config
from provisioned_validation import validate_report
from state_validation import cross_validate_provisioned as cross_validate
from provisioned_participant import ProvisionedParticipant
from protocol_adapters import STATES

OUT = MODULE / "results/tests/provisioned"


def scenario(name):
    return json.loads((MODULE / "scenarios" / (name + ".json")).read_text())


def matrix(checkpoint):
    d = checkpoint["density_matrix"]
    return np.array(d["real"]) + 1j * np.array(d["imag"])


def cp(report, sid=1):
    return report["snapshot"]["sessions"][str(sid)]["checkpoints"]


def zero_links():
    return {name: dict(delay_ns=0, depolar_rate_hz=0) for name in ("RA", "RB")}


def swap(start=1000000, sid=1):
    return dict(session_id=sid, protocol="swap", session_start_ns=start)


def tele(start=1000000, sid=1):
    return dict(session_id=sid, protocol="teleport", input_state="+i", session_start_ns=start)


class ProvisionedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = {}
        cls.mixed = ProvisionedManager(scenario("mixed")).run(cls.cache)
        save_report(OUT / "mixed.json.gz", cls.mixed)

    def run_case(self, raw):
        return ProvisionedManager(raw).run(self.cache)

    def test_01_real_network_delivery_and_single_state_owner(self):
        report = self.mixed
        network = report["snapshot"]["quantum_network"]
        self.assertEqual(set(network["nodes"]), {"A", "R", "B"})
        self.assertTrue(
            all(c["component"] == "QuantumChannel" for c in network["channels"].values())
        )
        self.assertEqual(len(network["deliveries"]), 6)
        self.assertTrue(
            all(d["placement_source"] == "quantum_channel_port" for d in network["deliveries"])
        )
        self.assertEqual(
            report["q2ns_status"],
            dict(native_state_count=0, native_qubit_count=0, sessions=4, corrections_applied=4),
        )

    def test_03_swap_waits_for_both_deliveries(self):
        report = self.run_case(
            dict(
                sessions=[swap()],
                quantum_links=dict(RA=dict(delay_ns=4000000), RB=dict(delay_ns=2000000)),
            )
        )
        m = report["metrics"]["sessions"][0]
        self.assertEqual(
            (m["resource_ready_ns"], m["resource_wait_ns"], m["bsm_start_ns"]),
            (4000000, 3000000, 4000000),
        )
        self.assertEqual(m["quantum_wait_ns"], 0)
        save_report(OUT / "late-resources.json.gz", report)

    def test_04_teleport_uses_only_rb(self):
        a = self.run_case(
            dict(
                sessions=[tele()],
                quantum_links=dict(RA=dict(delay_ns=9000000), RB=dict(delay_ns=400000)),
            )
        )
        self.assertEqual(a["metrics"]["sessions"][0]["bsm_start_ns"], 1000000)
        self.assertEqual(a["snapshot"]["quantum_network"]["deliveries"][0]["channel"], "RB")

    def test_05_ready_does_not_start_before_session(self):
        report = self.run_case(
            dict(sessions=[tele(4000000)], quantum_links=dict(RB=dict(delay_ns=200000)))
        )
        self.assertEqual(report["metrics"]["sessions"][0]["bsm_start_ns"], 4000000)
        self.assertEqual(report["metrics"]["sessions"][0]["resource_wait_ns"], 0)

    def test_06_readiness_reorders_fifo_without_head_blocking(self):
        report = self.run_case(
            dict(
                sessions=[swap(1000000, 1), tele(1500000, 2)],
                quantum_links=dict(RA=dict(delay_ns=4000000), RB=dict(delay_ns=500000)),
            )
        )
        starts = [e["session_id"] for e in report["events"] if e["event_type"] == "BSM_START"]
        self.assertEqual(starts, [2, 1])
        save_report(OUT / "ready-order.json.gz", report)

    def test_07_resource_delay_hidden_by_processor_wait(self):
        raw = dict(
            sessions=[tele(0, 1), swap(100000, 2)],
            quantum_links=dict(RA=dict(delay_ns=200000), RB=dict(delay_ns=0)),
        )
        a = self.run_case(raw)
        raw["quantum_links"]["RA"]["delay_ns"] = 1000000
        b = self.run_case(raw)
        ma, mb = a["metrics"]["sessions"][1], b["metrics"]["sessions"][1]
        self.assertEqual(ma["bsm_start_ns"], mb["bsm_start_ns"])
        self.assertEqual(
            ma["resource_wait_ns"] + ma["quantum_wait_ns"],
            mb["resource_wait_ns"] + mb["quantum_wait_ns"],
        )
        self.assertGreater(mb["resource_wait_ns"], ma["resource_wait_ns"])

    def test_08_same_time_readiness_and_start_order(self):
        for sessions in (
            [swap(1000000, 9), tele(1000000, 2)],
            [tele(1000000, 2), swap(1000000, 9)],
        ):
            for delay in (1000000, 2000000):
                report = self.run_case(
                    dict(
                        sessions=sessions,
                        quantum_links={k: dict(delay_ns=delay) for k in ("RA", "RB")},
                    )
                )
                starts = [
                    e["session_id"] for e in report["events"] if e["event_type"] == "BSM_START"
                ]
                self.assertEqual(starts, [s["session_id"] for s in sessions])
        # At t=1 ms: already-ready Teleport session start precedes newly-ready Swap.
        report = self.run_case(
            dict(
                sessions=[swap(1000000, 9), tele(1000000, 2)],
                quantum_links=dict(RA=dict(delay_ns=1000000), RB=dict(delay_ns=100000)),
            )
        )
        self.assertEqual(
            [e["session_id"] for e in report["events"] if e["event_type"] == "BSM_START"], [2, 9]
        )

    def test_09_classical_delay_isolation(self):
        raw = dict(
            sessions=[swap()],
            quantum_links=dict(RA=dict(delay_ns=1500000), RB=dict(delay_ns=2000000)),
        )
        a = self.run_case(raw)
        b = self.run_case(dict(raw, result_link=dict(rate_bps=10000000, delay_ns=5000000)))
        self.assertEqual(cp(a)["bsm_start"], cp(b)["bsm_start"])
        self.assertEqual(cp(a)["frame"], cp(b)["frame"])
        self.assertFalse(
            np.allclose(matrix(cp(a)["packet_arrival"]), matrix(cp(b)["packet_arrival"]))
        )

    def test_10_native_channel_depolarization_matches_analytic_bell_fidelity(self):
        rate = 100
        delay = 2000000
        report = self.run_case(
            dict(
                sessions=[swap()],
                memory_noise=dict(model="T1T2NoiseModel", T1_ns=0, T2_ns=0),
                quantum_links={k: dict(delay_ns=delay, depolar_rate_hz=rate) for k in ("RA", "RB")},
            )
        )
        expected = 1 - 0.75 * (1 - math.exp(-rate * delay / 1e9))
        for d in report["snapshot"]["quantum_network"]["deliveries"]:
            self.assertAlmostEqual(d["state"]["fidelity"], expected, places=12)
        save_report(OUT / "channel-noise.json.gz", report)

    def test_11_transit_has_no_destination_memory_aging(self):
        lifetime = 1000000
        delay = 1000000
        report = self.run_case(
            dict(
                sessions=[swap(2000000)],
                memory_noise=dict(model="T1T2NoiseModel", T1_ns=lifetime, T2_ns=500000),
                quantum_links=dict(RA=dict(delay_ns=delay), RB=dict(delay_ns=0)),
            )
        )
        d = next(
            d for d in report["snapshot"]["quantum_network"]["deliveries"] if d["channel"] == "RA"
        )
        decay = math.exp(-delay / lifetime)
        np.testing.assert_allclose(
            np.diag(matrix(d["state"])), [0.5, 0, 0.5 * (1 - decay), 0.5 * decay], atol=1e-12
        )

    def test_12_noiseless_all_inputs_all_branches(self):
        raw = dict(
            memory_noise=dict(model="T1T2NoiseModel", T1_ns=0, T2_ns=0),
            sessions=[
                dict(session_id=i + 1, protocol="teleport", input_state=state, session_start_ns=0)
                for i, state in enumerate(STATES)
            ],
        )
        branches = {state: set() for state in STATES}
        maximum = 0
        for seed in range(32):
            report = self.run_case(dict(raw, seed=seed))
            maximum = max(maximum, report["cross_validation"]["max_density_matrix_error"])
            for s in report["snapshot"]["sessions"].values():
                self.assertAlmostEqual(s["checkpoints"]["usable"]["fidelity"], 1, places=12)
                branches[s["input_state"]].add("".join(map(str, s["measurement_bits"])))
        self.assertTrue(all(v == {"00", "01", "10", "11"} for v in branches.values()))
        save_report(
            OUT / "branch-coverage.json",
            dict(
                passed=True,
                seeds=32,
                transactions=32 * len(STATES),
                branches={k: sorted(v) for k, v in branches.items()},
                max_density_matrix_error=maximum,
            ),
        )

    def test_13_mixed_b_contention(self):
        raw = scenario("mixed")
        raw["native_instructions"] = dict(cnot_ns=20000, h_ns=10000, measure_ns=10000)
        raw["background"] = {}
        raw["sessions"] = [swap(0, 1), tele(0, 2), swap(0, 3), tele(0, 4)]
        raw["quantum_links"] = {k: dict(delay_ns=1000000) for k in ("RA", "RB")}
        report = self.run_case(raw)
        self.assertTrue(any(r["correction_wait_ns"] > 0 for r in report["metrics"]["sessions"]))
        self.assertTrue(any(r["quantum_wait_ns"] > 0 for r in report["metrics"]["sessions"]))
        save_report(OUT / "mixed-fast-bsm.json.gz", report)

    def test_14_state_observation_invariance(self):
        raw = scenario("mixed")
        report = ProvisionedManager(raw).run(self.cache, capture_states=False)
        for s in raw["sessions"]:
            np.testing.assert_allclose(
                matrix(cp(report, s["session_id"])["usable"]),
                matrix(cp(self.mixed, s["session_id"])["usable"]),
                atol=1e-12,
            )

    def test_15_bad_delivery_trace_or_cache_rejected(self):
        for change in ("time", "resource", "cause"):
            bad = copy.deepcopy(self.mixed)
            if change == "time":
                bad["snapshot"]["quantum_network"]["deliveries"][0]["delivery_ns"] += 1
            elif change == "resource":
                bad["snapshot"]["resources"][0]["ready_ns"] += 1
            else:
                next(e for e in bad["events"] if e["event_type"] == "QCHANNEL_DELIVERED")[
                    "caused_by_event_id"
                ] = None
            with self.assertRaises(RuntimeError):
                validate_report(bad)
        cache = copy.deepcopy(self.cache)
        for r in cache.values():
            r["spec"]["quantum_links"]["RB"]["delay_ns"] += 1
        with self.assertRaises(RuntimeError):
            cross_validate(self.mixed, cache)

    def test_16_invalid_quantum_model_rejected(self):
        for fields in (
            dict(delay_ns=-1),
            dict(delay_ns=1.5),
            dict(depolar_rate_hz=float("nan")),
            dict(loss_probability=0.1),
        ):
            with self.assertRaises(ValueError):
                normalize_config(dict(quantum_links=dict(RB=fields)))
        with self.assertRaises(ValueError):
            normalize_config(dict(quantum_links=dict(AB={})))

    def test_17_duplicate_ready_notification_rejected(self):
        manager = ProvisionedManager(dict(sessions=[swap()]))
        p = ProvisionedParticipant(manager.binary, manager.config, {1: "p:0"})
        try:
            p.advance(0)
            p.notify_ready(0, [dict(session_id=1, cause="p:1")])
            with self.assertRaises(RuntimeError):
                p.notify_ready(0, [dict(session_id=1, cause="p:1")])
        finally:
            p.close()

    def test_18_background_drain_and_drop_rejection(self):
        report = self.run_case(
            dict(sessions=[tele()], background=dict(result=dict(start_ns=20000000, count=2)))
        )
        self.assertGreater(report["snapshot"]["time_ns"], report["batch_completion_ns"])
        with self.assertRaises(HybridValidationFailure) as caught:
            self.run_case(
                dict(
                    sessions=[swap()],
                    queue_packets=1,
                    background=dict(result=dict(start_ns=0, interval_ns=1, count=10)),
                )
            )
        self.assertIn("packet drop", str(caught.exception))
        save_report(OUT / "overflow-rejected.json.gz", caught.exception.report)

    def test_19_native_gate_delay_changes_actual_packet_time(self):
        raw = dict(sessions=[tele()])
        a = self.run_case(raw)
        b = self.run_case(dict(raw, native_instructions=dict(cnot_ns=1600000)))
        self.assertEqual(
            b["metrics"]["sessions"][0]["bsm_completion_ns"]
            - a["metrics"]["sessions"][0]["bsm_completion_ns"],
            1000000,
        )


if __name__ == "__main__":
    unittest.main()
