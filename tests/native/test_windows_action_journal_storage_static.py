"""Source-only checks for the inert Win32 ActionJournal storage boundary.

These tests do not compile, load, or execute Windows code. The small decision
model exists only to make refusal ordering and mutation boundaries explicit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import struct
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native" / "action_journal_storage" / "windows_storage.cpp"
HEADER = ROOT / "native" / "action_journal_storage" / "windows_storage.hpp"
CONTRACT = ROOT / "contracts" / "action-journal-storage" / "v0.1.0.json"
VECTOR = ROOT / "tests" / "native" / "fixtures" / "action_journal_storage" / "header-vector.json"
MAX_SOURCE_BYTES = 256 * 1024
MAX_JSON_BYTES = 64 * 1024


def strict_json(path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("fixture is oversized")

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
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source is oversized")
    return raw.decode("utf-8", errors="strict")


def storage_decision(state):
    """Minimal fail-before-mutation model matching the source contract."""

    trace = []
    if not state.get("request_valid", True):
        return "invalid_request", trace
    if not state.get("path_safe", True):
        return "unsafe_path", trace
    for ancestor in state.get("ancestors", [{"plain": True, "stable": True}]):
        trace.append("open_ancestor_no_follow")
        if not ancestor.get("plain") or not ancestor.get("stable"):
            return "reparse_refused", trace
        trace.append("hold_ancestor_deny_write_delete")
    if not state.get("parent_private", True):
        return "private_directory_required", trace
    if not state.get("parent_share_locked", True):
        return "identity_mismatch", trace
    if not state.get("fixed_volume", True) or state.get("hotplug", False):
        return "unsafe_volume", trace
    if state.get("filesystem", "NTFS") != "NTFS":
        return "unsupported_filesystem", trace
    if not state.get("parent_stable_before_leaf", True):
        return "identity_mismatch", trace
    trace.append("open_fixed_leaf")
    if not state.get("leaf_plain", True):
        return "reparse_refused", trace
    if state.get("links", 1) != 1:
        return "link_count_refused", trace
    if state.get("delete_pending", False):
        return "delete_pending", trace
    if not state.get("file_private", True):
        return "security_unavailable", trace
    if state.get("mode", "open") == "open":
        if not state.get("trusted_identity", True) or not state.get("identity_matches", True):
            return "identity_mismatch", trace
        if not state.get("size_matches", True):
            return "size_mismatch", trace
        if not state.get("header_valid", True):
            return "container_corrupt_header", trace
    else:
        trace.append("write_zero_container")
        if not state.get("genesis_flush_readback", True):
            return "genesis_incomplete", trace
        trace.append("write_header")
    trace.append("reopen_fixed_leaf")
    if not state.get("reopen_matches", True) or not state.get("final_stable", True):
        return "reopen_identity_mismatch", trace
    return "ok_created" if state.get("mode") == "create" else "ok_opened", trace


class WindowsActionJournalStorageStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = source(SOURCE)
        cls.hpp = source(HEADER)
        cls.contract = strict_json(CONTRACT)
        cls.vector = strict_json(VECTOR)
        # Everything in this translation unit EXCEPT the v2 descriptor-WAL
        # bodies, so the v1 container entry points and every shared helper
        # (acquire_directories, write_exact, read_exact, final_path,
        # private_security, validate_volume, ...) stay covered by the "never"
        # invariants below. The v2 boundary shares this translation unit; its
        # own invariants are pinned function-anchored by
        # tests/native/test_windows_descriptor_journal_bootstrap_static.py.
        wal_helpers = cls.cpp.index("bool expected_identity_valid(")
        spans = [
            (wal_helpers, cls.cpp.index("}  // namespace\n", wal_helpers)),
            (cls.cpp.index("struct DescriptorWalHandoff::Impl"),
             cls.cpp.index("const char* status_name(")),
        ]
        acquire_wal = re.search(
            r"StorageStatus acquire_descriptor_wal\(const DescriptorWalRequest.*?\n\}",
            cls.cpp, re.S)
        assert acquire_wal is not None
        spans.append(acquire_wal.span())
        remainder, cursor = [], 0
        for start, end in sorted(spans):
            assert cursor <= start < end
            remainder.append(cls.cpp[cursor:start])
            cursor = end
        remainder.append(cls.cpp[cursor:])
        cls.v1_only = "".join(remainder)

    def test_contract_and_source_remain_inert(self):
        for key in (
            "production_available", "native_target_registered", "helper_added",
            "transport_added", "node_integration_added", "package_added",
            "activation_permitted",
        ):
            self.assertIs(self.contract[key], False)
        cmake = source(ROOT / "native" / "CMakeLists.txt")
        self.assertNotIn("action_journal_storage", cmake)
        self.assertNotIn("windows_storage", cmake)
        for path in (
            ROOT / "host" / "agent" / "action-journal.mjs",
            ROOT / "host" / "agent" / "action-journal-protocol.mjs",
            ROOT / "lae-host.mjs",
        ):
            self.assertNotIn("action_journal_storage", source(path))
        self.assertNotRegex(self.cpp, r"\b(main|CreateProcessW|ShellExecuteW|socket|connect|listen)\s*\(")

    def test_machine_status_contract_exactly_matches_header_mapping(self):
        statuses = set(re.findall(r'return "([a-z0-9_]+)";', self.cpp))
        self.assertEqual(statuses, set(self.contract["status_codes"]))
        self.assertIn("kStorageAbiVersion = 1", self.hpp)
        self.assertIn("kContainerBytes = 33'558'528", self.hpp)
        self.assertIn('kFixedLeafName[] = L"action-journal-v1.container"', self.hpp)
        for forbidden in self.contract["receipt"]["forbidden"]:
            self.assertNotIn(forbidden, re.search(
                r"struct StorageReceipt \{(.+?)\n\};", self.hpp, re.S
            ).group(1).lower())

    def test_header_vector_matches_merged_container_layout(self):
        identifier = bytes.fromhex(self.vector["container_id_hex"])
        header = bytearray(4096)
        header[:16] = b"LAEJRNLCONTAINER"
        fields = {
            16: 1, 20: 0x01020304, 24: 4096, 28: 1024, 32: 2,
            36: 16384, 40: 15360, 44: 1024, 48: 1024, 52: 16,
            56: 896, 60: 768, 64: 256, 68: 768, 80: 25, 148: 1,
        }
        for offset, value in fields.items():
            header[offset:offset + 4] = struct.pack("<I", value)
        header[72:80] = struct.pack("<Q", 33558528)
        header[84:109] = b"lae.action-journal.v0.1.0"
        header[152:184] = identifier
        checksum = hashlib.sha256(
            b"lae.action-journal.container.v0.1.0\0"
            + b"header\0" + identifier + header[:4064]
        ).digest()
        header[4064:] = checksum
        self.assertEqual(checksum.hex(), self.vector["header_checksum_hex"])
        self.assertEqual(hashlib.sha256(header).hexdigest(), self.vector["header_sha256"])
        self.assertEqual(header[:208].hex(), self.vector["prefix_208_hex"])

    def test_path_is_fixed_canonical_and_ads_device_network_aliases_are_refused(self):
        for token in (
            "GetFullPathNameW", "safe_component", "reserved_component",
            'supplied.rfind(L"\\\\\\\\", 0)', 'supplied.rfind(L"\\\\\\\\?\\\\", 0)',
            'supplied.rfind(L"\\\\\\\\.\\\\", 0)', "index != 1",
            "equal_path(output, supplied)", "kFixedLeafName",
        ):
            self.assertIn(token, self.cpp)
        self.assertNotIn("request.leaf", self.cpp)

    def test_ancestors_and_leaf_are_nofollow_identity_held_with_strict_sharing(self):
        self.assertIn("FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT", self.cpp)
        self.assertIn("constexpr DWORD kDirectoryShare = FILE_SHARE_READ;", self.cpp)
        self.assertNotRegex(
            self.cpp,
            r"kDirectoryShare\s*=\s*[^;]*(?:FILE_SHARE_WRITE|FILE_SHARE_DELETE)",
        )
        self.assertIn("constexpr DWORD kFileShare = FILE_SHARE_READ;", self.cpp)
        # The whole translation unit minus the v2 descriptor-WAL bodies. Only
        # the v2 identity-reopen handle concedes FILE_SHARE_DELETE, and only
        # because its own first handle holds DELETE and still denies delete
        # sharing to everyone else; that is pinned by
        # tests/native/test_windows_descriptor_journal_bootstrap_static.py.
        self.assertNotIn("FILE_SHARE_DELETE", self.v1_only)
        self.assertIn("std::vector<HeldDirectory> directories", self.cpp)
        self.assertIn("UniqueHandle path_reopen", self.cpp)
        self.assertIn("GetFinalPathNameByHandleW", self.cpp)

    def test_v1_scope_excludes_only_the_v2_bodies_and_keeps_shared_helpers(self):
        for retained in (
            "bool acquire_directories(", "bool write_exact(", "bool read_exact(",
            "bool final_path(", "bool private_security(", "StorageStatus validate_volume(",
            "StorageStatus filesystem_policy(", "bool directories_stable(",
            "bool plain_attributes(", "StorageStatus file_shape(", "bool seek(",
            "bool get_identity(", "bool current_user(", "bool build_private_security(",
            "bool canonical_directory(", "StorageStatus acquire_storage(",
            "StorageStatus zero_initialize(", "const char* status_name(",
        ):
            self.assertIn(retained, self.v1_only)
        for excised in (
            "StorageStatus acquire_descriptor_wal(", "class CreatedFileDiscard final",
            "DescriptorWalLease::", "DescriptorWalHandoff::Impl",
            "StorageStatus descriptor_wal_shape(", "validate_descriptor_wal_prefix(",
        ):
            self.assertNotIn(excised, self.v1_only)
        self.assertLess(len(self.v1_only), len(self.cpp))
        self.assertGreater(len(self.v1_only), len(self.cpp) // 2)

    def test_private_dacl_is_atomic_on_create_and_strict_on_parent_and_file(self):
        for token in (
            "OpenProcessToken", "TokenUser", "CreateWellKnownSid", "GetSecurityInfo",
            "OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION",
            "SE_DACL_PROTECTED", "SE_DACL_DEFAULTED", "dacl->AceCount == 1",
            "ACCESS_ALLOWED_ACE_TYPE", "ace->Header.AceFlags == 0",
            "ace->Mask == kPrivateAccess", "EqualSid", "&security.attributes",
        ):
            self.assertIn(token, self.cpp)

    def test_volume_policy_rejects_nonfixed_hotplug_and_non_ntfs(self):
        for token in (
            "GetDriveTypeW(root) != DRIVE_FIXED", "IOCTL_STORAGE_GET_HOTPLUG_INFO",
            "hotplug.MediaRemovable", "hotplug.MediaHotplug", "hotplug.DeviceHotplug",
            "GetVolumeInformationByHandleW", 'L"NTFS"', "FILE_PERSISTENT_ACLS",
            "FILE_READ_ONLY_VOLUME | FILE_VOLUME_IS_COMPRESSED", "validate_volume",
            "filesystem_serial, true",
        ):
            self.assertIn(token, self.cpp)
        pre_leaf = re.search(
            r"filesystem_policy\(candidate->directories\.back\(\)\.handle\.get\(\)"
            r"[\s\S]+?directories_stable\(candidate->directories, user\.sid\)"
            r"[\s\S]+?HANDLE raw = CreateFileW",
            self.cpp,
        )
        self.assertIsNotNone(pre_leaf)
        self.assertLess(
            self.cpp.index("status = validate_volume(candidate->file.get()"),
            self.cpp.index("status = zero_initialize(candidate->file.get())"),
        )

    def test_leaf_refuses_reparse_unsupported_attributes_links_and_delete_pending(self):
        for token in (
            "FileAttributeTagInfo", "FileStandardInfo", "FILE_ATTRIBUTE_REPARSE_POINT",
            "FILE_ATTRIBUTE_DEVICE", "FILE_ATTRIBUTE_OFFLINE", "FILE_ATTRIBUTE_SPARSE_FILE",
            "FILE_ATTRIBUTE_COMPRESSED", "FILE_ATTRIBUTE_ENCRYPTED",
            "FILE_ATTRIBUTE_RECALL_ON_OPEN", "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
            "FILE_ATTRIBUTE_VIRTUAL", "standard.NumberOfLinks != 1", "standard.DeletePending",
        ):
            self.assertIn(token, self.cpp)

    def test_genesis_is_full_bounded_zero_flush_readback_rng_and_header_readback(self):
        for token in (
            "for (std::uint64_t offset = 0; offset < kContainerBytes",
            "kIoChunkBytes = 65'536", "write_exact", "read_exact", "FlushFileBuffers",
            "BCryptGenRandom", "BCRYPT_USE_SYSTEM_PREFERRED_RNG", "header_digest",
            "decode_header(readback", "SetEndOfFile",
        ):
            self.assertIn(token, self.cpp)
        # The whole translation unit minus the v2 descriptor-WAL bodies: the v1
        # container boundary and every shared helper never delete, rename,
        # replace, or set file information on any path, published or not.
        for forbidden in ("DeleteFile", "MoveFile", "ReplaceFile", "SetFileInformationByHandle"):
            self.assertNotIn(forbidden, self.v1_only)

    def test_existing_open_requires_trusted_identity_and_exact_header(self):
        self.assertIn("open && (!request.has_expected_identity", self.cpp)
        self.assertIn("expected_identity_valid", self.cpp)
        self.assertIn("same_identity(candidate->identity, request.expected_identity)", self.cpp)
        self.assertIn("request.expected_identity.container_id", self.cpp)
        self.assertIn("load_header(candidate->file.get()", self.cpp)

    def test_refusal_model_stops_before_leaf_mutation(self):
        cases = [
            ({"request_valid": False}, "invalid_request"),
            ({"path_safe": False}, "unsafe_path"),
            ({"ancestors": [{"plain": False, "stable": True}]}, "reparse_refused"),
            ({"parent_private": False}, "private_directory_required"),
            ({"fixed_volume": False}, "unsafe_volume"),
            ({"hotplug": True}, "unsafe_volume"),
            ({"filesystem": "ReFS"}, "unsupported_filesystem"),
        ]
        for state, expected in cases:
            with self.subTest(expected=expected):
                status, trace = storage_decision(state)
                self.assertEqual(status, expected)
                self.assertNotIn("write_zero_container", trace)
                self.assertNotIn("write_header", trace)

    def test_validated_parent_reparse_race_refuses_before_leaf_create_or_genesis(self):
        status, trace = storage_decision({
            "mode": "create",
            "ancestors": [{"plain": True, "stable": True}],
            "parent_private": True,
            "parent_share_locked": True,
            "parent_stable_before_leaf": False,
        })
        self.assertEqual(status, "identity_mismatch")
        self.assertIn("hold_ancestor_deny_write_delete", trace)
        self.assertNotIn("open_fixed_leaf", trace)
        self.assertNotIn("write_zero_container", trace)
        self.assertNotIn("write_header", trace)

    def test_file_and_identity_tamper_model_fail_closed(self):
        cases = [
            ({"leaf_plain": False}, "reparse_refused"),
            ({"links": 2}, "link_count_refused"),
            ({"delete_pending": True}, "delete_pending"),
            ({"file_private": False}, "security_unavailable"),
            ({"trusted_identity": False}, "identity_mismatch"),
            ({"identity_matches": False}, "identity_mismatch"),
            ({"size_matches": False}, "size_mismatch"),
            ({"header_valid": False}, "container_corrupt_header"),
            ({"reopen_matches": False}, "reopen_identity_mismatch"),
            ({"final_stable": False}, "reopen_identity_mismatch"),
        ]
        for state, expected in cases:
            with self.subTest(expected=expected):
                status, _ = storage_decision(state)
                self.assertEqual(status, expected)

    def test_create_failure_preserves_partial_file_and_success_reopens_identity(self):
        status, trace = storage_decision({"mode": "create", "genesis_flush_readback": False})
        self.assertEqual(status, "genesis_incomplete")
        self.assertIn("write_zero_container", trace)
        self.assertNotIn("write_header", trace)
        status, trace = storage_decision({"mode": "create"})
        self.assertEqual(status, "ok_created")
        self.assertEqual(trace[-1], "reopen_fixed_leaf")
        self.assertIn("never removes or repairs a partially created container", self.hpp)

    def test_success_receipt_is_finite_redacted_and_lease_bound(self):
        receipt = re.search(r"struct StorageReceipt \{(.+?)\n\};", self.hpp, re.S).group(1)
        self.assertNotRegex(receipt, r"\b(path|sid|username|error_detail|message)\b")
        self.assertIn("identity_reopened", receipt)
        self.assertIn("handle_retained", receipt)
        self.assertIn("return valid() ? impl_->file.get() : INVALID_HANDLE_VALUE", self.cpp)

    def test_source_contract_documents_unresolved_windows_acceptance(self):
        prose = source(ROOT / "contracts" / "action-journal-storage" / "v0.1.0.md")
        self.assertIn("production ActionJournal availability flag remains false", prose)
        self.assertIn("Remote Windows compilation/static analysis", prose)
        self.assertIn("exact-target", prose)
        self.assertGreaterEqual(prose.count("https://learn.microsoft.com/"), 10)


if __name__ == "__main__":
    unittest.main()
