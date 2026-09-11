#!/usr/bin/env python3
"""Bounded, secret-free NVIDIA device placement receipt for CUDA eval."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path



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


def probe(output: Path, expected_memory_mib: int = 70000) -> dict[str, object]:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv,noheader,nounits"],
        check=False, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 16 * 1024:
        raise RuntimeError("nvidia_smi_failed")
    rows = []
    for line in result.stdout.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != 4:
            raise RuntimeError("nvidia_smi_shape_invalid")
        try:
            index, memory = int(parts[0]), int(parts[2])
        except ValueError as exc:
            raise RuntimeError("nvidia_smi_shape_invalid") from exc
        if index < 0 or not parts[1] or not parts[3] or memory < 0:
            raise RuntimeError("nvidia_smi_identity_invalid")
        rows.append({"index": index, "name": parts[1][:160], "memory_total_mib": memory, "driver_version": parts[3][:80]})
    if len(rows) != 1 or rows[0]["memory_total_mib"] < expected_memory_mib or "a100" not in str(rows[0]["name"]).lower():
        raise RuntimeError("expected_single_a100_80g_not_proven")
    receipt = {"schema": "local_bmo.j1m.cuda-device-receipt.v1", "status": "verified", "selector": "CUDA0", "device_count": 1, "device": rows[0], "source": "nvidia-smi bounded query", **_run_identity()}
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
        print(f"CUDA device probe refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
