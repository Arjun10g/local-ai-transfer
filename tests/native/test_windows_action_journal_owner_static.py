"""Static/reference checks for the private helper journal authority owner.

These checks do not compile or execute the Windows helper.  The tiny model
only exercises ownership and startup ordering claims against bounded source
and contract text.
"""

from __future__ import annotations

import json
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
        self.assertIn("bool poisoned_ = false;", self.owner)
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
            "poison_for(status)", "if (poisoned_) return StoreStatus::kInternal",
            "std::lock_guard<std::mutex> lock(mutex_)",
        ):
            self.assertIn(token, self.owner + self.store)
        self.assertIs(self.contract["serialization"]["writer_count"], 1)
        self.assertIn("pipe reads/writes", self.contract["borrowed_boundary"]["pipe_io"])
        self.assertIn("reads/writes remain outside", bounded_text(HELPER / "JOURNAL_AUTHORITY_OWNER.md"))
        self.assertIn("return StoreStatus::kInternal", self.store)

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
