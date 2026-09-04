#!/usr/bin/env python3
"""One-command local QA run for fixture/conformance/security/release evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

# Direct script invocation starts with scripts/test on sys.path; make the
# checkout root importable without requiring PYTHONPATH configuration.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.clean_machine.package import scan_tree
from qa.harness.evidence import DEFAULT_ENV_ALLOWLIST, audit_environment


def run(command: list[str], cwd: Path, *, timeout: int = 120) -> dict[str, object]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"status": "SKIP", "command": command[0], "reason": "executable-unavailable"}
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    return {"status": "PASS" if result.returncode == 0 else "FAIL", "command": command, "return_code": result.returncode}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the dependency-free QA fixture suite")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="out/evidence/qa-local/summary.json")
    parser.add_argument("--skip-native", action="store_true", help="skip local CMake build/ctest")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    records: list[dict[str, object]] = []
    records.append(run([sys.executable, "-m", "unittest", "discover", "-v"], root))
    records.append(run([sys.executable, "-m", "qa.conformance.runner"], root))
    records.append(run([sys.executable, "-m", "qa.adversarial.run"], root))
    node_tests = [path.relative_to(root).as_posix() for path in sorted(root.glob("tests/host/*.test.mjs"))]
    records.append(run(["node", "--test", *node_tests], root) if node_tests else {"status": "SKIP", "test": "host-tests", "reason": "no-fixtures"})
    package_scan = scan_tree(root / "release" / "windows", require_runtime=False)
    records.append({"status": package_scan["status"], "test": "REL-001-skeleton-scan", "findings": package_scan["findings"]})
    if args.skip_native:
        records.append({"status": "SKIP", "test": "QA-003-native-cmake", "reason": "explicitly-skipped"})
    elif shutil.which("cmake"):
        native_dir = root / "out" / "build" / "qa-local"
        records.append(run(["cmake", "-S", ".", "-B", str(native_dir), "-DCMAKE_BUILD_TYPE=Release"], root, timeout=120))
        records.append(run(["cmake", "--build", str(native_dir), "-j2"], root, timeout=120))
        records.append(run(["ctest", "--test-dir", str(native_dir), "--output-on-failure"], root, timeout=120))
    else:
        records.append({"status": "SKIP", "test": "QA-003-native-cmake", "reason": "cmake-unavailable"})
    summary = {
        "schema_version": "qa-run.v1",
        "kind": "fixture/static",
        "machine": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "environment": {"keys": audit_environment(__import__("os").environ, DEFAULT_ENV_ALLOWLIST)},
        "results": records,
        "passed": all(record["status"] in {"PASS", "SKIP"} for record in records),
        "limitations": ["No real Qwen3.5 artifact/oracle, Shadeform instance, Intel profile, or native Windows launch is tested by this local command."],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": summary["passed"]}, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
