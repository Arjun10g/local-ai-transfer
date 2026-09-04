import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "windows_backend_plan.py"
spec = importlib.util.spec_from_file_location("windows_backend_plan", MODULE_PATH)
assert spec and spec.loader
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def receipt(*, sycl=False, adapter_count=1):
    adapters = []
    for index in range(adapter_count):
        adapters.append({
            "name": f"Intel Graphics {index}",
            "is_intel": True,
            "integrated": True,
            "pnp_device_id": f"PCI\\VEN_8086&DEV_7D55&INDEX_{index}",
            "driver_version": "32.0.101.8247",
        })
    return {
        "schema_version": "1.0.0",
        "receipt_kind": "windows-hardware-receipt",
        "collection": {"read_only": True, "admin_required": False, "network_changed": False, "secrets_collected": False},
        "safety": {"no_environment_dump": True, "no_credentials": True, "no_network_changes": True, "no_driver_install": True, "no_model_access": True},
        "os": {"caption": "Microsoft Windows 11 Pro", "architecture": "64-bit"},
        "computer": {"total_memory_bytes": 32 * 1024**3, "available_memory_bytes": 24 * 1024**3},
        "gpu_adapters": adapters,
        "sycl_level_zero": ({
            "checked": True,
            "available": True,
            "device_count": 1,
            "device_name": "Intel Arc integrated test device",
            "device_id": "level-zero-device-0",
            "driver_version": "1.3.30000",
            "runtime_version": "2026.0",
            "probe": "sycl-ls --verbose receipt",
        } if sycl else {"checked": False, "available": False, "device_count": 0}),
    }


class WindowsBackendPlanTests(unittest.TestCase):
    def write_receipt(self, value):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "hardware-receipt.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return path

    def test_cpu_requires_explicit_selection_and_reports_conservative_memory(self):
        path = self.write_receipt(receipt())
        plan = planner.build_plan("cpu-safe", path)
        self.assertEqual(plan["backend"], "cpu-safe")
        self.assertEqual(plan["selection"], "operator-explicit-no-fallback")
        self.assertEqual(plan["runtime"]["compiled_backend"], "cpu")
        self.assertFalse(plan["execution_ready"])
        self.assertFalse(plan["runtime"]["gpu_offload"])
        self.assertEqual(plan["memory"]["default_context_tokens"], 8192)
        self.assertEqual(plan["memory"]["maximum_context_tokens"], 16384)
        self.assertTrue(plan["memory"]["maximum_context_requires_target_measurement"])
        self.assertTrue(plan["memory"]["shared_memory_is_not_dedicated_vram"])

    def test_sycl_is_not_inferred_from_intel_name_or_loader(self):
        path = self.write_receipt(receipt())
        with self.assertRaisesRegex(planner.BackendPlanError, "not proven"):
            planner.build_plan("intel-sycl-experimental", path)

    def test_sycl_ready_plan_contains_exact_provenance_and_flags(self):
        path = self.write_receipt(receipt(sycl=True))
        plan = planner.build_plan("intel-sycl-experimental", path)
        self.assertEqual(plan["runtime"]["compiled_backend"], "sycl")
        self.assertTrue(plan["runtime"]["gpu_offload"])
        self.assertTrue(plan["runtime"]["diagnostic_only"])
        self.assertFalse(plan["execution_ready"])
        self.assertEqual(plan["runtime"]["device_selector"], "SYCL0")
        self.assertEqual(plan["runtime"]["build_flags"], planner.SYCL_BUILD_FLAGS)
        self.assertEqual(plan["provenance"]["llama_cpp_revision"], planner.LLAMA_CPP_REVISION)
        self.assertIn("exact Core Ultra SKU", plan["provenance"]["target_sku_status"])

    def test_sycl_rejects_ambiguous_integrated_adapters(self):
        path = self.write_receipt(receipt(sycl=True, adapter_count=2))
        with self.assertRaisesRegex(planner.BackendPlanError, "exactly one"):
            planner.build_plan("intel-sycl-experimental", path)

    def test_sycl_rejects_adapter_without_explicit_integrated_evidence(self):
        value = receipt(sycl=True)
        value["gpu_adapters"][0]["integrated"] = None
        path = self.write_receipt(value)
        with self.assertRaisesRegex(planner.BackendPlanError, "exactly one"):
            planner.build_plan("intel-sycl-experimental", path)

    def test_neither_invalid_backend_nor_failed_sycl_falls_back(self):
        path = self.write_receipt(receipt())
        with self.assertRaises(planner.BackendPlanError):
            planner.build_plan("intel-sycl-experimental", path)
        with self.assertRaises(planner.BackendPlanError):
            planner.build_plan("auto", path)

    def test_model_identity_is_pinned(self):
        path = self.write_receipt(receipt())
        with self.assertRaisesRegex(planner.BackendPlanError, "model identity"):
            planner.build_plan("cpu-safe", path, model_size=1)

    def test_receipt_must_be_windows_and_secret_free(self):
        value = receipt()
        value["os"]["caption"] = "Darwin"
        path = self.write_receipt(value)
        with self.assertRaisesRegex(planner.BackendPlanError, "does not prove Windows"):
            planner.build_plan("cpu-safe", path)

    def test_scripts_require_explicit_backend_and_pin_sycl_flags(self):
        build = (ROOT / "release/windows/Build-WindowsBackend.ps1").read_text(encoding="utf-8")
        run = (ROOT / "release/windows/Run-WindowsBackend.ps1").read_text(encoding="utf-8")
        self.assertIn("ValidateSet('cpu-safe', 'intel-sycl-experimental')", build)
        self.assertIn("AllowExperimentalSycl", build)
        self.assertIn("GGML_SYCL_TARGET=INTEL", build)
        self.assertIn("LinkType", build)
        self.assertIn("LinkType", run)
        self.assertIn("--device SYCL0", run)
        self.assertNotIn("fallback", run.lower())

    def test_profile_and_docs_state_target_uncertainty(self):
        profiles = json.loads((ROOT / "release/windows/backend-profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profiles["schema_version"], "backend-profiles.v2")
        self.assertEqual(profiles["memory_policy"]["default_context_tokens"], 8192)
        self.assertEqual([item["promotion_rank"] for item in profiles["profiles"]], [0, 1, 2])
        sycl = next(item for item in profiles["profiles"] if item["id"] == "intel-sycl-experimental")
        self.assertTrue(sycl["requires_explicit_sycl_probe"])
        self.assertTrue(sycl["diagnostic_only"])
        docs = (ROOT / "release/windows/README-OPERATOR.md").read_text(encoding="utf-8")
        self.assertIn("exact SKU", docs)
        self.assertIn("no fallback", docs.lower())
        self.assertIn("Vulkan (primary accelerated", docs)


if __name__ == "__main__":
    unittest.main()
