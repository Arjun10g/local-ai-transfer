import contextlib
import importlib.util
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.performance.lifecycle_test_isolation import (
    direct_execute_methods,
    install_lifecycle_execute_isolation,
)


ROOT = Path(__file__).resolve().parents[2]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "remote_external_tools_lifecycle",
        ROOT / "scripts" / "shadeform" / "remote_external_tools.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_watchdog_module():
    spec = importlib.util.spec_from_file_location(
        "shadeform_watchdog_test", ROOT / "scripts" / "shadeform_watchdog.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stored_cost_event(shadeform, event):
    canonical = shadeform._canonical_cost_event(event, stored=False)
    canonical["recorded_at_utc"] = "2026-01-01T00:00:00+00:00"
    return shadeform._canonical_cost_event(canonical, stored=True)


class FakeWatchdog:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def valid_receipt(run_id="test-run"):
    fixed_ids = [
        "graph.strict-arguments", "graph.bounded-hostile-projection", "graph.malformed-response",
        "graph.write-replay", "graph.draft-toctou", "graph.cancel-and-timeout", "graph.egress-boundary",
        "browser.private-and-malformed-destination", "browser.production-mutation-gate",
        "browser.hostile-resolver-and-bounds", "browser.cdp-response-bound",
        "copilot.legacy-fail-closed", "copilot.acp-session-binding", "copilot.acp-oversize",
        "grants.revoke-generation",
    ]
    return {
        "schema_version": "remote-external-tools-qa.v1",
        "run_id": run_id,
        "remote_marker_verified": True,
        "status": "PASS",
        "git_revision": "a" * 40,
        "seeds": [17],
        "bounds": {
            "max_fuzz_cases": 256,
            "max_soak_iterations": 1000,
            "max_cases": 1536,
            "max_response_bytes": 262144,
        },
        "cases": [{"id": case_id, "status": "PASS", "assertions": 1, "duration_ms": 2} for case_id in fixed_ids],
        "aggregate": {
            "passed": 15,
            "failed": 0,
            "case_count": 15,
            "requests": 15,
            "assertion_count": 15,
            "formula": "case-evidence-v1",
            "fuzz_cases": 0,
            "soak_iterations": 0,
            "seeds": [17],
        },
        "secret_free": True,
        "limitations": ["Synthetic hostile-emulator evidence only."],
    }


class RemoteExternalToolsReceiptTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.lifecycle_paths = install_lifecycle_execute_isolation(
            self, self.module.shadeform, prefix="remote-receipt-runtime-"
        )

    def validate(self, data):
        return self.module.validate_receipt(
            data, expected_run_id="test-run", expected_seeds=(17,),
            expected_fuzz_cases=0, expected_soak_iterations=0,
        )

    def test_strict_receipt_accepts_complete_producer_shape(self):
        result = self.validate(json.dumps(valid_receipt()).encode())
        self.assertEqual(result, {"status": "PASS", "run_id": "test-run", "secret_free": True, "passed": 15, "failed": 0, "case_count": 15})

    def test_fixed_case_contract_matches_shipped_javascript(self):
        source = (ROOT / "scripts" / "test" / "remote_external_tools_qa.mjs").read_text(encoding="utf-8")
        for case_id in self.module.FIXED_QA_CASE_IDS:
            self.assertIn(repr(case_id).replace("'", "'"), source.replace('"', "'"))
        self.assertEqual(len(self.module.FIXED_QA_CASE_IDS), 15)

    def test_duplicate_unknown_and_contradictory_receipts_fail(self):
        duplicate = b'{"schema_version":"remote-external-tools-qa.v1","schema_version":"remote-external-tools-qa.v1"}'
        with self.assertRaisesRegex(self.module.RunnerError, "duplicate"):
            self.validate(duplicate)
        unknown = valid_receipt()
        unknown["extra"] = True
        with self.assertRaises(self.module.RunnerError):
            self.validate(json.dumps(unknown).encode())
        contradictory = valid_receipt()
        contradictory["aggregate"]["passed"] = 0
        with self.assertRaisesRegex(self.module.RunnerError, "aggregate"):
            self.validate(json.dumps(contradictory).encode())

    def test_case_ids_and_error_codes_are_finite_and_unique(self):
        duplicate = valid_receipt()
        duplicate["cases"][1]["id"] = duplicate["cases"][0]["id"]
        with self.assertRaisesRegex(self.module.RunnerError, "unique"):
            self.validate(json.dumps(duplicate).encode())
        invalid_error = valid_receipt()
        invalid_error["cases"][0] = {"id": "fixed-case-0", "status": "FAIL", "assertions": 1, "duration_ms": 1, "error": "arbitrary_provider_message"}
        invalid_error["aggregate"]["passed"] = 13
        invalid_error["aggregate"]["failed"] = 1
        with self.assertRaisesRegex(self.module.RunnerError, "finite"):
            self.validate(json.dumps(invalid_error).encode())
        totals = valid_receipt()
        totals["aggregate"]["requests"] = 13
        with self.assertRaisesRegex(self.module.RunnerError, "transport totals"):
            self.validate(json.dumps(totals).encode())

    def test_malformed_oversize_and_secret_bearing_receipts_fail(self):
        with self.assertRaises(self.module.RunnerError):
            self.validate(b"{malformed")
        with self.assertRaisesRegex(self.module.RunnerError, "byte bound"):
            self.validate(b"x" * (self.module.MAX_RECEIPT_BYTES + 1))
        secret = valid_receipt()
        secret["limitations"] = ["Bearer secret-material"]
        with self.assertRaisesRegex(self.module.RunnerError, "credential"):
            self.validate(json.dumps(secret).encode())

    def test_bounded_file_refuses_oversize_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_bytes(b"x" * 33)
            with self.assertRaisesRegex(self.module.RunnerError, "byte bound"):
                self.module._bounded_file(path, 32)

    def test_repository_commit_supports_normal_git_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("a" * 40, encoding="ascii")
            with mock.patch.object(self.module.subprocess, "run", return_value=types.SimpleNamespace(stdout="")):
                self.assertEqual(self.module._repository_commit(root), "a" * 40)

    def test_repository_commit_supports_linked_worktree_gitfile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git_dir = root / "linked-git"
            common = root / "common-git"
            (common / "refs" / "heads").mkdir(parents=True)
            git_dir.mkdir()
            (root / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            (git_dir / "commondir").write_text(str(common), encoding="ascii")
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
            (common / "refs" / "heads" / "main").write_text("b" * 40, encoding="ascii")
            with mock.patch.object(self.module.subprocess, "run", return_value=types.SimpleNamespace(stdout="")):
                self.assertEqual(self.module._repository_commit(root), "b" * 40)

    def test_every_execute_calling_test_method_has_runtime_isolation(self):
        found = direct_execute_methods(ROOT / "tests" / "performance")
        # This census is duplicated in `test_j1m_lifecycle.py`; the comparator
        # merge added a thirteenth execute-calling test and updated only that
        # copy, so this one was stale and red on `main`. Both must agree.
        self.assertEqual(len(found), 13)
        self.assertTrue(
            all(item["isolated"] for item in found),
            [item for item in found if not item["isolated"]],
        )

    def test_instance_post_intent_is_bound_before_dispatch(self):
        events = []
        candidate = self.module.shadeform.Candidate("A100", "cloud", "region", "type", 1.0, 80, "ubuntu", False)
        with mock.patch.object(self.module.shadeform, "append_cost_event", side_effect=events.append):
            result = self.module.shadeform.append_instance_create_intent(
                "qa-remote-tools", "a" * 32, instance_name="ep-run-" + "a" * 32,
                ssh_key_id="key-123456", hourly_usd=candidate.hourly_usd,
                backstop_hours=0.3125,
                provider_delete_deadline_utc="2026-09-05T00:18:45+00:00",
                started_at_utc="2026-09-05T00:00:00+00:00",
            )
        self.assertEqual(result, "attempt-" + "a" * 32)
        self.assertEqual(events[0]["reservation"], "instance-create-intent")
        self.assertEqual(
            events[0]["intent_schema"],
            self.module.shadeform.INSTANCE_CREATE_INTENT_SCHEMA,
        )
        self.assertEqual(events[0]["instance_name"], "ep-run-" + "a" * 32)
        self.assertEqual(events[0]["ssh_key_id"], "key-123456")

    def test_salvage_path_rejects_malformed_and_oversize_receipts(self):
        for payload in (b"{malformed", b"x" * (self.module.MAX_RECEIPT_BYTES + 1)):
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "receipt.json"
                def transport(*_args, **_kwargs):
                    target = Path(_args[0][-1])
                    target.write_bytes(payload)
                    return {"status": "completed", "exit_code": 0, "stdout_bytes": 0, "stderr_bytes": 0}
                with mock.patch.object(self.module, "run_argv", side_effect=transport):
                    result = self.module._salvage_receipt(
                        scp=["scp"], info={"ssh_user": "runner", "ip": "203.0.113.10"},
                        remote_root="/scratch/test", destination=destination, run_id="test-run",
                        seeds=(17,), fuzz_cases=0, soak_iterations=0,
                        deadline=time.monotonic() + 1000, allowed_root=Path(directory),
                    )
                self.assertEqual(result["status"], "salvage_failed")


class RemoteExternalToolsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.lifecycle_paths = install_lifecycle_execute_isolation(
            self, self.module.shadeform, prefix="remote-lifecycle-runtime-"
        )
        self.candidate = self.module.shadeform.Candidate(
            "A100 80GB", "test-cloud", "test-region", "test-a100",
            1.0, 80, "ubuntu22.04_cuda12.4_shade_os", False,
        )
        self.env = {
            "SOL_SHADEFORM_REVIEWED": "1",
            "SOL_REMOTE_EXTERNAL_TOOLS_REVIEWED": "1",
            "SHADEFORM_API_KEY": "not-a-real-secret",
            "SHADEFORM_SSH": "ownership-input",
            "SHADEFORM_QA_APPROVED_GPU": self.candidate.gpu,
            "SHADEFORM_QA_APPROVED_CLOUD": self.candidate.cloud,
            "SHADEFORM_QA_APPROVED_REGION": self.candidate.region,
            "SHADEFORM_QA_APPROVED_INSTANCE_TYPE": self.candidate.instance_type,
        }
        self.args = types.SimpleNamespace(
            phase_id="qa-remote-tools", run_id="test-run", runtime_hours=0.25,
            fuzz_cases=1, soak_iterations=1, seeds=(17,),
            env_file=Path("/nonexistent/test.env"), output=ROOT / "experiments" / "results" / "test-run-repair.json",
        )

    def test_execute_isolation_provisions_private_json_authority(self):
        mode = self.lifecycle_paths.runtime_root.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)
        events = self.module.shadeform.cost_ledger_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_kind"], "genesis")
        self.assertEqual(events[0]["budget_cap_usd"], 50.0)

    def run_lifecycle(self, *, activation_error=None, checked_error=None, job_result=None,
                      job_error=None, salvage=None, deletion_error=None, deletion_receipt=None,
                      create_error=None, instance_pending_error=False,
                      direct_cost_error=False, direct_attempt_error=False,
                      record_persistence_error=False, real_recovery_ledger=False):
        events = []
        persisted = []
        teardown_deadlines = []
        watchdog_commands = []
        recovery_records = []
        watchdog = FakeWatchdog()
        info = {"id": "instance-123456", "ip": "203.0.113.10", "ssh_user": "runner", "ssh_port": 22}

        failed_pending = False
        def append_event(event):
            nonlocal failed_pending
            if (instance_pending_error and not failed_pending and
                    event.get("instance_id") == "instance-123456" and event.get("status") == "pending"):
                failed_pending = True
                raise OSError("cost ledger unavailable")
            if direct_cost_error and event.get("instance_id") == "instance-123456" and event.get("status") == "settled":
                raise OSError("direct cost ledger unavailable")
            if direct_attempt_error and event.get("instance_id") == "attempt-" + "a" * 32 and event.get("status") == "settled":
                raise OSError("direct attempt ledger unavailable")
            events.append(dict(event))

        def teardown(*_args, **_kwargs):
            teardown_deadlines.append(_kwargs.get("deadline"))
            if deletion_error is not None:
                raise deletion_error
            append_event({"instance_id": "instance-123456", "phase_id": self.args.phase_id, "status": "settled", "actual_cost_usd": 0.01})
            return deletion_receipt or {
                "deletion": {"success": True}, "status": "complete",
                "retry_required": False,
            }

        def recovered_teardown(record, *_args, **_kwargs):
            teardown_deadlines.append(_kwargs.get("deadline"))
            if deletion_error is not None:
                raise deletion_error
            append_event({"instance_id": record.instance_id, "phase_id": self.args.phase_id, "status": "settled", "actual_cost_usd": 0.01})
            append_event({"instance_id": "attempt-" + "a" * 32, "phase_id": self.args.phase_id, "status": "settled", "actual_cost_usd": 0.0})
            return deletion_receipt or {
                "deletion": {"success": True}, "status": "complete",
                "retry_required": False,
            }

        def checked(*_args, **kwargs):
            stage = kwargs["stage"]
            kwargs["lifecycle"]["stage"] = stage
            if checked_error is not None and checked_error[0] in stage:
                raise checked_error[1]
            if stage == "setup:sha256sum":
                return {"status": "completed", "exit_code": 0, "stdout": f"{self.module.NODE_SHA256}  /tmp/{self.module.NODE_ARCHIVE}\n", "stdout_bytes": 100, "stderr_bytes": 0}
            if stage.startswith("verify-upload:"):
                relative = stage.split(":", 1)[1]
                digest = next(item["sha256"] for item in self.module.closure_manifest() if item["path"] == relative)
                return {"status": "completed", "exit_code": 0, "stdout": f"{digest}  {Path(relative).name}\n", "stdout_bytes": 100, "stderr_bytes": 0}
            return {"status": "completed", "exit_code": 0, "stdout_bytes": 0, "stderr_bytes": 0}

        def run_job(*_args, **_kwargs):
            if job_error is not None:
                raise job_error
            return job_result or {"status": "completed", "exit_code": 0, "stdout_bytes": 0, "stderr_bytes": 0}

        def persist(_phase, lifecycle):
            persisted.append(json.loads(json.dumps(lifecycle)))

        def popen(argv, **_kwargs):
            watchdog_commands.append(list(argv))
            return watchdog

        def add_key_before_provider_mutation(*_args, **_kwargs):
            self.assertTrue(watchdog_commands, "watchdog must be prearmed before SSH-key mutation")
            return "key-123456"

        def delete_key_after_exact_settlement(*_args, **_kwargs):
            if real_recovery_ledger:
                instance_state = self.module.shadeform.exact_owner_cost_state(
                    self.args.phase_id, "a" * 32, "instance-123456",
                )
                attempt_state = self.module.shadeform.exact_owner_cost_state(
                    self.args.phase_id, "a" * 32, "attempt-" + "a" * 32,
                )
                self.assertEqual(instance_state["status"], "settled")
                self.assertEqual(attempt_state["status"], "settled")
                self.assertIsNotNone(
                    self.module.shadeform.read_recovery_owned_resource(
                        self.args.phase_id,
                    )
                )
            return {"status": "confirmed"}

        reserve_calls = []
        real_reserve = self.module.shadeform.reserve_create_attempt
        def reserve(*_args, **kwargs):
            reserve_calls.append(kwargs)
            if real_recovery_ledger:
                return real_reserve(*_args, **kwargs)
            append_event({"instance_id": "attempt-" + "a" * 32, "phase_id": self.args.phase_id, "status": "pending", "estimated_cost_usd": 0.4})
            return "attempt-" + "a" * 32

        real_write_owned = self.module.shadeform.write_owned_resource
        real_recovered_teardown = self.module.shadeform_teardown_recovered
        owned_write_calls = 0
        def write_owned(record):
            nonlocal owned_write_calls
            owned_write_calls += 1
            if record_persistence_error and owned_write_calls == 1:
                raise OSError("owned record unavailable")
            return real_write_owned(record)

        def recovered_with_real_ledger(record, *_args, **kwargs):
            recovery_records.append(record)
            first = real_recovered_teardown(record, self.args.env_file, **kwargs)
            # Exercise the same launcher fallback after a restart/completed
            # receipt. Immutable owner evidence must make this idempotent.
            second = real_recovered_teardown(record, self.args.env_file, **kwargs)
            self.assertEqual(second, first)
            return first

        with contextlib.ExitStack() as stack:
            patch = stack.enter_context
            patch(mock.patch.object(self.module, "REMOTE_EXECUTION_ENABLED", True))
            patch(mock.patch.object(self.module.shadeform, "load_env", return_value=self.env))
            patch(mock.patch.object(self.module, "_repository_commit", return_value="a" * 40))
            patch(mock.patch.object(self.module, "_commit_closure_manifest", return_value=self.module.closure_manifest()))
            patch(mock.patch.object(self.module.shadeform, "_auto_delete", return_value={"date_threshold": (self.module.shadeform.utc_now() + self.module.shadeform.timedelta(seconds=1125)).isoformat(), "spend_threshold": "1"}))
            patch(mock.patch.object(self.module.shadeform, "list_candidates", return_value=[self.candidate]))
            patch(mock.patch.object(self.module.shadeform, "new_ownership_nonce", return_value="a" * 32))
            patch(mock.patch.object(self.module.shadeform, "create_ephemeral_ssh_key", return_value=(Path("/tmp/id"), "ssh-ed25519 AAAA test")))
            patch(mock.patch.object(self.module.shadeform, "ssh_public_key_fingerprint", return_value="A" * 43))
            patch(mock.patch.object(self.module.shadeform, "reserve_create_attempt", side_effect=reserve))
            add_key = patch(mock.patch.object(self.module.shadeform, "add_ssh_key", side_effect=add_key_before_provider_mutation))
            patch(mock.patch.object(self.module.shadeform, "verify_ssh_key_ownership", return_value={}))
            patch(mock.patch.object(self.module.shadeform, "instance_info", return_value=info))
            patch(mock.patch.object(self.module.shadeform, "create_instance", return_value="instance-123456", side_effect=create_error))
            direct_delete = patch(mock.patch.object(self.module.shadeform, "_delete_instance", return_value={"success": True, "status": "deleted"}))
            delete_key = patch(mock.patch.object(
                self.module.shadeform,
                "delete_owned_ssh_key_exact",
                side_effect=delete_key_after_exact_settlement,
            ))
            if not real_recovery_ledger:
                patch(mock.patch.object(self.module.shadeform, "append_cost_event", side_effect=append_event))
            patch(mock.patch.object(self.module.shadeform, "write_owned_resource", side_effect=write_owned if record_persistence_error else None))
            if record_persistence_error and not real_recovery_ledger:
                patch(mock.patch.object(self.module.shadeform, "read_owned_resource", return_value=types.SimpleNamespace(instance_id="instance-123456", status="created", cost_usd=None)))
            patch(mock.patch.object(self.module.shadeform, "process_start_marker", return_value="marker"))
            patch(mock.patch.object(self.module.subprocess, "Popen", side_effect=popen))
            patch(mock.patch.object(self.module.shadeform, "wait_active", side_effect=activation_error or [info]))
            patch(mock.patch.object(self.module.shadeform, "verify_instance_ownership"))
            patch(mock.patch.object(self.module.shadeform, "acquire_pinned_host_key", return_value={"status": "verified", "proof": "provider-fingerprint", "fingerprint": "SHA256:test"}))
            patch(mock.patch.object(self.module.shadeform, "ssh_base", return_value=["ssh"]))
            patch(mock.patch.object(self.module.shadeform, "scp_base", return_value=["scp"]))
            patch(mock.patch.object(self.module, "_run_checked", side_effect=checked))
            patch(mock.patch.object(self.module, "run_argv", side_effect=run_job))
            patch(mock.patch.object(self.module, "_salvage_receipt", return_value=salvage or {"status": "verified", "receipt": {"status": "PASS", "run_id": "test-run", "secret_free": True, "passed": 1, "failed": 0, "case_count": 1}}))
            patch(mock.patch.object(self.module, "shadeform_teardown", side_effect=teardown))
            if real_recovery_ledger:
                patch(mock.patch.object(self.module.shadeform, "verify_owned_instance_before_delete", return_value={"status": "active"}))
                patch(mock.patch.object(self.module.shadeform, "verify_owned_ssh_key_before_delete", return_value={"status": "active"}))
                recovered = patch(mock.patch.object(self.module, "shadeform_teardown_recovered", side_effect=recovered_with_real_ledger))
            else:
                recovered = patch(mock.patch.object(self.module, "shadeform_teardown_recovered", side_effect=recovered_teardown))
            patch(mock.patch.object(self.module, "_persist_lifecycle", side_effect=persist))
            try:
                result = self.module.execute(self.args)
                raised = None
            except BaseException as exc:
                result = None
                raised = exc
        return types.SimpleNamespace(result=result, raised=raised, events=events, persisted=persisted, watchdog=watchdog, reserve_calls=reserve_calls, add_key=add_key, delete_key=delete_key, direct_delete=direct_delete, recovered_teardown=recovered, recovery_records=recovery_records, teardown_deadlines=teardown_deadlines, watchdog_commands=watchdog_commands)

    def assert_no_pending(self, outcome):
        latest = {event["instance_id"]: event["status"] for event in outcome.events}
        self.assertEqual(latest["attempt-" + "a" * 32], "settled")
        self.assertEqual(latest["instance-123456"], "settled")

    def assert_exact_cleanup(self, outcome):
        self.assertTrue(outcome.watchdog.terminated)
        self.assertTrue(outcome.persisted)
        self.assertEqual(outcome.persisted[-1]["cleanup"]["status"], "confirmed")
        self.assert_no_pending(outcome)
        self.assertTrue(all(call.get("public_key_fingerprint") == "A" * 43 for call in outcome.reserve_calls))
        self.assertEqual(outcome.add_key.call_args.args[2], "j1m-" + "a" * 32)
        self.assertTrue(outcome.teardown_deadlines and outcome.teardown_deadlines[0] is not None)
        max_index = outcome.watchdog_commands[0].index("--max-seconds") + 1
        self.assertGreater(float(outcome.watchdog_commands[0][max_index]), 239.0)
        self.assertLessEqual(float(outcome.watchdog_commands[0][max_index]), 240.0)

    def test_success_has_no_orphan_or_pending_cost(self):
        outcome = self.run_lifecycle()
        self.assertIsNone(outcome.raised)
        self.assertEqual(outcome.result["status"], "completed")
        self.assert_exact_cleanup(outcome)

    def test_activation_setup_upload_and_job_failures_cleanup_exactly(self):
        cases = (
            {"activation_error": TimeoutError("activation")},
            {"checked_error": ("setup:curl", self.module.RunnerError("setup"))},
            {"checked_error": ("upload:host/", self.module.RunnerError("upload"))},
            {"job_result": {"status": "completed", "exit_code": 1, "stdout_bytes": 0, "stderr_bytes": 12}},
        )
        for options in cases:
            with self.subTest(options=tuple(options)):
                outcome = self.run_lifecycle(**options)
                self.assertIsNotNone(outcome.raised)
                self.assert_exact_cleanup(outcome)

    def test_transport_timeout_keyboard_interrupt_and_system_exit_cleanup(self):
        failures = (
            ({"job_result": {"status": "timeout", "exit_code": None, "stdout_bytes": 0, "stderr_bytes": 0}}, self.module.RunnerError),
            ({"job_error": KeyboardInterrupt()}, KeyboardInterrupt),
            ({"job_error": SystemExit(7)}, SystemExit),
        )
        for options, expected in failures:
            with self.subTest(expected=expected.__name__):
                outcome = self.run_lifecycle(**options)
                self.assertIsInstance(outcome.raised, expected)
                self.assert_exact_cleanup(outcome)

    def test_salvage_failure_fails_run_but_does_not_bypass_deletion(self):
        outcome = self.run_lifecycle(salvage={"status": "salvage_failed", "code": "transport_failed"})
        self.assertIsInstance(outcome.raised, self.module.RunnerError)
        self.assert_exact_cleanup(outcome)
        self.assertEqual(outcome.persisted[-1]["salvage"]["status"], "salvage_failed")

    def test_deletion_failure_retains_watchdog_and_pending_instance(self):
        outcome = self.run_lifecycle(deletion_error=RuntimeError("unconfirmed"))
        self.assertIsInstance(outcome.raised, self.module.RunnerError)
        self.assertFalse(outcome.watchdog.terminated)
        self.assertEqual(outcome.persisted[-1]["cleanup"], {"status": "failed", "retry_required": True, "code": "runner_error"})
        latest = {event["instance_id"]: event["status"] for event in outcome.events}
        self.assertEqual(latest["instance-123456"], "pending")

    def test_cost_bookkeeping_failure_is_not_a_clean_teardown(self):
        outcome = self.run_lifecycle(deletion_receipt={"deletion": {"success": True}, "cost_bookkeeping_error_type": "OSError"})
        self.assertIsInstance(outcome.raised, self.module.RunnerError)
        self.assertFalse(outcome.watchdog.terminated)
        self.assertEqual(outcome.persisted[-1]["cleanup"]["status"], "failed")

    def test_ambiguous_instance_create_refuses_broad_cleanup_and_stays_pending(self):
        outcome = self.run_lifecycle(create_error=self.module.shadeform.AmbiguousProviderOutcome("unknown"))
        self.assertIsInstance(outcome.raised, self.module.shadeform.AmbiguousProviderOutcome)
        self.assertFalse(outcome.teardown_deadlines)
        outcome.delete_key.assert_not_called()
        self.assertEqual(outcome.persisted[-1]["cleanup"]["status"], "ambiguous_create")
        latest = {event["instance_id"]: event["status"] for event in outcome.events}
        self.assertEqual(latest["attempt-" + "a" * 32], "pending")

    def test_failed_instance_pending_append_still_deletes_and_settles_exact_cost(self):
        outcome = self.run_lifecycle(instance_pending_error=True)
        self.assertIsInstance(outcome.raised, OSError)
        outcome.direct_delete.assert_not_called()
        outcome.recovered_teardown.assert_called_once()
        latest = {event["instance_id"]: event["status"] for event in outcome.events}
        self.assertEqual(latest["instance-123456"], "settled")
        self.assertEqual(latest["attempt-" + "a" * 32], "settled")

    def test_direct_cost_failure_retains_key(self):
        outcome = self.run_lifecycle(instance_pending_error=True, direct_cost_error=True)
        self.assertIsInstance(outcome.raised, self.module.RunnerError)
        outcome.delete_key.assert_not_called()
        self.assertFalse(outcome.watchdog.terminated)

    def test_direct_attempt_failure_retains_key(self):
        outcome = self.run_lifecycle(instance_pending_error=True, direct_attempt_error=True)
        self.assertIsInstance(outcome.raised, self.module.RunnerError)
        outcome.delete_key.assert_not_called()
        self.assertFalse(outcome.watchdog.terminated)

    def test_owned_record_persistence_failure_uses_durable_recovery_teardown(self):
        self.assertTrue(
            self.module.shadeform.RUNTIME_ROOT.is_relative_to(
                self.lifecycle_paths.runtime_root.parent
            )
        )
        outcome = self.run_lifecycle(record_persistence_error=True)
        self.assertIsInstance(outcome.raised, OSError)
        outcome.direct_delete.assert_not_called()
        outcome.recovered_teardown.assert_called_once()
        self.assertTrue(outcome.watchdog.terminated)

    def test_owned_record_failure_reuses_real_pending_reservation(self):
        outcome = self.run_lifecycle(
            record_persistence_error=True,
            real_recovery_ledger=True,
        )
        self.assertIsInstance(outcome.raised, OSError)
        self.assertEqual(outcome.direct_delete.call_count, 1)
        # The recovery helper is deliberately invoked twice to model restart.
        # Both entries pass through the high-level exact state machine; the
        # raw key DELETE no-replay invariant is covered by the provider-call
        # counter in test_key_delete_accepted_but_present_retries_by_get_without_second_delete.
        self.assertEqual(outcome.delete_key.call_count, 2)
        self.assertEqual(len(outcome.recovery_records), 1)
        data = self.module.shadeform.bounded_stable_bytes(
            self.module.shadeform.COST_LEDGER,
            self.module.shadeform.MAX_COST_LEDGER_BYTES,
            label="test cost ledger",
        )
        events = self.module.shadeform._cost_ledger_events(data)
        instance = [
            event for event in events
            if event.get("instance_id") == "instance-123456"
        ]
        self.assertEqual(
            [event["status"] for event in instance],
            ["pending", "settled"],
        )
        attempt = [
            event for event in events
            if event.get("instance_id") == "attempt-" + "a" * 32
        ]
        self.assertEqual(
            instance[0]["estimated_cost_usd"],
            attempt[0]["estimated_cost_usd"],
        )
        self.assertLessEqual(instance[0]["estimated_cost_usd"], 0.3125)
        self.assertEqual(attempt[-1]["status"], "settled")
        self.assertEqual(self.module.shadeform.ledger_spend()[1], [])
        self.assertTrue(outcome.watchdog.terminated)

    def test_execution_flag_still_blocks_before_credentials(self):
        with mock.patch.object(self.module.shadeform, "load_env") as load_env:
            with self.assertRaisesRegex(self.module.RunnerError, "gated"):
                self.module.execute(types.SimpleNamespace())
        load_env.assert_not_called()

    def test_watchdog_command_binds_absolute_deadline_and_nonce(self):
        outcome = self.run_lifecycle()
        command = outcome.watchdog_commands[0]
        self.assertIn("--deadline-epoch", command)
        self.assertIn("--provider-delete-deadline-epoch", command)
        self.assertIn("--allow-unrecorded-exact", command)
        self.assertIn("--ownership-nonce", command)
        self.assertIn("--key-only-recovery", command)
        self.assertIn("--ssh-key-name", command)
        self.assertIn("--ssh-key-fingerprint", command)
        for option in ("--cloud", "--region", "--instance-type", "--hourly-usd", "--gpu", "--gpu-count", "--vram-gb", "--os-image"):
            self.assertIn(option, command)


class ExternalLifecycleBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.lifecycle_paths = install_lifecycle_execute_isolation(
            self, self.module.shadeform, prefix="remote-boundary-runtime-"
        )
        self.candidate = self.module.shadeform.Candidate("A100 80GB", "cloud", "region", "type", 1.0, 80, "ubuntu", False)

    def inject_legacy_after_outer_preflight(self, phase_id):
        self.module.shadeform.preflight_legacy_deletion_evidence(phase_id)
        legacy = self.module.shadeform.legacy_deletion_evidence_paths(phase_id)[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"{}")

    @staticmethod
    def watchdog_unrecorded_argv(phase_id, nonce):
        now = time.time()
        return [
            "--phase-id", phase_id,
            "--instance-name", "ep-run-" + nonce,
            "--launcher-pid", "1234",
            "--max-seconds", "1",
            "--deadline-epoch", str(now + 1000),
            "--provider-delete-deadline-epoch", str(now + 900),
            "--allow-unrecorded-exact",
            "--ownership-nonce", nonce,
            "--ssh-key-id", "key-123456",
            "--ssh-key-name", "j1m-" + nonce,
            "--ssh-key-fingerprint", "A" * 43,
            "--cloud", "cloud", "--region", "region",
            "--instance-type", "type", "--hourly-usd", "1",
            "--gpu", "A100", "--gpu-count", "1",
            "--vram-gb", "80", "--os-image", "ubuntu",
        ]

    def test_watchdog_broken_owner_symlink_refuses_before_reconciliation(self):
        watchdog = load_watchdog_module()
        phase = "watchdog-broken-owner"
        nonce = "b" * 32
        sf = self.module.shadeform
        sf.runtime_ledger_path(phase).symlink_to(
            self.lifecycle_paths.runtime_root / "missing-owner-target"
        )
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(watchdog, "_pending_intent") as pending, \
                mock.patch.object(sf, "load_env") as load_env, \
                mock.patch.object(sf, "reconcile_instance_by_nonce") as reconcile, \
                mock.patch.object(sf, "request") as provider:
            result = watchdog.main(self.watchdog_unrecorded_argv(phase, nonce))
        self.assertEqual(result, 1)
        pending.assert_not_called()
        load_env.assert_not_called()
        reconcile.assert_not_called()
        provider.assert_not_called()

    def test_watchdog_owner_swap_race_refuses_before_reconciliation(self):
        watchdog = load_watchdog_module()
        phase = "watchdog-owner-swap"
        nonce = "c" * 32
        sf = self.module.shadeform
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(
                    sf, "read_phase_ownership",
                    side_effect=[(None, False), sf.ShadeformError("owner path changed")],
                ), \
                mock.patch.object(watchdog, "_pending_intent", return_value={"status": "pending"}), \
                mock.patch.object(sf, "load_env") as load_env, \
                mock.patch.object(sf, "reconcile_instance_by_nonce") as reconcile, \
                mock.patch.object(sf, "request") as provider:
            result = watchdog.main(self.watchdog_unrecorded_argv(phase, nonce))
        self.assertEqual(result, 1)
        load_env.assert_not_called()
        reconcile.assert_not_called()
        provider.assert_not_called()
        source = (ROOT / "scripts" / "shadeform_watchdog.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("record_path.exists()", source)

    def test_ssh_key_boundary_rechecks_legacy_evidence_before_post(self):
        phase_id = "key-boundary-race"
        self.inject_legacy_after_outer_preflight(phase_id)
        with mock.patch.object(self.module.shadeform, "request") as transport:
            with self.assertRaisesRegex(
                self.module.shadeform.ShadeformError,
                "legacy deletion evidence",
            ):
                self.module.shadeform.add_ssh_key(
                    "token", phase_id, "j1m-" + "a" * 32, "ssh-ed25519 AAAA test",
                )
        transport.assert_not_called()

    def test_instance_boundary_rechecks_legacy_evidence_before_post(self):
        phase_id = "instance-boundary-race"
        self.inject_legacy_after_outer_preflight(phase_id)
        auto_delete = {
            "date_threshold": (
                self.module.shadeform.utc_now()
                + self.module.shadeform.timedelta(hours=1)
            ).isoformat(),
            "spend_threshold": "1.00",
        }
        with mock.patch.object(self.module.shadeform, "request") as transport:
            with self.assertRaisesRegex(
                self.module.shadeform.ShadeformError,
                "legacy deletion evidence",
            ):
                self.module.shadeform.create_instance(
                    "token",
                    {},
                    phase_id=phase_id,
                    run_id="test-run",
                    candidate=self.candidate,
                    ssh_key_id="key-123456",
                    nonce="a" * 32,
                    max_runtime_hours=0.25,
                    auto_delete_contract=auto_delete,
                )
        transport.assert_not_called()

    def test_teardown_reserve_includes_fresh_instance_and_key_proofs(self):
        self.assertGreaterEqual(self.module.DELETION_RESERVE_SECONDS, 570.0)
        self.assertEqual(
            self.module.DELETION_RESERVE_SECONDS,
            self.module.INSTANCE_VERIFY_TIMEOUT_SECONDS
            + self.module.DELETE_REQUEST_TIMEOUT_SECONDS
            + self.module.DELETE_POLL_TIMEOUT_SECONDS
            + self.module.SSH_KEY_VERIFY_TIMEOUT_SECONDS
            + self.module.SSH_KEY_DELETE_TIMEOUT_SECONDS
            + self.module.TEARDOWN_MARGIN_SECONDS,
        )

    def test_potentially_committed_create_statuses_are_ambiguous(self):
        for status in (408, 409, 425, 429, 500, 503):
            with self.subTest(status=status):
                error = self.module.shadeform.ShadeformHTTPError(status, "server")
                with mock.patch.object(self.module.shadeform, "request", side_effect=error):
                    with self.assertRaises(self.module.shadeform.AmbiguousProviderOutcome):
                        self.module.shadeform.create_instance(
                            "token", {}, phase_id="qa-remote-tools", run_id="run", candidate=self.candidate,
                            ssh_key_id="key-123456", nonce="a" * 32, max_runtime_hours=0.25,
                        )

    def test_provider_response_body_is_bounded_before_json_parse(self):
        module = self.module.shadeform
        class Stream:
            def read(self, _size):
                return b"{" + b"x" * module.MAX_PROVIDER_RESPONSE_BYTES

        stream = Stream()
        with self.assertRaisesRegex(module.MalformedProviderResponse, "bounded response"):
            module._read_provider_response(stream)

    def test_delete_rejects_provider_failure_dictionary(self):
        for response in ({"success": False, "error": "provider rejected"}, {"accepted": False}):
            with self.subTest(response=response), mock.patch.object(
                self.module.shadeform, "request", return_value=response,
            ):
                with self.assertRaises(self.module.shadeform.ShadeformError):
                    self.module.shadeform._delete_ssh_key_once("token", "qa-remote-tools", "key-123456")

    def test_watchdog_work_deadline_leaves_hard_teardown_reserve(self):
        watchdog = load_watchdog_module()
        work, hard = watchdog._deadline_windows(
            now_monotonic=100.0, now_epoch=1_000.0,
            max_seconds=600.0, deadline_epoch=1_900.0,
        )
        self.assertEqual(work, 700.0)
        self.assertEqual(hard, 1_000.0)
        with self.assertRaises(ValueError):
            watchdog._deadline_windows(
                now_monotonic=100.0, now_epoch=1_000.0,
                max_seconds=600.0, deadline_epoch=1_500.0,
            )

    def test_watchdog_intent_uses_latest_event_and_rejects_direct_unrecorded_id(self):
        watchdog = load_watchdog_module()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory).resolve() / "cost.jsonl"
            nonce = "a" * 32
            pending = stored_cost_event(self.module.shadeform, {
                "phase_id": "qa-remote-tools", "ownership_nonce": nonce,
                "instance_id": "attempt-" + nonce, "status": "pending",
                "estimated_cost_usd": 1.0,
            })
            settled = stored_cost_event(self.module.shadeform, {
                "phase_id": "qa-remote-tools", "ownership_nonce": nonce,
                "instance_id": "attempt-" + nonce, "status": "settled",
                "actual_cost_usd": 0.0,
            })
            ledger.write_text(
                json.dumps(pending) + "\n" + json.dumps(settled) + "\n",
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            with mock.patch.object(self.module.shadeform, "COST_LEDGER", ledger):
                self.assertFalse(watchdog._has_pending_intent(self.module.shadeform, "qa-remote-tools", nonce))
            with self.assertRaises(SystemExit):
                watchdog.main([
                    "--phase-id", "qa-remote-tools", "--instance-id", "instance-123456",
                    "--launcher-pid", "1", "--max-seconds", "1", "--allow-unrecorded-exact",
                    "--ownership-nonce", nonce, "--instance-name", "ep-run-" + nonce,
                    "--ssh-key-id", "key-123456", "--cloud", "cloud", "--region", "region",
                    "--instance-type", "type", "--hourly-usd", "1", "--gpu", "A100",
                    "--gpu-count", "1", "--vram-gb", "80", "--os-image", "ubuntu",
                ])

    def test_watchdog_pending_intent_includes_unrecorded_instance_event(self):
        watchdog = load_watchdog_module()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory).resolve() / "cost.jsonl"
            nonce = "a" * 32
            pending = stored_cost_event(self.module.shadeform, {
                "phase_id": "qa-remote-tools", "ownership_nonce": nonce,
                "instance_id": "instance-123456", "status": "pending",
                "estimated_cost_usd": 1.0,
                "create_started_at_utc": "2026-09-05T00:00:00+00:00",
            })
            ledger.write_text(
                json.dumps(pending) + "\n",
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            with mock.patch.object(self.module.shadeform, "COST_LEDGER", ledger):
                intent = watchdog._pending_intent(self.module.shadeform, "qa-remote-tools", nonce)
            self.assertEqual(intent["instance_id"], "instance-123456")

    def test_watchdog_uses_strict_authoritative_ledger_and_stops_before_provider(self):
        watchdog = load_watchdog_module()
        phase = "qa-remote-tools"
        nonce = "d" * 32
        base = stored_cost_event(self.module.shadeform, {
            "phase_id": phase,
            "ownership_nonce": nonce,
            "instance_id": "instance-123456",
            "status": "pending",
            "estimated_cost_usd": 1.0,
        })
        conflicting = stored_cost_event(self.module.shadeform, {
            "phase_id": phase,
            "ownership_nonce": "e" * 32,
            "instance_id": "instance-123456",
            "status": "pending",
            "estimated_cost_usd": 1.0,
        })
        canonical = json.dumps(base, sort_keys=True, separators=(",", ":"))
        cases = {
            "torn": canonical.encode("utf-8"),
            "duplicate": (
                canonical.replace('"schema":', '"schema":"duplicate","schema":', 1)
                + "\n"
            ).encode("utf-8"),
            "owner-conflict": (
                canonical + "\n"
                + json.dumps(conflicting, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
            "malformed": b'{"schema":"wrong"}\n',
        }
        for label, payload in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                ledger = root / "cost.jsonl"
                ledger.write_bytes(payload)
                ledger.chmod(0o600)
                with mock.patch.object(self.module.shadeform, "COST_LEDGER", ledger):
                    self.assertIsNone(
                        watchdog._pending_intent(
                            self.module.shadeform, phase, nonce,
                        )
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger = root / "cost.jsonl"
            ledger.write_bytes(cases["torn"])
            ledger.chmod(0o600)
            with mock.patch.object(watchdog, "ROOT", root), \
                    mock.patch.object(watchdog, "identity_alive", return_value=False), \
                    mock.patch.object(self.module.shadeform, "read_phase_ownership", return_value=(None, False)), \
                    mock.patch.object(self.module.shadeform, "COST_LEDGER", ledger), \
                    mock.patch.object(self.module.shadeform, "load_env") as load_env, \
                    mock.patch.object(self.module.shadeform, "reconcile_ssh_key") as reconcile, \
                    mock.patch.object(self.module.shadeform, "request") as provider:
                result = watchdog.main([
                    "--phase-id", phase,
                    "--instance-name", "ep-run-" + nonce,
                    "--launcher-pid", "1234",
                    "--max-seconds", "1",
                    "--deadline-epoch", str(__import__("time").time() + 1000),
                    "--provider-delete-deadline-epoch", str(__import__("time").time() + 900),
                    "--allow-unrecorded-exact",
                    "--ownership-nonce", nonce,
                    "--ssh-key-id", "key-123456",
                    "--cloud", "cloud", "--region", "region",
                    "--instance-type", "type", "--hourly-usd", "1",
                    "--gpu", "A100", "--gpu-count", "1", "--vram-gb", "80",
                    "--os-image", "ubuntu",
                ])
            self.assertEqual(result, 1)
            load_env.assert_not_called()
            reconcile.assert_not_called()
            provider.assert_not_called()

    def test_instance_reconciliation_requires_one_exact_nonce_match(self):
        expected_name = self.module.shadeform.owned_instance_name("run", "a" * 32)
        response = {"instances": [{"id": "instance-123456", "name": expected_name, "tags": ["local-bmo-j1m", "ep-phase-qa-remote-tools", "ep-run-" + "a" * 32]}]}
        info = {"id": "instance-123456", "name": expected_name, "tags": response["instances"][0]["tags"], "ssh_key_id": "key-123456", "cloud": "cloud", "region": "region", "shade_instance_type": "type", "hourly_price": 100, "configuration": {"gpu_type": "A100 80GB", "num_gpus": 1, "vram_per_gpu_in_gb": 80, "os": "ubuntu"}}
        profile = dict(ssh_key_id="key-123456", expected_cloud="cloud", expected_region="region", expected_instance_type="type", expected_hourly_usd=1.0, expected_gpu="A100 80GB", expected_gpu_count=1, expected_vram_gb=80, expected_os_image="ubuntu")
        with mock.patch.object(self.module.shadeform, "request", side_effect=[response, info]) as request:
            result = self.module.shadeform.reconcile_instance_by_nonce(
                "token", "qa-remote-tools", expected_name=expected_name, nonce="a" * 32, **profile,
            )
        self.assertEqual(result, "instance-123456")
        self.assertEqual(request.call_args_list[0].args[2], "/instances")
        duplicate = {"instances": response["instances"] * 2}
        with mock.patch.object(self.module.shadeform, "request", return_value=duplicate):
            with self.assertRaises(self.module.shadeform.AmbiguousProviderOutcome):
                self.module.shadeform.reconcile_instance_by_nonce(
                    "token", "qa-remote-tools", expected_name=expected_name, nonce="a" * 32, **profile,
                )

    def test_frozen_closure_is_descriptor_bound_and_hashable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            target = Path(directory) / "stage"
            for relative in self.module.UPLOAD_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())
            entries = self.module.freeze_closure(root, target)
            self.assertEqual(len(entries), len(self.module.UPLOAD_FILES))
            self.assertEqual(entries[0]["sha256"], __import__("hashlib").sha256(self.module.UPLOAD_FILES[0].encode()).hexdigest())

    def test_receipt_salvage_is_non_overwriting_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "receipt.json"
            payload = json.dumps(valid_receipt()).encode()
            def transport(argv, **_kwargs):
                Path(argv[-1]).write_bytes(payload)
                return {"status": "completed", "exit_code": 0, "stdout_bytes": 0, "stderr_bytes": 0}
            with mock.patch.object(self.module, "run_argv", side_effect=transport):
                result = self.module._salvage_receipt(
                    scp=["scp"], info={"ssh_user": "runner", "ip": "203.0.113.10"},
                    remote_root="/scratch/test", destination=destination, run_id="test-run",
                    seeds=(17,), fuzz_cases=0, soak_iterations=0, deadline=time.monotonic() + 1000,
                    allowed_root=root,
                )
            self.assertEqual(result["status"], "verified")
            self.assertEqual(destination.read_bytes(), payload)
            with mock.patch.object(self.module, "run_argv", side_effect=transport):
                refused = self.module._salvage_receipt(
                    scp=["scp"], info={"ssh_user": "runner", "ip": "203.0.113.10"},
                    remote_root="/scratch/test", destination=destination, run_id="test-run",
                    seeds=(17,), fuzz_cases=0, soak_iterations=0, deadline=time.monotonic() + 1000,
                    allowed_root=root,
                )
            self.assertEqual(refused["status"], "salvage_failed")


if __name__ == "__main__":
    unittest.main()
