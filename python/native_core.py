"""Native event-driven backend using shared resource ownership and FIFO queues.

NetSquid owns the event calendar. ns-3 next-event times are scheduled onto that
calendar as bridge wakeups; internal gates never predict a completion callback.
This avoids stepping past a classical input or relying on private PyDynAA APIs.
"""

import netsquid as ns
import pydynaa
from execution_core import HybridExecutionCore
from native_programs import physical_instructions
from limits import MAX_TIME_NS


class NativeExecutionCore(HybridExecutionCore):
    def __init__(self, config, capture_states=True):
        super().__init__(config)
        self.capture_states = capture_states
        self.completed, self.instruction_events = [], []
        self.wakeup = None
        for name in ("R", "B"):
            device = self.devices[name]
            for instruction in physical_instructions(
                config["native_instructions"], config["memory_noise"]
            ):
                device.add_physical_instruction(instruction)
            device.set_program_done_callback(self.program_done, name, once=False)
            device.set_program_fail_callback(self.program_failed, name, once=False)

    def sync_time(self):
        now = ns.sim_time()
        if not 0 <= now <= MAX_TIME_NS or now != int(now) or now < self.now_ns:
            raise RuntimeError("non-integer or backwards native time")
        self.now_ns = int(now)

    def drain(self):
        # Startup put notifications stay on the real native calendar.
        self.sync_time()

    def horizon(self):
        # Diagnostic only: native sequence ends, never fabricated op completions.
        times = [
            int(self.device_for(self.requests[k]).sequence_end_time)
            for p in self.processors.values()
            for k in p["running"]
        ]
        return min(times) if times else None

    def device_for(self, request):
        return self.devices[request["processor_id"]]

    def advance(self, at):
        raise RuntimeError("native backend must run through CrossSimulatorCoordinator")

    def dispatch(self):
        self.sync_time()
        for name, p in self.processors.items():
            while p["queue"] and len(p["running"]) < p["capacity"]:
                key = p["queue"].popleft()
                request = self.requests[key]
                request.update(state="RUNNING", start_ns=self.now_ns, completion_ns=None)
                p["running"].append(key)
                for handle in request["handles"]:
                    self.resources[(request["session_id"], handle)]["state"] = "IN_USE"
                adapter = self.adapters[request["session_id"]]
                adapter.on_start(request)
                request["cause"] = self.emit(
                    request["operation"] + "_START",
                    adapter.sid,
                    request["cause"],
                    protocol=adapter.protocol,
                    processor_id=name,
                    request_id=request["request_id"],
                    resource_handles=request["handles"],
                    waiting_ns=self.now_ns - request["arrival_ns"],
                )
                self.device_for(request).execute_program(
                    adapter.program,
                    qubit_mapping=self.target_positions(request),
                    error_on_fail=True,
                )
        self.check()

    def target_positions(self, request):
        return [position for _, position in request["targets"]]

    def instruction_started(self, request, index, name):
        self.sync_time()
        self.phase = "NATIVE"
        request["instruction_start_ns"] = self.now_ns
        request["cause"] = self.emit(
            "NATIVE_INSTRUCTION_START",
            request["session_id"],
            request["cause"],
            operation=request["operation"],
            processor_id=request["processor_id"],
            index=index,
            instruction=name,
        )

    def instruction_finished(self, request, index, name):
        self.sync_time()
        self.phase = "NATIVE"
        adapter = self.adapters[request["session_id"]]
        record = dict(
            session_id=adapter.sid,
            operation=request["operation"],
            processor_id=request["processor_id"],
            index=index,
            instruction=name,
            start_ns=request["instruction_start_ns"],
            completion_ns=self.now_ns,
            state=adapter.instruction_state(request) if self.capture_states else None,
        )
        self.instruction_events.append(record)
        request["cause"] = self.emit(
            "NATIVE_INSTRUCTION_COMPLETE",
            adapter.sid,
            request["cause"],
            operation=request["operation"],
            processor_id=request["processor_id"],
            index=index,
            instruction=name,
        )

    def program_failed(self, name):
        raise RuntimeError("native program failed: " + str(self.devices[name].fail_exception))

    def program_done(self, name, key=None):
        self.sync_time()
        self.phase = "NATIVE"
        p = self.processors[name]
        if key is None and len(p["running"]) == 1:
            key = p["running"][0]
        if key is None or key not in p["running"]:
            raise RuntimeError("unsolicited or duplicate native completion")
        request = self.requests[key]
        adapter = self.adapters[request["session_id"]]
        if self.device_for(request).busy:
            raise RuntimeError("completion while native processor busy")
        adapter.on_complete(request)
        request.update(state="COMPLETED", completion_ns=self.now_ns)
        p["running"].remove(key)
        for handle in request["handles"]:
            self.resources[(adapter.sid, handle)].update(
                owner=None, state="CONSUMED" if request["operation"] == "BSM" else "USABLE"
            )
        request["cause"] = self.emit(
            request["operation"] + "_COMPLETE",
            adapter.sid,
            request["cause"],
            protocol=adapter.protocol,
            request_id=request["request_id"],
            processor_id=name,
            completion_source="netsquid_program_done",
            measurement_bits=adapter.bits,
        )
        self.completed.append(
            dict(
                session_id=adapter.sid,
                operation=request["operation"],
                measurement_bits=adapter.bits,
                cause=request["cause"],
            )
        )
        if self.wakeup is None:
            raise RuntimeError("native completion has no coordinator receiver")
        self.wakeup(self.now_ns)

    def check(self):
        if ns.sim_time() != self.now_ns:
            raise RuntimeError("native/core clock mismatch")
        seen = set()
        locations = set()
        for name, p in self.processors.items():
            if len(p["running"]) > p["capacity"]:
                raise RuntimeError("native processor capacity exceeded")
            for key in list(p["queue"]) + p["running"]:
                if key in seen:
                    raise RuntimeError("request dispatched twice")
                seen.add(key)
                r = self.requests[key]
                if (
                    r["state"] != ("RUNNING" if key in p["running"] else "QUEUED")
                    or r["processor_id"] != name
                ):
                    raise RuntimeError("request/FIFO mismatch")
                for handle in r["handles"]:
                    resource = self.resources[(r["session_id"], handle)]
                    if resource["owner"] != r["request_id"] or resource["state"] != (
                        "IN_USE" if key in p["running"] else "RESERVED"
                    ):
                        raise RuntimeError("resource ownership mismatch")
                    if locations.intersection(resource["locations"]):
                        raise RuntimeError("resource positions overlap")
                    locations.update(resource["locations"])
        if seen != {k for k, r in self.requests.items() if r["state"] in ("QUEUED", "RUNNING")}:
            raise RuntimeError("orphaned native request")


class CrossSimulatorCoordinator(pydynaa.Entity):
    """Merge ns-3 inputs and native quantum events in the actual NetSquid calendar.

    Existing ns-3 events at t are drained before injecting new completions at t.
    Tied request arrivals keep the participant's input order. Processor slots are
    released only by native done callbacks, so same-time input/completion order
    cannot start an operation while its processor is still busy.
    """

    def __init__(self, core, participant):
        self.core, self.participant = core, participant
        self.steps, self.rows, self.pending = [], [], set()
        self.event_type = pydynaa.EventType("QUCL_BRIDGE", "ns-3 boundary or native completion")
        self._wait(pydynaa.EventHandler(self.step), entity=self, event_type=self.event_type)
        core.wakeup = self.schedule

    def schedule(self, at):
        if at is None or at in self.pending:
            return
        if at < ns.sim_time():
            raise RuntimeError("classical input scheduled in native past")
        self.pending.add(at)
        self._schedule_at(at, self.event_type)

    def step(self, event):
        c, p = self.core, self.participant
        c.sync_time()
        at = c.now_ns
        self.pending.remove(at)
        if len(self.steps) >= 100000:
            raise RuntimeError("native coordination step limit")
        c.round = len(self.steps)
        c.phase = "NS3"
        completed = c.completed
        c.completed = []
        rows = p.advance(at)
        self.rows.extend(rows)
        c.import_rows(rows)
        c.phase = "INPUT"
        for row in rows:
            if row["event_type"] in ("BSM_REQUEST", "CORRECTION_REQUEST"):
                c.adapters[row["session_id"]].receive(row)
        c.phase = "DISPATCH"
        c.dispatch()
        p.inject(at, completed)
        if c.now_ns != p.now_ns:
            raise RuntimeError("native coordination clock mismatch")
        self.steps.append(
            dict(
                time_ns=at,
                ns3_next_ns=p.next_time,
                quantum_next_ns=c.horizon(),
                completed=len(completed),
                inputs=len(rows),
            )
        )
        self.schedule(p.next_time)

    def run(self):
        self.schedule(self.participant.next_time)
        ns.sim_run()
        self.core.sync_time()
        self.core.check()
        if (
            self.pending
            or self.core.completed
            or any(p["queue"] or p["running"] for p in self.core.processors.values())
        ):
            raise RuntimeError("native coordination did not drain")
        if self.core.now_ns != self.participant.now_ns or ns.sim_count_events():
            raise RuntimeError("native final clocks/events mismatch")
        return self.core.snapshot()
