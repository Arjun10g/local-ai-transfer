"""Source-only checks for the inert ActionJournal helper/pipe slice.

Nothing here compiles or loads the Windows code, creates a pipe/process, or
opens the native journal. The behavioral tests use only merged in-memory
reference models and synthetic bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import struct
import unittest


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "native" / "action_journal_helper"
CONTRACT = ROOT / "contracts" / "action-journal-helper" / "v0.1.0.json"
VECTOR = ROOT / "tests" / "native" / "fixtures" / "action_journal_helper" / "session-vector.json"
PROTOCOL_VECTOR = ROOT / "contracts" / "action-journal" / "v0.1.0-vectors.json"
MAX_SOURCE = 256 * 1024
MAX_JSON = 128 * 1024


def strict_json(path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_JSON:
        raise ValueError("oversized JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)


def source(path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE:
        raise ValueError("oversized source")
    return raw.decode("utf-8", errors="strict")


class WindowsActionJournalHelperStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = strict_json(CONTRACT)
        cls.vector = strict_json(VECTOR)
        cls.protocol_vector = strict_json(PROTOCOL_VECTOR)
        cls.main = source(NATIVE / "main.cpp")
        cls.pipe = source(NATIVE / "pipe_server.cpp")
        cls.protocol = source(NATIVE / "protocol_codec.cpp")
        cls.store = source(NATIVE / "store_codec.cpp")
        cls.headers = "\n".join(source(path) for path in sorted(NATIVE.glob("*.hpp")))

    def test_contract_and_code_are_inert(self):
        for key in (
            "production_available", "native_target_registered",
            "node_integration_added", "cmake_added", "package_added",
            "activation_permitted",
        ):
            self.assertIs(self.contract[key], False)
        cmake = source(ROOT / "native" / "CMakeLists.txt")
        self.assertNotIn("action_journal_helper", cmake)
        for path in (
            ROOT / "host" / "agent" / "action-journal.mjs",
            ROOT / "host" / "agent" / "action-journal-protocol.mjs",
            ROOT / "lae-host.mjs",
        ):
            self.assertNotIn("action_journal_helper", source(path))
        self.assertIn("PRODUCTION_ACTION_JOURNAL_PROTOCOL_AVAILABLE = false", source(
            ROOT / "host" / "agent" / "action-journal-protocol.mjs"
        ))

    def test_windows_headers_define_macro_guards_before_windows(self):
        for path in NATIVE.glob("*.hpp"):
            text = source(path)
            self.assertLess(text.index("#define NOMINMAX"), text.index("#include <windows.h>"))
            self.assertLess(text.index("#define WIN32_LEAN_AND_MEAN"), text.index("#include <windows.h>"))

    def test_bootstrap_vector_is_exact_and_cross_language_decodable(self):
        item = self.vector["bootstrap"]
        raw = bytes.fromhex(item["hex"])
        self.assertEqual(len(raw), item["bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
        self.assertEqual(raw[:16], b"LAEJRNHELPBOOT\0\0")
        self.assertEqual(struct.unpack_from("<II", raw, 16), (1, len(raw)))
        self.assertEqual(struct.unpack_from("<II", raw, 24), (4242, 3))
        self.assertEqual(struct.unpack_from("<Q", raw, 32)[0], 133485408000000000)
        storage_chars, image_chars = struct.unpack_from("<II", raw, 168)
        self.assertEqual(raw[176:192], bytes(16))
        storage_end = 192 + storage_chars * 2
        self.assertEqual(raw[192:storage_end].decode("utf-16le"), item["storage_directory"])
        self.assertEqual(raw[storage_end:storage_end + image_chars * 2].decode("utf-16le"), item["client_image_path"])
        self.assertEqual(storage_end + image_chars * 2, len(raw))

    def test_bootstrap_is_stdin_pipe_only_bounded_closed_and_zeroed(self):
        for token in (
            "GetStdHandle(STD_INPUT_HANDLE)", "GetFileType(input) != FILE_TYPE_PIPE",
            "kMaximumBootstrapBytes = 65'536", "kBootstrapDeadlineMs = 15'000",
            "PeekNamedPipe", "ERROR_BROKEN_PIPE", "SecureZeroMemory",
            "VirtualLock", "VirtualUnlock",
        ):
            self.assertIn(token, self.pipe)
        self.assertNotIn("GetEnvironmentVariable", self.pipe)
        self.assertNotIn("GetCommandLine", self.pipe)
        self.assertNotRegex(self.main, r"argv\s*\[")
        self.assertIn("argc != 1", self.main)
        self.assertNotIn("CreateFileW(bootstrap", self.pipe)

    def test_storage_is_exact_open_existing_retained_authority_before_pipe(self):
        for token in (
            "OpenMode::kOpenExisting", "has_expected_identity = true",
            "expected_storage_volume_serial", "expected_storage_file_id",
            "expected_container_id", "JournalStorageLease&& lease",
            "retained_file_handle()", "load_and_recover", "CreateNamedPipeW",
        ):
            self.assertIn(token, self.pipe + self.store + self.headers)
        self.assertLess(self.pipe.index("store.load_and_recover"), self.pipe.index("CreateNamedPipeW"))
        self.assertNotIn("storage_directory", self.store)
        self.assertNotRegex(self.store, r"CreateFileW|DeleteFileW|MoveFileW")

    def test_pipe_is_one_local_overlapped_private_instance(self):
        for token in (
            "PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED", "PIPE_TYPE_BYTE",
            "PIPE_READMODE_BYTE", "PIPE_WAIT", "PIPE_REJECT_REMOTE_CLIENTS",
            "SE_DACL_PROTECTED", "observed->AceCount != 1",
            "AddAccessAllowedAceEx", "GENERIC_READ | GENERIC_WRITE | SYNCHRONIZE",
            "CreateWellKnownSid", "WinBuiltinAdministratorsSid", "WinLocalSystemSid",
        ):
            self.assertIn(token, self.pipe)
        self.assertRegex(self.pipe, r"PIPE_REJECT_REMOTE_CLIENTS,\s*\n\s*1,")
        self.assertNotIn("PIPE_UNLIMITED_INSTANCES", self.pipe)

    def test_connected_client_identity_is_exact_and_retained(self):
        for token in (
            "GetNamedPipeClientProcessId", "PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE",
            "GetProcessId", "GetProcessTimes", "ProcessIdToSessionId",
            "OpenProcessToken", "TokenUser", "TokenSessionId", "EqualSid",
            "QueryFullProcessImageNameW", "FILE_FLAG_OPEN_REPARSE_POINT",
            "FileAttributeTagInfo", "FILE_ATTRIBUTE_REPARSE_POINT",
            "basic.nNumberOfLinks != 1", "FileIdInfo", "VolumeSerialNumber",
            "GetFinalPathNameByHandleW", "ClientLease client",
        ):
            self.assertIn(token, self.pipe)
        self.assertNotIn("FILE_SHARE_WRITE", self.pipe)
        self.assertNotIn("FILE_SHARE_DELETE", self.pipe)

    def test_all_pipe_io_is_overlapped_deadline_and_cancel_checked(self):
        for token in (
            "OVERLAPPED operation", "CreateEventW", "WaitForMultipleObjects",
            "WaitForSingleObject", "CancelIoEx", "GetOverlappedResult",
            "kIoDeadlineMs = 15'000", "kCancellationGraceMs = 2'000",
            "kMaxSessionFrames", "kMaxFrameBytes", "kMaxPayloadBytes",
        ):
            self.assertIn(token, self.pipe)
        self.assertNotIn("INFINITE", self.pipe)
        self.assertNotRegex(self.pipe, r"(?:ReadFile|WriteFile)\([^;]+nullptr\)\s*;")

    def test_protocol_is_strict_canonical_authenticated_and_replay_bounded(self):
        for token in (
            "MB_ERR_INVALID_CHARS", "parse_event_t::key", "duplicate = true",
            "kMaximumJsonDepth = 16", "kMaximumJsonNodes = 4'096",
            "kMaximumObjectFields = 24", "kMaximumArrayItems = 128",
            "kMaximumStringBytes = 2'048", "canonical != input",
            "BCRYPT_ALG_HANDLE_HMAC_FLAG", "constant_time_equal",
            "nonce_", "inbound_sequence_", "outbound_sequence_",
            "request_ids_", "kMaxSessionFrames",
        ):
            self.assertIn(token, self.protocol + self.headers)
        self.assertIn("SecureZeroMemory(key_.data()", self.protocol)

    def test_merged_health_hmac_vector_matches_cpp_formula(self):
        health = next(item for item in self.protocol_vector["vectors"] if item["name"] == "health_request")
        key = bytes.fromhex(self.protocol_vector["key_hex"])
        actual = hmac.new(
            key,
            b"lae.action-journal.v0.1.0\0" + health["canonical_without_mac"].encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(actual, health["mac_hex"])
        self.assertIn("std::string(kProtocol) + '\\0'", self.protocol)

    def test_prepare_operation_and_binding_vectors_match_domains(self):
        item = self.vector["prepare_binding"]
        operation = hmac.new(
            bytes.fromhex(item["container_id_hex"]),
            b"operation\0" + item["operation_digest"].encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual("act_" + operation[:32], item["operation_id"])
        binding = hashlib.sha256(
            b"lae.action-journal.helper.v0.1.0\0prepare-binding\0"
            + item["canonical_body"].encode()
        ).hexdigest()
        self.assertEqual(binding, item["prepare_binding_digest"])
        self.assertIn('std::string prefix("operation\\0", 10)', self.store)

    def test_store_scans_fixed_authority_and_rejects_conflict_caps_duplicates(self):
        for token in (
            "for (std::uint32_t slot = 0; slot < kSlotCount; ++slot)",
            "kSlotCount = 1'024", "kBanksPerSlot = 2",
            "same_history_prefix", "kConflictingAuthority", "kDuplicateOperation",
            "kMaxActiveRecords = 256", "kMaxTerminalRecords = 768",
            "staged_bank.fill", "records_ = std::move(next)",
        ):
            self.assertIn(token, self.store + self.headers)

    def test_store_commit_order_is_fixed_flush_readback_and_session_poisoning(self):
        ordered = [
            "write_exact(file_, marker_offset(slot, bank), staging.data()",
            "FlushFileBuffers(file_)",
            "equal_bytes(staging.data(), marker_readback.data()",
            "write_exact(file_, bank_offset(slot, bank), body.data()",
            "equal_bytes(body.data(), body_readback.data()",
            "write_exact(file_, marker_offset(slot, bank), committed_marker.data()",
            "equal_bytes(committed_marker.data(), marker_readback.data()",
            "status = reload()",
        ]
        positions = []
        start = 0
        for token in ordered:
            position = self.store.index(token, start)
            positions.append(position)
            start = position + 1
        self.assertEqual(positions, sorted(positions))
        self.assertGreaterEqual(self.store.count("poisoned_ = true"), 8)
        self.assertIn("if (poisoned_) return StoreStatus::kInternal", self.store)
        self.assertIn("commit_section_ = true", self.store)

    def test_startup_recovery_never_replays_dispatch(self):
        for token in (
            'state == "prepared"', 'state == "authorized"',
            'state == "dispatching"', 'state == "acknowledged"',
            'state == "reconciling"', '"startup_recovery"',
            '"cancelled"', '"unknown_manual"', '"dispatch_ambiguous"',
        ):
            self.assertIn(token, self.store)
        recovery = self.store[self.store.index("FixedContainerStore::load_and_recover"):self.store.index("FixedContainerStore::append")]
        self.assertNotIn('"dispatch"', recovery)

    def test_response_surface_is_finite_and_redacted(self):
        helper_statuses = set(re.findall(r'return "([a-z0-9_]+)";', self.pipe))
        self.assertEqual(helper_statuses, set(self.contract["helper_status_codes"]))
        result_struct = re.search(r"struct EncodedResult \{(.+?)\n\};", self.headers, re.S).group(1).lower()
        for forbidden in self.contract["receipts"]["forbidden"]:
            self.assertNotIn(forbidden, result_struct)
        for forbidden in ("printf(", "fprintf(", "std::cout", "OutputDebugString", "FormatMessage"):
            self.assertNotIn(forbidden, self.pipe + self.protocol + self.store + self.main)

    def test_source_contains_no_process_network_service_or_provider_surface(self):
        text = self.pipe + self.protocol + self.store + self.main
        for forbidden in (
            "CreateProcess", "ShellExecute", "WinHttp", "WSAStartup", "socket(",
            "connect(", "listen(", "StartService", "CreateService", "RegSetValue",
            "graph.microsoft.com", "api.github.com", "model_path",
        ):
            self.assertNotIn(forbidden, text)

    def test_reference_model_is_tests_only_and_production_false(self):
        reference = source(ROOT / "tests" / "reference" / "action-journal-helper-model.mjs")
        self.assertIn("ACTION_JOURNAL_HELPER_REFERENCE_ONLY = true", reference)
        self.assertIn("ACTION_JOURNAL_HELPER_PRODUCTION_AVAILABLE = false", reference)
        for path in ROOT.glob("host/**/*.mjs"):
            self.assertNotIn("action-journal-helper-model", source(path))

    def test_qa_inventory_classifies_new_executable_and_support_files(self):
        qa = source(ROOT / "scripts" / "test" / "run_qa.py")
        for token in (
            '"tests/host/action-journal-helper-model.test.mjs": "host_fixture"',
            '"tests/native/test_windows_action_journal_helper_static.py": "native_static"',
            '"tests/reference/action-journal-helper-model.mjs"',
            '"tests/native/fixtures/action_journal_helper/session-vector.json"',
        ):
            self.assertIn(token, qa)


if __name__ == "__main__":
    unittest.main()
