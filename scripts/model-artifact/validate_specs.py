#!/usr/bin/env python3
"""Validate committed Phase 0 model specifications without model weights.

This validator intentionally uses only Python's standard library so it can run in
the controlled build environment before optional conversion dependencies exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def check_lock(path: Path) -> list[str]:
    lock = load(path)
    errors: list[str] = []
    revision = lock.get("revision")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        errors.append("source revision must be a 40-character lowercase commit")
    resolved = lock.get("resolved_from", {})
    if resolved.get("repository_sha_from_api") != revision:
        errors.append("API repository SHA does not equal locked revision")
    if not resolved.get("api_url", "").startswith("https://huggingface.co/api/models/Qwen/Qwen3.5-9B"):
        errors.append("source evidence must be the official Hugging Face model API")
    if resolved.get("private") is not False or resolved.get("disabled") is not False:
        errors.append("source must be public and enabled")
    files = lock.get("source_files", [])
    seen: set[str] = set()
    for item in files:
        name = item.get("path")
        if not isinstance(name, str) or name in seen:
            errors.append(f"duplicate or invalid source file: {name!r}")
        seen.add(name)
        size = item.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            errors.append(f"{name}: positive size required")
        digest = item.get("sha256") or item.get("lfs_sha256")
        if item.get("excluded_from_text_only") is True:
            if digest is not None:
                errors.append(f"{name}: excluded file must not claim a digest")
        elif not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            errors.append(f"{name}: sha256/lfs_sha256 required")
    if not lock.get("policy", {}).get("text_only"):
        errors.append("text-only policy must be true")
    if lock.get("policy", {}).get("weights_downloaded_during_phase_0") is not False:
        errors.append("Phase 0 must not download weights")
    receipts = lock.get("source_receipts", {})
    for key in ("model_card_sha256", "license_sha256", "tokenizer_sha256", "chat_template_sha256"):
        if not isinstance(receipts.get(key), str) or not SHA256_RE.fullmatch(receipts[key]):
            errors.append(f"source_receipts.{key} must be a SHA-256")
    return errors


def check_json_specs(root: Path) -> list[str]:
    errors: list[str] = []
    required = [
        root / "model/manifests/qwen35-9b-q4-k-m.schema.json",
        root / "model/conversion/qwen35-9b-conversion-spec.json",
        root / "model/quality-eval/quality-fixture-spec.json",
        root / "performance/harness/oracle-run-spec.json",
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"missing specification: {path}")
            continue
        try:
            load(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: invalid JSON ({exc})")
    fixture_path = root / "model/quality-eval/quality-fixture-spec.json"
    if fixture_path.is_file():
        fixture = load(fixture_path)
        categories = fixture.get("categories", [])
        for category in categories:
            if not isinstance(category.get("minimum_cases"), int) or category["minimum_cases"] <= 0:
                errors.append(f"quality category {category.get('id')!r}: positive minimum_cases required")
        if fixture.get("comparison", {}).get("bootstrap_resamples", 0) < 1000:
            errors.append("quality comparison must define at least 1000 bootstrap resamples")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    errors = check_lock(args.root / "model/source-lock/qwen35-9b.source-lock.json")
    errors.extend(check_json_specs(args.root))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("model/performance Phase 0 specifications: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
