#!/usr/bin/env python3
"""Bounded prerequisite probe for the remote CUDA build host."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


_VERSION = re.compile(r"(?i)(?:version|release)\s+(\d+)\.(\d+)")
_MAX_OUTPUT = 8192
_REQUIRED = {
    "python3": (3, 8),
    "git": (2, 30),
    "cmake": (3, 18),
    "g++": (9, 0),
    "nvcc": (11, 0),
}


def _probe(binary: str) -> dict[str, object]:
    try:
        result = subprocess.run([binary, "--version"], check=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{binary}_unavailable") from exc
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or len(output.encode("utf-8")) > _MAX_OUTPUT:
        raise RuntimeError(f"{binary}_version_failed")
    match = _VERSION.search(output)
    if match is None:
        # Python uses "Python 3.x" and g++ uses a bare leading version.
        match = re.search(r"(?:Python\s+)?(\d+)\.(\d+)", output)
    if match is None:
        raise RuntimeError(f"{binary}_version_unparseable")
    major, minor = int(match.group(1)), int(match.group(2))
    minimum = _REQUIRED[binary]
    if (major, minor) < minimum:
        raise RuntimeError(f"{binary}_version_too_old")
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")[:240]
    return {"major": major, "minor": minor, "reported": first_line}


def probe(output: Path) -> dict[str, object]:
    versions = {binary: _probe(binary) for binary in _REQUIRED}
    receipt: dict[str, object] = {
        "schema": "local_bmo.j1m.remote-toolchain-receipt.v1",
        "status": "verified",
        "required": {key: f">={value[0]}.{value[1]}" for key, value in _REQUIRED.items()},
        "versions": versions,
        "package_install": "ubuntu apt repositories; resolved package versions captured by reported tool versions",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.output), sort_keys=True))
    except (OSError, RuntimeError) as exc:
        print(f"remote toolchain refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
