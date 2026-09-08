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


def cpp_constructor_signature(source: str, marker: str) -> tuple[str, bool]:
    """Extract one balanced constructor signature and its noexcept marker."""

    # The public copy/move declarations contain the same class-name prefix;
    # use the final private constructor declaration/definition rather than a
    # first-match regex that could silently compare the wrong overload.
    start = source.rindex(marker)
    opening = source.index("(", start)
    depth = 0
    closing = None
    for index in range(opening, len(source)):
        character = source[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing is None:
        raise AssertionError(f"unterminated constructor signature: {marker}")
    suffix = source[closing + 1:]
    tokens = suffix.lstrip().split(None, 1)
    signature = " ".join(source[start:closing + 1].split())
    signature = signature.replace("( ", "(").replace(" )", ")")
    return signature, bool(tokens and tokens[0] == "noexcept")


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
        self.registry[operation] = {"dispatching": False, "started": False, "terminal": False, "state": "authorized"}
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
        lease["state"] = "dispatching"
        return "ok"

    def acknowledge(self, operation):
        lease = self.registry.get(operation)
        if lease is None or not lease["started"] or lease["terminal"]:
            return "invalid_state"
        lease["state"] = "acknowledged"
        lease["external_proof"] = ("receipt-a", "event-a")
        return "ok"

    def reconcile(self, operation):
        lease = self.registry.get(operation)
        if lease is None or not lease["started"] or lease["terminal"]:
            return "invalid_state"
        lease["state"] = "reconciling"
        return "ok"

    def complete(self, operation, proof):
        lease = self.registry.get(operation)
        if lease is None or lease.get("state") != "reconciling":
            return "invalid_state"
        if proof != lease.get("external_proof"):
            return "binding_mismatch"
        lease["terminal"] = True
        lease["state"] = "completed"
        return "ok"

    def mutation(self, operation):
        if operation in self.registry:
            return "mutation_conflict"
        if self.unknown:
            return "unknown_manual_blocked"
        return "ok"

    def lost_ack_lookup(self, operation, outcome="not_found",
                        proof=("receipt-a", "event-a")):
        lease = self.registry.get(operation)
        if lease is None:
            return "mutation_conflict"
        if lease["terminal"] or lease["state"] == "acknowledged":
            return "one_shot_used"
        if not lease["started"]:
            return "invalid_state"
        if outcome == "exact_ack":
            if proof != ("receipt-a", "event-a"):
                self.poisoned = True
                return "readback_mismatch"
            lease["state"] = "acknowledged"
            lease["external_proof"] = proof
            return "ok"
        self.poisoned = True
        return "readback_mismatch" if outcome in {"corrupt", "divergent"} else "lost_acknowledgement"

    def abandon(self, operation):
        lease = self.registry.pop(operation, None)
        if lease and lease["dispatching"] and not lease["terminal"]:
            self.poisoned = True

    def post_precheck_failure(self, operation, code):
        """Model an internal result after exact lease/authority prechecks."""
        if operation not in self.registry:
            return "mutation_conflict"
        if code == "mutation_conflict":
            self.poisoned = True
            return "mutation_conflict"
        if code in {"not_found", "invalid_transition", "authority_divergence",
                    "storage_failure", "readback_failure"}:
            self.poisoned = True
            return "storage_failure"
        return "invalid_state"

    def allocation_failure(self, phase):
        """Model any bounded allocation/hash/JSON failure at one lease phase."""
        if phase not in {"constructor", "acquire", "persist_dispatching",
                         "acknowledge", "begin_reconciliation", "complete",
                         "mark_unknown", "proof_hash"}:
            return "invalid_state"
        self.poisoned = True
        return "storage_failure"

    def mark_unknown(self, operation):
        lease = self.registry.get(operation)
        if lease is None or not lease["started"] or lease.get("state") not in {"dispatching", "acknowledged", "reconciling"}:
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
        self.assertIn('event.receipt_digest != expected_receipt', self.store)
        self.assertIn('process_transition_receipt(', self.store)

    def test_lost_ack_is_read_only_and_never_retries(self):
        self.assertIn("lookup_lost_ack", self.owner)
        lookup = self.store.index("JournalAuthorityOwner::lookup_lost_ack")
        self.assertIn("store_.reload(io)", self.store[lookup:])
        tail = self.store[lookup:]
        self.assertIn('event.state != "acknowledged"', tail)
        self.assertIn("kLostAcknowledgement", tail)
        self.assertNotIn("persist_dispatching(lease", tail)
        self.assertNotIn("begin_external_dispatch", tail)
        self.assertNotIn("acknowledge_external(lease", tail)

    def test_lost_ack_requires_external_start_and_recovers_exact_proof(self):
        operation = "act_" + "e" * 32
        model = LeaseModel()
        self.assertEqual(model.acquire(operation), "ok")
        self.assertEqual(model.persist(operation), "ok")
        self.assertEqual(model.lost_ack_lookup(operation, "exact_ack"),
                         "invalid_state")
        self.assertFalse(model.poisoned)
        self.assertEqual(model.begin_dispatch(operation), "ok")
        self.assertEqual(model.lost_ack_lookup(operation, "exact_ack",
                                               ("wrong", "proof")),
                         "readback_mismatch")
        self.assertTrue(model.poisoned)

        model = LeaseModel()
        self.assertEqual(model.acquire(operation), "ok")
        self.assertEqual(model.persist(operation), "ok")
        self.assertEqual(model.begin_dispatch(operation), "ok")
        self.assertEqual(model.lost_ack_lookup(operation, "exact_ack"), "ok")
        self.assertEqual(model.lost_ack_lookup(operation, "exact_ack"),
                         "one_shot_used")
        self.assertEqual(model.reconcile(operation), "ok")
        self.assertEqual(model.complete(operation, ("receipt-a", "event-a")),
                         "ok")

        lookup = self.store.index("JournalAuthorityOwner::lookup_lost_ack")
        tail = self.store[lookup:]
        self.assertIn("const ProcessExternalProof& external_proof", tail)
        self.assertIn("!lease.external_started_", tail)
        self.assertIn("lease.acknowledged_ = true", tail)
        self.assertIn("lease.acknowledged_receipt_digest_ = external_proof.external_receipt_digest", tail)
        self.assertIn("lease.acknowledged_event_digest_ = external_proof.external_event_digest", tail)
        self.assertIn("process_transition_receipt(", tail)
        self.assertIn("event.receipt_digest != expected_receipt", tail)

    def test_lost_ack_failures_poison_after_authoritative_lookup(self):
        for outcome in ("not_found", "invalid", "corrupt", "divergent"):
            model = LeaseModel()
            operation = "act_" + outcome[0] * 32
            self.assertEqual(model.acquire(operation), "ok")
            self.assertEqual(model.persist(operation), "ok")
            self.assertEqual(model.begin_dispatch(operation), "ok")
            result = model.lost_ack_lookup(operation, outcome)
            self.assertIn(result, {"lost_acknowledgement", "readback_mismatch"})
            self.assertTrue(model.poisoned, outcome)
            self.assertEqual(model.acquire("act_" + "z" * 32), "unknown_manual_blocked")
        # A caller-side lease/owner mismatch occurs before the authoritative
        # lookup and remains a non-poisoning conflict.
        model = LeaseModel()
        self.assertEqual(model.lost_ack_lookup("not-registered"), "mutation_conflict")
        self.assertFalse(model.poisoned)

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

    def test_unknown_is_valid_from_all_post_dispatch_history_states(self):
        for state in ("dispatching", "acknowledged", "reconciling"):
            model = LeaseModel()
            operation = "act_" + state[0] * 32
            self.assertEqual(model.acquire(operation), "ok")
            self.assertEqual(model.persist(operation), "ok")
            self.assertEqual(model.begin_dispatch(operation), "ok")
            if state == "acknowledged":
                self.assertEqual(model.acknowledge(operation), "ok")
            elif state == "reconciling":
                self.assertEqual(model.reconcile(operation), "ok")
            self.assertEqual(model.mark_unknown(operation), "ambiguous_no_replay")
        self.assertIn('current_state == "acknowledged"', self.store)
        self.assertIn('current_state == "reconciling"', self.store)

    def test_external_begin_is_admitted_and_atomic_against_close_poison_unknown(self):
        begin = self.store.index("JournalAuthorityOwner::begin_external_dispatch")
        region = self.store[begin:]
        self.assertIn("ActiveBorrow borrow(*this)", region)
        self.assertIn("admission_closing()", region)
        self.assertIn("poisoned_.load", region)
        self.assertIn("unknown_manual_present_locked()", region)
        self.assertIn("lease.external_started_ = true", region)
        self.assertLess(region.index("unknown_manual_present_locked()"), region.index("lease.external_started_ = true"))

    def test_terminal_registry_attachment_prevents_owner_uaf(self):
        transition = self.store.index("ProcessDispatchLeaseStatus JournalAuthorityOwner::transition_lease")
        terminal = self.store.index("lease.terminal_ = true", transition)
        self.assertNotIn("leased_operations_.erase", self.store[terminal:terminal + 220])
        self.assertIn("Keep terminal entries attached", self.store)
        self.assertIn("if (!leased_operations_.empty())", self.store)
        self.assertIn("owner_attached_", self.owner)

    def test_registry_insert_is_transactional_before_lease_destructor_callback(self):
        insertion = self.store.index("const auto inserted = leased_operations_.emplace")
        attachment = self.store.index("candidate->owner_attached_ = true", insertion)
        self.assertLess(insertion, attachment)
        self.assertIn("failed insertion", self.store)
        destructor = self.owner.index("~ProcessDispatchLease() noexcept")
        self.assertIn("owner_attached_", self.owner[destructor:])

    def test_completion_is_exactly_the_acknowledged_external_proof(self):
        self.assertIn("acknowledged_receipt_digest_", self.owner)
        self.assertIn("acknowledged_event_digest_", self.owner)
        self.assertIn("receipt_digest != lease.acknowledged_receipt_digest_", self.store)
        self.assertIn("event_digest != lease.acknowledged_event_digest_", self.store)
        self.assertIn("lease.acknowledged_ = true", self.store)
        model = LeaseModel()
        operation = "act_" + "f" * 32
        self.assertEqual(model.acquire(operation), "ok")
        self.assertEqual(model.persist(operation), "ok")
        self.assertEqual(model.begin_dispatch(operation), "ok")
        self.assertEqual(model.acknowledge(operation), "ok")
        self.assertEqual(model.reconcile(operation), "ok")
        self.assertEqual(model.complete(operation, ("receipt-b", "event-b")), "binding_mismatch")
        self.assertEqual(model.complete(operation, ("receipt-a", "event-a")), "ok")

    def test_fail_definitive_is_not_a_post_dispatch_lease_transition(self):
        fail = self.store.index('std::strcmp(method, "fail_definitive")')
        self.assertIn("return ProcessDispatchLeaseStatus::kInvalidState", self.store[fail:])
        self.assertIn("pre-dispatch-only", text(ROOT / "native/action_journal_helper/JOURNAL_AUTHORITY_OWNER.md"))

    def test_storage_reload_and_readback_failures_latch_poison(self):
        self.assertIn("poison_for(preflight)", self.store)
        transition = self.store.index("ProcessDispatchLeaseStatus JournalAuthorityOwner::transition_lease")
        self.assertIn("poisoned_.store(true", self.store[transition:])
        lookup = self.store.index("ProcessDispatchLeaseStatus JournalAuthorityOwner::lookup_lost_ack")
        self.assertIn("poisoned_.store(true", self.store[lookup:])
        fail = self.store.index('std::strcmp(method, "fail_definitive")')
        self.assertIn("return ProcessDispatchLeaseStatus::kInvalidState", self.store[fail:])

    def test_noexcept_boundaries_cover_all_lease_allocation_and_proof_paths(self):
        # Constructors that copy strings/containers must let acquisition's
        # catch boundary observe allocation failure; noexcept would terminate.
        self.assertNotIn("std::string operation_text) noexcept", self.owner)
        owner_decl, owner_decl_noexcept = cpp_constructor_signature(
            self.owner, "JournalAuthorityOwner(")
        owner_def, owner_def_noexcept = cpp_constructor_signature(
            self.store, "JournalAuthorityOwner::JournalAuthorityOwner(")
        self.assertEqual(owner_decl, owner_def.replace("JournalAuthorityOwner::", ""))
        self.assertEqual(owner_decl_noexcept, owner_def_noexcept)
        self.assertFalse(owner_decl_noexcept)
        open_start = self.store.index("JournalAuthorityOwner::open")
        self.assertLess(self.store.index("try {", open_start),
                        self.store.index("receipt = {}", open_start))
        self.assertLess(self.store.index("try {", open_start),
                        self.store.index("acquire_storage(", open_start))
        for marker in (
            "JournalAuthorityOwner::persist_dispatching",
            "JournalAuthorityOwner::acknowledge_external",
            "ProcessDispatchLease::begin_reconciliation",
            "ProcessDispatchLease::complete_external",
            "ProcessDispatchLease::mark_unknown",
        ):
            start = self.store.index(marker)
            body = self.store.index("nlohmann::json body", start)
            self.assertLess(self.store.index("try {", start), body, marker)
            self.assertIn("catch (...)", self.store[body:body + 1600], marker)
            self.assertIn("poisoned_.store(true", self.store[body:body + 1600], marker)
        transition = self.store.index("JournalAuthorityOwner::transition_lease")
        self.assertIn("process_transition_receipt(", self.store[transition:])
        self.assertIn("catch (...)", self.store[transition:])
        acquire = self.store.index("JournalAuthorityOwner::acquire_process_dispatch_lease")
        self.assertIn("new (std::nothrow) ProcessDispatchLease", self.store[acquire:])
        self.assertIn("catch (...)", self.store[acquire:])

    def test_post_precheck_failures_poison_and_do_not_leave_ready_owner(self):
        caller = LeaseModel()
        self.assertEqual(caller.acquire("act_" + "4" * 32, binding=False), "binding_mismatch")
        self.assertFalse(caller.poisoned)
        self.assertEqual(caller.acquire("act_" + "5" * 32), "ok")
        model = LeaseModel()
        operation = "act_" + "1" * 32
        self.assertEqual(model.acquire(operation), "ok")
        for code, expected in (
            ("not_found", "storage_failure"),
            ("invalid_transition", "storage_failure"),
            ("mutation_conflict", "mutation_conflict"),
            ("authority_divergence", "storage_failure"),
            ("storage_failure", "storage_failure"),
            ("readback_failure", "storage_failure"),
        ):
            trial = LeaseModel()
            self.assertEqual(trial.acquire(operation), "ok")
            self.assertEqual(trial.post_precheck_failure(operation, code), expected)
            self.assertTrue(trial.poisoned, code)
            self.assertEqual(trial.acquire("act_" + "2" * 32), "unknown_manual_blocked", code)
        transition = self.store.index("JournalAuthorityOwner::transition_lease")
        missing = self.store.index("current == store_.records_.end()", transition)
        self.assertIn("poisoned_.store(true", self.store[missing:missing + 420])
        invalid = self.store.index("if (!current_state_valid)", missing)
        self.assertIn("poisoned_.store(true", self.store[invalid:invalid + 360])
        mutation = self.store.index("if (status != StoreStatus::kOk", transition)
        mutation_end = self.store.index("return status ==", mutation)
        mutation_region = self.store[mutation:mutation_end]
        self.assertIn("poisoned_.store(true", mutation_region)
        self.assertNotIn("status != StoreStatus::kInvalidTransition", mutation_region)
        self.assertNotIn("status != StoreStatus::kNotFound", mutation_region)

    def test_exception_injection_model_is_finite_and_sticky_for_each_lease_phase(self):
        for phase in ("constructor", "acquire", "persist_dispatching",
                      "acknowledge", "begin_reconciliation", "complete",
                      "mark_unknown", "proof_hash"):
            model = LeaseModel()
            self.assertEqual(model.allocation_failure(phase), "storage_failure", phase)
            self.assertTrue(model.poisoned, phase)
            self.assertEqual(model.acquire("act_" + "3" * 32), "unknown_manual_blocked", phase)
        transition = self.store.index("JournalAuthorityOwner::transition_lease")
        receipt = self.store.index(
            "const std::string expected_receipt = process_transition_receipt(",
            transition,
        )
        self.assertLess(self.store.index("try {", transition), receipt)
        self.assertIn("catch (...)", self.store[receipt:])

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
