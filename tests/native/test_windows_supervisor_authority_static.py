"""Static/reference checks for the inert Windows supervisor boundary.

No compiler, Windows API, process, package, network, credential, or native
runtime is invoked. The reference checks intentionally prove refusal ordering
and contract shape only.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native" / "windows_supervisor" / "authority.cpp"
HEADER = ROOT / "native" / "windows_supervisor" / "authority.hpp"
CONTRACT = ROOT / "contracts" / "windows-supervisor" / "v1.0.0.json"
MAX_SOURCE_BYTES = 256 * 1024


def strict_json(path: Path):
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("oversized contract")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate contract key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


class SupervisorAuthorityStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = SOURCE.read_bytes().decode("utf-8")
        cls.hpp = HEADER.read_bytes().decode("utf-8")
        cls.contract = strict_json(CONTRACT)

    def test_contract_is_closed_and_inert(self):
        self.assertEqual(
            set(self.contract),
            {
                "schema", "status", "production_available",
                "native_target_registered", "cmake_registered", "launcher_registered",
                "host_registered", "package_registered", "activation_permitted",
                "trust", "transport", "limits", "capability",
                "journal",
                "redacted_receipt_fields", "forbidden_receipt_fields",
                "unknown_outcome_policy",
            },
        )
        self.assertEqual(self.contract["schema"], "lae.windows-supervisor.contract.v1")
        self.assertEqual(self.contract["status"], "SOURCE_ONLY_NOT_READY")
        self.assertEqual(
            self.contract["journal"]["dispatch_status_values"],
            ["pre_dispatch_failure", "dispatched_unknown", "terminal_failure",
             "terminal_success", "persistence_failure"],
        )
        self.assertTrue(self.contract["journal"]["persistence_failure_is_unknown"])
        self.assertTrue(self.contract["journal"]["next_operation_requires_terminal_record"])
        for key in (
            "production_available", "native_target_registered", "cmake_registered",
            "launcher_registered", "host_registered", "package_registered",
            "activation_permitted",
        ):
            self.assertIs(self.contract[key], False)
        for key, value in self.contract["trust"].items():
            self.assertIs(value, False, key)
        self.assertEqual(self.contract["transport"]["kind"], "loopback_metadata_only")
        self.assertEqual(self.contract["capability"]["id_encoding"], "lower_hex_256")
        self.assertIs(self.contract["transport"]["bearer_issued"], False)
        self.assertIs(self.contract["capability"]["secrets_persisted"], False)
        self.assertIs(self.contract["capability"]["secrets_logged"], False)
        self.assertIs(self.contract["capability"]["secrets_in_process_arguments"], False)
        self.assertIs(self.contract["capability"]["secrets_in_environment"], False)
        self.assertEqual(
            set(self.contract["trust"]),
            {
                "release_manifest_sha256_pinned", "self_authenticode_pinned",
                "package_identity_pinned", "cancellable_io_proven",
                "durable_journal_authority", "nested_job_policy_proven",
                "broker_issued_identity_proven",
                "retained_executing_section_identity_proven",
            },
        )
        self.assertEqual(
            set(self.contract["transport"]),
            {"kind", "bearer_issued", "external_network"},
        )

    def test_source_is_unlinked_and_has_no_activation_surface(self):
        cmake = (ROOT / "native" / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertNotIn("windows_supervisor", cmake)
        for text in (self.cpp, self.hpp):
            self.assertNotIn("CreateProcessA", text)
            self.assertNotIn("CreateProcessW", text)
            self.assertNotIn("SearchPath", text)
            self.assertNotIn("ShellExecute", text)
            self.assertNotIn("GetEnvironmentVariable", text)
            self.assertNotIn("INFINITE", text)
            self.assertNotIn("TerminateThread", text)
            self.assertNotIn("CancelSynchronousIo", text)
        self.assertIn("#if defined(_WIN32)", self.cpp)
        self.assertIn("#if !defined(_WIN32)", self.hpp)
        self.assertIn("#define WIN32_LEAN_AND_MEAN", self.hpp)

    def test_gates_precede_windows_access(self):
        start = self.cpp.index("Status Authority::start")
        gate = self.cpp.index("if (!trust_gates_open())", start)
        start_end = self.cpp.index("Status Authority::shutdown", start)
        start_body = self.cpp[start:start_end]
        for api in (
            "GetCurrentProcessId", "OpenProcessToken", "CreateJobObjectW",
            "GetHandleInformation",
        ):
            self.assertNotIn(api, start_body[:gate - start], api)
        self.assertIn("return Status::kUnavailable", start_body[gate - start:gate - start + 140])
        self.assertIn("kReleaseManifestPinned = false", self.hpp)
        self.assertIn("kSelfAuthenticodePinned = false", self.hpp)
        self.assertIn("kDurableJournalAuthority = false", self.hpp)

    def test_trust_and_identity_contract_is_present(self):
        for token in (
            "FixedIdentity", "reparse_free", "file_index",
            "release_manifest_is_pinned", "self_authenticode_is_pinned",
            "package_identity_is_pinned", "WinVerifyTrust",
        ):
            # The source documents the required gate even when the inert
            # implementation returns false before calling the API.
            self.assertIn(token, self.cpp)
        self.assertIn("kReleaseManifestPinned", self.hpp)
        self.assertIn("kSelfAuthenticodePinned", self.hpp)
        self.assertIn("WinVerifyTrust", self.cpp + (ROOT / "native" / "windows_broker" / "manifest.cpp").read_text(encoding="utf-8"))

    def test_bootstrap_is_narrow_and_bound(self):
        for token in (
            "control_read", "control_write", "inherited_only", "parent.pid",
            "pipe_server", "creation_time", "session_id", "token_sid_digest",
            "parent.image", "direction_proven", "PipeDirection",
            "anonymous_pipe_handle", "GetNamedPipeServerProcessId",
            "GetNamedPipeClientProcessId", "GetFileType", "FILE_TYPE_PIPE",
            "ProcessIdToSessionId", "QueryFullProcessImageNameW",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("HANDLE_FLAG_INHERIT", self.cpp)
        self.assertIn("TOKEN_QUERY", self.cpp)
        self.assertIn("TokenUser", self.cpp)
        self.assertIn("initialize_supervisor", self.cpp)

    def test_capabilities_are_cng_random_and_memory_only(self):
        for token in (
            "IssuedCapability", "BCryptGenRandom", "BCRYPT_USE_SYSTEM_PREFERRED_RNG",
            "BCryptCreateHash", "BCRYPT_ALG_HANDLE_HMAC_FLAG", "expires_at_ms",
            "operation_scope", "SecureZeroMemory", "CapabilityIssuer",
            "CapabilityVerifier", "signing_key_", "compute_mac",
        ):
            self.assertIn(token, self.cpp)
        for forbidden in ("ofstream", "CreateFileA", "CreateFile(", "GetCommandLine", "putenv", "setenv"):
            self.assertNotIn(forbidden, self.cpp)

    def test_capability_binding_is_complete_and_replay_safe(self):
        for token in (
            "kScopeProcessLaunch", "kScopeBrokerSession", "kScopeExternalAction",
            "valid_scope", "operation_id", "operation_digest", "argument_digest",
            "preview_digest", "constant_time_equal", "NonceReplaySet",
            "state.replay.consume", "now_ms >= capability.expires_at_ms",
            "capability.operation_scope != scope", "capability_valid",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("capability.clear_secrets();", self.cpp)
        self.assertIn("if (!compute_mac(issuer.signing_key_, capability))", self.cpp)
        self.assertIn("BCRYPT_ALG_HANDLE_HMAC_FLAG", self.cpp)
        self.assertNotIn("kCancellableIoPro &&", self.cpp)
        self.assertIn("kCancellableIoProven", self.cpp)
        self.assertIn("reinterpret_cast<PUCHAR>", self.cpp)
        self.assertNotIn("mac_key", self.cpp)
        self.assertIn("~CapabilityIssuer()", self.cpp)
        self.assertIn("initialize_epoch()", self.cpp)
        self.assertIn("epoch_initialized_", self.cpp)
        self.assertIn("std::array<std::byte, kCapabilityBytes> candidate{}", self.cpp)
        self.assertIn("kRetainedExecutingSectionIdentityProven = false", self.hpp)

    def test_bootstrap_process_and_pipe_proof_is_os_bound(self):
        for token in (
            "OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE",
            "GetProcessTimes", "QueryFullProcessImageNameW", "CreateFileW",
            "FILE_FLAG_OPEN_REPARSE_POINT", "FILE_ID_INFO", "FileIdInfo",
            "server_creation_time", "PIPE_SERVER_END", "direction_proven",
            "pipe.server_pid != expected_server_pid",
            "proof.pipe_server.creation_time != proof.parent.creation_time",
            "executable_identity_bound(observed_parent.image",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("expected.file_id == observed.file_id", self.cpp)
        self.assertIn("standard.NumberOfLinks != 1", self.cpp)
        self.assertIn("process_token_sid_matches(proof.pipe_server.pid", self.cpp)
        self.assertIn("proof.pipe_server.session_id != proof.parent.session_id", self.cpp)
        self.assertIn("proof.pipe_server.token_sid_digest != proof.parent.token_sid_digest", self.cpp)

    def test_bootstrap_ownership_and_direction_are_not_caller_claims(self):
        self.assertIn("DuplicateHandle", self.cpp)
        self.assertNotIn("FILE_ACCESS_INFORMATION", self.cpp)
        self.assertNotIn("(pipe_flags & PIPE_CLIENT_END) == 0", self.cpp)
        self.assertIn("(pipe_flags & PIPE_SERVER_END) != 0", self.cpp)
        self.assertIn("HANDLE_FLAG_PROTECT_FROM_CLOSE", self.cpp)
        self.assertIn("const DWORD desired", self.cpp)
        self.assertIn("const DWORD opposite", self.cpp)
        self.assertIn("GENERIC_READ", self.cpp)
        self.assertIn("GENERIC_WRITE", self.cpp)
        self.assertIn("SetHandleInformation(candidate.get(), HANDLE_FLAG_INHERIT, 0)", self.cpp)
        self.assertIn("adopt_bootstrap(BootstrapProof&", self.cpp)
        self.assertIn("proof.control_read.handle = INVALID_HANDLE_VALUE", self.cpp)
        self.assertIn("state.bootstrap_handles", self.cpp)
        self.assertIn("CloseHandle(original_read)", self.cpp)
        self.assertIn("CloseHandle(original_write)", self.cpp)
        self.assertIn("closed_read", self.cpp)
        self.assertIn("closed_write", self.cpp)
        self.assertIn("bootstrap_transfer_fail_stop()", self.cpp)
        self.assertIn("!kRetainedExecutingSectionIdentityProven", self.cpp)

    def test_job_membership_is_kernel_verified_before_registry_insert(self):
        self.assertIn("AssignProcessToJobObject", self.cpp)
        self.assertIn("IsProcessInJob", self.cpp)
        self.assertIn("verify_membership(child.process.get(), root_job)", self.cpp)
        self.assertIn("membership_verified = true", self.cpp)
        self.assertIn("children_.size() >= kMaxChildren", self.cpp)

    def test_partial_random_or_mac_layout_failure_clears_capability(self):
        issue_start = self.cpp.index("bool issue_capability(\n    CapabilityIssuer")
        issue = self.cpp[issue_start:self.cpp.index("bool capability_valid", issue_start)]
        self.assertIn("capability.clear_secrets();", issue)
        self.assertIn("if (!fill_random", issue)
        self.assertIn("if (!compute_mac(issuer.signing_key_, capability))", issue)
        self.assertLess(issue.index("capability.clear_secrets();"), issue.index("if (!trust_gates_open"))
        self.assertIn("append_mac_u64", self.cpp)
        self.assertIn("append_mac_u32", self.cpp)

    def test_root_policy_is_direct_and_nested_jobs_refuse(self):
        self.assertIn("ActiveProcessLimit = kMaxChildren", self.cpp)
        self.assertEqual(self.contract["limits"]["max_children"],
                         self.contract["limits"]["max_registry_entries"])
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", self.cpp)
        self.assertIn("!kNestedJobPolicyProven", self.cpp)
        self.assertIn("root_job_policy_proven", self.cpp)
        self.assertIn("return nullptr;", self.cpp[self.cpp.index("HANDLE create_child_job"):self.cpp.index("bool root_job_policy_proven")])

    def test_registry_raii_and_fail_stop_cleanup(self):
        for token in (
            "class UniqueHandle", "Child(Child&&)", "children_.emplace",
            "catch (...)", "bool unregister", "bool terminate_and_reap",
            "TerminateJobObject(root_job, 1)", "job_empty(root_job)",
            "state.root_job.reset()", "stop_supervisor(process_state(), kMaxWaitMs)",
        ):
            self.assertIn(token, self.cpp)
        self.assertNotIn("CloseHandle(child.process)", self.cpp)
        self.assertNotIn("CloseHandle(child.job)", self.cpp)

    def test_journal_terminal_order_is_explicit(self):
        start = self.cpp.index("JournalRecord start")
        persist_start = self.cpp.index("persist(start)", start)
        operation = self.cpp.index("const DispatchStatus operation_status = operation(capability)", start)
        terminal = self.cpp.index("JournalRecord completed", start)
        self.assertLess(start, persist_start)
        self.assertLess(persist_start, operation)
        self.assertLess(operation, terminal)
        self.assertIn("journal.dispatch(capability, persist, operation)", self.cpp)
        self.assertIn("bool acknowledge() const noexcept", self.cpp)
        self.assertIn("kPreDispatchFailure", self.cpp)
        self.assertIn("kDispatchedUnknown", self.cpp)
        self.assertIn("JournalState query()", self.cpp)
        self.assertIn("using JournalLoad", self.cpp)
        self.assertIn("begin_next_operation()", self.cpp)
        self.assertIn("bool recover(JournalLoad load)", self.cpp)
        self.assertIn("latest.state == JournalState::kStartDurable", self.cpp)
        self.assertIn("recovery_required()", self.cpp)

    def test_journal_recovery_does_not_downgrade_durable_start(self):
        recover = self.cpp[self.cpp.index("bool recover(JournalLoad load)"):self.cpp.index("bool recovery_required()", self.cpp.index("bool recover(JournalLoad load)"))]
        self.assertIn("latest.sequence == 0", recover)
        self.assertIn("latest.operation_id == 0", recover)
        self.assertIn("!valid_scope(latest.scope)", recover)
        self.assertIn("state_ = latest.state == JournalState::kStartDurable", recover)
        self.assertIn("JournalState::kUnknown", recover)

    def test_inventory_contains_supervisor_static_test(self):
        inventory = (ROOT / "scripts" / "test" / "run_qa.py").read_text(encoding="utf-8")
        self.assertIn('"tests/native/test_windows_supervisor_authority_static.py": "native_static"', inventory)

    def test_journal_authorize_preserves_typed_outcome(self):
        start = self.cpp.index("JournalOutcome durable_journal_authorize")
        body = self.cpp[start:self.cpp.index("bool consume_capability", start)]
        self.assertIn("return journal.dispatch(capability, persist, operation)", body)
        self.assertNotIn("return outcome.status ==", body)
        self.assertIn("DispatchStatus::kPreDispatchFailure", self.cpp)
        self.assertIn("DispatchStatus::kDispatchedUnknown", self.cpp)
        self.assertIn("DispatchStatus::kTerminalFailure", self.cpp)
        self.assertIn("DispatchStatus::kTerminalSuccess", self.cpp)
        self.assertIn("DispatchStatus::kPersistenceFailure", self.cpp)
        self.assertIn("return JournalOutcome{DispatchStatus::kPersistenceFailure", self.cpp)

    def test_job_registry_cleanup_is_bounded_and_whole_tree(self):
        for token in (
            "CreateJobObjectW", "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
            "JOB_OBJECT_LIMIT_ACTIVE_PROCESS", "TerminateJobObject",
            "QueryInformationJobObject", "ActiveProcesses", "wait_reaped",
            "ChildRegistry", "terminate_and_reap_all", "CloseHandle",
        ):
            self.assertIn(token, self.cpp)
        self.assertIn("state.children.terminate_and_reap_all", self.cpp)
        self.assertIn("state.children.close_all()", self.cpp)
        self.assertIn("kMaxChildren = 8", self.cpp)
        self.assertIn("kMaxWaitMs = 120000", self.cpp)
        self.assertIn("create_child_job", self.cpp)
        self.assertIn("executable_identity_bound", self.cpp)

    def test_reap_ignores_cancellation_after_root_termination(self):
        start = self.cpp.index("bool terminate_and_reap_all")
        reap = self.cpp[start:self.cpp.index("void close_all", start)]
        self.assertIn("TerminateJobObject(root_job, 1)", reap)
        self.assertIn("wait_reaped(child.process.get(), remaining, nullptr)", reap)
        self.assertNotIn("wait_reaped(child.process.get(), remaining, cancellation)", reap)
        self.assertEqual(reap.count("TerminateJobObject(root_job, 1)"), 1)
        self.assertIn("if (!job_empty(root_job)) ok = false", reap)

    def test_public_header_cannot_forge_authority(self):
        self.assertNotIn("IssuedCapability", self.hpp)
        self.assertNotIn("BootstrapProof", self.hpp)
        self.assertNotIn("issue_capability", self.hpp)
        self.assertNotIn("CreateJobObjectW", self.hpp)

    def test_durable_authority_and_redacted_transport(self):
        self.assertIn("durable_journal_authorize", self.cpp)
        self.assertIn("journal.dispatch(capability, persist, operation)", self.cpp)
        self.assertIn("bool acknowledge() const noexcept", self.cpp)
        self.assertIn("loopback_metadata", self.hpp)
        self.assertIn("kUnavailable, 0, false, false", self.cpp)
        for field in self.contract["forbidden_receipt_fields"]:
            self.assertNotIn(f'"{field}"', self.cpp)

    def test_reference_limits_are_bounded(self):
        self.assertLess(SOURCE.stat().st_size, MAX_SOURCE_BYTES)
        self.assertLess(HEADER.stat().st_size, 64 * 1024)
        limits = self.contract["limits"]
        self.assertEqual(limits["max_children"], 8)
        self.assertEqual(limits["max_capability_lifetime_ms"], 300000)
        self.assertEqual(limits["max_frame_bytes"], 65536)
        self.assertEqual(limits["max_wait_ms"], 120000)


if __name__ == "__main__":
    unittest.main()
