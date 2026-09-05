from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import shadeform_lifecycle as sf
from tests.performance.lifecycle_test_isolation import isolated_lifecycle_execute


ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def isolated_genesis_files():
    with tempfile.TemporaryDirectory(prefix="cost-genesis-") as directory:
        root = Path(directory).resolve()
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        os.chmod(runtime, 0o700)
        markdown = root / "LEDGER.md"
        incidents = runtime / "incidents.jsonl"
        markdown.write_bytes((sf.LEDGER_HEADER + "\n").encode("utf-8"))
        incidents.write_bytes(b'{"incident":"reviewed-fixture"}\n')
        with mock.patch.object(sf, "RUNTIME_ROOT", runtime), \
                mock.patch.object(sf, "MARKDOWN_LEDGER", markdown), \
                mock.patch.object(sf, "COST_LEDGER", runtime / "cost-ledger.jsonl"), \
                mock.patch.object(sf, "INCIDENTS", incidents):
            yield types.SimpleNamespace(
                root=root,
                runtime=runtime,
                markdown=markdown,
                incidents=incidents,
                cost=runtime / "cost-ledger.jsonl",
            )


def genesis_kwargs(paths, **changes):
    values = {
        "program": sf.COST_LEDGER_PROGRAM,
        "currency": sf.COST_LEDGER_CURRENCY,
        "budget_cap_usd": 50.0,
        "prior_settled_spend_usd": 1.25,
        "current_pending_owner_count": 0,
        "expected_display_ledger_sha256": hashlib.sha256(
            paths.markdown.read_bytes()
        ).hexdigest(),
        "expected_incidents_sha256": hashlib.sha256(
            paths.incidents.read_bytes()
        ).hexdigest(),
        "confirmation": sf.COST_LEDGER_GENESIS_CONFIRMATION,
    }
    values.update(changes)
    return values


class CostLedgerGenesisTests(unittest.TestCase):
    def test_cli_reports_created_then_exact_recovery_without_overwrite(self):
        from scripts.shadeform import initialize_cost_ledger as cli

        with isolated_genesis_files() as paths:
            values = genesis_kwargs(paths)
            argv = [
                "--program", values["program"],
                "--currency", values["currency"],
                "--budget-cap-usd", str(values["budget_cap_usd"]),
                "--prior-settled-spend-usd", str(values["prior_settled_spend_usd"]),
                "--current-pending-owner-count", str(values["current_pending_owner_count"]),
                "--expected-display-ledger-sha256", values["expected_display_ledger_sha256"],
                "--expected-incidents-sha256", values["expected_incidents_sha256"],
                "--confirm-reviewed-genesis", values["confirmation"],
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(argv), 0)
                first_bytes = paths.cost.read_bytes()
                self.assertEqual(cli.main(argv), 0)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([row["status"] for row in rows], ["created", "recovered_existing"])
            self.assertEqual(paths.cost.read_bytes(), first_bytes)

    def test_explicit_genesis_is_first_private_event_and_spend_is_counted_once(self):
        with isolated_genesis_files() as paths:
            genesis = sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            self.assertEqual(stat.S_IMODE(paths.cost.stat().st_mode), 0o600)
            self.assertEqual(paths.cost.stat().st_nlink, 1)
            self.assertEqual(sf.ledger_spend(), (1.25, []))
            self.assertEqual(
                sf.remaining_budget_usd({"SHADEFORM_MAX_TOTAL_COST_USD": "50"}),
                48.75,
            )
            nonce = "a" * 32
            sf.append_cost_event({
                "instance_id": "instance-genesis-1",
                "phase_id": "genesis-test",
                "ownership_nonce": nonce,
                "status": "pending",
                "estimated_cost_usd": 2.0,
            })
            sf.append_cost_event({
                "instance_id": "instance-genesis-1",
                "phase_id": "genesis-test",
                "ownership_nonce": nonce,
                "status": "settled",
                "actual_cost_usd": 0.5,
            })
            self.assertEqual(sf.ledger_spend(), (1.75, []))
            rows = sf._cost_ledger_events(paths.cost.read_bytes())
            self.assertEqual(rows[0], genesis)
            self.assertEqual(sum(row.get("event_kind") == "genesis" for row in rows), 1)

    def test_file_and_parent_are_locked_and_fsynced_before_success(self):
        with isolated_genesis_files() as paths:
            original_fsync = sf.os.fsync
            original_flock = sf.fcntl.flock
            synced_modes = []
            lock_operations = []

            def tracked_fsync(descriptor):
                synced_modes.append(sf.os.fstat(descriptor).st_mode)
                return original_fsync(descriptor)

            def tracked_flock(descriptor, operation):
                lock_operations.append((sf.os.fstat(descriptor).st_mode, operation))
                return original_flock(descriptor, operation)

            with mock.patch.object(sf.os, "fsync", side_effect=tracked_fsync), \
                    mock.patch.object(sf.fcntl, "flock", side_effect=tracked_flock):
                sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            self.assertTrue(any(stat.S_ISREG(mode) for mode in synced_modes))
            self.assertTrue(any(stat.S_ISDIR(mode) for mode in synced_modes))
            exclusive_modes = [
                mode for mode, operation in lock_operations
                if operation == sf.fcntl.LOCK_EX
            ]
            self.assertTrue(any(stat.S_ISREG(mode) for mode in exclusive_modes))
            self.assertTrue(any(stat.S_ISDIR(mode) for mode in exclusive_modes))

    def test_parent_fsync_failure_never_reports_success_or_allows_overwrite(self):
        with isolated_genesis_files() as paths:
            original_fsync = sf.os.fsync

            def fail_parent(descriptor):
                if stat.S_ISDIR(sf.os.fstat(descriptor).st_mode):
                    raise OSError("parent fsync denied")
                return original_fsync(descriptor)

            arguments = genesis_kwargs(paths)
            with mock.patch.object(sf.os, "fsync", side_effect=fail_parent):
                with self.assertRaisesRegex(OSError, "parent fsync denied"):
                    sf.initialize_cost_ledger_genesis(**arguments)
            retained = paths.cost.read_bytes()
            recovered = sf.initialize_cost_ledger_genesis(**arguments)
            self.assertEqual(recovered, sf._cost_ledger_events(retained)[0])
            self.assertEqual(paths.cost.read_bytes(), retained)

    def test_rerun_idempotently_verifies_identical_and_refuses_different_baseline(self):
        with isolated_genesis_files() as paths:
            arguments = genesis_kwargs(paths)
            first = sf.initialize_cost_ledger_genesis(**arguments)
            before = paths.cost.read_bytes()
            self.assertEqual(sf.initialize_cost_ledger_genesis(**arguments), first)
            self.assertEqual(paths.cost.read_bytes(), before)
            with self.assertRaisesRegex(sf.ShadeformError, "different genesis"):
                sf.initialize_cost_ledger_genesis(
                    **{**arguments, "prior_settled_spend_usd": 2.0}
                )
            self.assertEqual(paths.cost.read_bytes(), before)

    def test_invalid_reviewed_baselines_fail_without_creating_ledger(self):
        invalid = (
            {"program": "another-program"},
            {"currency": "CAD"},
            {"budget_cap_usd": 0.0},
            {"budget_cap_usd": float("nan")},
            {"prior_settled_spend_usd": -1.0},
            {"prior_settled_spend_usd": 51.0},
            {"current_pending_owner_count": 1},
            {"current_pending_owner_count": False},
            {"confirmation": "not-reviewed"},
            {"expected_display_ledger_sha256": "0" * 64},
            {"expected_incidents_sha256": "f" * 64},
        )
        for changes in invalid:
            with self.subTest(changes=changes), isolated_genesis_files() as paths:
                with self.assertRaises((ValueError, sf.ShadeformError)):
                    sf.initialize_cost_ledger_genesis(
                        **genesis_kwargs(paths, **changes)
                    )
                self.assertFalse(paths.cost.exists())

    def test_hostile_evidence_and_target_paths_fail_closed(self):
        with isolated_genesis_files() as paths:
            outside = paths.root / "outside"
            outside.write_bytes(paths.incidents.read_bytes())
            paths.incidents.unlink()
            paths.incidents.symlink_to(outside)
            with self.assertRaises(sf.ShadeformError):
                sf.initialize_cost_ledger_genesis(
                    **genesis_kwargs(
                        paths,
                        expected_incidents_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                    )
                )
            self.assertFalse(paths.cost.exists())

        with isolated_genesis_files() as paths:
            alias = paths.root / "incidents-alias"
            os.link(paths.incidents, alias)
            with self.assertRaises(sf.ShadeformError):
                sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            self.assertFalse(paths.cost.exists())

        with isolated_genesis_files() as paths:
            paths.incidents.write_bytes(b"x" * (sf.MAX_INCIDENT_EVIDENCE_BYTES + 1))
            with self.assertRaises(sf.ShadeformError):
                sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            self.assertFalse(paths.cost.exists())

        with isolated_genesis_files() as paths:
            target = paths.root / "existing"
            target.write_bytes(b"do-not-touch")
            os.link(target, paths.cost)
            with self.assertRaises(sf.ShadeformError):
                sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            self.assertEqual(target.read_bytes(), b"do-not-touch")

        with isolated_genesis_files() as paths:
            os.chmod(paths.runtime, 0o755)
            with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
                sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            self.assertFalse(paths.cost.exists())

    def test_authoritative_reads_and_appends_require_private_owner_and_parent(self):
        with isolated_genesis_files() as paths:
            sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            event = {
                "instance_id": "instance-permissions",
                "phase_id": "ledger-permissions",
                "ownership_nonce": "c" * 32,
                "status": "pending",
                "estimated_cost_usd": 1.0,
            }
            os.chmod(paths.cost, 0o666)
            for operation in (
                sf.ledger_spend,
                lambda: sf.exact_owner_cost_state(
                    event["phase_id"], event["ownership_nonce"], event["instance_id"],
                ),
                lambda: sf.append_cost_event(event),
            ):
                with self.subTest(operation=operation), self.assertRaises(sf.ShadeformError):
                    operation()
            os.chmod(paths.cost, 0o600)
            os.chmod(paths.runtime, 0o755)
            with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
                sf.ledger_spend()
            with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
                sf.append_cost_event(event)

        with isolated_genesis_files() as paths:
            sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            with mock.patch.object(sf.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(sf.ShadeformError):
                    sf.ledger_spend()

    def test_publication_path_swap_is_detected_and_never_reports_success(self):
        with isolated_genesis_files() as paths:
            original_stat = sf.os.stat

            def swapped(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if kwargs.get("dir_fd") is None and Path(path) == paths.cost:
                    values = list(result)
                    values[1] += 1
                    return os.stat_result(values)
                return result

            with mock.patch.object(sf.os, "stat", side_effect=swapped):
                with self.assertRaisesRegex(sf.ShadeformError, "identity.*unsafe"):
                    sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))

    def test_loader_rejects_missing_duplicate_or_late_genesis_and_cap_mismatch(self):
        with isolated_genesis_files() as paths:
            genesis = sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            with self.assertRaisesRegex(sf.BudgetError, "does not match"):
                sf.remaining_budget_usd({"SHADEFORM_MAX_TOTAL_COST_USD": "51"})
            canonical = json.dumps(genesis, sort_keys=True, separators=(",", ":"))
            paths.cost.write_text(
                canonical.replace('"program":', '"program":"duplicate","program":', 1)
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(sf.ShadeformError, "duplicate"):
                sf.ledger_spend()

        with isolated_genesis_files() as paths:
            genesis = sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            canonical = json.dumps(genesis, sort_keys=True, separators=(",", ":"))
            paths.cost.write_text(canonical + "\n" + canonical + "\n", encoding="utf-8")
            with self.assertRaisesRegex(sf.ShadeformError, "unique first"):
                sf.ledger_spend()

        with isolated_genesis_files() as paths:
            owner = {
                "instance_id": "instance-before-genesis",
                "phase_id": "before-genesis",
                "ownership_nonce": "b" * 32,
                "status": "pending",
                "estimated_cost_usd": 1.0,
            }
            stored = sf._canonical_cost_event(owner, stored=False)
            stored["recorded_at_utc"] = "2026-01-01T00:00:00+00:00"
            stored = sf._canonical_cost_event(stored, stored=True)
            paths.cost.write_text(
                json.dumps(stored, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            paths.cost.chmod(0o600)
            with self.assertRaisesRegex(sf.ShadeformError, "no reviewed genesis"):
                sf.remaining_budget_usd({"SHADEFORM_MAX_TOTAL_COST_USD": "50"})

    def test_generic_append_cannot_create_budget_authority(self):
        with isolated_genesis_files() as paths:
            with self.assertRaisesRegex(sf.ShadeformError, "explicit genesis"):
                sf.append_cost_event({
                    "instance_id": "instance-no-authority",
                    "phase_id": "no-authority",
                    "ownership_nonce": "d" * 32,
                    "status": "pending",
                    "estimated_cost_usd": 0.1,
                })
            self.assertFalse(paths.cost.exists())
            candidate = sf.Candidate(
                "A100", "cloud", "region", "a100", 0.5, 80, "ubuntu", False,
            )
            with self.assertRaisesRegex(sf.BudgetError, "explicit genesis"):
                sf.reserve_create_attempt(
                    "missing-authority", "e" * 32, candidate,
                    backstop_hours=1.0,
                    public_key_sha256="f" * 64,
                    expected_budget_cap_usd=50.0,
                )
            self.assertFalse(paths.cost.exists())

    def test_reservation_refuses_cap_mismatch_permission_drift_and_corruption(self):
        candidate = sf.Candidate(
            "A100", "cloud", "region", "a100", 0.5, 80, "ubuntu", False,
        )
        with isolated_genesis_files() as paths:
            sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            before = paths.cost.read_bytes()
            with self.assertRaisesRegex(sf.BudgetError, "does not match"):
                sf.reserve_create_attempt(
                    "cap-mismatch", "5" * 32, candidate,
                    backstop_hours=1.0,
                    public_key_sha256="a" * 64,
                    expected_budget_cap_usd=49.0,
                )
            self.assertEqual(paths.cost.read_bytes(), before)
            paths.cost.chmod(0o666)
            with self.assertRaises(sf.ShadeformError):
                sf.reserve_create_attempt(
                    "mode-drift", "6" * 32, candidate,
                    backstop_hours=1.0,
                    public_key_sha256="a" * 64,
                    expected_budget_cap_usd=50.0,
                )
            paths.cost.chmod(0o600)
            paths.cost.write_bytes(
                before.replace(b'"schema":', b'"schema":"duplicate","schema":', 1)
            )
            with self.assertRaisesRegex(sf.ShadeformError, "duplicate"):
                sf.reserve_create_attempt(
                    "corrupt-ledger", "7" * 32, candidate,
                    backstop_hours=1.0,
                    public_key_sha256="a" * 64,
                    expected_budget_cap_usd=50.0,
                )

    def test_two_stale_launchers_reserve_atomically_and_loser_never_mutates(self):
        with isolated_genesis_files() as paths:
            sf.initialize_cost_ledger_genesis(
                **genesis_kwargs(
                    paths, budget_cap_usd=1.0, prior_settled_spend_usd=0.0,
                )
            )
            candidate = sf.Candidate(
                "A100", "hyperstack", "region", "a100-80", 0.75, 80,
                "ubuntu", False,
            )
            barrier = threading.Barrier(2)
            result: dict[str, str] = {}
            provider_calls = {
                "j1m": (mock.Mock(), mock.Mock()),
                "external": (mock.Mock(), mock.Mock()),
            }

            def launcher(label: str, nonce: str) -> None:
                # Both actors see the same stale advisory balance. Authority is
                # decided only by reserve_create_attempt's locked re-read.
                result[f"{label}_advisory"] = str(sf.remaining_budget_usd({
                    "SHADEFORM_MAX_TOTAL_COST_USD": "1",
                }))
                barrier.wait(timeout=2)
                try:
                    sf.reserve_create_attempt(
                        f"{label}-atomic", nonce, candidate,
                        backstop_hours=1.0,
                        public_key_sha256="a" * 64,
                        expected_budget_cap_usd=1.0,
                    )
                except sf.BudgetError:
                    result[label] = "rejected"
                    return
                result[label] = "reserved"
                provider_calls[label][0]("ssh-key-post")
                provider_calls[label][1]("instance-post")

            threads = [
                threading.Thread(target=launcher, args=("j1m", "1" * 32)),
                threading.Thread(target=launcher, args=("external", "2" * 32)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(result["j1m_advisory"], "1.0")
            self.assertEqual(result["external_advisory"], "1.0")
            self.assertEqual(
                sorted((result["j1m"], result["external"])),
                ["rejected", "reserved"],
            )
            rejected = "j1m" if result["j1m"] == "rejected" else "external"
            provider_calls[rejected][0].assert_not_called()
            provider_calls[rejected][1].assert_not_called()
            events = sf.cost_ledger_events()
            pending = [event for event in events if event.get("status") == "pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["estimated_cost_usd"], 0.75)

    def test_launcher_sources_pass_reviewed_cap_to_both_reservations(self):
        for relative in (
            "scripts/j1m_orchestrator.py",
            "scripts/shadeform/remote_external_tools.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertEqual(source.count("expected_budget_cap_usd=budget_cap_usd"), 2)

    def test_key_delete_accepted_but_present_retries_by_get_without_second_delete(self):
        with isolated_genesis_files() as paths:
            sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            nonce = "3" * 32
            key_id = "key-atomic-123"
            key_name = f"j1m-{nonce}"
            public_key = "ssh-ed25519 AAAA fixture"
            fingerprint = sf.ssh_public_key_fingerprint(public_key)
            candidate = sf.Candidate(
                "A100", "hyperstack", "region", "a100-80", 0.75, 80,
                "ubuntu", False,
            )
            for exact_key in (None, key_id):
                sf.reserve_create_attempt(
                    "key-delete-proof", nonce, candidate,
                    backstop_hours=1.0,
                    public_key_sha256=hashlib.sha256(public_key.encode()).hexdigest(),
                    expected_budget_cap_usd=50.0,
                    public_key_fingerprint=fingerprint,
                    ssh_key_id=exact_key,
                )
            created = sf.utc_now()
            record = sf.OwnedResource(
                phase_id="key-delete-proof",
                run_id="key-delete-proof-run",
                instance_id="instance-key-delete-proof",
                instance_name=sf.owned_instance_name("key-delete-proof-run", nonce),
                ownership_nonce=nonce,
                ssh_key_id=key_id,
                ssh_key_name=key_name,
                ssh_public_key=public_key,
                ssh_public_key_fingerprint=fingerprint,
                gpu="A100",
                cloud="hyperstack",
                region="region",
                instance_type="a100-80",
                gpu_count=1,
                vram_gb=80,
                os_image="ubuntu",
                hourly_usd=0.75,
                created_at_utc=created.isoformat(),
                provider_delete_deadline_utc=(created + sf.timedelta(hours=2)).isoformat(),
            )
            sf.write_owned_resource(record)
            calls: list[tuple[str, str]] = []
            absent = False

            def provider(_api, method, path, **_kwargs):
                nonlocal absent
                calls.append((method, path))
                if method == "POST":
                    return {}
                if absent:
                    raise sf.ShadeformHTTPError(404, "gone")
                return {
                    "id": key_id,
                    "name": key_name,
                    "public_key": public_key,
                    "status": "deleting" if any(call[0] == "POST" for call in calls) else "active",
                }

            with mock.patch.object(sf, "request", side_effect=provider):
                with self.assertRaises(sf.AmbiguousProviderOutcome):
                    sf.delete_owned_ssh_key_exact(
                        "not-a-secret", "key-delete-proof", key_id,
                        ownership_nonce=nonce,
                        expected_name=key_name,
                        expected_public_key=public_key,
                        expected_fingerprint=fingerprint,
                        record=record,
                    )
                self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
                self.assertIsNotNone(sf._read_ssh_key_delete_evidence(
                    sf._ssh_key_delete_owner(
                        "key-delete-proof", nonce, key_id,
                        expected_name=key_name,
                        expected_public_key=public_key,
                        expected_fingerprint=fingerprint,
                        record=None,
                    ),
                    "intent",
                ))
                absent = True
                result = sf.delete_owned_ssh_key_exact(
                    "not-a-secret", "key-delete-proof", key_id,
                    ownership_nonce=nonce,
                    expected_name=key_name,
                    expected_public_key=public_key,
                    expected_fingerprint=fingerprint,
                    record=record,
                )
            self.assertEqual(result, {"status": "confirmed", "evidence": "provider-404"})
            self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
            self.assertTrue(sf.ssh_key_deletion_is_confirmed(
                "key-delete-proof", key_id,
                ownership_nonce=nonce,
                expected_name=key_name,
                expected_fingerprint=fingerprint,
            ))

    def test_key_delete_wrong_profile_and_malformed_intent_fail_before_transport(self):
        with isolated_genesis_files() as paths:
            sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            nonce = "4" * 32
            key_id = "key-profile-123"
            key_name = f"j1m-{nonce}"
            public_key = "ssh-ed25519 AAAA fixture"
            fingerprint = sf.ssh_public_key_fingerprint(public_key)
            candidate = sf.Candidate("A100", "cloud", "region", "a100", 0.5, 80, "ubuntu", False)
            for exact_key in (None, key_id):
                sf.reserve_create_attempt(
                    "key-profile", nonce, candidate, backstop_hours=1.0,
                    public_key_sha256=hashlib.sha256(public_key.encode()).hexdigest(),
                    expected_budget_cap_usd=50.0,
                    public_key_fingerprint=fingerprint,
                    ssh_key_id=exact_key,
                )
            provider = mock.Mock()
            with mock.patch.object(sf, "request", provider):
                with self.assertRaisesRegex(sf.ShadeformError, "reservation history"):
                    sf.delete_owned_ssh_key_exact(
                        "not-a-secret", "key-profile", key_id,
                        ownership_nonce=nonce,
                        expected_name="different-name",
                        expected_public_key=public_key,
                        expected_fingerprint=fingerprint,
                    )
            provider.assert_not_called()

            owner = sf._ssh_key_delete_owner(
                "key-profile", nonce, key_id,
                expected_name=key_name,
                expected_public_key=public_key,
                expected_fingerprint=fingerprint,
                record=None,
            )
            path = sf._ssh_key_delete_evidence_path(owner, "intent")
            path.write_bytes(b'{"schema":"x","schema":"y"}\n')
            path.chmod(0o600)
            with mock.patch.object(sf, "request") as provider:
                with self.assertRaisesRegex(sf.ShadeformError, "duplicate"):
                    sf.delete_owned_ssh_key_exact(
                        "not-a-secret", "key-profile", key_id,
                        ownership_nonce=nonce,
                        expected_name=key_name,
                        expected_public_key=public_key,
                        expected_fingerprint=fingerprint,
                    )
            provider.assert_not_called()

    def test_teardown_completes_only_after_key_confirmation(self):
        source = (ROOT / "scripts" / "shadeform_teardown.py").read_text(encoding="utf-8")
        start = source.index("elif pre_key_receipt_persisted:")
        finish = source.index("elif final_receipt_persisted", start)
        block = source[start:finish]
        self.assertLess(
            block.index("delete_owned_ssh_key_exact"),
            block.index('receipt["status"] = "complete"'),
        )
        self.assertLess(
            block.index('receipt["status"] = "complete"'),
            block.index("clear_owned_resource"),
        )
        lifecycle_source = (ROOT / "scripts" / "shadeform_lifecycle.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(lifecycle_source.count("_delete_ssh_key_once("), 2)
        for relative in (
            "scripts/j1m_orchestrator.py",
            "scripts/shadeform/remote_external_tools.py",
            "scripts/shadeform_watchdog.py",
            "scripts/shadeform_teardown.py",
        ):
            caller = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("_delete_ssh_key_once", caller)

    @isolated_lifecycle_execute
    def test_planning_stops_before_catalogue_until_genesis_and_gate_stays_false(self):
        with isolated_genesis_files() as paths:
            with mock.patch.object(sf, "_fetch_instance_types") as catalogue:
                with self.assertRaisesRegex(sf.ShadeformError, "absent"):
                    sf.list_candidates(
                        "not-a-secret", {"SHADEFORM_MAX_TOTAL_COST_USD": "50"},
                        phase_id="genesis-gate", min_vram_gb=80,
                        max_runtime_hours=0.25,
                    )
            catalogue.assert_not_called()
            with mock.patch.object(sf, "request") as provider, \
                    mock.patch("subprocess.Popen") as process:
                sf.initialize_cost_ledger_genesis(**genesis_kwargs(paths))
            provider.assert_not_called()
            process.assert_not_called()
            with mock.patch.object(sf, "_fetch_instance_types", return_value=[]) as catalogue:
                self.assertEqual(sf.list_candidates(
                    "not-a-secret", {"SHADEFORM_MAX_TOTAL_COST_USD": "50"},
                    phase_id="genesis-gate", min_vram_gb=80,
                    max_runtime_hours=0.25,
                ), [])
            catalogue.assert_called_once()

        spec = importlib.util.spec_from_file_location(
            "remote_external_tools_genesis_gate",
            ROOT / "scripts" / "shadeform" / "remote_external_tools.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertFalse(module.REMOTE_EXECUTION_ENABLED)
        with mock.patch.object(module.shadeform, "load_env") as load_env:
            with self.assertRaisesRegex(module.RunnerError, "gated"):
                module.execute(types.SimpleNamespace())
        load_env.assert_not_called()


if __name__ == "__main__":
    unittest.main()
