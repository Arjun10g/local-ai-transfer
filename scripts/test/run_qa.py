#!/usr/bin/env python3
"""One-command local QA run for fixture/conformance/security/release evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

# Direct script invocation starts with scripts/test on sys.path; make the
# checkout root importable without requiring PYTHONPATH configuration.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.clean_machine.package import scan_tree
from qa.harness.evidence import DEFAULT_ENV_ALLOWLIST, audit_environment


def run(command: list[str], cwd: Path, *, test: str, timeout: int = 120, mandatory: bool = True) -> dict[str, object]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"status": "SKIP", "test": test, "command": command[0], "reason": "executable-unavailable", "mandatory": mandatory, "evidence_state": "UNPROVEN"}
    try:
        result = subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "FAIL", "test": test, "command": command, "reason": "timeout", "mandatory": mandatory}
    return {"status": "PASS" if result.returncode == 0 else "FAIL", "test": test, "command": command, "return_code": result.returncode, "mandatory": mandatory}


def discover_node_tests(root: Path) -> list[str]:
    paths = [*root.glob("tests/host/*.test.mjs"), *root.glob("tests/security/*.test.mjs")]
    return [path.relative_to(root).as_posix() for path in sorted(paths)]


def skipped(test: str, reason: str, *, mandatory: bool = True) -> dict[str, object]:
    return {"status": "SKIP", "test": test, "reason": reason, "mandatory": mandatory, "evidence_state": "UNPROVEN"}


def summarize_records(records: list[dict[str, Any]]) -> dict[str, object]:
    fixture_checks_passed = not any(record.get("status") == "FAIL" for record in records)
    mandatory_unproven = [str(record.get("test", "unnamed")) for record in records if record.get("mandatory") is True and record.get("status") == "SKIP"]
    release_passed = fixture_checks_passed and not mandatory_unproven
    status = "PASS" if release_passed else ("CONDITIONAL_PASS" if fixture_checks_passed else "FAIL")
    return {
        "status": status,
        "passed": fixture_checks_passed,
        "fixture_checks_passed": fixture_checks_passed,
        "release_passed": release_passed,
        "mandatory_unproven": mandatory_unproven,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the dependency-free QA fixture suite")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="out/evidence/qa-local/summary.json")
    parser.add_argument("--skip-native", action="store_true", help="skip local CMake build/ctest")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    records: list[dict[str, object]] = []
    records.append(run([sys.executable, "-m", "unittest", "discover", "-v"], root, test="QA-001-python-unit"))
    records.append(run([sys.executable, "-m", "qa.conformance.runner"], root, test="QA-001-conformance"))
    records.append(run([sys.executable, "-m", "qa.adversarial.run"], root, test="QA-001-adversarial"))
    node_tests = discover_node_tests(root)
    records.append(run(["node", "--test", *node_tests], root, test="QA-002-host-and-security-node") if node_tests else skipped("QA-002-host-and-security-node", "no-fixtures"))
    node = shutil.which("node")
    if node:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        records.append({"status": "PASS", "test": "QA-002-node24-target", "version": version, "mandatory": True} if version.startswith("v24.") else skipped("QA-002-node24-target", f"required-node24-unavailable-observed-{version or 'unknown'}"))
    else:
        records.append(skipped("QA-002-node24-target", "required-node24-unavailable"))

    package_scan = scan_tree(root / "release" / "windows", require_runtime=False)
    records.append({"status": package_scan["status"], "test": "REL-001-skeleton-scan", "findings": package_scan["findings"], "mandatory": True})
    records.append(skipped("REL-001-native-windows-launch", "requires-native-Windows-package-and-loader-run"))
    if args.skip_native:
        records.append(skipped("QA-003-native-cmake", "explicitly-skipped"))
    elif shutil.which("cmake"):
        native_dir = root / "out" / "build" / "qa-local"
        records.append(run(["cmake", "-S", ".", "-B", str(native_dir), "-DCMAKE_BUILD_TYPE=Release"], root, test="QA-003-native-configure", timeout=120))
        records.append(run(["cmake", "--build", str(native_dir), "-j2"], root, test="QA-003-native-build", timeout=120))
        records.append(run(["ctest", "--test-dir", str(native_dir), "--output-on-failure"], root, test="QA-003-native-ctest", timeout=120))
    else:
        records.append(skipped("QA-003-native-cmake", "cmake-unavailable"))

    records.extend([
        skipped("MODEL-REAL-QWEN-ORACLE", "real-pinned-Qwen-artifact-and-oracle-not-exercised"),
        skipped("TARGET-INTEL-CORE-ULTRA", "exact-Windows-Intel-iGPU-target-not-exercised"),
        skipped("SHADEFORM-ACCEPTANCE", "Shadeform-instance-not-exercised"),
    ])
    aggregate = summarize_records(records)
    summary = {
        "schema_version": "qa-run.v2",
        "kind": "fixture/static",
        "machine": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "environment": {"keys": audit_environment(os.environ, DEFAULT_ENV_ALLOWLIST)},
        "results": records,
        **aggregate,
        "limitations": ["This local command is a typed fixture/static conditional pass unless all mandatory real-target evidence is present."],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": summary["status"], "passed": summary["passed"], "release_passed": summary["release_passed"]}, sort_keys=True))
    return 0 if summary["fixture_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
