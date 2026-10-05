"""Check native instruction ordering and program-done callbacks."""

from session_validation import require, validate_report as validate_common


def validate_report(report, expected=None, network=True):
    result = validate_common(report, expected, network)
    durations = report["config"]["native_instructions"]
    keys = {
        "CNOT": "cnot_ns",
        "H": "h_ns",
        "MEASURE_0": "measure_ns",
        "MEASURE_1": "measure_ns",
        "X": "x_ns",
        "IDLE_X": "x_ns",
        "Z": "z_ns",
        "IDLE_Z": "z_ns",
    }
    for request in report["snapshot"]["requests"]:
        sid = request["session_id"]
        op = request["operation"]
        own = [
            r
            for r in report["native_instructions"]
            if r["session_id"] == sid and r["operation"] == op
        ]
        bits = report["snapshot"]["sessions"][str(sid)]["measurement_bits"]
        names = (
            ["CNOT", "H", "MEASURE_0", "MEASURE_1"]
            if op == "BSM"
            else ["X" if bits[1] else "IDLE_X", "Z" if bits[0] else "IDLE_Z"]
        )
        require([r["instruction"] for r in own] == names, "native instruction sequence")
        previous = request["start_ns"]
        for index, r in enumerate(own):
            require(
                r["index"] == index and r["start_ns"] == previous, "native instruction ordering"
            )
            require(
                r["completion_ns"] - r["start_ns"] == durations[keys[r["instruction"]]],
                "native instruction duration",
            )
            previous = r["completion_ns"]
        require(previous == request["completion_ns"], "native operation/program end")
        callbacks = [
            e
            for e in report["events"]
            if e["event_type"] == op + "_COMPLETE" and e["session_id"] == sid
        ]
        require(
            len(callbacks) == 1
            and callbacks[0].get("completion_source") == "netsquid_program_done",
            "native completion source",
        )
    result["checks"] += ["native_instruction_chain", "native_program_done"]
    return result
