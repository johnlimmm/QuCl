#!/usr/bin/env python3
"""Run QuCl Python integration tests and the patched Q2NS native test suites."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
from runtime import NS3_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--python-only", action="store_true")
    group.add_argument("--native-only", action="store_true")
    args = parser.parse_args()
    if not args.native_only:
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(MODULE / "tests"), "-v"],
            check=True,
        )
    if args.python_only:
        return
    runners = [
        p
        for p in (NS3_DIR / "build/utils").glob("*test-runner*")
        if p.is_file() and os.access(str(p), os.X_OK)
    ]
    if len(runners) != 1:
        parser.error("Build q2ns-test and test-runner before running native tests")
    runner = str(runners[0])
    names = subprocess.check_output(
        [runner, "--print-test-name-list"], universal_newlines=True
    ).splitlines()
    suites = [name for name in names if name.startswith("q2ns-")]
    if not {"q2ns-swap-external", "q2ns-teleport-external"}.issubset(suites):
        parser.error("The patched Q2NS test suites were not built")
    for name in suites:
        subprocess.run([runner, "--suite=" + name, "--verbose"], check=True)
    label = (
        "All {} Q2NS suites passed."
        if args.native_only
        else "All QuCl Python tests and {} Q2NS suites passed."
    )
    print(label.format(len(suites)))


if __name__ == "__main__":
    main()
