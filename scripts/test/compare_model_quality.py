#!/usr/bin/env python3
"""Compute the Q4 versus higher-precision comparison receipt.

This module is deliberately offline, dependency-free and side-effect-free
apart from the one receipt it is asked to write.  It never contacts a model,
a provider, or a network; it only reads per-arm evaluation receipts that were
already salvaged and turns them into the verdicts that
``execution/ACCEPTANCE_CRITERIA.md`` section 11 and
``model/quality-eval/quality-fixture-spec.json`` ``comparison`` require.

Every quantity it cannot compute becomes a typed refusal, never a silent pass.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = ROOT / "model" / "quality-eval" / "quality-fixture-spec.json"
COMPARISON_SCHEMA = "local_bmo.j1m.comparison-receipt.v1"
ARM_SCHEMA = "local_bmo.j1m.comparator-eval-receipt.v1"
BASELINE_ARM = "q4_k_m"
COMPARATOR_ARMS = ("q8_0", "bf16")
KNOWN_ARMS = (BASELINE_ARM, *COMPARATOR_ARMS)
# execution/ACCEPTANCE_CRITERIA.md section 11 MUST 2.  The fixture spec fixes
# the margin, the resample count, the confidence and the category drop; the
# retention floor lives in the acceptance criteria, so it is pinned here.
RETENTION_MIN_PERCENT = 95.0
# Recorded in the receipt so an interval is reproducible from the receipt
# alone.  Changing it changes published numbers and is a reviewed act.
DEFAULT_SEED = 20260911
MAX_RESAMPLES = 100000
MAX_RECEIPT_BYTES = 256 * 1024
CRITICAL_CATEGORY_POLICY = "all_categories_conservative"
BOOTSTRAP_PAIRED_METHOD = "paired_case_percentile"
BOOTSTRAP_UNPAIRED_METHOD = "stratified_by_category_percentile"
BOOTSTRAP_PAIRED = "paired_by_case_id"
BOOTSTRAP_UNPAIRED = "unpaired_stratified_by_category"
CASE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MAX_CASES = 4096
SKIP_REASONS = frozenset({
    "comparator_not_requested",
    "comparator_clock_insufficient",
    "comparator_stage_failed",
    "comparator_receipt_missing",
    "comparator_receipt_invalid",
    "comparator_artifact_identity_mismatch",
    "comparator_baseline_missing",
})
RETENTION_UNDEFINED = "retention_undefined_zero_reference"


class ComparisonError(ValueError):
    """Typed refusal; the message is a finite reason, never dynamic detail."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_json(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ComparisonError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=reject_duplicates)


def _bounded_json_file(path: Path, limit: int = MAX_RECEIPT_BYTES) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ComparisonError("receipt_unavailable") from exc
    if size > limit:
        raise ComparisonError("receipt_too_large")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ComparisonError("receipt_unavailable") from exc
    if len(raw) > limit:
        raise ComparisonError("receipt_too_large")
    try:
        return _strict_json(raw)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ComparisonError("receipt_invalid") from exc


def load_gate(spec_path: Path = DEFAULT_SPEC, *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Read the comparison contract from the authored fixture spec."""

    spec = _bounded_json_file(spec_path)
    comparison = spec.get("comparison") if isinstance(spec, dict) else None
    if not isinstance(comparison, dict):
        raise ComparisonError("comparison_spec_invalid")
    required = {
        "runtime_oracle", "quality_comparator", "bootstrap_resamples",
        "confidence", "non_inferiority_margin_points",
        "critical_category_max_drop_points",
    }
    if set(comparison) != required:
        raise ComparisonError("comparison_spec_invalid")
    resamples = comparison["bootstrap_resamples"]
    confidence = comparison["confidence"]
    margin = comparison["non_inferiority_margin_points"]
    drop = comparison["critical_category_max_drop_points"]
    if (isinstance(resamples, bool) or not isinstance(resamples, int) or
            not 1 <= resamples <= MAX_RESAMPLES):
        raise ComparisonError("comparison_spec_invalid")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
            not math.isfinite(confidence) or not 0.5 <= confidence < 1.0):
        raise ComparisonError("comparison_spec_invalid")
    for value in (margin, drop):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not 0 <= value <= 100):
            raise ComparisonError("comparison_spec_invalid")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 63:
        raise ComparisonError("comparison_seed_invalid")
    if comparison["quality_comparator"] != "same-source-q8-or-higher":
        raise ComparisonError("comparison_spec_invalid")
    return {
        "retention_min_percent": RETENTION_MIN_PERCENT,
        "non_inferiority_margin_points": float(margin),
        "critical_category_max_drop_points": float(drop),
        "bootstrap_resamples": int(resamples),
        "confidence": float(confidence),
        "seed": int(seed),
        "quality_comparator": comparison["quality_comparator"],
        "runtime_oracle": comparison["runtime_oracle"],
    }


def score_points(passed: int, case_count: int) -> float:
    """Return the aggregate score in absolute points (0..100)."""

    if isinstance(passed, bool) or isinstance(case_count, bool):
        raise ComparisonError("metrics_invalid")
    if not isinstance(passed, int) or not isinstance(case_count, int):
        raise ComparisonError("metrics_invalid")
    if case_count <= 0:
        raise ComparisonError("no_cases")
    if not 0 <= passed <= case_count:
        raise ComparisonError("metrics_invalid")
    return round(passed * 100.0 / case_count, 6)


def normalize_metrics(metrics: Any) -> dict[str, Any]:
    """Accept only the bounded aggregate shape both arms must share."""

    if not isinstance(metrics, dict):
        raise ComparisonError("metrics_invalid")
    summary = metrics.get("category_summary")
    case_count = metrics.get("case_count")
    if not isinstance(summary, dict) or not summary:
        raise ComparisonError("metrics_invalid")
    if isinstance(case_count, bool) or not isinstance(case_count, int) or case_count <= 0:
        raise ComparisonError("no_cases" if case_count == 0 else "metrics_invalid")
    counts: dict[str, dict[str, int]] = {}
    total = 0
    for category, item in summary.items():
        if not isinstance(category, str) or not 1 <= len(category) <= 64 or not category.isascii():
            raise ComparisonError("metrics_invalid")
        if not isinstance(item, dict) or set(item) != {"case_count", "passed", "failed", "errors"}:
            raise ComparisonError("metrics_invalid")
        values = {}
        for key in ("case_count", "passed", "failed", "errors"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ComparisonError("metrics_invalid")
            values[key] = value
        if values["case_count"] <= 0:
            raise ComparisonError("metrics_invalid")
        if values["passed"] + values["failed"] + values["errors"] != values["case_count"]:
            raise ComparisonError("metrics_invalid")
        counts[category] = values
        total += values["case_count"]
    if total != case_count:
        raise ComparisonError("metrics_invalid")
    passed = metrics.get("passed")
    if isinstance(passed, bool) or not isinstance(passed, int) or passed < 0:
        raise ComparisonError("metrics_invalid")
    if sum(values["passed"] for values in counts.values()) != passed:
        raise ComparisonError("metrics_invalid")
    return {"case_count": case_count, "passed": passed, "category_summary": counts}


def normalize_cases(value: Any, category_summary: dict[str, dict[str, int]]) -> dict[str, bool]:
    """Accept a bounded per-case pass-indicator vector and cross-check it.

    The vector is what makes a *paired* interval possible.  It carries only a
    case identifier, its category, and a boolean: never a prompt, a response,
    or any model output.
    """

    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CASES:
        raise ComparisonError("case_indicators_invalid")
    indicators: dict[str, bool] = {}
    categories: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "category", "passed"}:
            raise ComparisonError("case_indicators_invalid")
        identifier = item["id"]
        category = item["category"]
        passed = item["passed"]
        if not isinstance(identifier, str) or not CASE_ID.fullmatch(identifier):
            raise ComparisonError("case_indicators_invalid")
        if identifier in indicators:
            raise ComparisonError("case_indicators_invalid")
        if not isinstance(category, str) or category not in category_summary:
            raise ComparisonError("case_indicators_invalid")
        if not isinstance(passed, bool):
            raise ComparisonError("case_indicators_invalid")
        indicators[identifier] = passed
        categories[category] = categories.get(category, 0) + (1 if passed else 0)
    if len(indicators) != sum(item["case_count"] for item in category_summary.values()):
        raise ComparisonError("case_indicators_invalid")
    for category, item in category_summary.items():
        if categories.get(category, 0) != item["passed"]:
            raise ComparisonError("case_indicators_invalid")
    return indicators


def _aligned(baseline: dict[str, Any], comparator: dict[str, Any]) -> list[str]:
    """Both arms must have scored the identical fixture shape."""

    if baseline["case_count"] != comparator["case_count"]:
        raise ComparisonError("fixture_shape_mismatch")
    left = baseline["category_summary"]
    right = comparator["category_summary"]
    if set(left) != set(right):
        raise ComparisonError("fixture_shape_mismatch")
    for category in left:
        if left[category]["case_count"] != right[category]["case_count"]:
            raise ComparisonError("fixture_shape_mismatch")
    return sorted(left)


def _percentile_index(count: int, probability: float) -> int:
    """Nearest-rank percentile index into an ascending list."""

    rank = math.ceil(probability * count)
    return min(max(rank - 1, 0), count - 1)


def bootstrap_delta_interval(
    baseline: dict[str, Any], comparator: dict[str, Any], *,
    resamples: int, confidence: float, seed: int,
    baseline_cases: dict[str, bool] | None = None,
    comparator_cases: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Percentile interval for ``score(baseline) - score(comparator)``.

    When both arms supply a per-case pass-indicator vector over the identical
    case-id set the interval is **paired**: one resample draws case ids with
    replacement and averages the per-case differences.  That is the design the
    fixture spec's 2-point non-inferiority margin assumes.

    Without those vectors the interval falls back to resampling within each
    category independently per arm.  That fallback is materially weaker: on
    the 37-case profile two *identical* arms still produce a 95% interval of
    about ±16 points, and even 1,200 cases only reach about ±3.25, so a
    2-point margin is not decidable unpaired at any realistic corpus size.
    The fallback therefore refuses rather than approves, and records why.
    """

    categories = _aligned(baseline, comparator)
    total = baseline["case_count"]
    if total <= 0:
        raise ComparisonError("no_cases")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or not 1 <= resamples <= MAX_RESAMPLES:
        raise ComparisonError("bootstrap_resamples_invalid")
    if not 0.5 <= confidence < 1.0:
        raise ComparisonError("bootstrap_confidence_invalid")
    tail = (1.0 - confidence) / 2.0
    if baseline_cases is not None and comparator_cases is not None:
        if set(baseline_cases) != set(comparator_cases) or len(baseline_cases) != total:
            raise ComparisonError("case_indicators_misaligned")
        differences = [
            int(baseline_cases[identifier]) - int(comparator_cases[identifier])
            for identifier in sorted(baseline_cases)
        ]
        rng = random.Random(seed)
        scale = 100.0 / total
        deltas = []
        for _ in range(resamples):
            accumulated = 0
            for _draw in range(total):
                accumulated += differences[rng.randrange(total)]
            deltas.append(accumulated * scale)
        deltas.sort()
        return {
            "resamples": resamples,
            "confidence": confidence,
            "seed": seed,
            "method": BOOTSTRAP_PAIRED_METHOD,
            "pairing": BOOTSTRAP_PAIRED,
            "delta_ci_lower_points": round(deltas[_percentile_index(resamples, tail)], 6),
            "delta_ci_upper_points": round(deltas[_percentile_index(resamples, 1.0 - tail)], 6),
        }
    strata = [
        (
            baseline["category_summary"][category]["case_count"],
            baseline["category_summary"][category]["passed"],
            comparator["category_summary"][category]["passed"],
        )
        for category in categories
    ]
    rng = random.Random(seed)
    scale = 100.0 / total
    deltas: list[float] = []
    for _ in range(resamples):
        difference = 0
        for size, baseline_passed, comparator_passed in strata:
            for _draw in range(size):
                if rng.randrange(size) < baseline_passed:
                    difference += 1
            for _draw in range(size):
                if rng.randrange(size) < comparator_passed:
                    difference -= 1
        deltas.append(difference * scale)
    deltas.sort()
    return {
        "resamples": resamples,
        "confidence": confidence,
        "seed": seed,
        "method": BOOTSTRAP_UNPAIRED_METHOD,
        "pairing": BOOTSTRAP_UNPAIRED,
        "delta_ci_lower_points": round(deltas[_percentile_index(resamples, tail)], 6),
        "delta_ci_upper_points": round(deltas[_percentile_index(resamples, 1.0 - tail)], 6),
    }


def category_deltas(baseline: dict[str, Any], comparator: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-category ``baseline - comparator`` in absolute points."""

    result: dict[str, dict[str, Any]] = {}
    for category in _aligned(baseline, comparator):
        left = baseline["category_summary"][category]
        right = comparator["category_summary"][category]
        baseline_points = score_points(left["passed"], left["case_count"])
        comparator_points = score_points(right["passed"], right["case_count"])
        result[category] = {
            "case_count": left["case_count"],
            "baseline_score_points": baseline_points,
            "comparator_score_points": comparator_points,
            "delta_points": round(baseline_points - comparator_points, 6),
        }
    return result


def compare_arm(
    *, comparator_label: str, baseline_metrics: Any, comparator_metrics: Any,
    gate: dict[str, Any],
    baseline_cases: Any = None, comparator_cases: Any = None,
) -> dict[str, Any]:
    """Return one fully decided comparison entry."""

    if comparator_label not in COMPARATOR_ARMS:
        raise ComparisonError("comparator_arm_unknown")
    baseline = normalize_metrics(baseline_metrics)
    comparator = normalize_metrics(comparator_metrics)
    categories = _aligned(baseline, comparator)
    baseline_indicators = (
        normalize_cases(baseline_cases, baseline["category_summary"])
        if baseline_cases is not None else None)
    comparator_indicators = (
        normalize_cases(comparator_cases, comparator["category_summary"])
        if comparator_cases is not None else None)
    baseline_points = score_points(baseline["passed"], baseline["case_count"])
    comparator_points = score_points(comparator["passed"], comparator["case_count"])
    entry: dict[str, Any] = {
        "comparator": comparator_label,
        "case_count": baseline["case_count"],
        "baseline_score_points": baseline_points,
        "comparator_score_points": comparator_points,
        "aggregate_delta_points": round(baseline_points - comparator_points, 6),
    }
    if comparator_points == 0.0:
        entry["retention_percent"] = None
        entry["retention_verdict"] = "fail"
        entry["retention_reason"] = RETENTION_UNDEFINED
    else:
        retention = round(baseline_points * 100.0 / comparator_points, 6)
        entry["retention_percent"] = retention
        entry["retention_verdict"] = "pass" if retention >= gate["retention_min_percent"] else "fail"
    deltas = category_deltas(baseline, comparator)
    drop = max([0.0] + [-item["delta_points"] for item in deltas.values()])
    entry["category_deltas"] = {category: deltas[category] for category in categories}
    entry["category_drop_max_points"] = round(drop, 6)
    entry["critical_category_policy"] = CRITICAL_CATEGORY_POLICY
    entry["critical_category_verdict"] = (
        "pass" if drop <= gate["critical_category_max_drop_points"] else "fail"
    )
    bootstrap = bootstrap_delta_interval(
        baseline, comparator,
        resamples=gate["bootstrap_resamples"], confidence=gate["confidence"],
        seed=gate["seed"],
        baseline_cases=baseline_indicators, comparator_cases=comparator_indicators,
    )
    margin = gate["non_inferiority_margin_points"]
    bootstrap["non_inferiority_margin_points"] = margin
    bootstrap["non_inferiority_verdict"] = (
        "pass" if bootstrap["delta_ci_lower_points"] >= -margin else "fail"
    )
    entry["bootstrap"] = bootstrap
    entry["verdict"] = "pass" if all(
        entry[key] == "pass" for key in
        ("retention_verdict", "critical_category_verdict")
    ) and bootstrap["non_inferiority_verdict"] == "pass" else "fail"
    return entry


def _indicator_vector(metrics: Any) -> list[dict[str, Any]] | None:
    """Return the validated per-case indicator vector, when the arm carried one."""

    if not isinstance(metrics, dict):
        return None
    indicators = metrics.get("case_indicators")
    return indicators if isinstance(indicators, list) else None


def _skip_entry(comparator: str, reason: str) -> dict[str, str]:
    if reason not in SKIP_REASONS:
        raise ComparisonError("skip_reason_unknown")
    return {"comparator": comparator, "reason": reason}


def build_comparison_receipt(
    *, arms: dict[str, Any], requested: list[str] | tuple[str, ...],
    gate: dict[str, Any], fixture_sha256: str | None = None,
    skipped: list[dict[str, str]] | None = None,
    product_engine_reference: dict[str, Any] | None = None,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Assemble the receipt, refusing rather than inventing any number."""

    for label in requested:
        if label not in COMPARATOR_ARMS:
            raise ComparisonError("comparator_arm_unknown")
    skips = list(skipped or [])
    for item in skips:
        if not isinstance(item, dict) or set(item) != {"comparator", "reason"} or item["reason"] not in SKIP_REASONS:
            raise ComparisonError("skip_reason_unknown")
    comparisons: list[dict[str, Any]] = []
    baseline_metrics = arms.get(BASELINE_ARM)
    baseline_summary: dict[str, Any] | None = None
    if baseline_metrics is None:
        for label in requested:
            if not any(item["comparator"] == label for item in skips):
                skips.append(_skip_entry(label, "comparator_baseline_missing"))
    else:
        normalized = normalize_metrics(baseline_metrics)
        baseline_summary = {
            "arm": BASELINE_ARM,
            "case_count": normalized["case_count"],
            "passed": normalized["passed"],
            "score_points": score_points(normalized["passed"], normalized["case_count"]),
        }
        for label in requested:
            if any(item["comparator"] == label for item in skips):
                continue
            metrics = arms.get(label)
            if metrics is None:
                skips.append(_skip_entry(label, "comparator_receipt_missing"))
                continue
            comparisons.append(compare_arm(
                comparator_label=label, baseline_metrics=baseline_metrics,
                comparator_metrics=metrics, gate=gate,
                baseline_cases=_indicator_vector(baseline_metrics),
                comparator_cases=_indicator_vector(metrics),
            ))
    if fixture_sha256 is not None and (
            not isinstance(fixture_sha256, str) or len(fixture_sha256) != 64 or
            any(character not in "0123456789abcdef" for character in fixture_sha256)):
        raise ComparisonError("fixture_identity_invalid")
    status = "verified" if comparisons else "skipped"
    receipt: dict[str, Any] = {
        "schema": COMPARISON_SCHEMA,
        "status": status,
        "generated_at_utc": generated_at_utc or utc_now(),
        "fixture_sha256": fixture_sha256,
        "case_count": baseline_summary["case_count"] if baseline_summary else None,
        "critical_category_policy": CRITICAL_CATEGORY_POLICY,
        "gate": dict(gate),
        "requested": list(requested),
        "baseline": baseline_summary,
        "comparisons": comparisons,
        "skipped": sorted(skips, key=lambda item: item["comparator"]),
        "product_engine_reference": product_engine_reference,
    }
    if product_engine_reference is not None and baseline_summary is not None:
        reference = product_engine_reference.get("score_points")
        if isinstance(reference, (int, float)) and not isinstance(reference, bool) and math.isfinite(reference):
            receipt["runtime_parity_delta_points"] = round(
                float(reference) - baseline_summary["score_points"], 6)
    return receipt


def write_comparison_receipt(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Write the receipt atomically; refuse to persist an oversize payload."""

    encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ComparisonError("receipt_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
    temporary.replace(path)
    return receipt


def arm_metrics_from_receipt(payload: Any) -> dict[str, Any]:
    """Extract the aggregate metrics from one salvaged per-arm receipt.

    ``case_indicators`` is optional in the schema but required for a paired
    interval; when absent the comparison falls back to the weaker unpaired
    method and records that in the receipt.
    """

    if not isinstance(payload, dict) or payload.get("schema") != ARM_SCHEMA:
        raise ComparisonError("comparator_receipt_invalid")
    if payload.get("status") not in {"verified", "completed_with_failures"}:
        raise ComparisonError("comparator_receipt_invalid")
    if payload.get("arm") not in KNOWN_ARMS:
        raise ComparisonError("comparator_receipt_invalid")
    metrics = normalize_metrics(payload.get("metrics"))
    if "case_indicators" in payload:
        normalize_cases(payload["case_indicators"], metrics["category_summary"])
        metrics["case_indicators"] = payload["case_indicators"]
    return metrics


def load_arm_metrics(path: Path) -> dict[str, Any]:
    """Read one salvaged per-arm receipt and return its aggregate metrics."""

    return arm_metrics_from_receipt(_bounded_json_file(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requested", default="q8_0", help="comma-separated comparator arms")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--fixture-sha256", default=None)
    args = parser.parse_args(argv)
    try:
        requested = tuple(part for part in args.requested.split(",") if part)
        gate = load_gate(args.spec, seed=args.seed)
        arms: dict[str, Any] = {}
        skipped: list[dict[str, str]] = []
        for arm in (BASELINE_ARM, *requested):
            path = args.receipt_dir / f"comparator-receipt-{arm}.json"
            if not path.is_file():
                if arm != BASELINE_ARM:
                    skipped.append(_skip_entry(arm, "comparator_receipt_missing"))
                continue
            try:
                arms[arm] = load_arm_metrics(path)
            except ComparisonError:
                if arm == BASELINE_ARM:
                    continue
                skipped.append(_skip_entry(arm, "comparator_receipt_invalid"))
        receipt = build_comparison_receipt(
            arms=arms, requested=requested, gate=gate,
            fixture_sha256=args.fixture_sha256, skipped=skipped,
        )
        write_comparison_receipt(args.output, receipt)
    except ComparisonError as exc:
        print(json.dumps({"schema": COMPARISON_SCHEMA, "status": "refused", "error_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"schema": receipt["schema"], "status": receipt["status"], "comparisons": len(receipt["comparisons"])}, sort_keys=True))
    return 0 if receipt["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
