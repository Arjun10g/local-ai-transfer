#!/usr/bin/env python3
"""MODEL-COMPARATOR-EVAL-001: comparison mathematics and flag plumbing.

Everything here is offline: no provider, no model, no network, no credential.
The suite pins three things the slice promises — that the comparison verdicts
are correct and deterministic, that the default path is unchanged when the
flag is absent, and that the budget gate refuses rather than overruns.
"""

import contextlib
import copy
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.test import compare_model_quality as comparison
from tests.performance.lifecycle_test_isolation import isolated_lifecycle_execute

ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = {
    "tool_selection": 18, "confirmation_sensitive": 15, "schema_edge": 1,
    "prompt_injection": 1, "abstention": 1, "no_tool": 1,
}


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arm_metrics(passed_by_category, *, errors_by_category=None):
    errors_by_category = errors_by_category or {}
    summary = {}
    for category, count in CATEGORIES.items():
        passed = passed_by_category[category]
        errors = errors_by_category.get(category, 0)
        summary[category] = {
            "case_count": count, "passed": passed, "errors": errors,
            "failed": count - passed - errors,
        }
    return {
        "case_count": sum(CATEGORIES.values()),
        "passed": sum(passed_by_category.values()),
        "category_summary": summary,
    }


def indicator_vector(passed_by_category):
    vector = []
    for category, count in CATEGORIES.items():
        for index in range(count):
            vector.append({
                "id": f"{category}-{index}", "category": category,
                "passed": index < passed_by_category[category],
            })
    return vector


BASELINE = {"tool_selection": 15, "confirmation_sensitive": 12, "schema_edge": 1,
            "prompt_injection": 1, "abstention": 1, "no_tool": 0}
STRONGER = {"tool_selection": 16, "confirmation_sensitive": 13, "schema_edge": 1,
            "prompt_injection": 1, "abstention": 1, "no_tool": 0}
WEAKER = {"tool_selection": 12, "confirmation_sensitive": 10, "schema_edge": 1,
          "prompt_injection": 1, "abstention": 1, "no_tool": 0}
ZERO = {category: 0 for category in CATEGORIES}


class ComparisonMathTests(unittest.TestCase):
    def setUp(self):
        self.gate = comparison.load_gate()

    def test_gate_is_read_from_the_authored_fixture_spec(self):
        spec = json.loads((ROOT / "model/quality-eval/quality-fixture-spec.json").read_text())
        block = spec["comparison"]
        self.assertEqual(self.gate["bootstrap_resamples"], block["bootstrap_resamples"])
        self.assertEqual(self.gate["confidence"], block["confidence"])
        self.assertEqual(self.gate["non_inferiority_margin_points"], float(block["non_inferiority_margin_points"]))
        self.assertEqual(self.gate["critical_category_max_drop_points"], float(block["critical_category_max_drop_points"]))
        self.assertEqual(self.gate["quality_comparator"], "same-source-q8-or-higher")
        # execution/ACCEPTANCE_CRITERIA.md section 11 MUST 2.
        self.assertEqual(self.gate["retention_min_percent"], 95.0)
        self.assertEqual(self.gate["seed"], comparison.DEFAULT_SEED)

    def test_score_points_refuses_a_zero_case_fixture(self):
        self.assertEqual(comparison.score_points(29, 37), round(29 * 100 / 37, 6))
        with self.assertRaises(comparison.ComparisonError) as raised:
            comparison.score_points(0, 0)
        self.assertEqual(str(raised.exception), "no_cases")
        with self.assertRaises(comparison.ComparisonError) as raised:
            comparison.normalize_metrics({"case_count": 0, "passed": 0, "category_summary": {}})
        self.assertEqual(str(raised.exception), "metrics_invalid")

    def test_equal_scores_retain_fully_and_pass_when_paired(self):
        entry = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=arm_metrics(BASELINE), comparator_metrics=arm_metrics(BASELINE),
            baseline_cases=indicator_vector(BASELINE), comparator_cases=indicator_vector(BASELINE),
        )
        self.assertEqual(entry["retention_percent"], 100.0)
        self.assertEqual(entry["aggregate_delta_points"], 0.0)
        self.assertEqual(entry["category_drop_max_points"], 0.0)
        self.assertEqual(entry["bootstrap"]["pairing"], comparison.BOOTSTRAP_PAIRED)
        self.assertEqual(entry["bootstrap"]["delta_ci_lower_points"], 0.0)
        self.assertEqual(entry["bootstrap"]["delta_ci_upper_points"], 0.0)
        self.assertEqual(entry["verdict"], "pass")

    def test_comparator_worse_than_q4_retains_above_one_hundred_percent(self):
        entry = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=arm_metrics(BASELINE), comparator_metrics=arm_metrics(WEAKER),
            baseline_cases=indicator_vector(BASELINE), comparator_cases=indicator_vector(WEAKER),
        )
        self.assertGreater(entry["retention_percent"], 100.0)
        self.assertEqual(entry["retention_verdict"], "pass")
        self.assertGreater(entry["aggregate_delta_points"], 0.0)
        self.assertEqual(entry["category_drop_max_points"], 0.0)
        self.assertEqual(entry["critical_category_verdict"], "pass")
        self.assertEqual(entry["bootstrap"]["non_inferiority_verdict"], "pass")
        self.assertEqual(entry["verdict"], "pass")

    def test_a_zero_scoring_reference_is_a_typed_fail_not_a_pass(self):
        entry = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=arm_metrics(BASELINE), comparator_metrics=arm_metrics(ZERO),
        )
        self.assertIsNone(entry["retention_percent"])
        self.assertEqual(entry["retention_verdict"], "fail")
        self.assertEqual(entry["retention_reason"], comparison.RETENTION_UNDEFINED)
        self.assertEqual(entry["verdict"], "fail")

    def test_category_deltas_use_absolute_points_and_carry_case_counts(self):
        entry = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=arm_metrics(BASELINE), comparator_metrics=arm_metrics(STRONGER),
        )
        deltas = entry["category_deltas"]
        self.assertEqual(set(deltas), set(CATEGORIES))
        self.assertEqual(deltas["tool_selection"]["case_count"], 18)
        self.assertEqual(deltas["tool_selection"]["delta_points"], round(15 * 100 / 18 - 16 * 100 / 18, 6))
        self.assertEqual(deltas["schema_edge"]["delta_points"], 0.0)
        self.assertEqual(entry["critical_category_policy"], comparison.CRITICAL_CATEGORY_POLICY)
        # A single flipped case in a one-case category is a 100 point drop and
        # must trip the 8 point limit rather than be averaged away.
        single = dict(BASELINE)
        single["schema_edge"] = 0
        tripped = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=arm_metrics(single), comparator_metrics=arm_metrics(BASELINE),
        )
        self.assertEqual(tripped["category_drop_max_points"], 100.0)
        self.assertEqual(tripped["critical_category_verdict"], "fail")
        self.assertEqual(tripped["verdict"], "fail")

    def test_the_critical_drop_threshold_is_exactly_the_fixture_spec_value(self):
        # A 100-case category makes one case worth exactly one point, so the
        # boundary can be hit on the nose rather than approached.  The
        # threshold under test is read from the gate, never hardcoded here, so
        # replacing it in source with any looser constant fails this test.
        limit = self.gate["critical_category_max_drop_points"]
        self.assertEqual(limit, 8.0)

        def single_category(passed):
            return {
                "case_count": 100, "passed": passed,
                "category_summary": {"tool_selection": {
                    "case_count": 100, "passed": passed,
                    "failed": 100 - passed, "errors": 0}},
            }

        def verdict(drop):
            entry = comparison.compare_arm(
                comparator_label="q8_0", gate=self.gate,
                baseline_metrics=single_category(100 - int(drop)),
                comparator_metrics=single_category(100))
            self.assertEqual(entry["category_drop_max_points"], float(drop))
            return entry["critical_category_verdict"], entry["verdict"]

        self.assertEqual(verdict(limit), ("pass", "fail"))          # 8.0 points
        self.assertEqual(verdict(limit + 1), ("fail", "fail"))      # 9.0 points
        self.assertEqual(verdict(0)[0], "pass")
        # Identical arms clear every gate, so the only thing separating the
        # two boundary cases above really is the critical-category rule.
        clean = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=single_category(100), comparator_metrics=single_category(100),
            baseline_cases=[{"id": f"c{index}", "category": "tool_selection", "passed": True}
                            for index in range(100)],
            comparator_cases=[{"id": f"c{index}", "category": "tool_selection", "passed": True}
                              for index in range(100)])
        self.assertEqual(clean["verdict"], "pass")

    def test_bootstrap_is_deterministic_under_the_recorded_seed(self):
        baseline = comparison.normalize_metrics(arm_metrics(BASELINE))
        comparator = comparison.normalize_metrics(arm_metrics(STRONGER))
        first = comparison.bootstrap_delta_interval(baseline, comparator, resamples=10000, confidence=0.95, seed=comparison.DEFAULT_SEED)
        second = comparison.bootstrap_delta_interval(baseline, comparator, resamples=10000, confidence=0.95, seed=comparison.DEFAULT_SEED)
        self.assertEqual(first, second)
        self.assertEqual(first["seed"], comparison.DEFAULT_SEED)
        other = comparison.bootstrap_delta_interval(baseline, comparator, resamples=10000, confidence=0.95, seed=comparison.DEFAULT_SEED + 1)
        self.assertNotEqual(other["delta_ci_lower_points"], first["delta_ci_lower_points"])
        paired = comparison.bootstrap_delta_interval(
            baseline, comparator, resamples=10000, confidence=0.95, seed=comparison.DEFAULT_SEED,
            baseline_cases=comparison.normalize_cases(indicator_vector(BASELINE), baseline["category_summary"]),
            comparator_cases=comparison.normalize_cases(indicator_vector(STRONGER), comparator["category_summary"]),
        )
        self.assertEqual(paired["pairing"], comparison.BOOTSTRAP_PAIRED)
        self.assertEqual(paired, comparison.bootstrap_delta_interval(
            baseline, comparator, resamples=10000, confidence=0.95, seed=comparison.DEFAULT_SEED,
            baseline_cases=comparison.normalize_cases(indicator_vector(BASELINE), baseline["category_summary"]),
            comparator_cases=comparison.normalize_cases(indicator_vector(STRONGER), comparator["category_summary"]),
        ))
        # Pairing is strictly the narrower interval on the same data.
        self.assertGreater(paired["delta_ci_lower_points"], first["delta_ci_lower_points"])

    def test_unpaired_fallback_cannot_decide_the_two_point_margin(self):
        entry = comparison.compare_arm(
            comparator_label="q8_0", gate=self.gate,
            baseline_metrics=arm_metrics(BASELINE), comparator_metrics=arm_metrics(BASELINE),
        )
        self.assertEqual(entry["bootstrap"]["pairing"], comparison.BOOTSTRAP_UNPAIRED)
        self.assertEqual(entry["retention_percent"], 100.0)
        # Identical arms, yet the unpaired interval is far wider than the
        # 2 point margin; the verdict refuses instead of approving.
        self.assertLess(entry["bootstrap"]["delta_ci_lower_points"], -self.gate["non_inferiority_margin_points"])
        self.assertEqual(entry["bootstrap"]["non_inferiority_verdict"], "fail")
        self.assertEqual(entry["verdict"], "fail")

    def test_case_indicators_must_agree_with_the_category_summary(self):
        metrics = comparison.normalize_metrics(arm_metrics(BASELINE))
        vector = indicator_vector(BASELINE)
        self.assertEqual(len(comparison.normalize_cases(vector, metrics["category_summary"])), 37)
        wrong = copy.deepcopy(vector)
        wrong[0]["passed"] = not wrong[0]["passed"]
        with self.assertRaises(comparison.ComparisonError) as raised:
            comparison.normalize_cases(wrong, metrics["category_summary"])
        self.assertEqual(str(raised.exception), "case_indicators_invalid")
        duplicated = copy.deepcopy(vector)
        duplicated[1]["id"] = duplicated[0]["id"]
        with self.assertRaises(comparison.ComparisonError):
            comparison.normalize_cases(duplicated, metrics["category_summary"])
        stripped = copy.deepcopy(vector)
        stripped[0].pop("category")
        with self.assertRaises(comparison.ComparisonError):
            comparison.normalize_cases(stripped, metrics["category_summary"])

    def test_two_arms_must_have_scored_the_same_fixture_shape(self):
        other = {"only": {"case_count": 37, "passed": 30, "failed": 7, "errors": 0}}
        with self.assertRaises(comparison.ComparisonError) as raised:
            comparison.compare_arm(
                comparator_label="q8_0", gate=self.gate,
                baseline_metrics=arm_metrics(BASELINE),
                comparator_metrics={"case_count": 37, "passed": 30, "category_summary": other},
            )
        self.assertEqual(str(raised.exception), "fixture_shape_mismatch")

    def test_errors_count_against_the_score(self):
        with_errors = comparison.normalize_metrics(
            arm_metrics({**BASELINE, "tool_selection": 13}, errors_by_category={"tool_selection": 2}))
        self.assertEqual(with_errors["passed"], sum(BASELINE.values()) - 2)


class ComparisonReceiptTests(unittest.TestCase):
    def setUp(self):
        self.gate = comparison.load_gate()

    def test_receipt_carries_every_required_key_and_the_gate_constants(self):
        receipt = comparison.build_comparison_receipt(
            arms={"q4_k_m": arm_metrics(BASELINE), "q8_0": arm_metrics(STRONGER)},
            requested=["q8_0"], gate=self.gate,
            fixture_sha256="c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c",
            product_engine_reference={"engine": "lae-engine", "case_count": 37, "passed": 28, "score_points": comparison.score_points(28, 37)},
        )
        required = {
            "schema", "status", "generated_at_utc", "fixture_sha256", "case_count",
            "critical_category_policy", "gate", "requested", "baseline",
            "comparisons", "skipped", "product_engine_reference",
        }
        self.assertTrue(required <= set(receipt))
        self.assertEqual(receipt["schema"], comparison.COMPARISON_SCHEMA)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["case_count"], 37)
        self.assertEqual(receipt["gate"], self.gate)
        self.assertEqual(len(receipt["comparisons"]), 1)
        entry = receipt["comparisons"][0]
        for key in ("comparator", "baseline_score_points", "comparator_score_points",
                    "retention_percent", "retention_verdict", "aggregate_delta_points",
                    "category_deltas", "category_drop_max_points",
                    "critical_category_policy", "critical_category_verdict",
                    "bootstrap", "verdict"):
            self.assertIn(key, entry)
        for key in ("resamples", "confidence", "seed", "method", "pairing",
                    "delta_ci_lower_points", "delta_ci_upper_points",
                    "non_inferiority_margin_points", "non_inferiority_verdict"):
            self.assertIn(key, entry["bootstrap"])
        self.assertEqual(receipt["runtime_parity_delta_points"],
                         round(comparison.score_points(28, 37) - comparison.score_points(sum(BASELINE.values()), 37), 6))

    def test_a_missing_or_invalid_arm_receipt_is_a_typed_skip(self):
        receipt = comparison.build_comparison_receipt(
            arms={"q4_k_m": arm_metrics(BASELINE)}, requested=["q8_0", "bf16"], gate=self.gate)
        self.assertEqual(receipt["status"], "skipped")
        self.assertEqual(receipt["comparisons"], [])
        self.assertEqual([item["reason"] for item in receipt["skipped"]],
                         ["comparator_receipt_missing", "comparator_receipt_missing"])
        missing_baseline = comparison.build_comparison_receipt(
            arms={"q8_0": arm_metrics(STRONGER)}, requested=["q8_0"], gate=self.gate)
        self.assertEqual(missing_baseline["status"], "skipped")
        self.assertIsNone(missing_baseline["baseline"])
        self.assertEqual(missing_baseline["skipped"], [{"comparator": "q8_0", "reason": "comparator_baseline_missing"}])

    def test_unknown_arms_and_skip_reasons_are_refused(self):
        with self.assertRaises(comparison.ComparisonError):
            comparison.build_comparison_receipt(arms={}, requested=["q3"], gate=self.gate)
        with self.assertRaises(comparison.ComparisonError):
            comparison.build_comparison_receipt(
                arms={}, requested=["q8_0"], gate=self.gate,
                skipped=[{"comparator": "q8_0", "reason": "because"}])
        with self.assertRaises(comparison.ComparisonError):
            comparison.build_comparison_receipt(
                arms={"q4_k_m": arm_metrics(BASELINE), "q8_0": arm_metrics(STRONGER)},
                requested=["q8_0"], gate=self.gate, fixture_sha256="not-a-digest")

    def test_arm_receipt_schema_and_status_are_enforced(self):
        payload = {
            "schema": comparison.ARM_SCHEMA, "status": "verified", "arm": "q8_0",
            "metrics": arm_metrics(STRONGER),
            "case_indicators": indicator_vector(STRONGER),
        }
        metrics = comparison.arm_metrics_from_receipt(payload)
        self.assertEqual(metrics["case_count"], 37)
        self.assertEqual(len(metrics["case_indicators"]), 37)
        for mutation in ({"schema": "other"}, {"status": "refused"}, {"arm": "q3"}):
            with self.assertRaises(comparison.ComparisonError):
                comparison.arm_metrics_from_receipt({**payload, **mutation})

    def test_receipt_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "comparison-receipt.json"
            receipt = comparison.build_comparison_receipt(
                arms={"q4_k_m": arm_metrics(BASELINE), "q8_0": arm_metrics(STRONGER)},
                requested=["q8_0"], gate=self.gate)
            comparison.write_comparison_receipt(destination, receipt)
            self.assertEqual(json.loads(destination.read_text()), receipt)
            self.assertEqual(list(Path(directory).iterdir()), [destination])


class ComparatorFlagTests(unittest.TestCase):
    """The default path must be byte-for-byte what it is without this slice."""

    def setUp(self):
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "comparator_flag_orchestrator")
        self.runner = load(ROOT / "scripts/j1m_runner.py", "comparator_flag_runner")
        self.config = self.runner.load_config()

    def test_selection_vocabulary_is_a_closed_set(self):
        self.assertEqual(self.orchestrator._comparator_selection(""), ())
        self.assertEqual(self.orchestrator._comparator_selection(None), ())
        self.assertEqual(self.orchestrator._comparator_selection("q8"), ("q8_0",))
        self.assertEqual(self.orchestrator._comparator_selection("q8,bf16"), ("q8_0", "bf16"))
        self.assertEqual(self.orchestrator._comparator_selection("q4-oracle"), ("q4_k_m",))
        for rejected in ("bf16", "q8,q8", "q4", "q8, bf16", "Q8", "q8;bf16", "q4_oracle", "x" * 64):
            with self.assertRaises(ValueError):
                self.orchestrator._comparator_selection(rejected)
        self.assertEqual(self.orchestrator._comparator_arms(()), ())
        # Retention needs a same-runtime Q4 arm, so it is always added.
        self.assertEqual(self.orchestrator._comparator_arms(("q8_0",)), ("q4_k_m", "q8_0"))
        # The oracle request is exactly one arm, never a duplicated baseline,
        # and it contributes no denominator.
        self.assertEqual(self.orchestrator._comparator_arms(("q4_k_m",)), ("q4_k_m",))
        self.assertEqual(self.orchestrator._comparator_comparators(("q4_k_m",)), ())
        self.assertEqual(self.orchestrator._comparator_comparators(("q8_0", "bf16")), ("q8_0", "bf16"))

    def test_plan_without_the_flag_is_identical_to_the_plan_with_it_absent(self):
        def plan(argv):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(self.orchestrator.main(argv), 0)
            value = json.loads(stream.getvalue())
            value.pop("created_at_utc")
            return value

        for mode in ("prove", "build", "eval", "canary"):
            baseline = plan(["--mode", mode])
            explicit = plan(["--mode", mode, "--evaluate-comparators", ""])
            self.assertEqual(baseline, explicit, mode)
        default = plan(["--mode", "eval"])
        for selection in ("q4-oracle", "q8", "q8,bf16"):
            requested = plan(["--mode", "eval", "--evaluate-comparators", selection])
            self.assertEqual(set(requested) - set(default), {
                "comparator_phase", "comparator_fetch_allowlist",
                "comparator_commands", "comparator_cleanup_commands"}, selection)
            for key in default:
                self.assertEqual(default[key], requested[key], (selection, key))
            # The engine exists now, so a plan without a live clock is
            # planned rather than refused; the flag is still default OFF.
            self.assertEqual(requested["comparator_phase"]["status"], "planned", selection)
            self.assertNotIn("reason", requested["comparator_phase"])

    def test_remote_commands_and_uploads_are_untouched(self):
        commands = self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        # 14 before J1M-HOST-PRIVACY-001, 15 with the `chmod 700` that follows
        # the plan's own `mkdir -p`; 16 with the J1M-HOST-ROOT-001 trust-store
        # republish that keeps `umask 077` from leaving a 0600 CA bundle.
        self.assertEqual(len(commands), 16)
        self.assertEqual(commands[1][:2], ["chmod", "700"])
        self.assertEqual(commands[0][2:], commands[1][2:])
        flattened = " ".join(part for command in commands for part in command)
        self.assertNotIn("comparator", flattened)
        self.assertNotIn("retain", flattened)
        uploads = self.orchestrator._eval_uploads(
            self.config, "/scratch/j1m", None, ROOT / "artifacts/qwen35-9b/model-manifest.json")
        self.assertEqual(len(uploads), 15)
        self.assertEqual(self.orchestrator._eval_deadline_ceiling(self.config)["upload_count"], 15)

    def test_fetch_allowlist_defaults_to_config_and_never_names_weights(self):
        self.assertEqual(self.orchestrator._eval_fetch_allowlist(self.config, ()),
                         self.config["artifacts"]["eval_fetch_allowlist"])
        extended = self.orchestrator._eval_fetch_allowlist(self.config, ("q8_0", "bf16"))
        base = len(self.config["artifacts"]["eval_fetch_allowlist"])
        self.assertEqual(extended[:base], self.config["artifacts"]["eval_fetch_allowlist"])
        self.assertEqual(extended[base:], [
            "comparator-receipt-q4_k_m.json", "comparator-receipt-q8_0.json",
            "comparator-receipt-bf16.json", "scan-receipt.json"])
        self.assertFalse(any(name.lower().endswith(".gguf") for name in extended))
        with mock.patch.dict(self.config["artifacts"], {"eval_fetch_allowlist": ["Qwen3.5-9B-Q8_0.gguf"]}):
            with self.assertRaises(ValueError):
                self.orchestrator._eval_fetch_allowlist(self.config, ())

    def test_retaining_the_comparators_only_moves_the_cleanup_tail(self):
        full = self.runner.command_plan(self.config)
        retained = self.runner.command_plan(self.config, retain_comparators=True)
        tail = self.runner.comparator_cleanup_plan()
        self.assertEqual(retained + tail, full)
        self.assertEqual(len(tail), 3)
        self.assertEqual(tail[0][:2], ["rm", "-f"])
        self.assertIn("--post-cleanup", tail[1])
        self.assertIn("--manifest", tail[2])
        # The deployable Q4 is never deleted by the deferred tail.
        self.assertNotIn("/scratch/j1m/artifacts/Qwen3.5-9B-Q4_K_M.gguf", tail[0])
        self.assertEqual(self.runner.command_plan(self.config, retain_comparators=False), full)

    def test_scan_receipt_trust_anchor_matches_the_checked_in_receipt(self):
        import hashlib
        path = ROOT / self.orchestrator._APPROVED_SCAN_RECEIPT_RELATIVE
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         self.orchestrator._APPROVED_SCAN_RECEIPT_SHA256)
        recorded = {item["name"] for item in json.loads(path.read_text())["artifacts"]}
        self.assertEqual(recorded, {"Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"})


class ComparatorBudgetTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "comparator_budget_orchestrator")
        self.runner = load(ROOT / "scripts/j1m_runner.py", "comparator_budget_runner")
        self.config = self.runner.load_config()

    def test_budget_is_bounded_and_raises_no_authorised_cost(self):
        budget = self.orchestrator._comparator_budget(self.config, ("q8_0",))
        self.assertEqual(budget["arms"], ["q4_k_m", "q8_0"])
        self.assertEqual(budget["required_seconds"], 1500.0 + 2 * 720.0)
        # 1.35 is the primary approved target's rate, which is what the budget
        # uses when no selected entry is supplied.
        self.assertEqual(budget["projected_marginal_cost_usd"], round(1.35 * 2940.0 / 3600.0, 6))
        self.assertEqual(budget["authorized_active_cost_usd"], self.config["modes"]["eval"]["active_cost_usd"])
        self.assertFalse(budget["raises_authorized_cost"])
        # On an approved alternate every dollar figure follows that entry's own
        # rate, while the clock figures -- which decide whether the phase runs
        # at all -- are unmoved.
        alternate = self.orchestrator._comparator_budget(self.config, ("q8_0",), hourly_usd=1.65)
        self.assertEqual(alternate["projected_marginal_cost_usd"], round(1.65 * 2940.0 / 3600.0, 6))
        self.assertEqual(alternate["authorized_active_cost_usd"], 3.201)
        self.assertEqual(alternate["required_seconds"], budget["required_seconds"])
        self.assertEqual(alternate["static_slack_seconds"], budget["static_slack_seconds"])
        self.assertEqual(alternate["fits_static_worst_case"], budget["fits_static_worst_case"])
        both = self.orchestrator._comparator_budget(self.config, ("q8_0", "bf16"))
        self.assertEqual(both["required_seconds"], 1500.0 + 3 * 720.0)
        self.assertGreater(both["required_seconds"], budget["required_seconds"])
        # The shipped eval clock does not fit the comparator worst case, and
        # this slice does not raise it.
        envelope = self.orchestrator._eval_deadline_ceiling(self.config)
        self.assertEqual(budget["static_slack_seconds"],
                         round(envelope["run_seconds"] - envelope["ceiling_seconds"], 3))
        self.assertFalse(budget["fits_static_worst_case"])
        self.assertTrue(self.orchestrator._comparator_budget(self.config, ())["fits_static_worst_case"])
        for bad in (0, -1, float("inf"), 36001, True):
            with self.assertRaises(ValueError):
                self.orchestrator._comparator_budget(self.config, ("q8_0",), arm_seconds=bad)

    def test_skip_reason_vocabularies_are_the_same_closed_set(self):
        self.assertEqual(self.orchestrator._COMPARATOR_SKIP_REASONS, comparison.SKIP_REASONS)

    def test_phase_is_available_but_still_refuses_when_the_engine_is_absent(self):
        # COMPARATOR-ENGINE-001 supplies the host, so the flag is live; the
        # typed pre-spend refusal must still exist for anyone who turns it off.
        self.assertTrue(self.orchestrator._COMPARATOR_ENGINE_AVAILABLE)
        self.assertEqual(self.orchestrator._comparator_phase(self.config, ("q8_0",))["status"], "planned")
        with mock.patch.object(self.orchestrator, "_COMPARATOR_ENGINE_AVAILABLE", False):
            phase = self.orchestrator._comparator_phase(self.config, ("q8_0",))
        self.assertEqual(phase["status"], "refused")
        self.assertEqual(phase["reason"], "comparator_engine_unavailable")
        self.assertIn(phase["reason"], self.orchestrator._COMPARATOR_SKIP_REASONS)
        absent = self.orchestrator._comparator_phase(self.config, ())
        self.assertEqual(absent["status"], "not_requested")
        self.assertEqual(absent["reason"], "comparator_not_requested")

    def test_budget_check_refuses_when_the_remaining_clock_is_short(self):
        import time
        required = self.orchestrator._comparator_budget(self.config, ("q8_0",))["required_seconds"]
        reserved = (float(self.config["modes"]["eval"]["stage_budgets_seconds"]["cleanup_reserve"])
                    + self.orchestrator._DELETION_RESERVE_SECONDS)
        short = self.orchestrator._comparator_phase(
            self.config, ("q8_0",),
            execution_deadline=time.monotonic() + reserved + required - 60.0)
        self.assertEqual(short["status"], "refused")
        self.assertEqual(short["reason"], "comparator_clock_insufficient")
        self.assertLess(short["available_seconds"], required)
        ample = self.orchestrator._comparator_phase(
            self.config, ("q8_0",),
            execution_deadline=time.monotonic() + reserved + required + 600.0)
        self.assertEqual(ample["status"], "approved")
        self.assertNotIn("reason", ample)
        self.assertGreaterEqual(ample["available_seconds"], required)

    def test_a_mid_phase_clock_exhaustion_is_a_typed_skip_not_a_run_failure(self):
        # _eval_timeout refuses a stage budget that would cross the provider
        # clock. Inside the comparator phase that must degrade to a typed
        # comparator skip: the Q4 job has already completed and nothing in
        # the comparator phase may retract it.
        import time
        with self.assertRaises(self.orchestrator.sf.ShadeformError):
            self.orchestrator._eval_timeout(time.monotonic() - 1.0, 60.0)
        source = (ROOT / "scripts/j1m_orchestrator.py").read_text()
        phase = source[source.index('if comparator_selection:\n                    # The Q4 job'):]
        phase = phase[:phase.index('if lifecycle["job"]["status"] != "completed"')]
        self.assertIn('except sf.ShadeformError:', phase)
        self.assertIn('"comparator_clock_insufficient"', phase)
        self.assertIn('"comparator_stage_failed"', phase)
        # The deferred deletion tail is in a finally, so it runs on the
        # refusal path too, and an unproven deletion is typed.
        self.assertIn('finally:', phase)
        self.assertIn('run_comparator_cleanup()', phase)
        self.assertLess(phase.index('finally:'), phase.index('run_comparator_cleanup()'))
        # And it is no longer only reachable from inside the phase. The phase's
        # `finally` sits below the eval-stage loop's `raise`, so a failure in
        # any later stage skipped the tail entirely and left
        # `comparator_cleanup_error` unset. The teardown `finally` now calls the
        # same idempotent closure, so every path after `--run` reaches it.
        teardown = source[source.index('        finally:\n            try:\n                _persist_lifecycle'):]
        self.assertIn('run_comparator_cleanup()', teardown)
        self.assertLess(teardown.index('run_comparator_cleanup()'),
                        teardown.index('lifecycle["salvage"]'))
        closure = source[source.index('def run_comparator_cleanup()'):]
        closure = closure[:closure.index('def deletion_confirmed()')]
        self.assertIn('if comparator_cleanup_done or comparator_cleanup_context is None:', closure)
        self.assertIn('comparator_cleanup_error', closure)
        self.assertIn('_eval_timeout(execution_deadline', closure)

    @isolated_lifecycle_execute
    def test_execute_refuses_comparators_before_any_provider_access(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            config_path = Path(directory) / "j1m-config.json"
            shutil.copy2(ROOT / "model/conversion/j1m-config.json", config_path)
            config_path.chmod(0o600)
            with mock.patch.object(self.orchestrator.sf, "preflight_legacy_deletion_evidence"), \
                    mock.patch.object(self.orchestrator.sf, "load_env") as load_env, \
                    mock.patch.object(self.orchestrator.sf, "list_candidates") as list_candidates, \
                    mock.patch.object(self.orchestrator.sf, "create_ephemeral_ssh_key") as create_key:
                # An engine-unavailable build still refuses before any spend.
                with mock.patch.object(self.orchestrator, "_COMPARATOR_ENGINE_AVAILABLE", False):
                    with self.assertRaises(ValueError):
                        self.orchestrator.execute(
                            Path(directory) / "env", config_path=config_path,
                            phase_id="comparator-gate", run_id="test",
                            artifact_destination=Path(directory) / "artifacts",
                            mode="eval", evaluate_comparators="q8")
                # Refused outside eval before even the deletion preflight.
                with self.assertRaises(ValueError):
                    self.orchestrator.execute(
                        Path(directory) / "env", config_path=config_path,
                        phase_id="comparator-gate", run_id="test",
                        artifact_destination=Path(directory) / "artifacts",
                        mode="prove", evaluate_comparators="q8")
                with self.assertRaises(ValueError):
                    self.orchestrator.execute(
                        Path(directory) / "env", config_path=config_path,
                        phase_id="comparator-gate", run_id="test",
                        artifact_destination=Path(directory) / "artifacts",
                        mode="eval", evaluate_comparators="bf16")
            load_env.assert_not_called()
            list_candidates.assert_not_called()
            create_key.assert_not_called()

    def test_cli_refuses_comparators_outside_eval_mode(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(self.orchestrator._safe_cli(["--mode", "prove", "--evaluate-comparators", "q8"]), 2)
        self.assertEqual(json.loads(stream.getvalue()), {"status": "refused", "error_code": "input_rejected"})

    def test_comparison_receipt_is_written_with_a_typed_skip_when_nothing_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            receipt = self.orchestrator._write_comparison_receipt(
                destination, ("q8_0",), phase_reason="comparator_engine_unavailable",
                fixture_sha256="c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c")
            self.assertEqual(receipt["status"], "skipped")
            # The baseline arm is always evaluated, so its skip is recorded
            # too: without it a refused `q4-oracle` wrote an empty `skipped`
            # list, indistinguishable from a run that asked for nothing.
            self.assertEqual(sorted(receipt["skipped"], key=lambda item: item["comparator"]), [
                {"comparator": "q4_k_m", "reason": "comparator_engine_unavailable"},
                {"comparator": "q8_0", "reason": "comparator_engine_unavailable"},
            ])
            self.assertEqual(receipt["comparisons"], [])
            written = json.loads((destination / "comparison-receipt.json").read_text())
            self.assertEqual(written, receipt)
            with self.assertRaises(ValueError):
                self.orchestrator._write_comparison_receipt(destination, ("q8_0",), phase_reason="made_up")

    def test_comparison_receipt_reads_salvaged_arm_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for arm, passed in (("q4_k_m", BASELINE), ("q8_0", STRONGER)):
                (destination / f"comparator-receipt-{arm}.json").write_text(json.dumps({
                    "schema": comparison.ARM_SCHEMA, "status": "completed_with_failures",
                    "arm": arm, "metrics": arm_metrics(passed),
                    "case_indicators": indicator_vector(passed),
                }))
            receipt = self.orchestrator._write_comparison_receipt(
                destination, ("q8_0",),
                product_engine_reference={"engine": "lae-engine", "case_count": 37, "passed": 28,
                                          "score_points": comparison.score_points(28, 37)})
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["comparisons"][0]["bootstrap"]["pairing"], comparison.BOOTSTRAP_PAIRED)
            self.assertEqual(receipt["comparisons"][0]["comparator"], "q8_0")
            self.assertIn("runtime_parity_delta_points", receipt)

    def test_product_engine_reference_projects_only_a_verified_receipt(self):
        self.assertIsNone(self.orchestrator._product_engine_reference({}))
        self.assertIsNone(self.orchestrator._product_engine_reference(
            {"eval_receipt": {"metrics": {"case_count": 0, "passed": 0}}}))
        reference = self.orchestrator._product_engine_reference(
            {"eval_receipt": {"metrics": {"case_count": 37, "passed": 28}}})
        self.assertEqual(reference, {"engine": "lae-engine", "case_count": 37, "passed": 28,
                                     "score_points": comparison.score_points(28, 37)})


if __name__ == "__main__":
    unittest.main()
