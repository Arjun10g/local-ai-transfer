"""The eval receipt's NESTED blocks carry the run-identity binding too.

Run `j1m-eval-20260914-a` scored 32/37 on a real A100, salvaged its receipt,
and was still reported `failed`. Neither the score nor the hardware was at
fault: `_verify_eval_receipt` checked the nested `cuda_device` and `toolchain`
blocks with exact-set comparisons, and the run-identity slice adds `run_id` and
`instance_id` to every receipt -- including those. This is the same defect
`2572169` fixed in the sibling consumers in `remote_model_eval.py`; it had two
more sites here, and fixing only the first moved the failure to the second.

The fixture is the real salvaged receipt from that paid run, so these tests pin
the exact shape that failed rather than a reconstruction of it.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import j1m_orchestrator as orc

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "eval-receipt-run-identity.json"


def _receipt() -> dict:
    """The real salvaged receipt, with its fixture block refreshed.

    The vendored receipt was produced against the shipping fixture as it stood
    on 2026-09-14. These tests are about the run-identity binding and the
    failed-case attribution, not about which fixture version was current, so
    the fixture identity is refreshed from the live contract. Without this the
    whole file fails whenever a tool description legitimately changes, which
    would make it a tripwire for unrelated work rather than a regression test.
    """

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["fixture"] = orc._tool_eval_contract()["fixture_identity"]
    return payload


class EvalReceiptRunIdentityTests(unittest.TestCase):
    def setUp(self):
        # The verifier requires an absolute path beneath the trusted output
        # root, so the variants are written inside the repository.
        self._dir = Path(tempfile.mkdtemp(dir=FIXTURE.parent, prefix=".receipt-identity-"))
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)

    def _verify(self, payload: dict):
        path = self._dir / "eval-receipt.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return orc._verify_eval_receipt(path, payload["artifact"])

    def test_real_paid_run_receipt_verifies_with_run_identity_present(self):
        payload = _receipt()
        # Both nested blocks really do carry the binding; if a future receipt
        # stops doing so this test should be re-examined, not deleted.
        for block in ("cuda_device", "toolchain"):
            self.assertLessEqual({"run_id", "instance_id"}, set(payload[block]), block)
        verified = self._verify(payload)
        self.assertEqual(verified["status"], "completed_with_failures")
        self.assertEqual(verified["metrics"]["passed"], 32)
        self.assertEqual(verified["metrics"]["errors"], 0)

    def test_missing_attestation_key_is_still_refused(self):
        # The relaxation tolerates the identity fields and nothing else. A
        # receipt that drops real placement or toolchain evidence must still be
        # refused, or the fix would have widened into a hole.
        for block, key in (
            ("cuda_device", "device"), ("cuda_device", "selector"),
            ("cuda_device", "source"), ("cuda_device", "device_count"),
            ("toolchain", "versions"), ("toolchain", "packages"),
            ("toolchain", "required"), ("toolchain", "package_install"),
        ):
            with self.subTest(block=block, removed=key):
                payload = _receipt()
                payload[block].pop(key)
                with self.assertRaises(ValueError):
                    self._verify(payload)

    def test_unexpected_extra_key_is_still_refused(self):
        for block in ("cuda_device", "toolchain"):
            with self.subTest(block=block):
                payload = _receipt()
                payload[block]["smuggled"] = "x"
                with self.assertRaises(ValueError):
                    self._verify(payload)

    def test_tampered_placement_is_still_refused(self):
        payload = _receipt()
        payload["cuda_device"]["device"]["name"] = "NVIDIA T4"
        with self.assertRaises(ValueError):
            self._verify(payload)
        payload = _receipt()
        payload["cuda_device"]["status"] = "unverified"
        with self.assertRaises(ValueError):
            self._verify(payload)


class ReceiptErrorDiagnosticTests(unittest.TestCase):
    """A refused receipt must say which rule refused it.

    The constant `receipt_verification_failed` was the only record left of a
    failed verification, so the cause had to be found by importing the verifier
    and re-running it by hand against the salvaged file.
    """

    def test_reason_and_type_are_recorded(self):
        lifecycle: dict = {}
        orc._record_receipt_error(lifecycle, ValueError("eval receipt toolchain evidence invalid"))
        self.assertEqual(lifecycle["receipt_error"], "receipt_verification_failed")
        self.assertEqual(lifecycle["receipt_error_type"], "ValueError")
        self.assertEqual(lifecycle["receipt_error_reason"], "eval receipt toolchain evidence invalid")

    def test_finite_code_survives_an_unscreenable_message(self):
        # A message that cannot be screened is dropped, but the finite code and
        # the bounded exception class are still recorded.
        lifecycle: dict = {}
        orc._record_receipt_error(lifecycle, ValueError("token=" + "A" * 64))
        self.assertEqual(lifecycle["receipt_error"], "receipt_verification_failed")
        self.assertEqual(lifecycle["receipt_error_type"], "ValueError")
        self.assertNotIn("A" * 64, json.dumps(lifecycle))


class FailedCaseAttributionTests(unittest.TestCase):
    """A failing run must say WHICH cases failed, without saying what was said.

    `j1m-eval-20260914-a` scored 32/37 and the five failures were
    unattributable: the histogram reported `malformed_call` twice and
    `argument_type_mismatch` twice without naming a case, so the only route to
    fixing them was another paid run.
    """

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp(dir=FIXTURE.parent, prefix=".receipt-attribution-"))
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)

    def _verify(self, payload: dict):
        path = self._dir / "eval-receipt.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return orc._verify_eval_receipt(path, payload["artifact"])

    @staticmethod
    def _with_attribution(payload: dict, entries: list) -> dict:
        payload["metrics"]["failed_cases"] = entries
        return payload

    def _five(self):
        # Shaped exactly like the real run: 4 confirmation_sensitive, 1
        # tool_selection, matching its quality histogram.
        return [
            {"id": "prod-mail-006", "category": "confirmation_sensitive", "reason": "malformed_call"},
            {"id": "prod-mail-007", "category": "confirmation_sensitive", "reason": "malformed_call"},
            {"id": "prod-teams-005", "category": "confirmation_sensitive", "reason": "argument_type_mismatch"},
            {"id": "prod-fs-004", "category": "confirmation_sensitive", "reason": "wrong_tool"},
            {"id": "prod-time-002", "category": "tool_selection", "reason": "argument_type_mismatch"},
        ]

    def test_attribution_is_accepted_and_names_the_failures(self):
        verified = self._verify(self._with_attribution(_receipt(), self._five()))
        self.assertEqual(len(verified["metrics"]["failed_cases"]), 5)
        self.assertEqual(
            {entry["id"] for entry in verified["metrics"]["failed_cases"]},
            {"prod-mail-006", "prod-mail-007", "prod-teams-005", "prod-fs-004", "prod-time-002"},
        )

    def test_attribution_must_account_for_every_non_passing_case(self):
        # Fewer or more entries than failed+errors means the attribution does
        # not explain the aggregate, which is the whole point of carrying it.
        with self.assertRaises(ValueError):
            self._verify(self._with_attribution(_receipt(), self._five()[:4]))
        with self.assertRaises(ValueError):
            self._verify(self._with_attribution(_receipt(), self._five() + [
                {"id": "prod-extra-001", "category": "tool_selection", "reason": "wrong_tool"}]))

    def test_attribution_cannot_become_a_content_channel(self):
        # The entire justification for widening the disclosure boundary is that
        # an entry can hold nothing but an id, a category and a finite code.
        for extra in ("prompt", "response", "content", "tokens"):
            with self.subTest(field=extra):
                entries = self._five()
                entries[0][extra] = "the user's private message"
                with self.assertRaises(ValueError):
                    self._verify(self._with_attribution(_receipt(), entries))

    def test_duplicate_and_malformed_ids_are_refused(self):
        entries = self._five()
        entries[1]["id"] = entries[0]["id"]
        with self.assertRaises(ValueError):
            self._verify(self._with_attribution(_receipt(), entries))
        entries = self._five()
        entries[0]["id"] = "bad id/with slashes"
        with self.assertRaises(ValueError):
            self._verify(self._with_attribution(_receipt(), entries))

    def test_receipt_without_attribution_still_verifies(self):
        # The field is optional, so receipts written before this change, and
        # the vendored one, are not retroactively invalid.
        payload = _receipt()
        self.assertNotIn("failed_cases", payload["metrics"])
        self.assertEqual(self._verify(payload)["status"], "completed_with_failures")


class EvaluatorDisclosureTests(unittest.TestCase):
    def test_aggregate_discloses_only_id_category_reason_for_non_passing(self):
        from scripts.test.evaluate_tool_calls import aggregate_result
        result = {
            "case_count": 3, "passed": 1, "failed": 1, "errors": 1, "peak_rss_kib": 1,
            "category_summary": {}, "canary": {}, "error_diagnostics": {}, "quality_diagnostics": {},
            "cases": [
                {"id": "a", "category": "tool_selection", "status": "pass", "reason": None, "latency_ms": 1.0},
                {"id": "b", "category": "tool_selection", "status": "fail", "reason": "wrong_tool", "latency_ms": 2.0},
                {"id": "c", "category": "no_tool", "status": "error", "reason": "http_400", "latency_ms": 3.0},
            ],
        }
        aggregate = aggregate_result(result)
        self.assertNotIn("cases", aggregate)
        self.assertEqual(aggregate["failed_cases"], [
            {"id": "b", "category": "tool_selection", "reason": "wrong_tool"},
            {"id": "c", "category": "no_tool", "reason": "http_400"},
        ])
        # latency is deliberately not disclosed
        for entry in aggregate["failed_cases"]:
            self.assertEqual(set(entry), {"id", "category", "reason"})


if __name__ == "__main__":
    unittest.main()
