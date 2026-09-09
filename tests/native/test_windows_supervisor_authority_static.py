"""Hostile static checks for the phase-2a supervisor topology.

The suite is source/model-only and never compiles or activates Windows code.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/windows_supervisor/authority.cpp"
BORROW = ROOT / "native/windows_supervisor/borrow_ticket.hpp"
HEADER = ROOT / "native/windows_supervisor/authority.hpp"
LAUNCH_HEADER = ROOT / "native/windows_supervisor/launch_authority.hpp"
CONTRACT = ROOT / "contracts/windows-supervisor/v1.0.0.json"


def strict_json(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


class SupervisorAuthorityStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = SOURCE.read_text(encoding="utf-8")
        cls.borrow = BORROW.read_text(encoding="utf-8")
        cls.source = cls.cpp + "\n" + cls.borrow
        cls.hpp = HEADER.read_text(encoding="utf-8")
        cls.launch_header = LAUNCH_HEADER.read_text(encoding="utf-8")
        cls.contract = strict_json(CONTRACT)

    def test_contract_remains_false_and_redacted(self):
        self.assertEqual(self.contract["status"], "SOURCE_ONLY_NOT_READY")
        for key in (
            "production_available", "native_target_registered", "cmake_registered",
            "launcher_registered", "host_registered", "package_registered",
            "activation_permitted",
        ):
            self.assertIs(self.contract[key], False)
        self.assertIs(self.contract["capability"]["secrets_persisted"], False)
        self.assertIs(self.contract["capability"]["secrets_logged"], False)
        self.assertIs(self.contract["capability"]["secrets_in_process_arguments"], False)

    def test_all_gates_are_compile_time_false(self):
        for token in (
            "kReleaseManifestPinned = false", "kSelfAuthenticodePinned = false",
            "kPackageIdentityPinned = false", "kCancellableIoProven = false",
            "kDurableJournalAuthority = false", "kNestedJobPolicyProven = false",
            "kSupervisorOwnedProcessTransactionAccepted = false",
            "if (!trust_gates_open()) return Status::kUnavailable",
        ):
            self.assertIn(token, self.hpp + self.cpp)
        self.assertNotIn("CreateProcess", self.cpp + self.hpp)

    def test_sole_owner_and_explicit_handoff(self):
        self.assertEqual(self.cpp.count("unique_ptr<JournalAuthorityOwner>"), 1)
        self.assertIn("explicit SupervisorState(const SupervisorStartupHandoff&", self.cpp)
        self.assertIn("StorageRequest request", self.cpp)
        self.assertIn("static std::unique_ptr<SupervisorState> open(", self.cpp)
        self.assertIn("return nullptr;", self.cpp[self.cpp.index("static std::unique_ptr<SupervisorState> open"):])
        self.assertNotIn("StorageRequest{}", self.cpp)
        self.assertNotIn("absolute_directory =", self.cpp)

    def test_borrowers_do_not_own_authority(self):
        for name in ("PipeServerBorrow", "ProcessLaunchAuthority"):
            self.assertIn(f"struct {name}", self.source)
        self.assertIn("class BorrowControlBlock", self.borrow)
        self.assertIn("std::shared_ptr<BorrowControlBlock> control_", self.borrow)
        self.assertNotIn("BorrowedOwnerHandle", self.source)
        process = self.cpp[self.cpp.index("struct ProcessLaunchAuthority"):
                           self.cpp.index("bool trust_gates_open")]
        self.assertIn("BorrowTicket owner", process)
        self.assertNotIn("SupervisorState* supervisor", process)
        self.assertNotRegex(process, r"JournalAuthorityOwner\s+\w+")
        self.assertNotRegex(process, r"unique_ptr<.*Journal")
        self.assertNotIn("JournalAuthority", process)
        self.assertIn("ProcessLaunchAuthority(const ProcessLaunchAuthority&) = delete", process)

    def test_borrow_lifetime_and_shutdown_order(self):
        borrow = self.cpp[self.cpp.index("BorrowTicket borrow_for_process"):
                          self.cpp.index("bool shutdown_ordered")]
        self.assertIn("return borrow(BorrowKind::kProcess)", borrow)
        self.assertIn("try_acquire", self.cpp)
        shutdown = self.cpp[self.cpp.index("bool shutdown_ordered"):
                            self.cpp.index("SupervisorStartupHandoff startup")]
        order = [
            "pipe_call.close_admission", "pipe_call.wait_drained", "pipe.stop",
            "stop_admission", "process_fence.drain", "children.drain",
            "wait_process_drained", "wait_all_drained", "leases.clear",
            "journal_owner.reset",
        ]
        self.assertEqual([shutdown.index(item) for item in order],
                         sorted(shutdown.index(item) for item in order))
        self.assertIn("No owner lock is held", self.cpp)
        self.assertIn("std::condition_variable", self.borrow)
        self.assertIn("std::atomic<std::uint32_t> state", self.borrow)

    def test_shutdown_timeout_is_finite_fail_stop(self):
        self.assertIn("kShutdownWaitMs = 250", self.borrow)
        self.assertIn("wait_for", self.cpp)
        self.assertIn("if (!shutdown_ordered()) std::terminate()", self.cpp)
        self.assertIn("same-thread or timed-out drain", self.cpp)

    def test_pipe_call_fence_closes_and_joins_before_ticket_release(self):
        fence = self.cpp[self.cpp.index("struct PipeCallFence"):
                         self.cpp.index("struct ChildRegistry")]
        for token in ("kClosing", "kActiveMask", "state.load", "compare_exchange_weak",
                      "state.fetch_or", "changed.wait_for", "kShutdownWaitMs"):
            self.assertIn(token, fence)
        shutdown = self.cpp[self.cpp.index("bool shutdown_ordered"):
                            self.cpp.index("SupervisorStartupHandoff startup")]
        self.assertLess(shutdown.index("pipe_call.close_admission"),
                        shutdown.index("pipe_call.wait_drained"))
        self.assertLess(shutdown.index("pipe_call.wait_drained"),
                        shutdown.index("pipe.stop"))

    def test_legacy_parallel_types_and_callbacks_are_removed(self):
        for pattern in (
            r"\bJournalState\b", r"\bDispatchStatus\b",
            r"\bDurableLoadResult\b", r"\bJournalOutcome\b",
            r"\bJournalRecord\b", r"\bDurableJournalAdapter\b",
            r"\bJournalAuthority\b", r"durable_journal_authorize",
            r"JournalAuthorityOwner::recover\s*\(",
            r"JournalAuthorityOwner::begin\s*\(", r"dispatch_owned",
        ):
            self.assertIsNone(re.search(pattern, self.cpp), pattern)

    def test_no_public_issuer_factory_friend_or_mint(self):
        for text in (self.hpp, self.cpp, self.launch_header):
            for token in ("CapabilityIssuer", "issue_capability", "mint"):
                self.assertNotIn(token, text)
        self.assertNotIn("IssuedCapability", self.hpp)
        self.assertIn("friend class LaunchAuthorityIssuer", self.launch_header)
        self.assertIn("LaunchAuthorityIssuer() = delete", self.launch_header)

    def test_start_shutdown_and_metadata_are_refusal_only(self):
        for method in ("Status Authority::start", "Status Authority::shutdown"):
            start = self.cpp.index(method)
            end = self.cpp.find("Status Authority::", start + len(method))
            body = self.cpp[start:] if end < 0 else self.cpp[start:end]
            self.assertIn("return Status::kUnavailable", body)
        self.assertIn("RedactedReceipt Authority::loopback_metadata", self.cpp)
        self.assertIn("Status::kUnavailable, 0, false, false", self.cpp)

    def test_current_qa_inventory_contains_this_test(self):
        inventory = (ROOT / "scripts/test/run_qa.py").read_text(encoding="utf-8")
        self.assertIn('"tests/native/test_windows_supervisor_authority_static.py": "native_static"', inventory)


if __name__ == "__main__":
    unittest.main()
