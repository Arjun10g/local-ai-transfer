#!/usr/bin/env python3
"""Bounded, secret-free NVIDIA device placement receipt for CUDA eval."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


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
    receipt = {"schema": "local_bmo.j1m.cuda-device-receipt.v1", "status": "verified", "selector": "CUDA0", "device_count": 1, "device": rows[0], "source": "nvidia-smi bounded query"}
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
