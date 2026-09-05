#!/usr/bin/env python3
"""Plan-only QA/release gate with an explicit fail-closed test inventory.

The default safe mode is intentionally non-executing. It inventories known
tests, records why lifecycle/native/model/provider fixtures are not run, and
performs only a bounded release-tree scan. This prevents ambient model
variables, CMake, child processes, loopback servers, and operator ledgers from
being touched by a routine QA command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.clean_machine.package import scan_tree
from qa.harness.evidence import DEFAULT_ENV_ALLOWLIST, audit_environment


NODE_TEST_PATTERNS = ("tests/host/*.test.mjs", "tests/security/*.test.mjs")
PYTHON_TEST_ROOTS = ("tests/qa", "tests/release", "tests/security", "tests/performance", "tests/model", "tests/native")
_SUMMARY_KEYS = ("tests", "suites", "pass", "fail", "cancelled", "skipped", "todo")

# Every current test file is classified. New tests must be assigned a class
# here before they can enter evidence; unknown files fail closed.
TEST_INVENTORY = {
    "tests/host/action-journal.test.mjs": "host_fixture",
    "tests/host/external-tools.test.mjs": "provider_fixture",
    "tests/host/fixture-host.test.mjs": "loopback_fixture",
    "tests/host/local-tools.test.mjs": "process_fixture",
    "tests/host/native-engine.test.mjs": "native_build",
    "tests/host/portable-supervisor.test.mjs": "static",
    "tests/host/process-run.test.mjs": "process_fixture",
    "tests/host/real-native-engine.test.mjs": "real_model",
    "tests/host/tool-chain-vertical.test.mjs": "loopback_fixture",
    "tests/model/production_tool_fixture_parity.test.mjs": "model_fixture",
    "tests/model/test_tool_call_eval.py": "model_fixture",
    "tests/native/test_windows_process_broker_static.py": "native_static",
    "tests/performance/test_j1m_lifecycle.py": "lifecycle",
    "tests/performance/test_model_specs.py": "model_fixture",
    "tests/performance/test_probe_and_preflight.py": "lifecycle",
    "tests/performance/test_vulkan_source_closure.py": "native_static",
    "tests/performance/test_windows_backend.py": "native_static",
    "tests/qa/test_evidence.py": "evidence_fixture",
    "tests/qa/test_safe_runner.py": "safe_runner_unit",
    "tests/release/test_package_scanner.py": "release_static",
    "tests/release/test_windows_acceptance.py": "release_static",
    "tests/security/permission-mode-adversarial.test.mjs": "security_fixture",
    "tests/security/test_adversarial.py": "security_fixture",
    "tests/security/tool-calling-adversarial.test.mjs": "loopback_fixture",
}

SAFE_MODE_REASON = "safe_mode_disables_subprocess_model_network_and_lifecycle_execution"
PROTECTED_OUTPUT_NAMES = frozenset({"incidents.jsonl", "ledger.md", "cost-ledger.jsonl", "cost_ledger.jsonl"})


class ReporterError(ValueError):
    """The built-in Node reporter did not produce a complete TAP result."""


def discover_node_tests(root: Path) -> list[str]:
    """Return all discovered Node tests for compatibility/reporting only."""

    paths = [path for pattern in NODE_TEST_PATTERNS for path in sorted(root.glob(pattern))]
    return [path.relative_to(root).as_posix() for path in paths]


def discover_python_test_modules(root: Path) -> list[str]:
    paths = sorted((root / "tests").glob("**/test_*.py"))
    return [".".join(path.relative_to(root).with_suffix("").parts) for path in paths]


def python_unittest_command(root: Path) -> list[str]:
    return [sys.executable, "-m", "unittest", "-v", *discover_python_test_modules(root)]


def discovered_test_paths(root: Path) -> set[str]:
    paths = set(discover_node_tests(root))
    paths.update(path.relative_to(root).as_posix() for path in root.glob("tests/**/test_*.py"))
    return paths


def inventory_check(root: Path) -> dict[str, Any]:
    discovered = discovered_test_paths(root)
    known = set(TEST_INVENTORY)
    return {
        "discovered": sorted(discovered),
        "unknown": sorted(discovered - known),
        "missing": sorted(known - discovered),
        "classes": {path: TEST_INVENTORY[path] for path in sorted(discovered & known)},
    }


def parse_tap_report(output: str) -> dict[str, int]:
    """Parse and structurally validate a bounded Node TAP report."""

    lines = [line.rstrip("\r") for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "TAP version 13":
        raise ReporterError("missing TAP version")
    for line in lines[1:]:
        if line.startswith("#") or line.startswith("  ") or re.match(r"^(ok|not ok) \d+ - .+$", line) or re.fullmatch(r"1\.\.\d+", line):
            continue
        raise ReporterError("unexpected reporter output")
    plans = [match for match in (re.fullmatch(r"1\.\.(\d+)", line) for line in lines) if match]
    if len(plans) != 1:
        raise ReporterError("TAP plan missing or duplicated")
    test_lines = []
    for line in lines:
        match = re.match(r"^(ok|not ok) (\d+) - (.+)$", line)
        if match:
            test_lines.append((int(match.group(2)), match.group(1)))
    if not test_lines:
        raise ReporterError("zero-count Node suite")
    if [number for number, _status in test_lines] != list(range(1, len(test_lines) + 1)):
        raise ReporterError("non-sequential TAP test numbering")
    planned = int(plans[0].group(1))
    if planned != len(test_lines):
        raise ReporterError("TAP plan does not match test records")
    summary: dict[str, int] = {}
    for key in _SUMMARY_KEYS:
        values = [int(match.group(1)) for line in lines if (match := re.fullmatch(rf"# {key} (\d+)", line))]
        if len(values) != 1:
            raise ReporterError(f"TAP summary missing or duplicated: {key}")
        summary[key] = values[0]
    if summary["tests"] != planned or sum(summary[key] for key in ("pass", "fail", "cancelled", "skipped", "todo")) != summary["tests"]:
        raise ReporterError("TAP summary is internally inconsistent")
    if summary["fail"] != sum(status == "not ok" for _number, status in test_lines):
        raise ReporterError("TAP fail count does not match test records")
    return summary


def skipped(test: str, reason: str, *, mandatory: bool = True) -> dict[str, object]:
    return {"status": "SKIP", "test": test, "reason": reason, "mandatory": mandatory, "evidence_state": "UNPROVEN"}


def summarize_records(records: list[dict[str, Any]]) -> dict[str, object]:
    fixture_checks_passed = not any(record.get("status") == "FAIL" for record in records)
    mandatory_unproven = [str(record.get("test", "unnamed")) for record in records if record.get("mandatory") is True and record.get("status") == "SKIP"]
    release_passed = fixture_checks_passed and not mandatory_unproven
    status = "PASS" if release_passed else ("CONDITIONAL_PASS" if fixture_checks_passed else "FAIL")
    return {"status": status, "passed": fixture_checks_passed, "fixture_checks_passed": fixture_checks_passed, "release_passed": release_passed, "mandatory_unproven": mandatory_unproven}


def evaluate_records(records: list[dict[str, object]], *, release: bool) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    for record in records:
        label = str(record.get("test", "unknown"))
        status = record.get("status")
        if status == "FAIL":
            blockers.append(f"{label}: failed")
        if release and record.get("mandatory", True) and status != "PASS":
            blockers.append(f"{label}: unverified mandatory suite ({status})")
    return not blockers, blockers


def safe_plan(root: Path, *, release: bool = False, skip_native: bool = False) -> dict[str, Any]:
    """Construct a no-execution plan; this function never starts a child."""

    inventory = inventory_check(root)
    records: list[dict[str, object]] = []
    for path in inventory["discovered"]:
        classification = inventory["classes"].get(path)
        if classification is None:
            records.append({"status": "FAIL", "test": path, "mandatory": True, "reason": "unknown_test_inventory_entry"})
        else:
            records.append({"status": "SKIP", "test": path, "mandatory": True, "classification": classification, "reason": SAFE_MODE_REASON, "evidence_state": "UNPROVEN"})
    if inventory["missing"]:
        records.append({"status": "FAIL", "test": "QA-INVENTORY", "mandatory": True, "reason": "inventory_entry_missing"})
    if inventory["unknown"]:
        records.append({"status": "FAIL", "test": "QA-INVENTORY", "mandatory": True, "reason": "unknown_test_inventory_entry"})

    package = scan_tree(root / "release" / "windows", require_runtime=release)
    records.append({"status": package["status"], "test": "REL-001-skeleton-scan", "mandatory": True, "findings": package.get("findings", [])})
    records.append(skipped("QA-003-native-cmake", "safe_mode_never_runs_cmake; skip-native-is-inherent" if skip_native else "safe_mode_never_runs_cmake"))
    records.extend(skipped(name, reason) for name, reason in (
        ("MODEL-REAL-QWEN-ORACLE", "safe_mode_never_reads_model_environment_or_starts_model"),
        ("SHADEFORM-ACCEPTANCE", "safe_mode_never contacts or mutates a provider"),
        ("REL-001-native-windows-package-launch", "target execution remains NOT_READY"),
        ("REL-001-target-hardware-receipt", "target receipt is unavailable"),
    ))
    _passed, blockers = evaluate_records(records, release=True)
    blockers.insert(0, "safe_mode_execution_disabled")
    return {"inventory": inventory, "results": records, "passed": False, "release_passed": False, "status": "BLOCKED", "release_blockers": blockers, "mode": "release" if release else "safe"}


def validate_output_path(path: Path) -> None:
    resolved = path.resolve()
    if resolved.name in PROTECTED_OUTPUT_NAMES or ("experiments" in resolved.parts and "runtime" in resolved.parts):
        raise ValueError("output path overlaps protected operator evidence")


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan the QA fixture/release gate without executing tests")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="out/evidence/qa-local/safe-summary.json")
    parser.add_argument("--skip-native", action="store_true", help="record native execution as skipped; safe mode never runs it")
    parser.add_argument("--release", action="store_true", help="apply strict release policy; evidence remains BLOCKED without target proof")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    output = Path(args.output)
    validate_output_path(output)
    plan = safe_plan(root, release=args.release, skip_native=args.skip_native)
    summary = {
        "schema_version": "qa-run.safe.v1",
        "kind": "plan-only-fixture-static",
        "machine": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "environment": {"keys": audit_environment(os.environ, DEFAULT_ENV_ALLOWLIST)},
        **plan,
        "limitations": ["No test subprocess, CMake/native fixture, model, browser, network, provider, or lifecycle execution occurs in safe mode."],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "mode": summary["mode"], "status": summary["status"], "release_blockers": summary["release_blockers"]}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
