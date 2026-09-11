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


_SUMMARY_KEYS = ("tests", "suites", "pass", "fail", "cancelled", "skipped", "todo")
MAX_DISCOVERY_ENTRIES = 4096
MAX_DISCOVERY_FILES = 1024
MAX_DISCOVERY_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY_DEPTH = 8
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TAP_BYTES = 4 * 1024 * 1024
MAX_TAP_LINES = 100_000
MAX_TAP_LINE_CHARS = 16 * 1024
IGNORED_GENERATED_DIRECTORY_NAMES = frozenset({"__pycache__"})

# Every executable test file is classified. Every other regular file below
# ``tests/`` must be explicitly reviewed as support data. New files in either
# category therefore fail closed rather than escaping through a filename
# convention.
TEST_INVENTORY = {
    "tests/host/action-journal-container-model.test.mjs": "host_fixture",
    "tests/host/action-journal-helper-model.test.mjs": "host_fixture",
    "tests/host/action-journal-protocol.test.mjs": "host_fixture",
    "tests/host/action-journal.test.mjs": "host_fixture",
    "tests/host/native-action-journal-client.test.mjs": "host_fixture",
    "tests/host/copilot-acp-hardening.test.mjs": "provider_fixture",
    "tests/host/controller-native-supervisor.test.mjs": "static",
    "tests/host/descriptor-action-journal.test.mjs": "provider_fixture",
    "tests/host/external-tools.test.mjs": "provider_fixture",
    "tests/host/graph-read-tools.test.mjs": "provider_fixture",
    "tests/host/graph-production-composition-restart.test.mjs": "provider_fixture",
    "tests/host/graph-restart-reconciliation.test.mjs": "provider_fixture",
    "tests/host/graph-sent-proof-projection.test.mjs": "provider_fixture",
    "tests/host/graph-manual-resolution-guard.test.mjs": "host_fixture",
    "tests/host/fixture-host.test.mjs": "loopback_fixture",
    "tests/host/local-tools.test.mjs": "process_fixture",
    "tests/host/native-engine.test.mjs": "native_build",
    "tests/host/portable-supervisor.test.mjs": "static",
    "tests/host/process-run.test.mjs": "process_fixture",
    "tests/host/real-native-engine.test.mjs": "real_model",
    "tests/host/tool-chain-vertical.test.mjs": "loopback_fixture",
    "tests/host/windows-fs-refusal-slice.test.mjs": "windows_refusal_static",
    "tests/model/production_tool_fixture_parity.test.mjs": "model_fixture",
    "tests/model/test_tool_call_eval.py": "model_fixture",
    "tests/model/test_windows_artifact_handoff.py": "release_static",
    "tests/native/http_negative_tests.py": "loopback_fixture",
    "tests/native/model_validator_tests.cpp": "native_build",
    "tests/native/real_init_guard.py": "real_model",
    "tests/native/real_model_smoke.py": "real_model",
    "tests/native/runtime_tests.cpp": "native_build",
    "tests/native/test_descriptor_wal_contract_static.py": "native_static",
    "tests/native/test_windows_action_journal_storage_static.py": "native_static",
    "tests/native/test_windows_descriptor_journal_bootstrap_static.py": "native_static",
    "tests/native/test_windows_action_journal_helper_static.py": "native_static",
    "tests/native/test_windows_action_journal_owner_static.py": "native_static",
    "tests/native/test_windows_process_dispatch_lease_static.py": "native_static",
    "tests/native/test_windows_hardware_attestor_static.py": "native_static",
    "tests/native/test_windows_clipboard_static.py": "native_static",
    "tests/native/test_windows_inert_compile_harness_static.py": "native_static",
    "tests/native/test_windows_process_broker_static.py": "native_static",
    "tests/native/test_windows_process_transaction_static.py": "native_static",
    "tests/native/test_windows_process_authority_static.py": "native_static",
    "tests/native/test_windows_release_verifier_static.py": "native_static",
    "tests/native/test_windows_supervisor_authority_static.py": "native_static",
    "tests/native/test_windows_readonly_fs_static.py": "native_static",
    "tests/performance/test_cost_ledger_genesis.py": "lifecycle",
    "tests/performance/test_shadeform_ledger_migration_preflight.py": "lifecycle",
    "tests/performance/test_shadeform_env_security.py": "lifecycle",
    "tests/performance/test_j1m_lifecycle.py": "lifecycle",
    "tests/performance/test_model_specs.py": "model_fixture",
    "tests/performance/test_probe_and_preflight.py": "lifecycle",
    "tests/performance/test_remote_canary_secret_hardening.py": "lifecycle",
    "tests/performance/test_remote_external_tools_lifecycle.py": "lifecycle",
    "tests/performance/test_shadeform_teardown_durability.py": "lifecycle",
    "tests/performance/test_vulkan_source_closure.py": "native_static",
    "tests/performance/test_windows_backend.py": "native_static",
    "tests/qa/test_evidence.py": "evidence_fixture",
    "tests/qa/test_safe_runner.py": "safe_runner_unit",
    "tests/release/test_package_scanner.py": "release_static",
    "tests/release/test_windows_acceptance.py": "release_static",
    "tests/release/test_windows_hardware_receipt_diagnostic.py": "release_static",
    "tests/security/permission-mode-adversarial.test.mjs": "security_fixture",
    "tests/security/test_adversarial.py": "security_fixture",
    "tests/security/tool-calling-adversarial.test.mjs": "loopback_fixture",
}

REVIEWED_TEST_SUPPORT_FILES = frozenset({
    "tests/__init__.py",
    "tests/model/__init__.py",
    "tests/model/production_tool_call_eval.json",
    "tests/model/qwen_xml_vectors.json",
    "tests/model/tool_call_eval.json",
    "tests/native/fixtures/windows_broker/application-user-argv.json",
    "tests/native/fixtures/windows_broker/argument-control.json",
    "tests/native/fixtures/windows_broker/browser-localhost.json",
    "tests/native/fixtures/windows_broker/browser-non-https.json",
    "tests/native/fixtures/windows_broker/class-mismatch.json",
    "tests/native/fixtures/windows_broker/clipboard-over-limit.json",
    "tests/native/fixtures/windows_broker/control-allowed-value.json",
    "tests/native/fixtures/windows_broker/control-literal.json",
    "tests/native/fixtures/windows_broker/copilot-prompt-argv.json",
    "tests/native/fixtures/windows_broker/duplicate-key.json",
    "tests/native/fixtures/windows_broker/generic-host.json",
    "tests/native/fixtures/windows_broker/identity-mismatch.json",
    "tests/native/fixtures/windows_broker/ignored-parameter.json",
    "tests/native/fixtures/windows_broker/non-exe-host.json",
    "tests/native/fixtures/windows_broker/numeric-allowed-invalid.json",
    "tests/native/fixtures/windows_broker/process-unconstrained-argv.json",
    "tests/native/fixtures/windows_broker/raw-command-surface.json",
    "tests/native/fixtures/windows_broker/replayed-request-id.json",
    "tests/native/fixtures/windows_broker/timeout-too-large.json",
    "tests/native/fixtures/windows_broker/unknown-argument.json",
    "tests/native/fixtures/action_journal_storage/header-vector.json",
    "tests/native/fixtures/windows_clipboard/refusal-and-commit-cases.json",
    "tests/native/fixtures/windows_release_verifier/cases.json",
    "tests/native/fixtures/windows_readonly_fs/refusal-cases.json",
    "tests/native/fixtures/action_journal_helper/session-vector.json",
    "tests/native/fixtures/windows_hardware_attestor/display-correlation-cases.fixture.json",
    "tests/native/fixtures/windows_hardware_attestor/source-refusal.fixture.json",
    "tests/performance/__init__.py",
    "tests/performance/fixtures/shadeform_donor_env_keys.txt",
    "tests/performance/lifecycle_test_isolation.py",
    "tests/qa/__init__.py",
    "tests/reference/action-journal-container-model.mjs",
    "tests/reference/action-journal-helper-model.mjs",
    "tests/reference/windows_release_verifier_model.py",
    "tests/release/__init__.py",
    "tests/security/README.md",
    "tests/security/__init__.py",
})

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
                if child.name in IGNORED_GENERATED_DIRECTORY_NAMES:
                    continue
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
            else:
                raise DiscoveryError("test discovery encountered an unsupported entry type")
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
    base = Path(root).absolute()
    tree_files = _discover_test_files(base, lambda _path: True)
    paths = {path.relative_to(base).as_posix() for path in tree_files}
    return paths - REVIEWED_TEST_SUPPORT_FILES


def inventory_check(root: Path) -> dict[str, Any]:
    try:
        base = Path(root).absolute()
        tree_files = {
            path.relative_to(base).as_posix()
            for path in _discover_test_files(base, lambda _path: True)
        }
        discovered = tree_files - REVIEWED_TEST_SUPPORT_FILES
        discovery_error = None
    except DiscoveryError as error:
        tree_files = set()
        discovered = set()
        discovery_error = str(error)
    known = set(TEST_INVENTORY)
    reviewed_tree = known | REVIEWED_TEST_SUPPORT_FILES
    return {
        "discovered": sorted(discovered),
        "unknown": sorted(tree_files - reviewed_tree),
        "missing": sorted(reviewed_tree - tree_files),
        "classes": {path: TEST_INVENTORY[path] for path in sorted(discovered & known)},
        "discovery_error": discovery_error,
    }


def parse_tap_report(output: str) -> dict[str, int]:
    """Parse and structurally validate a bounded Node TAP report."""

    if len(output) > MAX_TAP_BYTES or len(output.encode("utf-8")) > MAX_TAP_BYTES:
        raise ReporterError("TAP report exceeds byte bound")
    lines = [line.rstrip("\r") for line in output.splitlines() if line.strip()]
    if len(lines) > MAX_TAP_LINES or any(len(line) > MAX_TAP_LINE_CHARS for line in lines):
        raise ReporterError("TAP report exceeds structural bounds")
    if not lines or lines[0] != "TAP version 13":
        raise ReporterError("missing TAP version")
    record_pattern = re.compile(
        r"(ok|not ok) (\d+) - ([^#\r\n]+?)(?: # (SKIP|TODO)(?: [^#\r\n]+)?)?"
    )
    supported_comment_patterns = (
        re.compile(r"# Subtest: [^\r\n]+"),
        re.compile(r"# duration_ms (?:0|[1-9]\d*)(?:\.\d+)?"),
        *(re.compile(rf"# {key} \d+") for key in _SUMMARY_KEYS),
    )
    supported_diagnostic_patterns = (
        re.compile(r"  ---"),
        re.compile(r"  \.\.\."),
        re.compile(r"  duration_ms: (?:0|[1-9]\d*)(?:\.\d+)?"),
        re.compile(r"  type: '(?:test|suite)'"),
    )
    for line in lines[1:]:
        if (
            record_pattern.fullmatch(line)
            or re.fullmatch(r"1\.\.\d+", line)
            or any(pattern.fullmatch(line) for pattern in supported_comment_patterns)
            or any(pattern.fullmatch(line) for pattern in supported_diagnostic_patterns)
        ):
            continue
        raise ReporterError("unexpected or unsupported TAP reporter output")
    plans = [match for match in (re.fullmatch(r"1\.\.(\d+)", line) for line in lines) if match]
    if len(plans) != 1:
        raise ReporterError("TAP plan missing or duplicated")
    test_lines = []
    for line in lines:
        match = record_pattern.fullmatch(line)
        if match:
            test_lines.append((int(match.group(2)), match.group(1), match.group(3), match.group(4)))
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
        "cancelled": 0,
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
    if not _safe_file_output_supported():
        raise ValueError("secure file output is unavailable on this platform; use --output -")
    value = Path(os.path.abspath(os.fspath(path)))
    folded = [part.casefold() for part in value.parts]
    protected = {name.casefold() for name in PROTECTED_OUTPUT_NAMES}
    if any(part in protected for part in folded) or any(folded[index:index + 2] == ["experiments", "runtime"] for index in range(len(folded) - 1)):
        raise ValueError("output path overlaps protected operator evidence")
    if not value.is_absolute() or not value.name or value.name in {".", ".."}:
        raise ValueError("output path is invalid")
    return value


def _safe_file_output_supported() -> bool:
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return False
    required = (os.open, os.mkdir, os.stat, os.unlink, os.link)
    return all(function in os.supports_dir_fd for function in required)


def _snapshot(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_uid),
        int(info.st_gid),
        int(info.st_nlink),
        int(info.st_size),
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _validate_directory_info(info: os.stat_result) -> None:
    allowed_owners = {0, os.geteuid()}
    if (
        not stat.S_ISDIR(info.st_mode)
        or int(info.st_uid) not in allowed_owners
        or stat.S_IMODE(info.st_mode) & 0o022
        or int(info.st_nlink) < 1
        or int(info.st_size) < 0
    ):
        raise ValueError("output directory ownership, mode, link count, or type is unsafe")


def _validate_private_parent_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or int(info.st_uid) != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or int(info.st_nlink) < 1
        or int(info.st_size) < 0
    ):
        raise ValueError("output parent must be a private current-user 0700 directory")


def _validate_private_file_info(info: os.stat_result, *, size: int, links: int = 1) -> None:
    if _reparse(info):
        raise ValueError("output path contains a link or reparse point")
    if (
        not stat.S_ISREG(info.st_mode)
        or int(info.st_uid) != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or int(info.st_nlink) != links
        or int(info.st_size) != size
        or size < 0
        or size > MAX_OUTPUT_BYTES
    ):
        raise ValueError("output file ownership, mode, link count, size, or type changed")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_output_directory(path: Path, *, create: bool) -> int:
    """Walk an absolute directory from a pinned root fd without following links."""

    if not _safe_file_output_supported():
        raise ValueError("secure file output is unavailable on this platform; use --output -")
    if not path.is_absolute() or path.anchor != os.path.sep:
        raise ValueError("output directory must be an absolute local path")
    descriptor = os.open(path.anchor, _directory_flags())
    try:
        _validate_directory_info(os.fstat(descriptor))
        for component in path.parts[1:]:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise ValueError("output parent does not exist") from None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except OSError as error:
                    raise ValueError("output parent cannot be created safely") from error
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                raise ValueError("output parent cannot be inspected") from error
            _validate_directory_info(before)
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except OSError as error:
                raise ValueError("output parent cannot be opened without following links") from error
            try:
                after = os.fstat(child)
                _validate_directory_info(after)
                if _snapshot(before) != _snapshot(after):
                    raise ValueError("output parent changed while it was opened")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        _validate_private_parent_info(os.fstat(descriptor))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_parent_path_identity(path: Path, expected: os.stat_result) -> None:
    descriptor = _open_output_directory(path, create=False)
    try:
        current = os.fstat(descriptor)
        _validate_directory_info(current)
        if _snapshot(current) != _snapshot(expected):
            raise ValueError("output parent path identity or metadata changed")
    finally:
        os.close(descriptor)


def _stat_at_optional(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("output entry cannot be inspected") from error


def _read_descriptor_bounded(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, MAX_OUTPUT_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_OUTPUT_BYTES:
            raise ValueError("output exceeds byte bound")


def _reopen_verified_temp(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    data: bytes,
    *,
    links: int = 1,
) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise ValueError("temporary output cannot be reopened safely") from error
    try:
        before = os.fstat(descriptor)
        _validate_private_file_info(before, size=len(data), links=links)
        if _snapshot(before) != _snapshot(expected):
            raise ValueError("reopened temporary output identity changed")
        if _read_descriptor_bounded(descriptor) != data:
            raise ValueError("temporary output content changed")
        after = os.fstat(descriptor)
        if _snapshot(after) != _snapshot(before):
            raise ValueError("temporary output changed while being verified")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unlink_if_exact(parent_fd: int, name: str, expected: os.stat_result) -> bool:
    current = _stat_at_optional(parent_fd, name)
    if current is None or _identity(current) != _identity(expected):
        return False
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise ValueError("exact failed-publication entry cannot be removed durably") from error
    return True


def validate_output_path(path: Path) -> Path:
    """Validate an existing parent and target through stable POSIX handles."""

    target = _absolute_output_path(path)
    parent_fd = _open_output_directory(target.parent, create=False)
    try:
        parent = os.fstat(parent_fd)
        _verify_parent_path_identity(target.parent, parent)
        current = _stat_at_optional(parent_fd, target.name)
        if current is not None:
            _validate_private_file_info(current, size=int(current.st_size))
    finally:
        os.close(parent_fd)
    return target


def write_output_atomically(path: Path, text: str) -> None:
    # This check deliberately precedes Path/os.fspath access. Windows has no
    # accepted handle-relative publisher in this source-only lane.
    if not _safe_file_output_supported():
        raise ValueError("secure file output is unavailable on this platform; use --output -")
    target = _absolute_output_path(path)
    data = text.encode("utf-8")
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("output exceeds byte bound")
    parent_fd = _open_output_directory(target.parent, create=True)
    try:
        _validate_directory_info(os.fstat(parent_fd))
        if _stat_at_optional(parent_fd, target.name) is not None:
            raise ValueError("output target already exists; evidence publication is create-new")
    except BaseException:
        os.close(parent_fd)
        raise
    temporary_name = f".{target.name}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    writer = None
    reader = None
    target_reader = None
    initial = None
    published = False
    try:
        writer = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(writer, 0o600)
        written = 0
        while written < len(data):
            count = os.write(writer, data[written:])
            if count <= 0:
                raise ValueError("output write made no progress")
            written += count
        os.fsync(writer)
        initial = os.fstat(writer)
        _validate_private_file_info(initial, size=len(data))
        staged = _stat_at_optional(parent_fd, temporary_name)
        if staged is None or _snapshot(staged) != _snapshot(initial):
            raise ValueError("temporary output path identity changed")
        reader = _reopen_verified_temp(parent_fd, temporary_name, initial, data)
        _verify_parent_path_identity(target.parent, os.fstat(parent_fd))
        if _stat_at_optional(parent_fd, target.name) is not None:
            raise ValueError("output target appeared during publication")
        try:
            os.link(
                temporary_name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise ValueError("output target appeared during publication") from error
        except OSError as error:
            raise ValueError("handle-relative output publication failed") from error
        linked_writer = os.fstat(writer)
        _validate_private_file_info(linked_writer, size=len(data), links=2)
        target_info = _stat_at_optional(parent_fd, target.name)
        if target_info is None or _identity(target_info) != _identity(initial):
            raise ValueError("published output does not match the verified temporary identity")
        _validate_private_file_info(target_info, size=len(data), links=2)
        target_reader = _reopen_verified_temp(parent_fd, target.name, target_info, data, links=2)
        os.fsync(parent_fd)
        current_temp = _stat_at_optional(parent_fd, temporary_name)
        if current_temp is None or _identity(current_temp) != _identity(initial):
            raise ValueError("temporary output changed before finalization")
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        final_writer = os.fstat(writer)
        _validate_private_file_info(final_writer, size=len(data), links=1)
        if _read_descriptor_bounded(writer) != data:
            raise ValueError("final output content changed")
        if _snapshot(os.fstat(writer)) != _snapshot(final_writer):
            raise ValueError("final output changed while being verified")
        final_target = _stat_at_optional(parent_fd, target.name)
        if final_target is None or _snapshot(final_target) != _snapshot(final_writer):
            raise ValueError("final output identity or metadata changed")
        _verify_parent_path_identity(target.parent, os.fstat(parent_fd))
        published = True
    finally:
        if not published and initial is not None:
            try:
                # A successful link is not authoritative until every
                # post-link identity/content/path check completes. On failure,
                # remove only our exact inode through the pinned parent fd and
                # durably settle that directory entry before permitting retry.
                _unlink_if_exact(parent_fd, target.name, initial)
                _unlink_if_exact(parent_fd, temporary_name, initial)
            finally:
                for descriptor in (target_reader, reader, writer, parent_fd):
                    if descriptor is not None:
                        os.close(descriptor)
        else:
            for descriptor in (target_reader, reader, writer, parent_fd):
                if descriptor is not None:
                    os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan the QA fixture/release gate without executing tests")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="out/evidence/qa-local/safe-summary.json")
    parser.add_argument("--skip-native", action="store_true", help="record native execution as skipped; safe mode never runs it")
    parser.add_argument("--release", action="store_true", help="apply strict release policy; evidence remains BLOCKED without target proof")
    args = parser.parse_args()
    if args.output != "-" and not _safe_file_output_supported():
        print("secure file output is unavailable on this platform; use --output -", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    plan = safe_plan(root, release=args.release, skip_native=args.skip_native)
    summary = {
        "schema_version": "qa-run.safe.v1",
        "kind": "plan-only-fixture-static",
        "machine": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "environment": {"keys": audit_environment(os.environ, DEFAULT_ENV_ALLOWLIST)},
        **plan,
        "limitations": ["No test subprocess, CMake/native fixture, model, browser, network, provider, or lifecycle execution occurs in safe mode."],
    }
    serialized = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        print(serialized, end="")
    else:
        output = Path(args.output)
        write_output_atomically(output, serialized)
        print(json.dumps({"output": str(output), "mode": summary["mode"], "status": summary["status"], "release_blockers": summary["release_blockers"]}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
