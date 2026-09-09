#!/usr/bin/env python3
"""Validate a signed HF-to-Windows artifact handoff without reading model bytes.

The handoff is a bounded, metadata-only message.  The public entry point has no
trust anchor and therefore refuses activation even when the message claims to
be verified.  A future approved integration may inject a verifier for the
canonical payload; that seam is explicit and never reads a model artifact.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import stat
import re
from typing import Any, Callable

MAX_HANDOFF_BYTES = 64 * 1024
MAX_JSON_DEPTH = 8
SIGNATURE_BYTES = 64
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
ASCII_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SCHEMA = "local_bmo.windows-hf-release-handoff.v1"
MODEL_ID = "qwen35-9b-q4-k-m"
MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
MODEL_SIZE_BYTES = 5_629_109_088
MODEL_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
SOURCE_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
LLAMA_CPP_REVISION = "3581ba0cf591b3f772fbb002de0f70e294bc0396"
MAX_MODEL_BYTES = MODEL_SIZE_BYTES

TOP_LEVEL = {"schema", "artifact", "evidence", "release", "security", "signature"}
ARTIFACT_KEYS = {"model_id", "file_name", "size_bytes", "sha256", "manifest_sha256", "source_revision", "llama_cpp_revision", "quantization", "text_only", "vision_projection_present"}
EVIDENCE_KEYS = {"source_model_receipt_sha256", "conversion_receipt_sha256", "model_receipt_sha256", "scan_receipt_sha256", "artifact_manifest_sha256", "toolchain_receipt_sha256", "checksums_sha256"}
RELEASE_KEYS = {"manifest_sha256", "package_kind", "model_included"}
SECURITY_KEYS = {"no_model_bytes_in_handoff", "no_credentials", "no_urls", "offline_verifier"}
SIGNATURE_KEYS = {"algorithm", "key_id", "signature_base64", "payload_sha256"}


class HandoffError(ValueError):
    """A bounded handoff is malformed or cannot satisfy its contract."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("duplicate JSON key")
        result[key] = value
    return result


def _depth(value: Any, level: int = 0) -> None:
    if level > MAX_JSON_DEPTH:
        raise HandoffError("JSON nesting exceeds bound")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > 128:
                raise HandoffError("JSON key exceeds bound")
            _depth(item, level + 1)
    elif isinstance(value, list):
        if len(value) > 64:
            raise HandoffError("JSON array exceeds bound")
        for item in value:
            _depth(item, level + 1)
    elif isinstance(value, str) and len(value.encode("utf-8")) > MAX_HANDOFF_BYTES:
        raise HandoffError("JSON string exceeds bound")


def parse_handoff_bytes(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_HANDOFF_BYTES:
        raise HandoffError("handoff exceeds bound")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(HandoffError("non-finite JSON number")),
        )
    except HandoffError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise HandoffError("handoff is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise HandoffError("handoff must be one JSON object")
    _depth(value)
    return value


def load_handoff(path: Path) -> dict[str, Any]:
    if not isinstance(path, Path):
        raise HandoffError("handoff path type is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise HandoffError("nofollow open capability is unavailable")
    fd = -1
    failed = False
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
        opened = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_HANDOFF_BYTES:
            raise HandoffError("handoff file identity or size is unsafe")
        before = path.stat()
        if identity(before) != identity(opened) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HandoffError("handoff identity changed before bounded read")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_HANDOFF_BYTES:
            chunk = os.read(fd, MAX_HANDOFF_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    except HandoffError:
        failed = True
        raise
    except (OSError, ValueError, TypeError) as exc:
        failed = True
        raise HandoffError("handoff could not be read") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except (OSError, ValueError) as exc:
                if not failed:
                    raise HandoffError("handoff close failed") from exc
    data = b"".join(chunks)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or identity(before) != identity(after)
        or len(data) > MAX_HANDOFF_BYTES
    ):
        raise HandoffError("handoff changed during bounded read")
    return parse_handoff_bytes(data)


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HandoffError(f"{label} keys are not exact")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise HandoffError(f"{label} is not a lowercase SHA-256")
    if value == "0" * 64:
        raise HandoffError(f"{label} is a placeholder")
    return value


def _revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or REVISION.fullmatch(value) is None:
        raise HandoffError(f"{label} is not a lowercase revision")
    return value


def _canonical_payload(value: dict[str, Any]) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    signature = value.get("signature")
    if isinstance(signature, dict):
        unsigned["signature"] = {
            key: item for key, item in signature.items()
            if key not in {"signature_base64", "payload_sha256"}
        }
    return json.dumps(unsigned, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def validate_handoff(value: Any) -> dict[str, Any]:
    root = _exact(value, TOP_LEVEL, "handoff")
    if root["schema"] != SCHEMA:
        raise HandoffError("handoff schema is not exact")

    artifact = _exact(root["artifact"], ARTIFACT_KEYS, "artifact")
    if (
        artifact["model_id"] != MODEL_ID
        or artifact["file_name"] != MODEL_NAME
        or artifact["size_bytes"] != MODEL_SIZE_BYTES
        or artifact["sha256"] != MODEL_SHA256
        or artifact["source_revision"] != SOURCE_REVISION
        or artifact["llama_cpp_revision"] != LLAMA_CPP_REVISION
    ):
        raise HandoffError("artifact identity is not the approved Q4_K_M model")
    size = artifact["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_MODEL_BYTES:
        raise HandoffError("artifact size is outside the bound")
    _hash(artifact["sha256"], "artifact SHA-256")
    _hash(artifact["manifest_sha256"], "artifact manifest SHA-256")
    _revision(artifact["source_revision"], "source revision")
    _revision(artifact["llama_cpp_revision"], "llama.cpp revision")
    if artifact["quantization"] != "Q4_K_M" or artifact["text_only"] is not True or artifact["vision_projection_present"] is not False:
        raise HandoffError("artifact modality or quantization is not approved")

    evidence = _exact(root["evidence"], EVIDENCE_KEYS, "evidence")
    for label, digest in evidence.items():
        _hash(digest, label)
    if evidence["artifact_manifest_sha256"] != artifact["manifest_sha256"]:
        raise HandoffError("artifact manifest digest is not cross-bound")

    release = _exact(root["release"], RELEASE_KEYS, "release")
    _hash(release["manifest_sha256"], "release manifest SHA-256")
    if release["package_kind"] not in {"fixture-skeleton", "portable-release"} or release["model_included"] is not False:
        raise HandoffError("release must exclude model bytes")

    security = _exact(root["security"], SECURITY_KEYS, "security")
    if any(value is not True for value in security.values()):
        raise HandoffError("security declarations are incomplete")

    signature = _exact(root["signature"], SIGNATURE_KEYS, "signature")
    if signature["algorithm"] != "ed25519" or not isinstance(signature["key_id"], str) or ASCII_ID.fullmatch(signature["key_id"]) is None:
        raise HandoffError("signature identity is invalid")
    encoded = signature["signature_base64"]
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= 512:
        raise HandoffError("signature encoding is invalid")
    try:
        detached = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise HandoffError("signature encoding is invalid") from exc
    if len(detached) != SIGNATURE_BYTES:
        raise HandoffError("signature exceeds bound")
    payload = _canonical_payload(root)
    if hashlib.sha256(payload).hexdigest() != signature["payload_sha256"]:
        raise HandoffError("signed payload digest mismatch")
    return {"payload": payload, "signature": detached, "key_id": signature["key_id"], "artifact": artifact}


def _refused(reason: str, *, identity_valid: bool = True) -> dict[str, Any]:
    return {
        "schema": "local_bmo.windows-hf-release-verdict.v1",
        "status": "REFUSED_NOT_ACTIVATED",
        "reason": reason,
        "artifact_identity_valid": identity_valid,
        "signature_verified": False,
        "model_external": True,
        "activated": False,
    }


def verify_handoff(value: Any) -> dict[str, Any]:
    """Validate metadata and refuse; the public API has no trust bypass."""

    validate_handoff(value)
    return _refused("signature_trust_anchor_unavailable")


def _verify_handoff_with_trust_anchor(value: Any, signature_verifier: Callable[[bytes, bytes, str], bool]) -> dict[str, Any]:
    """Private seam for a separately reviewed verifier integration."""

    parsed = validate_handoff(value)
    try:
        trusted = signature_verifier(parsed["payload"], parsed["signature"], parsed["key_id"])
    except Exception as exc:
        raise HandoffError("signature verifier failed closed") from exc
    if trusted is not True:
        return _refused("signature_untrusted")
    return {
        "schema": "local_bmo.windows-hf-release-verdict.v1",
        "status": "VERIFIED",
        "reason": "none",
        "artifact_identity_valid": True,
        "signature_verified": True,
        "model_external": True,
        "activated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handoff", type=Path)
    args = parser.parse_args(argv)
    try:
        verdict = verify_handoff(load_handoff(args.handoff))
    except HandoffError as exc:
        verdict = {
            "schema": "local_bmo.windows-hf-release-verdict.v1",
            "status": "REFUSED_NOT_ACTIVATED",
            "reason": str(exc),
            "artifact_identity_valid": False,
            "signature_verified": False,
            "model_external": True,
            "activated": False,
        }
    print(json.dumps(verdict, sort_keys=True))
    return 0 if verdict["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
