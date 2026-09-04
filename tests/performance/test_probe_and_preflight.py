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
            ]) + "\n", encoding="utf-8")
            catalogue = root / "catalogue.json"
            catalogue.write_text(json.dumps({"profiles": [
                {"id": "cheap", "name": "CPU", "hourly_usd": 1.25, "vcpus": 16, "memory_gib": 32},
                {"id": "over-cap", "name": "GPU", "hourly_usd": 3.5},
            ]}), encoding="utf-8")
            values = self.module.parse_env(env)
            selected = self.module.select_profiles(json.loads(catalogue.read_text()), values)
            self.assertEqual([profile["id"] for profile in selected], ["cheap"])
            redacted = self.module.redact(values)
            self.assertEqual(redacted["SHADEFORM_API_KEY"], "<present>")
            self.assertNotIn("secret-not-output", json.dumps(redacted))


if __name__ == "__main__":
    unittest.main()
