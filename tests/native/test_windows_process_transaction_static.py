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


@dataclass
class ReceiptProjection:
    status: str
    journal_bound: bool
    job_reaped: bool


def project_receipt(journal_status: str, callback_status: str,
                    journal_bound: bool, job_reaped: bool) -> ReceiptProjection:
    concrete_failures = {
        "pre_dispatch_failure", "cancelled", "deadline", "output_limit",
        "exit_failure", "terminal_failure",
    }
    if journal_status == "terminal_success":
        status = "ok" if journal_bound and job_reaped and callback_status == "ok" \
            else "dispatched_unknown"
    elif journal_status == "terminal_failure":
        if not journal_bound or not job_reaped:
            status = "dispatched_unknown"
        else:
            status = callback_status if callback_status in concrete_failures \
                else "terminal_failure"
    elif journal_status == "pre_dispatch_failure":
        status = "pre_dispatch_failure"
    else:
        status = "dispatched_unknown"
    return ReceiptProjection(status, journal_bound, job_reaped)


@dataclass
class ContextDestruction:
    freed: bool
    retained: bool
    fail_stop: bool


def model_context_destruction(worker_started: bool,
                              join_proved: bool) -> ContextDestruction:
    if not worker_started or join_proved:
        return ContextDestruction(True, False, False)
    return ContextDestruction(False, True, True)


def model_active_checkpoint(shutting_down: bool,
                            cancellation_signalled: bool) -> bool:
    return not shutting_down and not cancellation_signalled


class MutationFenceModel:
    """Linearization oracle for shutdown versus mutation/finalization."""

    def __init__(self):
        self.shutting_down = False
        self.mutations = 0
        self.successes = 0
        self.hash_updates = 0
        self.drain_bytes = 0
        self.cleanup_mutations = 0
        self.root_termination_issued = False

    def shutdown_wins(self, event_signal_succeeds: bool = True) -> None:
        # The atomic publication and signal attempt share the shutdown fence.
        self.shutting_down = True
        _ = event_signal_succeeds

    def mutation_wins(self) -> bool:
        if self.shutting_down:
            return False
        self.mutations += 1
        return True

    def terminal_wins(self) -> bool:
        if self.shutting_down:
            return False
        self.successes += 1
        return True

    def precheck(self) -> bool:
        return not self.shutting_down

    def consume_hash(self) -> bool:
        if self.shutting_down:
            return False
        self.hash_updates += 1
        return True

    def append_drain(self, count: int) -> bool:
        if self.shutting_down:
            return False
        self.drain_bytes += count
        return True

    def cleanup_root_once(self) -> bool:
        # Cleanup is permitted after shutdown, but its mutation is serialized
        # and idempotent under the same fence.
        if self.root_termination_issued:
            return True
        self.root_termination_issued = True
        self.cleanup_mutations += 1
        return True


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
        self.assertIs(
            self.contract["containment"]["forward_mutation_after_shutdown"],
            False,
        )
        self.assertEqual(
            self.contract["containment"]["cleanup_after_shutdown"],
            "mandatory_serialized_idempotent_root_termination_and_registry_detach",
        )
        self.assertIs(
            self.contract["containment"]["cleanup_waits_hold_mutation_fence"],
            False,
        )
        self.assertEqual(self.contract["limits"]["max_cleanup_reserve_ms"], 120000)
        self.assertEqual(self.contract["limits"]["max_transaction_horizon_ms"], 240000)
        self.assertTrue(any("cooperative cancellation" in item
                            for item in self.contract["unresolved_activation_blockers"]))
        self.assertIn("terminal_failure", self.contract["receipt"]["statuses"])

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
        owned = self.cpp[self.cpp.index("JournalOutcome dispatch_owned"):
                         self.cpp.index("bool recover_owned")]
        implementation = self.cpp[self.cpp.index("JournalOutcome dispatch_impl"):
                                  self.cpp.index("public:\n  JournalState query")]
        self.assertIn("persist(adapter, record, context)", owned)
        self.assertLess(implementation.index("persist(start)"),
                        implementation.index("operation(capability)"))
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
        self.assertIn("context.authority->poisoned.store(true", cleanup)
        self.assertIn("children.terminate_root_once", cleanup)
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
        close_registry = shutdown.index("state.children.close_all(launch.mutation_fence)")
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

    def test_failed_set_event_still_stops_every_later_checkpoint(self):
        checkpoint = self.tx[self.tx.index("bool checkpoint"):
                             self.tx.index("bool append_bytes")]
        shutdown = self.cpp[self.cpp.index("bool stop_supervisor"):
                            self.cpp.index("JournalOutcome durable_journal_authorize")]
        self.assertLess(shutdown.index("shutting_down.store(true"),
                        shutdown.index("SetEvent(state.cancellation.get())"))
        self.assertIn("authority.shutting_down.load(std::memory_order_acquire)",
                      checkpoint)
        self.assertGreaterEqual(
            self.tx.count("shutting_down.load(std::memory_order_acquire)"), 4)
        # A failed event signal cannot erase the already-published atomic stop.
        self.assertFalse(model_active_checkpoint(
            shutting_down=True, cancellation_signalled=False))

    def test_mutation_fence_orders_shutdown_create_resume_and_journal(self):
        authority = self.tx[self.tx.index("struct ProcessLaunchAuthority"):
                            self.tx.index("ProcessLaunchAuthority&")]
        shutdown = self.cpp[self.cpp.index("bool stop_supervisor"):
                            self.cpp.index("JournalOutcome durable_journal_authorize")]
        create = self.tx[self.tx.index("BOOL launched = FALSE"):
                         self.tx.index("if (!launched)")]
        resume = self.tx[self.tx.index("bool monitor_registered_child"):
                         self.tx.index("bool saw_exit")]
        persist = self.tx[self.tx.index("bool persist_launch_record"):
                          self.tx.index("bool make_pipe_pair")]
        self.assertIn("std::mutex mutation_fence", authority)
        self.assertIn("std::lock_guard fence(launch.mutation_fence)", shutdown)
        self.assertLess(shutdown.index("mutation_fence"),
                        shutdown.index("shutting_down.store(true"))
        self.assertLess(shutdown.index("shutting_down.store(true"),
                        shutdown.index("SetEvent(state.cancellation.get())"))
        self.assertIn("std::lock_guard fence(context->authority->mutation_fence)",
                      create)
        self.assertIn("std::lock_guard fence(context->authority->mutation_fence)",
                      resume)
        self.assertIn("std::lock_guard fence(context->authority->mutation_fence)",
                      persist)
        self.assertIn("journal_transition_claimed_sequence = record.sequence",
                      persist)
        self.assertLess(persist.index("mutation_fence"),
                        persist.index("adapter.append_and_readback_exact"))
        wrapper = self.tx[self.tx.index("const JournalOutcome journal"):]
        self.assertIn("finish_claimed_terminal", wrapper)
        self.assertIn(
            "prepared.journal_transition_claimed_sequence != journal.sequence",
            wrapper,
        )
        for bounded_section in (create, resume):
            self.assertNotIn("Sleep(", bounded_section)
            self.assertNotIn("WaitForMultipleObjects", bounded_section)

    def test_interleaving_model_never_mutates_or_succeeds_after_shutdown_wins(self):
        for boundary in (
            "before_create", "before_final", "during_sync_read",
            "set_event_failure",
        ):
            with self.subTest(boundary=boundary):
                model = MutationFenceModel()
                if boundary == "before_create":
                    model.shutdown_wins()
                    self.assertFalse(model.mutation_wins())
                elif boundary == "before_final":
                    self.assertTrue(model.mutation_wins())
                    model.shutdown_wins()
                    self.assertFalse(model.terminal_wins())
                elif boundary == "during_sync_read":
                    self.assertTrue(model.precheck())
                    model.shutdown_wins()
                    self.assertFalse(model.consume_hash())
                else:
                    model.shutdown_wins(event_signal_succeeds=False)
                    self.assertFalse(model.mutation_wins())
                self.assertEqual(model.successes, 0)

        create_first = MutationFenceModel()
        self.assertTrue(create_first.mutation_wins())
        create_first.shutdown_wins()
        self.assertEqual(create_first.mutations, 1)
        final_first = MutationFenceModel()
        self.assertTrue(final_first.terminal_wins())
        final_first.shutdown_wins()
        self.assertEqual(final_first.successes, 1)

    def test_check_to_hash_and_drain_append_are_fence_owned(self):
        hash_model = MutationFenceModel()
        self.assertTrue(hash_model.precheck())
        hash_model.shutdown_wins()
        self.assertFalse(hash_model.consume_hash())
        self.assertEqual(hash_model.hash_updates, 0)

        drain_model = MutationFenceModel()
        self.assertTrue(drain_model.precheck())
        drain_model.shutdown_wins()
        self.assertFalse(drain_model.append_drain(32))
        self.assertEqual(drain_model.drain_bytes, 0)

    def test_cleanup_is_serialized_idempotent_and_allowed_after_shutdown(self):
        model = MutationFenceModel()
        model.shutdown_wins()
        self.assertTrue(model.cleanup_root_once())
        self.assertTrue(model.cleanup_root_once())
        self.assertEqual(model.cleanup_mutations, 1)
        self.assertFalse(model.mutation_wins())

    def test_sync_and_awaited_reads_recheck_stop_before_consuming_bytes(self):
        read = self.tx[self.tx.index("bool overlapped_read"):
                       self.tx.index("bool hash_retained_executable")]
        usable = self.tx[self.tx.index("bool consume_hash_chunk"):
                         self.tx.index("bool append_bytes")]
        self.assertIn("std::lock_guard fence(authority.mutation_fence)", usable)
        self.assertIn("authority.shutting_down.load", usable)
        self.assertIn("WaitForSingleObject(cancellation, 0) != WAIT_TIMEOUT", usable)
        self.assertIn("BCryptHashData", usable)
        self.assertIn("BCryptFinishHash", usable)
        self.assertIn("transferred = 0", read)
        hashing = self.tx[self.tx.index("bool hash_retained_executable"):
                          self.tx.index("bool append_quoted_argument")]
        self.assertIn("consume_hash_chunk", hashing)
        self.assertIn("finish_hash_under_fence", hashing)
        self.assertNotIn("BCryptHashData(hash", hashing)
        drain = self.cpp[self.cpp.index("DWORD WINAPI drain_child_pipe"):
                         self.cpp.index("bool settle_drain_worker")]
        self.assertGreaterEqual(
            drain.count("std::lock_guard fence(*context->mutation_fence)"), 2)
        self.assertGreaterEqual(
            drain.count("drain_result_usable_locked(*context)"), 2)
        self.assertEqual(drain.count("context->captured.store"), 6)
        self.assertEqual(
            drain.count("std::lock_guard fence(*context->mutation_fence)"), 4)
        self.assertIn("SecureZeroMemory(context->bytes.data() + total, read)",
                      drain)
        settle = self.cpp[self.cpp.index("bool settle_child_drains"):
                          self.cpp.index("class ChildRegistry")]
        self.assertIn(
            "std::lock_guard fence(*child.stdout_context->mutation_fence)",
            settle,
        )
        self.assertLess(settle.index("std::lock_guard fence"),
                        settle.index("child.stdout_bytes ="))
        self.assertIn("capture_usable", settle)
        self.assertIn("? child.stdout_context->captured.load", settle)
        self.assertIn("? child.stderr_context->captured.load", settle)

    def test_terminal_claim_is_fenced_with_exact_sequence_and_stop_state(self):
        wrapper = self.tx[self.tx.index("const JournalOutcome journal"):]
        finish = wrapper[wrapper.index("const auto finish_claimed_terminal"):
                         wrapper.index("switch (journal.status)")]
        self.assertIn("std::lock_guard fence(authority.mutation_fence)", finish)
        self.assertIn("prepared.journal_transition_claimed_sequence != journal.sequence", finish)
        self.assertIn("authority.poisoned.load", finish)
        self.assertIn("authority.shutting_down.load", finish)
        self.assertIn("GetTickCount64() >= deadline_at_ms", finish)
        self.assertIn("WaitForSingleObject(supervisor.cancellation.get(), 0) != WAIT_TIMEOUT", finish)
        self.assertLess(finish.index("std::lock_guard fence"),
                        finish.index("authority.journal.begin_next_operation()"))

    def test_cleanup_mutations_share_fence_and_root_termination_is_once(self):
        registry = self.cpp[self.cpp.index("class ChildRegistry"):
                            self.cpp.index("struct SupervisorState")]
        helper = registry[registry.index("bool terminate_root_once_locked"):
                          registry.index("std::mutex mutex_")]
        self.assertIn("std::lock_guard fence(cleanup_fence)", helper)
        self.assertIn("root_termination_issued_", helper)
        self.assertIn("TerminateJobObject(root_job, 1)", helper)
        self.assertEqual(registry.count("TerminateJobObject(root_job, 1)"), 1)
        self.assertGreaterEqual(registry.count("terminate_root_once_locked("), 3)
        self.assertIn("std::lock_guard fence(cleanup_fence);\n      children_.erase", registry)
        self.assertIn("std::lock_guard fence(cleanup_fence);\n    for (auto&", registry)
        cleanup = self.tx[self.tx.index("bool terminate_unregistered"):
                          self.tx.index("DispatchStatus create_monitor")]
        self.assertIn("children.terminate_root_once", cleanup)
        self.assertNotIn("TerminateJobObject", cleanup)

    def test_preparation_rechecks_stop_between_resource_acquisitions(self):
        launch = self.tx[self.tx.index("PreparedLaunch prepared"):
                         self.tx.index("const JournalOutcome journal")]
        for token in (
            "CreateFileW(", "hash_retained_executable(",
            "create_restricted_low_token(", "make_pipe_pair(",
            "make_stdin_pair(", "prepare_startup_attributes(",
        ):
            self.assertIn(token, launch)
        self.assertGreaterEqual(launch.count("checkpoint(supervisor, authority"), 8)

    def test_worker_context_requires_join_before_release_or_fail_stops(self):
        owner = self.cpp[self.cpp.index("class DrainContextOwner"):
                         self.cpp.index("struct Child final")]
        settle = self.cpp[self.cpp.index("bool settle_child_drains"):
                          self.cpp.index("class ChildRegistry")]
        self.assertIn("if (context_) std::terminate()", owner)
        self.assertIn("release_after_join", owner)
        self.assertNotIn("std::unique_ptr<DrainContext>", self.cpp)
        self.assertLess(settle.index("stdout_worker.reset()"),
                        settle.index("stdout_context.release_after_join()"))
        self.assertLess(settle.index("stderr_worker.reset()"),
                        settle.index("stderr_context.release_after_join()"))
        unresolved = model_context_destruction(True, False)
        self.assertEqual(unresolved, ContextDestruction(False, True, True))
        self.assertEqual(model_context_destruction(True, True),
                         ContextDestruction(True, False, False))
        self.assertEqual(model_context_destruction(False, False),
                         ContextDestruction(True, False, False))

    def test_receipt_projection_uses_actual_journal_and_callback_outcome(self):
        wrapper = self.tx[self.tx.index("const JournalOutcome journal"):]
        self.assertIn("switch (journal.status)", wrapper)
        self.assertIn("concrete_terminal_failure(output.status)", wrapper)
        self.assertIn("output.status = LaunchResultCode::kTerminalFailure", wrapper)
        for callback in ("ok", "unavailable", "invalid_request"):
            with self.subTest(callback=callback):
                result = project_receipt(
                    "terminal_failure", callback, True, True)
                self.assertEqual(result.status, "terminal_failure")
                self.assertTrue(result.journal_bound)
                self.assertTrue(result.job_reaped)
        for callback in (
            "pre_dispatch_failure", "cancelled", "deadline", "output_limit",
            "exit_failure", "terminal_failure",
        ):
            with self.subTest(callback=callback):
                self.assertEqual(
                    project_receipt("terminal_failure", callback, True, True).status,
                    callback,
                )
        self.assertEqual(
            project_receipt("terminal_success", "ok", True, True).status, "ok")
        self.assertEqual(
            project_receipt("terminal_success", "unavailable", True, True).status,
            "dispatched_unknown",
        )
        for journal in ("dispatched_unknown", "persistence_failure"):
            with self.subTest(journal=journal):
                result = project_receipt(journal, "ok", False, True)
                self.assertEqual(result.status, "dispatched_unknown")
                self.assertFalse(result.journal_bound)
                self.assertTrue(result.job_reaped)

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
