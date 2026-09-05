"""Static/model checks for the inert supervisor-owned process transaction.

These tests do not compile native code or invoke Windows, process, journal,
network, provider, browser, or model APIs.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native/windows_supervisor/authority.cpp"
HPP = ROOT / "native/windows_supervisor/authority.hpp"
TRANSACTION = ROOT / "native/windows_supervisor/process_transaction.inc"
CONTRACT = ROOT / "contracts/windows-process-transaction/v0.1.0.json"


def strict_json(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("oversized contract")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("contract must be object")
    return value


@dataclass
class ModelResult:
    status: str
    create_calls: int
    terminal_durable: bool
    job_empty: bool
    poisoned: bool


class TransactionModel:
    """Small failure-boundary oracle; it is not production persistence."""

    def __init__(self):
        self.active = False
        self.used_nonces: set[str] = set()

    def run(self, fault: str | None = None, nonce: str = "n1") -> ModelResult:
        if self.active or nonce in self.used_nonces:
            return ModelResult("refused", 0, False, True, self.active)
        self.active = True
        create_calls = 0
        terminal = False
        empty = True
        poisoned = False
        try:
            if fault in {"gate", "shape", "identity", "token", "pipes"}:
                return ModelResult("pre_dispatch", 0, False, True, False)
            self.used_nonces.add(nonce)
            if fault == "start_persist":
                return ModelResult("unknown_manual", 0, False, True, False)
            if fault == "post_start_recheck":
                return ModelResult("terminal_failure", 0, True, True, False)
            create_calls = 1
            if fault == "create_return":
                return ModelResult("unknown_manual", 1, False, True, False)
            empty = False
            if fault in {"membership", "registry", "pre_resume", "monitor", "drain"}:
                poisoned = True
                empty = fault != "drain"
                return ModelResult(
                    "terminal_failure" if empty else "unknown_manual",
                    create_calls,
                    empty,
                    empty,
                    poisoned,
                )
            empty = True
            if fault == "terminal_persist":
                return ModelResult("unknown_manual", create_calls, False, True, False)
            terminal = True
            return ModelResult("ok", create_calls, terminal, empty, False)
        finally:
            self.active = False


@dataclass
class IoSettlement:
    returned: bool
    settled: bool
    cancellation_issued: bool
    fail_stop: bool


def model_overlapped_read(path: str) -> IoSettlement:
    if path == "sync_success":
        return IoSettlement(True, True, False, False)
    if path in {"sync_terminal_error", "not_started"}:
        return IoSettlement(False, True, False, False)
    if path == "pending_success":
        return IoSettlement(True, True, False, False)
    if path in {
        "pending_terminal_error", "deadline", "cancel", "wait_error",
        "cancel_api_error_then_terminal",
    }:
        return IoSettlement(False, True, path != "pending_terminal_error", False)
    if path in {"pending_never_signals", "get_still_incomplete"}:
        return IoSettlement(False, False, True, True)
    raise AssertionError("unknown fixture path")


@dataclass
class ShutdownSettlement:
    cancellation_signalled: bool
    authority_retained: bool
    job_closed: bool


def model_shutdown(lock_acquired: bool, reap_proved: bool) -> ShutdownSettlement:
    # Signal always precedes lock acquisition. A timeout or ambiguous reap
    # retains every authority object; only full proof permits close.
    if not lock_acquired or not reap_proved:
        return ShutdownSettlement(True, True, False)
    return ShutdownSettlement(True, False, True)


class WindowsProcessTransactionStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = CPP.read_bytes().decode("utf-8")
        cls.hpp = HPP.read_bytes().decode("utf-8")
        cls.tx = TRANSACTION.read_bytes().decode("utf-8")
        cls.contract = strict_json(CONTRACT)

    def test_contract_is_closed_and_explicitly_inert(self):
        self.assertEqual(
            set(self.contract),
            {
                "schema", "status", "availability", "authority",
                "capability_binding", "launch", "journal", "containment",
                "limits", "receipt", "unresolved_activation_blockers",
            },
        )
        self.assertEqual(
            self.contract["schema"],
            "lae.windows-process-transaction.contract.v0.1.0",
        )
        self.assertEqual(self.contract["status"], "SOURCE_ONLY_NOT_READY")
        self.assertTrue(all(value is False for value in self.contract["availability"].values()))
        self.assertIs(self.contract["journal"]["adapter_implementation_present"], False)
        self.assertGreaterEqual(len(self.contract["unresolved_activation_blockers"]), 8)
        self.assertEqual(self.contract["containment"]["evidence"],
                         "uncompiled_static_source_design_only")
        self.assertEqual(self.contract["limits"]["max_cleanup_reserve_ms"], 120000)
        self.assertEqual(self.contract["limits"]["max_transaction_horizon_ms"], 240000)
        self.assertTrue(any("cooperative cancellation" in item
                            for item in self.contract["unresolved_activation_blockers"]))

    def test_no_public_or_product_integration_surface(self):
        for token in (
            "LaunchPlan", "LaunchReceipt", "DurableJournalAdapter",
            "CreateProcessAsUserW", "process_launch_authority",
        ):
            self.assertNotIn(token, self.hpp)
        cmake = (ROOT / "native/CMakeLists.txt").read_text(encoding="utf-8")
        self.assertNotIn("process_transaction", cmake)
        self.assertNotIn("windows_supervisor", cmake)
        product_surfaces = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "host/agent/controller.mjs",
                "host/providers/index.mjs",
                "host/tools/local/index.mjs",
                "host/server/host-server.mjs",
            )
        )
        self.assertNotIn("process_transaction.inc", product_surfaces)
        self.assertNotIn("supervisor_owned_launch_transaction", product_surfaces)

    def test_immutable_false_gate_precedes_path_handle_and_process_access(self):
        self.assertIn("kSupervisorOwnedProcessTransactionAccepted = false", self.hpp)
        self.assertIn("kRetainedExecutingSectionIdentityProven = false", self.hpp)
        self.assertIn("kRetainedWorkingDirectoryIdentityProven = false", self.hpp)
        start = self.tx.index("LaunchReceipt supervisor_owned_launch_transaction")
        body = self.tx[start:]
        gate = body.index("if (!process_launch_gates_open()) return output")
        for token in (
            "process_state()", "GetWindowsDirectoryW", "CreateFileW(",
            "create_restricted_low_token", "make_pipe_pair",
            "dispatch_owned(",
        ):
            self.assertGreater(body.index(token), gate, token)

    def test_adapter_is_module_private_absent_and_recovery_is_mandatory(self):
        self.assertIn("class DurableJournalAdapter", self.cpp)
        self.assertIn("append_and_readback_exact", self.cpp)
        self.assertIn("load_latest_exact", self.cpp)
        self.assertIn("DurableJournalAdapter* const durable_adapter = nullptr", self.tx)
        self.assertIn("const bool recovered = false", self.tx)
        self.assertIn("const std::uint64_t root_generation = 0", self.tx)
        self.assertNotIn("durable_adapter = &", self.tx)
        self.assertIn("bool recover_owned(DurableJournalAdapter& adapter)", self.cpp)
        launch = self.tx[self.tx.index("LaunchReceipt supervisor_owned_launch_transaction"):]
        self.assertIn("!authority.recovered || !authority.durable_adapter", launch)
        self.assertNotIn("class DurableJournalAdapter", self.hpp)

    def test_creation_is_in_root_job_at_first_existence(self):
        owner = self.tx[self.tx.index("class StartupAttributeOwner"):
                        self.tx.index("struct PreparedLaunch")]
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", owner)
        self.assertIn("PROC_THREAD_ATTRIBUTE_JOB_LIST", owner)
        self.assertIn("const std::array<HANDLE, 3> inherited_handles_", owner)
        self.assertIn("const std::array<HANDLE, 1> job_handles_", owner)
        self.assertIn("std::unique_ptr<StartupAttributeOwner>", self.tx)
        create = self.tx[self.tx.index("STARTUPINFOEXW startup"):
                         self.tx.index("if (!launched)")]
        self.assertIn("payloads_match", create)
        self.assertIn("startup_attributes->get()", create)
        self.assertIn("CREATE_SUSPENDED", create)
        self.assertIn("EXTENDED_STARTUPINFO_PRESENT", create)
        self.assertIn("CreateProcessAsUserW", create)
        self.assertNotIn("AssignProcessToJobObject", self.tx)
        self.assertNotIn("CREATE_BREAKAWAY_FROM_JOB", self.tx)

    def test_durable_start_is_immediately_before_creation_callback(self):
        dispatch = self.cpp[self.cpp.index("JournalOutcome dispatch_owned"):
                            self.cpp.index("#include \"process_transaction.inc\"")]
        self.assertLess(dispatch.index("append_and_readback_exact"),
                        dispatch.index("operation(capability)"))
        callback = self.tx[self.tx.index("DispatchStatus create_monitor_reap_after_durable_start"):
                           self.tx.index("[[maybe_unused]] LaunchReceipt")]
        self.assertLess(callback.index("checkpoint("), callback.index("CreateProcessAsUserW"))
        self.assertLess(callback.index("same_identity_handle"), callback.index("CreateProcessAsUserW"))
        between = callback[callback.index("const LaunchPlan& plan"):callback.index("CreateProcessAsUserW")]
        for forbidden in ("Sleep(", "WaitForMultipleObjects", "ReadFile(", "WriteFile(", "CreateThread"):
            self.assertNotIn(forbidden, between)

    def test_capability_is_private_recomputed_complete_and_one_use(self):
        launch = self.tx[self.tx.index("LaunchReceipt supervisor_owned_launch_transaction"):]
        self.assertLess(launch.index("digest_plan("), launch.index("consume_capability("))
        self.assertLess(launch.index("consume_capability("), launch.index("dispatch_owned("))
        for token in (
            "request_id", "operation_id", "session_id", "root_generation",
            "expected_content_sha256", "expected_token_policy_digest",
            "expected_executable.file_id", "expected_executable.volume_serial",
            "expected_executable.file_index", "expected_executable.size",
            "digests.arguments", "digests.limits", "digests.environment",
            "capability.expires_at_ms",
        ):
            self.assertIn(token, self.tx)
        self.assertIn("state.replay.consume(capability)", self.cpp)

    def test_exact_executable_fixed_argv_minimal_environment_and_restricted_token(self):
        for token in (
            "FILE_SHARE_READ", "FILE_FLAG_OPEN_REPARSE_POINT",
            "hash_retained_executable", "same_identity_handle",
            "CreateRestrictedToken", "DISABLE_MAX_PRIVILEGE",
            "SECURITY_MANDATORY_LOW_RID", "SystemRoot=",
            "build_command_line", "append_quoted_argument",
        ):
            self.assertIn(token, self.tx)
        for forbidden in (
            "CreateProcessW(", "CreateProcessA(", "ShellExecute", "SearchPath",
            "system(", "cmd.exe", "powershell", "GetEnvironmentVariable",
            "CREATE_BREAKAWAY_FROM_JOB",
        ):
            self.assertNotIn(forbidden, self.tx)

    def test_registry_transfer_and_whole_job_failure_are_fail_closed(self):
        registry = self.cpp[self.cpp.index("class ChildRegistry"):
                            self.cpp.index("struct SupervisorState")]
        self.assertIn("children_.try_emplace(child.stable_id)", registry)
        self.assertLess(registry.index("try_emplace"), registry.index("std::move(child)"))
        cleanup = self.tx[self.tx.index("bool terminate_unregistered"):
                          self.tx.index("DispatchStatus create_monitor")]
        self.assertIn("context.authority->poisoned = true", cleanup)
        self.assertIn("TerminateJobObject", cleanup)
        self.assertIn("wait_job_empty_until", cleanup)
        self.assertIn("cleanup_deadline_at_ms", cleanup)
        self.assertIn("!context.child.process ||", cleanup)
        self.assertIn("authority.active", self.tx)

    def test_output_and_waits_are_bounded_and_secret_free(self):
        self.assertIn("kMaxCapturedBytesPerStream = 64 * 1024", self.cpp)
        self.assertIn("context->limit", self.cpp)
        self.assertIn("CancelSynchronousIo", self.cpp)
        self.assertIn("settle_drain_worker", self.cpp)
        self.assertNotIn("TerminateThread", self.cpp + self.tx)
        self.assertNotIn("INFINITE", self.cpp + self.tx)
        self.assertEqual(self.contract["limits"]["max_aggregate_output_bytes"], 131072)
        self.assertNotIn("std::cout", self.tx)
        self.assertNotIn("printf(", self.tx)
        self.assertEqual(
            set(self.contract["receipt"]["fields"]),
            {"status", "stdout_bytes", "stderr_bytes", "exit_code",
             "journal_bound", "job_reaped", "mutation_attempted"},
        )

    def test_overlapped_read_settles_before_stack_objects_can_return(self):
        settle = self.tx[self.tx.index("bool settle_started_overlapped_read"):
                         self.tx.index("bool overlapped_read")]
        read = self.tx[self.tx.index("bool overlapped_read"):
                       self.tx.index("bool hash_retained_executable")]
        self.assertIn("CancelIoEx(file, &operation)", settle)
        self.assertIn("WaitForSingleObject(operation.hEvent", settle)
        self.assertIn("GetOverlappedResult(file, &operation", settle)
        self.assertIn("GetTickCount64() >= cleanup_deadline_at_ms", settle)
        self.assertIn("ERROR_IO_INCOMPLETE", settle)
        self.assertIn("launch_cleanup_fail_stop()", settle)
        for branch in (
            "now >= deadline_at_ms", "WAIT_OBJECT_0 + 1",
            "result != WAIT_TIMEOUT", "ERROR_IO_INCOMPLETE",
        ):
            self.assertIn(branch, read)
        self.assertGreaterEqual(read.count("settle_started_overlapped_read("), 4)
        self.assertIn("if (!operation.hEvent || !ResetEvent(operation.hEvent))", read)

    def test_overlapped_model_covers_every_terminal_and_ambiguous_path(self):
        for path in (
            "sync_success", "sync_terminal_error", "not_started",
            "pending_success", "pending_terminal_error", "deadline", "cancel",
            "wait_error", "cancel_api_error_then_terminal",
        ):
            with self.subTest(path=path):
                result = model_overlapped_read(path)
                self.assertTrue(result.settled)
                self.assertFalse(result.fail_stop)
        for path in ("pending_never_signals", "get_still_incomplete"):
            with self.subTest(path=path):
                result = model_overlapped_read(path)
                self.assertFalse(result.returned)
                self.assertFalse(result.settled)
                self.assertTrue(result.fail_stop)

    def test_cancellation_event_and_shutdown_order_are_supervisor_owned(self):
        initialize = self.cpp[self.cpp.index("bool initialize_supervisor"):
                              self.cpp.index("bool executable_identity_bound",
                                             self.cpp.index("bool initialize_supervisor"))]
        shutdown = self.cpp[self.cpp.index("bool stop_supervisor"):
                            self.cpp.index("JournalOutcome durable_journal_authorize")]
        self.assertIn("state.cancellation.reset(CreateEventW(nullptr, TRUE, FALSE, nullptr))", initialize)
        signal = shutdown.index("SetEvent(state.cancellation.get())")
        lock = shutdown.index("transaction_lock.try_lock()")
        registry = shutdown.index("terminate_and_reap_all(")
        self.assertLess(signal, lock)
        self.assertLess(lock, registry)
        self.assertIn("cleanup_deadline_at_ms = now + deadline_ms", shutdown)
        self.assertIn("cleanup_deadline_at_ms,", shutdown)
        self.assertNotIn("kMaxWaitMs, nullptr", shutdown)
        self.assertIn("return GetTickCount64() < deadline_at_ms", self.cpp)

    def test_shutdown_retains_authority_when_settlement_is_unproved(self):
        shutdown = self.cpp[self.cpp.index("bool stop_supervisor"):
                            self.cpp.index("JournalOutcome durable_journal_authorize")]
        failure = shutdown.index("return false;", shutdown.index("terminate_and_reap_all"))
        close_registry = shutdown.index("state.children.close_all()")
        close_job = shutdown.index("state.root_job.reset()")
        self.assertLess(failure, close_registry)
        self.assertLess(failure, close_job)
        self.assertIn("Retain the signaled event, root Job, registry", shutdown)
        self.assertIn("launch.shutting_down.store(true", shutdown)

    def test_shutdown_model_signals_first_and_never_drops_ambiguous_ownership(self):
        for lock_acquired, reap_proved in ((False, False), (True, False)):
            with self.subTest(lock=lock_acquired, reap=reap_proved):
                result = model_shutdown(lock_acquired, reap_proved)
                self.assertTrue(result.cancellation_signalled)
                self.assertTrue(result.authority_retained)
                self.assertFalse(result.job_closed)
        complete = model_shutdown(True, True)
        self.assertTrue(complete.cancellation_signalled)
        self.assertFalse(complete.authority_retained)
        self.assertTrue(complete.job_closed)

    def test_cleanup_uses_bound_absolute_deadlines(self):
        for token in (
            "wait_reaped_until", "wait_job_empty_until",
            "cleanup_deadline_at_ms", "now >= deadline_at_ms",
        ):
            self.assertIn(token, self.cpp + self.tx)
        self.assertIn("plan.limits.cleanup_deadline_at_ms", self.tx)
        self.assertNotIn("wait_reaped(context.child.process.get(), kMaxWaitMs", self.tx)
        self.assertNotIn("wait_job_empty_bounded", self.cpp + self.tx)

    def test_model_refuses_all_pre_dispatch_faults_without_create(self):
        for fault in ("gate", "shape", "identity", "token", "pipes"):
            with self.subTest(fault=fault):
                result = TransactionModel().run(fault)
                self.assertEqual(result.status, "pre_dispatch")
                self.assertEqual(result.create_calls, 0)
                self.assertTrue(result.job_empty)

    def test_model_start_persistence_and_create_ack_loss_are_unknown(self):
        start = TransactionModel().run("start_persist")
        self.assertEqual((start.status, start.create_calls, start.terminal_durable),
                         ("unknown_manual", 0, False))
        create = TransactionModel().run("create_return")
        self.assertEqual((create.status, create.create_calls, create.terminal_durable),
                         ("unknown_manual", 1, False))

    def test_model_post_create_failures_require_reap_or_unknown(self):
        for fault in ("membership", "registry", "pre_resume", "monitor"):
            with self.subTest(fault=fault):
                result = TransactionModel().run(fault)
                self.assertEqual(result.create_calls, 1)
                self.assertTrue(result.job_empty)
                self.assertTrue(result.poisoned)
                self.assertEqual(result.status, "terminal_failure")
        ambiguous = TransactionModel().run("drain")
        self.assertEqual(ambiguous.status, "unknown_manual")
        self.assertFalse(ambiguous.job_empty)

    def test_model_terminal_ack_replay_and_serialization(self):
        model = TransactionModel()
        result = model.run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.terminal_durable)
        self.assertTrue(result.job_empty)
        self.assertEqual(model.run(nonce="n1").status, "refused")
        model.active = True
        self.assertEqual(model.run(nonce="n2").status, "refused")

    def test_current_qa_inventory_includes_this_test(self):
        inventory = (ROOT / "scripts/test/run_qa.py").read_text(encoding="utf-8")
        self.assertIn(
            '"tests/native/test_windows_process_transaction_static.py": "native_static"',
            inventory,
        )


if __name__ == "__main__":
    unittest.main()
