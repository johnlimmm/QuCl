"""Shared request registry, FIFO queues and resource ownership."""

from collections import deque
import copy
import netsquid as ns
from netsquid.components import QuantumMemory, QuantumProcessor
from netsquid.components.models.qerrormodels import T1T2NoiseModel
from limits import MAX_TIME_NS, integer


class HybridExecutionCore:
    def __init__(self, config, r_capacity=1):
        ns.sim_reset()
        ns.set_qstate_formalism(ns.QFormalism.DM)
        ns.set_random_state(seed=config["seed"])
        self.config, self.now_ns = config, 0
        self.events, self.resources, self.requests = [], {}, {}
        self.adapters, self.roots = {}, {}
        self.round, self.phase = 0, "INITIALIZE"
        n = len(config["sessions"])
        self.devices = {
            "A": QuantumMemory("A", num_positions=n),
            "R": QuantumProcessor("R", num_positions=2 * n),
            "B": QuantumProcessor("B", num_positions=n),
        }
        noise = config["memory_noise"]
        for device in self.devices.values():
            for position in device.mem_positions:
                position.models["noise_model"] = T1T2NoiseModel(
                    T1=noise["T1_ns"], T2=noise["T2_ns"]
                )
        integer(r_capacity, "R execution capacity", 1, n)
        self.r_capacity = r_capacity
        self.processors = {
            name: dict(queue=deque(), running=[], capacity=r_capacity if name == "R" else 1)
            for name in ("R", "B")
        }
        for session in config["sessions"]:
            self.roots[session["session_id"]] = self.emit(
                "SESSION_CREATED", session["session_id"], protocol=session["protocol"]
            )

    def emit(self, kind, sid, cause=None, **fields):
        event = dict(
            event_id="p:" + str(len(self.events)),
            sequence=len(self.events),
            event_type=kind,
            sim_time_ns=self.now_ns,
            session_id=sid,
            caused_by_event_id=cause,
            source="quantum",
            round=self.round,
            phase=self.phase,
        )
        event.update(fields)
        self.events.append(event)
        return event["event_id"]

    def import_rows(self, rows):
        for row in rows:
            self.events.append(
                dict(
                    event_id=row["event_id"],
                    sequence=len(self.events),
                    event_type=row["event_type"],
                    sim_time_ns=row["time_ns"],
                    session_id=row["session_id"],
                    caused_by_event_id=None if row["cause"] == "-" else row["cause"],
                    source="ns3",
                    round=self.round,
                    phase="NS3",
                    details=copy.deepcopy(row),
                )
            )

    def register(self, sid, handle, kind, locations, state="AVAILABLE"):
        key = (sid, handle)
        if key in self.resources:
            raise ValueError("resource handle reused")
        if len(set(locations)) != len(locations):
            raise ValueError("duplicate physical resource position")
        self.resources[key] = dict(
            session_id=sid,
            handle=handle,
            kind=kind,
            locations=list(locations),
            state=state,
            owner=None,
            created_ns=self.now_ns,
        )
        return key

    def peek(self, location):
        return self.devices[location[0]].peek(location[1])[0]

    def submit(self, adapter, operation, processor, handles, targets, duration, cause, bits=None):
        if operation not in ("BSM", "CORRECTION"):
            raise ValueError("unsupported quantum operation")
        sid = adapter.sid
        key = (sid, operation)
        if key in self.requests:
            old = self.requests[key]
            if old["bits"] != bits:
                raise ValueError("request reuse with different measurement bits")
            self.emit(operation + "_DUPLICATE", sid, cause, protocol=adapter.protocol)
            return False
        if processor not in self.processors or any(p != processor for p, _ in targets):
            raise ValueError("operation targets must be local to processor")
        keys = [(sid, h) for h in handles]
        expected = "AVAILABLE" if operation == "BSM" else "FRAME_PENDING"
        owned = []
        for resource in keys:
            r = self.resources[resource]
            if r["owner"] is not None or r["state"] != expected:
                raise ValueError("resource unavailable")
            owned.extend(r["locations"])
        if len(set(keys)) != len(keys) or not set(targets).issubset(owned):
            raise ValueError("invalid operation resources")
        active_locations = {
            tuple(loc)
            for r in self.resources.values()
            if r["owner"] is not None
            for loc in r["locations"]
        }
        if active_locations.intersection(owned):
            raise ValueError("physical resource conflict")
        integer(duration, "duration", 1, MAX_TIME_NS)
        pending = sum(
            r["duration_ns"]
            for r in self.requests.values()
            if r["processor_id"] == processor and r["state"] in ("QUEUED", "RUNNING")
        )
        if self.now_ns + pending + duration > MAX_TIME_NS:
            raise ValueError("operation time overflow")
        request = dict(
            session_id=sid,
            protocol=adapter.protocol,
            operation=operation,
            request_id=str(sid) + ":" + operation,
            processor_id=processor,
            handles=list(handles),
            targets=list(targets),
            duration_ns=duration,
            arrival_ns=self.now_ns,
            start_ns=None,
            completion_ns=None,
            state="QUEUED",
            bits=copy.deepcopy(bits),
            cause=cause,
        )
        self.requests[key] = request
        for k in keys:
            self.resources[k].update(state="RESERVED", owner=request["request_id"])
        request["cause"] = self.emit(
            operation + "_QUEUED",
            sid,
            cause,
            request_id=request["request_id"],
            protocol=adapter.protocol,
            processor_id=processor,
            resource_handles=handles,
        )
        self.processors[processor]["queue"].append(key)
        return True

    def snapshot(self):
        self.check()
        return dict(
            time_ns=self.now_ns,
            netsquid_time_ns=int(ns.sim_time()),
            R_execution_capacity=self.r_capacity,
            requests=copy.deepcopy(list(self.requests.values())),
            resources=copy.deepcopy(list(self.resources.values())),
            sessions={str(sid): a.snapshot() for sid, a in self.adapters.items()},
        )
