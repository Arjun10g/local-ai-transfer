import importlib.util
import json
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HardwareProbeTests(unittest.TestCase):
    def test_probe_refuses_before_unbounded_collection(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text()
        self.assertIn("throw 'NOT_READY: bounded native Windows hardware collector is unavailable", source)
        self.assertIn("no query or probe ran", source)
        for forbidden in ("Get-CimInstance", "ConvertTo-Json", "System.Diagnostics.Process", "WriteAllText", "New-Item", "Invoke-WebRequest"):
            self.assertNotIn(forbidden, source)


class ShadeformPreflightTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module(ROOT / "scripts/shadeform/readonly_preflight.py", "readonly_preflight")

    def test_lowercase_legacy_credential_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("git_access=must-not-be-used\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.module.parse_env(path)

    def test_plan_selects_only_capped_profiles_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("\n".join([
                "SHADEFORM_API_KEY=secret-not-output",
                "SHADEFORM_SSH=private-not-output",
                "SHADEFORM_SSH_KEY_ID=key-not-output",
                "SHADEFORM_MAX_HOURLY_COST_USD=2.00",
                "SHADEFORM_MAX_TOTAL_COST_USD=10.00",
                "SHADEFORM_GPU_TYPES=Arc,A770",
                "SHADEFORM_GPU_COUNT=1",
                "SHADEFORM_MIN_VRAM_GB=8",
                "SHADEFORM_CLOUD=allowed-cloud",
                "SHADEFORM_REGION=region-a",
                "SHADEFORM_EXCLUDED_CLOUDS=blocked-cloud",
                "SHADEFORM_PREFER_INTERRUPTIBLE=true",
            ]) + "\n", encoding="utf-8")
            catalogue = root / "catalogue.json"
            catalogue.write_text(json.dumps({"profiles": [
                {"id": "cheap", "name": "Arc", "hourly_usd": 1.25, "cloud": "allowed-cloud", "region": "region-a", "gpu": "Arc-A770", "gpu_count": 1, "vram_gib": 16, "interruptible": True, "available": True},
                {"id": "over-cap", "name": "GPU", "hourly_usd": 3.5, "cloud": "allowed-cloud", "region": "region-a", "gpu": "Arc-A770", "gpu_count": 1, "vram_gib": 16},
                {"id": "forgone", "name": "Arc", "hourly_usd": 0.50, "cloud": "blocked-cloud", "region": "region-a", "gpu": "Arc-A770", "gpu_count": 1, "vram_gib": 16},
            ]}), encoding="utf-8")
            values = self.module.parse_env(env)
            selected, report = self.module.select_profiles(json.loads(catalogue.read_text()), values, 1.0)
            self.assertEqual([profile["id"] for profile in selected], ["cheap"])
            self.assertEqual(selected[0]["hourly_usd"], 1.25)
            self.assertEqual(selected[0]["worst_case_runtime_cost_usd"], 1.5625)
            self.assertEqual(report["forgone_cheaper_options"][0]["identity"]["id"], "forgone")
            self.assertIn("cloud_excluded", report["forgone_cheaper_options"][0]["reasons"])
            redacted = self.module.redact(values)
            self.assertEqual(redacted["SHADEFORM_API_KEY"], "<present>")
            self.assertNotIn("secret-not-output", json.dumps(redacted))

    def test_official_price_is_cents_and_mutation_readiness_is_separate(self):
        catalogue = {"instance_types": [{
            "shade_instance_type": "Arc-1",
            "cloud": "allowed-cloud",
            "hourly_price": 35,
            "configuration": {"num_gpus": 1, "vram_per_gpu_in_gb": 16, "gpu_type": "Arc-A770", "gpu_manufacturer": "intel"},
            "availability": [{"region": "region-a", "available": True}],
        }]}
        normalized = self.module.normalize_catalogue(catalogue)
        profile = normalized["profiles"][0]
        self.assertEqual(profile["hourly_price_cents"], 35)
        self.assertEqual(profile["hourly_usd"], 0.35)
        values = {"SHADEFORM_API_KEY": "secret", "SHADEFORM_MAX_HOURLY_COST_USD": "1", "SHADEFORM_MAX_TOTAL_COST_USD": "2"}
        self.assertTrue(self.module.mutation_readiness(values)["read_only_catalogue_ready"])
        self.assertFalse(self.module.mutation_readiness(values)["mutation_ready"])
        self.assertIn("SHADEFORM_SSH", self.module.mutation_readiness(values)["missing_or_invalid_inputs"])
        self.assertNotIn("SHADEFORM_SSH_KEY_ID", self.module.mutation_readiness(values)["missing_or_invalid_inputs"])

    def test_pending_ledger_refuses_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            ledger.write_text(json.dumps({"run_id": "old", "status": "pending", "estimated_cost_usd": 1.0}) + "\n", encoding="utf-8")
            values = {"SHADEFORM_API_KEY": "secret", "SHADEFORM_MAX_HOURLY_COST_USD": "1", "SHADEFORM_MAX_TOTAL_COST_USD": "2"}
            selected, report = self.module.select_profiles({"profiles": [{"id": "x", "hourly_usd": 0.35, "available": True}]}, values, 1, ledger)
            self.assertEqual(selected, [])
            self.assertTrue(report["pending_ledger_cost"])
            self.assertIn("pending_ledger_cost", report["excluded_profiles"][0]["reasons"])

    def test_budget_uses_provider_backstop_not_requested_runtime(self):
        values = {"SHADEFORM_API_KEY": "secret", "SHADEFORM_MAX_HOURLY_COST_USD": "10", "SHADEFORM_MAX_TOTAL_COST_USD": "3.5"}
        catalogue = {"profiles": [{"id": "x", "hourly_usd": 3.0, "available": True}]}
        selected, report = self.module.select_profiles(catalogue, values, 1.0)
        self.assertEqual(selected, [])
        self.assertEqual(report["provider_backstop_hours"], 1.25)
        self.assertIn("worst_case_provider_backstop_over_remaining_budget", report["excluded_profiles"][0]["reasons"])

    def test_ledger_latest_settled_event_replaces_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            ledger.write_text("\n".join([
                json.dumps({"instance_id": "attempt-1", "status": "pending", "estimated_cost_usd": 2}),
                json.dumps({"instance_id": "attempt-1", "status": "settled", "actual_cost_usd": 0.5}),
            ]) + "\n", encoding="utf-8")
            self.assertEqual(self.module.read_ledger(ledger), (0.5, [], False))

    def test_policy_rejects_nonfinite_caps_and_runtime(self):
        values = {
            "SHADEFORM_API_KEY": "secret",
            "SHADEFORM_MAX_HOURLY_COST_USD": "10",
            "SHADEFORM_MAX_TOTAL_COST_USD": "20",
        }
        for key in ("SHADEFORM_MAX_HOURLY_COST_USD", "SHADEFORM_MAX_TOTAL_COST_USD"):
            for raw in ("nan", "inf", "-inf", "0", "-1"):
                invalid = dict(values)
                invalid[key] = raw
                with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                    self.module.policy(invalid)
        catalogue = {"profiles": []}
        with self.assertRaises(ValueError):
            self.module.select_profiles(catalogue, values, float("nan"))
        with self.assertRaises(ValueError):
            self.module.select_profiles(catalogue, values, float("inf"))

    def test_settled_ledger_requires_finite_actual_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            for value in (None, True, -1, float("nan"), float("inf")):
                ledger.write_text(json.dumps({"instance_id": "attempt-1", "status": "settled", "actual_cost_usd": value}) + "\n", encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.module.read_ledger(ledger)

    def test_unknown_latest_status_cannot_replace_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            ledger.write_text("\n".join([
                json.dumps({"instance_id": "attempt-1", "status": "pending", "estimated_cost_usd": 1.0}),
                json.dumps({"instance_id": "attempt-1", "status": "bogus", "actual_cost_usd": 0.0}),
            ]) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.module.read_ledger(ledger)

    def test_pending_ledger_requires_estimate_and_no_actual(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            for event in (
                {"instance_id": "attempt-1", "status": "pending"},
                {"instance_id": "attempt-1", "status": "pending", "estimated_cost_usd": 1.0, "actual_cost_usd": 0.0},
            ):
                ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
                with self.subTest(event=event), self.assertRaises(ValueError):
                    self.module.read_ledger(ledger)

    def test_invalid_historical_line_is_not_hidden_by_later_valid_event(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            ledger.write_text("\n".join([
                json.dumps({"instance_id": "attempt-1", "status": "settled", "actual_cost_usd": float("nan")}),
                json.dumps({"instance_id": "attempt-1", "status": "settled", "actual_cost_usd": 0.5}),
            ]) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.module.read_ledger(ledger)


class RemoteExternalToolsGateTests(unittest.TestCase):
    def test_shared_legacy_deletion_preflight_is_read_only_and_clean_phase_passes(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(sf, "RUNTIME_ROOT", root):
                sf.preflight_legacy_deletion_evidence("clean-phase")
                (root / "legacy-phase.deletion-receipt.json").write_bytes(b"phase-only")
                with self.assertRaisesRegex(sf.ShadeformError, "legacy deletion evidence"):
                    sf.preflight_legacy_deletion_evidence("legacy-phase")
                with mock.patch.object(Path, "lstat", side_effect=OSError("metadata denied")):
                    with self.assertRaisesRegex(sf.ShadeformError, "legacy deletion evidence"):
                        sf.preflight_legacy_deletion_evidence("error-phase")

    def test_j1m_legacy_evidence_blocks_before_any_provider_mutation(self):
        module = load_module(ROOT / "scripts/j1m_orchestrator.py", "j1m_legacy_evidence_gate")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy-phase.deletion-intent.json").write_bytes(b"phase-only")
            with mock.patch.object(module.sf, "RUNTIME_ROOT", root), \
                 mock.patch.object(module.j1m_runner, "load_config") as load_config, \
                 mock.patch.object(module.sf, "load_env") as load_env, \
                 mock.patch.object(module.sf, "list_candidates") as list_candidates, \
                 mock.patch.object(module.sf, "create_ephemeral_ssh_key") as create_keypair, \
                 mock.patch.object(module.sf, "reserve_create_attempt") as reserve_attempt, \
                 mock.patch.object(module.sf, "add_ssh_key") as add_key, \
                 mock.patch.object(module.sf, "create_instance") as create_instance, \
                 mock.patch.object(module.sf, "delete_ssh_key") as delete_key, \
                 mock.patch.object(module, "teardown_exact") as teardown_exact:
                with self.assertRaisesRegex(module.sf.ShadeformError, "legacy deletion evidence"):
                    module.execute(Path(directory) / "missing.env", config_path=Path(directory) / "missing.json", phase_id="legacy-phase", run_id="run", artifact_destination=root / "out")
            load_config.assert_not_called(); load_env.assert_not_called(); list_candidates.assert_not_called(); create_keypair.assert_not_called(); reserve_attempt.assert_not_called(); add_key.assert_not_called(); create_instance.assert_not_called(); delete_key.assert_not_called(); teardown_exact.assert_not_called()

    def test_remote_external_tools_legacy_evidence_blocks_before_any_provider_mutation(self):
        module = load_module(ROOT / "scripts/shadeform/remote_external_tools.py", "remote_external_tools_legacy_evidence_gate")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy-phase.deletion-confirmation.json").write_bytes(b"phase-only")
            args = types.SimpleNamespace(phase_id="legacy-phase", env_file=root / "missing.env")
            with mock.patch.object(module, "REMOTE_EXECUTION_ENABLED", True), \
                 mock.patch.object(module.shadeform, "RUNTIME_ROOT", root), \
                 mock.patch.object(module.shadeform, "load_env") as load_env, \
                 mock.patch.object(module.shadeform, "list_candidates") as list_candidates, \
                 mock.patch.object(module.shadeform, "create_ephemeral_ssh_key") as create_keypair, \
                 mock.patch.object(module.shadeform, "reserve_create_attempt") as reserve_attempt, \
                 mock.patch.object(module.shadeform, "add_ssh_key") as add_key, \
                 mock.patch.object(module.shadeform, "create_instance") as create_instance, \
                 mock.patch.object(module.shadeform, "delete_ssh_key") as delete_key, \
                 mock.patch.object(module, "shadeform_teardown") as teardown:
                with self.assertRaisesRegex(module.shadeform.ShadeformError, "legacy deletion evidence"):
                    module.execute(args)
            load_env.assert_not_called(); list_candidates.assert_not_called(); create_keypair.assert_not_called(); reserve_attempt.assert_not_called(); add_key.assert_not_called(); create_instance.assert_not_called(); delete_key.assert_not_called(); teardown.assert_not_called()

    def test_inherited_remote_qa_cannot_reach_provider(self):
        module = load_module(ROOT / "scripts/shadeform/remote_external_tools.py", "remote_external_tools_gate")
        args = types.SimpleNamespace()
        with mock.patch.object(module.shadeform, "load_env") as load_env, mock.patch.object(module.shadeform, "list_candidates") as list_candidates, mock.patch.object(module.shadeform, "create_instance") as create_instance:
            with self.assertRaisesRegex(module.RunnerError, "gated"):
                module.execute(args)
        load_env.assert_not_called()
        list_candidates.assert_not_called()
        create_instance.assert_not_called()

    def test_cli_execute_gate_precedes_environment_loading(self):
        module = load_module(ROOT / "scripts/shadeform/remote_external_tools.py", "remote_external_tools_cli_gate")
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(module.shadeform, "load_env") as load_env, mock.patch.object(module, "build_plan") as build_plan:
                self.assertEqual(module.main(["--execute", "--env-file", str(Path(directory) / "missing.env")]), 2)
            load_env.assert_not_called()
            build_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
