#!/usr/bin/env python3
"""Apply and verify the external-execution patches on the pinned Q2NS checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

MODULE = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root)] + list(args), universal_newlines=True
    ).strip()


def apply(root):
    root = root.resolve()
    manifests = [
        json.loads(p.read_text()) for p in sorted((MODULE / "integrations").glob("*.json"))
    ]
    expected = {}
    patches = []
    for manifest in manifests:
        patch = MODULE / "integrations" / manifest["patch"]
        if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
            raise RuntimeError("patch checksum mismatch: " + patch.name)
        if git(root, "rev-parse", "HEAD") != manifest["q2ns_base_commit"]:
            raise RuntimeError("Q2NS must be checked out at " + manifest["q2ns_base_commit"])
        expected.update(manifest["source_sha256"])
        patches.append(str(patch))

    def matches():
        return all(
            (root / name).is_file()
            and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            for name, digest in expected.items()
        )

    if matches():
        print("Q2NS integration already applied; all patched files verified.")
        return
    if git(root, "status", "--porcelain"):
        raise RuntimeError("Q2NS has local changes; use a clean pinned checkout")
    git(root, "apply", "--check", *patches)
    git(root, "apply", *patches)
    if not matches():
        raise RuntimeError("patched source checksum mismatch")
    print("Applied both Q2NS patches; all patched files verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("q2ns_root", type=Path)
    args = parser.parse_args()
    try:
        apply(args.q2ns_root)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
