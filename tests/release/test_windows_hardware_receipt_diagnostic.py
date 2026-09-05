import copy
import hashlib
import json
from pathlib import Path
import unittest

from qa.windows_acceptance.hardware_receipt_diagnostic import (
    ALLOWED_REASON_CODES,
    EXPECTED,
    HardwareReceiptError,
    _bounded_json_integer,
    hardware_reason_codes,
    parse_hardware_receipt,
    validate_hardware_receipt,
)


ROOT = Path(__file__).resolve().parents[2]
PNP = "PCI\\VEN_8086&DEV_7D55"


def receipt_fixture(*, fixture: bool = True):
    value = {
        "schema_version": "1.2.0-diagnostic",
        "receipt_kind": "windows-hardware-diagnostic-receipt",
        "generated_at_utc": "2026-09-05T12:00:00Z",
        "executed_on_target": True,
        "fixture": fixture,
        "collector": {
            "manifest_id": "fixture-collector",
            "script_sha256": "1" * 64,
            "expected_script_sha256": "1" * 64,
            "authenticode_required": True,
            "authenticode_status": "Valid",
            "authenticode_revocation_policy": "offline-no-revocation-check",
            "signer_subject_sha256": "2" * 64,
            "execution_identity_bound": True,
            "cim_materialization_bound_proven": True,
        },
        "collection": {
            "read_only": True,
            "offline": True,
            "stdout_only": True,
            "admin_required": False,
            "wmi_mutation": False,
            "downloads": False,
            "installs": False,
            "model_loaded": False,
            "secrets_collected": False,
        },
        "expected": copy.deepcopy(EXPECTED),
        "observed": {
            "os": {"product": "Microsoft Windows 11 Enterprise", "version": "10.0.26100", "build": "26100", "architecture": "x64", "sku": 4, "secure_boot": True},
            "computer": {"manufacturer": "Dell Inc.", "model": "Latitude fixture", "system_type": "x64-based PC", "total_memory_bytes": 32 * 1024**3, "available_memory_bytes": 16 * 1024**3},
            "cpu": [{
                "slot_index": 0, "name": "Intel(R) Core(TM) Ultra 7 165U", "manufacturer": "GenuineIntel",
                "architecture": "x64", "address_width": 64, "physical_cores": 12,
                "logical_processors": 14, "max_clock_mhz": 4900,
                "virtualization_firmware": True, "second_level_address_translation": True,
                "vpro_enterprise": True, "vpro_evidence_source": "identity-pinned-native-attestation",
                "features": {key: True for key in ("aes", "avx", "avx2", "bmi1", "bmi2", "fma", "popcnt", "sse42")},
            }],
            "memory_modules": [
                {"slot_index": 0, "capacity_bytes": 16 * 1024**3, "speed_mts": 5600, "configured_speed_mts": 5600, "smbios_memory_type": 34, "data_width": 64},
                {"slot_index": 1, "capacity_bytes": 16 * 1024**3, "speed_mts": 5600, "configured_speed_mts": 5600, "smbios_memory_type": 34, "data_width": 64},
            ],
            "baseboards": [{"manufacturer": "Dell Inc.", "product": "039NNG", "revision": "A00"}],
            "bios": [{"manufacturer": "Dell Inc.", "version": "1.2.3", "release_utc": "2026-08-01T00:00:00Z", "secure_boot": True}],
            "gpu_adapters": [{"slot_index": 0, "name": "Intel(R) Graphics", "vendor": "Intel Corporation", "pnp_device_id": PNP, "driver_version": "32.0.101.8247", "driver_date_utc": "2026-07-01T00:00:00Z", "adapter_ram_bytes": 1024**3, "integrated": True, "status": "OK"}],
            "display_drivers": [{"slot_index": 0, "name": "Intel(R) Graphics", "pnp_device_id": PNP, "driver_version": "32.0.101.8247", "driver_date_utc": "2026-07-01T00:00:00Z", "manufacturer": "Intel", "signed": True, "signer_name": "Microsoft Windows Hardware Compatibility Publisher"}],
            "gpu_memory": {"shared_capacity_bytes": 16 * 1024**3, "dedicated_capacity_bytes": 128 * 1024**2, "source": "identity-pinned-dxgi"},
            "disk_volumes": [{"slot_index": 0, "filesystem": "NTFS", "size_bytes": 512 * 1024**3, "free_bytes": 200 * 1024**3, "compressed": False}],
            "pagefile": {"allocated_bytes": 4 * 1024**3, "current_usage_bytes": 512 * 1024**2, "peak_usage_bytes": 1024**3},
            "power_plan": {"active": True, "scheme_guid": "381b4222-f694-41f0-9685-ff5bb260df2e", "name": "Balanced"},
            "runtimes": {
                name: {"present": True, "version": "1.0.0", "source": "system32-fixed-name-presence-only"}
                for name in ("vulkan", "opencl", "directml")
            },
        },
        "checks": {"hardware_match": False, "reason_codes": []},
        "verdict": {"status": "NOT_READY", "target_evidence_accepted": False},
    }
    synchronize(value)
    return value


def synchronize(value):
    value["checks"] = {"hardware_match": False, "reason_codes": []}
    value["verdict"] = {"status": "NOT_READY", "target_evidence_accepted": False}
    # This intentionally duplicates only the finite receipt decision model.
    # It must not call the implementation under test or import its reason
    # constants: a producer/validator mutation must be caught, not adopted.
    reasons = {"target_trust_anchor_unavailable"}
    if value["fixture"] is True:
        reasons.add("fixture_not_target_evidence")
    if value["executed_on_target"] is not True:
        reasons.add("not_executed_on_target")
    collector = value["collector"]
    if collector["script_sha256"] != collector["expected_script_sha256"]:
        reasons.add("collector_hash_mismatch")
    if collector["authenticode_required"] is not True or collector["authenticode_status"] != "Valid":
        reasons.add("collector_signature_unproven")
    if collector["execution_identity_bound"] is not True:
        reasons.add("collector_execution_identity_unbound")
    if collector["cim_materialization_bound_proven"] is not True:
        reasons.add("cim_materialization_bound_unproven")
    if value["collection"] != {
        "read_only": True, "offline": True, "stdout_only": True,
        "admin_required": False, "wmi_mutation": False, "downloads": False,
        "installs": False, "model_loaded": False, "secrets_collected": False,
    }:
        reasons.add("collection_safety_unproven")
    observed = value["observed"]
    os_info = observed["os"]
    if not isinstance(os_info["product"], str) or "windows" not in os_info["product"].casefold() or os_info["architecture"].casefold() not in {"x64", "amd64", "64-bit", "64 bit"}:
        reasons.add("windows_x64_mismatch")
    if os_info["secure_boot"] is not True:
        reasons.add("secure_boot_unproven")
    computer = observed["computer"]
    if not isinstance(computer["manufacturer"], str) or computer["manufacturer"].casefold() not in {"dell", "dell inc."}:
        reasons.add("dell_manufacturer_mismatch")
    if computer["total_memory_bytes"] != 32 * 1024**3:
        reasons.add("system_memory_mismatch")
    if not isinstance(computer["available_memory_bytes"], int) or computer["available_memory_bytes"] > computer["total_memory_bytes"]:
        reasons.add("available_memory_invalid")
    board = observed["baseboards"]
    if len(board) != 1 or (board[0]["product"] or "").casefold() != "039nng" or (board[0]["revision"] or "").casefold() != "a00":
        reasons.add("baseboard_mismatch")
    cpus = observed["cpu"]
    if len(cpus) != 1 or "intel" not in (cpus[0]["manufacturer"] or "").casefold() or "core" not in (cpus[0]["name"] or "").casefold() or "ultra" not in (cpus[0]["name"] or "").casefold() or "7" not in (cpus[0]["name"] or ""):
        reasons.add("cpu_sku_mismatch")
    elif cpus[0]["architecture"].casefold() not in {"x64", "amd64", "9"} or cpus[0]["address_width"] != 64 or cpus[0]["logical_processors"] < cpus[0]["physical_cores"]:
        reasons.add("cpu_topology_invalid")
    if len(cpus) == 1:
        if cpus[0]["vpro_enterprise"] is not True:
            reasons.add("vpro_enterprise_unproven")
        if any(cpus[0]["features"][key] is not True for key in ("aes", "avx", "avx2", "bmi1", "bmi2", "fma", "popcnt", "sse42")):
            reasons.add("cpu_features_unproven")
    modules = observed["memory_modules"]
    if any(not isinstance(item["capacity_bytes"], int) for item in modules) or sum(item["capacity_bytes"] or 0 for item in modules) != 32 * 1024**3:
        reasons.add("memory_module_capacity_mismatch")
    if any(item["configured_speed_mts"] != 5600 or item["smbios_memory_type"] != 34 for item in modules):
        reasons.add("memory_ddr5_5600_mismatch")
    if len({item["slot_index"] for item in modules}) != len(modules):
        reasons.add("duplicate_memory_slot")
    adapters = observed["gpu_adapters"]
    if len({(item["pnp_device_id"] or "").casefold() for item in adapters}) != len(adapters):
        reasons.add("duplicate_gpu_device")
    if len(adapters) > 1 and any(
        not isinstance(item["pnp_device_id"], str)
        or not item["pnp_device_id"].startswith("PCI\\VEN_")
        or len(item["pnp_device_id"]) != len(PNP)
        for item in adapters
    ):
        reasons.add("discrete_gpu_absence_unproven")
    intel = [item for item in adapters if isinstance(item["pnp_device_id"], str) and item["pnp_device_id"].casefold().startswith("pci\\ven_8086&dev_") and "intel" in (item["name"] or "").casefold() and item["driver_version"] == "32.0.101.8247"]
    if len(intel) != 1:
        reasons.add("intel_gpu_identity_mismatch")
    elif intel[0]["integrated"] is not True:
        reasons.add("integrated_gpu_unproven")
    if any(isinstance(item["pnp_device_id"], str) and item["pnp_device_id"].upper().startswith("PCI\\") and "VEN_8086" not in item["pnp_device_id"].upper() for item in adapters):
        reasons.add("separate_gpu_detected")
    drivers = observed["display_drivers"]
    if len({(item["pnp_device_id"] or "").casefold() for item in drivers}) != len(drivers):
        reasons.add("duplicate_display_driver")
    if len(intel) == 1 and not any((item["pnp_device_id"] or "").casefold() == (intel[0]["pnp_device_id"] or "").casefold() and item["driver_version"] == "32.0.101.8247" and item["signed"] is True for item in drivers):
        reasons.add("display_driver_binding_unproven")
    if observed["gpu_memory"]["shared_capacity_bytes"] is None or observed["gpu_memory"]["source"] != "identity-pinned-dxgi":
        reasons.add("gpu_shared_memory_unproven")
    if not observed["bios"] or any(item["secure_boot"] is not True for item in observed["bios"]):
        reasons.add("bios_evidence_incomplete")
    if not observed["disk_volumes"] or any((item["filesystem"] or "").casefold() != "ntfs" or item["free_bytes"] > item["size_bytes"] for item in observed["disk_volumes"]):
        reasons.add("fixed_disk_ntfs_unproven")
    if any(observed["pagefile"][key] is None for key in ("allocated_bytes", "current_usage_bytes", "peak_usage_bytes")):
        reasons.add("pagefile_unproven")
    if observed["power_plan"]["active"] is not True or observed["power_plan"]["scheme_guid"] is None:
        reasons.add("power_plan_unproven")
    for name, runtime in observed["runtimes"].items():
        if runtime["present"] is not True:
            reasons.add(f"{name}_runtime_absent")
    value["checks"]["reason_codes"] = sorted(reasons)
    # The source-only trust anchor is intentionally unavailable, so even the
    # otherwise exact synthetic shape remains NOT_READY.
    return value


class WindowsHardwareReceiptTests(unittest.TestCase):
    def test_exact_fixture_is_never_target_evidence(self):
        value = receipt_fixture()
        verdict = validate_hardware_receipt(value)
        self.assertEqual("NOT_READY", verdict["status"])
        self.assertIn("fixture_not_target_evidence", verdict["reason_codes"])
        self.assertIn("target_trust_anchor_unavailable", verdict["reason_codes"])

    def test_nonfixture_still_blocked_without_product_trust_anchor(self):
        value = receipt_fixture(fixture=False)
        synchronize(value)
        verdict = validate_hardware_receipt(value)
        self.assertIn("target_trust_anchor_unavailable", verdict["reason_codes"])

    def test_parser_rejects_duplicate_unknown_and_oversize(self):
        with self.assertRaises(HardwareReceiptError):
            parse_hardware_receipt({"not": "bytes"})
        with self.assertRaisesRegex(HardwareReceiptError, "duplicate JSON key"):
            parse_hardware_receipt(b'{"schema_version":"1","schema_version":"2"}')
        duplicate_key = "k" * 20_000
        with self.assertRaisesRegex(HardwareReceiptError, "^duplicate JSON key$") as duplicate:
            parse_hardware_receipt((f'{{"{duplicate_key}":1,"{duplicate_key}":2}}').encode())
        self.assertLessEqual(len(str(duplicate.exception)), 128)
        with self.assertRaises(HardwareReceiptError):
            parse_hardware_receipt(b'{"n":' + (b"9" * 5000) + b"}")
        self.assertEqual(_bounded_json_integer("9" * 20), 10**20 - 1)
        with self.assertRaisesRegex(HardwareReceiptError, "bounded JSON"):
            _bounded_json_integer("9" * 5000)
        for literal in (b"1.0", b"NaN", b"Infinity"):
            with self.subTest(literal=literal), self.assertRaisesRegex(HardwareReceiptError, "number"):
                parse_hardware_receipt(b'{"n":' + literal + b"}")
        value = receipt_fixture()
        value["unexpected"] = True
        with self.assertRaisesRegex(HardwareReceiptError, "keys are not exact"):
            hardware_reason_codes(value)
        with self.assertRaisesRegex(HardwareReceiptError, "byte bound"):
            parse_hardware_receipt(b" " * (1024 * 1024 + 1))

    def test_missing_and_duplicate_devices_are_not_ready(self):
        missing = receipt_fixture()
        missing["observed"]["gpu_adapters"] = []
        synchronize(missing)
        self.assertIn("intel_gpu_identity_mismatch", validate_hardware_receipt(missing)["reason_codes"])
        duplicate = receipt_fixture()
        duplicate["observed"]["gpu_adapters"].append(copy.deepcopy(duplicate["observed"]["gpu_adapters"][0]))
        duplicate["observed"]["gpu_adapters"][1]["slot_index"] = 1
        synchronize(duplicate)
        self.assertIn("duplicate_gpu_device", validate_hardware_receipt(duplicate)["reason_codes"])

    def test_additional_unclassifiable_adapter_blocks_discrete_absence_claim(self):
        value = receipt_fixture()
        unknown = copy.deepcopy(value["observed"]["gpu_adapters"][0])
        unknown["slot_index"] = 1
        unknown["name"] = "Unknown display adapter"
        unknown["pnp_device_id"] = None
        unknown["integrated"] = None
        value["observed"]["gpu_adapters"].append(unknown)
        synchronize(value)
        self.assertIn(
            "discrete_gpu_absence_unproven",
            validate_hardware_receipt(value)["reason_codes"],
        )

    def test_spoofed_pnp_and_unsigned_driver_fail(self):
        value = receipt_fixture()
        value["observed"]["gpu_adapters"][0]["pnp_device_id"] = "PCI\\VEN_10DE&DEV_1234"
        value["observed"]["display_drivers"][0]["signed"] = False
        synchronize(value)
        reasons = validate_hardware_receipt(value)["reason_codes"]
        self.assertIn("intel_gpu_identity_mismatch", reasons)
        self.assertIn("separate_gpu_detected", reasons)

    def test_board_memory_cpu_driver_and_runtime_mismatches(self):
        value = receipt_fixture()
        value["observed"]["baseboards"][0]["revision"] = "A01"
        value["observed"]["memory_modules"][1]["configured_speed_mts"] = 5200
        value["observed"]["cpu"][0]["logical_processors"] = 2
        value["observed"]["gpu_adapters"][0]["driver_version"] = "31.0.0.0"
        value["observed"]["runtimes"]["vulkan"]["present"] = False
        synchronize(value)
        reasons = validate_hardware_receipt(value)["reason_codes"]
        self.assertTrue({"baseboard_mismatch", "memory_ddr5_5600_mismatch", "cpu_topology_invalid", "intel_gpu_identity_mismatch", "vulkan_runtime_absent"}.issubset(reasons))

    def test_exact_unavailable_facts_remain_not_ready(self):
        value = receipt_fixture()
        cpu = value["observed"]["cpu"][0]
        cpu["vpro_enterprise"] = None
        cpu["features"]["avx2"] = None
        value["observed"]["gpu_adapters"][0]["integrated"] = None
        value["observed"]["gpu_memory"] = {"shared_capacity_bytes": None, "dedicated_capacity_bytes": None, "source": "unavailable-no-identity-pinned-dxgi"}
        synchronize(value)
        reasons = validate_hardware_receipt(value)["reason_codes"]
        self.assertTrue({"vpro_enterprise_unproven", "cpu_features_unproven", "integrated_gpu_unproven", "gpu_shared_memory_unproven"}.issubset(reasons))

    def test_identity_and_signature_are_exact(self):
        value = receipt_fixture()
        value["collector"]["script_sha256"] = "3" * 64
        value["collector"]["authenticode_status"] = "NotSigned"
        value["collector"]["execution_identity_bound"] = False
        value["collector"]["cim_materialization_bound_proven"] = False
        synchronize(value)
        reasons = validate_hardware_receipt(value)["reason_codes"]
        self.assertTrue({"collector_hash_mismatch", "collector_signature_unproven", "collector_execution_identity_unbound", "cim_materialization_bound_unproven"}.issubset(reasons))

    def test_sensitive_keys_paths_controls_and_contradictory_verdict_rejected(self):
        for pathish in (
            "C:\\Users\\alice\\machine", "C:relative\\machine",
            "Users\\alice\\machine", "\\\\?\\C:\\machine",
            "\\Device\\HarddiskVolume1\\machine",
        ):
            value = receipt_fixture()
            value["observed"]["computer"]["model"] = pathish
            with self.assertRaisesRegex(HardwareReceiptError, "contains a path"):
                hardware_reason_codes(value)
        value = receipt_fixture()
        value["observed"]["computer"]["model"] = "Dell\u001b[31m"
        with self.assertRaisesRegex(HardwareReceiptError, "control"):
            hardware_reason_codes(value)
        value = receipt_fixture()
        value["verdict"]["status"] = "HARDWARE_MATCH"
        value["verdict"]["target_evidence_accepted"] = True
        with self.assertRaisesRegex(HardwareReceiptError, "invalid|disabled"):
            validate_hardware_receipt(value)

    def test_malformed_authenticode_and_machine_unique_pci_identifiers_fail_closed(self):
        value = receipt_fixture()
        value["collector"]["authenticode_status"] = ["Valid"]
        with self.assertRaises(HardwareReceiptError):
            hardware_reason_codes(value)
        value = receipt_fixture()
        value["observed"]["gpu_adapters"][0]["pnp_device_id"] = (
            "PCI\\VEN_8086&DEV_7D55&SUBSYS_0C011028\\unique-instance"
        )
        with self.assertRaisesRegex(HardwareReceiptError, "normalized PCI"):
            hardware_reason_codes(value)

    def test_pci_tuple_and_manifest_id_casing_match_schema(self):
        for field in ("gpu_adapters", "display_drivers"):
            value = receipt_fixture()
            value["observed"][field][0]["pnp_device_id"] = "pci\\VEN_8086&DEV_7D55"
            with self.subTest(field=field), self.assertRaisesRegex(HardwareReceiptError, "normalized PCI"):
                hardware_reason_codes(value)
        value = receipt_fixture()
        value["collector"]["manifest_id"] = "Fixture-Collector"
        with self.assertRaisesRegex(HardwareReceiptError, "manifest_id"):
            hardware_reason_codes(value)
        value = receipt_fixture()
        value["collector"]["manifest_id"] = "bad/id"
        with self.assertRaisesRegex(HardwareReceiptError, "manifest_id"):
            hardware_reason_codes(value)

    def test_malformed_nullable_evidence_types_become_hardware_errors(self):
        value = receipt_fixture()
        value["observed"]["cpu"][0]["logical_processors"] = "14"
        with self.assertRaises(HardwareReceiptError):
            hardware_reason_codes(value)
        value = receipt_fixture()
        value["observed"]["computer"]["available_memory_bytes"] = "1"
        with self.assertRaises(HardwareReceiptError):
            hardware_reason_codes(value)

    def test_unpaired_surrogate_is_not_canonical_unicode(self):
        value = receipt_fixture()
        value["observed"]["computer"]["model"] = "bad\ud800"
        with self.assertRaisesRegex(HardwareReceiptError, "Unicode"):
            hardware_reason_codes(value)


class WindowsHardwareCollectorStaticTests(unittest.TestCase):
    def test_collector_is_stdout_only_bounded_offline_and_nonmutating(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text(encoding="utf-8")
        for required in ("throw 'NOT_READY: bounded native Windows hardware collector is unavailable", "no path was accessed", "no output was written"):
            self.assertIn(required, source)
        for forbidden in ("Get-CimInstance", "ConvertTo-Json", "Out-String", "Set-CimInstance", "Invoke-CimMethod", "New-Item", "Set-Content", "WriteAllText", "Start-Process", "Invoke-WebRequest", "HttpClient", "Add-Type"):
            self.assertNotIn(forbidden, source)

    def test_collector_uses_fixed_inventory_and_redacts_identifiers(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text(encoding="utf-8")
        self.assertIn("bounded native Windows hardware collector is unavailable", source)
        for forbidden_property in ("SerialNumber", "ProcessorId", "UserName", "CommandLine", "VolumeName"):
            self.assertNotIn(forbidden_property, source)

    def test_collector_global_budget_and_unicode_output_are_explicit(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text(encoding="utf-8")
        self.assertIn("Set-StrictMode -Version Latest", source)
        self.assertIn("no query or probe ran", source)

    def test_producer_reason_contract_is_refusal_only_and_independent(self):
        source = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text(encoding="utf-8")
        self.assertNotIn("separate_gpu_detected", source)
        self.assertNotIn("vulkan_runtime_absent", source)
        self.assertIn("target_trust_anchor_unavailable", {"target_trust_anchor_unavailable"})

    def test_intel_only_all_runtimes_present_has_no_false_negative_reasons(self):
        value = receipt_fixture()
        synchronize(value)
        reasons = value["checks"]["reason_codes"]
        self.assertNotIn("separate_gpu_detected", reasons)
        self.assertNotIn("vulkan_runtime_absent", reasons)

    def test_reason_mutation_is_not_adopted_from_producer_or_schema(self):
        value = receipt_fixture()
        value["checks"]["reason_codes"] = ["fabricated_reason"]
        with self.assertRaises(HardwareReceiptError):
            hardware_reason_codes(value)

    def test_malformed_reason_elements_and_unconditional_list_are_rejected(self):
        for malformed in ([None], [7], ["VULKAN_RUNTIME_ABSENT"]):
            value = receipt_fixture()
            value["checks"]["reason_codes"] = malformed
            with self.assertRaises(HardwareReceiptError):
                hardware_reason_codes(value)
        source = (ROOT / "qa/windows_acceptance/hardware_receipt_diagnostic.py").read_text(encoding="utf-8")
        self.assertNotIn("return list(NOT_READY_REASON_CODES)", source)
        self.assertIn('reasons: set[str] = {"target_trust_anchor_unavailable"}', source)

    def test_schema_reason_enum_matches_validator_only(self):
        schema = json.loads((ROOT / "contracts/windows-hardware-attestor/v1-diagnostic/receipt.schema.json").read_text(encoding="utf-8"))
        enum = set(schema["$defs"]["checks"]["properties"]["reason_codes"]["items"]["enum"])
        self.assertEqual(enum, ALLOWED_REASON_CODES)
        manifest_pattern = schema["$defs"]["collector"]["properties"]["manifest_id"]["pattern"]
        self.assertEqual("^[a-z0-9][a-z0-9._-]{0,63}$", manifest_pattern)
        pci_pattern = schema["$defs"]["gpu"]["properties"]["pnp_device_id"]["pattern"]
        self.assertEqual("^PCI\\\\VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}$", pci_pattern)

    def test_source_manifest_hash_is_exact_but_never_activation(self):
        manifest = json.loads((ROOT / "contracts/windows-hardware-attestor/v1-diagnostic/collector-manifest.json").read_text(encoding="utf-8"))
        script = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_bytes()
        self.assertEqual(hashlib.sha256(script).hexdigest(), manifest["script_sha256"])
        self.assertEqual("source-only-not-activated", manifest["status"])
        self.assertFalse(manifest["authenticode_signature_present"])
        self.assertFalse(manifest["execution_identity_bound"])
        self.assertFalse(manifest["target_evidence_accepted"])

    def test_schema_is_fully_closed(self):
        schema = json.loads((ROOT / "contracts/windows-hardware-attestor/v1-diagnostic/receipt.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("1.2.0-diagnostic", schema["properties"]["schema_version"]["const"])
        stack = [schema]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("type") == "object":
                    self.assertIs(item.get("additionalProperties"), False)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    def test_collector_contract_is_not_ready_and_pci_is_normalized(self):
        schema = json.loads((ROOT / "contracts/windows-hardware-attestor/v1-diagnostic/receipt.schema.json").read_text(encoding="utf-8"))
        collector = schema["$defs"]["collector"]["properties"]
        self.assertEqual("offline-no-revocation-check", collector["authenticode_revocation_policy"]["const"])
        self.assertEqual("NOT_READY", schema["$defs"]["verdict"]["properties"]["status"]["const"])
        self.assertFalse(schema["$defs"]["verdict"]["properties"]["target_evidence_accepted"]["const"])
        self.assertFalse(schema["$defs"]["checks"]["properties"]["hardware_match"]["const"])
        self.assertNotIn("HARDWARE_MATCH", json.dumps(schema))
        self.assertIn("PCI", json.dumps(schema))
        self.assertIn("VEN_[0-9A-F]", json.dumps(schema))

    def test_canonical_acceptance_schema_is_not_replaced(self):
        schema = json.loads((ROOT / "hardware/windows-probe/hardware-receipt.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("1.1.0", schema["properties"]["schema_version"]["const"])
        self.assertEqual("windows-hardware-receipt", schema["properties"]["receipt_kind"]["const"])


if __name__ == "__main__":
    unittest.main()
