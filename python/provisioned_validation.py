"""Validate packet timing, processor schedules and resource lifecycle."""

from provisioned_config import expected_timing, readiness


def require(ok, message):
    if not ok:
        raise RuntimeError("Hybrid validation: " + message)


def validate_report(report, expected=None, network=True, *, r_capacity=1):
    c = report["config"]
    snap = report["snapshot"]
    rows = report.get("ns3_events", [])
    events = report["events"]
    expected = expected if expected is not None else expected_timing(c)
    require(snap["time_ns"] == snap["netsquid_time_ns"], "clock mismatch")
    if network:
        require(report["ns3_time_ns"] == snap["time_ns"], "ns-3 clock mismatch")
        require(report["nodes"] == dict(A=0, R=1, B=2), "node mapping")
        require(
            not any(
                r["event_type"].startswith("COMMAND_") or r["node_id"] not in (1, 2) for r in rows
            ),
            "removed command path/node",
        )
    require(type(r_capacity) is int and 1 <= r_capacity <= len(c["sessions"]), "invalid R capacity")
    require(snap["R_execution_capacity"] == r_capacity, "R execution capacity")

    def times(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k.endswith("_ns") and v is not None:
                    require(type(v) is int, "integer time: " + k)
                times(v)
        elif isinstance(value, list):
            for v in value:
                times(v)

    times(report)
    if network:
        require(not any("DROP" in r["event_type"] for r in rows), "packet drop")
        require(
            report["q2ns_status"]
            == dict(
                native_state_count=0,
                native_qubit_count=0,
                sessions=len(c["sessions"]),
                corrections_applied=len(c["sessions"]),
            ),
            "Q2NS ownership/completion",
        )
        require(not any("DROP" in r["event_type"] for r in rows), "packet drop")
        packet_types = {
            "RESULT_TX",
            "BACKGROUND_TX",
            "RESULT_RX",
            "BACKGROUND_RX",
            "QUEUE_ENQUEUE",
            "QUEUE_DEQUEUE",
            "PHY_TX",
            "PHY_TX_END",
            "PHY_RX",
        }
        packet_rows = [r for r in rows if r["event_type"] in packet_types]
        require(
            {r["message_id"] for r in packet_rows} == {p["packet_id"] for p in expected["packets"]},
            "packet IDs",
        )
        for p in expected["packets"]:
            own = [r for r in packet_rows if r["message_id"] == p["packet_id"]]
            tx = "BACKGROUND_TX" if not p["session_id"] else "RESULT_TX"
            rx = tx.replace("TX", "RX")
            schedule = {
                tx: "app_tx_ns",
                rx: "app_rx_ns",
                "QUEUE_ENQUEUE": "enqueue_ns",
                "QUEUE_DEQUEUE": "dequeue_ns",
                "PHY_TX": "phy_tx_ns",
                "PHY_TX_END": "phy_tx_end_ns",
                "PHY_RX": "phy_rx_ns",
            }
            require(len(own) == len(schedule), "packet trace count")
            for kind, key in schedule.items():
                matches = [r for r in own if r["event_type"] == kind]
                require(
                    len(matches) == 1 and matches[0]["time_ns"] == p[key],
                    "FIFO packet timing " + kind,
                )
                r = matches[0]
                require(r["session_id"] == p["session_id"], "packet session")
                require(r["node_id"] == (2 if kind in (rx, "PHY_RX") else 1), "packet endpoint")
                require(
                    r["packet_bytes"] == p["payload_bytes"] + (0 if kind in (tx, rx) else 30),
                    "wire bytes",
                )
    requests = snap["requests"]
    require(len(requests) == 2 * len(c["sessions"]), "request count")
    for s in c["sessions"]:
        sid = s["session_id"]
        out = snap["sessions"][str(sid)]
        pred = expected["sessions"][str(sid)]
        require(
            out["protocol"] == s["protocol"] and out["correction_count"] == 1, "protocol completion"
        )
        require(out["output"]["state"] == "USABLE", "output not usable")
        for op in ("BSM", "CORRECTION"):
            req = [r for r in requests if r["session_id"] == sid and r["operation"] == op]
            require(len(req) == 1, "unique operation")
            r = req[0]
            require(
                r["state"] == "COMPLETED" and r["protocol"] == s["protocol"],
                "operation terminal state",
            )
            for field in ("arrival_ns", "start_ns", "completion_ns"):
                require(r[field] == pred[op.lower() + "_" + field], "quantum FIFO timing")
            require(r["processor_id"] == ("R" if op == "BSM" else "B"), "shared processor mapping")
        if network:
            own = [r for r in rows if r["session_id"] == sid]
            prefix = "Q2NS_" if s["protocol"] == "swap" else "Q2NS_TELEPORT_"
            for suffix, key in [
                ("BSM_REQUEST", "bsm_arrival_ns"),
                ("BSM_DONE", "bsm_completion_ns"),
                ("CORRECTION_REQUEST", "correction_arrival_ns"),
                ("CORRECTION_APPLIED", "correction_completion_ns"),
            ]:
                selected = [r for r in own if r["event_type"] == prefix + suffix]
                require(
                    len(selected) == 1 and selected[0]["time_ns"] == pred[key],
                    "native app " + suffix,
                )
            for name in ("RESULT_TX", "RESULT_RX", "CORRECTION_REQUEST"):
                selected = [r for r in own if r["event_type"] == name]
                require(
                    len(selected) == 1
                    and [selected[0]["m1"], selected[0]["m2"]] == out["measurement_bits"],
                    "actual payload bits",
                )
            local = [r for r in own if r["event_type"] == "SESSION_START"]
            bsm = [r for r in own if r["event_type"] == "BSM_REQUEST"]
            ready = [r for r in own if r["event_type"] == "Q2NS_RESOURCES_READY"]
            eligible = [r for r in own if r["event_type"] == "SESSION_ELIGIBLE"]
            require(
                len(local) == len(bsm) == len(ready) == len(eligible) == 1,
                "activation/readiness uniqueness",
            )
            require(
                local[0]["time_ns"] == s["session_start_ns"]
                and ready[0]["time_ns"] == readiness(c, s)
                and eligible[0]["time_ns"] == pred["eligible_ns"]
                and bsm[0]["time_ns"] == pred["eligible_ns"],
                "resource-gated session activation",
            )
            require(
                bsm[0]["cause"] == eligible[0]["event_id"]
                and eligible[0]["cause"] in (local[0]["event_id"], ready[0]["event_id"]),
                "readiness causal chain",
            )
            quantum_ready = [
                e for e in events if e["event_type"] == "RESOURCES_READY" and e["session_id"] == sid
            ]
            require(
                len(quantum_ready) == 1 and ready[0]["cause"] == quantum_ready[0]["event_id"],
                "ready notification provenance",
            )
            require(
                out["resource_ready_ns"] == readiness(c, s)
                and pred["bsm_start_ns"] >= out["resource_ready_ns"],
                "BSM before resource ready",
            )
        resources = [r for r in snap["resources"] if r["session_id"] == sid]
        require(len(resources) == 3, "resource lifecycle count")
        for r in resources:
            require(
                r["owner"] is None
                and r["state"] == ("USABLE" if r["handle"] == "output" else "CONSUMED"),
                "resource lifecycle",
            )
        if s["protocol"] == "teleport":
            require(
                out["output"]["kind"] == "qubit"
                and out["logical_handles"] == dict(input=201, epr=202, output=203),
                "teleport handles",
            )
    known = set()
    last = -1
    for sequence, e in enumerate(events):
        require(e["sequence"] == sequence and e["sim_time_ns"] >= last, "log sequence/time")
        require(e["event_id"] not in known, "event ID reused")
        require(
            e["caused_by_event_id"] is None or e["caused_by_event_id"] in known,
            "causal parent missing/future",
        )
        known.add(e["event_id"])
        last = e["sim_time_ns"]
    require(snap["time_ns"] == expected["simulation_completion_ns"], "final drain boundary")
    from native_validation import validate_report as validate_native

    validate_native(report, expected, network=False, r_capacity=r_capacity)
    validate_provisioning(report)
    return dict(
        passed=True,
        checks=(
            ["packet_FIFO", "native_apps", "local_session_start", "single_state_owner"]
            if network
            else []
        )
        + [
            "processor_FIFO",
            "resource_lifecycle",
            "causal_log",
            "integer_time",
            "channel_delivery",
            "resource_readiness",
            "native_instruction_chain",
            "native_program_done",
        ],
        expected_timing=expected,
    )


def validate_provisioning(report):
    c = report["config"]
    snap = report["snapshot"]
    net = snap["quantum_network"]
    require(
        set(net["nodes"]) == {"A", "R", "B"} and set(net["channels"]) == {"RA", "RB"},
        "native quantum topology",
    )
    require(
        net["ready_notification"] == "ideal_zero_delay"
        and net["loss_model"] == "none"
        and net["transit_memory_noise"] is False,
        "provisioning assumptions",
    )
    for s in c["sessions"]:
        sid = s["session_id"]
        deliveries = [d for d in net["deliveries"] if d["session_id"] == sid]
        links = {"RA", "RB"} if s["protocol"] == "swap" else {"RB"}
        require(
            len(deliveries) == len(links) and {d["channel"] for d in deliveries} == links,
            "delivery session isolation",
        )
        for d in deliveries:
            require(
                d["send_ns"] == 0
                and d["delivery_ns"] == c["quantum_links"][d["channel"]]["delay_ns"]
                and d["placement_source"] == "quantum_channel_port",
                "actual quantum channel delivery",
            )
            resources = [
                r
                for r in snap["resources"]
                if r["session_id"] == sid and r["handle"] == d["resource_handle"]
            ]
            require(
                len(resources) == 1
                and resources[0]["created_ns"] == 0
                and resources[0]["ready_ns"] == d["delivery_ns"],
                "resource creation and readiness",
            )
            events = [
                e
                for e in report["events"]
                if e["session_id"] == sid and e.get("resource_handle") == d["resource_handle"]
            ]
            names = ["EPR_CREATED", "QCHANNEL_SEND", "QCHANNEL_DELIVERED", "RESOURCE_AVAILABLE"]
            selected = [e for e in events if e["event_type"] in names]
            require([e["event_type"] for e in selected] == names, "quantum delivery lifecycle")
            require(
                [e["sim_time_ns"] for e in selected] == [0, 0, d["delivery_ns"], d["delivery_ns"]],
                "delivery trace times",
            )
            require(
                all(
                    b["caused_by_event_id"] == a["event_id"] for a, b in zip(selected, selected[1:])
                ),
                "delivery causal chain",
            )
