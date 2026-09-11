"""Static/model checks for the inert phase-2a process seam.

No compiler, Windows API, process, provider, network, credential, or model
runtime is invoked.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from itertools import product


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native/windows_supervisor/authority.cpp"
BORROW = ROOT / "native/windows_supervisor/borrow_ticket.hpp"
TRANSACTION = ROOT / "native/windows_supervisor/process_transaction.inc"
OWNER = ROOT / "native/action_journal_helper/journal_authority_owner.hpp"
PIPE_HPP = ROOT / "native/action_journal_helper/pipe_server.hpp"
PIPE_CPP = ROOT / "native/action_journal_helper/pipe_server.cpp"
HELPER_MAIN = ROOT / "native/action_journal_helper/main.cpp"
CONTRACT = ROOT / "contracts/windows-process-transaction/v0.1.0.json"


def strict_json(path: Path) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


class Phase2aModel:
    """A tiny refusal oracle; it is not production execution."""

    def __init__(self):
        self.create_calls = 0
        self.persist_calls = 0
        self.external_calls = 0

    def execute(self, gates: bool, proof_issuer: bool):
        if not gates or not proof_issuer:
            return "unavailable"
        self.create_calls += 1
        self.persist_calls += 1
        self.external_calls += 1
        return "ok"


class BorrowDomainModel:
    """Reference model for the combined admission/count control word."""

    CLOSING = 1 << 30
    POISONED = 1 << 31
    MAX = 4096

    def __init__(self):
        self.active = 0
        self.process = 0
        self.closing = False
        self.poisoned = False

    def acquire(self, kind):
        if self.closing or self.poisoned or self.active >= self.MAX:
            self.poisoned = True
            return False
        if kind == "process" and self.process >= self.MAX:
            self.poisoned = True
            return False
        self.active += 1
        if kind == "process":
            self.process += 1
        return True

    def close(self):
        self.closing = True

    def release(self, kind):
        if self.active == 0 or (kind == "process" and self.process == 0):
            self.poisoned = True
            return False
        self.active -= 1
        if kind == "process":
            self.process -= 1
        return True


class PipeCallModel:
    """Models helper join ordering without invoking a platform API."""

    def __init__(self):
        self.closing = False
        self.active = False
        self.ticket_in_state = True

    def begin(self):
        if self.closing or self.active:
            return False
        self.active = True
        self.ticket_in_state = False
        return True

    def close(self):
        self.closing = True

    def end(self):
        self.ticket_in_state = True
        self.active = False

    def stop_pipe(self):
        return not self.active and self.ticket_in_state


class PipeCallWordModel:
    """Exact observe/close/CAS interleaving for the one-word fence."""

    CLOSING = 1 << 31
    ACTIVE = 1

    def __init__(self):
        self.word = 0

    def observe_open(self):
        return self.word

    def close(self):
        self.word |= self.CLOSING

    def late_cas_acquire(self, observed):
        if self.word != observed or self.word & self.CLOSING or self.word & self.ACTIVE:
            return False
        self.word = observed | self.ACTIVE
        return True

    def release(self):
        self.word &= ~self.ACTIVE


class ProcessAuthorityModel:
    """Reference move/consume semantics for the process call authority."""

    def __init__(self, ticket=True):
        self.ticket = ticket
        self.used = False

    def move(self):
        moved = ProcessAuthorityModel(self.ticket)
        moved.used = self.used
        self.ticket = False
        self.used = True
        return moved

    def consume(self):
        if self.used or not self.ticket:
            return False
        self.used = True
        return True


class WindowsProcessTransactionStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = CPP.read_text(encoding="utf-8")
        cls.borrow = BORROW.read_text(encoding="utf-8")
        cls.tx = TRANSACTION.read_text(encoding="utf-8")
        cls.owner = OWNER.read_text(encoding="utf-8")
        cls.pipe_hpp = PIPE_HPP.read_text(encoding="utf-8")
        cls.pipe_cpp = PIPE_CPP.read_text(encoding="utf-8")
        cls.helper_main = HELPER_MAIN.read_text(encoding="utf-8")
        cls.contract = strict_json(CONTRACT)
        cls.source = cls.cpp + "\n" + cls.borrow + "\n" + cls.tx + "\n" + cls.owner

    def test_legacy_parallel_authority_surface_is_absent(self):
        banned = (
            r"\bJournalState\b", r"\bDispatchStatus\b",
            r"\bDurableLoadResult\b", r"\bJournalOutcome\b",
            r"\bJournalRecord\b", r"\bDurableJournalAdapter\b",
            r"\bJournalAuthority\b", r"durable_journal_authorize",
            r"consume_capability", r"recover_owned", r"begin_next_operation",
        )
        for pattern in banned:
            self.assertIsNone(re.search(pattern, self.source), pattern)

    def test_process_ids_are_exact_arrays_not_integer_ids(self):
        self.assertIn("using ProcessIdentity = std::array<std::uint8_t, 16>", self.tx)
        for field in ("request_identity", "session_identity", "stable_identity"):
            self.assertRegex(self.tx, rf"ProcessIdentity {field}")
        self.assertNotRegex(self.tx, r"uint64_t\s+(request|operation|session|stable)_?id")
        self.assertNotIn("std::hash", self.tx)
        self.assertIn("std::map<OperationIdentity, ChildSlot>", self.cpp)

    def test_binding_is_complete_and_dispatch_lease_only(self):
        for token in (
            "ProcessDispatchBinding", "request_ref", "call_ref", "tool",
            "risk", "side_effect", "args_digest", "preview_digest",
            "operation_digest", "authorization_kind", "authorized_sequence",
            "authorized_receipt_digest", "authorized_event_digest",
            "ProcessDispatchLease",
        ):
            self.assertIn(token, self.source)
        self.assertNotIn("dispatch_owned", self.source)
        self.assertNotIn("JournalPersist", self.source)
        self.assertNotIn("DispatchOperation", self.source)

    def test_process_launch_authority_is_borrow_only(self):
        region = self.cpp[self.cpp.index("struct ProcessLaunchAuthority"):
                          self.cpp.index("bool trust_gates_open")]
        self.assertIn("BorrowTicket owner", region)
        self.assertNotIn("SupervisorState* supervisor", region)
        self.assertIn(": owner(state.borrow_for_process())", region)
        self.assertNotIn("BorrowedOwnerHandle", self.cpp)
        for token in ("JournalAuthority", "DurableJournalAdapter", "JournalRecord"):
            self.assertNotIn(token, region)
        self.assertIn("ProcessLaunchAuthority(const ProcessLaunchAuthority&) = delete", region)

    def test_pipe_server_borrows_owner_and_standalone_helper_refuses(self):
        self.assertIn("PipeServerBorrow&& borrow", self.pipe_hpp)
        self.assertIn("PipeServerBorrow&& borrow", self.pipe_cpp)
        self.assertIn("borrow.checked_owner()", self.pipe_cpp)
        self.assertIn("owner->apply", self.pipe_cpp)
        self.assertNotIn("JournalAuthorityOwner::open", self.pipe_cpp)
        self.assertNotIn("StorageRequest", self.pipe_cpp)
        self.assertNotIn("run_foreground_helper_from_inherited_stdin()", self.helper_main)
        self.assertIn("return 70", self.helper_main)

    def test_explicit_startup_and_sole_owner(self):
        self.assertIn("SupervisorStartupHandoff(", self.cpp)
        self.assertIn("StorageRequest request", self.cpp)
        self.assertIn("explicit SupervisorState(const SupervisorStartupHandoff&", self.cpp)
        self.assertEqual(self.cpp.count("unique_ptr<JournalAuthorityOwner>"), 1)
        self.assertIn("static std::unique_ptr<SupervisorState> open(", self.cpp)
        self.assertIn("return nullptr;", self.cpp[self.cpp.index("static std::unique_ptr<SupervisorState> open"):])
        self.assertNotIn("StorageRequest{}", self.source)

    def test_shutdown_order_is_explicit_and_borrow_safe(self):
        shutdown = self.cpp[self.cpp.index("bool shutdown_ordered"):
                            self.cpp.index("SupervisorStartupHandoff startup")]
        order = (
            "pipe_call.close_admission", "pipe_call.wait_drained", "pipe.stop",
            "stop_admission", "process_fence.drain", "children.drain",
            "wait_process_drained", "wait_all_drained", "leases.clear",
            "journal_owner.reset",
        )
        positions = [shutdown.index(token) for token in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("No owner lock is held", self.cpp)
        self.assertIn("bool proven() const noexcept", self.borrow)
        self.assertIn("std::shared_ptr<BorrowControlBlock> control_", self.borrow)
        self.assertIn("std::condition_variable drained", self.borrow)

    def test_startup_copy_and_open_failures_are_refusal_safe(self):
        handoff = self.cpp[self.cpp.index("struct SupervisorStartupHandoff"):
                           self.cpp.index("struct ProcessTransactionFence")]
        self.assertNotIn(") noexcept", handoff)
        self.assertRegex(self.cpp, r"explicit SupervisorState\(const SupervisorStartupHandoff& handoff\)\n")
        opening = self.cpp[self.cpp.index("static std::unique_ptr<SupervisorState> open"):
                           self.cpp.index("BorrowTicket borrow_for_process")]
        for token in ("try {", "std::bad_alloc", "std::system_error", "catch (...)", "return nullptr;"):
            self.assertIn(token, opening)

    def test_pipe_and_process_retain_move_only_tickets(self):
        pipe = self.borrow[self.borrow.index("struct PipeServerBorrow"):]
        process = self.cpp[self.cpp.index("struct ProcessLaunchAuthority"):
                          self.cpp.index("bool trust_gates_open")]
        self.assertIn("BorrowTicket ticket", pipe)
        self.assertIn("ticket = std::move(value)", pipe)
        self.assertIn("~ProcessLaunchAuthority() noexcept = default", process)
        self.assertIn("BorrowTicket(BorrowTicket&& other) noexcept", self.borrow)
        self.assertIn("BorrowTicket(const BorrowTicket&) = delete", self.borrow)

    def test_pipe_helper_requires_ticket_and_returns_before_shutdown(self):
        self.assertIn("PipeServerBorrow&& borrow", self.pipe_hpp)
        self.assertIn("JournalAuthorityOwner* owner = borrow.checked_owner()", self.pipe_cpp)
        self.assertNotIn("run_foreground_helper_from_inherited_stdin(\n    JournalAuthorityOwner&", self.pipe_cpp)
        call = self.cpp[self.cpp.index("run_pipe_helper"):
                        self.cpp.index("bool shutdown_ordered")]
        for token in ("BorrowTicket ticket = borrow(BorrowKind::kPipe)",
                      "call_borrow.attach(std::move(ticket))",
                      "std::move(call_borrow)", "pipe_call.end()"):
            self.assertIn(token, call)
        shutdown = self.cpp[self.cpp.index("bool shutdown_ordered"):
                            self.cpp.index("SupervisorStartupHandoff startup")]
        self.assertLess(shutdown.index("pipe_call.wait_drained"),
                        shutdown.index("pipe.stop"))

    def test_process_transaction_requires_move_only_authority(self):
        signature = self.tx[self.tx.index("LaunchReceipt execute_process_transaction"):
                            self.tx.index("{", self.tx.index("LaunchReceipt execute_process_transaction"))]
        self.assertIn("ProcessLaunchAuthority authority", signature)
        self.assertNotIn("SupervisorState&", signature)
        self.assertNotIn("JournalAuthorityOwner", signature)
        body = self.tx[self.tx.index("LaunchReceipt execute_process_transaction"):]
        self.assertIn("plan.authority.has_value()", body)
        self.assertIn("validate_for_admission", body)
        self.assertIn("if (!authority.consume())", body)
        self.assertLess(body.index("validate_for_admission"),
                        body.index("authority.consume"))
        self.assertLess(body.index("if (!kProcessLaunchAvailable"),
                        body.index("authority.consume"))

    def test_process_authority_is_move_only_and_one_use(self):
        authority = self.cpp[self.cpp.index("struct ProcessLaunchAuthority"):
                             self.cpp.index("bool trust_gates_open")]
        for token in ("ProcessLaunchAuthority(ProcessLaunchAuthority&& other) noexcept",
                      "operator=(ProcessLaunchAuthority&& other) noexcept",
                      "consumed.exchange(true", "consumed.compare_exchange_strong",
                      "bool proven() const noexcept", "bool consume() noexcept"):
            self.assertIn(token, authority)
        self.assertIn("std::atomic_bool consumed{false}", authority)
        self.assertNotIn("ProcessLaunchAuthority(ProcessLaunchAuthority const&", authority)

    def test_pipe_call_model_never_releases_state_ticket_while_in_flight(self):
        model = PipeCallModel()
        self.assertTrue(model.begin())
        model.close()
        self.assertFalse(model.begin())
        self.assertFalse(model.stop_pipe())
        model.end()
        self.assertTrue(model.stop_pipe())

    def test_pipe_call_word_rejects_late_cas_after_shutdown_close(self):
        model = PipeCallWordModel()
        observed_open = model.observe_open()
        model.close()
        self.assertFalse(model.late_cas_acquire(observed_open))
        self.assertEqual(model.word & model.CLOSING, model.CLOSING)
        model.release()
        self.assertEqual(model.word & model.ACTIVE, 0)

    def test_process_authority_model_rejects_source_reuse_and_second_consume(self):
        source = ProcessAuthorityModel()
        moved = source.move()
        self.assertFalse(source.consume())
        self.assertTrue(moved.consume())
        self.assertFalse(moved.consume())

    def test_borrow_domain_has_linearized_close_count_and_wake(self):
        domain = self.borrow[self.borrow.index("class BorrowControlBlock"):
                            self.borrow.index("class BorrowTicket")]
        for token in ("kClosing", "kPoisoned", "kMaxActiveBorrows", "compare_exchange_weak",
                      "kProcessMask", "notify_all", "wait_process_drained", "wait_all_drained"):
            self.assertIn(token, domain)
        self.assertIn("active >= kMaxActiveBorrows", domain)
        self.assertIn("active == 0", domain)

    def test_borrow_domain_model_rejects_late_and_overflow_acquisition(self):
        model = BorrowDomainModel()
        self.assertTrue(model.acquire("pipe"))
        self.assertTrue(model.acquire("process"))
        model.close()
        self.assertFalse(model.acquire("process"))
        self.assertEqual((model.active, model.process), (2, 1))
        self.assertTrue(model.release("process"))
        self.assertTrue(model.release("pipe"))
        self.assertEqual((model.active, model.process), (0, 0))
        full = BorrowDomainModel()
        full.active = full.MAX
        self.assertFalse(full.acquire("pipe"))
        self.assertTrue(full.release("pipe"))
        self.assertTrue(full.poisoned)

    def test_borrow_domain_model_interleavings_preserve_counts(self):
        for events in product(("pipe_acquire", "process_acquire", "close",
                               "pipe_release", "process_release"), repeat=5):
            model = BorrowDomainModel()
            held_pipe = held_process = 0
            for event in events:
                if event == "close":
                    model.close()
                    self.assertEqual(model.active, held_pipe + held_process)
                    self.assertEqual(model.process, held_process)
                    continue
                kind, action = event.rsplit("_", 1)
                if action == "acquire":
                    accepted = model.acquire(kind)
                    if accepted:
                        if kind == "pipe": held_pipe += 1
                        else: held_process += 1
                elif action == "release":
                    if (kind == "pipe" and not held_pipe) or (kind == "process" and not held_process):
                        continue
                    accepted = model.release(kind)
                    if accepted:
                        if kind == "pipe" and held_pipe: held_pipe -= 1
                        elif kind == "process" and held_process: held_process -= 1
                self.assertEqual(model.active, held_pipe + held_process)
                self.assertEqual(model.process, held_process)
                self.assertLessEqual(model.active, model.MAX)
                self.assertLessEqual(model.process, model.MAX)

    def test_hard_refusal_precedes_every_mutation_boundary(self):
        body = self.tx[self.tx.index("LaunchReceipt execute_process_transaction") :]
        refusal = body.index("if (!kProcessLaunchAvailable")
        refused_return = body.index("return LaunchReceipt{};", refusal)
        self.assertLess(refusal, refused_return)
        self.assertNotIn("persist_dispatching", body[:refusal])
        self.assertNotIn("begin_external_dispatch", body[:refusal])
        for forbidden in ("CreateProcess", "ShellExecute", "system(", "popen("):
            self.assertNotIn(forbidden, self.source)

    def test_model_cannot_reach_canary_execution_when_gate_is_false(self):
        model = Phase2aModel()
        result = model.execute(False, False)
        self.assertEqual(result, "unavailable")
        self.assertEqual(model.create_calls, 0)
        self.assertEqual(model.persist_calls, 0)
        self.assertEqual(model.external_calls, 0)

    def test_contract_is_closed_and_inert(self):
        self.assertEqual(self.contract["status"], "SOURCE_ONLY_NOT_READY")
        self.assertTrue(all(value is False for value in self.contract["availability"].values()))
        self.assertEqual(self.contract["authority"]["operation_identity"],
                         "exact 16-byte array, never integer or hash coercion")
        self.assertEqual(self.contract["journal"]["owner_api"], "ProcessDispatchLease")
        self.assertFalse(self.contract["launch"]["reachable"])
        self.assertFalse(self.contract["launch"]["irreversible_mutation"])

    def test_no_product_activation_or_registry_changes(self):
        native = (ROOT / "native/CMakeLists.txt").read_text(encoding="utf-8")
        self.assertNotIn("windows_supervisor", native)
        supervisor_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (CPP, TRANSACTION)
        )
        self.assertNotIn("CreateProcess", supervisor_text)

    def test_current_qa_inventory_contains_this_test(self):
        inventory = (ROOT / "scripts/test/run_qa.py").read_text(encoding="utf-8")
        self.assertIn('"tests/native/test_windows_process_transaction_static.py": "native_static"', inventory)


if __name__ == "__main__":
    unittest.main()
