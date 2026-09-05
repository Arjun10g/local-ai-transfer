"""Static/mock checks for the inert Windows read-only filesystem boundary.

Nothing in this file compiles or executes native Windows code. The small model
only makes refusal order, read-only effects, and lease publication explicit.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native" / "windows_readonly_fs" / "windows_readonly_fs.cpp"
HPP = ROOT / "native" / "windows_readonly_fs" / "windows_readonly_fs.hpp"
CONTRACT = ROOT / "contracts" / "windows-readonly-fs" / "v0.1.0.json"
PROSE = ROOT / "contracts" / "windows-readonly-fs" / "v0.1.0.md"
CASES = ROOT / "tests" / "native" / "fixtures" / "windows_readonly_fs" / "refusal-cases.json"
MAX_SOURCE_BYTES = 256 * 1024
MAX_JSON_BYTES = 64 * 1024


def source(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source exceeds static review bound")
    return raw.decode("utf-8", errors="strict")


def strict_json(path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("fixture exceeds static review bound")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)


def readonly_decision(state):
    """A finite model of the fail-before-result source boundary."""

    trace = []
    if not state.get("request_valid", True):
        return "invalid_request", trace
    if state.get("cancelled", False):
        return "cancelled", trace
    if not state.get("deadline", True):
        return "deadline_exceeded", trace
    if not state.get("grant_safe", True):
        return "unsafe_grant", trace
    trace.append("open_root_nofollow_deny_write_delete")
    if not state.get("root_identity", True):
        return "identity_mismatch", trace
    if not state.get("private_acl", True):
        return "security_unavailable", trace
    if not state.get("fixed_volume", True):
        return "unsafe_volume", trace
    if state.get("filesystem", "NTFS") != "NTFS":
        return "unsupported_filesystem", trace
    if not state.get("components_safe", True):
        return "unsafe_component", trace
    trace.append("nt_open_relative_one_component")
    if not state.get("relative_open", True):
        return "not_found", trace
    if not state.get("plain_target", True):
        return "reparse_refused", trace
    if state.get("links", 1) != 1:
        return "link_count_refused", trace
    trace.append("bounded_stat_list_or_read")
    if state.get("operation", "stat") == "read" and not state.get("utf8", True):
        return "binary_refused" if state.get("nul", False) else "utf8_required", trace
    trace.append("revalidate_all_retained_handles")
    if not state.get("final_identity", True):
        return "identity_mismatch", trace
    trace.append("publish_result_and_lease")
    return "ok", trace


class WindowsReadonlyFilesystemStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = source(CPP)
        cls.hpp = source(HPP)
        cls.contract = strict_json(CONTRACT)
        cls.prose = source(PROSE)
        cls.cases = strict_json(CASES)

    def test_contract_and_source_are_inert_and_production_stays_refused(self):
        for key in (
            "production_available", "native_target_registered", "helper_added",
            "transport_added", "node_integration_added", "package_added",
            "activation_permitted",
        ):
            self.assertIs(self.contract[key], False)
        cmake = source(ROOT / "native" / "CMakeLists.txt")
        self.assertNotIn("windows_readonly_fs", cmake)
        for production in (
            ROOT / "lae-host.mjs",
            ROOT / "host" / "tools" / "local" / "filesystem.mjs",
            ROOT / "host" / "tools" / "local" / "index.mjs",
            ROOT / "host" / "tools" / "local" / "platform-safety.mjs",
        ):
            self.assertNotIn("windows_readonly_fs", source(production))
        platform_safety = source(ROOT / "host" / "tools" / "local" / "platform-safety.mjs")
        self.assertIn("platform_path_safety_unavailable", platform_safety)
        self.assertIn("platform === 'win32'", platform_safety)

    def test_api_has_only_fixed_stat_list_read_and_no_mutation_or_process_surface(self):
        enum_body = re.search(r"enum class Operation[^\{]+\{(.+?)\};", self.hpp, re.S).group(1)
        self.assertEqual(set(re.findall(r"k([A-Za-z]+)", enum_body)), {"Stat", "List", "Read"})
        self.assertEqual(self.contract["operations"], ["stat", "list", "read"])
        for forbidden in (
            r"\bWriteFile\s*\(", r"\bDeleteFile(?:W|A)?\s*\(",
            r"\bMoveFile", r"\bReplaceFile", r"\bCreateDirectory",
            r"\bSetEndOfFile\s*\(", r"\bSetFileInformationByHandle\s*\(",
            r"\bCreateProcess", r"\bShellExecute", r"\bsocket\s*\(",
            r"\bconnect\s*\(", r"\bmain\s*\(",
        ):
            self.assertNotRegex(self.cpp, forbidden)

    def test_root_is_exact_canonical_local_and_bound_to_independent_identity(self):
        for token in (
            "GetFullPathNameW", "equal_path(canonical, supplied)",
            'supplied.rfind(L"\\\\\\\\", 0)', 'supplied.rfind(L"\\\\\\\\?\\\\", 0)',
            'supplied.rfind(L"\\\\\\\\.\\\\", 0)', "index != 1",
            "FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT",
            "same_identity(held.back().identity, request.expected_root_identity)",
            "GetFinalPathNameByHandleW", "equal_path(resolved, canonical)",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("expected_root_identity", self.hpp)
        self.assertIn("all_zero(request.expected_root_identity.file_id.data()", self.cpp)

    def test_all_descendants_use_handle_relative_one_component_nofollow_opens(self):
        for token in (
            "NtOpenFile", "attributes.RootDirectory", "OBJ_CASE_INSENSITIVE",
            "FILE_OPEN_REPARSE_POINT", "open_relative(held.back().handle.get()",
            "request.relative_components[index]", "open_relative(directory_handle, name",
        ):
            self.assertIn(token, self.cpp)
        self.assertEqual(self.cpp.count("CreateFileW("), 2)
        self.assertNotRegex(self.cpp, r"(?:PathCchCombine|PathCombine|std::filesystem|relative_components\s*\[.+?\]\s*\+)")
        self.assertIn('value == L\':\' || value == L\'/\' ||', self.cpp)
        self.assertIn("value == L'\\\\'", self.cpp)
        self.assertIn("component == L\"..\"", self.cpp)
        self.assertIn("reserved_component", self.cpp)
        self.assertIn("valid_utf16", self.cpp)

    def test_strict_share_and_retained_handle_chain_close_toctou_window(self):
        self.assertIn("constexpr DWORD kObjectShare = FILE_SHARE_READ;", self.cpp)
        self.assertNotRegex(
            self.cpp,
            r"kObjectShare\s*=\s*[^;]*(?:FILE_SHARE_WRITE|FILE_SHARE_DELETE)",
        )
        self.assertIn("std::vector<HeldObject> held", self.cpp)
        self.assertIn("held.push_back(HeldObject{std::move(child)", self.cpp)
        self.assertIn("status = validate_held(candidate->held", self.cpp)
        self.assertIn("response.receipt.handles_retained = true", self.cpp)
        self.assertIn("lease.impl_ = std::move(candidate)", self.cpp)

    def test_acl_volume_reparse_hardlink_and_attribute_refusals_are_handle_based(self):
        for token in (
            "OpenProcessToken", "TokenUser", "GetSecurityInfo",
            "OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION",
            "SE_DACL_PROTECTED", "SE_DACL_DEFAULTED", "dacl->AceCount == 1",
            "ace->Header.AceFlags == 0", "ace->Mask == kPrivateAccess",
            "GetDriveTypeW(drive_root) != DRIVE_FIXED",
            "IOCTL_STORAGE_GET_HOTPLUG_INFO", "hotplug.MediaRemovable",
            "hotplug.MediaHotplug", "hotplug.DeviceHotplug", 'L"NTFS"',
            "FILE_PERSISTENT_ACLS", "FileAttributeTagInfo", "FileStandardInfo",
            "FILE_ATTRIBUTE_REPARSE_POINT", "FILE_ATTRIBUTE_DEVICE",
            "FILE_ATTRIBUTE_OFFLINE", "FILE_ATTRIBUTE_SPARSE_FILE",
            "FILE_ATTRIBUTE_COMPRESSED", "FILE_ATTRIBUTE_ENCRYPTED",
            "FILE_ATTRIBUTE_RECALL_ON_OPEN", "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
            "FILE_ATTRIBUTE_VIRTUAL", "standard.NumberOfLinks != 1",
            "standard.DeletePending", "identity.volume_serial != expected_volume",
        ):
            self.assertIn(token, self.cpp)

    def test_list_is_immediate_bounded_and_opens_every_reported_child(self):
        for token in (
            "kMaxListEntries = 256", "kMaxListNameBytes = 65'536",
            "FileIdBothDirectoryRestartInfo", "FileIdBothDirectoryInfo",
            "kEnumerationBufferBytes = 65'536", "item->NextEntryOffset",
            "safe_component(name)", "open_relative(directory_handle, name",
            "inspect_object(child.get()", "response.entries.push_back",
        ):
            self.assertIn(token, self.hpp + self.cpp)
        self.assertLess(
            self.cpp.index("inspect_object(child.get()"),
            self.cpp.index("response.entries.push_back"),
        )

    def test_read_is_bounded_chunked_strict_utf8_and_binary_refusing(self):
        for token in (
            "kMaxReadBytes = 65'536", "kMaxReadableFileBytes = 16'777'216",
            "kReadChunkBytes = 16'384", "SetFilePointerEx", "ReadFile",
            "MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS",
            "if (byte == 0 || (byte < 0x20", "Status::kBinaryRefused",
            "Status::kUtf8Required", "target < available",
        ):
            self.assertIn(token, self.hpp + self.cpp)

    def test_deadline_cancel_and_failure_outputs_are_checked_throughout(self):
        self.assertGreaterEqual(self.cpp.count("checkpoint(request.execution)"), 8)
        self.assertIn("execution.is_cancelled(execution.opaque)", self.cpp)
        self.assertIn("now >= execution.deadline_monotonic_ms", self.cpp)
        failure_block = re.search(
            r"if \(status != Status::kOk\) \{\n      response = Response\{\};(.+?)return status;",
            self.cpp,
            re.S,
        )
        self.assertIsNotNone(failure_block)
        receipt = re.search(r"struct Receipt \{(.+?)\n\};", self.hpp, re.S).group(1).lower()
        for forbidden in ("path", "sid", "username", "text", "entry", "error", "message"):
            self.assertNotIn(forbidden, receipt)

    def test_status_contract_exact_and_finite(self):
        status_switch = re.search(
            r"const char\* status_name\(Status status\).*?switch \(status\) \{(.+?)\n  \}",
            self.cpp,
            re.S,
        ).group(1)
        source_statuses = set(re.findall(r'return "([a-z0-9_]+)";', status_switch))
        self.assertEqual(source_statuses, set(self.contract["status_codes"]))
        self.assertIn("kAbiVersion = 1", self.hpp)

    def test_static_refusal_vectors_match_and_never_publish_unsafe_results(self):
        self.assertEqual(self.cases["schema"], "lae.windows-readonly-fs.static-cases.v1")
        for case in self.cases["cases"]:
            with self.subTest(case=case["name"]):
                status, trace = readonly_decision(case["state"])
                self.assertEqual(status, case["status"])
                if status != "ok":
                    self.assertNotIn("publish_result_and_lease", trace)

    def test_contract_is_honest_about_native_and_target_residuals(self):
        for phrase in (
            "production capability remains unavailable",
            "no pathname fallback",
            "Windows compilation",
            "filter drivers",
            "Dell target acceptance",
        ):
            self.assertIn(phrase, self.prose)
        self.assertGreaterEqual(self.prose.count("https://learn.microsoft.com/"), 10)
        self.assertIn("independent source and security audit", self.contract["residual_gates"])


if __name__ == "__main__":
    unittest.main()
