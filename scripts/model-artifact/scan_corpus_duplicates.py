#!/usr/bin/env python3
"""Report near-duplicate quality-corpus cases by first-user-message similarity.

Standard library only. This is a review aid, not a gate: it never edits or deletes
a case and always exits 0. Two cases in the same category are "suspicious" when the
token-set Jaccard similarity of their normalized first user message is at or above
the threshold. Diagnostics print case ids and a score, never prompt text, per the
fixture spec's logging.never_record rule.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
CASES_RELATIVE = "model/quality-eval/cases"
DEFAULT_THRESHOLD = 0.9

WORD_RE = re.compile(r"[a-z0-9]+")
# Tokens that carry no discriminating signal for a task description.
STOPWORDS = frozenset(
    "a an and are as at be by do for from has have in is it its of on or that the "
    "then there this to with you your".split()
)


def normalize(text: str) -> frozenset[str]:
    return frozenset(WORD_RE.findall(text.lower())) - STOPWORDS


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def first_user_message(case: dict) -> str:
    for message in case.get("messages", []):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def scan(root: Path, threshold: float) -> tuple[dict[str, int], list[tuple[float, str, str, str]]]:
    per_category: dict[str, int] = {}
    pairs: list[tuple[float, str, str, str]] = []
    for path in sorted((root / CASES_RELATIVE).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        category = document.get("category", path.stem)
        entries = [(case["id"], normalize(first_user_message(case))) for case in document.get("cases", [])]
        count = 0
        for index, (left_id, left) in enumerate(entries):
            for right_id, right in entries[index + 1 :]:
                score = jaccard(left, right)
                if score >= threshold:
                    count += 1
                    pairs.append((score, category, left_id, right_id))
        per_category[category] = count
    pairs.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    return per_category, pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)

    per_category, pairs = scan(args.root.resolve(), args.threshold)
    print(f"near-duplicate scan: token-set Jaccard over the first user message, threshold {args.threshold}")
    print(f"{'category':<24}{'suspicious pairs':>18}")
    print("-" * 42)
    for category, count in per_category.items():
        print(f"{category:<24}{count:>18}")
    print("-" * 42)
    print(f"{'TOTAL':<24}{sum(per_category.values()):>18}")
    if pairs:
        print(f"\ntop {min(args.top, len(pairs))} pairs by similarity:")
        for score, category, left_id, right_id in pairs[: args.top]:
            print(f"  {score:.3f}  {category:<24} {left_id}  ~  {right_id}")
    else:
        print("\nno pair reached the threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
