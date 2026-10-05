"""Direct R session starts, A–R–B quantum resources and one R→B UDP link.

The old command fields are rejected, not silently interpreted as zero delay.
Times are integer nanoseconds; equal-time starts retain configuration order.
"""

import copy
from packet_timing import tx_duration_ns
from limits import MAX_TIME_NS, integer
from protocol_adapters import STATES

DEFAULTS = dict(
    name="hybrid-direct-start",
    seed=7,
    bsm_duration_ns=1600000,
    correction_duration_ns=500000,
    result_link=dict(rate_bps=10000000, delay_ns=200000),
    result_payload_bytes=80,
    queue_packets=128,
    background={},
    memory_noise=dict(model="T1T2NoiseModel", T1_ns=20000000, T2_ns=10000000),
    sessions=[dict(session_id=1, protocol="teleport", input_state="+i", session_start_ns=1000000)],
)
DEFAULT_FLOW = dict(start_ns=0, interval_ns=1000000, count=0, payload_bytes=1000)


def normalize_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("unsupported hybrid configuration field (command path removed)")
    c = copy.deepcopy(DEFAULTS)
    c.update(copy.deepcopy(raw))
    if not isinstance(c["name"], str):
        raise ValueError("name must be a string")
    integer(c["seed"], "seed", maximum=2**32 - 1)
    for k in ("bsm_duration_ns", "correction_duration_ns"):
        integer(c[k], k, 1, MAX_TIME_NS)
    integer(c["queue_packets"], "queue_packets", 1, 65536)
    integer(c["result_payload_bytes"], "result_payload_bytes", 60, 1400)
    link = c["result_link"]
    if not isinstance(link, dict) or set(link) != {"rate_bps", "delay_ns"}:
        raise ValueError("invalid result link")
    integer(link["rate_bps"], "rate_bps", 1, 10**12)
    integer(link["delay_ns"], "delay_ns", maximum=MAX_TIME_NS)
    noise = c["memory_noise"]
    if (
        not isinstance(noise, dict)
        or set(noise) != {"model", "T1_ns", "T2_ns"}
        or noise["model"] != "T1T2NoiseModel"
    ):
        raise ValueError("requires native T1T2NoiseModel")
    for k in ("T1_ns", "T2_ns"):
        integer(noise[k], k, maximum=MAX_TIME_NS)
    if noise["T1_ns"] and noise["T2_ns"] > noise["T1_ns"]:
        raise ValueError("NetSquid requires T2 <= T1")
    if not isinstance(c["background"], dict) or set(c["background"]) - {"result"}:
        raise ValueError("background supports the result link only")
    f = copy.deepcopy(DEFAULT_FLOW)
    given = c["background"].get("result", {})
    if not isinstance(given, dict) or set(given) - set(f):
        raise ValueError("unknown background fields")
    f.update(given)
    integer(f["start_ns"], "background start", maximum=MAX_TIME_NS)
    integer(f["interval_ns"], "background interval", 1, MAX_TIME_NS)
    integer(f["count"], "background count", maximum=2000)
    integer(f["payload_bytes"], "background payload", 60, 1400)
    c["background"] = {"result": f}
    if f["count"] and f["start_ns"] + (f["count"] - 1) * f["interval_ns"] > MAX_TIME_NS:
        raise ValueError("background time overflow")
    for payload in (2, c["result_payload_bytes"], f["payload_bytes"]):
        if ((payload + 30) * 8 * 10**9 % link["rate_bps"]) * 2 == link[
            "rate_bps"
        ] or tx_duration_ns(payload, link["rate_bps"]) < 1:
            raise ValueError("serialization must avoid half-ns ties and zero duration")
    sessions = c["sessions"]
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 64:
        raise ValueError("requires 1..64 sessions")
    seen = set()
    for s in sessions:
        if not isinstance(s, dict) or set(s) - {
            "session_id",
            "session_start_ns",
            "protocol",
            "input_state",
            "input_created_ns",
            "epr_created_ns",
        }:
            raise ValueError("unknown hybrid session field; use session_start_ns")
        sid = integer(s.get("session_id"), "session_id", 1)
        integer(s.get("session_start_ns"), "session_start_ns", maximum=MAX_TIME_NS)
        if sid in seen:
            raise ValueError("duplicate session ID")
        seen.add(sid)
        s.setdefault("protocol", "swap")
        s.setdefault("epr_created_ns", 0)
        if s["protocol"] not in ("swap", "teleport"):
            raise ValueError("unknown protocol")
        if type(s["epr_created_ns"]) is not int or s["epr_created_ns"] != 0:
            raise ValueError("EPR creation must be at t=0")
        if s["protocol"] == "teleport":
            s.setdefault("input_state", "+i")
            s.setdefault("input_created_ns", 0)
            if s["input_state"] not in STATES:
                raise ValueError("unsupported input state")
            if type(s["input_created_ns"]) is not int or s["input_created_ns"] != 0:
                raise ValueError("input creation must be at t=0")
        elif "input_state" in s or "input_created_ns" in s:
            raise ValueError("swap has no input qubit")
    if expected_timing(c)["simulation_completion_ns"] > MAX_TIME_NS:
        raise ValueError("simulation time overflow")
    return c


def result_fifo(config, controls):
    flow = config["background"]["result"]
    link = config["result_link"]
    packets = [
        dict(
            packet_id=100000 + i,
            session_id=0,
            link="result",
            traffic_class="background",
            app_tx_ns=flow["start_ns"] + i * flow["interval_ns"],
            payload_bytes=flow["payload_bytes"],
        )
        for i in range(flow["count"])
    ]
    packets += [dict(p, link="result", traffic_class="control") for p in controls]
    packets.sort(key=lambda p: (p["app_tx_ns"], p["session_id"] != 0, p["packet_id"]))
    free = 0
    for p in packets:
        start = max(free, p["app_tx_ns"])
        duration = tx_duration_ns(p["payload_bytes"], link["rate_bps"])
        free = start + duration
        p.update(
            enqueue_ns=p["app_tx_ns"],
            dequeue_ns=start,
            phy_tx_ns=start,
            phy_tx_end_ns=free,
            phy_rx_ns=free + link["delay_ns"],
            app_rx_ns=free + link["delay_ns"],
            queue_wait_ns=start - p["app_tx_ns"],
            serialization_ns=duration,
            propagation_ns=link["delay_ns"],
            total_delay_ns=free + link["delay_ns"] - p["app_tx_ns"],
            wire_bytes=p["payload_bytes"] + 30,
        )
    return packets


def expected_timing(config):
    """Independent FIFO oracle from configuration alone, never observed traces."""
    sessions = {}
    results = []
    free = 0
    for slot, s in sorted(
        enumerate(config["sessions"]), key=lambda entry: entry[1]["session_start_ns"]
    ):
        at = s["session_start_ns"]
        start = max(free, at)
        end = start + config["bsm_duration_ns"]
        free = end
        sessions[str(s["session_id"])] = dict(
            session_start_ns=at,
            bsm_arrival_ns=at,
            bsm_start_ns=start,
            bsm_completion_ns=end,
            bsm_wait_ns=start - at,
        )
        results.append(
            dict(
                packet_id=slot + 1,
                session_id=s["session_id"],
                app_tx_ns=end,
                payload_bytes=2 if s["protocol"] == "teleport" else config["result_payload_bytes"],
            )
        )
    packets = result_fifo(config, results)
    free = 0
    for p in packets:
        if not p["session_id"]:
            continue
        start = max(free, p["app_rx_ns"])
        free = start + config["correction_duration_ns"]
        sessions[str(p["session_id"])].update(
            correction_arrival_ns=p["app_rx_ns"],
            correction_start_ns=start,
            correction_completion_ns=free,
            correction_wait_ns=start - p["app_rx_ns"],
        )
    drain = max(p["app_rx_ns"] for p in packets)
    batch = max(t["correction_completion_ns"] for t in sessions.values())
    return dict(
        sessions=sessions,
        packets=packets,
        batch_completion_ns=batch,
        traffic_drain_completion_ns=drain,
        simulation_completion_ns=max(batch, drain),
    )
