"""Persistent isolated NetSquid reference workers; each branch resets its simulator."""

import atexit
import json
import select
import subprocess
import sys
import tempfile

WORKERS = {}
REQUESTS = {}
ERRORS = {}
MAX_REQUESTS = 16


def close_worker(mode):
    p = WORKERS.pop(mode, None)
    if p is not None and p.poll() is None:
        p.stdin.close()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    if p is not None:
        p.stdout.close()
    if mode in ERRORS:
        ERRORS.pop(mode).close()
    REQUESTS.pop(mode, None)


def reference(mode, spec):
    # Bound native component/cache lifetime; each calculation still starts from
    # sim_reset() in the independent reference. A file avoids stderr pipe stalls.
    if REQUESTS.get(mode, 0) >= MAX_REQUESTS:
        close_worker(mode)
    if mode not in WORKERS:
        ERRORS[mode] = tempfile.TemporaryFile(mode="w+t")
        WORKERS[mode] = subprocess.Popen(
            [sys.executable, __file__, mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=ERRORS[mode],
            universal_newlines=True,
            bufsize=1,
        )
        REQUESTS[mode] = 0
    p = WORKERS[mode]
    p.stdin.write(json.dumps(spec) + "\n")
    p.stdin.flush()
    if not select.select([p.stdout], [], [], 90)[0]:
        ERRORS[mode].seek(0)
        details = ERRORS[mode].read()[-8192:]
        close_worker(mode)
        raise RuntimeError("reference worker timeout: " + mode + "\n" + details)
    line = p.stdout.readline()
    if not line:
        ERRORS[mode].seek(0)
        details = ERRORS[mode].read()[-8192:]
        close_worker(mode)
        raise RuntimeError("reference worker failed: " + details)
    REQUESTS[mode] += 1
    return json.loads(line)


def close_workers():
    for mode in list(WORKERS):
        close_worker(mode)


atexit.register(close_workers)

if __name__ == "__main__":
    # Workers import only the standalone reference circuit, never the co-sim core.
    if sys.argv[1] == "chained":
        from reference_chained import run_reference
    elif sys.argv[1] == "provisioned":
        from reference_provisioned import run_reference
    else:
        raise ValueError("unknown reference mode")
    for line in sys.stdin:
        value = run_reference(json.loads(line))
        sys.stdout.write(json.dumps(value, allow_nan=False) + "\n")
        sys.stdout.flush()
