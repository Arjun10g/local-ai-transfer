#!/usr/bin/env python3
"""Verify a remote J1M-produced Q4 against the accepted small manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
_MAX_MANIFEST = 256 * 1024


def _read_bounded(path: Path, limit: int, error_code: str) -> bytes:
    """Read one descriptor-bound snapshot; reject replacement or oversize."""
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > limit:
                raise ValueError(error_code)
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            current = path.stat()
            if (len(raw) > limit or before.st_size != after.st_size or
                    after.st_size != len(raw) or
                    (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                    (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError(error_code)
            return raw
    except ValueError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ValueError(error_code) from exc


def _strict_json(raw: bytes) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=reject_duplicates)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()



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


def verify(artifact: Path, manifest_path: Path, output: Path) -> dict[str, object]:
    if artifact.name != MODEL_NAME or not artifact.is_file() or not manifest_path.is_file():
        raise ValueError("remote_q4_input_invalid")
    lock_path = manifest_path.with_name("model-manifest.sha256")
    if not lock_path.is_file():
        raise ValueError("remote_q4_manifest_lock_missing")
    lock_bytes = _read_bounded(lock_path, 256, "remote_q4_manifest_lock_invalid")
    try:
        lock_parts = lock_bytes.decode("utf-8").strip().split()
    except UnicodeDecodeError as exc:
        raise ValueError("remote_q4_manifest_lock_invalid") from exc
    if len(lock_parts) != 2 or lock_parts[1] != manifest_path.name or len(lock_parts[0]) != 64 or any(char not in "0123456789abcdef" for char in lock_parts[0]):
        raise ValueError("remote_q4_manifest_lock_invalid")
    manifest_bytes = _read_bounded(manifest_path, _MAX_MANIFEST, "remote_q4_manifest_invalid")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_digest != lock_parts[0]:
        raise ValueError("remote_q4_manifest_lock_mismatch")
    try:
        manifest = _strict_json(manifest_bytes)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("remote_q4_manifest_invalid") from exc
    expected = manifest.get("artifact") if isinstance(manifest, dict) else None
    if not isinstance(expected, dict) or expected.get("expected_file_name") != MODEL_NAME or expected.get("quantization_profile") != "Q4_K_M" or expected.get("modality_profile") != "text_only_no_mmproj":
        raise ValueError("remote_q4_manifest_invalid")
    observed = {"size_bytes": artifact.stat().st_size, "sha256": _sha256(artifact)}
    if observed != {"size_bytes": expected.get("expected_size_bytes"), "sha256": expected.get("sha256")}:
        raise ValueError("remote_q4_hash_mismatch")
    receipt: dict[str, object] = {"schema": "local_bmo.j1m.remote-eval-artifact-receipt.v1", "status": "verified", "name": MODEL_NAME, "size_bytes": observed["size_bytes"], "sha256": observed["sha256"], "manifest_sha256": manifest_digest, "manifest_lock_sha256": lock_parts[0], **_run_identity()}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=os.fspath(output.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
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
