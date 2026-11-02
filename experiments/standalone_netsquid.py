"""Standalone NetSquid chain: native classical channels and a single event calendar.

Quantum programs, resource ownership and FIFO policy are shared with QuCl.
There is no ns-3 participant, socket or cross-simulator event coordinator. The small
protocol below reacts to native resource, channel and program-done events.
"""

import netsquid as ns
import pydynaa
from netsquid.components import ClassicalChannel, Message
from chained_config import normalize_config
from chained_core import ChainedCore, ChainedSwapAdapter, ChainedTeleportAdapter
from chained_validation import validate_report, metrics
from gate_noise import split_config, install_gate_noise
from packet_timing import tx_duration_ns
from state_validation import cross_validate_chained

PATHS = ("swap_packet_ns", "ready_packet_ns", "teleport_packet_ns")


def unloaded_delays(config):
    def hop(size, link):
        return tx_duration_ns(size, link["rate_bps"]) + link["delay_ns"]

    rb, ar = config["result_link"], config["access_link"]
    return dict(
        swap_packet_ns=hop(config["result_payload_bytes"], rb),
        ready_packet_ns=hop(16, rb) + hop(16, ar),
        teleport_packet_ns=hop(2, ar) + hop(2, rb),
    )


class NetSquidChainProtocol(pydynaa.Entity):
    """Readiness-gated classical control, with bit payloads carried by channels."""

    def __init__(self, core, delays):
        if set(delays) != set(PATHS) or any(
            type(v) is not int or v < 0 for v in delays.values()
        ):
            raise ValueError("three nonnegative integer path delays required")
        self.core, self.delays = core, dict(delays)
        self.rows, self.hops, self.channels = [], [], {}
        self.started, self.ready, self.issued = set(), set(), set()
        self.flush_pending = False
        self.swap_ids = {c["swap_session_id"] for c in core.config["chains"]}
        # The first hop of a two-hop path uses its unloaded wire+propagation
        # delay; any remaining calibrated waiting belongs to the second hop.
        # This split preserves the supplied end-to-end path exactly. It is a
        # documented fixed-delay abstraction, not a measured per-hop queue.
        c = core.config
        for path, route, size, first_link in (
            (PATHS[0], ("R", "B"), c["result_payload_bytes"], c["result_link"]),
            (PATHS[1], ("B", "R", "A"), 16, c["result_link"]),
            (PATHS[2], ("A", "R", "B"), 2, c["access_link"]),
        ):
            total = delays[path]
            first = min(
                total, tx_duration_ns(size, first_link["rate_bps"]) + first_link["delay_ns"]
            )
            durations = [total] if len(route) == 2 else [first, total - first]
            channels = []
            for index, delay in enumerate(durations):
                channel = ClassicalChannel(path + "-" + str(index), delay=delay)
                channel.ports["recv"].bind_output_handler(
                    lambda msg, p=path, i=index: self.arrive(p, i, msg)
                )
                core.quantum_network.nodes[route[index]].add_subcomponent(channel)
                channels.append(channel)
            self.channels[path] = (route, durations, channels)
        core.wakeup = self.wake
        for session in c["sessions"]:
            sid = session["session_id"]
            self.schedule(session["session_start_ns"], lambda sid=sid: self.start(sid))

    def schedule(self, at, callback):
        kind = pydynaa.EventType("CLASSICAL_PROTOCOL", "Standalone native protocol event")
        self._wait_once(
            pydynaa.EventHandler(lambda event: callback()), entity=self, event_type=kind
        )
        self._schedule_at(at, kind)

    def emit(self, kind, sid, cause, bits=None):
        c = self.core
        c.sync_time()
        row = dict(
            event_id="channel:" + str(len(self.rows)),
            time_ns=c.now_ns,
            event_type=kind,
            session_id=sid,
            cause=cause,
            m1=None if bits is None else bits[0],
            m2=None if bits is None else bits[1],
        )
        self.rows.append(row)
        c.import_rows([row])
        c.events[-1].update(source="netsquid-classical", phase="CLASSICAL")
        return row

    def start(self, sid):
        row = self.emit("SESSION_START", sid, self.core.roots[sid])
        self.started.add(sid)
        self.try_bsm(sid, row["event_id"])

    def try_bsm(self, sid, cause):
        if sid in self.started and sid in self.ready and sid not in self.issued:
            self.issued.add(sid)
            self.request("BSM_REQUEST", sid, cause)

    def request(self, kind, sid, cause, bits=None):
        row = self.emit(kind, sid, cause, bits)
        self.core.adapters[sid].receive(row)
        self.wake(int(ns.sim_time()))

    def wake(self, at):
        if at != ns.sim_time():
            raise RuntimeError("protocol wakeup must be at current native time")
        if not self.flush_pending:
            self.flush_pending = True
            self.schedule(at, self.flush)

    def flush(self):
        self.flush_pending = False
        c = self.core
        c.sync_time()
        ready, c.pending_ready = c.pending_ready, []
        for r in sorted(ready, key=lambda r: c.adapters[r["session_id"]].slot):
            sid = r["session_id"]
            if sid not in self.swap_ids or sid in self.ready:
                raise RuntimeError("unexpected elementary readiness")
            self.ready.add(sid)
            self.try_bsm(sid, r["cause"])
        completed, c.completed = c.completed, []
        for r in completed:
            sid = r["session_id"]
            if r["operation"] == "BSM":
                path = PATHS[0] if sid in self.swap_ids else PATHS[2]
                self.send(path, "RESULT", sid, r["cause"], r["measurement_bits"])
            elif sid in self.swap_ids:
                self.send(PATHS[1], "PAIR_READY", sid + 1, r["cause"])
        c.phase = "DISPATCH"
        c.dispatch()

    def send(self, path, kind, sid, cause, bits=None):
        row = self.emit(kind + "_TX", sid, cause, bits)
        payload = dict(kind=kind, sid=sid, bits=bits, cause=row["event_id"])
        self.transmit(path, 0, payload)

    def transmit(self, path, index, payload):
        route, delays, channels = self.channels[path]
        self.hops.append(
            dict(
                event="TX",
                path=path,
                hop=index,
                source=route[index],
                target=route[index + 1],
                session_id=payload["sid"],
                time_ns=int(ns.sim_time()),
                delay_ns=delays[index],
            )
        )
        channels[index].ports["send"].tx_input(Message([dict(payload)]))

    def arrive(self, path, index, message):
        payload = message.items[0]
        route, delays, channels = self.channels[path]
        self.hops.append(
            dict(
                event="RX",
                path=path,
                hop=index,
                source=route[index],
                target=route[index + 1],
                session_id=payload["sid"],
                time_ns=int(ns.sim_time()),
                delay_ns=delays[index],
            )
        )
        if index + 1 < len(channels):
            self.transmit(path, index + 1, payload)
            return
        sid, kind = payload["sid"], payload["kind"]
        row = self.emit(kind + "_RX", sid, payload["cause"], payload["bits"])
        if kind == "PAIR_READY":
            if sid in self.ready:
                raise RuntimeError("duplicate pair-ready delivery")
            self.ready.add(sid)
            self.try_bsm(sid, row["event_id"])
        else:
            self.request("CORRECTION_REQUEST", sid, row["event_id"], payload["bits"])


def run_netsquid(raw, delays=None, reference=True, enumerate_branches=False):
    raw, probability = split_config(raw)
    config = normalize_config(raw)
    config["gate_depolar_probability"] = probability
    core = ChainedCore(config)
    install_gate_noise(core)
    for slot, chain in enumerate(config["chains"]):
        swap, tele = config["sessions"][2 * slot : 2 * slot + 2]
        core.adapters[swap["session_id"]] = ChainedSwapAdapter(core, swap, slot)
        core.adapters[tele["session_id"]] = ChainedTeleportAdapter(core, tele, slot)
    protocol = NetSquidChainProtocol(
        core, unloaded_delays(config) if delays is None else delays
    )
    ns.sim_run()
    core.sync_time()
    if core.completed or core.pending_ready or protocol.flush_pending or ns.sim_count_events():
        raise RuntimeError("standalone native calendar did not drain")
    if any(p["queue"] or p["running"] for p in core.processors.values()):
        raise RuntimeError("unfinished standalone quantum requests")
    report = dict(
        schema_version=1,
        model="NetSquid-only",
        config=config,
        time_unit="ns",
        engine="NetSquid",
        quantum_state_owner="NetSquid",
        ipc_messages=0,
        transport="Native ClassicalChannel; fixed end-to-end calibrated delays",
        fixed_delays=protocol.delays,
        transport_events=protocol.rows,
        classical_channel_events=protocol.hops,
        snapshot=core.snapshot(),
        events=core.events,
        native_instructions=core.instruction_events,
    )
    report["validation"] = validate_report(report, network=False)
    report["metrics"] = metrics(report)
    if reference:
        report["cross_validation"] = cross_validate_chained(report, enumerate_branches)
    return report
