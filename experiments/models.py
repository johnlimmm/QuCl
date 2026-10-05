"""Controlled ablations of R capacity and classical result delivery.

Full Sync uses the public production core. No R contention gives each session
its own native R execution engine with the same memory positions and physics.
Fixed delay replaces only classical result transport; it retains shared R/B
processors and native quantum-channel delivery.
"""

import heapq
import time
from netsquid.components import QuantumProcessor
from netsquid.components.models.qerrormodels import T1T2NoiseModel
from native_core import NativeExecutionCore
from native_programs import physical_instructions
from provisioned_core import (
    ProvisionedCore,
    ProvisionedFederation,
    ProvisionedSwapAdapter,
    ProvisionedTeleportAdapter,
)
from provisioned_participant import ProvisionedParticipant
from quantum_network_backend import QuantumNetworkBackend
from provisioned_validation import validate_report
from state_validation import cross_validate_provisioned as cross_validate
from gate_noise import split_config, install_gate_noise
from run_provisioned import ProvisionedManager
from runtime import HybridValidationFailure, session_metrics
from timing import variant_timing
from analysis import model_rows


class RMemoryView:
    """Keep logical R positions while each session has its own execution engine.

    There are still exactly two R memory positions per session, with one state
    owner. Only simultaneous instruction execution capacity changes.
    """

    def __init__(self, devices):
        self.devices = devices

    def put(self, qubit, positions):
        self.devices[positions // 2].put(qubit, positions=positions % 2)

    def peek(self, position):
        return self.devices[position // 2].peek(position % 2)

    def pop(self, positions):
        return [self.devices[p // 2].pop(p % 2)[0] for p in positions]


class NativeNoDqCore(NativeExecutionCore):
    def __init__(self, config, capture_states=True):
        super().__init__(config, capture_states)
        self.r_capacity = len(config["sessions"])
        self.processors["R"]["capacity"] = self.r_capacity
        self.r_devices, self.r_slot = [], {}
        for slot, session in enumerate(config["sessions"]):
            sid = session["session_id"]
            noise = config["memory_noise"]
            device = QuantumProcessor(
                "R-evaluation-" + str(sid),
                num_positions=2,
                mem_noise_models=[
                    T1T2NoiseModel(T1=noise["T1_ns"], T2=noise["T2_ns"]) for _ in range(2)
                ],
                phys_instructions=physical_instructions(config["native_instructions"], noise),
            )
            device.set_program_done_callback(self.program_done, "R", (sid, "BSM"), once=False)
            device.set_program_fail_callback(self.failed_replica, slot, once=False)
            self.r_slot[sid] = slot
            self.r_devices.append(device)
        self.devices["R"] = RMemoryView(self.r_devices)

    def failed_replica(self, slot):
        raise RuntimeError(
            "native R evaluation program failed: " + str(self.r_devices[slot].fail_exception)
        )

    def device_for(self, request):
        return (
            self.r_devices[self.r_slot[request["session_id"]]]
            if request["processor_id"] == "R"
            else super().device_for(request)
        )

    def target_positions(self, request):
        return [0, 1] if request["processor_id"] == "R" else super().target_positions(request)


class FixedParticipant:
    """Virtual result delivery only; explicitly not an ns-3/Q2NS packet simulation."""

    def __init__(self, config, roots, delay):
        self.now_ns = 0
        self.future = []
        self.sequence = 0
        self.delay = delay
        for s in config["sessions"]:
            self.schedule(
                s["session_start_ns"], "BSM_REQUEST", s["session_id"], roots[s["session_id"]]
            )

    @property
    def next_time(self):
        return self.future[0][0] if self.future else None

    def schedule(self, at, kind, sid, cause, bits=None):
        sequence = self.sequence
        self.sequence += 1
        heapq.heappush(
            self.future,
            (
                at,
                sequence,
                dict(
                    event_id="fixed:" + str(sequence),
                    time_ns=at,
                    event_type=kind,
                    session_id=sid,
                    cause=cause,
                    m1=None if bits is None else bits[0],
                    m2=None if bits is None else bits[1],
                ),
            ),
        )

    def advance(self, at):
        self.now_ns = at
        rows = []
        while self.future and self.future[0][0] == at:
            rows.append(heapq.heappop(self.future)[2])
        return rows

    def inject(self, at, completed):
        for r in completed:
            if r["operation"] == "BSM":
                self.schedule(
                    at + self.delay,
                    "CORRECTION_REQUEST",
                    r["session_id"],
                    r["cause"],
                    r["measurement_bits"],
                )


class ProvisionedNoDqCore(NativeNoDqCore):
    def __init__(self, config, capture_states=True):
        super().__init__(config, capture_states)
        self.pending_ready = []
        # Node.qmemory must be a real QuantumMemory, not the logical position view.
        # The first replica is qmemory; the other physical replicas are child components.
        view = self.devices["R"]
        self.devices["R"] = self.r_devices[0]
        self.quantum_network = QuantumNetworkBackend(self)
        self.devices["R"] = view
        for replica in self.r_devices[1:]:
            self.quantum_network.nodes["R"].add_subcomponent(replica)

    def snapshot(self):
        value = super().snapshot()
        if self.pending_ready:
            raise RuntimeError("resource notification not delivered")
        value["quantum_network"] = self.quantum_network.snapshot()
        value["quantum_network"]["nodes"]["R"]["execution_replicas"] = [
            dict(name=device.name, positions=device.num_positions) for device in self.r_devices
        ]
        return value


class ProvisionedFixedParticipant(FixedParticipant):
    """Virtual result transport with actual native resource-ready callbacks."""

    def __init__(self, config, roots, delay):
        self.now_ns = 0
        self.future = []
        self.sequence = 0
        self.delay = delay
        self.started = set()
        self.ready = set()
        self.issued = set()
        for session in config["sessions"]:
            sid = session["session_id"]
            self.schedule(session["session_start_ns"], "SESSION_START", sid, roots[sid])

    def advance(self, at):
        rows = []
        while self.next_time == at:
            batch = super().advance(at)
            rows.extend(batch)
            for row in batch:
                sid = row["session_id"]
                kind = row["event_type"]
                if kind == "SESSION_START":
                    self.started.add(sid)
                elif kind == "FIXED_RESOURCES_READY":
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
            if sid in self.ready:
                raise RuntimeError("duplicate fixed resource readiness")
            self.schedule(at, "FIXED_RESOURCES_READY", sid, row["cause"])


def execute(
    raw,
    model="Full-Sync",
    references=None,
    delays=None,
    participant_class=ProvisionedParticipant,
    require_no_b_wait=True,
):
    raw, probability = split_config(raw)
    manager = ProvisionedManager(raw)
    config = manager.config
    config["gate_depolar_probability"] = probability
    expected = variant_timing(config, model, delays)
    if require_no_b_wait and any(s["correction_wait_ns"] for s in expected["sessions"].values()):
        raise ValueError(model + ": workload violates B wait=0")
    t0 = time.perf_counter()
    core = (ProvisionedNoDqCore if model == "No-Dq-R" else ProvisionedCore)(config)
    install_gate_noise(core)
    for slot, session in enumerate(config["sessions"]):
        core.adapters[session["session_id"]] = (
            ProvisionedSwapAdapter if session["protocol"] == "swap" else ProvisionedTeleportAdapter
        )(core, session, slot)
    t1 = time.perf_counter()
    network = model != "Fixed-Dc"
    participant = (
        participant_class(manager.binary, config, core.roots)
        if network
        else ProvisionedFixedParticipant(config, core.roots, delays["result_ns"])
    )
    federation = ProvisionedFederation(core, participant)
    t2 = time.perf_counter()
    try:
        snapshot = federation.run()
        t3 = time.perf_counter()
        if network:
            participant.finish()
    finally:
        if network:
            participant.close()
    t4 = time.perf_counter()
    report = dict(
        schema_version=4,
        architecture="provisioned-native-v4",
        model=model,
        config=config,
        execution_core=(
            "ProvisionedCore(NativeExecutionCore)" if model != "No-Dq-R" else "ProvisionedNoDqCore"
        ),
        federation="ProvisionedFederation(NativeHybridFederation)",
        time_unit="ns",
        snapshot=snapshot,
        events=core.events,
        bridge_steps=federation.steps,
        native_instructions=core.instruction_events,
        instruction_state_recording=True,
    )
    if network:
        report.update(
            nodes=participant.nodes,
            ns3_binary=str(manager.binary),
            ns3_time_ns=participant.now_ns,
            ns3_events=federation.rows,
            q2ns_status=participant.q2ns_status,
        )
    else:
        for event in core.events:
            if event["source"] == "ns3":
                event["source"] = "fixed-delay"
        report.update(
            fixed_delays=delays,
            transport="constant result-delay events; actual native quantum delivery; no ns-3 packet network",
        )
    try:
        report["validation"] = validate_report(
            report,
            expected,
            network,
            r_capacity=len(config["sessions"]) if model == "No-Dq-R" else 1,
        )
        report["cross_validation"] = cross_validate(report, references)
        report["metrics"] = session_metrics(report)
        for row in report["metrics"]["sessions"]:
            pred = expected["sessions"][str(row["session_id"])]
            row.update(
                resource_ready_ns=pred["resource_ready_ns"],
                eligible_ns=pred["eligible_ns"],
                resource_wait_ns=pred["resource_wait_ns"],
            )
        report["batch_completion_ns"] = max(
            row["completion_ns"] for row in report["metrics"]["sessions"]
        )
    except (ValueError, RuntimeError, KeyError) as error:
        report["validation"] = dict(passed=False, error=str(error))
        raise HybridValidationFailure(report, error) from error
    t5 = time.perf_counter()
    return (
        report,
        dict(
            core_initialization_seconds=t1 - t0,
            participant_startup_seconds=t2 - t1,
            federation_seconds=t3 - t2,
            participant_finish_seconds=t4 - t3,
            simulation_seconds=t4 - t0,
            offline_validation_seconds=t5 - t4,
            simulation_seconds_per_transaction=(t4 - t0) / len(config["sessions"]),
            federation_seconds_per_transaction=(t3 - t2) / len(config["sessions"]),
        ),
        participant,
    )


def run_model(config, model, cache, delays=None):
    return execute(config, model, cache, delays)[0]
