from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import shadeform_lifecycle as sf
from scripts import shadeform_teardown as teardown
from scripts.shadeform import remote_external_tools


class ShadeformTeardownDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS)
        sf.RUNTIME_ROOT = self.root / "runtime"
        sf.MARKDOWN_LEDGER = self.root / "LEDGER.md"
        sf.COST_LEDGER = sf.RUNTIME_ROOT / "cost-ledger.jsonl"
        sf.INCIDENTS = sf.RUNTIME_ROOT / "incidents.jsonl"
        sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
        self.env_file = self.root / "env"
        self.env_file.write_text("SHADEFORM_API_KEY=fixture-only\n", encoding="utf-8")

    def tearDown(self) -> None:
        sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS = self.originals
        self.temporary.cleanup()

    def record(self, *, phase: str = "durable-delete", instance: str = "instance-durable-1") -> sf.OwnedResource:
        nonce = "0123456789abcdef0123456789abcdef"
        created = sf.utc_now() - timedelta(hours=1)
        return sf.OwnedResource(
            phase_id=phase,
            run_id="durability-test",
            instance_id=instance,
            instance_name=sf.owned_instance_name("durability-test", nonce),
            ownership_nonce=nonce,
            ssh_key_id="key-durable-1",
            ssh_key_name=f"j1m-{nonce}",
            ssh_public_key="ssh-ed25519 AAAA",
            gpu="A100_80G",
            cloud="fixture-cloud",
            region="fixture-region",
            instance_type="fixture-a100",
            gpu_count=1,
            vram_gb=80,
            os_image="fixture-os",
            hourly_usd=1.0,
            created_at_utc=created.isoformat(),
            provider_delete_deadline_utc=(created + timedelta(hours=3)).isoformat(),
        )

    def test_all_instance_delete_callers_route_through_shared_state_machine(self) -> None:
        targets = {
            "remote_external_tools.py": Path(remote_external_tools.__file__),
            "shadeform_watchdog.py": sf.ROOT / "scripts" / "shadeform_watchdog.py",
            "j1m_orchestrator.py": sf.ROOT / "scripts" / "j1m_orchestrator.py",
        }
        for name, path in targets.items():
            with self.subTest(name=name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("._delete_instance(", source)
                self.assertIn("teardown_recovered", source)
        shared = (sf.ROOT / "scripts" / "shadeform_teardown.py").read_text(encoding="utf-8")
        self.assertEqual(shared.count("shadeform._delete_instance("), 2)

    def test_dispatched_deleted_status_is_reconciled_without_second_delete_and_caps_cost(self) -> None:
        record = self.record()
        record.created_at_utc = "2026-01-01T00:00:00+00:00"
        record.provider_delete_deadline_utc = "2026-01-01T03:00:00+00:00"
        sf.write_owned_resource(record)
        dispatched = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
        with mock.patch.object(sf, "utc_now", return_value=dispatched):
            teardown._write_deletion_intent(record.phase_id, record, status="dispatched")
        events: list[dict[str, object]] = []
        much_later = dispatched + timedelta(days=30)
        with mock.patch.object(sf, "utc_now", return_value=much_later), \
                mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "deleted"}), \
                mock.patch.object(sf, "_delete_instance") as delete, \
                mock.patch.object(sf, "append_cost_event", side_effect=lambda event: events.append(dict(event))), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_ssh_key", return_value={"success": True}):
            receipt = teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        delete.assert_not_called()
        instance_event = next(event for event in events if event["instance_id"] == record.instance_id)
        self.assertEqual(float(instance_event["actual_cost_usd"]), 3.0)
        self.assertEqual(receipt["status"], "complete")
        confirmation = teardown._load_deletion_intent(record.phase_id, record)
        self.assertEqual(confirmation["confirmed_at_utc"], record.provider_delete_deadline_utc)

    def test_dispatched_but_still_present_resource_retries_only_after_fresh_full_proof(self) -> None:
        record = self.record(phase="pre-dispatch-crash", instance="instance-pre-dispatch")
        sf.write_owned_resource(record)
        teardown._write_deletion_intent(record.phase_id, record, status="dispatched")
        order: list[str] = []

        def verify(*_args, **_kwargs):
            order.append("full-proof")
            return {"status": "active"}

        def delete(*_args, **_kwargs):
            order.append("delete")
            return {"success": True, "status": "deleted"}

        with mock.patch.object(sf, "verify_owned_instance_before_delete", side_effect=verify), \
                mock.patch.object(sf, "_delete_instance", side_effect=delete), \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_ssh_key", return_value={"success": True}):
            receipt = teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        self.assertEqual(order, ["full-proof", "delete"])
        self.assertEqual(receipt["status"], "complete")

    def test_provider_absence_without_prior_intent_refuses_without_delete(self) -> None:
        record = self.record(phase="naked-absence", instance="instance-naked-absence")
        sf.write_owned_resource(record)
        with mock.patch.object(
            sf, "verify_owned_instance_before_delete", return_value={"status": "deleted"}
        ), mock.patch.object(sf, "_delete_instance") as delete:
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                teardown.teardown_exact(
                    record.phase_id, record.instance_id, env_file=self.env_file
                )
        delete.assert_not_called()
        self.assertIsNone(teardown._load_deletion_intent(record.phase_id, record))
        self.assertIsNotNone(sf.read_owned_resource(record.phase_id))

    def test_delayed_404_bills_through_immutable_provider_backstop_not_retry_time(self) -> None:
        record = self.record(phase="delayed-absence", instance="instance-delayed-404")
        record.created_at_utc = "2026-01-01T00:00:00+00:00"
        record.provider_delete_deadline_utc = "2026-01-01T02:30:00+00:00"
        sf.write_owned_resource(record)
        with mock.patch.object(sf, "utc_now", return_value=datetime(2026, 1, 1, 1, tzinfo=timezone.utc)):
            teardown._write_deletion_intent(record.phase_id, record, status="dispatched")
        events: list[dict[str, object]] = []
        with mock.patch.object(sf, "utc_now", return_value=datetime(2026, 2, 1, tzinfo=timezone.utc)), \
                mock.patch.object(sf, "verify_owned_instance_before_delete", side_effect=sf.ShadeformHTTPError(404, "gone")), \
                mock.patch.object(sf, "_delete_instance") as delete, \
                mock.patch.object(sf, "append_cost_event", side_effect=lambda event: events.append(dict(event))), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_ssh_key", return_value={"success": True}):
            receipt = teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        delete.assert_not_called()
        cost = next(event["actual_cost_usd"] for event in events if event["instance_id"] == record.instance_id)
        self.assertEqual(cost, 2.5)
        self.assertEqual(receipt["deletion"]["confirmed_at_utc"], record.provider_delete_deadline_utc)

    def test_key_delete_crash_reconciles_exact_404_without_second_delete(self) -> None:
        record = self.record(phase="key-delete-retry", instance="instance-key-retry")
        sf.write_owned_resource(record)
        real_writer = teardown._write_deletion_receipt
        writes = 0

        def fail_final(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("final receipt fsync failed")
            return real_writer(*args, **kwargs)

        delete_key = mock.Mock(return_value={"success": True})
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={}), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_ssh_key", delete_key), \
                mock.patch.object(teardown, "_write_deletion_receipt", side_effect=fail_final):
            with self.assertRaises(RuntimeError):
                teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        self.assertIsNotNone(sf.read_owned_resource(record.phase_id))
        delete_key.assert_called_once()

        with mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", side_effect=sf.ShadeformHTTPError(404, "absent")), \
                mock.patch.object(sf, "delete_ssh_key") as second_delete:
            receipt = teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        second_delete.assert_not_called()
        self.assertEqual(receipt["status"], "complete")
        self.assertIsNone(sf.read_owned_resource(record.phase_id))

    def test_recovered_caller_crash_after_provider_delete_retains_exact_retry_state(self) -> None:
        record = self.record(phase="recovered-delete-crash", instance="instance-recovered-crash")
        real_intent_writer = teardown._write_deletion_intent

        def fail_confirmation(*args, **kwargs):
            if kwargs.get("status") == "confirmed":
                raise OSError("confirmation barrier failed")
            return real_intent_writer(*args, **kwargs)

        first_delete = mock.Mock(return_value={"success": True, "status": "deleted"})
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "active"}), \
                mock.patch.object(sf, "_delete_instance", first_delete), \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(teardown, "_write_deletion_intent", side_effect=fail_confirmation):
            with self.assertRaises(RuntimeError):
                teardown.teardown_recovered_exact(record, env_file=self.env_file)
        first_delete.assert_called_once()
        self.assertIsNotNone(sf.read_recovery_owned_resource(record.phase_id))
        self.assertEqual(teardown._load_deletion_intent(record.phase_id, record)["status"], "dispatched")

        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "deleted"}), \
                mock.patch.object(sf, "_delete_instance") as second_delete, \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_ssh_key", return_value={"success": True}):
            receipt = teardown.teardown_recovered_exact(record, env_file=self.env_file)
        second_delete.assert_not_called()
        self.assertEqual(receipt["status"], "complete")

    def test_strict_receipt_and_intent_loaders_reject_untrusted_files(self) -> None:
        record = self.record(phase="strict-evidence", instance="instance-strict-1")
        receipt_path = sf.RUNTIME_ROOT / f"{record.phase_id}.deletion-receipt.json"
        sf._ensure_durable_directory(receipt_path.parent)
        cases = {
            "schema-less": b"{}",
            "duplicate": b'{"schema":"x","schema":"x"}',
            "nonfinite": b'{"actual_cost_usd":1e999}',
            "oversize": b"{" + (b" " * (teardown.MAX_LOCAL_SALVAGE_BYTES + 1)) + b"}",
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                receipt_path.write_bytes(payload)
                with self.assertRaises(RuntimeError):
                    teardown._load_deletion_receipt(record.phase_id, record.instance_id, record)

        hostile_receipt = teardown._canonical_deletion_receipt(record.phase_id, {
            "schema": teardown.DELETION_RECEIPT_SCHEMA,
            "phase_id": record.phase_id,
            "instance_id": record.instance_id,
            "owner": teardown._owner_binding(record),
            "status": "delete-failed",
            "deletion": {"success": False, "error_type": "RuntimeError"},
            "salvage": {"status": "not_available"},
            "retry_required": True,
        })
        hostile_receipt["owner"] = dict(hostile_receipt["owner"])
        hostile_receipt["owner"]["cloud"] = "wrong-profile"
        receipt_path.write_text(json.dumps(hostile_receipt), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            teardown._load_deletion_receipt(record.phase_id, record.instance_id, record)
        receipt_path.unlink()

        with mock.patch.object(sf, "utc_now", return_value=datetime(2026, 1, 1, tzinfo=timezone.utc)):
            intent = teardown._write_deletion_intent(record.phase_id, record, status="dispatched")
        intent_path = teardown._deletion_intent_path(record.phase_id)
        malicious = dict(intent)
        malicious["owner"] = dict(intent["owner"])
        malicious["owner"]["ownership_nonce"] = "f" * 32
        intent_path.write_text(json.dumps(malicious), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            teardown._load_deletion_intent(record.phase_id, record)
        intent_path.write_bytes(b'{"schema":"x","schema":"x"}')
        with self.assertRaises((RuntimeError, sf.ShadeformError)):
            teardown._load_deletion_intent(record.phase_id, record)

    def test_stable_reader_rejects_hardlinks_and_path_identity_change(self) -> None:
        source = self.root / "evidence.json"
        source.write_bytes(b"{}")
        alias = self.root / "alias.json"
        os.link(source, alias)
        with self.assertRaises(sf.ShadeformError):
            sf.bounded_stable_bytes(source, 100, label="fixture evidence")
        alias.unlink()

        original_stat = os.stat

        def changed_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            values = list(result)
            values[1] += 1
            return os.stat_result(values)

        with mock.patch.object(sf.os, "stat", side_effect=changed_stat):
            with self.assertRaises(sf.ShadeformError):
                sf.bounded_stable_bytes(source, 100, label="fixture evidence")

    def test_recursive_directory_and_unlink_barriers_are_mandatory(self) -> None:
        calls: list[Path] = []
        target = self.root / "first" / "second" / "receipt.json"
        with mock.patch.object(sf, "_fsync_directory", side_effect=lambda path: calls.append(Path(path))):
            sf.durable_create_new(target, b"evidence")
        self.assertIn(self.root, calls)
        self.assertIn(self.root / "first", calls)
        self.assertIn(self.root / "first" / "second", calls)

        doomed = self.root / "doomed"
        doomed.write_bytes(b"x")
        with mock.patch.object(sf, "_fsync_directory", side_effect=OSError("barrier failed")):
            with self.assertRaises(OSError):
                sf.durable_unlink(doomed)

    def test_recovery_owner_cannot_coexist_with_different_normal_owner(self) -> None:
        recovery = self.record(phase="split-owner", instance="instance-split-1")
        normal = self.record(phase="split-owner", instance="instance-split-2")
        sf.write_recovery_owned_resource(recovery)
        sf.write_owned_resource(normal)
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            teardown.teardown_exact(normal.phase_id, normal.instance_id, env_file=self.env_file)

    def test_incomplete_recovery_owner_is_rejected_before_publication(self) -> None:
        record = self.record(phase="incomplete-owner", instance="instance-incomplete")
        record.os_image = None
        with self.assertRaisesRegex(ValueError, "complete immutable instance profile"):
            teardown.teardown_recovered_exact(record, env_file=self.env_file)
        self.assertIsNone(sf.read_recovery_owned_resource(record.phase_id))

    def test_receipt_publication_avoids_hardlinks_and_requires_unlink_barrier(self) -> None:
        source = self.root / "incoming"
        destination = self.root / "published"
        source.write_bytes(b"receipt")
        source_text = Path(remote_external_tools.__file__).read_text(encoding="utf-8")
        publish = source_text[source_text.index("def _publish_receipt_no_replace"):source_text.index("def _deletion_confirmed")]
        self.assertNotIn("os.link", publish)
        with mock.patch.object(sf, "durable_unlink", side_effect=OSError("unlink barrier failed")):
            with self.assertRaises(OSError):
                remote_external_tools._publish_receipt_no_replace(source, destination)
        self.assertEqual(destination.read_bytes(), b"receipt")

    def test_explicit_empty_auto_delete_contract_is_rejected_without_dispatch(self) -> None:
        candidate = sf.Candidate(
            gpu="A100_80G",
            cloud="fixture-cloud",
            region="fixture-region",
            instance_type="fixture-a100",
            hourly_usd=1.0,
            vram_gb=80,
            os_image="fixture-os",
            interruptible=False,
        )
        with mock.patch.object(sf, "request") as request:
            with self.assertRaisesRegex(sf.ShadeformError, "auto-delete contract"):
                sf.create_instance(
                    "fixture-api-key",
                    {},
                    phase_id="empty-contract",
                    run_id="durability-test",
                    candidate=candidate,
                    ssh_key_id="key-durable-1",
                    nonce="0123456789abcdef0123456789abcdef",
                    max_runtime_hours=1.0,
                    auto_delete_contract={},
                )
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
