#!/usr/bin/env python3
"""Check the active quantum runtime and the two compiled ns-3 participants."""
import os
from pathlib import Path
import sys

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "python"))
from runtime import NS3_DIR


def main():
    try:
        import netsquid as ns
        import numpy as np
    except ImportError as error:
        sys.exit("Use the Python environment with NetSquid installed: " + str(error))
    print("Python: {} ({})".format(sys.version.split()[0], sys.executable))
    print("NetSquid: {} | NumPy: {}".format(ns.__version__, np.__version__))
    print("ns-3 root: " + str(NS3_DIR))
    for target in ("cosim-chained", "cosim-provisioned"):
        binaries = [
            p
            for p in (NS3_DIR / "build/contrib/cosim").rglob("*" + target + "*")
            if p.is_file() and os.access(str(p), os.X_OK)
        ]
        if len(binaries) != 1:
            sys.exit(
                "Expected one compiled {} participant; run ./ns3 build {}".format(target, target)
            )
        print(target + ": " + str(binaries[0]))
    print("Environment check passed. Run an example to check IPC and state validation.")


if __name__ == "__main__":
    main()
