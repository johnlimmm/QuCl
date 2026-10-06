"""Validate packet timing, processor schedules and resource lifecycle."""

from packet_timing import tx_duration_ns


def require(condition, message):
    if not condition:
        raise RuntimeError("Chained validation: " + message)


def only(rows, kind, sid=None):
    found = [
        r for r in rows if r["event_type"] == kind and (sid is None or r["session_id"] == sid)
    ]
    require(len(found) == 1, "unique " + kind + " for " + str(sid))
    return found[0]


def validate_report(report, *, r_capacity=1, network=True):
    c = report["config"]
    snap = report["snapshot"]
    rows = report["ns3_events"] if network else report["transport_events"]
    events = report["events"]
    require(type(r_capacity) is int and 1 <= r_capacity <= len(c["chains"]), "R capacity")
    require(snap["time_ns"] == snap["netsquid_time_ns"], "quantum clocks")
    if not network:
        require(
            "ns3_events" not in report and "q2ns_status" not in report,
            "virtual transport claims",
        )
        if "frozen_delays" in report:
            require(
                "fixed_delays" not in report and report.get("model") == "Decoupled",
                "frozen transport model",
            )
            require(
                set(report["frozen_delays"]) == {str(c["chain_id"]) for c in c["chains"]},
                "frozen transport workflow identities",
            )
        for chain in c["chains"]:
            swap, tele = chain["swap_session_id"], chain["teleport_session_id"]
            delays = (
                report["frozen_delays"][str(chain["chain_id"])]
                if "frozen_delays" in report
                else report["fixed_delays"]
            )
            require(
                set(delays) == {"swap_packet_ns", "ready_packet_ns", "teleport_packet_ns"}
                and all(type(v) is int and v >= 0 for v in delays.values()),
                "virtual path delay values",
            )
            for sid, prefix, field in (
                (swap, "RESULT", "swap_packet_ns"),
                (tele, "PAIR_READY", "ready_packet_ns"),
                (tele, "RESULT", "teleport_packet_ns"),
            ):
                require(
                    only(rows, prefix + "_RX", sid)["time_ns"]
                    - only(rows, prefix + "_TX", sid)["time_ns"]
                    == delays[field],
                    "fixed path delay",
                )
    if network:
        require(report["nodes"] == dict(A=0, R=1, B=2), "node placement")
        require(report["ns3_time_ns"] == snap["time_ns"] == snap["netsquid_time_ns"], "clocks")
        require(
            report["q2ns_status"]
            == dict(
                native_state_count=0,
                native_qubit_count=0,
                sessions=2 * len(c["chains"]),
                corrections_applied=2 * len(c["chains"]),
            ),
            "Q2NS ownership/completion",
        )
        require(not any("DROP" in r["event_type"] for r in rows), "packet drop")
        require(
            not any(r["event_type"].startswith("COMMAND_") for r in rows),
            "synthetic command path",
        )
    seen = {}
    for event in events:
        require(
            type(event["sim_time_ns"]) is int and event["sim_time_ns"] >= 0,
            "integer event time",
        )
        require(event["event_id"] not in seen, "duplicate event ID")
        cause = event.get("caused_by_event_id")
        if cause is not None:
            require(
                cause in seen and seen[cause] <= event["sim_time_ns"],
                "causal event identity/time",
            )
        seen[event["event_id"]] = event["sim_time_ns"]
    ids, hops = set(), []
    if network:
        packet_rows = [
            r
            for r in rows
            if r["event_type"]
            in (
                "RESULT_TX",
                "RESULT_RX",
                "BACKGROUND_TX",
                "BACKGROUND_RX",
                "PAIR_READY_TX",
                "PAIR_READY_RX",
                "QUEUE_ENQUEUE",
                "QUEUE_DEQUEUE",
                "PHY_TX",
                "PHY_TX_END",
                "PHY_RX",
            )
        ]
        ids = {r["message_id"] for r in packet_rows}
        require(
            len(ids) == 3 * len(c["chains"]) + c["background"]["result"]["count"],
            "packet count",
        )
        hops = []
        for pid in ids:
            own = [r for r in packet_rows if r["message_id"] == pid]
            kind = own[0]["kind"]
            sid = own[0]["session_id"]
            app = "BACKGROUND" if kind == 4 else "PAIR_READY" if kind == 6 else "RESULT"
            tx, rx = only(own, app + "_TX"), only(own, app + "_RX")
            protocol = next(
                (s["protocol"] for s in c["sessions"] if s["session_id"] == sid), None
            )
            route = (
                [(2, 1, "BR"), (1, 0, "RA")]
                if kind == 6
                else (
                    [(0, 1, "AR"), (1, 2, "RB")] if protocol == "teleport" else [(1, 2, "RB")]
                )
            )
            require(
                tx["node_id"] == route[0][0] and rx["node_id"] == route[-1][1],
                "packet route endpoints",
            )
            require(len(own) == 2 + 5 * len(route), "packet trace multiplicity")
            expected_bytes = (
                16
                if kind == 6
                else (
                    2
                    if protocol == "teleport"
                    else (
                        c["background"]["result"]["payload_bytes"]
                        if kind == 4
                        else c["result_payload_bytes"]
                    )
                )
            )
            require(
                tx["packet_bytes"] == rx["packet_bytes"] == expected_bytes,
                "application payload bytes",
            )
            if kind == 4:
                f = c["background"]["result"]
                i = pid - 100000
                require(
                    sid == 0
                    and 0 <= i < f["count"]
                    and tx["time_ns"] == f["start_ns"] + i * f["interval_ns"],
                    "background schedule",
                )
            at = tx["time_ns"]
            for src, dst, link in route:
                stages = {
                    name: only(
                        [r for r in own if r["node_id"] == (dst if name == "PHY_RX" else src)],
                        name,
                    )
                    for name in (
                        "QUEUE_ENQUEUE",
                        "QUEUE_DEQUEUE",
                        "PHY_TX",
                        "PHY_TX_END",
                        "PHY_RX",
                    )
                }
                cfg = c["access_link"] if link in ("AR", "RA") else c["result_link"]
                duration = tx_duration_ns(expected_bytes, cfg["rate_bps"])
                en, de, pt, end, pr = [
                    stages[k]["time_ns"]
                    for k in (
                        "QUEUE_ENQUEUE",
                        "QUEUE_DEQUEUE",
                        "PHY_TX",
                        "PHY_TX_END",
                        "PHY_RX",
                    )
                ]
                require(
                    en == at
                    and de == pt
                    and end == pt + duration
                    and pr == end + cfg["delay_ns"],
                    "packet hop timing " + link,
                )
                require(
                    all(r["packet_bytes"] == expected_bytes + 30 for r in stages.values()),
                    "wire byte accounting",
                )
                hops.append(
                    dict(
                        link=link,
                        enqueue_ns=en,
                        dequeue_ns=de,
                        end_ns=end,
                        sequence=stages["QUEUE_ENQUEUE"]["source_sequence"],
                    )
                )
                at = pr
            require(rx["time_ns"] == at, "application receive timing")
        # Independent FIFO recurrence uses observed queue arrival/order as inputs;
        # multi-hop causality above determines those arrivals from prior deliveries.
        for link in ("AR", "RA", "RB", "BR"):
            free = 0
            for h in sorted(
                [h for h in hops if h["link"] == link],
                key=lambda h: (h["enqueue_ns"], h["sequence"]),
            ):
                require(
                    h["dequeue_ns"] == max(free, h["enqueue_ns"]),
                    "independent link FIFO " + link,
                )
                free = h["end_ns"]
    requests = snap["requests"]
    require(len(requests) == 4 * len(c["chains"]), "request count")
    resources = {(r["session_id"], r["handle"]): r for r in snap["resources"]}
    for name in ("R", "A", "B"):
        capacity = r_capacity if name == "R" else 1
        free = [0] * capacity
        own = sorted(
            [r for r in requests if r["processor_id"] == name],
            key=lambda r: (
                r["arrival_ns"],
                next(
                    e["sequence"]
                    for e in events
                    if e["event_type"] == r["operation"] + "_QUEUED"
                    and e["session_id"] == r["session_id"]
                ),
            ),
        )
        for r in own:
            duration = c[
                "bsm_duration_ns" if r["operation"] == "BSM" else "correction_duration_ns"
            ]
            slot = min(range(capacity), key=lambda i: free[i])
            require(
                r["state"] == "COMPLETED"
                and r["start_ns"] == max(free[slot], r["arrival_ns"])
                and r["completion_ns"] == r["start_ns"] + duration,
                "independent processor FIFO " + name,
            )
            free[slot] = r["completion_ns"]
    for chain in c["chains"]:
        swap, tele = chain["swap_session_id"], chain["teleport_session_id"]
        req = {(r["session_id"], r["operation"]): r for r in requests}
        require(
            req[swap, "BSM"]["processor_id"] == "R" and req[tele, "BSM"]["processor_id"] == "A",
            "BSM locality",
        )
        require(
            all(req[s, "CORRECTION"]["processor_id"] == "B" for s in (swap, tele)),
            "shared B processor",
        )
        require(
            req[swap, "BSM"]["arrival_ns"]
            == max(
                chain["session_start_ns"],
                max(q["delay_ns"] for q in c["quantum_links"].values()),
            ),
            "swap eligibility",
        )
        ready_tx, ready_rx = only(rows, "PAIR_READY_TX", tele), only(
            rows, "PAIR_READY_RX", tele
        )
        require(
            ready_tx["time_ns"] == req[swap, "CORRECTION"]["completion_ns"],
            "ready after native swap correction",
        )
        require(
            req[tele, "BSM"]["arrival_ns"]
            == max(chain["session_start_ns"], ready_rx["time_ns"]),
            "teleport waits for real ready packet",
        )
        transfer = [h for h in snap["handoffs"] if h["to_session_id"] == tele]
        require(
            len(transfer) == 1
            and transfer[0]["same_qubit_objects"]
            and transfer[0]["sim_time_ns"] == ready_tx["time_ns"],
            "actual pair handoff",
        )
        require(
            resources[swap, "output"]["state"] == "TRANSFERRED"
            and resources[tele, "epr"]["state"] == "CONSUMED",
            "pair consumed once",
        )
        require(
            tuple(resources[tele, "epr"]["origin"]) == (swap, "output")
            and resources[tele, "epr"]["locations"] == resources[swap, "output"]["locations"],
            "pair provenance",
        )
        require(
            resources[tele, "output"]["state"] == "USABLE"
            and resources[tele, "input"]["state"] == "CONSUMED",
            "teleport lifecycle",
        )
        for sid in (swap, tele):
            stage = snap["sessions"][str(sid)]
            require(stage["correction_count"] == 1, "one correction per stage")
            require(
                only(rows, "SESSION_START", sid)["time_ns"] == chain["session_start_ns"],
                "local session start",
            )
            for op in ("BSM", "CORRECTION"):
                r = req[sid, op]
                require(
                    only(rows, op + "_REQUEST", sid)["time_ns"] == r["arrival_ns"],
                    "Q2NS request boundary",
                )
                require(
                    only(rows, "RESULT_TX" if op == "BSM" else "RESULT_RX", sid)["time_ns"]
                    == (r["completion_ns"] if op == "BSM" else r["arrival_ns"]),
                    "native completion/packet boundary",
                )
            for name in ("RESULT_TX", "RESULT_RX", "CORRECTION_REQUEST"):
                row = only(rows, name, sid)
                require([row["m1"], row["m2"]] == stage["measurement_bits"], "actual Q2NS bits")
        deliveries = [
            d for d in snap["quantum_network"]["deliveries"] if d["session_id"] in (swap, tele)
        ]
        require(
            len(deliveries) == 2 and {d["session_id"] for d in deliveries} == {swap},
            "no fresh teleport EPR",
        )
    require(len(snap["handoffs"]) == len(c["chains"]), "handoff count")
    return dict(
        passed=True,
        checks=[
            "processor_FIFO",
            "causality",
            "actual_pair_handoff",
            "resource_lifecycle",
            "measurement_bits",
            "ready_delivery_gating",
        ]
        + (
            ["packet_routes", "per_hop_FIFO", "Q2NS_payloads", "sole_state_owner"]
            if network
            else ["fixed_delay_transport"]
        ),
        packets=len(ids),
        packet_hops=len(hops),
    )


def metrics(report):
    rows = report.get("ns3_events", report.get("transport_events"))
    req = {(r["session_id"], r["operation"]): r for r in report["snapshot"]["requests"]}
    chains = []
    for chain in report["config"]["chains"]:
        s, t = chain["swap_session_id"], chain["teleport_session_id"]
        start = chain["session_start_ns"]
        ready = only(rows, "PAIR_READY_RX", t)["time_ns"]
        delays = dict(
            resource_wait_ns=req[s, "BSM"]["arrival_ns"] - start,
            swap_R_wait_ns=req[s, "BSM"]["start_ns"] - req[s, "BSM"]["arrival_ns"],
            swap_bsm_ns=req[s, "BSM"]["completion_ns"] - req[s, "BSM"]["start_ns"],
            swap_packet_ns=req[s, "CORRECTION"]["arrival_ns"] - req[s, "BSM"]["completion_ns"],
            swap_B_wait_ns=req[s, "CORRECTION"]["start_ns"]
            - req[s, "CORRECTION"]["arrival_ns"],
            swap_correction_ns=req[s, "CORRECTION"]["completion_ns"]
            - req[s, "CORRECTION"]["start_ns"],
            ready_packet_ns=ready - req[s, "CORRECTION"]["completion_ns"],
            teleport_A_wait_ns=req[t, "BSM"]["start_ns"] - ready,
            teleport_bsm_ns=req[t, "BSM"]["completion_ns"] - req[t, "BSM"]["start_ns"],
            teleport_packet_ns=req[t, "CORRECTION"]["arrival_ns"]
            - req[t, "BSM"]["completion_ns"],
            teleport_B_wait_ns=req[t, "CORRECTION"]["start_ns"]
            - req[t, "CORRECTION"]["arrival_ns"],
            teleport_correction_ns=req[t, "CORRECTION"]["completion_ns"]
            - req[t, "CORRECTION"]["start_ns"],
        )
        latency = req[t, "CORRECTION"]["completion_ns"] - start
        require(sum(delays.values()) == latency, "end-to-end latency decomposition")
        stage = report["snapshot"]["sessions"][str(t)]
        chains.append(
            dict(
                chain_id=chain["chain_id"],
                swap_session_id=s,
                teleport_session_id=t,
                session_start_ns=start,
                completion_ns=req[t, "CORRECTION"]["completion_ns"],
                latency_ns=latency,
                ready_received_ns=ready,
                teleport_input_age_ns=req[t, "BSM"]["start_ns"],
                delays=delays,
                swap_fidelity=report["snapshot"]["sessions"][str(s)]["checkpoints"]["usable"][
                    "fidelity"
                ],
                input_at_teleport_fidelity=stage["checkpoints"]["bsm_start"]["input"][
                    "fidelity"
                ],
                output_fidelity=stage["checkpoints"]["usable"]["fidelity"],
            )
        )
    return dict(chains=chains, batch_completion_ns=max(r["completion_ns"] for r in chains))
