"""Hostile, offline contract tests for the legacy ledger review report."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import copy
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from scripts import shadeform_ledger_migration_preflight as preflight


class PreflightFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_umask = os.umask(0o077)
        self.directory = tempfile.TemporaryDirectory(prefix="ledger-preflight-", dir=Path.cwd())
        self.root = Path(self.directory.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.ledger = self.runtime / "cost-ledger.jsonl"
        self.display = self.root / "LEDGER.md"
        self.incidents = self.runtime / "incidents.jsonl"

    def tearDown(self) -> None:
        self.directory.cleanup()
        os.umask(self.old_umask)

    def write_jsonl(self, path: Path, rows: list[dict], *, final_newline: bool = True) -> None:
        payload = "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) for row in rows)
        path.write_text(payload + ("\n" if final_newline else ""), encoding="utf-8")

    def write_receipt(self, path: Path, receipt: dict) -> None:
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), default=str)
        actual = receipt.get("actual_cost_usd")
        if isinstance(actual, Decimal):
            encoded = encoded.replace('"actual_cost_usd":"' + str(actual) + '"', '"actual_cost_usd":' + str(actual), 1)
        path.write_text(encoded + "\n", encoding="utf-8")

    def row(self, status: str = "settled", amount: str = "1.000000", *, phase: str = "phase-a", instance: str = "instance-a") -> dict:
        row = {"phase_id": phase, "instance_id": instance, "status": status, "ownership_nonce": "n"}
        row["actual_cost_usd" if status == "settled" else "estimated_cost_usd"] = Decimal(amount)
        return row

    def receipt(self, *, phase: str = "phase-a", instance: str = "instance-a", evidence: str = "deleted", amount: str = "1.000000") -> dict:
        return {
            "schema": "local_bmo.shadeform.deletion-receipt.v1",
            "phase_id": phase,
            "instance_id": instance,
            "owner": {
                "phase_id": phase, "instance_id": instance, "instance_name": instance,
                "ownership_nonce": "a" * 32, "ssh_key_id": "key-aa", "ssh_key_name": "key-aa",
                "ssh_key_fingerprint": "A" * 43, "cloud": "cloud", "region": "region",
                "instance_type": "gpu-type", "gpu": "gpu", "gpu_count": 1, "vram_gb": 80,
                "os_image": "ubuntu", "hourly_usd": 1.0,
                "created_at_utc": "2026-01-01T00:00:00+00:00",
                "provider_delete_deadline_utc": "2026-01-01T02:00:00+00:00",
            },
            "status": "complete",
            "deletion": {
                "success": True, "evidence": evidence,
                "confirmed_at_utc": "2026-01-01T01:00:00+00:00",
                "reconciled_from_deletion_intent": evidence == "intent-reconciled", "error_type": None,
            },
            "salvage": {"status": "not_available", "name": None, "size_bytes": None, "error_type": None},
            "actual_cost_usd": Decimal(amount),
            "attempt_reservation_settled": True, "owned_record_persisted": True,
            "key_cleanup_pending": False, "key_cleanup_completed": True, "key_cleanup_deferred": False,
            "retry_required": False,
            "cost_bookkeeping_error_type": None, "attempt_reservation_error_type": None,
            "record_bookkeeping_error_type": None, "deletion_receipt_error_type": None,
            "ssh_key_cleanup_error_type": None, "clear_bookkeeping_error_type": None,
        }

    def report(self, rows: list[dict], *, receipts: list[dict] | None = None, incident_rows: list[dict] | None = None) -> dict:
        self.write_jsonl(self.ledger, rows)
        prefix = preflight._canonical_display_prefix()
        self.assertIsNotNone(prefix)
        self.display.write_bytes(prefix + (preflight._DISPLAY_HEADER + "\n" + preflight._DISPLAY_SEPARATOR + "\n").encode())
        self.write_jsonl(self.incidents, incident_rows or [])
        for index, receipt in enumerate(receipts or []):
            self.write_receipt(self.runtime / f"r{index}.deletion-receipt.json", receipt)
        return preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )

    def test_current_shape_is_review_only_and_exact(self) -> None:
        report = self.report([self.row()])
        self.assertFalse(report["safe_to_migrate_now"])
        self.assertTrue(report["bookkeeping_is_not_spend_authorization"])
        self.assertFalse(report["authoritative_prior_spend_choice_proposed"])
        self.assertFalse(report["zero_pending_genesis_proposed"])
        self.assertNotIn("instance-a", json.dumps(report))

    def test_legacy_row_is_rejected_by_authoritative_v2_parser(self) -> None:
        from scripts import shadeform_lifecycle as lifecycle
        with self.assertRaises((ValueError, lifecycle.ShadeformError)):
            lifecycle._canonical_cost_event(self.row(), stored=True)

    def test_duplicate_changing_pending_and_settled_rewrite_are_blocked(self) -> None:
        report = self.report([
            self.row("pending", "1.000000"),
            self.row("pending", "2.000000"),
            self.row("settled", "1.000000"),
            self.row("settled", "2.000000"),
        ])
        self.assertIn("duplicate_or_changing_pending", report["path_safety"]["issues"])
        self.assertIn("settled_rewrite", report["path_safety"]["issues"])

    def test_decimal_scale_tiny_and_huge_values_never_round(self) -> None:
        for value in ("0.0000001", "1e-100", "1000000001"):
            with self.subTest(value=value):
                report = self.report([self.row("settled", value)])
                self.assertIn("legacy_settled_cost_invalid", report["path_safety"]["issues"])
        with self.assertRaises(InvalidOperation):
            preflight._decimal(Decimal("0.0000001"))
        with self.assertRaises(InvalidOperation):
            preflight._decimal(Decimal("0E+999"))
        for value in (" 1", "+1", "1e2", "01"):
            with self.subTest(lexical=value), self.assertRaises(InvalidOperation):
                preflight._decimal(value)

    def test_huge_exponent_and_missing_secure_flags_are_sanitized(self) -> None:
        with self.assertRaises(preflight._EvidenceError):
            preflight._strict_object(b'{"amount":0e999999}')
        issues: set[str] = set()
        with mock.patch.object(preflight.os, "O_NOFOLLOW", None):
            with self.assertRaises(preflight._EvidenceError):
                preflight._read_snapshot(self.ledger, limit=100, issues=issues)
        self.assertIn("secure_read_capability_unavailable", issues)
        original_stat = preflight.os.stat
        with mock.patch.object(preflight.os, "stat", lambda path: original_stat(path)):
            issues = set()
            with self.assertRaises(preflight._EvidenceError):
                preflight._read_snapshot(self.ledger, limit=100, issues=issues)
        self.assertIn("secure_read_capability_unavailable", issues)

    def test_missing_identity_future_fields_partial_and_limits(self) -> None:
        row = self.row()
        row["future_field"] = True
        report = self.report([row])
        self.assertIn("legacy_future_or_unknown_fields", report["path_safety"]["issues"])
        self.ledger.write_bytes(b'{"phase_id":"x"}')
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        self.assertIn("legacy_partial_line", report["path_safety"]["issues"])
        with mock.patch.object(preflight, "MAX_JSONL_LINES", 1):
            report = self.report([self.row(), self.row(instance="instance-b")])
        self.assertIn("legacy_line_limit", report["path_safety"]["issues"])
        with mock.patch.object(preflight, "MAX_FILE_BYTES", 1):
            report = self.report([self.row()])
        self.assertIn("byte_limit", report["path_safety"]["issues"])

    def test_orphan_absent_receipt_rounding_and_pending_are_explicit(self) -> None:
        receipt = self.receipt(phase="orphan", instance="orphan-instance", evidence="absent", amount="0.000071")
        display = "| 2026 | phase-a | instance-a | gpu | $1 | x | deleted | $0.9999 | 0 |\n"
        self.write_jsonl(self.ledger, [self.row("settled", "1.000000"), self.row("pending", "2.000000", instance="instance-b")])
        prefix = preflight._canonical_display_prefix()
        self.assertIsNotNone(prefix)
        self.display.write_bytes(prefix + (preflight._DISPLAY_HEADER + "\n" + preflight._DISPLAY_SEPARATOR + "\n").encode() + display.encode("utf-8"))
        self.write_jsonl(self.incidents, [])
        self.write_receipt(self.runtime / "orphan.deletion-receipt.json", receipt)
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        self.assertIsNone(report["deletion_receipts"]["unmatched_absent_actual_cost_usd"])
        self.assertIsNone(report["legacy_ledger"]["latest_stream_pending_group_count"])
        self.assertIn("display_rounding_mismatch", report["path_safety"]["issues"])

    def test_duplicate_json_and_invalid_mode_are_refused(self) -> None:
        self.ledger.write_bytes(b'{"phase_id":"x","phase_id":"y"}\n')
        self.display.write_text("", encoding="utf-8")
        self.write_jsonl(self.incidents, [])
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        self.assertIn("legacy_duplicate_json_key", report["path_safety"]["issues"])
        with self.assertRaises(ValueError):
            preflight.run_preflight(
                legacy_ledger=self.ledger, display_ledger=self.display,
                deletion_root=self.runtime, incidents=self.incidents, mode="audit",
            )

    def test_bounded_json_scalar_schema_and_duplicate_evidence(self) -> None:
        huge = {"phase_id": "p", "instance_id": "i", "status": "settled", "actual_cost_usd": int("9" * 65)}
        deep = "{" + "\"x\":[" * 33 + "0" + "]" * 33 + "}\n"
        self.ledger.write_text(json.dumps(huge) + "\n" + deep, encoding="utf-8")
        prefix = preflight._canonical_display_prefix()
        self.assertIsNotNone(prefix)
        self.display.write_bytes(prefix + (preflight._DISPLAY_HEADER + "\n" + preflight._DISPLAY_SEPARATOR + "\n").encode() + b"| malformed | row |\n")
        self.write_jsonl(self.incidents, [{"phase_id": ["not-scalar"], "instance_id": "i", "incident": "x"}])
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        issues = report["path_safety"]["issues"]
        self.assertIn("legacy_json_number_limit", issues)
        self.assertIn("legacy_json_depth_limit", issues)
        self.assertIn("display_malformed_row", issues)
        self.assertIn("incident_identity_invalid", issues)

        duplicate = {"phase_id": "p", "instance_id": "i", "incident": "x"}
        self.write_jsonl(self.incidents, [duplicate, duplicate])
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        self.assertIn("duplicate_incident", report["path_safety"]["issues"])
        with self.assertRaises(preflight._EvidenceError):
            preflight._strict_object((b'{"' + b"a" * 257 + b'":1}\n'))
        with self.assertRaises(preflight._EvidenceError):
            preflight._strict_object((b'{"a":[' + b"0," * 1024 + b"0]}\n"))
        with self.assertRaises(preflight._EvidenceError):
            preflight._strict_object((b'{"a":"' + b"x" * 20_000 + b'"}\n'))

    def test_receipt_overflow_and_root_swap_never_return_partial_counts(self) -> None:
        receipt = self.receipt(phase="p", instance="instance-i")
        self.write_jsonl(self.runtime / "a.deletion-receipt.json", [receipt])
        self.write_jsonl(self.runtime / "b.deletion-receipt.json", [receipt])
        with mock.patch.object(preflight, "MAX_RECEIPTS", 1):
            report = preflight.run_preflight(
                legacy_ledger=self.ledger, display_ledger=self.display,
                deletion_root=self.runtime, incidents=self.incidents,
            )
        self.assertTrue(report["deletion_receipts"]["parse_refused"])
        self.assertIsNone(report["deletion_receipts"]["receipt_count"])

        original_fstat = preflight.os.fstat
        calls = {"count": 0}
        def swapped(fd):
            result = original_fstat(fd)
            calls["count"] += 1
            if calls["count"] == 2:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
            return result
        issues: set[str] = set()
        with mock.patch.object(preflight.os, "fstat", side_effect=swapped):
            report = preflight._parse_receipts(self.runtime, {}, issues)
        self.assertTrue(report["parse_refused"])
        self.assertIsNone(report["receipt_count"])
        self.assertIn("evidence_mutated_during_read", issues)

    def test_hardlink_created_after_open_is_rejected(self) -> None:
        self.ledger.write_bytes(b"{}\n")
        parent_fd = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent_stat = os.fstat(parent_fd)
        original_fstat = preflight.os.fstat
        calls = {"count": 0}
        def linked(fd):
            result = original_fstat(fd)
            calls["count"] += 1
            if calls["count"] == 2:
                values = list(result)
                values[3] = 2
                return os.stat_result(values)
            return result
        issues: set[str] = set()
        try:
            with mock.patch.object(preflight.os, "fstat", side_effect=linked):
                with self.assertRaises(preflight._EvidenceError):
                    preflight._read_relative_snapshot(parent_fd, parent_stat, "cost-ledger.jsonl", limit=100, issues=issues)
        finally:
            os.close(parent_fd)
        self.assertIn("evidence_mutated_during_read", issues)

    def test_ancestor_component_swap_is_rejected_after_read(self) -> None:
        expected = preflight._check_ancestors(self.ledger, set())
        original_lstat = preflight.os.lstat
        calls = {"count": 0}
        def swapped(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 1:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
            return result
        issues: set[str] = set()
        with mock.patch.object(preflight.os, "lstat", side_effect=swapped):
            self.assertFalse(preflight._ancestors_stable(self.ledger, expected, issues))
        self.assertIn("ancestor_component_swap", issues)

    def test_owner_mismatch_is_only_a_sanitized_blocker(self) -> None:
        self.write_jsonl(self.ledger, [self.row()])
        self.display.write_text("", encoding="utf-8")
        self.write_jsonl(self.incidents, [])
        with mock.patch.object(preflight.os, "geteuid", return_value=os.geteuid() + 1):
            report = preflight.run_preflight(
                legacy_ledger=self.ledger, display_ledger=self.display,
                deletion_root=self.runtime, incidents=self.incidents,
            )
        self.assertIn("parent_unsafe_owner", report["path_safety"]["issues"])

        actual_stat = preflight.os.stat
        def permissive_parent(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if Path(path) == self.runtime:
                values = list(result)
                values[0] = stat.S_IFDIR | 0o755
                return os.stat_result(values)
            return result
        with mock.patch.object(preflight.os, "stat", side_effect=permissive_parent) as stat_mock:
            capabilities = set(preflight.os.supports_dir_fd)
            capabilities.add(stat_mock)
            with mock.patch.object(preflight.os, "supports_dir_fd", capabilities):
                report = preflight.run_preflight(
                    legacy_ledger=self.ledger, display_ledger=self.display,
                    deletion_root=self.runtime, incidents=self.incidents,
                )
        self.assertIn("parent_unsafe_permissions", report["path_safety"]["issues"])

    def test_symlink_hardlink_and_evidence_mutation_are_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"{}\n")
        self.ledger.symlink_to(outside)
        report = self.report([])
        self.assertIn("symlink_path", report["path_safety"]["issues"])

        self.ledger.unlink()
        self.ledger.write_bytes(b"{}\n")
        alias = self.runtime / "alias.jsonl"
        os.link(self.ledger, alias)
        issues: set[str] = set()
        with self.assertRaises(preflight._EvidenceError):
            preflight._read_snapshot(self.ledger, limit=100, issues=issues)
        self.assertIn("hardlink_file", issues)

        original_stat = preflight.os.stat
        calls = {"count": 0}
        def changed(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 4:
                values = list(result)
                values[6] += 1
                return os.stat_result(values)
            return result
        self.ledger.unlink()
        alias.unlink()
        self.ledger.write_bytes(b"{}\n")
        with mock.patch.object(preflight.os, "stat", side_effect=changed) as stat_mock:
            capabilities = set(preflight.os.supports_dir_fd)
            capabilities.add(stat_mock)
            with mock.patch.object(preflight.os, "supports_dir_fd", capabilities):
                issues = set()
                with self.assertRaises(preflight._EvidenceError):
                    preflight._read_snapshot(self.ledger, limit=100, issues=issues)
        self.assertIn("evidence_mutated_during_read", issues)

    def test_empty_partial_invalid_utf8_and_non_table_evidence_refuse(self) -> None:
        self.ledger.write_bytes(b"")
        self.display.write_bytes(b"not a ledger table\n")
        self.incidents.write_bytes(b"\xff\n")
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        issues = report["path_safety"]["issues"]
        self.assertIn("legacy_empty", issues)
        self.assertIn("display_schema_invalid", issues)
        self.assertIn("incidents_invalid_utf8", issues)
        self.assertFalse(report["evidence_complete"])
        self.assertFalse(report["cross_stream_reconciliation_available"])

    def test_every_scoped_ancestor_rejects_world_writable(self) -> None:
        self.root.chmod(0o777)
        self.write_jsonl(self.ledger, [self.row()])
        self.display.write_text("not a table\n", encoding="utf-8")
        self.incidents.write_bytes(b"{}\n")
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        self.assertIn("ancestor_unsafe_permissions", report["path_safety"]["issues"])
        self.assertFalse(report["evidence_complete"])

    def test_receipt_preopen_identity_and_generation_changes_refuse_whole_stream(self) -> None:
        receipt = self.receipt(phase="p", instance="instance-i")
        self.write_jsonl(self.runtime / "r.deletion-receipt.json", [receipt])
        original_enumerate = preflight._enumerate_receipts
        fd_for_first = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            first = original_enumerate(fd_for_first, set())
        finally:
            os.close(fd_for_first)
        # A bounded re-enumeration that loses the entry is a directory
        # generation change, not permission to retain the first total.
        calls = {"count": 0}
        def changing(fd, issues):
            calls["count"] += 1
            return first if calls["count"] == 1 else {}
        issues: set[str] = set()
        with mock.patch.object(preflight, "_enumerate_receipts", side_effect=changing):
            report = preflight._parse_receipts(self.runtime, {}, issues)
        self.assertTrue(report["parse_refused"])
        self.assertIsNone(report["receipt_count"])
        self.assertIn("receipt_directory_changed", issues)

        # The root pre-open lstat identity is bound to the opened descriptor.
        original_fstat = preflight.os.fstat
        calls = {"count": 0}
        def changed_root(fd):
            result = original_fstat(fd)
            calls["count"] += 1
            if calls["count"] == 1:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
            return result
        with mock.patch.object(preflight.os, "fstat", side_effect=changed_root):
            issues = set()
            report = preflight._parse_receipts(self.runtime, {}, issues)
        self.assertTrue(report["parse_refused"])
        self.assertIn("evidence_mutated_during_read", issues)

        # A replacement between the directory snapshot and fd-relative open
        # must not be interpreted as the originally enumerated receipt.
        original_enumerate = preflight._enumerate_receipts
        fd_for_first = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            first = original_enumerate(fd_for_first, set())
        finally:
            os.close(fd_for_first)
        replacement = dict(first)
        key = next(iter(replacement))
        identity = list(replacement[key])
        identity[1] += 1
        replacement[key] = tuple(identity)
        calls = {"count": 0}
        def replaced(fd, issues):
            calls["count"] += 1
            return first if calls["count"] == 1 else replacement
        issues = set()
        with mock.patch.object(preflight, "_enumerate_receipts", side_effect=replaced):
            report = preflight._parse_receipts(self.runtime, {}, issues)
        self.assertTrue(report["parse_refused"])
        self.assertIn("receipt_directory_changed", issues)

    def test_file_preopen_lstat_swap_is_refused(self) -> None:
        self.write_jsonl(self.ledger, [self.row()])
        original_lstat = preflight.os.lstat
        def swapped(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if Path(path) == self.ledger:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
            return result
        issues: set[str] = set()
        with mock.patch.object(preflight.os, "lstat", side_effect=swapped):
            with self.assertRaises(preflight._EvidenceError):
                preflight._read_snapshot(self.ledger, limit=100, issues=issues)
        self.assertIn("evidence_mutated_during_read", issues)

    def test_incident_schema_and_display_completeness_refuse(self) -> None:
        self.write_jsonl(self.ledger, [self.row(), self.row(instance="instance-b")])
        prefix = preflight._canonical_display_prefix()
        self.assertIsNotNone(prefix)
        self.display.write_bytes(prefix + (
            "| date | phase | instance id | gpu | $/hr | purpose | status | cost logged | idle min |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| 2026-01-01 | phase-a | instance-a | gpu | $1 | run | deleted | $1.000000 | 0 |\n").encode("utf-8"))
        self.write_jsonl(self.incidents, [{"phase_id": "phase-a", "incident": "x", "unexpected": True}])
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        issues = report["path_safety"]["issues"]
        self.assertIn("display_incomplete_against_ledger", issues)
        self.assertIn("incident_schema_invalid", issues)
        self.assertTrue(report["display_ledger"]["parse_refused"])
        self.assertTrue(report["incidents"]["parse_refused"])
        self.assertFalse(report["evidence_complete"])

    def test_receipt_requires_exact_canonical_shape_before_defaults(self) -> None:
        baseline = self.receipt()
        mutations = {
            "minimal": {"schema": baseline["schema"]},
            "unknown_top_level": {**baseline, "future": True},
            "unknown_owner": {**baseline, "owner": {**baseline["owner"], "future": True}},
            "unknown_deletion": {**baseline, "deletion": {**baseline["deletion"], "future": True}},
            "unknown_salvage": {**baseline, "salvage": {**baseline["salvage"], "future": True}},
            "missing_owner": {key: value for key, value in baseline.items() if key != "owner"},
            "missing_status": {key: value for key, value in baseline.items() if key != "status"},
            "missing_salvage": {key: value for key, value in baseline.items() if key != "salvage"},
            "missing_cost": {**baseline, "actual_cost_usd": None},
            "missing_absent_cost": {**self.receipt(evidence="absent"), "actual_cost_usd": None},
            "missing_confirmation": {**baseline, "deletion": {**baseline["deletion"], "confirmed_at_utc": None}},
            "malformed_nested": {**baseline, "deletion": {**baseline["deletion"], "success": "yes"}},
            "malformed_bool": {**baseline, "retry_required": "false"},
            "malformed_error": {**baseline, "deletion": {**baseline["deletion"], "error_type": {"kind": "x"}}},
        }
        for label, value in mutations.items():
            with self.subTest(label=label), self.assertRaises(preflight._EvidenceError):
                preflight._validate_canonical_receipt(copy.deepcopy(value))

    def test_display_framing_is_byte_exact_and_contiguous(self) -> None:
        prefix = preflight._canonical_display_prefix()
        self.assertIsNotNone(prefix)
        framing = (preflight._DISPLAY_HEADER + "\n" + preflight._DISPLAY_SEPARATOR + "\n").encode()
        row = b"| 2026-01-01 | phase-a | instance-a | gpu | $1 | run | deleted | $1.000000 | 0 |\n"
        variants = {
            "extra_preamble": prefix + b"\n" + framing + row,
            "header_whitespace": prefix + b" " + framing + row,
            "duplicate_separator": prefix + framing + preflight._DISPLAY_SEPARATOR.encode() + b"\n" + row,
            "trailing_garbage": prefix + framing + row + b"trailing\n",
        }
        for label, payload in variants.items():
            with self.subTest(label=label):
                self.display.write_bytes(payload)
                self.display.chmod(0o600)
                issues: set[str] = set()
                result = preflight._parse_display(self.display, {("phase-a", "instance-a"): self.row()}, issues)
                self.assertTrue(result["parse_refused"])


if __name__ == "__main__":
    unittest.main()
