"""Run actual Q2NS density-matrix operations in one native ns-3 process.

The executable uses no NetSquid, socket or cross-simulator event coordinator. The
Python launcher shares QuCl's configuration parser only, and explicitly rejects
noise this comparison adapter does not implement. Configured QuCl instruction
durations are retained as metadata; Q2NS applies operations instantaneously.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile

import numpy as np
from chained_validation import only, require, validate_packets
from runtime import NS3_DIR


def batch_config(c):
    values = [
        c["result_link"]["rate_bps"],
        c["result_link"]["delay_ns"],
        c["result_payload_bytes"],
        c["queue_packets"],
        len(c["sessions"]),
        c["access_link"]["rate_bps"],
        c["access_link"]["delay_ns"],
    ]
    lines = ["CONFIG " + " ".join(map(str, values))]
    for s in c["sessions"]:
        lines.append(
            "SESSION {} {} root:{} {} {}".format(
                s["session_id"],
                s["session_start_ns"],
                s["session_id"],
                int(s["protocol"] == "teleport"),
                s["parent_session_id"],
            )
        )
    f = c["background"]["result"]
    lines.append(
        "FLOW result {} {} {} {}".format(
            f["start_ns"], f["interval_ns"], f["count"], f["payload_bytes"]
        )
    )
    lines.append(
        "NATIVE {} {} {}".format(
            c["seed"],
            c["quantum_links"]["RA"]["delay_ns"],
            c["quantum_links"]["RB"]["delay_ns"],
        )
    )
    lines += [
        "INPUT {} {}".format(ch["swap_session_id"], ch["input_state"]) for ch in c["chains"]
    ]
    return "\n".join(lines) + "\n"


def parse_output(text, config):
    events, resources, outputs = [], [], {}
    done = None
    for line in text.splitlines():
        p = line.split()
        if p[0] == "HELLO":
            require(p == ["HELLO", "COSIM_CHAINED", "5"], "native batch version")
        elif p[0] == "TRACE":
            require(len(p) == 17 and int(p[1]) == len(events), "native trace order")
            row = dict(
                zip(
                    (
                        "source_sequence",
                        "time_ns",
                        "event_type",
                        "node_id",
                        "message_id",
                        "cause",
                        "session_id",
                        "request_id",
                        "pair_a",
                        "pair_b",
                        "output_pair_id",
                        "m1",
                        "m2",
                        "packet_bytes",
                        "kind",
                        "queue_depth",
                    ),
                    [v if i in (3, 6) else int(v) for i, v in enumerate(p[1:], 1)],
                )
            )
            row["event_id"] = "n:" + p[1]
            events.append(row)
        elif p[0] == "NATIVE_RESOURCE":
            require(len(p) == 6, "native resource format")
            resources.append(
                dict(
                    event=p[1],
                    session_id=int(p[2]),
                    time_ns=int(p[3]),
                    qubit_ids=[int(p[4]), int(p[5])],
                )
            )
        elif p[0] == "NATIVE_STATE":
            require(len(p) == 12 and p[1] not in outputs, "native output format")
            values = list(map(float, p[4:]))
            outputs[p[1]] = dict(
                completion_ns=int(p[2]),
                output_fidelity=float(p[3]),
                density_matrix=dict(
                    real=np.array(values[::2]).reshape(2, 2).tolist(),
                    imag=np.array(values[1::2]).reshape(2, 2).tolist(),
                ),
            )
        elif p[0] == "NATIVE_DONE":
            require(done is None and len(p) == 4, "native completion format")
            done = dict(
                time_ns=int(p[1]), corrections_applied=int(p[2]), native_states=int(p[3])
            )
        else:
            raise RuntimeError("unexpected native output: " + line)
    require(done is not None, "missing native completion")
    return dict(
        schema_version=1,
        model="Q2NS-only",
        engine="ns-3/Q2NS",
        config=config,
        quantum_state_owner="Q2NS-QStateDM",
        quantum_timing="instantaneous",
        memory_noise="disabled",
        gate_noise="disabled",
        ipc_messages=0,
        ns3_events=events,
        native_resources=resources,
        outputs=outputs,
        status=done,
    )


def validate_native(report):
    c, rows = report["config"], report["ns3_events"]
    validate_packets(c, rows)
    require(
        report["status"]["corrections_applied"] == 2 * len(c["chains"]), "native corrections"
    )
    require(report["status"]["native_states"] > 0, "native quantum ownership")
    seen = {"root:" + str(s["session_id"]): 0 for s in c["sessions"]}
    for r in rows:
        require(
            r["time_ns"] >= 0 and (not seen or r["time_ns"] >= max(seen.values())),
            "native monotonic clock",
        )
        require(r["cause"] == "-" or r["cause"] in seen, "native causal parent")
        require(r["event_id"] not in seen, "native duplicate event")
        seen[r["event_id"]] = r["time_ns"]
    metrics, max_error = {}, 0.0
    target = {
        "0": [1, 0],
        "1": [0, 1],
        "+": [1, 1],
        "-": [1, -1],
        "+i": [1, 1j],
        "-i": [1, -1j],
    }
    require(
        set(report["outputs"]) == {str(ch["swap_session_id"]) for ch in c["chains"]},
        "native output identities",
    )
    for ch in c["chains"]:
        swap, tele = ch["swap_session_id"], ch["teleport_session_id"]

        def at(kind, sid):
            return only(rows, kind, sid)["time_ns"]

        bsm = at("BSM_REQUEST", swap)
        require(
            bsm
            == max(
                ch["session_start_ns"], *(q["delay_ns"] for q in c["quantum_links"].values())
            ),
            "native ready-gated swap start",
        )
        require(
            at("BSM_REQUEST", tele) == max(ch["session_start_ns"], at("PAIR_READY_RX", tele)),
            "native ready-packet gating",
        )
        bits = []
        for sid in (swap, tele):
            require(at("RESULT_TX", sid) == at("BSM_REQUEST", sid), "native instantaneous BSM")
            require(
                at("CORRECTION_REQUEST", sid) == at("RESULT_RX", sid),
                "native correction arrival",
            )
            kind = (
                "Q2NS_CORRECTION_APPLIED" if sid == swap else "Q2NS_TELEPORT_CORRECTION_APPLIED"
            )
            require(at(kind, sid) == at("RESULT_RX", sid), "native instantaneous correction")
            tx, rx = only(rows, "RESULT_TX", sid), only(rows, "RESULT_RX", sid)
            require((tx["m1"], tx["m2"]) == (rx["m1"], rx["m2"]), "native measurement payload")
            bits.extend([tx["m1"], tx["m2"]])
        require(at("PAIR_READY_TX", tele) == at("RESULT_RX", swap), "native readiness creation")
        own = [r for r in report["native_resources"] if r["session_id"] == swap]

        def resource(kind):
            found = [r for r in own if r["event"] == kind]
            require(len(found) == 1, "native resource " + kind)
            return found[0]

        born, handoff, consumed = [resource(k) for k in ("CREATED", "HANDOFF", "CONSUMED")]
        require(
            born["time_ns"] == 0
            and len(set(born["qubit_ids"])) == 2
            and born["qubit_ids"] == handoff["qubit_ids"] == consumed["qubit_ids"],
            "same native physical pair",
        )
        require(
            handoff["time_ns"] == at("RESULT_RX", swap)
            and consumed["time_ns"] == at("BSM_REQUEST", tele),
            "native handoff times",
        )
        delivered = [r for r in own if r["event"] == "DELIVERED"]
        require(
            len(delivered) == 2
            and sorted(r["time_ns"] for r in delivered)
            == sorted(q["delay_ns"] for q in c["quantum_links"].values()),
            "native quantum delivery",
        )
        out = report["outputs"][str(swap)]
        require(out["completion_ns"] == at("RESULT_RX", tele), "native output completion")
        psi = np.asarray(target[ch["input_state"]], dtype=complex)
        psi /= np.linalg.norm(psi)
        rho = np.asarray(out["density_matrix"]["real"]) + 1j * np.asarray(
            out["density_matrix"]["imag"]
        )
        error = float(np.max(np.abs(rho - np.outer(psi, psi.conj()))))
        max_error = max(error, max_error)
        require(
            error < 1e-12 and abs(out["output_fidelity"] - 1) < 1e-12, "native noiseless state"
        )
        metrics[str(ch["chain_id"])] = dict(
            input_state=ch["input_state"],
            joint_branch="".join(map(str, bits)),
            latency_ns=out["completion_ns"] - ch["session_start_ns"],
            completion_ns=out["completion_ns"],
            output_fidelity=out["output_fidelity"],
            swap_packet_ns=at("RESULT_RX", swap) - at("RESULT_TX", swap),
            ready_packet_ns=at("PAIR_READY_RX", tele) - at("PAIR_READY_TX", tele),
            teleport_packet_ns=at("RESULT_RX", tele) - at("RESULT_TX", tele),
        )
    return (
        dict(
            passed=True,
            max_density_matrix_error=max_error,
            checks=[
                "native_DM_state",
                "same_physical_pair",
                "packet_FIFO",
                "payload_bits",
                "quantum_delivery",
                "packet_gating",
                "instantaneous_execution",
                "causality",
            ],
        ),
        metrics,
    )


def run_q2ns(raw, binary=None):
    # Shared parsing imports NetSquid constants but performs no state execution.
    from chained_config import normalize_config
    from gate_noise import split_config

    raw, probability = split_config(raw)
    c = normalize_config(raw)
    c["gate_depolar_probability"] = probability
    if (
        probability
        or any(c["memory_noise"][k] for k in ("T1_ns", "T2_ns"))
        or any(q["depolar_rate_hz"] for q in c["quantum_links"].values())
    ):
        raise ValueError("Q2NS-only comparison supports explicit noise-off configurations only")
    if binary is None:
        found = [
            p
            for p in (NS3_DIR / "build/contrib/cosim").rglob("*cosim-chained*")
            if p.is_file() and os.access(str(p), os.X_OK)
        ]
        if len(found) != 1:
            raise RuntimeError("build cosim-chained first")
        binary = found[0]
    with tempfile.TemporaryDirectory(prefix="qucl-q2ns-native-") as folder:
        src, dst = Path(folder) / "config.txt", Path(folder) / "output.txt"
        src.write_text(batch_config(c))
        process = subprocess.run(
            [str(binary), "--nativeConfig=" + str(src), "--nativeOutput=" + str(dst)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if process.returncode:
            raise RuntimeError("native Q2NS failed: " + process.stderr.decode())
        report = parse_output(dst.read_text(), c)
    report["validation"], report["metrics"] = validate_native(report)
    return report
