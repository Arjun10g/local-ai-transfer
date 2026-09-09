from __future__ import annotations

import hashlib
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
        self.root = Path(self.temporary.name).resolve()
        self.originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS)
        sf.RUNTIME_ROOT = self.root / "runtime"
        sf.MARKDOWN_LEDGER = self.root / "LEDGER.md"
        sf.COST_LEDGER = sf.RUNTIME_ROOT / "cost-ledger.jsonl"
        sf.INCIDENTS = sf.RUNTIME_ROOT / "incidents.jsonl"
        sf.RUNTIME_ROOT.mkdir(mode=0o700)
        sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
        sf.INCIDENTS.write_text('{"incident":"fixture"}\n', encoding="utf-8")
        sf.initialize_cost_ledger_genesis(
            program=sf.COST_LEDGER_PROGRAM,
            currency=sf.COST_LEDGER_CURRENCY,
            budget_cap_usd=50.0,
            prior_settled_spend_usd=0.0,
            current_pending_owner_count=0,
            expected_display_ledger_sha256=hashlib.sha256(
                sf.MARKDOWN_LEDGER.read_bytes()
            ).hexdigest(),
            expected_incidents_sha256=hashlib.sha256(
                sf.INCIDENTS.read_bytes()
            ).hexdigest(),
            confirmation=sf.COST_LEDGER_GENESIS_CONFIRMATION,
        )
        self.env_file = self.root / "env"
        self.env_file.write_text("SHADEFORM_API_KEY=fixture-only\n", encoding="utf-8")
        self.env_file.chmod(0o600)

    def tearDown(self) -> None:
        sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS = self.originals
        self.temporary.cleanup()

    def record(
        self,
        *,
        phase: str = "durable-delete",
        instance: str = "instance-durable-1",
        nonce: str = "0123456789abcdef0123456789abcdef",
        run_id: str = "durability-test",
        key_id: str = "key-durable-1",
    ) -> sf.OwnedResource:
        created = sf.utc_now() - timedelta(hours=1)
        return sf.OwnedResource(
            phase_id=phase,
            run_id=run_id,
            instance_id=instance,
            instance_name=sf.owned_instance_name(run_id, nonce),
            ownership_nonce=nonce,
            ssh_key_id=key_id,
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

    def test_authoritative_owner_files_require_private_parent_and_mode(self) -> None:
        record = self.record(phase="private-owner-evidence")
        sf.write_owned_resource(record)
        owner_path = sf.runtime_ledger_path(record.phase_id)
        owner_path.chmod(0o666)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.read_owned_resource(record.phase_id)
        owner_path.chmod(0o600)
        self.root.joinpath("runtime").chmod(0o777)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.read_owned_resource(record.phase_id)
        self.root.joinpath("runtime").chmod(0o700)

        recovery = self.record(
            phase="private-recovery-evidence",
            instance="instance-private-recovery",
        )
        sf.write_recovery_owned_resource(recovery)
        recovery_path = sf.recovery_owned_resource_path(recovery.phase_id)
        recovery_path.chmod(0o666)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.read_recovery_owned_resource(recovery.phase_id)

    def test_phase_cleanup_lock_rejects_symlink_and_unsafe_existing_file(self) -> None:
        phase = "private-cleanup-lock"
        path = sf.RUNTIME_ROOT / f"{phase}.cleanup.lock"
        target = self.root / "outside-lock"
        target.write_bytes(b"")
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(sf.ShadeformError):
            with sf.phase_cleanup_lock(phase):
                self.fail("symlink lock must never be acquired")
        path.unlink()
        path.write_bytes(b"")
        path.chmod(0o666)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            with sf.phase_cleanup_lock(phase):
                self.fail("public lock must never be acquired")

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
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
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
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
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
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
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

        delete_key = mock.Mock(return_value={"status": "confirmed"})
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={}), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", delete_key), \
                mock.patch.object(teardown, "_write_deletion_receipt", side_effect=fail_final):
            with self.assertRaises(RuntimeError):
                teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        self.assertIsNotNone(sf.read_owned_resource(record.phase_id))
        delete_key.assert_called_once()

        with mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}) as second_delete:
            receipt = teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
        second_delete.assert_called_once()
        self.assertEqual(receipt["status"], "complete")
        self.assertIsNone(sf.read_owned_resource(record.phase_id))

    def test_recovered_caller_crash_after_provider_delete_retains_exact_retry_state(self) -> None:
        record = self.record(phase="recovered-delete-crash", instance="instance-recovered-crash")
        sf.append_cost_event({
            "instance_id": f"attempt-{record.ownership_nonce}",
            "phase_id": record.phase_id,
            "ownership_nonce": record.ownership_nonce,
            "status": "pending",
            "estimated_cost_usd": 3.0,
        })
        real_intent_writer = teardown._write_deletion_intent

        def fail_confirmation(*args, **kwargs):
            if kwargs.get("status") == "confirmed":
                raise OSError("confirmation barrier failed")
            return real_intent_writer(*args, **kwargs)

        first_delete = mock.Mock(return_value={"success": True, "status": "deleted"})
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "active"}), \
                mock.patch.object(sf, "_delete_instance", first_delete), \
                mock.patch.object(teardown, "_write_deletion_intent", side_effect=fail_confirmation):
            with self.assertRaises(RuntimeError):
                teardown.teardown_recovered_exact(record, env_file=self.env_file)
        first_delete.assert_called_once()
        self.assertIsNotNone(sf.read_recovery_owned_resource(record.phase_id))
        self.assertEqual(teardown._load_deletion_intent(record.phase_id, record)["status"], "dispatched")

        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "deleted"}), \
                mock.patch.object(sf, "_delete_instance") as second_delete, \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
            receipt = teardown.teardown_recovered_exact(record, env_file=self.env_file)
        second_delete.assert_not_called()
        self.assertEqual(receipt["status"], "complete")

    def test_ambiguous_key_delete_reconciles_absence_without_second_delete(self) -> None:
        record = self.record(
            phase="transient-key-cleanup",
            instance="instance-transient-key-cleanup",
            nonce="9" * 32,
            key_id="key-transient-cleanup",
        )
        for instance_id in (record.instance_id, f"attempt-{record.ownership_nonce}"):
            sf.append_cost_event({
                "instance_id": instance_id,
                "phase_id": record.phase_id,
                "ownership_nonce": record.ownership_nonce,
                "status": "pending",
                "estimated_cost_usd": 3.0,
            })
        sf.write_owned_resource(record)
        delete_instance = mock.Mock(return_value={"success": True, "status": "deleted"})
        key_proof = mock.Mock(side_effect=[
            {},
            sf.ShadeformHTTPError(404, "exact key absent after ambiguous delete"),
        ])
        delete_key = mock.Mock(side_effect=[
            OSError("ambiguous key delete transport"),
            {"status": "confirmed"},
        ])
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "active"}), \
                mock.patch.object(sf, "_delete_instance", delete_instance), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", key_proof), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", delete_key):
            first = teardown.teardown_exact(
                record.phase_id, record.instance_id, env_file=self.env_file,
            )
            self.assertEqual(first["status"], "deleted-key-cleanup-failed")
            self.assertTrue(first["retry_required"])
            retained = sf.read_owned_resource(record.phase_id)
            self.assertIsNotNone(retained)
            self.assertEqual(retained.status, "deleted-key-cleanup-failed")

            second = teardown.teardown_exact(
                record.phase_id, record.instance_id, env_file=self.env_file,
            )
        self.assertEqual(second["status"], "complete")
        self.assertFalse(second["retry_required"])
        delete_instance.assert_called_once()
        self.assertEqual(key_proof.call_count, 0)
        self.assertEqual(delete_key.call_count, 2)
        self.assertIsNone(sf.read_owned_resource(record.phase_id))

    def test_confirmation_fault_uses_same_provider_ceiling_before_or_after_retry(self) -> None:
        evidence_paths: list[Path] = []
        cases = (
            ("before", datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc), False),
            ("after-404", datetime(2026, 2, 1, tzinfo=timezone.utc), True),
        )
        for suffix, retry_at, retry_404 in cases:
            with self.subTest(retry=suffix):
                record = self.record(
                    phase=f"confirmation-fault-{suffix}",
                    instance=f"instance-confirmation-fault-{suffix}",
                    nonce=("1" if suffix == "before" else "2") * 32,
                    key_id=f"key-confirmation-fault-{suffix}",
                )
                record.created_at_utc = "2026-01-01T00:00:00+00:00"
                record.provider_delete_deadline_utc = "2026-01-01T03:00:00+00:00"
                sf.write_owned_resource(record)
                real_intent_writer = teardown._write_deletion_intent

                def fail_confirmation(*args, **kwargs):
                    if kwargs.get("status") == "confirmed":
                        raise OSError("confirmation barrier failed")
                    return real_intent_writer(*args, **kwargs)

                first_delete = mock.Mock(return_value={"success": True, "status": "deleted"})
                observed_at = datetime(2026, 1, 1, 1, 15, tzinfo=timezone.utc)
                with mock.patch.object(sf, "utc_now", return_value=observed_at), \
                        mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={"status": "active"}), \
                        mock.patch.object(sf, "_delete_instance", first_delete), \
                        mock.patch.object(teardown, "_write_deletion_intent", side_effect=fail_confirmation):
                    with self.assertRaisesRegex(RuntimeError, "confirmation could not be persisted"):
                        teardown.teardown_exact(record.phase_id, record.instance_id, env_file=self.env_file)
                first_delete.assert_called_once()
                dispatched = teardown._load_deletion_intent(record.phase_id, record)
                self.assertEqual(dispatched["status"], "dispatched")

                order: list[str] = []

                def append_cost(event):
                    self.assertEqual(event["ownership_nonce"], record.ownership_nonce)
                    order.append(f"cost:{event['instance_id']}")

                def verify_key(*_args, **_kwargs):
                    order.append("key-verify")
                    return {}

                def delete_key(*_args, **_kwargs):
                    order.append("key-verify")
                    order.append("key-delete")
                    return {"status": "confirmed"}

                real_receipt_writer = teardown._write_deletion_receipt

                def write_receipt(*args, **kwargs):
                    order.append(f"receipt:{args[1]['status']}")
                    return real_receipt_writer(*args, **kwargs)

                verification = (
                    sf.ShadeformHTTPError(404, "gone")
                    if retry_404 else {"status": "deleted"}
                )
                with mock.patch.object(sf, "utc_now", return_value=retry_at), \
                        mock.patch.object(sf, "verify_owned_instance_before_delete", side_effect=(verification if retry_404 else None), return_value=(None if retry_404 else verification)), \
                        mock.patch.object(sf, "_delete_instance") as second_delete, \
                        mock.patch.object(sf, "append_cost_event", side_effect=append_cost), \
                        mock.patch.object(sf, "verify_owned_ssh_key_before_delete", side_effect=verify_key), \
                        mock.patch.object(sf, "delete_owned_ssh_key_exact", side_effect=delete_key), \
                        mock.patch.object(teardown, "_write_deletion_receipt", side_effect=write_receipt):
                    receipt = teardown.teardown_exact(
                        record.phase_id, record.instance_id, env_file=self.env_file,
                    )
                second_delete.assert_not_called()
                evidence_paths.append(teardown._deletion_confirmation_path(record.phase_id, record))
                self.assertEqual(receipt["status"], "complete")
                self.assertEqual(
                    receipt["deletion"]["confirmed_at_utc"],
                    record.provider_delete_deadline_utc,
                )
                self.assertEqual(receipt["actual_cost_usd"], 3.0)
                self.assertEqual(
                    order,
                    [
                        f"cost:{record.instance_id}",
                        f"cost:attempt-{record.ownership_nonce}",
                        "receipt:recovery-pending",
                        "key-verify",
                        "key-delete",
                        "receipt:complete",
                    ],
                )
        self.assertEqual(len(evidence_paths), 2)
        self.assertNotEqual(evidence_paths[0], evidence_paths[1])

    def test_cost_ledger_owner_collision_cannot_settle_another_reservation(self) -> None:
        instance = "instance-owner-collision"
        phase = "owner-cost-collision"
        first_nonce = "3" * 32
        second_nonce = "4" * 32
        sf.append_cost_event({
            "instance_id": instance,
            "phase_id": phase,
            "ownership_nonce": first_nonce,
            "status": "pending",
            "estimated_cost_usd": 3.0,
        })
        before = sf.bounded_stable_bytes(
            sf.COST_LEDGER, sf.MAX_COST_LEDGER_BYTES, label="test cost ledger",
        )
        with self.assertRaisesRegex(sf.ShadeformError, "different owner|unknown owner"):
            sf.append_cost_event({
                "instance_id": instance,
                "phase_id": phase,
                "ownership_nonce": second_nonce,
                "status": "settled",
                "actual_cost_usd": 1.0,
            })
        self.assertEqual(
            sf.bounded_stable_bytes(
                sf.COST_LEDGER, sf.MAX_COST_LEDGER_BYTES, label="test cost ledger",
            ),
            before,
        )
        self.assertEqual(sf.ledger_spend(), (0.0, [instance]))
        sf.append_cost_event({
            "instance_id": instance,
            "phase_id": phase,
            "ownership_nonce": first_nonce,
            "status": "settled",
            "actual_cost_usd": 1.0,
        })
        self.assertEqual(sf.ledger_spend(), (1.0, []))
        rows = [
            json.loads(line) for line in sf.COST_LEDGER.read_text().splitlines()
            if '"event_kind":"genesis"' not in line
        ]
        self.assertTrue(all(row["ownership_nonce"] == first_nonce for row in rows))
        self.assertTrue(all(row["schema"] == sf.COST_EVENT_SCHEMA for row in rows))
        self.assertTrue(all(row["owner_binding_sha256"] == sf.cost_owner_binding_sha256(
            phase, first_nonce, instance,
        ) for row in rows))

    def test_teardown_terminal_cost_rows_preserve_exact_owner_continuity(self) -> None:
        record = self.record(
            phase="teardown-cost-owner", instance="instance-teardown-cost-owner",
            nonce="7" * 32, key_id="key-teardown-cost-owner",
        )
        sf.append_cost_event({
            "instance_id": record.instance_id,
            "phase_id": record.phase_id,
            "ownership_nonce": record.ownership_nonce,
            "status": "pending",
            "estimated_cost_usd": 3.0,
        })
        sf.append_cost_event({
            "instance_id": f"attempt-{record.ownership_nonce}",
            "phase_id": record.phase_id,
            "ownership_nonce": record.ownership_nonce,
            "status": "pending",
            "estimated_cost_usd": 3.0,
        })
        sf.write_owned_resource(record)
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={}), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
            receipt = teardown.teardown_exact(
                record.phase_id, record.instance_id, env_file=self.env_file,
            )
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(sf.ledger_spend(), (3.0, []))
        rows = [json.loads(line) for line in sf.COST_LEDGER.read_text().splitlines()]
        instance_rows = [
            row for row in rows
            if row.get("instance_id") == record.instance_id
        ]
        self.assertEqual([row["status"] for row in instance_rows], ["pending", "settled"])
        self.assertEqual(
            {row["owner_binding_sha256"] for row in instance_rows},
            {sf.cost_owner_binding_sha256(
                record.phase_id, record.ownership_nonce, record.instance_id,
            )},
        )

    def test_cost_ledger_refuses_symlink_hardlink_duplicate_and_path_swap(self) -> None:
        event = {
            "instance_id": "instance-ledger-path",
            "phase_id": "ledger-path-safety",
            "ownership_nonce": "5" * 32,
            "status": "pending",
            "estimated_cost_usd": 1.0,
        }
        sf._ensure_durable_directory(sf.COST_LEDGER.parent)
        original_authority = sf.COST_LEDGER.read_bytes()
        sf.COST_LEDGER.unlink()
        target = self.root / "outside-cost-ledger"
        target.write_bytes(b"outside")
        sf.COST_LEDGER.symlink_to(target)
        with self.assertRaises((OSError, sf.ShadeformError)):
            sf.append_cost_event(event)
        self.assertEqual(target.read_bytes(), b"outside")
        sf.COST_LEDGER.unlink()
        sf.COST_LEDGER.write_bytes(original_authority)
        sf.COST_LEDGER.chmod(0o600)
        sf.append_cost_event(event)
        alias = self.root / "cost-ledger-alias"
        os.link(sf.COST_LEDGER, alias)
        with self.assertRaises(sf.ShadeformError):
            sf.ledger_spend()
        with self.assertRaises(sf.ShadeformError):
            sf.append_cost_event({**event, "estimated_cost_usd": 2.0})
        alias.unlink()

        original = sf.COST_LEDGER.read_bytes()
        sf.COST_LEDGER.write_bytes(
            original.replace(b'"schema":', b'"schema":"duplicate","schema":', 1)
        )
        with self.assertRaisesRegex(sf.ShadeformError, "duplicate"):
            sf.ledger_spend()
        sf.COST_LEDGER.write_bytes(original)

        with mock.patch.object(
            sf,
            "_require_cost_ledger_path_identity",
            side_effect=[None, None, sf.ShadeformError("simulated path swap")],
        ):
            with self.assertRaisesRegex(sf.ShadeformError, "path swap"):
                sf.append_cost_event(event)

    def test_cost_ledger_fsync_fault_never_reports_append_success(self) -> None:
        sf._ensure_durable_directory(sf.COST_LEDGER.parent)
        event = {
            "instance_id": "instance-ledger-fsync",
            "phase_id": "ledger-fsync-safety",
            "ownership_nonce": "6" * 32,
            "status": "pending",
            "estimated_cost_usd": 1.0,
        }
        with mock.patch.object(sf.os, "fsync", side_effect=OSError("fsync failed")):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                sf.append_cost_event(event)
        # A full row may have reached the descriptor, but the caller observed
        # failure and it remains conservatively pending rather than settled.
        self.assertEqual(sf.ledger_spend(), (0.0, [event["instance_id"]]))

    def test_two_sequential_owners_share_phase_without_reusing_evidence(self) -> None:
        phase = "sequential-owner"
        first = self.record(
            phase=phase, instance="instance-sequential-1", run_id="sequential-run-1",
            key_id="key-sequential-1",
        )
        second = self.record(
            phase=phase, instance="instance-sequential-2", run_id="sequential-run-2",
            nonce="fedcba9876543210fedcba9876543210", key_id="key-sequential-2",
        )

        def complete(record: sf.OwnedResource) -> dict[str, object]:
            self.assertIsNotNone(record.ssh_public_key)
            public_key = str(record.ssh_public_key)
            fingerprint = sf.ssh_public_key_fingerprint(public_key)
            candidate = sf.Candidate(
                record.gpu, record.cloud, record.region,
                str(record.instance_type), record.hourly_usd,
                int(record.vram_gb or 0), str(record.os_image), False,
            )
            for key_id in (None, record.ssh_key_id):
                sf.reserve_create_attempt(
                    phase, record.ownership_nonce, candidate,
                    backstop_hours=3.0,
                    public_key_sha256=hashlib.sha256(
                        public_key.encode("utf-8")
                    ).hexdigest(),
                    expected_budget_cap_usd=50.0,
                    public_key_fingerprint=fingerprint,
                    ssh_key_id=key_id,
                )
            sf.write_owned_resource(record)
            delete_dispatched = False

            def key_provider(_api, method, _path, **_kwargs):
                nonlocal delete_dispatched
                if method == "POST":
                    delete_dispatched = True
                    return {}
                if delete_dispatched:
                    raise sf.ShadeformHTTPError(404, "exact key absent")
                return {
                    "id": record.ssh_key_id,
                    "name": record.ssh_key_name,
                    "public_key": public_key,
                    "status": "active",
                }

            with mock.patch.object(sf, "request", side_effect=key_provider):
                key_result = sf.delete_owned_ssh_key_exact(
                    "fixture-only", phase, record.ssh_key_id,
                    ownership_nonce=record.ownership_nonce,
                    expected_name=record.ssh_key_name,
                    expected_public_key=public_key,
                    expected_fingerprint=fingerprint,
                    record=record,
                )
            self.assertEqual(key_result["status"], "confirmed")
            with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={}), \
                    mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                    mock.patch.object(sf, "append_cost_event"), \
                    mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}):
                return teardown.teardown_exact(phase, record.instance_id, env_file=self.env_file)

        first_receipt = complete(first)
        first_path = teardown._deletion_receipt_path(phase, first)
        second_receipt = complete(second)
        second_path = teardown._deletion_receipt_path(phase, second)

        self.assertEqual(first_receipt["status"], "complete")
        self.assertEqual(second_receipt["status"], "complete")
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(
            teardown.teardown_exact(phase, first.instance_id, env_file=self.env_file)["instance_id"],
            first.instance_id,
        )
        self.assertEqual(
            teardown.teardown_exact(phase, second.instance_id, env_file=self.env_file)["instance_id"],
            second.instance_id,
        )
        with self.assertRaisesRegex(RuntimeError, "no durable exact ownership evidence"):
            teardown.teardown_exact(phase, "instance-sequential-missing", env_file=self.env_file)

    def test_stale_namespaced_intent_is_isolated_from_new_owner(self) -> None:
        phase = "stale-owner-evidence"
        stale = self.record(
            phase=phase, instance="instance-stale-owner-1", run_id="stale-run",
            key_id="key-stale-owner-1",
        )
        current = self.record(
            phase=phase, instance="instance-current-owner-2", run_id="current-run",
            nonce="abcdefabcdefabcdefabcdefabcdefab", key_id="key-current-owner-2",
        )
        sf.write_owned_resource(stale)
        stale_intent = teardown._write_deletion_intent(phase, stale, status="dispatched")
        stale_path = teardown._deletion_intent_path(phase, stale)
        sf.clear_owned_resource(phase, stale.instance_id)
        sf.write_owned_resource(current)

        delete = mock.Mock(return_value={"success": True})
        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={}), \
                mock.patch.object(sf, "_delete_instance", delete), \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
            receipt = teardown.teardown_exact(phase, current.instance_id, env_file=self.env_file)

        self.assertEqual(receipt["instance_id"], current.instance_id)
        self.assertEqual(delete.call_args.args[2], current.instance_id)
        self.assertEqual(
            sf.strict_json_object(stale_path.read_bytes(), label="stale test intent"),
            stale_intent,
        )

    def test_crash_after_new_owner_persistence_creates_its_own_namespace_on_retry(self) -> None:
        record = self.record(
            phase="owner-persist-crash", instance="instance-owner-persist-crash",
            nonce="11111111111111111111111111111111", key_id="key-owner-persist-crash",
        )
        sf.write_owned_resource(record)
        index_path = teardown._deletion_evidence_index_path(record.phase_id, record.instance_id)
        self.assertFalse(index_path.exists())

        with mock.patch.object(sf, "verify_owned_instance_before_delete", return_value={}), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "append_cost_event"), \
                mock.patch.object(sf, "verify_owned_ssh_key_before_delete", return_value={}), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
            receipt = teardown.teardown_exact(
                record.phase_id, record.instance_id, env_file=self.env_file,
            )

        self.assertEqual(receipt["status"], "complete")
        indexed = sf.strict_json_object(index_path.read_bytes(), label="test evidence index")
        self.assertEqual(indexed["owner"], teardown._owner_binding(record))

    def test_malformed_or_different_owner_index_refuses_before_provider_access(self) -> None:
        record = self.record(
            phase="bad-owner-namespace", instance="instance-bad-namespace",
        )
        sf.write_owned_resource(record)
        expected = teardown._bind_owner_evidence(record.phase_id, record)
        path = teardown._deletion_evidence_index_path(record.phase_id, record.instance_id)
        malformed = dict(expected)
        malformed["owner_namespace"] = "0" * 64
        sf._durable_atomic_write(
            path, (json.dumps(malformed, sort_keys=True) + "\n").encode("utf-8"),
        )

        with mock.patch.object(sf, "load_env") as load_env, \
                mock.patch.object(sf, "verify_owned_instance_before_delete") as provider:
            with self.assertRaisesRegex(RuntimeError, "different owner"):
                teardown.teardown_exact(
                    record.phase_id, record.instance_id, env_file=self.env_file,
                )
        load_env.assert_not_called()
        provider.assert_not_called()

    def test_legacy_phase_only_evidence_requires_manual_recovery(self) -> None:
        record = self.record(
            phase="legacy-phase-evidence", instance="instance-after-legacy",
        )
        sf.write_owned_resource(record)
        legacy = sf.RUNTIME_ROOT / f"{record.phase_id}.deletion-receipt.json"
        sf.durable_create_new(legacy, b"{}")

        with mock.patch.object(sf, "load_env") as load_env, \
                mock.patch.object(sf, "verify_owned_instance_before_delete") as provider:
            with self.assertRaisesRegex(RuntimeError, "legacy deletion evidence"):
                teardown.teardown_exact(
                    record.phase_id, record.instance_id, env_file=self.env_file,
                )
        load_env.assert_not_called()
        provider.assert_not_called()

    def test_strict_receipt_and_intent_loaders_reject_untrusted_files(self) -> None:
        record = self.record(phase="strict-evidence", instance="instance-strict-1")
        sf.write_owned_resource(record)
        teardown._bind_owner_evidence(record.phase_id, record)
        receipt_path = teardown._deletion_receipt_path(record.phase_id, record)
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
        intent_path = teardown._deletion_intent_path(record.phase_id, record)
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
