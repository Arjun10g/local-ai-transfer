"""Hostile, offline contract tests for the legacy ledger review report."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
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

    def row(self, status: str = "settled", amount: str = "1.000000", *, phase: str = "phase-a", instance: str = "instance-a") -> dict:
        row = {"phase_id": phase, "instance_id": instance, "status": status, "ownership_nonce": "n"}
        row["actual_cost_usd" if status == "settled" else "estimated_cost_usd"] = Decimal(amount)
        return row

    def report(self, rows: list[dict], *, receipts: list[dict] | None = None, incident_rows: list[dict] | None = None) -> dict:
        self.write_jsonl(self.ledger, rows)
        self.display.write_text("| date | phase | instance | gpu | hourly | purpose | status | cost | idle |\n", encoding="utf-8")
        self.write_jsonl(self.incidents, incident_rows or [])
        for index, receipt in enumerate(receipts or []):
            self.write_jsonl(self.runtime / f"r{index}.deletion-receipt.json", [receipt])
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

    def test_huge_exponent_and_missing_secure_flags_are_sanitized(self) -> None:
        with self.assertRaises(preflight._EvidenceError):
            preflight._strict_object(b'{"amount":0e999999}')
        issues: set[str] = set()
        with mock.patch.object(preflight.os, "O_NOFOLLOW", None):
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
        receipt = {
            "schema": "local_bmo.shadeform.deletion-receipt.v1",
            "phase_id": "orphan", "instance_id": "orphan-instance",
            "actual_cost_usd": Decimal("0.000071"),
            "deletion": {"status": "absent"}, "salvage": {"status": "nothing_available"},
        }
        display = "| 2026 | phase-a | instance-a | gpu | $1 | x | deleted | $0.9999 | 0 |\n"
        self.write_jsonl(self.ledger, [self.row("settled", "1.000000"), self.row("pending", "2.000000", instance="instance-b")])
        self.display.write_text(display, encoding="utf-8")
        self.write_jsonl(self.incidents, [])
        self.write_jsonl(self.runtime / "orphan.deletion-receipt.json", [receipt])
        report = preflight.run_preflight(
            legacy_ledger=self.ledger, display_ledger=self.display,
            deletion_root=self.runtime, incidents=self.incidents,
        )
        self.assertEqual(report["deletion_receipts"]["unmatched_absent_actual_cost_usd"], "$0.000071")
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
        self.display.write_text("| malformed | row |\n", encoding="utf-8")
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

    def test_receipt_overflow_and_root_swap_never_return_partial_counts(self) -> None:
        receipt = {
            "schema": "local_bmo.shadeform.deletion-receipt.v1", "phase_id": "p", "instance_id": "i",
            "actual_cost_usd": "1.000000", "deletion": {"status": "deleted"}, "salvage": {},
        }
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
            if calls["count"] == 3:
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


if __name__ == "__main__":
    unittest.main()
