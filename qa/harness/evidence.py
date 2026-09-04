"""Evidence schemas and secret-safe manifest construction.

This module intentionally reports environment *presence* only.  It never
serializes an environment value, including values for keys that are not on the
allowlist.  This is the regression boundary for SI-001.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "evidence.v1"

# Names are deliberately narrow.  Additions require a review because this is
# an output allowlist, not merely a redaction list.
DEFAULT_ENV_ALLOWLIST = (
    "CI",
    "GITHUB_ACTIONS",
    "RUNNER_ARCH",
    "RUNNER_OS",
    "PYTHON_VERSION",
    "NODE_VERSION",
    "CMAKE_VERSION",
    "SHADEFORM_PROFILE",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key_from_assignment(line: str) -> str | None:
    """Return only an assignment's key; never inspect or retain its value."""

    candidate = line.strip()
    if not candidate or candidate.startswith("#"):
        return None
    if "=" not in candidate:
        # Permit shell ``export KEY`` syntax, still returning the key only.
        if candidate.startswith("export "):
            candidate = candidate[7:].strip()
        if candidate.isidentifier():
            return candidate
        return None
    key = candidate.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[7:].strip()
    return key if key.isidentifier() else None


def audit_environment(
    environment: Mapping[str, object] | Iterable[str],
    allowlist: Iterable[str] = DEFAULT_ENV_ALLOWLIST,
) -> dict[str, dict[str, bool]]:
    """Return allowlisted key presence without returning any credential value.

    ``environment`` may be an ``os.environ``-like mapping or assignment lines
    from a diagnostic source.  Unknown keys are omitted from the result rather
    than being echoed.  Callers must treat the returned shape as the only
    permissible environment-audit output.
    """

    allowed = tuple(sorted({str(key) for key in allowlist if str(key).isidentifier()}))
    allowed_set = set(allowed)
    if isinstance(environment, Mapping):
        present = {str(key) for key in environment if str(key) in allowed_set}
    else:
        present = {
            key
            for line in environment
            for key in (_key_from_assignment(str(line)),)
            if key in allowed_set
        }
    return {key: {"present": key in present} for key in allowed}


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def build_evidence_manifest(
    *,
    source_root: str | os.PathLike[str],
    build_id: str,
    test_name: str = "qa.bootstrap",
    status: str = "fixture",
    skipped: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a reproducible, metadata-only evidence manifest."""

    root = Path(source_root).resolve()
    commit = _git_value(root, "rev-parse", "HEAD")
    dirty = _git_value(root, "status", "--porcelain")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "qa.evidence",
        "created_at_utc": _utc_now(),
        "status": status,
        "build": {
            "build_id": str(build_id),
            "source_commit": commit,
            "dirty_tree": bool(dirty),
            "source_root_present": root.is_dir(),
        },
        "machine": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        },
        "environment": {
            "allowlist_version": 1,
            "keys": audit_environment(os.environ),
        },
        "tests": [{"name": test_name, "status": "PASS", "kind": "fixture"}],
        "skipped": [str(item) for item in skipped],
        "limitations": [
            "This local runner does not establish native Windows, Shadeform, Intel, or real-model evidence."
        ],
    }
    return manifest


def validate_evidence_manifest(value: object) -> list[str]:
    """Return deterministic validation errors; an empty list means valid."""

    errors: list[str] = []
    if not isinstance(value, dict):
        return ["manifest must be an object"]
    required = ("schema_version", "kind", "created_at_utc", "status", "build", "machine", "environment", "tests")
    errors.extend(f"missing:{key}" for key in required if key not in value)
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be evidence.v1")
    if value.get("kind") != "qa.evidence":
        errors.append("kind must be qa.evidence")
    if value.get("status") not in {"fixture", "pass", "fail", "skipped"}:
        errors.append("status must be fixture/pass/fail/skipped")
    if not isinstance(value.get("build"), dict):
        errors.append("build must be an object")
    if not isinstance(value.get("machine"), dict):
        errors.append("machine must be an object")
    environment = value.get("environment")
    if not isinstance(environment, dict) or not isinstance(environment.get("keys"), dict):
        errors.append("environment.keys must be an object")
    else:
        for key, record in environment["keys"].items():
            if not isinstance(key, str) or not isinstance(record, dict) or set(record) != {"present"} or not isinstance(record["present"], bool):
                errors.append(f"environment.keys.{key} must contain only boolean present")
    tests = value.get("tests")
    if not isinstance(tests, list) or any(not isinstance(item, dict) for item in tests):
        errors.append("tests must be a list of objects")
    return errors


def canonical_sha256(value: object) -> str:
    """Hash canonical JSON for evidence index references."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
