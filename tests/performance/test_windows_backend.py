import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "windows_backend_plan.py"
spec = importlib.util.spec_from_file_location("windows_backend_plan", MODULE_PATH)
assert spec and spec.loader
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def receipt(*, sycl=False, adapter_count=1, integrated=True):
    adapters = []
    for index in range(adapter_count):
        adapters.append({
            "name": f"Intel Graphics {index}",
            "is_intel": True,
            "integrated": integrated,
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
        "vulkan": {"loader_present": True, "enumeration": {"exit_code": 0, "summary": "Intel Graphics Vulkan device"}},
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

    def write_vulkan_attestation(self, receipt_path):
        path = receipt_path.with_name("windows-gpu-attestation.json")
        path.write_text(json.dumps({
            "schema": "local_bmo.windows-gpu-attestation.v1",
            "receipt_sha256": planner._receipt_hash(receipt_path),
            "integrated": True,
            "pnp_device_id": "PCI\\VEN_8086&DEV_7D55&INDEX_0",
            "basis": "operator DXGI and Vulkan device correlation",
            "vulkan": {"pnp_device_id": "PCI\\VEN_8086&DEV_7D55&INDEX_0", "device_name": "Intel Graphics 0", "driver_version": "32.0.101.8247"},
        }), encoding="utf-8")
        return path

    def test_cpu_requires_explicit_selection_and_reports_conservative_memory(self):
        path = self.write_receipt(receipt(integrated=None))
        plan = planner.build_plan("cpu-safe", path)
        self.assertEqual(plan["backend"], "cpu-safe")
        self.assertEqual(plan["selection"], "operator-explicit-no-fallback")
        self.assertEqual(plan["runtime"]["compiled_backend"], "cpu")
        self.assertEqual(plan["status"], "planned")
        self.assertFalse(plan["execution_ready"])
        self.assertFalse(plan["runtime"]["gpu_offload"])
        self.assertEqual(plan["memory"]["default_context_tokens"], 8192)
        self.assertEqual(plan["memory"]["maximum_context_tokens"], 16384)
        self.assertTrue(plan["memory"]["maximum_context_requires_target_measurement"])
        self.assertTrue(plan["memory"]["shared_memory_is_not_dedicated_vram"])

    def test_sycl_is_not_inferred_from_intel_name_or_loader(self):
        path = self.write_receipt(receipt(integrated=None))
        with self.assertRaisesRegex(planner.BackendPlanError, "exactly one explicitly integrated"):
            planner.build_plan("intel-sycl-experimental", path)

    def test_vulkan_product_profile_requires_receipt_and_has_bounded_offload(self):
        path = self.write_receipt(receipt(integrated=None))
        attestation = self.write_vulkan_attestation(path)
        plan = planner.build_plan("intel-vulkan-conservative", path, attestation_path=attestation)
        self.assertTrue(plan["provenance"]["vulkan_source_closure"]["verified"])
        self.assertEqual(plan["runtime"]["compiled_backend"], "vulkan")
        self.assertEqual(plan["runtime"]["gpu_layers_default"], 20)

    def test_probe_shaped_unknown_integrated_field_needs_matching_attestation(self):
        path = self.write_receipt(receipt(integrated=None))
        with self.assertRaisesRegex(planner.BackendPlanError, "integrated-GPU attestation"):
            planner.build_plan("intel-vulkan-conservative", path)
        attestation = self.write_vulkan_attestation(path)
        plan = planner.build_plan("intel-vulkan-conservative", path, attestation_path=attestation)
        self.assertTrue(plan["provenance"]["vulkan_source_closure"]["verified"])

    def test_vulkan_rejects_missing_loader_or_enumeration(self):
        value = receipt()
        value["vulkan"] = {"loader_present": False, "enumeration": None}
        path = self.write_receipt(value)
        attestation = self.write_vulkan_attestation(path)
        with self.assertRaisesRegex(planner.BackendPlanError, "loader"):
            planner.build_plan("intel-vulkan-conservative", path, attestation_path=attestation)

    def test_vulkan_rejects_unbound_or_mismatched_attestation(self):
        path = self.write_receipt(receipt())
        attestation = self.write_vulkan_attestation(path)
        value = json.loads(attestation.read_text(encoding="utf-8"))
        value["vulkan"]["driver_version"] = "wrong-driver"
        attestation.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(planner.BackendPlanError, "exact adapter and driver"):
            planner.build_plan("intel-vulkan-conservative", path, attestation_path=attestation)

    def test_vulkan_planner_rechecks_tampered_closure(self):
        path = self.write_receipt(receipt())
        attestation = self.write_vulkan_attestation(path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "llama.cpp"
            import shutil
            shutil.copytree(planner.VULKAN_SOURCE_ROOT, root)
            (root / "ggml/src/ggml-vulkan/vulkan-shaders/add.comp").write_text("tampered", encoding="utf-8")
            with mock.patch.object(planner, "VULKAN_SOURCE_ROOT", root), mock.patch.object(planner, "VULKAN_SOURCE_MANIFEST", root / planner.VULKAN_SOURCE_MANIFEST.name):
                with self.assertRaisesRegex(planner.BackendPlanError, "failed verification"):
                    planner.build_plan("intel-vulkan-conservative", path, attestation_path=attestation)

    def test_sycl_requires_explicit_integrated_evidence_after_vulkan_closure(self):
        path = self.write_receipt(receipt(sycl=True))
        plan = planner.build_plan("intel-sycl-experimental", path)
        self.assertEqual(plan["runtime"]["compiled_backend"], "sycl")
        self.assertTrue(plan["runtime"]["diagnostic_only"])

    def test_sycl_rejects_ambiguous_integrated_adapters(self):
        path = self.write_receipt(receipt(sycl=True, adapter_count=2))
        with self.assertRaisesRegex(planner.BackendPlanError, "exactly one explicitly integrated"):
            planner.build_plan("intel-sycl-experimental", path)

    def test_sycl_rejects_adapter_without_explicit_integrated_evidence(self):
        value = receipt(sycl=True)
        value["gpu_adapters"][0]["integrated"] = None
        path = self.write_receipt(value)
        with self.assertRaisesRegex(planner.BackendPlanError, "exactly one explicitly integrated"):
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
        self.assertIn("ValidateSet('cpu-safe', 'intel-vulkan-conservative', 'intel-sycl-experimental')", build)
        self.assertIn("AllowExperimentalSycl", build)
        self.assertIn("GGML_SYCL_TARGET=INTEL", build)
        self.assertIn("LAE_ENABLE_LLAMA_VULKAN=ON", build)
        self.assertIn("VulkanAttestation", build)
        self.assertIn("LinkType", build)
        self.assertIn("LinkType", run)
        self.assertNotIn("--output", run)
        self.assertIn("--device SYCL0", run)
        self.assertIn("--backend intel-vulkan", run)
        self.assertIn("--n-predict 1", run)
        self.assertNotIn("--host 127.0.0.1", run)
        self.assertNotIn("llama-server", run.lower())
        self.assertNotIn("fallback", run.lower())

    def test_profile_and_docs_state_target_uncertainty(self):
        profiles = json.loads((ROOT / "release/windows/backend-profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profiles["schema_version"], "backend-profiles.v2")
        self.assertEqual(profiles["memory_policy"]["default_context_tokens"], 8192)
        self.assertEqual([item["promotion_rank"] for item in profiles["profiles"]], [0, 1, 2])
        sycl = next(item for item in profiles["profiles"] if item["id"] == "intel-sycl-experimental")
        self.assertTrue(sycl["requires_explicit_sycl_probe"])
        self.assertTrue(sycl["diagnostic_only"])
        self.assertFalse(sycl["operator_selectable"])
        self.assertIn("GGML_VULKAN", profiles["profiles"][1]["cmake_flags"][-1])
        self.assertEqual(profiles["profiles"][1]["status"], "implemented-not-promoted")
        self.assertEqual(profiles["profiles"][1]["implementation_status"], "implemented-source-locked-runtime")
        runtime = json.loads((ROOT / "release/windows/backend-runtime.example.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["vulkan"]["status"], "implemented-not-promoted")
        self.assertIn("attestation", runtime["vulkan"]["blocked_reason"])
        docs = (ROOT / "release/windows/README-OPERATOR.md").read_text(encoding="utf-8")
        self.assertIn("exact SKU", docs)
        self.assertIn("no fallback", docs.lower())
        self.assertIn("Vulkan (primary accelerated", docs)
        self.assertIn("GGML_VULKAN", docs.replace("ggml-vulkan", "GGML_VULKAN"))
        self.assertIn("not promoted", docs.lower())

    def test_native_vulkan_profile_is_authenticated_and_never_cpu_fallback(self):
        cmake = (ROOT / "native/CMakeLists.txt").read_text(encoding="utf-8")
        main = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        backend = (ROOT / "native/backend/llama_backend.cpp").read_text(encoding="utf-8")
        self.assertIn("option(LAE_ENABLE_LLAMA_VULKAN", cmake)
        self.assertIn("set(GGML_VULKAN ON", cmake)
        self.assertIn("vulkan_source_closure.py", cmake)
        self.assertIn("LAE_VULKAN_CLOSURE_RESULT", cmake)
        self.assertIn("LAE_ENABLE_LLAMA_VULKAN=1", cmake)
        self.assertIn('backend == "intel-vulkan"', main)
        self.assertIn("--gpu-layers", main)
        self.assertIn("ggml_backend_dev_count", backend)
        self.assertIn("model_params.devices", backend)
        self.assertIn("exact configured Vulkan integrated device is unavailable", backend)
        self.assertIn("ggml_backend_dev_description", backend)
        self.assertNotIn("intel-vulkan.*cpu", backend)

    def test_cli_accepts_vulkan_choice_and_reports_real_blocker(self):
        path = self.write_receipt(receipt(integrated=None))
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--backend", "intel-vulkan-conservative", "--receipt", str(path)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("integrated-GPU attestation", result.stdout)
        self.assertNotIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()
