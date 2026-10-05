"""Shared runner paths, failure reports and session metrics."""

import gzip
import json
import os
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
NS3_DIR = Path(os.environ.get("QUCL_NS3_ROOT", str(MODULE_DIR.parents[1]))).resolve()


class HybridValidationFailure(RuntimeError):
    def __init__(self, report, error):
        super().__init__(str(error))
        self.report = report


def session_metrics(report):
    """Latency starts at the local Q2NS session activation, before the R FIFO."""
    rows = []
    packets = {
        p["session_id"]: p
        for p in report["validation"]["expected_timing"]["packets"]
        if p["session_id"]
    }
    for s in report["config"]["sessions"]:
        sid = s["session_id"]
        req = {r["operation"]: r for r in report["snapshot"]["requests"] if r["session_id"] == sid}
        b, c = req["BSM"], req["CORRECTION"]
        p = packets[sid]
        rows.append(
            dict(
                session_id=sid,
                session_start_ns=s["session_start_ns"],
                quantum_wait_ns=b["start_ns"] - b["arrival_ns"],
                correction_wait_ns=c["start_ns"] - c["arrival_ns"],
                bsm_start_ns=b["start_ns"],
                bsm_completion_ns=b["completion_ns"],
                result_delay_ns=c["arrival_ns"] - b["completion_ns"],
                result_queue_ns=p["queue_wait_ns"],
                correction_start_ns=c["start_ns"],
                completion_ns=c["completion_ns"],
                transaction_latency_ns=c["completion_ns"] - s["session_start_ns"],
                usable_fidelity=report["snapshot"]["sessions"][str(sid)]["checkpoints"]["usable"][
                    "fidelity"
                ],
            )
        )
    return dict(sessions=rows)


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(str(path), "wt") as stream:
        json.dump(report, stream, sort_keys=True, allow_nan=False)
