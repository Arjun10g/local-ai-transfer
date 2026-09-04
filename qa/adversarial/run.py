#!/usr/bin/env python3
"""Run static/fixture SEC-001 checks and emit machine-readable results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .checks import (
    bounded_json,
    no_shell_string,
    reject_path_escape,
    require_loopback,
    safe_https_url,
    validate_tool_envelope,
)
from qa.harness.evidence import audit_environment


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic SEC-001 adversarial fixtures")
    parser.add_argument("--output")
    args = parser.parse_args()
    deep = "[" * 40 + "0" + "]" * 40
    checks = {
        "SEC-001-loopback": require_loopback("127.0.0.1") and not require_loopback("0.0.0.0"),
        "SEC-001-path": reject_path_escape("notes/readme.md") and not reject_path_escape("../secrets.txt") and not reject_path_escape("notes/file.txt:stream"),
        "SEC-001-json-limits": isinstance(bounded_json('{"ok":true}'), dict) and _raises(lambda: bounded_json(deep, max_depth=16)),
        "SEC-001-tool-envelope": validate_tool_envelope({"id":"call_1","name":"time.now","arguments":{}}) and not validate_tool_envelope({"id":"call_1","name":"time.now","arguments":{},"execute":True}),
        "SEC-001-url": safe_https_url("https://example.invalid/a") and not safe_https_url("file:///tmp/x") and not safe_https_url("https://127.0.0.1/") and not safe_https_url("https://user:pass@example.invalid/"),
        "SEC-001-process": no_shell_string(["git", "status"]) and not no_shell_string("git status"),
        "SI-001-env-presence": _si001_safe(),
    }
    results = [{"test_id": key, "status": "PASS" if value else "FAIL", "kind": "fixture", "details": {}} for key, value in checks.items()]
    summary = {"schema_version": "security-fixtures.v1", "results": results, "passed": all(item["status"] == "PASS" for item in results), "limitations": ["No running engine/host, native Windows, Shadeform, or real-model evidence is produced."]}
    payload = json.dumps(summary, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if summary["passed"] else 1


def _raises(function) -> bool:
    try:
        function()
    except ValueError:
        return True
    return False


def _si001_safe() -> bool:
    secret = "TEST_ONLY_TOKEN_SHOULD_NEVER_APPEAR"
    report = audit_environment(
        [f"git_access = {secret}", f"SAFE_MODE={secret}", f"API_TOKEN={secret}"],
        allowlist=("SAFE_MODE",),
    )
    encoded = json.dumps(report, sort_keys=True)
    return report == {"SAFE_MODE": {"present": True}} and secret not in encoded and "git_access" not in encoded and "API_TOKEN" not in encoded


if __name__ == "__main__":
    raise SystemExit(main())
