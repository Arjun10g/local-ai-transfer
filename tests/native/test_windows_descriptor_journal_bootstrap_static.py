"""Source/protocol checks for the dormant Win32 descriptor-WAL bootstrap.

This suite does not compile or execute Windows code and creates no processes.

Every property that is also expressed as a Python model below is additionally
asserted against the C++ source with a regex anchored to the relevant function
body, and the models themselves are parameterised from values parsed out of
that source. The models are documentation of the intended state machine; the
source-anchored assertions are the verification, so a real C++ regression
fails this suite instead of passing against a hand-written Python twin.
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
HOST_SUITE = ROOT / "tests" / "host" / "descriptor-action-journal.test.mjs"
MAX_BYTES = 32 * 1024 * 1024
WAL_HEADER = b'{"format":"lae-action-journal-wal","version":2}\n'

# Every check the acquisition performs on the retained leaf handle. The
# pre-duplication recheck set must equal this set, not a subset of it.
LEAF_CHECKS = (
    "GetHandleInformation",
    "HANDLE_FLAG_INHERIT",
    "get_identity",
    "same_identity",
    "descriptor_wal_shape",
    "private_security",
    "final_path",
    "equal_path",
    "validate_volume",
    "directories_stable",
)


def bounded_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError(f"oversized source: {path}")
    return raw.decode("utf-8", errors="strict")


def function_body(text: str, signature: str) -> str:
    """Text of one C++ definition, from its signature to its column-0 brace."""
    match = re.search(signature + r".*?\n\}", text, re.S)
    if match is None:
        raise AssertionError(f"no definition matched: {signature}")
    return match.group(0)


def code_only(text: str) -> str:
    """C++ text with line comments removed, for absence assertions."""
    return re.sub(r"//[^\n]*", "", text)


def call_arguments(text: str, name: str) -> list[str]:
    """Argument text of every `name(...)` call, paren-balanced."""
    calls = []
    for match in re.finditer(re.escape(name) + r"\(", text):
        index = match.end()
        depth = 1
        while index < len(text) and depth:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        if depth:
            raise AssertionError(f"unbalanced call: {name}")
        calls.append(text[match.end() : index - 1])
    return calls


def modeled_reopen(data: bytes, *, header: bytes, limit: int,
                   limit_inclusive: bool, identity_matches: bool = True) -> str:
    """Documentation model. Its parameters are parsed from the C++ source."""
    if not identity_matches:
        return "identity_mismatch"
    if len(data) > limit if limit_inclusive else len(data) >= limit:
        return "size_mismatch"
    prefix = data[: len(header)]
    if prefix != header[: len(prefix)]:
        return "container_corrupt_header"
    return "ok_opened"


def modeled_handoff(*, already_transferred=False, recheck_ok=True,
                    allocation_ok=True, duplicate_ok=True, flag_ok=True):
    """Documentation model of the fault order and the one-shot guard."""
    state = {"live": 0, "closed": 0, "transferred": 0, "one_shot": False,
             "output_disturbed": False}
    if already_transferred:
        state["one_shot"] = True
        return "handoff_already_transferred", state
    if not recheck_ok:
        return "identity_mismatch", state
    state["output_disturbed"] = True
    if not allocation_ok:
        return "internal", state
    if not duplicate_ok:
        return "io_failed", state
    state["live"] = 1
    if not flag_ok:
        state["live"] = 0
        state["closed"] = 1
        return "inheritance_control_failed", state
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
        cls.host_suite = bounded_text(HOST_SUITE)
        cls.acquire = function_body(
            cls.cpp, r"StorageStatus acquire_descriptor_wal\(const DescriptorWalRequest")
        cls.handoff = function_body(
            cls.cpp, r"StorageStatus DescriptorWalLease::prepare_inheritable_handoff\(")
        cls.recheck = function_body(
            cls.cpp, r"StorageStatus DescriptorWalLease::recheck\(\) const noexcept \{")
        cls.revalidate = function_body(
            cls.cpp, r"StorageStatus DescriptorWalLease::revalidate\(\) const noexcept \{")
        cls.shape = function_body(
            cls.cpp, r"StorageStatus descriptor_wal_shape\(HANDLE handle")
        cls.validator = function_body(
            cls.cpp, r"StorageStatus validate_descriptor_wal_prefix\(HANDLE handle")
        cls.discard = function_body(cls.cpp, r"class CreatedFileDiscard final \{")

    # --- header/format parity -------------------------------------------------

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
        # The Node suite keeps a third independent copy of the literal. Nothing
        # compared it against the C++, so a drift there would not be caught.
        host_suite = re.search(
            r"const WAL_HEADER = Buffer\.from\('([^']+)'\);", self.host_suite)
        self.assertIsNotNone(host_suite)
        self.assertEqual(
            bytes(host_suite.group(1), "utf-8").decode("unicode_escape").encode(),
            WAL_HEADER)
        self.assertIn("kDescriptorWalHeaderBytes =\n    sizeof(kDescriptorWalHeader) - 1",
                      self.cpp)

    # --- create path ----------------------------------------------------------

    def test_create_is_secure_exclusive_bounded_and_durable_before_success(self):
        for token in (
            "canonical_directory", "acquire_directories", "filesystem_policy",
            "build_private_security", "directories_stable", "kDescriptorWalLeafName",
            "GENERIC_READ | GENERIC_WRITE | READ_CONTROL", "kFileShare",
            "&security.attributes", "CREATE_NEW : OPEN_EXISTING",
            "FILE_FLAG_OPEN_REPARSE_POINT", "FILE_FLAG_WRITE_THROUGH",
            "descriptor_wal_shape", "private_security", "get_identity",
            "validate_volume", "write_exact", "FlushFileBuffers", "read_exact",
        ):
            self.assertIn(token, self.acquire)
        self.assertLess(self.acquire.index("write_exact"),
                        self.acquire.index("FlushFileBuffers"))
        self.assertLess(self.acquire.index("FlushFileBuffers"),
                        self.acquire.index("read_exact"))
        self.assertIn("constexpr DWORD kFileShare = FILE_SHARE_READ", self.cpp)
        self.assertNotRegex(self.cpp, r"kFileShare\s*=\s*[^;]*FILE_SHARE_WRITE")
        for forbidden in ("DeleteFileW(", "MoveFile", "ReplaceFile",
                          "SetEndOfFile", "SetFileInformationByHandle"):
            self.assertNotIn(forbidden, self.acquire)

    def test_every_create_file_call_in_the_translation_unit_pins_anonymous_sqos(self):
        # Every path-derived open, not only this slice's two. The shared
        # ancestor walk runs first, so if path validation ever regressed an
        # ancestor, not the leaf, would be the impersonation vector.
        calls = call_arguments(self.cpp, "CreateFileW")
        self.assertEqual(len(calls), self.cpp.count("CreateFileW("))
        self.assertGreaterEqual(len(calls), 6)
        for index, call in enumerate(calls):
            with self.subTest(call=index):
                self.assertIn("SECURITY_SQOS_PRESENT", call)
                self.assertIn("SECURITY_ANONYMOUS", call)
        ancestors = function_body(self.cpp, r"bool acquire_directories\(const std::wstring&")
        self.assertIn("SECURITY_SQOS_PRESENT | SECURITY_ANONYMOUS", ancestors)

    def test_published_size_is_asserted_and_the_dead_store_is_gone(self):
        self.assertNotIn("candidate->size = kDescriptorWalHeaderBytes;", self.acquire)
        self.assertRegex(
            self.acquire,
            r"if \(create && candidate->size != kDescriptorWalHeaderBytes\)\s*\n"
            r"\s*return fail\(StorageStatus::kSizeMismatch\);",
        )
        publication = self.acquire.index("FlushFileBuffers")
        assertion = self.acquire.index("candidate->size != kDescriptorWalHeaderBytes")
        receipt = self.acquire.index("receipt.wal_bytes")
        self.assertLess(publication, assertion)
        self.assertLess(assertion, receipt)

    # --- failed create discards, reopen never does ----------------------------

    def test_failed_create_discards_the_unpublished_leaf_and_reopen_never_deletes(self):
        self.assertIn("FILE_DISPOSITION_INFO", self.discard)
        self.assertIn("disposition.DeleteFile = TRUE;", self.discard)
        self.assertIn("SetFileInformationByHandle(handle_, FileDispositionInfo",
                      self.discard)
        # DELETE access exists only when this call creates the leaf, and that is
        # the only unqualified DELETE right anywhere in the function.
        code = code_only(self.acquire)
        self.assertRegex(
            code,
            r"const DWORD leaf_access = GENERIC_READ \| GENERIC_WRITE \| READ_CONTROL \|\s*\n"
            r"\s*\(create \? DELETE : 0\);",
        )
        self.assertEqual(len(re.findall(r"(?<![_A-Za-z])DELETE\b", code)), 1)
        # Armed once, only under `create`; disarmed once, before the lease owns it.
        self.assertEqual(self.acquire.count("discard.arm("), 1)
        self.assertIn("if (create) discard.arm(candidate->file.get());", self.acquire)
        self.assertEqual(self.acquire.count("discard.disarm();"), 1)
        self.assertLess(self.acquire.index("discard.arm("),
                        self.acquire.index("discard.disarm();"))
        self.assertLess(self.acquire.index("discard.disarm();"),
                        self.acquire.index("lease.impl_ = std::move(candidate)"))
        # Destruction order: the guard is declared after the handle owner, so the
        # handle is still open when the disposition is set.
        self.assertLess(
            self.acquire.index("auto candidate = std::make_unique<DescriptorWalLease::Impl>()"),
            self.acquire.index("CreatedFileDiscard discard;"))
        self.assertLess(self.acquire.index("CreatedFileDiscard discard;"),
                        self.acquire.index("HANDLE raw = CreateFileW"))

    def test_reopen_branch_performs_no_write_truncation_or_deletion(self):
        start = self.acquire.index("    } else {")
        reopen_branch = self.acquire[start : self.acquire.index("\n    }\n", start)]
        self.assertIn("validate_descriptor_wal_prefix", reopen_branch)
        for forbidden in ("write_exact", "WriteFile", "SetEndOfFile", "FlushFileBuffers",
                          "DeleteFile", "DELETE", "discard"):
            self.assertNotIn(forbidden, code_only(reopen_branch))
        # Nothing outside the `create` branch can arm the discard.
        self.assertEqual(code_only(self.acquire).count("discard.arm("), 1)
        for forbidden in ("write_exact", "WriteFile", "SetEndOfFile", "FlushFileBuffers"):
            self.assertNotIn(forbidden, self.validator)
        self.assertIn("exact strict prefix", self.hpp)
        self.assertIn("neither parses nor repairs frames", self.design)
        self.assertIn("Failure on the reopen path preserves the file", self.design)
        self.assertIn("is not a WAL repair", self.design)

    # --- reopen acceptance, anchored to the C++ ------------------------------

    def test_reopen_requires_external_identity_before_any_header_acceptance(self):
        request = self.acquire.index("open && (!request.has_expected_identity")
        open_leaf = self.acquire.index("HANDLE raw = CreateFileW")
        identity = self.acquire.index("open && !same_identity(candidate->identity")
        prefix = self.acquire.index("validate_descriptor_wal_prefix", identity)
        self.assertLess(request, open_leaf)
        self.assertLess(identity, prefix)
        self.assertRegex(
            self.acquire,
            r"if \(open && !same_identity\(candidate->identity,\s*\n"
            r"\s*request\.expected_identity\)\)\s*\n"
            r"\s*return fail\(StorageStatus::kIdentityMismatch\);",
        )
        self.assertEqual(
            modeled_reopen(WAL_HEADER, header=WAL_HEADER, limit=MAX_BYTES,
                           limit_inclusive=True, identity_matches=False),
            "identity_mismatch")

    def test_acceptance_boundary_is_read_out_of_the_cpp_not_assumed(self):
        bound = re.search(
            r"return size (<=|<) kDescriptorWalMaxBytes \? StorageStatus::kOkOpened\s*\n"
            r"\s*: StorageStatus::kSizeMismatch;",
            self.shape)
        self.assertIsNotNone(bound, "descriptor_wal_shape bound changed shape")
        self.assertEqual(bound.group(1), "<=")
        self.assertRegex(
            self.shape,
            r"standard\.EndOfFile\.QuadPart < 0\)\s*\n\s*return StorageStatus::kIoFailed;")
        self.assertRegex(
            self.validator,
            r"std::min<std::uint64_t>\(size, kDescriptorWalHeaderBytes\)")
        self.assertRegex(
            self.validator,
            r"if \(count != 0 && !read_exact\(handle, 0, observed\.data\(\), count\)\)\s*\n"
            r"\s*return StorageStatus::kIoFailed;")
        self.assertRegex(
            self.validator,
            r"if \(!equal_bytes\(observed\.data\(\),\s*\n"
            r"\s*reinterpret_cast<const std::uint8_t\*>\(kDescriptorWalHeader\),\s*\n"
            r"\s*count\)\)\s*\n"
            r"\s*return StorageStatus::kContainerCorruptHeader;")
        self.assertTrue(self.validator.rstrip().endswith(
            "return StorageStatus::kOkOpened;\n}"))

    def test_only_empty_exact_header_prefix_or_full_header_leading_data_is_eligible(self):
        # The model is driven by values parsed from the C++, so editing the C++
        # bound, operator, or header literal changes what this test expects.
        literal = re.search(
            r'constexpr char kDescriptorWalHeader\[\] =\s*"([^"]*(?:\\"[^"]*)*)";',
            self.cpp)
        header = bytes(literal.group(1), "utf-8").decode("unicode_escape").encode()
        limit_text = re.search(
            r"kDescriptorWalMaxBytes = (\d+)ull \* (\d+)ull \* (\d+)ull", self.hpp)
        limit = 1
        for part in limit_text.groups():
            limit *= int(part)
        inclusive = re.search(
            r"return size (<=|<) kDescriptorWalMaxBytes", self.shape).group(1) == "<="
        self.assertEqual(header, WAL_HEADER)
        self.assertEqual(limit, MAX_BYTES)
        self.assertTrue(inclusive)
        model = lambda data: modeled_reopen(  # noqa: E731
            data, header=header, limit=limit, limit_inclusive=inclusive)
        for length in range(len(header) + 1):
            with self.subTest(length=length):
                self.assertEqual(model(header[:length]), "ok_opened")
        self.assertEqual(model(header + b"@frame"), "ok_opened")
        for offset in range(len(header)):
            changed = bytearray(header)
            changed[offset] ^= 1
            with self.subTest(offset=offset):
                self.assertEqual(model(bytes(changed)), "container_corrupt_header")
        self.assertEqual(model(b"x" * limit), "container_corrupt_header")
        self.assertEqual(model(b"x" * (limit + 1)), "size_mismatch")

    # --- handoff --------------------------------------------------------------

    def test_source_handle_must_be_verified_non_inheritable_before_duplication(self):
        self.assertRegex(
            self.acquire,
            r"if \(!GetHandleInformation\(candidate->file\.get\(\), &source_flags\)\)\s*\n"
            r"\s*return fail\(StorageStatus::kSecurityUnavailable\);\s*\n"
            r"\s*if \(\(source_flags & HANDLE_FLAG_INHERIT\) != 0\)\s*\n"
            r"\s*return fail\(StorageStatus::kSourceHandleInheritable\);")
        self.assertRegex(
            self.recheck,
            r"if \(!GetHandleInformation\(impl_->file\.get\(\), &source_flags\)\)\s*\n"
            r"\s*return StorageStatus::kSecurityUnavailable;\s*\n"
            r"\s*if \(\(source_flags & HANDLE_FLAG_INHERIT\) != 0\)\s*\n"
            r"\s*return StorageStatus::kSourceHandleInheritable;")
        self.assertLess(self.handoff.index("recheck()"),
                        self.handoff.index("DuplicateHandle("))

    def test_duplicate_is_not_born_inheritable_and_carries_no_delete(self):
        self.assertRegex(
            self.handoff,
            r"DuplicateHandle\(GetCurrentProcess\(\), impl_->file\.get\(\),\s*\n"
            r"\s*GetCurrentProcess\(\), candidate->handle\.put\(\),\s*\n"
            r"\s*GENERIC_READ \| GENERIC_WRITE, FALSE, 0\)")
        code = code_only(self.handoff)
        self.assertNotIn("DUPLICATE_SAME_ACCESS", code)
        self.assertNotIn("TRUE", code)
        self.assertNotIn("DELETE", code)
        self.assertNotIn("SetHandleInformation", code)
        self.assertRegex(
            self.handoff,
            r"if \(!GetHandleInformation\(candidate->handle\.get\(\), &duplicate_flags\) \|\|\s*\n"
            r"\s*\(duplicate_flags & HANDLE_FLAG_INHERIT\) != 0\)\s*\n"
            r"\s*return StorageStatus::kInheritanceControlFailed;")

    def test_inheritance_is_armed_and_revoked_explicitly_and_idempotently(self):
        arm = function_body(
            self.cpp, r"StorageStatus DescriptorWalHandoff::arm_inheritance\(\) noexcept \{")
        revoke = function_body(
            self.cpp, r"StorageStatus DescriptorWalHandoff::revoke_inheritance\(\) noexcept \{")
        armed = function_body(
            self.cpp, r"bool DescriptorWalHandoff::inheritance_armed\(\) const noexcept \{")
        self.assertRegex(
            arm,
            r"SetHandleInformation\(impl_->handle\.get\(\), HANDLE_FLAG_INHERIT,\s*\n"
            r"\s*HANDLE_FLAG_INHERIT\)")
        self.assertRegex(
            revoke,
            r"SetHandleInformation\(impl_->handle\.get\(\), HANDLE_FLAG_INHERIT, 0\)")
        for body in (arm, revoke):
            self.assertIn("StorageStatus::kInheritanceControlFailed", body)
            self.assertIn("StorageStatus::kInvalidRequest", body)
            self.assertIn("StorageStatus::kOkOpened", body)
            # Idempotent: neither refuses on the strength of a cached flag, and
            # both decide on the observed kernel flag after the call.
            self.assertIn("inheritance_armed()", body)
            self.assertNotIn("already", body)
        self.assertRegex(armed, r"\(flags & HANDLE_FLAG_INHERIT\) != 0")
        take = function_body(
            self.cpp, r"HANDLE DescriptorWalHandoff::take_handle\(\) noexcept \{")
        self.assertIn("if (!valid() || inheritance_armed()) return INVALID_HANDLE_VALUE;",
                      take)
        valid = function_body(
            self.cpp, r"bool DescriptorWalHandoff::valid\(\) const noexcept \{")
        self.assertNotIn("HANDLE_FLAG_INHERIT", valid)
        for declaration in ("StorageStatus arm_inheritance() noexcept;",
                            "StorageStatus revoke_inheritance() noexcept;",
                            "bool inheritance_armed() const noexcept;",
                            "HANDLE take_handle() noexcept;"):
            self.assertIn(declaration, self.hpp)
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", self.hpp)
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", self.design)
        self.assertIn("no_ambient_handle_inheritance", self.hpp)
        self.assertIn("no_ambient_handle_inheritance", self.design)

    def test_one_shot_guard_runs_before_the_only_handoff_can_be_destroyed(self):
        guard = self.handoff.index("if (impl_->handoff_transferred)")
        reset = self.handoff.index("output.reset();")
        recheck = self.handoff.index("recheck()")
        self.assertLess(guard, recheck)
        self.assertLess(recheck, reset)
        self.assertRegex(
            self.handoff,
            r"if \(impl_->handoff_transferred\)\s*\n"
            r"\s*return StorageStatus::kHandoffAlreadyTransferred;")
        self.assertEqual(self.handoff.count("output.reset();"), 1)
        status, state = modeled_handoff(already_transferred=True)
        self.assertEqual((status, state["output_disturbed"]),
                         ("handoff_already_transferred", False))
        status, state = modeled_handoff(recheck_ok=False)
        self.assertEqual((status, state["output_disturbed"]),
                         ("identity_mismatch", False))

    def test_duplicate_is_born_owned_and_allocation_failure_is_retry_safe(self):
        allocation = self.handoff.index("std::make_unique<DescriptorWalHandoff::Impl>()")
        duplicate = self.handoff.index("DuplicateHandle(")
        mark_one_shot = self.handoff.index("impl_->handoff_transferred = true")
        transfer = self.handoff.index("output.impl_ = std::move(candidate)")
        self.assertLess(allocation, duplicate)
        self.assertLess(duplicate, mark_one_shot)
        self.assertLess(mark_one_shot, transfer)
        call = self.handoff[
            duplicate : self.handoff.index("return StorageStatus::kIoFailed", duplicate)]
        self.assertIn("candidate->handle.put()", call)
        self.assertNotRegex(self.handoff[duplicate:transfer],
                            r"\b(new|make_unique|resize|reserve)\b")
        self.assertIn("HANDLE* put() noexcept", self.cpp)
        self.assertEqual(self.handoff.count("DuplicateHandle("), 1)

        status, state = modeled_handoff(allocation_ok=False)
        self.assertEqual((status, state["live"], state["closed"], state["one_shot"]),
                         ("internal", 0, 0, False))
        status, state = modeled_handoff(duplicate_ok=False)
        self.assertEqual((status, state["live"], state["closed"], state["one_shot"]),
                         ("io_failed", 0, 0, False))
        status, state = modeled_handoff(flag_ok=False)
        self.assertEqual((status, state["live"], state["closed"], state["one_shot"]),
                         ("inheritance_control_failed", 0, 1, False))
        status, state = modeled_handoff()
        self.assertEqual((status, state["live"], state["transferred"], state["one_shot"]),
                         ("ok_opened", 0, 1, True))

    # --- recheck set and typed statuses --------------------------------------

    def test_pre_duplication_recheck_set_equals_the_acquisition_check_set(self):
        for token in LEAF_CHECKS:
            self.assertIn(token, self.acquire)
            self.assertIn(token, self.recheck)
        self.assertIn("equal_path(impl_->path, observed_path)", self.recheck)
        self.assertIn("validate_volume(impl_->file.get(), impl_->volume.get()",
                      self.recheck)
        self.assertIn("validate_descriptor_wal_prefix(impl_->file.get(), size)",
                      self.recheck)
        self.assertIn("size != impl_->size", self.recheck)
        self.assertIn("recheck()", self.handoff)
        self.assertIn("recheck()", self.revalidate)

    def test_recheck_failures_report_distinct_typed_statuses(self):
        expected = {
            r"\(source_flags & HANDLE_FLAG_INHERIT\) != 0": "kSourceHandleInheritable",
            r"!same_identity\(identity, impl_->identity\)": "kIdentityMismatch",
            r"size != impl_->size": "kSizeMismatch",
            r"!private_security\(impl_->file\.get\(\), impl_->user\.sid\)":
                "kSecurityUnavailable",
            r"!equal_path\(impl_->path, observed_path\)": "kFinalPathMismatch",
            r"leaf_filesystem != impl_->filesystem": "kUnsupportedFilesystem",
            r"!directories_stable\(impl_->directories, impl_->user\.sid\)":
                "kPrivateDirectoryRequired",
        }
        for condition, status in expected.items():
            with self.subTest(status=status):
                self.assertRegex(
                    self.recheck,
                    condition + r"\)?\s*(?:\n\s*)?return StorageStatus::" + status + ";")
        self.assertEqual(len(set(expected.values())), len(expected))
        # Propagated, not flattened.
        self.assertEqual(self.recheck.count(
            "if (status != StorageStatus::kOkOpened) return status;"), 3)
        for name in ("kHandoffAlreadyTransferred", "kSourceHandleInheritable",
                     "kInheritanceControlFailed", "kFinalPathMismatch"):
            self.assertIn("  " + name + ",\n", self.hpp)
            self.assertIn("case StorageStatus::" + name + ": return", self.cpp)

    def test_transferred_lease_refuses_parent_side_io_on_the_shared_position(self):
        self.assertIn("bool handoff_transferred() const noexcept;", self.hpp)
        self.assertIn("StorageStatus revalidate() const noexcept;", self.hpp)
        self.assertRegex(
            self.revalidate,
            r"if \(impl_->handoff_transferred\)\s*\n"
            r"\s*return StorageStatus::kHandoffAlreadyTransferred;\s*\n"
            r"\s*return recheck\(\);")
        # The refusal precedes the only I/O-performing call in the lease.
        self.assertLess(self.revalidate.index("kHandoffAlreadyTransferred"),
                        self.revalidate.index("recheck()"))
        self.assertIn("read_exact", self.validator)
        for token in ("share one file object", "positional reads",
                      "child owns that position", "handoff_already_transferred"):
            self.assertIn(token, self.design)

    # --- receipts, dormancy, and truthfulness ---------------------------------

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
            "power-loss recovery", "not a\nphysical-media or power-loss guarantee",
            "the deepest directory only", "pre-existing merged behaviour",
            "not an authenticity anchor", "the\ncreate-failure discard",
        ):
            self.assertIn(token, self.design)


if __name__ == "__main__":
    unittest.main()
