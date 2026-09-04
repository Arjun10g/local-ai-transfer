#!/usr/bin/env python3
"""Verify a remote J1M-produced Q4 against the accepted small manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
_MAX_MANIFEST = 256 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(artifact: Path, manifest_path: Path, output: Path) -> dict[str, object]:
    if artifact.name != MODEL_NAME or not artifact.is_file() or not manifest_path.is_file() or manifest_path.stat().st_size > _MAX_MANIFEST:
        raise ValueError("remote_q4_input_invalid")
    lock_path = manifest_path.with_name("model-manifest.sha256")
    if not lock_path.is_file() or lock_path.stat().st_size > 256:
        raise ValueError("remote_q4_manifest_lock_missing")
    lock_parts = lock_path.read_text(encoding="utf-8").strip().split()
    if len(lock_parts) != 2 or lock_parts[1] != manifest_path.name or len(lock_parts[0]) != 64 or any(char not in "0123456789abcdef" for char in lock_parts[0]):
        raise ValueError("remote_q4_manifest_lock_invalid")
    manifest_digest = sha256(manifest_path)
    if manifest_digest != lock_parts[0]:
        raise ValueError("remote_q4_manifest_lock_mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("artifact") if isinstance(manifest, dict) else None
    if not isinstance(expected, dict) or expected.get("expected_file_name") != MODEL_NAME or expected.get("quantization_profile") != "Q4_K_M" or expected.get("modality_profile") != "text_only_no_mmproj":
        raise ValueError("remote_q4_manifest_invalid")
    observed = {"size_bytes": artifact.stat().st_size, "sha256": sha256(artifact)}
    if observed != {"size_bytes": expected.get("expected_size_bytes"), "sha256": expected.get("sha256")}:
        raise ValueError("remote_q4_hash_mismatch")
    receipt: dict[str, object] = {"schema": "local_bmo.j1m.remote-eval-artifact-receipt.v1", "status": "verified", "name": MODEL_NAME, "size_bytes": observed["size_bytes"], "sha256": observed["sha256"], "manifest_sha256": manifest_digest, "manifest_lock_sha256": lock_parts[0]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.artifact, args.manifest, args.output), sort_keys=True))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"remote eval artifact refused: {type(exc).__name__}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
