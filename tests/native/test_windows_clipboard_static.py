"""Static/reference checks for the inert direct Win32 clipboard boundary.

This suite does not compile or execute native code and never reads or writes
the operating-system clipboard.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native" / "windows_clipboard" / "windows_clipboard.cpp"
HEADER = ROOT / "native" / "windows_clipboard" / "windows_clipboard.hpp"
ANCHOR = ROOT / "native" / "windows_clipboard" / "trust_anchor.hpp"
CONTRACT = ROOT / "contracts" / "windows-clipboard" / "v0.1.0.json"
PROSE = ROOT / "contracts" / "windows-clipboard" / "v0.1.0.md"
CASES = (
    ROOT
    / "tests"
    / "native"
    / "fixtures"
    / "windows_clipboard"
    / "refusal-and-commit-cases.json"
)
MAX_SOURCE_BYTES = 256 * 1024
MAX_JSON_BYTES = 64 * 1024


def bounded_source(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source exceeds static-review bound")
    return raw.decode("utf-8", errors="strict")


def strict_json(path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("fixture exceeds static-review bound")

    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError(f"duplicate key: {key}")
            output[key] = value
        return output

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)


def assert_sorted_keys(test: unittest.TestCase, value):
    if isinstance(value, dict):
        test.assertEqual(list(value), sorted(value))
        for child in value.values():
            assert_sorted_keys(test, child)
    elif isinstance(value, list):
        for child in value:
            assert_sorted_keys(test, child)


def modeled_decision(state):
    if not state.get("gates", False):
        return "platform_unavailable", []
    trace = ["validate_capability"]
    if not state.get("capability", True) or not state.get("mac", True):
        return "capability_refused", trace
    trace.append("verify_interactive_identity")
    if not state.get("session", True):
        return "session_refused", trace

    operation = state["operation"]
    if operation == "query":
        journal_state = state.get("journal_state", "dispatched")
        trace.append(f"journal_lookup_{journal_state}")
        if not state.get("post_journal_identity", True):
            trace.append("revalidate_identity_after_journal_lookup")
            return "mutation_unknown", trace
        if journal_state == "applied":
            return "ok", trace
        trace.extend(["read_current_postcondition", "manual_no_replay"])
        return (
            "postcondition_present_manual"
            if state.get("content_matches", False)
            else "postcondition_absent_manual"
        ), trace

    trace.append("check_cancel_deadline")
    if state.get("cancelled", False):
        return "cancelled", trace
    if not state.get("deadline", True):
        return "deadline_exceeded", trace
    if operation == "write":
        trace.append("prepare_global_memory")
    trace.append("open_clipboard_bounded")
    if not state.get("open", True):
        return "deadline_exceeded", trace
    if not state.get("post_open_identity", True):
        trace.append("revalidate_identity_after_open")
        return "session_refused", trace
    if operation == "read":
        trace.extend(["read_unicode_snapshot", "publish_redacted_receipt"])
        return "ok", trace

    trace.append("compare_confirmed_sequence")
    if not state.get("sequence", True):
        return "capability_refused", trace
    trace.append("durable_dispatch")
    dispatch = state.get("dispatch", "dispatched")
    if dispatch == "already":
        return "already_dispatched", trace
    if dispatch != "dispatched":
        return "journal_unavailable", trace
    if not state.get("pre_mutation_identity", True):
        trace.extend(
            ["revalidate_identity_before_mutation", "durable_failed_before_mutation"]
        )
        return "session_refused", trace
    trace.append("empty_all_confirmed_formats")
    if not state.get("empty", True):
        trace.append("durable_mutation_attempt_failed")
        if not state.get("post_journal_identity", True):
            trace.append("revalidate_identity_after_journal_outcome")
            return "mutation_unknown", trace
        return "clipboard_busy", trace
    trace.append("set_unicode_text")
    if not state.get("set", True):
        trace.append("durable_failed_after_mutation")
        return "mutation_unknown", trace
    if not state.get("final_identity", True):
        trace.extend(["revalidate_identity_before_close", "durable_unknown_outcome"])
        return "mutation_unknown", trace
    trace.append("close_and_correlate_sequence")
    if not state.get("sequence_after", True):
        trace.append("durable_unknown_outcome")
        return "mutation_unknown", trace
    trace.append("durable_applied_outcome")
    if not state.get("outcome_saved", True):
        return "mutation_unknown", trace
    if not state.get("post_journal_identity", True):
        trace.append("revalidate_identity_after_journal_outcome")
        return "mutation_unknown", trace
    trace.append("publish_redacted_receipt")
    return "ok", trace


def modeled_mutation_attempt_state(state):
    if state.get("lookup_result", "success") != "success" or state.get(
        "corrupt", False
    ):
        return "may_have_been_attempted"
    journal_state = state.get("journal_state")
    if journal_state in {"dispatched", "mutation_prepared"}:
        return "may_have_been_attempted"
    if journal_state in {"applied", "mutation_attempt_failed", "unknown_after_mutation"}:
        return "attempted"
    if journal_state in {"not_dispatched", "failed_before_mutation"}:
        return "not_attempted"
    raise ValueError("unknown fixture journal state")


class WindowsClipboardStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = bounded_source(SOURCE)
        cls.hpp = bounded_source(HEADER)
        cls.anchor = bounded_source(ANCHOR)
        cls.contract = strict_json(CONTRACT)
        cls.prose = bounded_source(PROSE)
        cls.cases = strict_json(CASES)

    def test_source_is_inert_and_all_activation_gates_are_false(self):
        activation = self.contract["activation"]
        for key, value in activation.items():
            self.assertIs(value, False, key)
        for marker in (
            "kSupervisorIdentityAvailable = false",
            "kAuthenticatedCapabilityIssuerAvailable = false",
            "kCancellableClipboardIoAvailable = false",
            "kDurableJournalAvailable = false",
            "kTargetAcceptancePassed = false",
        ):
            self.assertIn(marker, self.anchor)
        self.assertNotIn(
            "windows_clipboard", bounded_source(ROOT / "native" / "CMakeLists.txt")
        )
        for path in (
            ROOT / "lae-host.mjs",
            ROOT / "host" / "tools" / "local" / "index.mjs",
            ROOT / "release" / "windows" / "RELEASE_MANIFEST.json",
        ):
            self.assertNotIn("windows_clipboard", bounded_source(path))

    def test_public_entries_refuse_before_request_pointer_or_os_access(self):
        execute = self.cpp[
            self.cpp.index("Result execute(const Request& request") :
            self.cpp.index("ReconciliationResult query_write_status(")
        ]
        gate = execute.index("!trust_anchor::activation_prerequisites_available()")
        for later in (
            "request.capability",
            "request.context",
            "execute_read(*request.capability",
            "execute_write(*request.capability",
            "*journal",
        ):
            self.assertLess(gate, execute.index(later), later)
        query = self.cpp[self.cpp.index("ReconciliationResult query_write_status(") :]
        gate = query.index("!trust_anchor::activation_prerequisites_available()")
        for later in (
            "capability->operation()",
            "*context",
            "journal->lookup_write",
            "open_clipboard_bounded",
        ):
            self.assertLess(gate, query.index(later), later)

    def test_capability_is_opaque_exactly_bound_and_has_no_issuer(self):
        capability = re.search(
            r"class BrokerClipboardCapability final \{(.+?)\n\};",
            self.hpp,
            re.S,
        ).group(1)
        private = capability.split("private:", 1)[1]
        for field in (
            "operation_", "session_id_", "interactive_session_id_",
            "issued_monotonic_ms_", "expires_monotonic_ms_", "operation_id_",
            "request_digest_", "content_digest_", "confirmation_digest_",
            "authority_nonce_", "mac_", "confirmed_sequence_number_",
            "replace_all_formats_confirmed_",
        ):
            self.assertIn(field, private)
        for forbidden in (
            r"\bissue\s*\(", r"\bdeserialize\s*\(",
            r"\bset_[A-Za-z0-9_]*\s*\(", r"ForTest", r"getenv\s*\(",
            r"GetEnvironmentVariable",
        ):
            self.assertNotRegex(self.hpp + self.cpp + self.anchor, forbidden)
        self.assertTrue(self.contract["capability"]["caller_constructible"] is False)

        authority = re.search(
            r"class BrokerClipboardAuthority final \{(.+?)\n\};",
            self.hpp,
            re.S,
        ).group(1)
        authority_private = authority.split("private:", 1)[1]
        self.assertIn("BrokerClipboardAuthority() = default", authority_private)
        self.assertIn("mac_key_", authority_private)
        self.assertIn("authority_nonce_", authority_private)
        self.assertNotIn("mac_key()", authority)
        self.assertNotIn("authority_nonce()", authority)
        self.assertIn(
            "const BrokerClipboardAuthority* authority = nullptr", self.hpp
        )

    def test_windows_macro_containment_precedes_windows_header(self):
        windows = self.hpp.index("#include <windows.h>")
        self.assertLess(self.hpp.index("#define WIN32_LEAN_AND_MEAN"), windows)
        self.assertLess(self.hpp.index("#define NOMINMAX"), windows)
        self.assertIn("std::min<std::uint64_t>", self.cpp)
        self.assertIn("std::min<DWORD>", self.cpp)

    def test_mac_contract_binds_exact_payload_not_itself(self):
        self.assertEqual(
            self.contract["capability"]["bound_fields"],
            [
                "authority_nonce",
                "confirmation_digest",
                "confirmed_sequence_number",
                "content_digest",
                "expires_monotonic_ms",
                "interactive_session_id",
                "issued_monotonic_ms",
                "operation",
                "operation_id",
                "replace_all_formats_confirmed",
                "request_digest",
                "session_id",
            ],
        )
        self.assertNotIn("mac", self.contract["capability"]["bound_fields"])
        self.assertIn("not recursively included", self.prose)
        self.assertEqual(self.contract["capability"]["mac_algorithm"], "HMAC-SHA-256")
        self.assertFalse(self.contract["capability"]["issuer_key_exposed"])

        verify = self.cpp[
            self.cpp.index("bool BrokerClipboardAuthority::verify_capability") :
            self.cpp.index("bool BrokerClipboardCapability::structurally_valid")
        ]
        for marker in (
            "lae.windows-clipboard.capability-mac.v1\\0",
            "capability.authority_nonce_",
            "capability.confirmation_digest_",
            "capability.confirmed_sequence_number_",
            "capability.content_digest_",
            "capability.expires_monotonic_ms_",
            "capability.interactive_session_id_",
            "capability.issued_monotonic_ms_",
            "capability.operation_",
            "capability.operation_id_",
            "capability.replace_all_formats_confirmed_",
            "capability.request_digest_",
            "capability.session_id_",
            "hmac_sha256(mac_key_, frame, expected)",
            "digest_equal(expected, capability.mac_)",
            "digest_equal(authority_nonce_, capability.authority_nonce_)",
        ):
            self.assertIn(marker, verify)
        frame = verify[verify.index("std::string frame") :]
        ordered_fields = (
            "capability.authority_nonce_",
            "capability.confirmation_digest_",
            "capability.confirmed_sequence_number_",
            "capability.content_digest_",
            "capability.expires_monotonic_ms_",
            "capability.interactive_session_id_",
            "capability.issued_monotonic_ms_",
            "capability.operation_",
            "capability.operation_id_",
            "capability.replace_all_formats_confirmed_",
            "capability.request_digest_",
            "capability.session_id_",
        )
        positions = [frame.index(field) for field in ordered_fields]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("memcmp", verify)
        hmac = self.cpp[
            self.cpp.index("bool hmac_sha256") : self.cpp.index("void append_u32")
        ]
        self.assertIn("BCRYPT_ALG_HANDLE_HMAC_FLAG", hmac)
        self.assertIn("SecureZeroMemory(object.data()", hmac)
        validate = self.cpp[
            self.cpp.index("Status validate_common") :
            self.cpp.index("Result execute_read")
        ]
        self.assertLess(
            validate.index("context.authority->verify_capability(capability)"),
            validate.index("GetTickCount64()"),
        )

    def test_cng_hash_objects_are_destroyed_before_backing_buffers_are_wiped(self):
        sha = self.cpp[
            self.cpp.index("bool sha256") : self.cpp.index("bool hmac_sha256")
        ]
        hmac = self.cpp[
            self.cpp.index("bool hmac_sha256") : self.cpp.index("void append_u32")
        ]
        for implementation in (sha, hmac):
            destroy = implementation.index("BCryptDestroyHash(hash)")
            wipe = implementation.index("SecureZeroMemory(object.data()")
            self.assertLess(destroy, wipe)
            self.assertIn("cleanup_fail_stop()", implementation[destroy:wipe])
            self.assertIn("hash = nullptr", implementation[destroy:wipe])

    def test_exact_interactive_console_desktop_identity_is_checked(self):
        identity = self.cpp[
            self.cpp.index("bool session_identity") :
            self.cpp.index("Status stop_status")
        ]
        for token in (
            "ProcessIdToSessionId", "WTSGetActiveConsoleSessionId",
            "OpenProcessToken", "TokenSessionId", "GetProcessWindowStation",
            "GetThreadDesktop", "OpenInputDesktop", "OpenWindowStationW",
            'L"WinSta0"', 'L"Default"',
        ):
            self.assertIn(token, identity)
        for token in (
            "struct InteractiveIdentityLease",
            "UniqueWindowStation retained_window_station",
            "UniqueDesktop retained_input_desktop",
            "UserObjectIdentity window_station_identity",
            "UserObjectIdentity input_desktop_identity",
            "acquire_interactive_identity",
            "revalidate_interactive_identity",
            "desktop_is_input(identity.retained_input_desktop.get())",
            "identity_equal(identity.input_desktop_identity",
            "identity_equal(identity.window_station_identity",
        ):
            self.assertIn(token, identity)
        for token in (
            "GetUserObjectSecurity",
            "OWNER_SECURITY_INFORMATION",
            "GROUP_SECURITY_INFORMATION",
            "DACL_SECURITY_INFORMATION",
            "IsValidSecurityDescriptor",
            "SE_SELF_RELATIVE",
            "UOI_NAME",
            "UOI_TYPE",
            "UOI_FLAGS",
            "UOI_IO",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("kMaximumSecurityDescriptorBytes = 64 * 1024", self.cpp)
        self.assertIn("DESKTOP_READOBJECTS | READ_CONTROL", identity)
        self.assertIn("WINSTA_READATTRIBUTES | READ_CONTROL", identity)

    def test_desktop_and_stop_are_revalidated_around_open(self):
        open_body = self.cpp[
            self.cpp.index("Status open_clipboard_bounded") :
            self.cpp.index("bool valid_utf16")
        ]
        before = open_body.index("revalidate_interactive_identity(identity)")
        opened = open_body.index("OpenClipboard(owner)")
        after = open_body.index(
            "revalidate_interactive_identity(identity)", before + 1
        )
        acquired_stop = open_body.index("stop_status(context, deadline)", opened)
        self.assertLess(before, opened)
        self.assertLess(opened, after)
        self.assertLess(after, acquired_stop)
        self.assertIn("lease.close_or_fail_stop()", open_body[opened:])

    def test_desktop_identity_is_revalidated_at_mutation_close_and_publication(self):
        write = self.cpp[
            self.cpp.index("Result execute_write") :
            self.cpp.index("}  // namespace\n\nBrokerClipboardAuthority::~")
        ]
        dispatch = write.index("journal.dispatch_write")
        pre_dispatch_identity = write.index(
            "revalidate_interactive_identity(identity)", dispatch
        )
        prepared = write.index("journal.record_mutation_prepared", dispatch)
        post_prepare_identity = write.index(
            "revalidate_interactive_identity(identity)", prepared
        )
        empty = write.index("EmptyClipboard()", dispatch)
        attempted = write.index(
            "MutationAttemptState::kAttempted", empty
        )
        after_empty = write.index("revalidate_interactive_identity(identity)", empty)
        set_data = write.index("SetClipboardData(CF_UNICODETEXT", empty)
        before_close = write.index("identity_before_close", set_data)
        close = write.index("clipboard.close_or_fail_stop()", before_close)
        after_close = write.index("identity_after_close", close)
        self.assertLess(dispatch, pre_dispatch_identity)
        self.assertLess(pre_dispatch_identity, prepared)
        self.assertLess(prepared, post_prepare_identity)
        self.assertLess(post_prepare_identity, empty)
        self.assertLess(empty, attempted)
        self.assertLess(empty, after_empty)
        self.assertLess(after_empty, set_data)
        self.assertLess(set_data, before_close)
        self.assertLess(before_close, close)
        self.assertLess(close, after_close)
        pre_mutation_tail = write[pre_dispatch_identity:empty]
        self.assertIn("JournalOutcome::kFailedBeforeMutation", pre_mutation_tail)
        self.assertIn("receipt.journal_mutation_prepared_durable", pre_mutation_tail)

        read = self.cpp[
            self.cpp.index("Result execute_read") :
            self.cpp.index("Result journaled_unknown")
        ]
        first_revalidation = read.index("revalidate_interactive_identity(identity)")
        read_close = read.index("clipboard.close_or_fail_stop()", first_revalidation)
        self.assertLess(first_revalidation, read_close)
        self.assertGreaterEqual(read.count("revalidate_interactive_identity(identity)"), 3)
        query = self.cpp[self.cpp.index("ReconciliationResult query_write_status(") :]
        self.assertGreaterEqual(query.count("revalidate_interactive_identity(identity)"), 3)
        lookup = query.index("journal->lookup_write")
        after_lookup = query.index("revalidate_interactive_identity(identity)", lookup)
        lookup_shape = query.index("const bool lookup_shape", lookup)
        self.assertLess(lookup, after_lookup)
        self.assertLess(after_lookup, lookup_shape)

    def test_every_blocking_journal_result_is_followed_by_identity_revalidation(self):
        write = self.cpp[
            self.cpp.index("Result execute_write") :
            self.cpp.index("}  // namespace\n\nBrokerClipboardAuthority::~")
        ]
        for call in (
            "journal.dispatch_write",
            "journal.record_mutation_prepared",
        ):
            position = write.index(call)
            revalidated = write.index(
                "revalidate_interactive_identity(identity)", position
            )
            self.assertLess(revalidated, write.index("return ", position))
        outcome_positions = [
            match.start()
            for match in re.finditer(r"journal\.record_write_outcome", write)
        ]
        self.assertEqual(len(outcome_positions), 6)
        for position in outcome_positions:
            revalidated = write.index(
                "revalidate_interactive_identity(identity)", position
            )
            self.assertLess(revalidated, write.index("return ", position))

        query = self.cpp[self.cpp.index("ReconciliationResult query_write_status(") :]
        lookup = query.index("journal->lookup_write")
        post_lookup = query.index("revalidate_interactive_identity(identity)", lookup)
        self.assertLess(post_lookup, query.index("if (!lookup_ok)", lookup))
        for terminal in (
            "output.status = Status::kOk",
            "output.status = Status::kPostconditionAbsentManual",
        ):
            position = query.index(terminal)
            preceding = query.rfind(
                "revalidate_interactive_identity(identity)", lookup, position
            )
            self.assertGreater(preceding, lookup)

    def test_open_clipboard_retry_is_bounded_cancelled_and_backed_off(self):
        open_body = self.cpp[
            self.cpp.index("Status open_clipboard_bounded") :
            self.cpp.index("bool valid_utf16")
        ]
        self.assertIn("stop_status(context, deadline)", open_body)
        self.assertIn("OpenClipboard(owner)", open_body)
        self.assertIn("WaitForSingleObject(context.cancellation_event, wait)", open_body)
        self.assertIn("std::min<DWORD>(delay * 2, kMaximumOpenRetryMs)", open_body)
        self.assertIn("if (now >= deadline) return Status::kDeadlineExceeded", open_body)
        self.assertNotIn("if (now >= deadline) return Status::kClipboardBusy", open_body)
        self.assertEqual(self.contract["limits"]["deadline_ms"], 5000)
        self.assertEqual(self.contract["limits"]["open_retry_maximum_ms"], 80)

    def test_only_bounded_strict_cf_unicode_text_is_supported(self):
        self.assertNotIn("CF_TEXT", self.cpp)
        self.assertNotIn("CF_OEMTEXT", self.cpp)
        self.assertIn("CF_UNICODETEXT", self.cpp)
        for token in (
            "GlobalSize(global)", "bytes > kMaximumUtf16AllocationBytes",
            "GlobalLockLease locked(global)", "terminator < units", "valid_utf16",
            "MB_ERR_INVALID_CHARS", "WC_ERR_INVALID_CHARS",
            "input.find('\\0')", "kMaximumUtf8Bytes",
        ):
            self.assertIn(token, self.cpp)
        self.assertEqual(self.contract["limits"]["utf8_text_bytes"], 65536)
        self.assertEqual(
            self.contract["limits"]["utf16_allocation_bytes"], 131074
        )

    def test_write_rejects_size_nul_and_invalid_utf8_before_hashing(self):
        write = self.cpp[
            self.cpp.index("Result execute_write") :
            self.cpp.index("}  // namespace\n\nBrokerClipboardAuthority::~")
        ]
        validate = write.index("validate_utf8_input(input)")
        hash_input = write.index("sha256(input, content_digest)")
        convert = write.index("utf8_to_utf16(input, wide)")
        self.assertLess(validate, hash_input)
        self.assertLess(hash_input, convert)
        validator = self.cpp[
            self.cpp.index("Status validate_utf8_input") :
            self.cpp.index("bool utf8_to_utf16")
        ]
        for marker in (
            "input.size() > kMaximumUtf8Bytes",
            "input.find('\\0')",
            "MB_ERR_INVALID_CHARS",
            "MultiByteToWideChar",
        ):
            self.assertIn(marker, validator)

    def test_write_explicitly_confirms_clearing_all_formats_and_current_sequence(self):
        write = self.cpp[
            self.cpp.index("Result execute_write") :
            self.cpp.index("}  // namespace\n\nBrokerClipboardAuthority::~")
        ]
        confirmation = write.index("replace_all_formats_confirmed()")
        compare_sequence = write.index(
            "receipt.sequence_before != capability.confirmed_sequence_number()"
        )
        dispatch = write.index("journal.dispatch_write")
        empty = write.index("EmptyClipboard()")
        set_data = write.index("SetClipboardData(CF_UNICODETEXT")
        self.assertLess(confirmation, compare_sequence)
        self.assertLess(compare_sequence, dispatch)
        self.assertLess(dispatch, empty)
        prepared = write.index("journal.record_mutation_prepared")
        attempted = write.index("MutationAttemptState::kAttempted", empty)
        self.assertLess(dispatch, prepared)
        self.assertLess(prepared, empty)
        self.assertLess(empty, attempted)
        self.assertLess(empty, set_data)
        self.assertLess(empty, write.index("GetClipboardOwner() != owner.get()"))
        self.assertLess(write.index("GetClipboardOwner() != owner.get()"), set_data)
        self.assertIn("clears every existing format", self.prose)

    def test_noncancellable_commit_and_durable_outcome_order_are_explicit(self):
        write = self.cpp[
            self.cpp.index("Result execute_write") :
            self.cpp.index("}  // namespace\n\nBrokerClipboardAuthority::~")
        ]
        dispatch = write.index("journal.dispatch_write")
        after_dispatch = write[dispatch:]
        self.assertNotIn("stop_status", after_dispatch)
        self.assertLess(after_dispatch.index("journal.record_mutation_prepared"),
                        after_dispatch.index("EmptyClipboard()"))
        self.assertLess(after_dispatch.index("EmptyClipboard()"),
                        after_dispatch.index("SetClipboardData(CF_UNICODETEXT"))
        self.assertLess(after_dispatch.index("SetClipboardData(CF_UNICODETEXT"),
                        after_dispatch.index("memory.release_to_clipboard()"))
        self.assertLess(after_dispatch.index("memory.release_to_clipboard()"),
                        after_dispatch.rindex("journal.record_write_outcome"))
        self.assertIn("JournalOutcome::kFailedBeforeMutation", after_dispatch)
        self.assertIn("JournalOutcome::kMutationAttemptFailed", after_dispatch)
        self.assertIn("JournalOutcome::kFailedAfterMutation", after_dispatch)
        self.assertIn("JournalOutcome::kUnknownAfterMutation", after_dispatch)
        self.assertIn("JournalOutcome::kApplied", after_dispatch)

    def test_global_memory_lock_unlock_free_and_transfer_are_closed(self):
        for token in (
            "GMEM_MOVEABLE | GMEM_ZEROINIT",
            "GlobalLockLease locked(memory.get())", "locked.unlock()",
            "SecureZeroMemory(wide.data()", "SecureZeroMemory(sensitive, bytes_)",
            "GlobalFree(value_)", "release_to_clipboard()",
            "The system owns and frees it after success",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("if (value_) DestroyWindow(value_)", self.cpp)
        self.assertIn("if (open_ && !close()) cleanup_fail_stop()", self.cpp)
        self.assertIn("if (value_ && !wipe_and_free()) cleanup_fail_stop()", self.cpp)
        self.assertIn("if (data_ && !unlock()) cleanup_fail_stop()", self.cpp)
        self.assertIn("[[noreturn]] void cleanup_fail_stop()", self.cpp)
        self.assertIn("std::terminate()", self.cpp)

    def test_close_failures_cannot_publish_or_continue(self):
        self.assertNotIn("(void)closed", self.cpp)
        self.assertNotRegex(self.cpp, r"const bool closed = clipboard\.close\(\)")
        self.assertGreaterEqual(self.cpp.count("clipboard.close_or_fail_stop()"), 8)
        close_class = self.cpp[
            self.cpp.index("class ClipboardLease") :
            self.cpp.index("bool nonzero")
        ]
        self.assertIn("if (!CloseClipboard()) return false", close_class)
        self.assertIn("if (!close()) cleanup_fail_stop()", close_class)

    def test_receipt_is_fixed_redacted_metadata_only(self):
        receipt = re.search(r"struct Receipt final \{(.+?)\n\};", self.hpp, re.S).group(1)
        for forbidden in (
            "text", "digest", "clipboard_content", "last_error", "window_title",
            "clipboard_owner", "session_id", "operation_id",
        ):
            self.assertNotIn(forbidden, receipt)
        self.assertIn("bool content_logged = false", receipt)
        self.assertFalse(self.contract["receipt"]["clipboard_content_allowed"])
        self.assertFalse(self.contract["receipt"]["content_digests_allowed"])
        self.assertFalse(self.contract["receipt"]["raw_win32_errors_allowed"])
        self.assertEqual(
            self.contract["receipt"]["mutation_attempt_state_values"],
            ["attempted", "may_have_been_attempted", "not_attempted"],
        )
        receipt_fields = self.contract["receipt"]["allowed_fields"]
        self.assertIn("mutation_attempt_state", receipt_fields)
        self.assertNotIn("mutation_attempted", receipt_fields)
        self.assertIn("MutationAttemptState mutation_attempt_state", receipt)
        self.assertNotIn("bool mutation_attempted", receipt)

    def test_lost_ack_query_never_writes_or_fabricates_completion(self):
        query = self.cpp[self.cpp.index("ReconciliationResult query_write_status(") :]
        self.assertNotIn("EmptyClipboard", query)
        self.assertNotIn("SetClipboardData", query)
        self.assertNotIn("dispatch_write", query)
        self.assertIn("JournalLookupState::kApplied", query)
        self.assertIn("JournalLookupState::kFailedBeforeMutation", query)
        self.assertIn("JournalLookupState::kMutationAttemptFailed", query)
        self.assertIn("JournalLookupState::kMutationPrepared", query)
        self.assertIn("JournalLookupState::kUnknownAfterMutation", query)
        self.assertIn("lookup.sequence_before == capability->confirmed_sequence_number()", query)
        self.assertIn("const bool lookup_shape", query)
        self.assertIn("Status::kPostconditionPresentManual", query)
        self.assertIn("Status::kPostconditionAbsentManual", query)
        self.assertFalse(self.contract["reconciliation"]["repeats_write"])
        self.assertEqual(
            self.contract["reconciliation"]["dispatched_only_exact_postcondition"],
            "manual_resolution_no_replay",
        )
        self.assertGreaterEqual(query.count("const Status final_stop = stop_status"), 3)
        dispatched_tail = query[query.index("ClipboardLease clipboard") :]
        self.assertLess(
            dispatched_tail.index("SecureZeroMemory(observed_digest.data()"),
            dispatched_tail.index("const Status final_stop = stop_status"),
        )
        mapping = self.cpp[
            self.cpp.index("MutationAttemptState mutation_state_for_lookup") :
            self.cpp.index("Result result(")
        ]
        self.assertIn("JournalLookupState::kDispatched", mapping)
        self.assertIn("JournalLookupState::kMutationPrepared", mapping)
        self.assertIn("MutationAttemptState::kMayHaveBeenAttempted", mapping)
        self.assertIn("JournalLookupState::kUnknownAfterMutation", mapping)
        self.assertIn("MutationAttemptState::kAttempted", mapping)

    def test_restart_attempt_state_is_tri_state_and_never_false_for_dispatch(self):
        attempt_cases = self.cases["mutation_attempt_cases"]
        self.assertGreaterEqual(len(attempt_cases), 9)
        for case in attempt_cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    modeled_mutation_attempt_state(case["state"]),
                    case["expected_mutation_attempt_state"],
                )
        by_name = {case["name"]: case for case in attempt_cases}
        self.assertEqual(
            by_name["crash_after_dispatch_before_marker"][
                "expected_mutation_attempt_state"
            ],
            "may_have_been_attempted",
        )
        self.assertEqual(
            by_name["crash_inside_empty_after_marker"][
                "expected_mutation_attempt_state"
            ],
            "may_have_been_attempted",
        )
        self.assertEqual(
            by_name["journal_lookup_failure_after_operation_id"][
                "expected_mutation_attempt_state"
            ],
            "may_have_been_attempted",
        )
        self.assertEqual(
            by_name["corrupt_failed_before_record"][
                "expected_mutation_attempt_state"
            ],
            "may_have_been_attempted",
        )
        contract_cases = {
            "corrupt_or_unknown_lookup": "corrupt_failed_before_record",
            "durable_applied": "applied_terminal",
            "durable_dispatch_only": "crash_after_dispatch_before_marker",
            "durable_failed_before_mutation": "failed_before_mutation_terminal",
            "durable_mutation_attempt_failed": "empty_returned_failure",
            "durable_mutation_prepared": "crash_inside_empty_after_marker",
            "durable_start_not_dispatched": "durable_start_only_not_dispatched",
            "durable_unknown_after_mutation": "unknown_after_mutation_terminal",
            "lookup_failure_or_unavailable": "journal_lookup_failure_after_operation_id",
        }
        fixture_table = {
            contract_key: by_name[fixture_name]["expected_mutation_attempt_state"]
            for contract_key, fixture_name in contract_cases.items()
        }
        self.assertEqual(
            fixture_table,
            self.contract["receipt"]["mutation_attempt_state_table"],
        )

        query = self.cpp[self.cpp.index("ReconciliationResult query_write_status(") :]
        conservative = query.index(
            "MutationAttemptState::kMayHaveBeenAttempted"
        )
        validate = query.index("validate_common(")
        lookup = query.index("journal->lookup_write")
        shape = query.index("const bool lookup_shape", lookup)
        exact_mapping = query.index("mutation_state_for_lookup", shape)
        self.assertLess(conservative, validate)
        self.assertLess(validate, lookup)
        self.assertLess(shape, exact_mapping)
        lookup_failure = query[query.index("if (!lookup_ok)") : shape]
        self.assertNotIn("kNotAttempted", lookup_failure)

    def test_not_attempted_after_dispatch_requires_exact_durable_readback(self):
        write = self.cpp[
            self.cpp.index("Result execute_write") :
            self.cpp.index("}  // namespace\n\nBrokerClipboardAuthority::~")
        ]
        self.assertEqual(
            write.count("MutationAttemptState::kNotAttempted"), 2
        )
        self.assertEqual(
            write.count("confirm_durable_failed_before_mutation("), 2
        )
        for match in re.finditer(
            r"receipt\.mutation_attempt_state\s*=\s*"
            r"MutationAttemptState::kNotAttempted",
            write,
        ):
            guard = write.rfind("if (!exact_failed_before)", 0, match.start())
            record = write.rfind("JournalOutcome::kFailedBeforeMutation", 0, match.start())
            self.assertGreater(guard, record)
        verifier = self.cpp[
            self.cpp.index("bool confirm_durable_failed_before_mutation") :
            self.cpp.index("Result execute_write")
        ]
        for marker in (
            "journal.lookup_write",
            "capability.operation_id()",
            "capability.request_digest()",
            "JournalLookupState::kFailedBeforeMutation",
            "verification.sequence_before == sequence_before",
            "verification.sequence_after == 0",
            "verification.mutation_prepared_durable == mutation_prepared_durable",
            "revalidate_interactive_identity(identity)",
        ):
            self.assertIn(marker, verifier)

    def test_no_process_shell_network_or_generic_clipboard_surface(self):
        for forbidden in (
            r"\bCreateProcess", r"\bShellExecute", r"\bWinExec",
            r"\b_popen", r"\bsystem\s*\(", r"\bsocket\s*\(",
            r"\bconnect\s*\(", r"powershell", r"clip\.exe",
            r"AddClipboardFormatListener", r"RegisterClipboardFormat",
        ):
            self.assertNotRegex((self.cpp + self.hpp).lower(), forbidden.lower())

    def test_reference_cases_cover_refusal_commit_and_no_replay(self):
        assert_sorted_keys(self, self.cases)
        for case in self.cases["cases"]:
            with self.subTest(case=case["name"]):
                status, trace = modeled_decision(case["state"])
                self.assertEqual(status, case["expected_status"])
                self.assertEqual(trace, case["expected_trace"])
        by_name = {case["name"]: case for case in self.cases["cases"]}
        for name in (
            "all_production_gates_false",
            "forged_or_tampered_capability_mac",
            "valid_mac_replayed_after_bound_field_tamper",
            "input_desktop_switched_after_open",
            "desktop_object_replaced_before_empty",
            "desktop_object_replaced_before_final_close",
            "replayed_write_never_mutates",
            "dispatch_failure_never_mutates",
            "set_failure_after_clear_is_never_replayed",
            "outcome_persistence_failure",
            "dispatched_only_matching_content_remains_manual",
            "desktop_switch_during_journal_lookup",
            "desktop_switch_after_applied_outcome_persistence",
        ):
            self.assertIn(name, by_name)

    def test_contract_and_fixture_are_strict_canonical_metadata(self):
        assert_sorted_keys(self, self.contract)
        assert_sorted_keys(self, self.cases)
        self.assertEqual(self.contract["status"], "SOURCE_ONLY_NOT_READY")
        self.assertTrue(self.cases["fixture_only"])
        source_statuses = set(re.findall(r'return "([a-z_]+)";', self.cpp[
            self.cpp.index("const char* status_name") :
            self.cpp.index("Result result(")
        ]))
        self.assertEqual(source_statuses, set(self.contract["status_codes"]))


if __name__ == "__main__":
    unittest.main()
