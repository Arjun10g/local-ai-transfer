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
import stat
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.clean_machine.package import scan_tree
from qa.harness.evidence import DEFAULT_ENV_ALLOWLIST, audit_environment


PYTHON_TEST_ROOTS = ("tests/qa", "tests/release", "tests/security", "tests/performance", "tests/model", "tests/native")
_SUMMARY_KEYS = ("tests", "suites", "pass", "fail", "cancelled", "skipped", "todo")
MAX_DISCOVERY_ENTRIES = 4096
MAX_DISCOVERY_FILES = 1024
MAX_DISCOVERY_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY_DEPTH = 8
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

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


class DiscoveryError(ValueError):
    """Test discovery was incomplete or exceeded a safe bound."""


def _discover_test_files(root: Path, predicate) -> list[Path]:
    base = (root / "tests").absolute()
    if not base.is_dir():
        raise DiscoveryError("tests root is missing")
    pending: list[tuple[Path, int]] = [(base, 0)]
    files: list[Path] = []
    entries_seen = 0
    bytes_seen = 0
    identities: set[tuple[int, int]] = set()
    while pending:
        directory, depth = pending.pop()
        try:
            directory_stat = directory.stat(follow_symlinks=False)
        except OSError as error:
            raise DiscoveryError("test directory cannot be inspected") from error
        if stat.S_ISLNK(directory_stat.st_mode) or (os.name == "nt" and bool(getattr(directory_stat, "st_file_attributes", 0) & 0x400)):
            raise DiscoveryError("test directory is a link or reparse point")
        identity = (int(directory_stat.st_dev), int(directory_stat.st_ino))
        if identity in identities:
            raise DiscoveryError("test directory identity repeats")
        identities.add(identity)
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise DiscoveryError("test directory cannot be enumerated") from error
        for child in children:
            entries_seen += 1
            if entries_seen > MAX_DISCOVERY_ENTRIES:
                raise DiscoveryError("test discovery entry bound exceeded")
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as error:
                raise DiscoveryError("test entry cannot be inspected") from error
            if stat.S_ISLNK(info.st_mode) or (os.name == "nt" and bool(getattr(info, "st_file_attributes", 0) & 0x400)):
                raise DiscoveryError("test discovery encountered a link or reparse point")
            child_path = Path(child.path)
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 > MAX_DISCOVERY_DEPTH:
                    raise DiscoveryError("test discovery depth bound exceeded")
                pending.append((child_path, depth + 1))
            elif stat.S_ISREG(info.st_mode):
                bytes_seen += max(0, int(info.st_size))
                if bytes_seen > MAX_DISCOVERY_BYTES:
                    raise DiscoveryError("test discovery byte bound exceeded")
                if predicate(child_path):
                    files.append(child_path)
                    if len(files) > MAX_DISCOVERY_FILES:
                        raise DiscoveryError("test discovery file bound exceeded")
    return sorted(files)


def discover_node_tests(root: Path) -> list[str]:
    """Return every bounded ``tests/**/*.test.mjs`` path."""

    base = Path(root).absolute()
    paths = _discover_test_files(base, lambda path: path.name.endswith(".test.mjs"))
    return [path.relative_to(base).as_posix() for path in paths]


def discover_python_test_modules(root: Path) -> list[str]:
    base = Path(root).absolute()
    paths = _discover_test_files(base, lambda path: path.name.startswith("test_") and path.suffix == ".py")
    return [".".join(path.relative_to(base).with_suffix("").parts) for path in paths]


def python_unittest_command(root: Path) -> list[str]:
    return [sys.executable, "-m", "unittest", "-v", *discover_python_test_modules(root)]


def discovered_test_paths(root: Path) -> set[str]:
    paths = set(discover_node_tests(root))
    paths.update(module.replace(".", "/") + ".py" for module in discover_python_test_modules(root))
    return paths


def inventory_check(root: Path) -> dict[str, Any]:
    try:
        discovered = discovered_test_paths(root)
        discovery_error = None
    except DiscoveryError as error:
        discovered = set()
        discovery_error = str(error)
    known = set(TEST_INVENTORY)
    return {
        "discovered": sorted(discovered),
        "unknown": sorted(discovered - known),
        "missing": sorted(known - discovered),
        "classes": {path: TEST_INVENTORY[path] for path in sorted(discovered & known)},
        "discovery_error": discovery_error,
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
        match = re.fullmatch(r"(ok|not ok) (\d+) - (.*?)(?:\s+#\s+(SKIP|TODO)(?:\s+.*)?)?", line)
        if match:
            test_lines.append((int(match.group(2)), match.group(1), match.group(3), match.group(4)))
        elif re.match(r"^(?:ok|not ok) \d+ - ", line):
            raise ReporterError("unknown TAP directive")
    if not test_lines:
        raise ReporterError("zero-count Node suite")
    if [number for number, _status, _name, _directive in test_lines] != list(range(1, len(test_lines) + 1)):
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
    calculated = {
        "pass": sum(status == "ok" and directive is None for _number, status, _name, directive in test_lines),
        "fail": sum(status == "not ok" and directive not in {"SKIP", "TODO"} for _number, status, _name, directive in test_lines),
        "skipped": sum(directive == "SKIP" for _number, _status, _name, directive in test_lines),
        "todo": sum(directive == "TODO" for _number, _status, _name, directive in test_lines),
    }
    if any(summary[key] != value for key, value in calculated.items()):
        raise ReporterError("TAP summary does not match test records")
    return summary


def skipped(test: str, reason: str, *, mandatory: bool = True) -> dict[str, object]:
    return {"status": "SKIP", "test": test, "reason": reason, "mandatory": mandatory, "evidence_state": "UNPROVEN"}


def summarize_records(records: list[dict[str, Any]]) -> dict[str, object]:
    allowed = {"PASS", "FAIL", "SKIP", "BROKEN"}
    invalid = [str(record.get("test", "unnamed")) for record in records if record.get("status") not in allowed]
    if not records or invalid or any(record.get("status") == "BROKEN" for record in records):
        return {"status": "BLOCKED", "passed": False, "fixture_checks_passed": False, "release_passed": False, "mandatory_unproven": invalid}
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
        if status not in {"PASS", "FAIL", "SKIP", "BROKEN"}:
            blockers.append(f"{label}: unknown status ({status})")
        elif status in {"FAIL", "BROKEN"}:
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
    if inventory.get("discovery_error"):
        records.append({"status": "FAIL", "test": "QA-INVENTORY", "mandatory": True, "reason": "test_discovery_failed", "detail": inventory["discovery_error"]})

    package = scan_tree(root / "release" / "windows", require_runtime=release, metadata_only=True)
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


def _reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or (os.name == "nt" and bool(getattr(info, "st_file_attributes", 0) & 0x400))


def _absolute_output_path(path: Path) -> Path:
    value = Path(os.path.abspath(os.fspath(path)))
    folded = [part.casefold() for part in value.parts]
    protected = {name.casefold() for name in PROTECTED_OUTPUT_NAMES}
    if any(part in protected for part in folded) or any(folded[index:index + 2] == ["experiments", "runtime"] for index in range(len(folded) - 1)):
        raise ValueError("output path overlaps protected operator evidence")
    current = Path(value.anchor)
    for part in value.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError("output path cannot be inspected") from error
        # macOS exposes /var (and on some versions /tmp) as stable system
        # aliases. They are not caller-controlled output ancestors; retain
        # the stricter rejection for every other supplied link/reparse path.
        trusted_system_alias = platform.system() == "Darwin" and current in {Path("/var"), Path("/tmp")}
        if _reparse(info) and not trusted_system_alias:
            raise ValueError("output path contains a link or reparse point")
        if current != value and not stat.S_ISDIR(info.st_mode) and not trusted_system_alias:
            raise ValueError("output parent is not a directory")
    try:
        info = value.lstat()
    except FileNotFoundError:
        return value
    if not stat.S_ISREG(info.st_mode) or int(info.st_nlink) != 1:
        raise ValueError("output target must be an unlinked regular file")
    return value


def validate_output_path(path: Path) -> Path:
    """Validate a lexical output path without resolving links or aliases."""

    return _absolute_output_path(path)


def _ensure_output_parent(path: Path) -> None:
    missing: list[Path] = []
    current = path.parent
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise ValueError("output parent does not exist")
            current = current.parent
            continue
        trusted_system_alias = platform.system() == "Darwin" and current in {Path("/var"), Path("/tmp")}
        if (_reparse(info) and not trusted_system_alias) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("output parent is unsafe")
        break
    for directory in reversed(missing):
        directory.mkdir()
        info = directory.lstat()
        if _reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("output parent changed during creation")


def write_output_atomically(path: Path, text: str) -> None:
    target = validate_output_path(path)
    data = text.encode("utf-8")
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("output exceeds byte bound")
    _ensure_output_parent(target)
    target = validate_output_path(target)
    parent = target.parent
    parent_before = parent.stat(follow_symlinks=False)
    old_info = None
    try:
        old_info = target.lstat()
    except FileNotFoundError:
        pass
    temporary_name = f".{target.name}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    use_dir_fd = os.name != "nt" and hasattr(os, "supports_dir_fd") and os.open in os.supports_dir_fd
    parent_fd = None
    temporary = parent / temporary_name
    if use_dir_fd:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(parent, directory_flags)
        opened_parent = os.fstat(parent_fd)
        if (opened_parent.st_ino, opened_parent.st_dev) != (parent_before.st_ino, parent_before.st_dev):
            os.close(parent_fd)
            raise ValueError("output parent changed during write")
    descriptor = None
    try:
        descriptor = os.open(temporary_name if use_dir_fd else temporary, flags, 0o600, dir_fd=parent_fd) if use_dir_fd else os.open(temporary, flags, 0o600)
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise ValueError("output write made no progress")
            written += count
        os.fsync(descriptor)
        final_temp = os.fstat(descriptor)
        if not stat.S_ISREG(final_temp.st_mode) or final_temp.st_nlink != 1:
            raise ValueError("temporary output identity changed")
    except BaseException:
        try:
            if use_dir_fd:
                os.unlink(temporary_name, dir_fd=parent_fd)
            else:
                os.unlink(temporary)
        except OSError:
            pass
        if parent_fd is not None:
            os.close(parent_fd)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        temp_info = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False) if use_dir_fd else temporary.lstat()
        if not stat.S_ISREG(temp_info.st_mode) or int(temp_info.st_nlink) != 1:
            raise ValueError("temporary output identity changed")
        current_parent = os.fstat(parent_fd) if use_dir_fd else parent.stat(follow_symlinks=False)
        if (current_parent.st_ino, current_parent.st_dev) != (parent_before.st_ino, parent_before.st_dev):
            raise ValueError("output parent changed during write")
        try:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False) if use_dir_fd else target.lstat()
        except FileNotFoundError:
            current = None
        if (old_info is None) != (current is None) or old_info is not None and (old_info.st_dev, old_info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("output target changed during write")
        if use_dir_fd:
            os.replace(temporary_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            os.replace(temporary, target)
        descriptor = None
        try:
            descriptor = os.dup(parent_fd) if use_dir_fd else os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    finally:
        try:
            if use_dir_fd:
                os.unlink(temporary_name, dir_fd=parent_fd)
            else:
                os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            # The output is already fail-closed if cleanup cannot remove the
            # private temporary name; never follow or truncate that path.
            pass
        if parent_fd is not None:
            os.close(parent_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan the QA fixture/release gate without executing tests")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="out/evidence/qa-local/safe-summary.json")
    parser.add_argument("--skip-native", action="store_true", help="record native execution as skipped; safe mode never runs it")
    parser.add_argument("--release", action="store_true", help="apply strict release policy; evidence remains BLOCKED without target proof")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    output = validate_output_path(Path(args.output))
    plan = safe_plan(root, release=args.release, skip_native=args.skip_native)
    summary = {
        "schema_version": "qa-run.safe.v1",
        "kind": "plan-only-fixture-static",
        "machine": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "environment": {"keys": audit_environment(os.environ, DEFAULT_ENV_ALLOWLIST)},
        **plan,
        "limitations": ["No test subprocess, CMake/native fixture, model, browser, network, provider, or lifecycle execution occurs in safe mode."],
    }
    write_output_atomically(output, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "mode": summary["mode"], "status": summary["status"], "release_blockers": summary["release_blockers"]}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
