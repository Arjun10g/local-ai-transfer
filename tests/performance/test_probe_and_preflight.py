import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HardwareProbeTests(unittest.TestCase):
    def test_probe_is_read_only_and_bounded(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text()
        self.assertIn("read_only = $true", source)
        self.assertIn("admin_required = $false", source)
        self.assertIn("WaitForExit(10000)", source)
        self.assertNotIn("Invoke-WebRequest", source)
        self.assertNotIn("Install-", source)
        self.assertNotIn("Remove-Item", source)
        self.assertNotIn("Get-ChildItem Env:", source)
        self.assertNotIn("IdentifyingNumber", source)
        self.assertNotIn("product_identifier", source)
        self.assertNotIn('Write-Output "Receipt written: $([IO.Path]::GetFullPath', source)
        self.assertIn("pnp_device_id", source)
        self.assertIn("vulkan-1.dll", source)


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
            self.assertEqual(selected[0]["worst_case_runtime_cost_usd"], 1.25)
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
        self.assertIn("SHADEFORM_SSH_KEY_ID", self.module.mutation_readiness(values)["missing_or_invalid_inputs"])

    def test_pending_ledger_refuses_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            ledger.write_text(json.dumps({"run_id": "old", "status": "pending"}) + "\n", encoding="utf-8")
            values = {"SHADEFORM_API_KEY": "secret", "SHADEFORM_MAX_HOURLY_COST_USD": "1", "SHADEFORM_MAX_TOTAL_COST_USD": "2"}
            selected, report = self.module.select_profiles({"profiles": [{"id": "x", "hourly_usd": 0.35, "available": True}]}, values, 1, ledger)
            self.assertEqual(selected, [])
            self.assertTrue(report["pending_ledger_cost"])
            self.assertIn("pending_ledger_cost", report["excluded_profiles"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
