"""Static/model checks for the inert phase-2a process seam.

No compiler, Windows API, process, provider, network, credential, or model
runtime is invoked.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native/windows_supervisor/authority.cpp"
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


class WindowsProcessTransactionStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = CPP.read_text(encoding="utf-8")
        cls.tx = TRANSACTION.read_text(encoding="utf-8")
        cls.owner = OWNER.read_text(encoding="utf-8")
        cls.pipe_hpp = PIPE_HPP.read_text(encoding="utf-8")
        cls.pipe_cpp = PIPE_CPP.read_text(encoding="utf-8")
        cls.helper_main = HELPER_MAIN.read_text(encoding="utf-8")
        cls.contract = strict_json(CONTRACT)
        cls.source = cls.cpp + "\n" + cls.tx + "\n" + cls.owner

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
        self.assertIn("BorrowedOwnerHandle owner", region)
        self.assertIn("SupervisorState* supervisor", region)
        for token in ("JournalAuthority", "DurableJournalAdapter", "JournalRecord"):
            self.assertNotIn(token, region)
        self.assertIn("ProcessLaunchAuthority(const ProcessLaunchAuthority&) = delete", region)

    def test_pipe_server_borrows_owner_and_standalone_helper_refuses(self):
        self.assertIn("JournalAuthorityOwner& owner", self.pipe_hpp)
        self.assertIn("JournalAuthorityOwner& owner", self.pipe_cpp)
        self.assertIn("owner.apply", self.pipe_cpp)
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
        shutdown = self.cpp[self.cpp.index("void shutdown_ordered"):
                            self.cpp.index("SupervisorStartupHandoff startup")]
        order = (
            "stop_admission", "process_fence.drain", "children.drain",
            "process_borrow.release", "pipe.stop", "leases.clear",
            "journal_owner.reset",
        )
        positions = [shutdown.index(token) for token in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("No owner lock is held", self.cpp)
        self.assertIn("bool proven() const noexcept", self.cpp)

    def test_hard_refusal_precedes_every_mutation_boundary(self):
        body = self.tx[self.tx.index("LaunchReceipt execute_process_transaction") :]
        refusal = body.index("if (!kProcessLaunchAvailable")
        self.assertLess(refusal, body.index("return LaunchReceipt{};"))
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
