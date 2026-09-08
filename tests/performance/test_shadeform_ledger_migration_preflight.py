"""Hostile, offline contract tests for the legacy ledger review report."""

from __future__ import annotations

from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import shadeform_ledger_migration_preflight as preflight


class PreflightFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ledger-preflight-", dir=Path.cwd())
        self.root = Path(self.directory.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.ledger = self.runtime / "cost-ledger.jsonl"
        self.display = self.root / "LEDGER.md"
        self.incidents = self.runtime / "incidents.jsonl"

    def tearDown(self) -> None:
        self.directory.cleanup()

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
        self.assertEqual(report["legacy_ledger"]["latest_stream_pending_group_count"], 1)
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

    def test_owner_mismatch_is_only_a_sanitized_blocker(self) -> None:
        self.write_jsonl(self.ledger, [self.row()])
        self.display.write_text("", encoding="utf-8")
        self.write_jsonl(self.incidents, [])
        with mock.patch.object(preflight.os, "geteuid", return_value=os.geteuid() + 1):
            report = preflight.run_preflight(
                legacy_ledger=self.ledger, display_ledger=self.display,
                deletion_root=self.runtime, incidents=self.incidents,
            )
        self.assertIn("unsafe_owner", report["path_safety"]["issues"])

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
        with mock.patch.object(preflight.os, "stat", side_effect=changed):
            issues = set()
            with self.assertRaises(preflight._EvidenceError):
                preflight._read_snapshot(self.ledger, limit=100, issues=issues)
        self.assertIn("evidence_mutated_during_read", issues)


if __name__ == "__main__":
    unittest.main()
