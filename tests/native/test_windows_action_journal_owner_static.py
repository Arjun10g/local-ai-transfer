"""Static/reference checks for the private helper journal authority owner.

These checks do not compile or execute the Windows helper.  The tiny model
only exercises ownership and startup ordering claims against bounded source
and contract text.
"""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "native" / "action_journal_helper"
OWNER_HPP = HELPER / "journal_authority_owner.hpp"
STORE_HPP = HELPER / "store_codec.hpp"
STORE_CPP = HELPER / "store_codec.cpp"
PIPE_CPP = HELPER / "pipe_server.cpp"
CONTRACT = ROOT / "contracts" / "action-journal-helper" / "owner-v0.1.0.json"
MAX_BYTES = 256 * 1024


def bounded_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError(f"oversized source: {path}")
    return raw.decode("utf-8", errors="strict")


def strict_json(path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError(f"oversized contract: {path}")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)


def startup_model(*, owner_opened: bool, recovered: bool, pipe_requested: bool):
    """Model the only safe publication path, without touching Windows APIs."""
    trace = []
    if owner_opened:
        trace.append("owner_open")
    if not owner_opened or not recovered:
        return False, trace
    trace.append("owner_recovered")
    if pipe_requested:
        trace.append("pipe_publish")
    return pipe_requested, trace


def owner_interleaving_model(*, active: bool, shutdown: bool, poisoned: bool,
                             event: str):
    """Small lock/admission model for adversarial ordering only."""
    if event == "complete_after_shutdown":
        return "internal", False, True, poisoned
    if shutdown or poisoned:
        return "internal", active, shutdown, poisoned
    if event == "shutdown":
        # Publishing admission stop precedes waiting for the active borrow.
        return "waiting" if active else "closed", active, True, poisoned
    if event == "exception":
        return "internal", False, shutdown, True
    return "ok", False, shutdown, poisoned


def latched_monitor_model(*, monitor_signaled: bool, original_closed: bool,
                          fresh_probe: bool) -> bool:
    """Original handle teardown cannot erase a monitor's latched signal."""
    del original_closed, fresh_probe
    return monitor_signaled


ADMISSION_CLOSING = 1 << 63
ADMISSION_COUNT_MASK = (1 << 32) - 1


def admission_model(word: int, event: str, maximum: int = 4096):
    """CAS-word oracle: closing and borrower count linearize together."""
    count = word & ADMISSION_COUNT_MASK
    if word & ~(ADMISSION_CLOSING | ADMISSION_COUNT_MASK):
        return word, False
    if event == "acquire":
        if word & ADMISSION_CLOSING or count >= maximum:
            return word, False
        return word + 1, True
    if event == "close":
        return word | ADMISSION_CLOSING, True
    if event == "release":
        if count == 0:
            return word, False
        return (word & ~ADMISSION_COUNT_MASK) | (count - 1), True
    raise ValueError(event)


class WindowsActionJournalOwnerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = bounded_text(OWNER_HPP)
        cls.store_header = bounded_text(STORE_HPP)
        cls.store = bounded_text(STORE_CPP)
        cls.pipe = bounded_text(PIPE_CPP)
        cls.contract = strict_json(CONTRACT)

    def test_contract_is_private_source_only_and_gated(self):
        for key in (
            "production_available", "native_target_registered", "node_integration_added",
            "cmake_added", "package_added", "activation_permitted",
        ):
            self.assertIs(self.contract[key], False)
        self.assertEqual(self.contract["construction"]["factory"], "JournalAuthorityOwner::open only")
        self.assertIs(self.contract["construction"]["caller_storage_acquisition"], False)
        self.assertIs(self.contract["construction"]["caller_store_construction"], False)
        self.assertIs(self.contract["construction"]["returns_ready_only_after_recovery"], True)
        self.assertEqual(self.contract["borrowed_boundary"]["supervisor_bridge"], "absent in this phase")

    def test_owner_is_the_only_storage_acquisition_and_store_construction_boundary(self):
        self.assertEqual(self.store.count("action_journal_storage::acquire_storage"), 1)
        self.assertNotIn("action_journal_storage::acquire_storage", self.pipe)
        self.assertNotIn("FixedContainerStore store", self.pipe)
        self.assertIn("JournalAuthorityOwner::open", self.pipe)
        self.assertIn("owner->apply(request, request_io, result)", self.pipe)
        self.assertEqual(self.store.count("store_(lease_, container_id)"), 1)
        self.assertEqual(self.store.count("FixedContainerStore::FixedContainerStore("), 1)
        self.assertIn("JournalStorageLease& lease", self.store_header)
        self.assertIn("JournalStorageLease* lease_ = nullptr", self.store_header)
        self.assertIn("friend class JournalAuthorityOwner", self.store_header)

    def test_lease_outlives_store_and_owner_has_no_public_arbitrary_constructor(self):
        self.assertLess(
            self.owner.index("JournalStorageLease lease_;"),
            self.owner.index("FixedContainerStore store_;"),
        )
        self.assertIn("JournalAuthorityOwner(action_journal_storage::JournalStorageLease&&", self.owner)
        public_region = self.owner[:self.owner.index(" private:")]
        private_region = self.owner[self.owner.index(" private:"):]
        self.assertNotIn("JournalAuthorityOwner(action_journal_storage", public_region)
        self.assertIn("JournalAuthorityOwner(action_journal_storage", private_region)
        self.assertIn("store_(lease_, container_id)", self.store)
        self.assertIn("lease_(&lease)", self.store)
        self.assertIn("if (lease_ == nullptr || !lease_->valid()", self.store)

    def test_owner_solely_owns_recovery_and_poison_state(self):
        self.assertIn("std::uint32_t recovery_count_ = 0;", self.owner)
        self.assertIn("std::atomic_bool poisoned_{false};", self.owner)
        self.assertNotIn("std::uint32_t recovery_count_ = 0;", self.store_header)
        self.assertNotIn("bool poisoned_ = false;", self.store_header)
        store_impl = self.store.split("const char* store_status_name", 1)[0]
        self.assertNotIn("poisoned_", store_impl)
        self.assertIn("result.body[\"recovery_count\"] = recovery_count_;", self.store)
        self.assertIn("owner->store_.load_and_recover", self.store)
        recovery_call = self.store.index("owner->store_.load_and_recover")
        self.assertLess(
            self.store.rfind("std::lock_guard<std::mutex>", 0, recovery_call),
            recovery_call,
        )

    def test_open_acquires_once_recovers_before_ready_and_fails_closed(self):
        acquire = self.store.index("action_journal_storage::acquire_storage")
        recover = self.store.index("owner->store_.load_and_recover")
        ready = self.store.index("owner->recovered_ = true")
        returned = self.store.index("return owner", ready)
        self.assertLess(acquire, recover)
        self.assertLess(recover, ready)
        self.assertLess(ready, returned)
        self.assertIn("if (recovery != StoreStatus::kOk)", self.store)
        self.assertIn("return nullptr", self.store[self.store.index("if (recovery != StoreStatus::kOk"):ready])
        self.assertIn("if (io.stop_requested())", self.store)
        self.assertLess(self.store.index("owner->recovered_ = true"), self.store.index("return owner", ready))

    def test_pipe_publication_is_after_owner_open_and_final_startup_probe(self):
        opened = self.pipe.index("JournalAuthorityOwner::open")
        failed = self.pipe.index("if (!owner) return authority_status(owner_status)", opened)
        final_probe = self.pipe.index("if (startup_io.stop_requested())", failed)
        create_pipe = self.pipe.index("CreateNamedPipeW", final_probe)
        self.assertLess(opened, failed)
        self.assertLess(failed, final_probe)
        self.assertLess(final_probe, create_pipe)
        self.assertNotIn("store.load_and_recover", self.pipe)

    def test_owner_serializes_only_store_application_and_poison_is_sticky(self):
        for token in (
            "std::mutex", "std::lock_guard<std::mutex>",
            "const auto status = store_.apply(request, io, result)",
            "poison_for(status)", "if (admission_closing()",
            "std::lock_guard<std::mutex> lock(mutex_)",
        ):
            self.assertIn(token, self.owner + self.store)
        self.assertIn("catch (const std::bad_alloc&)", self.store)
        self.assertIn("poisoned_.store(true", self.store)
        self.assertIs(self.contract["serialization"]["writer_count"], 1)
        self.assertIn("pipe reads/writes", self.contract["borrowed_boundary"]["pipe_io"])
        self.assertIn("reads/writes remain outside", bounded_text(HELPER / "JOURNAL_AUTHORITY_OWNER.md"))
        self.assertIn("return StoreStatus::kInternal", self.store)

    def test_shutdown_closes_admission_waits_store_and_getters_fail_closed(self):
        self.assertIn("bool begin_shutdown() noexcept", self.owner)
        self.assertIn("admission_.compare_exchange_weak", self.store)
        self.assertIn("kAdmissionClosing", self.owner)
        self.assertIn("kMaxActiveBorrows = 4096", self.owner)
        self.assertNotIn("shutdown_requested_", self.owner + self.store)
        self.assertNotIn("active_borrows_", self.owner + self.store)
        shutdown = self.store[
            self.store.index("bool JournalAuthorityOwner::begin_shutdown"):
            self.store.index("bool JournalAuthorityOwner::ready")
        ]
        self.assertIn("admission_.compare_exchange_weak", shutdown)
        self.assertIn("wait_for_borrowers()", shutdown)
        self.assertIn("WaitOnAddress", self.store)
        self.assertIn("ERROR_TIMEOUT", self.store)
        self.assertIn("shutting_down_ = true", shutdown)
        self.assertIn("return false;", self.store[self.store.index("bool JournalAuthorityOwner::ready"):])
        self.assertIn("poisoned_.load", self.store[self.store.index("bool JournalAuthorityOwner::poisoned"):])
        self.assertIn("UINT32_MAX", self.store[self.store.index("std::uint32_t JournalAuthorityOwner::recovery_count"):])
        self.assertIn("~JournalAuthorityOwner() noexcept", self.store)
        apply = self.store[
            self.store.index("StoreStatus JournalAuthorityOwner::apply"):
            self.store.index("bool JournalAuthorityOwner::begin_shutdown")
        ]
        self.assertLess(apply.index("ActiveBorrow borrow(*this)"),
                        apply.index("std::unique_lock<std::mutex> lock"))
        self.assertIn("try_acquire_borrow", self.owner + self.store)
        self.assertIn("release_borrow", self.owner + self.store)
        self.assertIn("poisoned_.store(true", apply[apply.rindex("catch (...)"):])

    def test_single_admission_word_has_linearized_close_and_bounded_overflow(self):
        word, admitted = admission_model(0, "acquire")
        self.assertTrue(admitted)
        word, closed = admission_model(word, "close")
        self.assertTrue(closed)
        word_after, admitted_after = admission_model(word, "acquire")
        self.assertFalse(admitted_after)
        self.assertEqual(word_after, word)
        word, released = admission_model(word, "release")
        self.assertTrue(released)
        self.assertEqual(word & ADMISSION_COUNT_MASK, 0)
        self.assertEqual(word & ADMISSION_CLOSING, ADMISSION_CLOSING)
        near_max = 4095
        word, admitted = admission_model(near_max, "acquire")
        self.assertTrue(admitted)
        self.assertEqual(word & ADMISSION_COUNT_MASK, 4096)
        word_after, admitted_after = admission_model(word, "acquire")
        self.assertFalse(admitted_after)
        self.assertEqual(word_after, word)
        corrupt = 1 << 40
        self.assertFalse(admission_model(corrupt, "acquire")[1])
        self.assertFalse(admission_model(0, "release")[1])

    def test_admission_interleavings_preserve_count_and_close_invariant(self):
        # Exhaustively enumerate the bounded event traces.  This is a model of
        # the CAS linearization, not a claim that source text is a runtime
        # concurrency test.
        for events in product(("acquire", "close", "release"), repeat=6):
            word = 0
            expected_count = 0
            closed = False
            for event in events:
                before = word
                word, accepted = admission_model(word, event)
                if event == "acquire":
                    if accepted:
                        expected_count += 1
                    if closed:
                        self.assertFalse(accepted)
                        self.assertEqual(word, before)
                elif event == "close":
                    closed = True
                    self.assertTrue(word & ADMISSION_CLOSING)
                elif event == "release":
                    if accepted:
                        expected_count -= 1
                self.assertEqual(word & ADMISSION_COUNT_MASK, expected_count)
                self.assertGreaterEqual(expected_count, 0)
                self.assertLessEqual(expected_count, 4096)

    def test_shutdown_and_exception_interleavings_latch_fail_closed(self):
        outcome, active, shutdown, poisoned = owner_interleaving_model(
            active=True, shutdown=False, poisoned=False, event="shutdown")
        self.assertEqual((outcome, active, shutdown, poisoned),
                         ("waiting", True, True, False))
        outcome, active, shutdown, poisoned = owner_interleaving_model(
            active=True, shutdown=True, poisoned=False,
            event="complete_after_shutdown")
        self.assertEqual((outcome, active, shutdown, poisoned),
                         ("internal", False, True, False))
        outcome, active, shutdown, poisoned = owner_interleaving_model(
            active=True, shutdown=False, poisoned=False, event="exception")
        self.assertEqual((outcome, active, shutdown, poisoned),
                         ("internal", False, False, True))
        outcome, *_ = owner_interleaving_model(
            active=False, shutdown=False, poisoned=True, event="apply")
        self.assertEqual(outcome, "internal")

    def test_owner_lock_never_executes_pipe_io_and_pipe_probes_are_outside_apply(self):
        apply = self.store[
            self.store.index("StoreStatus JournalAuthorityOwner::apply"):
            self.store.index("bool JournalAuthorityOwner::begin_shutdown")
        ]
        for token in ("PeekNamedPipe", "ReadFile", "WriteFile", "DisconnectNamedPipe",
                      "ConnectNamedPipe", "CreateNamedPipeW"):
            self.assertNotIn(token, apply)
        self.assertIn("RequestCancellationMonitor", self.pipe)
        self.assertIn("monitored_request_cancelled", self.pipe)
        apply_at = self.pipe.index("owner->apply(request, request_io, result)")
        pre = self.pipe.rfind("request_cancelled(&cancellation_context)", 0, apply_at)
        post = self.pipe.index("cancellation_monitor.cancellation_signaled()", apply_at)
        self.assertGreater(pre, -1)
        self.assertLess(pre, apply_at)
        self.assertGreater(post, apply_at)
        monitor = self.pipe[
            self.pipe.index("bool monitored_request_cancelled"):
            self.pipe.index("struct StartupCancellationContext")
        ]
        self.assertNotIn("PeekNamedPipe", monitor)
        self.assertIn("std::atomic_bool", self.pipe)
        self.assertIn("unix_time_ms() >= deadline_at_ms_", self.pipe)

    def test_monitor_owns_duplicate_handles_and_latched_signal_survives_original_close(self):
        monitor = self.pipe[
            self.pipe.index("class RequestCancellationMonitor"):
            self.pipe.index("bool monitored_request_cancelled")
        ]
        self.assertIn("DuplicateHandle", monitor)
        self.assertIn("pipe_copy", monitor)
        self.assertIn("process_copy", monitor)
        self.assertIn("pipe_.reset(pipe_copy)", monitor)
        self.assertIn("client_process_.reset(process_copy)", monitor)
        self.assertIn("std::atomic_bool cancelled_{false};", monitor)
        self.assertIn("cancelled_.store(true", monitor)
        self.assertLess(monitor.index("DuplicateHandle"), monitor.index("std::thread"))
        self.assertIn("CloseHandle(pipe_copy)", monitor)
        self.assertIn("pipe_.get()", monitor)
        self.assertIn("client_process_.get()", monitor)
        self.assertNotIn("WaitForSingleObject(client_process_,", monitor)
        self.assertNotIn("PeekNamedPipe(pipe_,", monitor)
        self.assertIn("cancellation_monitor.cancellation_signaled()", self.pipe)
        self.assertNotIn("request_cancelled(&cancellation_context)", self.pipe[
            self.pipe.index("const auto applied = owner->apply"):
            self.pipe.index("if (applied != StoreStatus::kOk)")
        ])
        self.assertTrue(latched_monitor_model(
            monitor_signaled=True, original_closed=True, fresh_probe=False))
        self.assertFalse(latched_monitor_model(
            monitor_signaled=False, original_closed=True, fresh_probe=False))

    def test_corrupt_unknown_and_poisoned_owner_cannot_publish_or_mutate(self):
        for token in (
            "kStorageCorrupt", "kRecoveryFailed", "kIoTimeout", "kIoCancelFailed",
            "kNotReady", "kPoisoned", "kInternal", "AuthorityStatus::kStorageCorrupt",
            "AuthorityStatus::kRecoveryFailed",
        ):
            self.assertIn(token, self.owner + self.store)
        self.assertEqual(startup_model(owner_opened=False, recovered=False, pipe_requested=True)[0], False)
        self.assertEqual(startup_model(owner_opened=True, recovered=False, pipe_requested=True)[0], False)
        published, trace = startup_model(owner_opened=True, recovered=True, pipe_requested=True)
        self.assertTrue(published)
        self.assertEqual(trace, ["owner_open", "owner_recovered", "pipe_publish"])

    def test_protocol_session_secrets_are_not_owner_state(self):
        self.assertNotIn("hmac_key", self.owner)
        self.assertNotIn("session_nonce", self.owner)
        self.assertIn("ProtocolSession", bounded_text(HELPER / "protocol_codec.hpp"))
        self.assertIn("protocol HMAC/nonce handling", bounded_text(HELPER / "JOURNAL_AUTHORITY_OWNER.md"))

    def test_owner_does_not_add_supervisor_or_product_surface(self):
        source = self.owner + self.store
        for forbidden in (
            "supervisor bridge", "CreateProcess", "ShellExecute", "add_executable",
            "target_link_libraries", "registry", "package", "lae-host",
        ):
            self.assertNotIn(forbidden.lower(), source.lower())
        self.assertNotIn("JournalAuthorityOwner", bounded_text(ROOT / "lae-host.mjs"))

    def test_qa_inventory_contains_owner_test(self):
        qa = bounded_text(ROOT / "scripts" / "test" / "run_qa.py")
        self.assertIn('"tests/native/test_windows_action_journal_owner_static.py": "native_static"', qa)


if __name__ == "__main__":
    unittest.main()
