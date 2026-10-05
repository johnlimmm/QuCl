"""A--R--B swapping followed by teleportation consuming the actual swap output."""

import copy
from provisioned_config import normalize_config as normalize_provisioned
from limits import integer, MAX_TIME_NS
from packet_timing import tx_duration_ns
from protocol_adapters import STATES


def normalize_config(raw):
    raw = copy.deepcopy(raw)
    if not isinstance(raw, dict) or "sessions" in raw:
        raise ValueError("chained configuration uses chains, not independent sessions")
    chains = raw.pop("chains", [dict(chain_id=1, session_start_ns=1000000, input_state="+i")])
    access = raw.pop("access_link", dict(rate_bps=10000000, delay_ns=200000))
    if not isinstance(chains, list) or not 1 <= len(chains) <= 32:
        raise ValueError("requires 1..32 chains")
    sessions = []
    seen = set()
    for slot, chain in enumerate(chains):
        if not isinstance(chain, dict) or set(chain) - {
            "chain_id",
            "session_start_ns",
            "input_state",
        }:
            raise ValueError("invalid chain fields")
        cid = integer(chain.get("chain_id"), "chain_id", 1)
        integer(chain.get("session_start_ns"), "session_start_ns", maximum=MAX_TIME_NS)
        if cid in seen:
            raise ValueError("duplicate chain ID")
        seen.add(cid)
        chain.setdefault("input_state", "+i")
        if not isinstance(chain["input_state"], str) or chain["input_state"] not in STATES:
            raise ValueError("unsupported chained input state")
        sid = 2 * slot + 1
        sessions.extend(
            [
                dict(session_id=sid, protocol="swap", session_start_ns=chain["session_start_ns"]),
                dict(
                    session_id=sid + 1,
                    protocol="teleport",
                    session_start_ns=chain["session_start_ns"],
                    input_state=chain["input_state"],
                ),
            ]
        )
    raw["sessions"] = sessions
    config = normalize_provisioned(raw)
    for slot, chain in enumerate(chains):
        config["sessions"][2 * slot + 1]["input_state"] = chain["input_state"]
    if not isinstance(access, dict) or set(access) != {"rate_bps", "delay_ns"}:
        raise ValueError("invalid A--R classical access link")
    integer(access["rate_bps"], "access rate", 1, 10**12)
    integer(access["delay_ns"], "access delay", maximum=MAX_TIME_NS)
    for link in (access, config["result_link"]):
        for size in (
            2,
            16,
            config["result_payload_bytes"],
            config["background"]["result"]["payload_bytes"],
        ):
            if (
                tx_duration_ns(size, link["rate_bps"]) < 1
                or ((size + 30) * 8 * 10**9 % link["rate_bps"]) * 2 == link["rate_bps"]
            ):
                raise ValueError("serialization must avoid zero duration and half-ns ties")
    config.update(chains=chains, access_link=access)
    for slot, chain in enumerate(chains):
        chain.update(swap_session_id=2 * slot + 1, teleport_session_id=2 * slot + 2)
        for s in config["sessions"][2 * slot : 2 * slot + 2]:
            s.update(
                chain_id=chain["chain_id"],
                parent_session_id=2 * slot + 1 if s["protocol"] == "teleport" else 0,
            )
    # Conservative bound covers four serialized quantum operations and five
    # routed control hops per chain, including finite background drain.
    bound = max(s["session_start_ns"] for s in sessions) + max(
        q["delay_ns"] for q in config["quantum_links"].values()
    )
    bound += len(chains) * (
        2 * config["bsm_duration_ns"]
        + 2 * config["correction_duration_ns"]
        + 5
        * (
            max(access["delay_ns"], config["result_link"]["delay_ns"])
            + tx_duration_ns(1400, min(access["rate_bps"], config["result_link"]["rate_bps"]))
        )
    )
    f = config["background"]["result"]
    bound += f["start_ns"] + f["count"] * (
        f["interval_ns"] + tx_duration_ns(f["payload_bytes"], config["result_link"]["rate_bps"])
    )
    if bound > MAX_TIME_NS:
        raise ValueError("chained simulation time overflow")
    return config
