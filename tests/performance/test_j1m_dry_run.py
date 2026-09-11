"""The offline dry run, and proof that it would have caught the bugs.

Three refusals in a row reached a state where a paid run would have created an
instance and then been unable to use it, and each was found by reading. This
harness is the replacement for reading: it drives the real `execute()` against a
fake provider and a subprocess shim and asserts nothing would be refused.

A gate is only worth its runtime if it fails when it should, so most of what is
here is negative: the old key location, a withheld receipt, a refused argv, a
receipt with no run binding, an unreachable destination. Each is injected and
each must turn the gate red.

No network, no provider, no process launch, no spend; the harness enforces all
four itself and raises if a run reaches one.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import j1m_dry_run as dry_run
from scripts import j1m_orchestrator as orchestrator
from scripts import j1m_runner, shadeform_lifecycle as sf

ROOT = Path(__file__).resolve().parents[2]


def private_key_root() -> Path:
    directory = tempfile.mkdtemp(prefix="dry-run-keys-", dir=ROOT)
    os.chmod(directory, 0o700)
    return Path(directory)


class DryRunGateTests(unittest.TestCase):
    """The gate as the live-run lane runs it."""

    @classmethod
    def setUpClass(cls):
        cls.key_root = private_key_root()
        # The live default destination is an operator precondition, not a
        # source fact: Git does not record directory modes, so the gate is
        # asked about a destination this suite owns rather than the checkout's.
        cls.receipt = dry_run.run_dry_run(("eval", "prove", "build"), key_root=cls.key_root)
        cls.comparator_run = next(
            run for name, run in cls.receipt["runs"].items()
            if name.startswith("__comparators__"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_root, ignore_errors=True)

    def failures(self):
        return {item["check"] for item in self.receipt["checks"] if item["status"] == "FAIL"}

    def test_every_check_except_the_operator_precondition_passes(self):
        self.assertLessEqual(
            self.failures(), {"operator_artifact_destination_is_salvage_ready"},
            json.dumps([item for item in self.receipt["checks"] if item["status"] == "FAIL"], indent=2))

    def test_no_argv_in_a_complete_run_is_refused_by_any_local_validator(self):
        refused = [item for item in self.receipt["commands"] if item["validator"] != "accepted"]
        self.assertEqual(refused, [])
        # A full three-mode run plus the comparator phase is well over a
        # hundred commands; a harness that recorded a handful would be passing
        # by not looking.
        self.assertGreater(self.receipt["argv_count"], 120)

    def test_the_receipt_lists_every_command_and_no_secret(self):
        self.assertEqual(self.receipt["schema"], "local_bmo.j1m.dry-run-receipt.v1")
        self.assertEqual(self.receipt["argv_count"], len(self.receipt["commands"]))
        serialized = json.dumps(self.receipt)
        for value in dry_run.SENTINELS.values():
            self.assertNotIn(value, serialized)
        self.assertNotIn(str(self.key_root), serialized)
        for entry in self.receipt["commands"]:
            if "-i" in entry["argv"]:
                self.assertEqual(entry["argv"][entry["argv"].index("-i") + 1],
                                 "<ephemeral-private-handle>")

    def test_the_run_reached_every_stage_that_costs_money_to_discover(self):
        commands = " ".join(part for entry in self.receipt["commands"] for part in entry["argv"])
        for marker in ("ssh-keyscan", "j1m-config.json", "run-identity.json",
                       "remote_model_eval.py", "cmake", "eval-receipt.json"):
            self.assertIn(marker, commands, marker)
        self.assertEqual(self.receipt["runs"]["eval"]["status"], "completed")
        self.assertEqual(self.receipt["runs"]["prove"]["status"], "completed")
        self.assertEqual(self.receipt["runs"]["build"]["status"], "completed")

    def test_build_mode_returns_receipts_and_never_the_weights(self):
        build = self.receipt["runs"]["build"]
        self.assertGreaterEqual(len(build["build_receipts"]["salvaged"]), 8)
        self.assertIn("Qwen3.5-9B-Q4_K_M.gguf", build["build_receipts"]["refused_non_receipt"])
        self.assertNotIn("Qwen3.5-9B-Q4_K_M.gguf", build["published"])
        self.assertIsNone(build["receipt_error"])

    def test_the_key_is_gone_on_every_path_including_the_failed_one(self):
        for name, run in self.receipt["runs"].items():
            self.assertIn(run["key_cleanup"].get("status"), {"removed", "absent"}, name)
            self.assertFalse(run["key_directory_present"], name)
        self.assertEqual(sorted(self.key_root.iterdir()), [])

    def test_the_comparator_phase_argv_is_accepted_and_carries_no_bearer_path(self):
        """The comparator stages are argv like any other and face the same policy."""

        arms = [entry for entry in self.receipt["commands"]
                if any("remote_comparator_eval.py" in part for part in entry["argv"])]
        self.assertTrue(arms)
        for entry in arms:
            self.assertEqual(entry["validator"], "accepted")
            self.assertNotIn("--token-file", entry["argv"])
            self.assertFalse([part for part in entry["argv"] if "comparator-token" in part])
        self.assertEqual(self.comparator_run["comparator_phase"]["status"], "approved")

    def test_the_deferred_cleanup_tail_runs_on_a_failure_before_the_phase(self):
        """`--retain-comparators` owes three stages; a failure must not eat them."""

        expected = ["--manifest", "--post-cleanup", "rm"]
        self.assertEqual(self.comparator_run["comparator_cleanup_stages"], expected)
        failure = self.receipt["runs"]["__comparator_cleanup_on_failure__"]
        self.assertEqual(failure["fail_eval_stage"], "remote_model_eval.py")
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["comparator_cleanup_stages"], expected)
        self.assertIsNone(failure["comparator_cleanup_error"])

    def test_a_refused_comparator_phase_records_a_typed_reason(self):
        receipt = self.comparator_run["comparison_receipt"]
        self.assertTrue(receipt["skipped"])
        for item in receipt["skipped"]:
            self.assertIn(item["reason"], sorted(dry_run.orchestrator._COMPARATOR_SKIP_REASONS))
        # The baseline arm is never a comparator, so without its own entry a
        # refused `q4-oracle` wrote a receipt indistinguishable from one that
        # asked for nothing.
        self.assertIn("q4_k_m", {item["comparator"] for item in receipt["skipped"]})

    def test_no_unexpected_child_process_was_requested(self):
        for name, run in self.receipt["runs"].items():
            self.assertEqual(run["unknown_subprocesses"], [], name)


class DryRunDetectionTests(unittest.TestCase):
    """Inject each defect the gate exists to catch; every one must turn it red."""

    def setUp(self):
        self.key_root = private_key_root()
        self.addCleanup(shutil.rmtree, self.key_root, ignore_errors=True)

    def failing(self, receipt):
        return {item["check"] for item in receipt["checks"] if item["status"] == "FAIL"}

    def test_the_old_key_location_makes_the_gate_fail_loudly(self):
        """The exact bug this slice fixes, replayed through the gate.

        With the key back in a system temporary directory under its old name,
        every ssh and scp argv is refused before it spawns -- which is what a
        paid run would have discovered after creating an instance.
        """

        legacy = Path(tempfile.mkdtemp(prefix="legacy-key-"))
        self.addCleanup(shutil.rmtree, legacy, ignore_errors=True)
        real_create = sf.create_keypair

        def legacy_keypair(directory):
            target = legacy / "id_ed25519"
            target.write_text("DRY-RUN-NOT-A-KEY\n", encoding="utf-8")
            target.chmod(0o600)
            os.chmod(legacy, 0o700)
            Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
            return target, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixture j1m"

        with mock.patch.object(sf, "create_keypair", side_effect=legacy_keypair), \
                mock.patch.object(sf, "assert_persisted_argv_handle"):
            receipt = dry_run.run_dry_run(("eval",), key_root=self.key_root)
        self.assertEqual(receipt["status"], "FAIL")
        self.assertIn("argv_validator_accepts_every_command", self.failing(receipt))
        refused = [item for item in receipt["commands"] if item["validator"] == "REFUSED"]
        self.assertTrue(refused)
        self.assertEqual(refused[0]["refusal"], "ValueError")

    def test_a_secret_reaching_a_command_line_makes_the_gate_fail(self):
        real_ssh_base = sf.ssh_base

        def leaking_ssh_base(info, identity, known_hosts):
            return real_ssh_base(info, identity, known_hosts) + [
                "--unused", dry_run.SENTINELS["SHADEFORM_API_KEY"]]

        with mock.patch.object(sf, "ssh_base", side_effect=leaking_ssh_base):
            receipt = dry_run.run_dry_run(("prove",), key_root=self.key_root)
        self.assertIn("no_secret_value_in_any_argv", self.failing(receipt))
        self.assertNotIn(dry_run.SENTINELS["SHADEFORM_API_KEY"], json.dumps(receipt))

    def test_a_receipt_without_its_run_binding_is_refused_and_fails_the_gate(self):
        """A stale receipt from an earlier run must not become this run's evidence."""

        real_receipts = dry_run.fake_receipts

        def unbound(*args, **kwargs):
            built = real_receipts(*args, **kwargs)
            payload = json.loads(built["proving-receipt.json"])
            payload.pop("run_id", None)
            built["proving-receipt.json"] = (json.dumps(payload, sort_keys=True) + "\n").encode()
            return built

        with mock.patch.object(dry_run, "fake_receipts", side_effect=unbound):
            receipt = dry_run.run_dry_run(("prove",), key_root=self.key_root)
        self.assertEqual(receipt["status"], "FAIL")
        prove = receipt["runs"]["prove"]
        self.assertEqual(prove["salvage_codes"]["proving-receipt.json"], "salvage_identity_missing")
        self.assertEqual(prove["status"], "failed")
        self.assertNotIn("proving-receipt.json", prove["published"])

    def test_a_receipt_claiming_another_run_is_refused(self):
        real_receipts = dry_run.fake_receipts

        def foreign(*args, **kwargs):
            built = real_receipts(*args, **kwargs)
            payload = json.loads(built["proving-receipt.json"])
            payload["instance_id"] = "instance-from-some-other-run"
            built["proving-receipt.json"] = (json.dumps(payload, sort_keys=True) + "\n").encode()
            return built

        with mock.patch.object(dry_run, "fake_receipts", side_effect=foreign):
            receipt = dry_run.run_dry_run(("prove",), key_root=self.key_root)
        self.assertEqual(
            receipt["runs"]["prove"]["salvage_codes"]["proving-receipt.json"],
            "salvage_identity_mismatch")

    def test_an_unreachable_teardown_still_reports_the_key_removed(self):
        receipt = dry_run.run_dry_run(("prove",), key_root=self.key_root)
        injected = receipt["runs"]["__injected_failure__"]
        self.assertTrue(injected["inject_failure"])
        self.assertIn("teardown was not confirmed", injected["error"])
        self.assertEqual(injected["key_cleanup"]["status"], "removed")

    def test_an_unexpected_child_process_is_refused_rather_than_ignored(self):
        shim = dry_run.SubprocessShim(dry_run.Recorder(), mode="eval", receipts={})
        with self.assertRaises(dry_run.DryRunError):
            shim(["/usr/bin/curl", "https://example.invalid"])
        with self.assertRaises(dry_run.DryRunError):
            shim("rm -rf /")

    def test_the_harness_writes_a_receipt_a_reviewer_can_read(self):
        destination = Path(tempfile.mkdtemp(prefix="dry-run-receipt-"))
        self.addCleanup(shutil.rmtree, destination, ignore_errors=True)
        path = destination / "dry-run-receipt.json"
        receipt = dry_run.run_dry_run(("prove",), key_root=self.key_root, receipt_path=path)
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], receipt["status"])
        self.assertEqual(stored["spend_usd"], 0.0)
        self.assertEqual(stored["network"], "none")
        self.assertIn("PASS", dry_run.render(receipt))
        for item in stored["checks"]:
            self.assertIn(item["status"], {"PASS", "FAIL"})
            self.assertTrue(item["detail"])


class DryRunRegistrationTests(unittest.TestCase):
    """The gate has to be in the inventory and in the live-run instructions."""

    def test_both_new_suites_are_registered_as_lifecycle_classes(self):
        spec = importlib.util.spec_from_file_location(
            "run_qa_inventory", ROOT / "scripts/test/run_qa.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("tests/performance/test_j1m_dry_run.py",
                     "tests/performance/test_j1m_key_handle.py"):
            self.assertEqual(module.TEST_INVENTORY.get(name), "lifecycle", name)

    def test_the_command_is_documented_where_the_operator_will_look(self):
        note = (ROOT / "scripts/shadeform/SALVAGE_TRANSPORT.md").read_text(encoding="utf-8")
        for marker in ("scripts/j1m_dry_run.py", "dry-run-receipt.json",
                       ".secrets/j1m", "ssh-key"):
            self.assertTrue(marker in note, f"{marker} is not documented in the design note")

    def test_the_gate_refuses_a_mode_the_orchestrator_cannot_execute(self):
        with self.assertRaises(SystemExit):
            dry_run.main(["--mode", "canary"])


if __name__ == "__main__":
    unittest.main()
