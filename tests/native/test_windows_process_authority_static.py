"""Static/model checks for the dormant Windows process authority contract.

This test never compiles or loads the Windows sources and never launches a
process.  The model is deliberately a refusal oracle: source and target
evidence are required before any authority can be admitted.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/windows-process-authority/v1.0.0.json"
HEADER = ROOT / "native/windows_supervisor/launch_authority.hpp"
TRANSACTION = ROOT / "native/windows_supervisor/process_transaction.inc"
AUTHORITY = ROOT / "native/windows_supervisor/authority.hpp"
BROKER = ROOT / "native/windows_broker/win32_process.cpp"


def strict_json(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


class RefusalModel:
    """Small adversarial model for the pre-child admission boundary."""

    REQUIRED = (
        "executable_handle", "working_directory_handle", "containment",
        "environment", "cancellation", "issuer",
    )

    def __init__(self):
        self.create_calls = 0
        self.mutation_calls = 0

    def admit(self, proof: dict, gates: bool = False) -> str:
        if not gates or any(not proof.get(key, False) for key in self.REQUIRED):
            return "unavailable"
        self.create_calls += 1
        self.mutation_calls += 1
        return "ok"


class CancellationModel:
    """Models supervisor-owned cancellation and stale completion fencing."""

    def __init__(self):
        self.generation = 0
        self.active = None
        self.state = "idle"
        self.retry_calls = 0

    def start(self):
        self.generation += 1
        self.active = self.generation
        self.state = "running"
        return self.active

    def cancel(self):
        if self.active is not None and self.state == "running":
            self.state = "cancelled"

    def complete(self, generation):
        if generation != self.active:
            return False
        if self.state != "running":
            self.state = "unknown_manual"
            return False
        self.state = "complete"
        return True


class WindowsProcessAuthorityStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = strict_json(CONTRACT)
        cls.header = HEADER.read_text(encoding="utf-8")
        cls.transaction = TRANSACTION.read_text(encoding="utf-8")
        cls.authority = AUTHORITY.read_text(encoding="utf-8")
        cls.broker = BROKER.read_text(encoding="utf-8")

    def test_contract_is_exactly_deny_closed(self):
        contract = self.contract
        self.assertEqual(contract["schema"],
                         "lae.windows-process-authority.contract.v1.0.0")
        self.assertEqual(contract["status"], "SOURCE_ONLY_NOT_READY")
        self.assertTrue(all(value is False for value in contract["availability"].values()))
        activation = contract["activation"]
        self.assertTrue(all(value is False for key, value in activation.items()
                            if key != "activation_requires_all" and
                            key != "activation_refusal_is_sticky"))
        self.assertTrue(activation["activation_requires_all"])
        self.assertTrue(activation["activation_refusal_is_sticky"])
        self.assertFalse(activation["nested_job_policy"])
        authority = contract["authority_object"]
        self.assertEqual(authority["type"], "private noncopyable RAII LaunchAuthority")
        self.assertEqual(authority["issuer"], "private LaunchAuthorityIssuer only")
        self.assertEqual(authority["handle_owner"],
                         "UniqueHandle closes each owning HANDLE exactly once")
        self.assertTrue(authority["move_only"])
        self.assertFalse(authority["public_raw_handle_accessor"])
        self.assertEqual(authority["operation_identity"],
                         ["operation_id_128", "generation_uint64", "nonce_128"])

    def test_identity_contract_requires_handles_and_rechecks(self):
        identity = self.contract["identity_pinned_handles"]
        executable = identity["executable"]
        working = identity["working_directory"]
        for field in (
            "supervisor_owned_executable_handle",
            "supervisor_owned_containing_directory_handle",
            "absolute_manifest_path_binding", "size_bytes", "volume_serial",
            "file_id_128", "sha256", "read_only_open",
            "identity_rechecked_after_open",
        ):
            self.assertIn(field, executable["required"])
        for field in (
            "supervisor_owned_directory_handle", "absolute_manifest_path_binding",
            "volume_serial", "directory_id_128", "no_reparse_components",
            "identity_rechecked_after_open",
        ):
            self.assertIn(field, working["required"])
        self.assertFalse(executable["request_path_or_reopen_allowed"])
        self.assertFalse(working["request_path_or_reopen_allowed"])
        self.assertFalse(identity["raw_paths_or_handles_in_receipt"])

    def test_containment_environment_and_orphan_contracts_are_complete(self):
        containment = self.contract["pre_child_containment"]
        for field in (
            "job_created_before_child", "kill_on_job_close",
            "active_process_zero_on_terminal", "no_ambient_handle_inheritance",
            "least_privilege_token",
        ):
            self.assertIn(field, containment["required"])
        self.assertFalse(containment["create_then_attach_gap_allowed"])
        self.assertFalse(containment["child_creation_without_proof"])

        environment = self.contract["environment"]
        self.assertFalse(environment["caller_environment_or_map_allowed"])
        self.assertFalse(environment["credential_categories_inherited"])
        self.assertFalse(environment["ambient_environment_inherited"])
        for category in ("tokens", "keys", "SSH_AUTH_SOCK", "proxy", "user_config"):
            self.assertIn(category, environment["credential_categories"])

        orphan = self.contract["cancellation_and_orphan"]
        for field in (
            "supervisor_owned_cancellation_event", "bounded_cancel_join",
            "kill_entire_contained_tree", "active_process_zero_before_release",
            "supervisor_death_closes_containment",
        ):
            self.assertIn(field, orphan["required"])
        self.assertFalse(orphan["late_completion_may_mutate_new_run"])
        self.assertFalse(orphan["automatic_retry_after_ambiguous_outcome"])
        self.assertEqual(orphan["ambiguous_outcome"], "unknown_manual")

    def test_header_has_nonserializable_proof_shape_and_sticky_false_gate(self):
        for token in (
            "class UniqueHandle final", "UniqueHandle(const UniqueHandle&) = delete",
            "::CloseHandle(value_)", "class CancellationState final",
            "std::uint64_t active_generation_", "kCancelRequested",
            "kUnknownManual", "class LaunchAuthority final",
            "LaunchAuthority(const LaunchAuthority&) = delete",
            "class LaunchAuthorityIssuer final", "LaunchAuthorityIssuer() = delete",
            "struct MintedParts final", "explicit LaunchAuthority(MintedParts&& parts)",
            "std::optional<LaunchAuthority> issue", "return std::nullopt",
            "std::wstring canonical_absolute_path",
            "operation_id_", "generation_", "nonce_", "volume_serial",
            "file_id", "sha256", "token_", "job_", "cancellation_event_",
            "environment_digest_", "kMinimalEnvironmentAllowlist",
            "kLaunchAuthorityAvailable = false", "valid_for_admission",
            "validate_for_admission", "mark_orphaned",
            "friend class LaunchAuthorityIssuer", "CloseHandle(value_)",
        ):
            self.assertIn(token, self.header)
        self.assertIn("#error", self.header)
        self.assertIn("private:", self.header)
        self.assertNotIn("HANDLE get(", self.header)
        self.assertNotIn("operator HANDLE", self.header)

    def test_cancellation_state_machine_is_source_connected_and_fail_closed(self):
        for token in (
            "cancellation_state_", "cancellation_state_->begin",
            "cancellation_state_->request_cancel", "cancellation_state_->complete",
            "cancellation_state_->orphan", "std::lock_guard<std::mutex>",
            "active_generation_ != generation", "state_ = RunState::kUnknownManual",
            "kill_on_job_close_", "active_process_zero_on_terminal_",
        ):
            self.assertIn(token, self.header)
        self.assertNotIn("TerminateProcess", self.header)

    def test_transaction_checks_proof_and_all_gates_before_mutation(self):
        self.assertIn("std::optional<LaunchAuthority> authority", self.transaction)
        refusal = self.transaction.index("if (!plan.authority.has_value()")
        returned = self.transaction.index("return LaunchReceipt{};", refusal)
        self.assertLess(refusal, returned)
        pre_refusal = self.transaction[:refusal]
        for forbidden in ("persist_dispatching", "begin_external_dispatch",
                          "CreateProcess", "ShellExecute", "popen(", "system("):
            self.assertNotIn(forbidden, pre_refusal)
        gate = self.transaction.index("if (!kProcessLaunchAvailable", returned)
        self.assertGreater(gate, returned)
        self.assertIn("return LaunchReceipt{};", self.transaction[gate:])
        self.assertIn("kSupervisorOwnedProcessTransactionAccepted", self.authority)
        consume = self.transaction.index("authority.consume()")
        self.assertGreater(consume, gate)
        self.assertNotIn("CreateProcess", self.transaction)

    def test_broker_launch_remains_refusal_only_and_no_public_activation(self):
        self.assertIn("launch_containment_unproven", self.broker)
        self.assertIn("launch_confinement_unproven", self.broker)
        self.assertIn("broker_not_activated", self.broker)
        self.assertNotIn("CreateProcess", self.broker)
        self.assertIn("kSupervisorContainmentProven = false", 
                      (ROOT / "native/windows_broker/trust_anchor.hpp").read_text())
        self.assertIn("kLaunchConfinementProven = false",
                      (ROOT / "native/windows_broker/trust_anchor.hpp").read_text())

    def test_every_missing_pre_child_proof_refuses_without_calls(self):
        base = {key: True for key in RefusalModel.REQUIRED}
        for missing in RefusalModel.REQUIRED:
            proof = dict(base)
            proof[missing] = False
            model = RefusalModel()
            self.assertEqual(model.admit(proof, gates=True), "unavailable", missing)
            self.assertEqual((model.create_calls, model.mutation_calls), (0, 0))

    def test_global_gate_false_refuses_even_with_complete_proof(self):
        model = RefusalModel()
        proof = {key: True for key in RefusalModel.REQUIRED}
        self.assertEqual(model.admit(proof, gates=False), "unavailable")
        self.assertEqual((model.create_calls, model.mutation_calls), (0, 0))

    def test_cancel_restart_fences_late_completion_and_retry(self):
        model = CancellationModel()
        old_generation = model.start()
        model.cancel()
        new_generation = model.start()
        self.assertNotEqual(old_generation, new_generation)
        self.assertFalse(model.complete(old_generation))
        self.assertEqual(model.state, "running")
        self.assertTrue(model.complete(new_generation))
        self.assertEqual(model.state, "complete")
        self.assertEqual(model.retry_calls, 0)

    def test_cancelled_completion_becomes_unknown_manual(self):
        model = CancellationModel()
        generation = model.start()
        model.cancel()
        self.assertFalse(model.complete(generation))
        self.assertEqual(model.state, "unknown_manual")
        self.assertEqual(model.retry_calls, 0)

    def test_contract_forbids_raw_receipts_and_ambient_or_fallback_execution(self):
        forbidden = self.contract["forbidden"]
        for field in (
            "caller_supplied_executable_or_cwd_path",
            "caller_supplied_environment", "ambient_credential_inheritance",
            "CreateProcess_before_containment_proof", "shell_or_fallback_execution",
            "raw_path_handle_or_environment_receipt",
            "automatic_retry_or_completion_after_ambiguous_outcome",
        ):
            self.assertIn(field, forbidden)
        transaction = self.contract["transaction"]
        self.assertTrue(transaction["one_use_move_only"])
        self.assertTrue(transaction["complete_binding_required"])
        self.assertTrue(transaction["pre_mutation_refusal"])
        self.assertFalse(transaction["journal_write_or_external_launch_in_this_revision"])


if __name__ == "__main__":
    unittest.main()
