"""Source/protocol checks for the dormant Win32 descriptor-WAL bootstrap.

This suite does not compile or execute Windows code and creates no processes.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native" / "action_journal_storage" / "windows_storage.cpp"
HPP = ROOT / "native" / "action_journal_storage" / "windows_storage.hpp"
DESIGN = ROOT / "native" / "action_journal_storage" / "DESCRIPTOR_WAL_BOOTSTRAP.md"
HOST = ROOT / "host" / "agent" / "action-journal.mjs"
MAX_BYTES = 32 * 1024 * 1024
WAL_HEADER = b'{"format":"lae-action-journal-wal","version":2}\n'


def bounded_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError(f"oversized source: {path}")
    return raw.decode("utf-8", errors="strict")


def modeled_reopen(data: bytes, *, identity_matches: bool = True) -> str:
    if not identity_matches:
        return "identity_mismatch"
    if len(data) > MAX_BYTES:
        return "size_mismatch"
    prefix = data[: len(WAL_HEADER)]
    if prefix != WAL_HEADER[: len(prefix)]:
        return "container_corrupt_header"
    return "ok_opened"


def modeled_handoff(*, allocation_ok=True, duplicate_ok=True, flag_ok=True):
    """Fault-order model: a duplicate is always born inside its RAII owner."""
    state = {"live": 0, "closed": 0, "transferred": 0, "one_shot": False}
    if not allocation_ok:
        return "internal", state
    if not duplicate_ok:
        return "io_failed", state
    state["live"] = 1
    if not flag_ok:
        state["live"] = 0
        state["closed"] = 1
        return "io_failed", state
    state["live"] = 0
    state["transferred"] = 1
    state["one_shot"] = True
    return "ok_opened", state


class WindowsDescriptorJournalBootstrapStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = bounded_text(CPP)
        cls.hpp = bounded_text(HPP)
        cls.design = bounded_text(DESIGN)
        cls.host = bounded_text(HOST)

    def test_native_and_host_wal_constants_are_exactly_aligned(self):
        literal = re.search(
            r'constexpr char kDescriptorWalHeader\[\] =\s*"([^"]*(?:\\"[^"]*)*)";',
            self.cpp,
        )
        self.assertIsNotNone(literal)
        native = bytes(literal.group(1), "utf-8").decode("unicode_escape").encode()
        host = re.search(r"const DESCRIPTOR_WAL_HEADER = '([^']+)';", self.host)
        self.assertIsNotNone(host)
        host_bytes = bytes(host.group(1), "utf-8").decode("unicode_escape").encode()
        self.assertEqual(native, WAL_HEADER)
        self.assertEqual(host_bytes, WAL_HEADER)
        self.assertEqual(len(WAL_HEADER), 48)
        self.assertIn("kDescriptorWalMaxBytes = 32ull * 1024ull * 1024ull", self.hpp)
        self.assertIn("const MAX_DESCRIPTOR_WAL_BYTES = 32 * 1024 * 1024", self.host)

    def test_create_is_secure_exclusive_bounded_and_durable_before_success(self):
        body = self.cpp[self.cpp.index("StorageStatus acquire_descriptor_wal(") :]
        for token in (
            "canonical_directory", "acquire_directories", "filesystem_policy",
            "build_private_security", "directories_stable", "kDescriptorWalLeafName",
            "GENERIC_READ | GENERIC_WRITE | READ_CONTROL", "kFileShare",
            "&security.attributes", "CREATE_NEW : OPEN_EXISTING",
            "FILE_FLAG_OPEN_REPARSE_POINT", "FILE_FLAG_WRITE_THROUGH",
            "descriptor_wal_shape", "private_security", "get_identity",
            "validate_volume", "write_exact", "FlushFileBuffers", "read_exact",
        ):
            self.assertIn(token, body)
        self.assertLess(body.index("write_exact"), body.index("FlushFileBuffers"))
        self.assertLess(body.index("FlushFileBuffers"), body.index("read_exact"))
        self.assertIn("constexpr DWORD kFileShare = FILE_SHARE_READ", self.cpp)
        self.assertNotRegex(self.cpp, r"kFileShare\s*=\s*[^;]*FILE_SHARE_WRITE")
        for forbidden in ("DeleteFileW(", "MoveFile", "ReplaceFile", "SetFileInformationByHandle"):
            self.assertNotIn(forbidden, body)

    def test_reopen_requires_external_identity_before_any_header_acceptance(self):
        body = self.cpp[self.cpp.index("StorageStatus acquire_descriptor_wal(") :]
        request = body.index("open && (!request.has_expected_identity")
        open_leaf = body.index("HANDLE raw = CreateFileW")
        identity = body.index("open && !same_identity(candidate->identity")
        prefix = body.index("validate_descriptor_wal_prefix", identity)
        self.assertLess(request, open_leaf)
        self.assertLess(identity, prefix)
        self.assertEqual(modeled_reopen(WAL_HEADER, identity_matches=False),
                         "identity_mismatch")

    def test_only_empty_exact_header_prefix_or_full_header_leading_data_is_eligible(self):
        for length in range(len(WAL_HEADER) + 1):
            with self.subTest(length=length):
                self.assertEqual(modeled_reopen(WAL_HEADER[:length]), "ok_opened")
        self.assertEqual(modeled_reopen(WAL_HEADER + b"@frame"), "ok_opened")
        for offset in range(len(WAL_HEADER)):
            changed = bytearray(WAL_HEADER)
            changed[offset] ^= 1
            with self.subTest(offset=offset):
                self.assertEqual(modeled_reopen(bytes(changed)),
                                 "container_corrupt_header")
        self.assertEqual(modeled_reopen(b"x" * (MAX_BYTES + 1)), "size_mismatch")

    def test_recovery_is_conservative_and_delegated_without_native_mutation(self):
        validator = re.search(
            r"StorageStatus validate_descriptor_wal_prefix\(.+?\n\}", self.cpp, re.S
        ).group(0)
        for forbidden in ("write_exact", "WriteFile", "SetEndOfFile", "FlushFileBuffers"):
            self.assertNotIn(forbidden, validator)
        self.assertIn("exact strict prefix", self.hpp)
        self.assertIn("neither parses\nnor repairs frames", self.design)
        self.assertIn("Failure preserves the file", self.design)

    def test_handoff_is_single_same_access_inheritable_duplicate_after_rechecks(self):
        handoff = re.search(
            r"StorageStatus DescriptorWalLease::prepare_inheritable_handoff\(.+?\n\}",
            self.cpp,
            re.S,
        ).group(0)
        for token in (
            "impl_->handoff_prepared", "GetHandleInformation", "HANDLE_FLAG_INHERIT",
            "same_identity", "descriptor_wal_shape", "validate_descriptor_wal_prefix",
            "private_security", "directories_stable", "DuplicateHandle",
            "DUPLICATE_SAME_ACCESS", "impl_->handoff_prepared = true",
        ):
            self.assertIn(token, handoff)
        self.assertEqual(handoff.count("DuplicateHandle("), 1)
        self.assertIn("bInheritHandle = FALSE", self.cpp)
        self.assertIn("take_inheritable_handle", self.hpp)

    def test_duplicate_is_born_owned_and_allocation_failure_is_retry_safe(self):
        handoff = re.search(
            r"StorageStatus DescriptorWalLease::prepare_inheritable_handoff\(.+?\n\}",
            self.cpp,
            re.S,
        ).group(0)
        allocation = handoff.index("std::make_unique<DescriptorWalHandoff::Impl>()")
        duplicate = handoff.index("DuplicateHandle(")
        mark_one_shot = handoff.index("impl_->handoff_prepared = true")
        transfer = handoff.index("output.impl_ = std::move(candidate)")
        self.assertLess(allocation, duplicate)
        self.assertLess(duplicate, mark_one_shot)
        self.assertLess(mark_one_shot, transfer)
        call = handoff[duplicate : handoff.index("return StorageStatus::kIoFailed", duplicate)]
        self.assertIn("candidate->handle.put()", call)
        self.assertNotRegex(handoff[duplicate:transfer], r"\b(new|make_unique|resize|reserve)\b")
        self.assertIn("HANDLE* put() noexcept", self.cpp)

        status, state = modeled_handoff(allocation_ok=False)
        self.assertEqual((status, state["live"], state["closed"], state["one_shot"]),
                         ("internal", 0, 0, False))
        status, state = modeled_handoff(duplicate_ok=False)
        self.assertEqual((status, state["live"], state["closed"], state["one_shot"]),
                         ("io_failed", 0, 0, False))
        status, state = modeled_handoff(flag_ok=False)
        self.assertEqual((status, state["live"], state["closed"], state["one_shot"]),
                         ("io_failed", 0, 1, False))
        status, state = modeled_handoff()
        self.assertEqual((status, state["live"], state["transferred"], state["one_shot"]),
                         ("ok_opened", 0, 1, True))

    def test_receipt_cannot_disclose_handle_or_claim_bridge_availability(self):
        receipt = re.search(r"struct DescriptorWalReceipt \{(.+?)\n\};", self.hpp, re.S).group(1)
        self.assertNotRegex(receipt.lower(), r"\b(handle|path|sid|username|error|message)\b")
        self.assertIn("descriptor_bridge_available = false", receipt)
        self.assertIn("receipt.descriptor_bridge_available = false", self.cpp)

    def test_no_launcher_host_package_or_activation_wiring_exists(self):
        for token in (
            "not linked into the\nhost, supervisor, helper, product, installer, or package",
            "must not be\nserialized as `LAE_ACTION_JOURNAL_FD`",
            "No `CreateProcess*`", "activation switch is present here",
        ):
            self.assertIn(token, self.design)
        for path in (
            ROOT / "lae-host.mjs",
            ROOT / "host" / "agent" / "action-journal.mjs",
            ROOT / "host" / "agent" / "native-action-journal-client.mjs",
            ROOT / "native" / "windows_supervisor" / "authority.cpp",
            ROOT / "native" / "action_journal_helper" / "pipe_server.cpp",
            ROOT / "native" / "CMakeLists.txt",
        ):
            self.assertNotIn("acquire_descriptor_wal", bounded_text(path))
        self.assertNotRegex(self.cpp, r"\b(CreateProcess\w*|ShellExecute\w*)\s*\(")
        self.assertNotIn("LAE_ACTION_JOURNAL_FD", self.cpp)

    def test_design_truthfully_names_remaining_windows_evidence(self):
        for token in (
            "Win32 `HANDLE` numeric value is not a Microsoft C runtime file descriptor",
            "explicit handle allowlist", "convert\nit in the child",
            "Remote MSVC compile/static analysis", "exact-target Windows tests",
            "power-loss recovery", "not a physical-media or\npower-loss guarantee",
        ):
            self.assertIn(token, self.design)


if __name__ == "__main__":
    unittest.main()
