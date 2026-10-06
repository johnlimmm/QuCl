"""Native R swap, actual pair handoff, native A teleport, shared B corrections."""

from collections import deque
from netsquid.components import QuantumProcessor
from netsquid.components.models.qerrormodels import T1T2NoiseModel
from netsquid.qubits import qubitapi as qapi
from native_core import NativeExecutionCore
from native_programs import physical_instructions
from native_adapters import NativeTeleportAdapter
from protocol_adapters import STATES
from provisioned_core import ProvisionedSwapAdapter, ProvisionedCoordinator
from quantum_network_backend import QuantumNetworkBackend


class ChainedCore(NativeExecutionCore):
    def __init__(self, config, capture_states=True):
        super().__init__(config, capture_states)
        a = QuantumProcessor("A", num_positions=2 * len(config["chains"]))
        noise = config["memory_noise"]
        for position in a.mem_positions:
            position.models["noise_model"] = T1T2NoiseModel(
                T1=noise["T1_ns"], T2=noise["T2_ns"]
            )
        for instruction in physical_instructions(config["native_instructions"], noise):
            a.add_physical_instruction(instruction)
        a.set_program_done_callback(self.program_done, "A", once=False)
        a.set_program_fail_callback(self.program_failed, "A", once=False)
        self.devices["A"] = a
        self.processors["A"] = dict(queue=deque(), running=[], capacity=1)
        self.pending_ready = []
        self.handoffs = []
        self.quantum_network = self.create_quantum_network()

    def create_quantum_network(self):
        """Attach native memories before creating quantum delivery components."""
        return QuantumNetworkBackend(self)

    def program_done(self, name, key=None):
        # Only a native program-done callback can authorize handing off a pair.
        if key is None and len(self.processors[name]["running"]) == 1:
            key = self.processors[name]["running"][0]
        super().program_done(name, key)
        request = self.requests[key]
        if request["protocol"] == "swap" and request["operation"] == "CORRECTION":
            swap = self.adapters[request["session_id"]]
            tele = self.adapters[swap.sid + 1]
            pair = self.resources[(swap.sid, "output")]
            if (
                pair["state"] != "USABLE"
                or pair["owner"] is not None
                or (tele.sid, "epr") in self.resources
            ):
                raise RuntimeError("invalid or duplicate swap-output transfer")
            pair.update(state="TRANSFERRED", transferred_to=(tele.sid, "epr"))
            self.register(tele.sid, "epr", "pair", swap.survivors)
            target = self.resources[(tele.sid, "epr")]
            target.update(
                created_ns=pair["created_ns"], ready_ns=self.now_ns, origin=(swap.sid, "output")
            )
            tele.original_pair = tuple(self.peek(loc) for loc in swap.survivors)
            if any(x is not y for x, y in zip(tele.original_pair, swap.original_output)):
                raise RuntimeError("handoff replaced an entangled qubit")
            tele.original_output = (tele.original_pair[1],)
            tele.ready_ns = self.now_ns
            tele.checkpoints["handoff"] = tele.input_states()
            cause = self.emit(
                "PAIR_TRANSFERRED",
                tele.sid,
                request["cause"],
                from_session_id=swap.sid,
                from_handle="output",
                to_handle="epr",
                same_qubit_objects=True,
                locations=swap.survivors,
            )
            self.handoffs.append(
                dict(
                    chain_id=tele.session["chain_id"],
                    from_session_id=swap.sid,
                    to_session_id=tele.sid,
                    sim_time_ns=self.now_ns,
                    event_id=cause,
                    same_qubit_objects=True,
                )
            )

    def snapshot(self):
        value = super().snapshot()
        value.update(quantum_network=self.quantum_network.snapshot(), handoffs=self.handoffs)
        if self.pending_ready:
            raise RuntimeError("undelivered swap readiness notification")
        return value


class ChainedSwapAdapter(ProvisionedSwapAdapter):
    def __init__(self, core, session, slot):
        # Allocate Alice's pair half beside her input. No teleport EPR is created.
        self.core, self.session, self.sid = core, session, session["session_id"]
        self.slot = slot
        self.local = [("R", 2 * slot), ("R", 2 * slot + 1)]
        self.bits = None
        self.checkpoints = {}
        self.correction_count = 0
        self.ready_ns = None
        self.survivors = [("A", 2 * slot + 1), ("B", slot)]
        for handle, remote, local in zip(self.inputs, self.survivors, self.local):
            core.quantum_network.pair(self, handle, remote, local)


class ChainedTeleportAdapter(NativeTeleportAdapter):
    def __init__(self, core, session, slot):
        self.core, self.session, self.sid = core, session, session["session_id"]
        self.slot = slot
        self.local = [("A", 2 * slot), ("A", 2 * slot + 1)]
        self.survivors = [("B", slot)]
        self.bits = None
        self.checkpoints = {}
        self.correction_count = 0
        self.ready_ns = None
        self.target = STATES[session["input_state"]]
        q = qapi.create_qubits(1)[0]
        qapi.assign_qstate([q], self.target)
        self.put(q, self.local[0])
        core.register(self.sid, "input", "qubit", [self.local[0]])
        core.emit(
            "INPUT_CREATED",
            self.sid,
            core.roots[self.sid],
            created_ns=0,
            location=self.local[0],
        )

    def receive(self, row):
        if self.ready_ns is None:
            raise RuntimeError("teleport request before pair handoff")
        op = "BSM" if row["event_type"] == "BSM_REQUEST" else "CORRECTION"
        if row["session_id"] != self.sid:
            raise ValueError("teleport session mismatch")
        bits = None
        if op == "BSM":
            if any(
                x is not y
                for x, y in zip(
                    self.qubits([self.local[1]] + self.survivors), self.original_pair
                )
            ):
                raise RuntimeError("teleport did not consume actual swapped pair")
            self.checkpoints.setdefault("arrival", self.input_states())
        else:
            bits = [row["m1"], row["m2"]]
            if bits != self.bits:
                raise ValueError("teleport result mismatch")
            self.checkpoints.setdefault("packet_arrival", self.output_state())
        return self.core.submit(
            self,
            op,
            "A" if op == "BSM" else "B",
            self.inputs if op == "BSM" else ["output"],
            self.local if op == "BSM" else self.survivors,
            self.core.config["bsm_duration_ns" if op == "BSM" else "correction_duration_ns"],
            row["event_id"],
            bits,
        )

    def on_complete(self, request):
        if request["operation"] == "BSM":
            self.bits = [int(self.program.output[k][0]) for k in ("m1", "m2")]
            for q in self.core.devices["A"].pop([p[1] for p in self.local]):
                qapi.discard(q)
            self.core.register(self.sid, "output", "qubit", self.survivors, "FRAME_PENDING")
            self.checkpoints["frame"] = self.output_state()
        else:
            super().on_complete(request)

    def snapshot(self):
        value = super().snapshot()
        value.update(
            resource_ready_ns=self.ready_ns, parent_session_id=self.session["parent_session_id"]
        )
        return value


# The same conservative coordinator transports only elementary-pair readiness
# through ideal IPC. Teleport readiness is exclusively an actual ns-3 packet.
ChainedCoordinator = ProvisionedCoordinator
