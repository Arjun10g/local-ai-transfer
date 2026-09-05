from contextlib import redirect_stdout
import io
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from qa.windows_acceptance.verify import (
    LLAMA_CPP_REVISION,
    MODEL_NAME,
    MODEL_SHA256,
    MODEL_SIZE,
    NODE_SHA256,
    NODE_VERSION,
    PORTABLE_RUNTIME_BLOCKER,
    REQUIRED_LIVE_CHECKS,
    ReceiptError,
    evaluate_receipt,
    load_receipt,
    main as verify_main,
)


ROOT = Path(__file__).resolve().parents[2]


def hardware_receipt():
    pnp = "PCI\\VEN_8086&DEV_7D55&SUBSYS_0C011028"
    return {
        "schema_version": "1.1.0",
        "receipt_kind": "windows-hardware-receipt",
        "generated_at_utc": "2026-09-04T12:00:00Z",
        "collection": {"read_only": True, "admin_required": False, "network_changed": False, "secrets_collected": False},
        "os": {"caption": "Microsoft Windows 11 Enterprise", "version": "10.0.26100", "build": "26100", "architecture": "64-bit"},
        "computer": {"manufacturer": "Dell Inc.", "model": "target-model", "system_type": "x64-based PC", "total_memory_bytes": 32 * 1024**3, "available_memory_bytes": 18 * 1024**3},
        "baseboards": [{"manufacturer": "Dell Inc.", "product": "039NNG", "version": "A00", "status": "OK"}],
        "bios": [{"manufacturer": "Dell Inc.", "smbios_bios_version": "fixture"}],
        "memory_modules": [
            {"device_locator": "DIMM A", "capacity_bytes": 16 * 1024**3, "speed_mts": 5600, "configured_speed_mts": 5600, "smbios_memory_type": 34},
            {"device_locator": "DIMM B", "capacity_bytes": 16 * 1024**3, "speed_mts": 5600, "configured_speed_mts": 5600, "smbios_memory_type": 34},
        ],
        "cpu": [{"name": "Intel(R) Core(TM) Ultra 7 165U", "device_id": "CPU0", "processor_id": "BFEBFBFF000A06A4", "address_width": 64, "physical_cores": 12, "logical_processors": 14}],
        "gpu_adapters": [{"name": "Intel(R) Graphics", "is_intel": True, "pnp_device_id": pnp, "driver_version": "32.0.101.8247"}],
        "display_drivers": [],
        "display_pnp_entities": [{"name": "Intel(R) Graphics", "pnp_device_id": pnp}],
        "vulkan": {
            "loader_present": True,
            "enumeration": {
                "exit_code": 0,
                "timed_out": False,
                "output_truncated": False,
                "explicitly_pinned": True,
                "command_sha256": "1" * 64,
                "summary_sha256": "2" * 64,
                "summary": "Intel Graphics Vulkan device",
                "primary_device": {"name": "Intel(R) Graphics", "vendor_id": "0x8086", "device_id": "0x7d55", "device_type": "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU", "api_version": "1.3.290", "driver_version": "32.0.101.8247"},
            },
        },
        "approved_runtime_dlls": {},
        "storage": [],
        "sycl_level_zero": {"checked": False},
        "safety": {"no_environment_dump": True, "no_credentials": True, "no_network_changes": True, "no_driver_install": True, "no_model_access": True},
    }


def complete_receipt(*, fixture=False, include_live=True):
    pnp = "PCI\\VEN_8086&DEV_7D55&SUBSYS_0C011028"
    live_checks = [
        {"id": check_id, "status": "PASS", "operator_confirmed": True, "synthetic_target": True, "observed_at_utc": "2026-09-04T12:00:00Z"}
        for check_id in REQUIRED_LIVE_CHECKS
    ] if include_live else []
    value = {
        "schema": "local_bmo.windows-clean-machine-acceptance.v1",
        "run_id": "12345678-1234-1234-1234-123456789abc",
        "captured_at_utc": "2026-09-04T12:00:00Z",
        "executed_on_target": True,
        "fixture": fixture,
        "hardware_receipt_sha256": "pending",
        "hardware": hardware_receipt(),
        "artifacts": {
            "release_manifest_sha256": "4" * 64,
            "release_verified": True,
            "host_config_sha256": "5" * 64 if include_live else None,
            "cpu_engine_sha256": "6" * 64,
            "vulkan_engine_sha256": "7" * 64,
            "model_name": MODEL_NAME,
            "model_size_bytes": MODEL_SIZE,
            "model_sha256": MODEL_SHA256,
            "llama_cpp_revision": LLAMA_CPP_REVISION,
            "node_version": NODE_VERSION,
            "node_sha256": NODE_SHA256,
        },
        "checks": {
            "cpu": {"status": "PASS", "backend": f"llama.cpp/{LLAMA_CPP_REVISION[:8]}/cpu", "model_loaded": True, "bounded_chat": True, "cancellation": True, "clean_exit": True, "no_orphan": True, "loopback_only": True, "offline": True},
            "vulkan_candidate": {"attempted": True, "status": "PASS", "promoted": False, "reason": None, "cpu_fallback_used": False, "backend": f"llama.cpp/{LLAMA_CPP_REVISION[:8]}/vulkan", "pnp_device_id": pnp, "driver_version": "32.0.101.8247", "device_name": "Intel(R) Graphics", "gpu_layers": 20, "no_silent_fallback": True, "model_loaded": True, "bounded_chat": True, "terminated_no_orphan": True},
            "portable": {"status": "PASS", "powershell_foreground": True, "current_user_pipes": True, "job_kill_on_close": True, "launch_gate_after_job_assignment": True, "browser_shell_execute": True, "fragment_cleared": True, "bootstrap_query_absent": True, "bootstrap_referer_rejected": True, "bootstrap_hostile_origin_rejected": True, "bootstrap_one_shot": True, "api_bearer_required": True, "console_secret_scan": True, "graceful_no_orphan": True, "abrupt_no_orphan": True},
        },
        "live_actions": {"opt_in": include_live, "explicit_consent": include_live, "synthetic_accounts_only": include_live, "disposable_workspace_only": include_live, "secrets_logged": False, "content_logged": False, "checks": live_checks},
        "safety": {"no_admin": True, "no_install": True, "no_download": True, "no_environment_dump": True, "no_secret_logging": True, "no_sensitive_test_data": True, "loopback_only": True},
    }
    source = json.dumps(value["hardware"], separators=(",", ":"), sort_keys=True).encode()
    value["hardware_receipt_sha256"] = hashlib.sha256(source).hexdigest()
    return value


def hardware_bytes(value):
    return json.dumps(value["hardware"], separators=(",", ":"), sort_keys=True).encode()


class WindowsAcceptanceGateTests(unittest.TestCase):
    def test_complete_historical_contract_cannot_authorize_disabled_runtime(self):
        value = complete_receipt()
        verdict = evaluate_receipt(value, hardware_source_bytes=hardware_bytes(value))
        self.assertEqual("NOT_READY", verdict["status"], verdict)
        self.assertFalse(verdict["core_ready"])
        self.assertFalse(verdict["full_access_ready"])
        self.assertIn(PORTABLE_RUNTIME_BLOCKER, verdict["reasons"])
        self.assertEqual("UNPROVEN_OR_REJECTED", verdict["cpu_disposition"])
        self.assertEqual("UNPROVEN_OR_REJECTED", verdict["vulkan_disposition"])

    def test_fixture_or_unbound_hardware_can_never_be_ready(self):
        fixture_value = complete_receipt(fixture=True)
        fixture = evaluate_receipt(fixture_value, hardware_source_bytes=hardware_bytes(fixture_value))
        self.assertEqual("NOT_READY", fixture["status"])
        self.assertIn("fixture/simulated receipts can never be READY", fixture["reasons"])
        unbound = evaluate_receipt(complete_receipt())
        self.assertFalse(unbound["core_ready"])
        self.assertTrue(any("not bound" in item for item in unbound["reasons"]))

    def test_historical_core_shape_cannot_bypass_current_producer_blocker(self):
        value = complete_receipt(include_live=False)
        verdict = evaluate_receipt(value, hardware_source_bytes=hardware_bytes(value))
        self.assertFalse(verdict["core_ready"], verdict)
        self.assertIn(PORTABLE_RUNTIME_BLOCKER, verdict["reasons"])
        self.assertFalse(verdict["full_access_ready"])
        self.assertEqual("NOT_READY", verdict["status"])
        self.assertEqual(len(REQUIRED_LIVE_CHECKS), sum(item.startswith("live check is not proven") for item in verdict["live_reasons"]))

    def test_exact_target_and_vulkan_evidence_fail_closed(self):
        value = complete_receipt()
        value["hardware"]["baseboards"][0]["product"] = "wrong"
        value["hardware"]["gpu_adapters"][0]["driver_version"] = "31.0.0.0"
        value["hardware"]["vulkan"]["enumeration"]["explicitly_pinned"] = False
        value["checks"]["vulkan_candidate"]["attempted"] = False
        value["checks"]["vulkan_candidate"]["status"] = "SKIP"
        # Rebind this deliberately modified source so the failures below are
        # the target invariants rather than a stale hash.
        source = hardware_bytes(value)
        value["hardware_receipt_sha256"] = hashlib.sha256(source).hexdigest()
        verdict = evaluate_receipt(value, hardware_source_bytes=source)
        self.assertFalse(verdict["core_ready"])
        self.assertTrue(any("039NNG" in item for item in verdict["reasons"]))
        self.assertTrue(any("32.0.101.8247" in item for item in verdict["reasons"]))
        self.assertTrue(any("pinned successful" in item for item in verdict["reasons"]))
        self.assertTrue(any("Vulkan candidate was skipped" in item for item in verdict["reasons"]))

    def test_strict_loader_rejects_duplicate_keys_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "receipt.json"
            duplicate.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
            with self.assertRaisesRegex(ReceiptError, "duplicate JSON key"):
                load_receipt(duplicate)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlink unavailable")
            with self.assertRaisesRegex(ReceiptError, "non-link"):
                load_receipt(link)

    def test_cli_requires_and_binds_sibling_hardware_receipt(self):
        value = complete_receipt()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hardware_path = root / "hardware-receipt.json"
            hardware_path.write_bytes(hardware_bytes(value))
            receipt_path = root / "target-acceptance-receipt.json"
            receipt_path.write_text(json.dumps(value), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(2, verify_main([str(receipt_path)]))
            hardware_path.write_text("{}", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(2, verify_main([str(receipt_path)]))


class WindowsAcceptanceHarnessStaticTests(unittest.TestCase):
    def test_hardware_probe_refuses_before_query_probe_or_output(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text(encoding="utf-8")
        self.assertIn("throw 'NOT_READY: bounded native Windows hardware collector is unavailable", source)
        self.assertIn("no output was written", source)
        for forbidden in ("Get-CimInstance", "ConvertTo-Json", "Add-Type", "System.Diagnostics.Process", "WriteAllText", "Set-Content", "New-Item", "Get-FileHash", "Invoke-WebRequest"):
            self.assertNotIn(forbidden, source)

    def test_harness_refuses_before_package_account_process_or_receipt_access(self):
        source = (ROOT / "qa/windows_acceptance/Invoke-WindowsAcceptance.ps1").read_text(encoding="utf-8")
        self.assertIn("throw 'NOT_READY: portable package, launch, and bounded hardware collection are unavailable", source)
        self.assertIn("no receipt was written", source)
        for forbidden in ("Get-Item", "Get-Content", "Get-CimInstance", "Add-Type", "System.Diagnostics.Process", "HttpClient", "ShellExecute", "Start-Process", "Read-Host", "WriteAllText", "Set-Content", "New-Item"):
            self.assertNotIn(forbidden, source)

    def test_profile_and_docs_refuse_current_readiness_or_vulkan_promotion(self):
        profile = json.loads((ROOT / "qa/windows_acceptance/target-profile.json").read_text(encoding="utf-8"))
        self.assertEqual("not-ready-producers-and-launcher-disabled", profile["status"])
        self.assertEqual("disabled-before-path-access", profile["package_builder"])
        self.assertEqual("disabled-before-process-creation", profile["portable_launch"])
        self.assertEqual("disabled-before-management-query", profile["hardware_collection"])
        self.assertEqual("disabled-before-input-access", profile["acceptance_producer"])
        self.assertFalse(profile["backends"]["candidate_is_promoted"])
        self.assertEqual(list(REQUIRED_LIVE_CHECKS), profile["required_live_checks_for_full_access"])
        docs = (ROOT / "qa/windows_acceptance/README.md").read_text(encoding="utf-8")
        self.assertIn("NOT_READY", docs)
        self.assertIn("refuses before reading", docs)
        self.assertIn("always includes", docs)


if __name__ == "__main__":
    unittest.main()
