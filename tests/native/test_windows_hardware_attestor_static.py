import copy
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "native" / "windows_hardware_attestor"
CPP = SOURCE_DIR / "windows_hardware_attestor.cpp"
HEADER = SOURCE_DIR / "windows_hardware_attestor.hpp"
ANCHOR = SOURCE_DIR / "trust_anchor.hpp"
SCHEMA = ROOT / "contracts" / "windows-hardware-attestor" / "v1" / "receipt.schema.json"
CONTRACT = ROOT / "contracts" / "windows-hardware-attestor" / "v1" / "contract.json"
FIXTURE = ROOT / "tests" / "native" / "fixtures" / "windows_hardware_attestor" / "source-refusal.fixture.json"


def strict_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def assert_sorted_keys(test, value):
    if isinstance(value, dict):
        test.assertEqual(list(value), sorted(value))
        for child in value.values():
            assert_sorted_keys(test, child)
    elif isinstance(value, list):
        for child in value:
            assert_sorted_keys(test, child)


class WindowsHardwareAttestorStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = CPP.read_text(encoding="utf-8")
        cls.header = HEADER.read_text(encoding="utf-8")
        cls.anchor = ANCHOR.read_text(encoding="utf-8")
        cls.schema = strict_json(SCHEMA)
        cls.contract = strict_json(CONTRACT)
        cls.fixture = strict_json(FIXTURE)

    def test_source_is_windows_only_and_not_activated(self):
        self.assertIn('#error "The Windows hardware attestor source is Windows-only"', self.header)
        self.assertNotIn("windows_hardware_attestor", (ROOT / "native" / "CMakeLists.txt").read_text(encoding="utf-8"))
        self.assertFalse(self.contract["activation"]["cmake_target_present"])
        self.assertFalse(self.contract["activation"]["host_import_present"])
        self.assertFalse(self.contract["activation"]["package_entry_present"])
        self.assertFalse(self.contract["activation"]["production_available"])

    def test_compile_time_trust_anchor_is_empty_and_has_no_override(self):
        self.assertIn("kConfigured = false", self.anchor)
        self.assertIn("kExpectedExecutableSize = 0", self.anchor)
        self.assertGreaterEqual(self.anchor.count("Sha256{}"), 4)
        for forbidden in ("getenv", "GetEnvironmentVariable", "argv", "registry", "config_path"):
            self.assertNotIn(forbidden, self.anchor)

    def test_public_entry_refuses_before_hardware_collection(self):
        body = self.cpp[self.cpp.index("AttestorStatus collect_attestation("):]
        gate = body.index("!trust_anchor::kConfigured")
        policy = body.index("!trust_anchor::kOfflineAuthenticodePolicyReviewed")
        supervision = body.index("!trust_anchor::kSupervisedGlobalDeadlineAvailable")
        collection = body.index("status = collect_full(context, evidence)")
        self.assertLess(gate, policy)
        self.assertLess(policy, supervision)
        self.assertLess(supervision, collection)

    def test_stdout_refuses_before_handle_access_without_supervision(self):
        body = self.cpp[self.cpp.index("AttestorStatus write_receipt_stdout"):]
        self.assertLess(body.index("!trust_anchor::kSupervisedGlobalDeadlineAvailable"), body.index("GetStdHandle"))
        self.assertIn("type != FILE_TYPE_CHAR && type != FILE_TYPE_PIPE", body)
        self.assertIn("result.canonical_json.size() > kMaximumReceiptBytes", body)

    def test_global_deadline_is_explicitly_unproven(self):
        self.assertIn("kGlobalDeadlineMs = 30'000", self.header)
        self.assertIn("kSupervisedGlobalDeadlineAvailable = false", self.anchor)
        self.assertIn("supervised_process_wide_30_second_deadline", self.contract["required_activation_evidence"])
        self.assertEqual(self.fixture["checks"]["deadline_supervised"], False)

    def test_identity_handles_deny_write_and_delete_and_are_rechecked(self):
        self.assertIn("constexpr DWORD kIdentityShare = FILE_SHARE_READ", self.cpp)
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", self.cpp)
        self.assertIn("standard.NumberOfLinks != 1", self.cpp)
        self.assertIn("reopened_executable.file_id != executable.file_id", self.cpp)
        self.assertIn("reopened_executable.volume_serial != executable.volume_serial", self.cpp)

    def test_offline_authenticode_policy_is_explicit_and_unapproved(self):
        self.assertIn("WTD_CACHE_ONLY_URL_RETRIEVAL", self.cpp)
        self.assertIn("WTD_REVOKE_NONE", self.cpp)
        self.assertIn("kOfflineAuthenticodePolicyReviewed = false", self.anchor)
        self.assertIn("kExpectedSignerCertificateSha256", self.cpp)

    def test_no_wmi_cim_or_mutating_or_dynamic_runtime_surface(self):
        for forbidden in (
            "IWbem", "Win32_", "CIM_", "CoCreateInstance", "LoadLibrary",
            "GetProcAddress", "CreateProcess", "ShellExecute", "WinHttp",
            "URLDownload", "PowerShell", "RegSetValue", "SetupDiSet",
        ):
            self.assertNotIn(forbidden, self.cpp)

    def test_cpu_firmware_and_memory_are_bounded(self):
        self.assertIn("__cpuidex", self.cpp)
        self.assertIn("GetLogicalProcessorInformationEx", self.cpp)
        self.assertIn("bytes > kMaximumTopologyBytes", self.cpp)
        self.assertIn("GetSystemFirmwareTable", self.cpp)
        self.assertIn("bytes > kMaximumFirmwareBytes", self.cpp)
        self.assertIn("kMaximumMemoryDevices", self.cpp)
        smbios = self.cpp[self.cpp.index("bool collect_smbios"):self.cpp.index("bool sha256_bytes")]
        self.assertNotIn("SerialNumber", smbios)

    def test_pnp_identity_is_normalized_and_full_instance_id_not_emitted(self):
        self.assertIn("SPDRP_HARDWAREID", self.cpp)
        self.assertIn("PCI;DEV=%04X;REV=%02X;SUBSYS=%08X;VEN=%04X", self.cpp)
        self.assertIn("lae.hardware.pnp-tuple.v1", self.cpp)
        builder = self.cpp[self.cpp.index("void append_display_devices"):]
        self.assertNotIn('\\"hardware_tuple\\"', builder)
        for forbidden in ("SPDRP_LOCATION_INFORMATION", "SPDRP_LOCATION_PATHS", "SetupDiGetDeviceInstanceId"):
            self.assertNotIn(forbidden, self.cpp)

    def test_dxgi_correlation_is_exact_and_ambiguity_fails(self):
        self.assertIn("CreateDXGIFactory1", self.cpp)
        self.assertIn("matches.size() != 1", self.cpp)
        self.assertIn("!matched_display_devices.insert(matches.front()).second", self.cpp)
        self.assertIn('integrated_classification = "unproven"', self.cpp)
        self.assertIn("kMaximumAdapters", self.cpp)

    def test_vulkan_is_fixed_presence_hash_only(self):
        vulkan = self.cpp[self.cpp.index("bool collect_vulkan_presence"):self.cpp.index("bool utc_now")]
        self.assertIn("GetSystemDirectoryW", vulkan)
        self.assertIn('path.append(L"\\\\vulkan-1.dll")', vulkan)
        self.assertIn("open_proof", vulkan)
        self.assertNotIn("LoadLibrary", vulkan)
        self.assertNotIn("CreateProcess", vulkan)

    def test_vpro_is_never_inferred(self):
        self.assertEqual(self.fixture["observed"]["vpro"], {"authoritative": False, "status": "unproven"})
        self.assertFalse(self.fixture["checks"]["vpro_authoritative"])
        self.assertIn('vpro_status = "unproven"', self.cpp)

    def test_receipt_fixture_is_canonical_key_ordered_and_not_evidence(self):
        assert_sorted_keys(self, self.fixture)
        self.assertTrue(self.fixture["fixture"])
        self.assertFalse(self.fixture["executed_on_target"])
        self.assertFalse(self.fixture["target_evidence_accepted"])
        self.assertEqual(self.fixture["verdict"], "NOT_READY")

    def test_schema_closes_every_object(self):
        def walk(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(self.schema)

    def test_fixture_has_exact_closed_top_level_shape(self):
        self.assertEqual(set(self.fixture), set(self.schema["required"]))
        self.assertEqual(set(self.fixture["checks"]), set(self.schema["properties"]["checks"]["required"]))
        self.assertEqual(set(self.fixture["collector"]), set(self.schema["properties"]["collector"]["required"]))
        observed = self.schema["$defs"]["observed"]
        self.assertEqual(set(self.fixture["observed"]), set(observed["required"]))

    def test_schema_rejects_fixture_as_target_evidence(self):
        self.assertEqual(self.schema["properties"]["target_evidence_accepted"]["const"], False)
        self.assertEqual(self.schema["properties"]["verdict"]["const"], "NOT_READY")
        self.assertEqual(self.schema["properties"]["receipt_kind"]["const"], "diagnostic_only")

    def test_expected_profile_does_not_fabricate_exact_cpu_sku(self):
        self.assertEqual(self.fixture["expected"]["cpu_description"], "Intel Core Ultra 7 vPro Enterprise")
        self.assertIsNone(self.fixture["expected"]["cpu_exact_sku"])
        self.assertEqual(self.fixture["expected"]["display_driver"], "32.0.101.8247")
        self.assertEqual(self.fixture["expected"]["board_product"], "039NNG")
        self.assertEqual(self.fixture["expected"]["board_revision"], "A00")

    def test_reason_codes_are_finite(self):
        allowed = set(self.schema["properties"]["reason_codes"]["items"]["enum"])
        self.assertIn("target_trust_anchor_unavailable", allowed)
        for marker in ('add_reason(evidence, "',):
            start = 0
            while True:
                start = self.cpp.find(marker, start)
                if start < 0:
                    break
                start += len(marker)
                end = self.cpp.index('"', start)
                self.assertIn(self.cpp[start:end], allowed)

    def test_bounded_collections_and_receipt(self):
        self.assertEqual(self.contract["limits"]["deadline_ms"], 30000)
        self.assertEqual(self.contract["limits"]["receipt_bytes"], 1048576)
        self.assertIn("index <= kMaximumDisplayDevices", self.cpp)
        self.assertIn("index <= kMaximumAdapters", self.cpp)
        self.assertIn("output.size() > kMaximumReceiptBytes", self.cpp)

    def test_no_sensitive_or_unique_fields_in_receipt_contract(self):
        serialized = json.dumps(self.schema, sort_keys=True).lower()
        for forbidden in ("serial_number", "serialnumber", "username", "user_name", "environment", "file_path", "pnpdeviceid", "device_instance"):
            self.assertNotIn(forbidden, serialized)

    def test_mutated_fixture_unknown_or_missing_fields_are_detectable(self):
        unknown = copy.deepcopy(self.fixture)
        unknown["unexpected"] = True
        missing = copy.deepcopy(self.fixture)
        missing.pop("verdict")
        required = set(self.schema["required"])
        self.assertNotEqual(set(unknown), required)
        self.assertNotEqual(set(missing), required)

    def test_contract_keeps_activation_evidence_explicit(self):
        self.assertEqual(self.contract["status"], "SOURCE_ONLY_NOT_READY")
        required = set(self.contract["required_activation_evidence"])
        self.assertIn("remote_windows_build_and_signature_receipt", required)
        self.assertIn("target_binary_acceptance", required)
        self.assertIn("audited_nonempty_compile_time_trust_anchor", required)


if __name__ == "__main__":
    unittest.main()
