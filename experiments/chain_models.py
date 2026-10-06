"""Complete Swap--Teleport ablations with native states and physical pair handoff."""

from collections import OrderedDict
import json

from netsquid.components import QuantumProcessor
from netsquid.components.models.qerrormodels import T1T2NoiseModel
from native_programs import physical_instructions
from chained_core import (
    ChainedCore,
    ChainedSwapAdapter,
    ChainedTeleportAdapter,
    ChainedFederation,
)
from chained_participant import ChainedParticipant
from chained_validation import validate_report, metrics
from run_chained import ChainedManager
from quantum_network_backend import QuantumNetworkBackend
from gate_noise import install_gate_noise
from state_validation import cross_validate_chained, chained_reference_spec
from models import RMemoryView, FixedParticipant
from runtime import HybridValidationFailure


class ReferenceCache(OrderedDict):
    """Bound independent-reference memory; never substitute a prior coupled state."""

    hits = 0

    def __getitem__(self, key):
        self.hits += 1
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key not in self and len(self) >= 256:
            self.popitem(last=False)
        super().__setitem__(key, value)


REFERENCES = ReferenceCache()


def seed_reference_cache(report):
    """Memoize independently calculated references, never coupled native states.

    A hit requires the complete physical model, all operation/arrival times,
    input and observed bits to match. Every new native execution and checkpoint
    comparison still runs. Parent reports are hash-verified by the campaign.
    """
    if not report["cross_validation"]["passed"]:
        raise ValueError("cannot cache a failed independent reference")
    for chain in report["config"]["chains"]:
        ref = report["cross_validation"]["chains"][str(chain["chain_id"])]["reference"]
        if len(ref["branches"]) != 16:
            raise ValueError("complete independent branches required for cache")
        spec = chained_reference_spec(report, chain, True)
        REFERENCES[json.dumps(spec, sort_keys=True)] = ref


class ChainedNoRCore(ChainedCore):
    """Relax only R execution capacity; preserve shared A/B and every resource."""

    def create_quantum_network(self):
        c = self.config
        self.processors["R"]["capacity"] = len(c["chains"])
        self.r_devices, self.r_slot = [], {}
        for slot, chain in enumerate(c["chains"]):
            sid = chain["swap_session_id"]
            noise = c["memory_noise"]
            device = QuantumProcessor(
                "R-chain-" + str(chain["chain_id"]),
                num_positions=2,
                mem_noise_models=[
                    T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"]) for _ in range(2)
                ],
                phys_instructions=physical_instructions(c["native_instructions"], noise),
            )
            device.set_program_done_callback(self.program_done, "R", (sid, "BSM"), once=False)
            device.set_program_fail_callback(self.failed_replica, slot, once=False)
            self.r_slot[sid] = slot
            self.r_devices.append(device)
        self.devices["R"] = self.r_devices[0]
        network = QuantumNetworkBackend(self)
        for device in self.r_devices[1:]:
            network.nodes["R"].add_subcomponent(device)
        self.devices["R"] = RMemoryView(self.r_devices)
        return network

    def failed_replica(self, slot):
        raise RuntimeError(
            "native R replica failed: " + str(self.r_devices[slot].fail_exception)
        )

    def device_for(self, request):
        if request["processor_id"] == "R":
            return self.r_devices[self.r_slot[request["session_id"]]]
        return super().device_for(request)

    def target_positions(self, request):
        return [0, 1] if request["processor_id"] == "R" else super().target_positions(request)

    def snapshot(self):
        value = super().snapshot()
        value["quantum_network"]["nodes"]["R"]["execution_replicas"] = [
            dict(name=d.name, positions=d.num_positions) for d in self.r_devices
        ]
        return value


class FixedChainParticipant(FixedParticipant):
    """Three calibrated virtual paths; no simulated packet or Q2NS ownership claim."""

    def __init__(self, config, roots, delays):
        fields = {"swap_packet_ns", "ready_packet_ns", "teleport_packet_ns"}
        if (
            not isinstance(delays, dict)
            or set(delays) != fields
            or any(type(v) is not int or v < 0 for v in delays.values())
        ):
            raise ValueError("three nonnegative integer chain path delays required")
        self.now_ns, self.sequence = 0, 0
        self.future = []
        self.delays = delays
        self.config = config
        self.started, self.ready, self.issued = set(), set(), set()
        self.swap_ids = {c["swap_session_id"] for c in config["chains"]}
        for s in config["sessions"]:
            self.schedule(
                s["session_start_ns"], "SESSION_START", s["session_id"], roots[s["session_id"]]
            )

    def schedule(self, at, kind, sid, cause, bits=None):
        event_id = "fixed:" + str(self.sequence)
        super().schedule(at, kind, sid, cause, bits)
        return event_id

    def advance(self, at):
        rows = []
        while self.next_time == at:
            for row in super().advance(at):
                rows.append(row)
                sid, kind = row["session_id"], row["event_type"]
                if kind == "SESSION_START":
                    self.started.add(sid)
                elif kind in ("FIXED_RESOURCES_READY", "PAIR_READY_RX"):
                    self.ready.add(sid)
                else:
                    continue
                if sid in self.started and sid in self.ready and sid not in self.issued:
                    self.issued.add(sid)
                    self.schedule(at, "BSM_REQUEST", sid, row["event_id"])
        self.now_ns = at
        return rows

    def notify_ready(self, at, ready):
        for row in ready:
            sid = row["session_id"]
            if sid not in self.swap_ids or sid in self.ready:
                raise RuntimeError("invalid elementary-pair readiness")
            self.schedule(at, "FIXED_RESOURCES_READY", sid, row["cause"])

    def inject(self, at, completed):
        for r in completed:
            sid = r["session_id"]
            if r["operation"] == "BSM":
                bits = r["measurement_bits"]
                tx = self.schedule(at, "RESULT_TX", sid, r["cause"], bits)
                field = "swap_packet_ns" if sid in self.swap_ids else "teleport_packet_ns"
                arrival = at + self.path_delay(sid, field)
                rx = self.schedule(arrival, "RESULT_RX", sid, tx, bits)
                self.schedule(arrival, "CORRECTION_REQUEST", sid, rx, bits)
            elif sid in self.swap_ids:
                tele = sid + 1
                tx = self.schedule(at, "PAIR_READY_TX", tele, r["cause"])
                self.schedule(
                    at + self.path_delay(sid, "ready_packet_ns"), "PAIR_READY_RX", tele, tx
                )

    def path_delay(self, sid, field):
        return self.delays[field]


class DecoupledChainParticipant(FixedChainParticipant):
    """Replay each workflow's measured paths without packet-timing feedback."""

    def __init__(self, config, roots, delays):
        from chain_analysis import PATHS

        expected = {str(c["chain_id"]) for c in config["chains"]}
        if not isinstance(delays, dict) or set(delays) != expected:
            raise ValueError("one frozen delay set per workflow required")
        for own in delays.values():
            if (
                not isinstance(own, dict)
                or set(own) != set(PATHS)
                or any(type(v) is not int or v < 0 for v in own.values())
            ):
                raise ValueError("three nonnegative integer frozen path delays required")
        super().__init__(config, roots, next(iter(delays.values())))
        self.frozen = delays
        self.chain_by_session = {
            sid: str(c["chain_id"])
            for c in config["chains"]
            for sid in (c["swap_session_id"], c["teleport_session_id"])
        }

    def path_delay(self, sid, field):
        return self.frozen[self.chain_by_session[sid]][field]


def run_chain_model(raw, model="Full-Sync", delays=None, enumerate_branches=True):
    if model not in ("Full-Sync", "No-Dq-R", "Fixed-Dc", "Decoupled"):
        raise ValueError("unknown chain model")
    manager = ChainedManager(raw)
    if model == "Full-Sync":
        result = manager.run(reference=False)
        result["cross_validation"] = cross_validate_chained(
            result, enumerate_branches, REFERENCES
        )
        result["model"] = model
        return result
    c = manager.config
    core = (ChainedNoRCore if model == "No-Dq-R" else ChainedCore)(c)
    install_gate_noise(core)
    for slot, chain in enumerate(c["chains"]):
        swap, tele = c["sessions"][2 * slot : 2 * slot + 2]
        core.adapters[swap["session_id"]] = ChainedSwapAdapter(core, swap, slot)
        core.adapters[tele["session_id"]] = ChainedTeleportAdapter(core, tele, slot)
    network = model in ("Full-Sync", "No-Dq-R")
    p = (
        ChainedParticipant(manager.binary, c, core.roots)
        if network
        else (DecoupledChainParticipant if model == "Decoupled" else FixedChainParticipant)(
            c, core.roots, delays
        )
    )
    federation = ChainedFederation(core, p)
    try:
        snapshot = federation.run()
        if network:
            p.finish()
    finally:
        if network:
            p.close()
    report = dict(
        schema_version=5,
        architecture="swapping-assisted-teleportation-v5",
        model=model,
        config=c,
        time_unit="ns",
        snapshot=snapshot,
        events=core.events,
        bridge_steps=federation.steps,
        native_instructions=core.instruction_events,
    )
    if network:
        report.update(
            nodes=p.nodes,
            ns3_time_ns=p.now_ns,
            ns3_events=federation.rows,
            q2ns_status=p.q2ns_status,
        )
    else:
        for row in report["events"]:
            if row["source"] == "ns3":
                row["source"] = "decoupled" if model == "Decoupled" else "fixed-delay"
        report.update(
            transport_events=federation.rows,
            transport=(
                "per-workflow frozen No-R paths; no packet feedback"
                if model == "Decoupled"
                else "calibrated virtual Swap/ready/Teleport delivery; no packet network"
            ),
        )
        report["frozen_delays" if model == "Decoupled" else "fixed_delays"] = delays
    try:
        report["validation"] = validate_report(
            report, network=network, r_capacity=len(c["chains"]) if model == "No-Dq-R" else 1
        )
        report["metrics"] = metrics(report)
        prior_hits = REFERENCES.hits
        report["cross_validation"] = cross_validate_chained(
            report, enumerate_branches, REFERENCES
        )
        if model == "Decoupled":
            report["reference_cache"] = dict(
                exact_specification_hits=REFERENCES.hits - prior_hits,
                chains=len(c["chains"]),
                method="Memoized independent reference only when full specifications match; native execution and all state comparisons always run.",
            )
    except (ValueError, RuntimeError, KeyError) as error:
        report["validation"] = dict(passed=False, error=str(error))
        raise HybridValidationFailure(report, error) from error
    return report
