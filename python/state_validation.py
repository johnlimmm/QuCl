"""Independent reference validation at native instruction and protocol checkpoints."""

import json
import numpy as np
from chained_validation import require
from reference_client import reference as run_reference


def chained_reference_spec(report, chain, enumerate_branches):
    """Complete identity of a deterministic independent reference calculation."""
    c = report["config"]
    req = {(r["session_id"], r["operation"]): r for r in report["snapshot"]["requests"]}
    ids = [chain["swap_session_id"], chain["teleport_session_id"]]
    stages = [report["snapshot"]["sessions"][str(s)] for s in ids]
    return dict(
        input_state=chain["input_state"],
        quantum_links=c["quantum_links"],
        memory_noise=c["memory_noise"],
        gate_depolar_probability=c["gate_depolar_probability"],
        native_instructions=c["native_instructions"],
        bits=[s["measurement_bits"] for s in stages],
        stages=[
            {
                op.lower(): {
                    k: req[s, op][k] for k in ("arrival_ns", "start_ns", "completion_ns")
                }
                for op in ("BSM", "CORRECTION")
            }
            for s in ids
        ],
        enumerate_branches=enumerate_branches,
    )


def cross_validate_chained(report, enumerate_branches=False, references=None):
    c = report["config"]
    results = {}
    maximum = 0.0
    for chain in c["chains"]:
        ids = [chain["swap_session_id"], chain["teleport_session_id"]]
        stages = [report["snapshot"]["sessions"][str(s)] for s in ids]
        spec = chained_reference_spec(report, chain, enumerate_branches)
        # An evaluation may reuse an independent reference only for an identical
        # complete specification, including input, noise, all times and outcomes.
        # The coupled native execution and every checkpoint comparison still run.
        key = json.dumps(spec, sort_keys=True)
        if references is not None and key in references:
            ref = references[key]
            if hasattr(references, "move_to_end"):
                references.move_to_end(key)
        else:
            ref = run_reference("chained", spec)
            if references is not None:
                references[key] = ref
        errors = {}

        def compare(label, left, right):
            a, b = left["density_matrix"], right["density_matrix"]
            require(
                a["qubit_order"] == b["qubit_order"]
                and left["sim_time_ns"] == right["sim_time_ns"],
                "reference identity " + label,
            )
            da = np.array(a["real"]) + 1j * np.array(a["imag"])
            db = np.array(b["real"]) + 1j * np.array(b["imag"])
            require(
                da.shape == db.shape and np.all(np.isfinite(da)) and np.all(np.isfinite(db)),
                "finite density matrix",
            )
            error = float(np.max(np.abs(da - db)))
            errors[label] = error
            require(error < 1e-10, "reference state mismatch " + label + " " + str(error))
            if "fidelity" in left and "fidelity" in right:
                require(
                    abs(left["fidelity"] - right["fidelity"]) < 1e-10,
                    "reference fidelity " + label,
                )

        observed = ref["observed"]
        require(observed["probability"] > 0, "impossible measurement branch")
        for i, stage in enumerate(stages):
            expected = observed["stages"][i]
            require(expected["timing"] == spec["stages"][i], "reference stage timing")
            for name in ("bsm_start", "frame", "packet_arrival", "correction_start", "usable"):
                if name == "bsm_start":
                    for part, state in stage["checkpoints"][name].items():
                        compare(
                            str(i) + name + part, state, expected["checkpoints"][name][part]
                        )
                else:
                    compare(
                        str(i) + name, stage["checkpoints"][name], expected["checkpoints"][name]
                    )
            native = [r for r in report["native_instructions"] if r["session_id"] == ids[i]]
            require(
                len(native) == len(expected["instructions"]) == 6, "reference instruction count"
            )
            for n, r in zip(native, expected["instructions"]):
                require(
                    all(
                        n[k] == r[k]
                        for k in (
                            "operation",
                            "index",
                            "instruction",
                            "start_ns",
                            "completion_ns",
                        )
                    ),
                    "reference gate timing",
                )
                compare(str(i) + n["operation"] + str(n["index"]), n["state"], r["state"])
        if enumerate_branches:
            require(
                len(ref["branches"]) == 16
                and abs(sum(b["probability"] for b in ref["branches"].values()) - 1) < 1e-10,
                "16 joint branch probabilities",
            )
        error = max(errors.values())
        maximum = max(maximum, error)
        results[str(chain["chain_id"])] = dict(
            passed=True, max_density_matrix_error=error, errors=errors, reference=ref
        )
    return dict(passed=True, max_density_matrix_error=maximum, chains=results)


def cross_validate_provisioned(report, references=None):
    cache = {} if references is None else references
    results = {}
    for sid, session in report["snapshot"]["sessions"].items():
        requests = {
            r["operation"]: r
            for r in report["snapshot"]["requests"]
            if str(r["session_id"]) == sid
        }
        spec = dict(
            gate_depolar_probability=report["config"]["gate_depolar_probability"],
            protocol=session["protocol"],
            input_state=session["input_state"],
            memory_noise=report["config"]["memory_noise"],
            native_instructions=report["config"]["native_instructions"],
            quantum_links=report["config"]["quantum_links"],
            bsm_start_ns=requests["BSM"]["start_ns"],
            bsm_completion_ns=requests["BSM"]["completion_ns"],
            correction_arrival_ns=requests["CORRECTION"]["arrival_ns"],
            correction_start_ns=requests["CORRECTION"]["start_ns"],
            correction_completion_ns=requests["CORRECTION"]["completion_ns"],
        )
        key = json.dumps(spec, sort_keys=True)
        if key not in cache:
            cache[key] = run_reference("provisioned", spec)
        ref = cache[key]
        require(ref["spec"] == spec, "native reference specification")
        probabilities = [v["probability"] for v in ref["branches"].values()]
        require(
            len(probabilities) == 4
            and all(np.isfinite(p) and 0 <= p <= 1 + 1e-12 for p in probabilities)
            and abs(sum(probabilities) - 1) <= 1e-12,
            "native branch probabilities",
        )
        bits = "".join(map(str, session["measurement_bits"]))
        branch = ref["branches"][bits]
        require(branch["probability"] > 0, "native impossible branch")
        for b in ref["branches"].values():
            require(
                b["timing"]
                == {
                    k: spec[k]
                    for k in (
                        "bsm_start_ns",
                        "bsm_completion_ns",
                        "correction_arrival_ns",
                        "correction_start_ns",
                        "correction_completion_ns",
                    )
                },
                "native reference timing",
            )
        errors = {}
        ferrors = {}

        def compare(label, a, b):
            da, db = a["density_matrix"], b["density_matrix"]
            require(
                a["sim_time_ns"] == b["sim_time_ns"] and da["qubit_order"] == db["qubit_order"],
                "native state identity " + label,
            )
            left = np.array(da["real"]) + 1j * np.array(da["imag"])
            right = np.array(db["real"]) + 1j * np.array(db["imag"])
            require(
                left.shape == right.shape
                and np.all(np.isfinite(left))
                and np.all(np.isfinite(right)),
                "native state shape",
            )
            error = float(np.max(np.abs(left - right)))
            errors[label] = error
            require(error <= 1e-12, "native reference state " + label)
            if "fidelity" in a:
                error = abs(a["fidelity"] - b["fidelity"])
                ferrors[label] = error
                require(
                    np.isfinite(error) and error <= 1e-12, "native reference fidelity " + label
                )

        cp = session["checkpoints"]
        reference = branch["checkpoints"]
        require(
            branch["resource_ready_ns"] == session["resource_ready_ns"],
            "reference resource-ready time",
        )
        for name in reference["resources_ready"]:
            compare(
                "resources_ready." + name,
                cp["resources_ready"][name],
                reference["resources_ready"][name],
            )
        for delivery in report["snapshot"]["quantum_network"]["deliveries"]:
            if str(delivery["session_id"]) == sid:
                compare(
                    "delivery." + delivery["channel"],
                    delivery["state"],
                    branch["deliveries"][delivery["channel"]],
                )
        for name in reference["bsm_start"]:
            compare("bsm_start." + name, cp["bsm_start"][name], reference["bsm_start"][name])
        for name in ("frame", "packet_arrival", "correction_start", "usable"):
            compare(name, cp[name], reference[name])
        actual = [r for r in report["native_instructions"] if str(r["session_id"]) == sid]
        require(len(actual) == len(branch["instructions"]), "reference instruction count")
        for a, b in zip(actual, branch["instructions"]):
            require(
                all(
                    a[k] == b[k]
                    for k in ("operation", "index", "instruction", "start_ns", "completion_ns")
                ),
                "reference instruction timestamps",
            )
            # Before the first measurement, the state is unconditional. Between
            # measurements, the reference has projected only the observed prefix.
            if report["instruction_state_recording"]:
                require(a["state"] is not None, "missing native instruction state")
                compare(a["operation"] + "." + a["instruction"], a["state"], b["state"])
        results[sid] = dict(
            passed=True,
            observed_branch=bits,
            checkpoint_errors=errors,
            max_density_matrix_error=max(errors.values()),
            max_fidelity_error=max(ferrors.values()),
            reference=ref,
        )
    return dict(
        passed=True,
        sessions=results,
        max_density_matrix_error=max(v["max_density_matrix_error"] for v in results.values()),
    )
