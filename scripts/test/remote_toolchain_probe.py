#!/usr/bin/env python3
"""Bounded prerequisite probe for the remote CUDA build host."""
from __future__ import annotations

import argparse
import json
import os
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
    "nvcc": (12, 0),
}
_PACKAGES = ("ca-certificates", "cmake", "build-essential", "git", "python3", "python3-venv")



# Required run-identity binding.  The orchestrator uploads this file next to the
# uploaded config before the first receipt-producing command; the path is
# source-fixed on both sides so no caller, configuration value, or remote
# response can redirect it.  Every receipt this script publishes carries the
# binding, and the salvage transport refuses any receipt whose binding is
# missing or does not match the run that is fetching it -- that is what stops a
# receipt left behind by an earlier run being published as this run's evidence.
_RUN_IDENTITY_PATH = Path("/scratch/j1m/run-identity.json")
_RUN_IDENTITY_SCHEMA = "local_bmo.j1m.run-identity.v1"
_RUN_IDENTITY_FIELDS = ("run_id", "instance_id")
_RUN_IDENTITY_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _run_identity() -> dict[str, str]:
    """Return this run's receipt binding, or ``unbound`` when unprovable."""

    unbound = {field: "unbound" for field in _RUN_IDENTITY_FIELDS}
    try:
        raw = _RUN_IDENTITY_PATH.read_bytes()
        if len(raw) > 4096:
            return unbound
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return unbound
    if not isinstance(payload, dict) or payload.get("schema") != _RUN_IDENTITY_SCHEMA:
        return unbound
    resolved = {}
    for field in _RUN_IDENTITY_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not _RUN_IDENTITY_VALUE.match(value):
            return unbound
        resolved[field] = value
    return resolved


def _write_receipt(output: Path, receipt: dict[str, object]) -> None:
    receipt = {**receipt, **_run_identity()}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)


def _probe(binary: str, executable: str | None = None) -> dict[str, object]:
    executable = executable or binary
    try:
        result = subprocess.run([executable, "--version"], check=False, capture_output=True, text=True, timeout=20)
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
    return {"major": major, "minor": minor, "reported": first_line, "executable": executable}


def _package_versions() -> dict[str, str]:
    try:
        result = subprocess.run(["dpkg-query", "-W", "-f=${Package}=${Version}\n", *_PACKAGES], check=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("dpkg_query_unavailable") from exc
    output = result.stdout or ""
    if result.returncode != 0 or len(output.encode("utf-8")) > _MAX_OUTPUT:
        raise RuntimeError("dpkg_query_failed")
    versions: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, version = line.partition("=")
        if separator != "=" or name not in _PACKAGES or not version or name in versions:
            raise RuntimeError("dpkg_query_shape_invalid")
        versions[name] = version[:160]
    if set(versions) != set(_PACKAGES):
        raise RuntimeError("dpkg_query_missing_package")
    return versions


def probe(output: Path, *, nvcc: str) -> dict[str, object]:
    if nvcc != "/usr/local/cuda/bin/nvcc":
        raise RuntimeError("nvcc_path_unapproved")
    versions = {binary: _probe(binary, nvcc if binary == "nvcc" else None) for binary in _REQUIRED}
    receipt: dict[str, object] = {
        "schema": "local_bmo.j1m.remote-toolchain-receipt.v1",
        "status": "verified",
        "required": {key: f">={value[0]}.{value[1]}" for key, value in _REQUIRED.items()},
        "versions": versions,
        "packages": _package_versions(),
        "package_install": "ubuntu apt repositories; exact resolved package versions captured by dpkg-query",
    }
    _write_receipt(output, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nvcc", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.output, nvcc=args.nvcc), sort_keys=True))
    except (OSError, RuntimeError) as exc:
        _write_receipt(args.output, {
            "schema": "local_bmo.j1m.remote-toolchain-receipt.v1",
            "status": "refused",
            "error_type": str(exc)[:120],
        })
        print(f"remote toolchain refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
