"""Hostile source/model checks for the inert process-dispatch lease slice.

No Windows APIs, compiler, process, pipe, journal helper, network, or model
are executed here. The small model intentionally checks only the lease
protocol's fail-closed ordering and bounded state claims.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "native" / "action_journal_helper"
OWNER = HELPER / "journal_authority_owner.hpp"
STORE = HELPER / "store_codec.cpp"
CONTRACT = ROOT / "contracts" / "action-journal-helper" / "process-dispatch-lease-v0.1.0.json"
MAX_BYTES = 256 * 1024


def text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError(f"oversized source: {path}")
    return raw.decode("utf-8", errors="strict")


def strict_json(path: Path):
    return json.loads(text(path), object_pairs_hook=lambda pairs: dict(pairs))


class LeaseModel:
    """Bounded state model for hostile ordering, not a runtime implementation."""

    def __init__(self, maximum=8):
        self.registry = {}
        self.maximum = maximum
        self.unknown = False
        self.poisoned = False

    def acquire(self, operation, authorized=True, binding=True):
        if self.poisoned or self.unknown:
            return "unknown_manual_blocked"
        if not binding or not authorized:
            return "binding_mismatch"
        if operation in self.registry:
            return "already_leased"
        if len(self.registry) >= self.maximum:
            return "lease_limit"
        self.registry[operation] = {"dispatching": False, "started": False, "terminal": False}
        return "ok"

    def persist(self, operation, exact_readback=True):
        lease = self.registry.get(operation)
        if lease is None:
            return "mutation_conflict"
        if lease["dispatching"] or lease["terminal"]:
            return "one_shot_used"
        if not exact_readback:
            return "readback_mismatch"
        lease["dispatching"] = True
        return "ok"

    def begin_dispatch(self, operation):
        lease = self.registry.get(operation)
        if lease is None or not lease["dispatching"] or lease["terminal"]:
            return "invalid_state"
        lease["started"] = True
        return "ok"

    def mutation(self, operation):
        if operation in self.registry:
            return "mutation_conflict"
        if self.unknown:
            return "unknown_manual_blocked"
        return "ok"

    def lost_ack_lookup(self, operation, exact_ack=False):
        if exact_ack:
            return "ok"
        return "lost_acknowledgement"

    def abandon(self, operation):
        lease = self.registry.pop(operation, None)
        if lease and lease["dispatching"] and not lease["terminal"]:
            self.poisoned = True

    def mark_unknown(self, operation):
        lease = self.registry.get(operation)
        if lease is None or not lease["started"]:
            return "invalid_state"
        lease["terminal"] = True
        self.unknown = True
        return "ambiguous_no_replay"


class ProcessDispatchLeaseStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = text(OWNER)
        cls.store = text(STORE)
        cls.contract = strict_json(CONTRACT)

    def test_binding_is_exact_and_operation_id_stays_16_bytes(self):
        self.assertIn("std::array<std::uint8_t, 16> operation_id", self.owner)
        self.assertIn("coerce_to_uint64\": false", text(CONTRACT))
        for field in (
            "request_ref", "call_ref", "tool", "risk", "side_effect",
            "args_digest", "preview_digest", "operation_digest",
            "authorization_kind", "authorized_sequence",
            "authorized_receipt_digest", "authorized_event_digest",
        ):
            self.assertIn(field, self.owner)
        self.assertIn("binding.authorized_sequence != 1", self.store)
        self.assertIn("process_binding_valid", self.store)

    def test_acquisition_requires_read_only_reload_and_authorized_sequence_one(self):
        acquire = self.store.index("JournalAuthorityOwner::acquire_process_dispatch_lease")
        reload_at = self.store.index("store_.reload(io)", acquire)
        unknown = self.store.index("unknown_manual_present_locked()", reload_at)
        binding = self.store.index("process_binding_valid", unknown)
        records = self.store.index("store_.records_.find(operation)", binding)
        self.assertLess(reload_at, unknown)
        self.assertLess(unknown, binding)
        self.assertLess(binding, records)
        self.assertIn('authorized.state != "authorized"', self.store[records:])
        self.assertIn('authorized.sequence != binding.authorized_sequence', self.store[records:])

    def test_second_lease_and_mutation_race_fail_closed(self):
        model = LeaseModel()
        self.assertEqual(model.acquire("act_" + "a" * 32), "ok")
        self.assertEqual(model.acquire("act_" + "a" * 32), "already_leased")
        self.assertEqual(model.mutation("act_" + "a" * 32), "mutation_conflict")
        self.assertIn("leased_operations_", self.owner)
        self.assertIn("lease_conflict_locked", self.store)
        self.assertIn("StoreStatus::kMutationConflict", self.store)

    def test_one_shot_transition_exact_readback_and_barrier(self):
        model = LeaseModel()
        operation = "act_" + "b" * 32
        self.assertEqual(model.acquire(operation), "ok")
        self.assertEqual(model.persist(operation, exact_readback=False), "readback_mismatch")
        self.assertEqual(model.persist(operation), "ok")
        self.assertEqual(model.persist(operation), "one_shot_used")
        self.assertEqual(model.begin_dispatch(operation), "ok")
        dispatch = self.store.index("ProcessDispatchLeaseStatus JournalAuthorityOwner::persist_dispatching")
        barrier = self.store.index("lease.dispatching_persisted_ = true", dispatch)
        external = self.store.index("ProcessDispatchLeaseStatus ProcessDispatchLease::begin_external_dispatch")
        self.assertLess(dispatch, barrier)
        self.assertLess(barrier, external)
        self.assertIn('event.state != expected_state', self.store)
        self.assertIn('event.action != method', self.store)

    def test_lost_ack_is_read_only_and_never_retries(self):
        self.assertIn("lookup_lost_ack", self.owner)
        lookup = self.store.index("JournalAuthorityOwner::lookup_lost_ack")
        self.assertIn("store_.reload(io)", self.store[lookup:])
        tail = self.store[lookup:]
        self.assertIn('event.state != "acknowledged"', tail)
        self.assertIn("kLostAcknowledgement", tail)
        self.assertNotIn("persist_dispatching(lease", tail)
        self.assertNotIn("begin_external_dispatch", tail)

    def test_ambiguity_poison_and_global_unknown_block(self):
        model = LeaseModel()
        operation = "act_" + "c" * 32
        self.assertEqual(model.acquire(operation), "ok")
        self.assertEqual(model.persist(operation), "ok")
        self.assertEqual(model.begin_dispatch(operation), "ok")
        self.assertEqual(model.mark_unknown(operation), "ambiguous_no_replay")
        self.assertEqual(model.mutation("act_" + "d" * 32), "unknown_manual_blocked")
        model2 = LeaseModel()
        self.assertEqual(model2.acquire(operation), "ok")
        self.assertEqual(model2.persist(operation), "ok")
        model2.abandon(operation)
        self.assertTrue(model2.poisoned)
        self.assertIn("poisoned_.store(true", self.store)
        self.assertIn("ProcessDispatchLeaseStatus::kAmbiguousNoReplay", self.store)
        self.assertIn("kUnknownManualBlocked", self.store)

    def test_registry_bounded_and_lease_noncopyable(self):
        model = LeaseModel()
        for index in range(8):
            self.assertEqual(model.acquire(f"act_{index:032x}"), "ok")
        self.assertEqual(model.acquire("act_" + "e" * 32), "lease_limit")
        self.assertIn("kMaxLeasedProcessDispatches = 8", self.owner)
        self.assertIn("ProcessDispatchLease(const ProcessDispatchLease&) = delete", self.owner)
        self.assertIn("ProcessDispatchLease(ProcessDispatchLease&&) = delete", self.owner)
        self.assertIn("~ProcessDispatchLease() noexcept", self.owner)

    def test_no_secrets_raw_json_or_topology_activation(self):
        lease_region = self.owner[self.owner.index("class ProcessDispatchLease"):self.owner.index("class JournalAuthorityOwner final")]
        for forbidden in ("hmac_key", "session_nonce", "process_handle", "nlohmann::json"):
            self.assertNotIn(forbidden, lease_region)
        for key in ("production_available", "native_target_registered", "node_integration_added",
                    "cmake_added", "package_added", "activation_permitted"):
            self.assertIs(self.contract[key], False)
        for forbidden in ("CreateProcess", "ShellExecute", "add_executable", "target_link_libraries"):
            self.assertNotIn(forbidden.lower(), (self.owner + self.store).lower())
        self.assertIn("owner_mutex_never_held_over_process_pipe_or_provider_wait", text(CONTRACT))


if __name__ == "__main__":
    unittest.main()
