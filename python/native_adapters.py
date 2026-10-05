"""The same Swap/Teleport resources with native program execution at dispatch."""

from netsquid.qubits import qubitapi as qapi
from protocol_adapters import SwapAdapter, TeleportAdapter
from quantum_state import encode_state
from native_programs import NativeOperation


class NativeAdapter:
    def on_start(self, request):
        super().on_start(request)
        self.program = NativeOperation(
            request["operation"],
            request["bits"],
            lambda i, n: self.core.instruction_started(request, i, n),
            lambda i, n: self.core.instruction_finished(request, i, n),
            self.core.capture_states,
        )

    def instruction_state(self, request):
        if request["operation"] == "BSM":
            positions = (
                self.local + self.survivors
                if self.protocol == "teleport"
                else [self.survivors[0]] + self.local + [self.survivors[1]]
            )
            order = (
                ["input", "Alice_EPR", "B"]
                if self.protocol == "teleport"
                else ["A", "R_AR", "R_RB", "B"]
            )
            qs = self.qubits(positions)
            dm = qapi.reduced_dm(qs)
            return dict(
                sim_time_ns=self.core.now_ns,
                density_matrix=dict(
                    qubit_order=order, real=dm.real.tolist(), imag=dm.imag.tolist()
                ),
            )
        return encode_state(
            self.qubits(self.survivors), self.output_order, self.core.now_ns, self.target
        )

    def on_complete(self, request):
        if request["operation"] == "BSM":
            self.bits = [int(self.program.output[k][0]) for k in ("m1", "m2")]
            for q in self.core.devices["R"].pop([p[1] for p in self.local]):
                qapi.discard(q)
            self.core.register(
                self.sid, "output", self.output_kind, self.survivors, "FRAME_PENDING"
            )
            self.checkpoints["frame"] = self.output_state()
        else:
            if any(a is not b for a, b in zip(self.qubits(self.survivors), self.original_output)):
                raise RuntimeError("native output qubit identity changed")
            self.checkpoints["usable"] = self.output_state(corrected=True)
            self.correction_count += 1


class NativeSwapAdapter(NativeAdapter, SwapAdapter):
    pass


class NativeTeleportAdapter(NativeAdapter, TeleportAdapter):
    pass
