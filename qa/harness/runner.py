#!/usr/bin/env python3
"""Common local evidence runner; no network or third-party dependencies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .evidence import DEFAULT_ENV_ALLOWLIST, audit_environment, build_evidence_manifest, validate_evidence_manifest
except ImportError:  # Supports direct invocation: python qa/harness/runner.py ...
    from evidence import DEFAULT_ENV_ALLOWLIST, audit_environment, build_evidence_manifest, validate_evidence_manifest  # type: ignore[import-not-found]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or validate metadata-only QA evidence")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("run", help="write a bootstrap evidence manifest")
    make.add_argument("--source-root", default=".")
    make.add_argument("--build-id", required=True)
    make.add_argument("--output", required=True)
    make.add_argument("--test-name", default="qa.bootstrap")
    make.add_argument("--status", choices=("fixture", "pass", "fail", "skipped"), default="fixture")
    make.add_argument("--skip", action="append", default=[])

    env = sub.add_parser("audit-env", help="emit allowlisted environment key presence only")
    env.add_argument("--input", help="assignment file; stdin when omitted")
    env.add_argument("--output", required=True)
    env.add_argument("--allow", action="append", default=[])

    validate = sub.add_parser("validate", help="validate an evidence manifest")
    validate.add_argument("manifest")
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.command == "run":
        value = build_evidence_manifest(
            source_root=args.source_root,
            build_id=args.build_id,
            test_name=args.test_name,
            status=args.status,
            skipped=args.skip,
        )
        _write_json(Path(args.output), value)
        errors = validate_evidence_manifest(value)
        if errors:
            print("invalid generated evidence: " + "; ".join(errors), file=sys.stderr)
            return 1
        print(f"evidence={Path(args.output)} status={args.status}")
        return 0
    if args.command == "audit-env":
        if args.input:
            # The file is read only to extract names; values are never retained.
            lines = Path(args.input).read_text(encoding="utf-8", errors="replace").splitlines()
            value = audit_environment(lines, args.allow or DEFAULT_ENV_ALLOWLIST)
        else:
            value = audit_environment(dict(__import__("os").environ), args.allow or DEFAULT_ENV_ALLOWLIST)
        _write_json(Path(args.output), {"schema_version": "environment-presence.v1", "keys": value})
        print(f"environment_presence={Path(args.output)}")
        return 0
    value = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    errors = validate_evidence_manifest(value)
    if errors:
        print("invalid: " + "; ".join(errors), file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
