#!/usr/bin/env python3
"""Ownership-bound, read-only Shadeform catalogue/plan preflight.

There are deliberately no create, terminate, delete, or account-wide operations
in this module. It accepts a redacted catalogue snapshot so planning can happen
without guessing provider API paths or spending money.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SECRET_KEYS = {"SHADEFORM_API_KEY", "SHADEFORM_SSH", "SHADEFORM_SSH_KEY_ID"}
CAP_KEYS = {"SHADEFORM_MAX_HOURLY_COST_USD", "SHADEFORM_MAX_TOTAL_COST_USD"}
# SSH material is intentionally not required for a catalogue GET or cost plan;
# it becomes a separate approval/validation gate before any instance mutation.
REQUIRED_KEYS = {"SHADEFORM_API_KEY"} | CAP_KEYS


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        raise ValueError(f"env file does not exist: {path}")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "git_access":
            raise ValueError("legacy lowercase git_access is prohibited")
        if key.startswith("SHADEFORM_"):
            values[key] = value
    return values


def redact(values: dict[str, str]) -> dict[str, object]:
    return {key: ("<present>" if key in SECRET_KEYS and value else "<missing>") if key in SECRET_KEYS else value for key, value in values.items()}


def cap_float(values: dict[str, str], key: str) -> float:
    try:
        value = float(values[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def read_catalogue(path: Path) -> dict:
    try:
        catalogue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid catalogue: {exc}") from exc
    return normalize_catalogue(catalogue)


def normalize_catalogue(catalogue: dict) -> dict:
    if not isinstance(catalogue, dict):
        raise ValueError("catalogue must be a JSON object")
    if isinstance(catalogue.get("profiles"), list):
        return catalogue
    # Official Shadeform /v1/instances/types response. Keep only planning
    # fields; availability and nested configuration are flattened without IDs
    # that could identify an account or instance.
    if isinstance(catalogue.get("instance_types"), list):
        profiles = []
        for item in catalogue["instance_types"]:
            if not isinstance(item, dict):
                continue
            config = item.get("configuration") if isinstance(item.get("configuration"), dict) else {}
            availability = item.get("availability") if isinstance(item.get("availability"), list) else []
            profiles.append({
                "id": item.get("shade_instance_type") or item.get("cloud_instance_type"),
                "name": item.get("shade_instance_type"),
                "hourly_usd": item.get("hourly_price"),
                "vcpus": item.get("vcpus", config.get("vcpus")),
                "memory_gib": item.get("memory_in_gb", config.get("memory_in_gb")),
                "gpu": item.get("gpu_type", config.get("gpu_type")),
                "gpu_manufacturer": config.get("gpu_manufacturer"),
                "cloud": item.get("cloud"),
                "region": item.get("region"),
                "available": any(bool(region.get("available")) for region in availability if isinstance(region, dict)),
                "interruptible": False,
            })
        return {"profiles": profiles, "source": "shadeform-instances-types"}
    raise ValueError("catalogue must have a profiles or instance_types array")


def fetch_catalogue(url: str, api_key: str | None) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("catalogue URL must use HTTPS")
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    if api_key:
        request.add_header("X-API-KEY", api_key)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(4 * 1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"read-only catalogue request failed: {exc}") from exc
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("catalogue response exceeds 4 MiB")
    try:
        catalogue = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"catalogue was not JSON: {exc}") from exc
    return normalize_catalogue(catalogue)


def select_profiles(catalogue: dict, values: dict[str, str]) -> list[dict]:
    max_hourly = cap_float(values, "SHADEFORM_MAX_HOURLY_COST_USD")
    selected: list[dict] = []
    for profile in catalogue["profiles"]:
        if not isinstance(profile, dict):
            continue
        if profile.get("available") is False:
            continue
        try:
            rate_value = float(profile.get("hourly_usd"))
        except (TypeError, ValueError):
            continue
        if 0 < rate_value <= max_hourly:
            selected.append({"id": profile.get("id"), "name": profile.get("name"), "hourly_usd": rate_value, "vcpus": profile.get("vcpus"), "memory_gib": profile.get("memory_gib"), "gpu": profile.get("gpu"), "interruptible": profile.get("interruptible", False)})
    return sorted(selected, key=lambda item: (not item["interruptible"], item["hourly_usd"]))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Shadeform preflight; never provisions or tears down instances")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--catalogue", type=Path, help="local JSON snapshot with a profiles array")
    parser.add_argument("--catalogue-url", help="explicit HTTPS GET-only catalogue endpoint")
    parser.add_argument("--output", type=Path, default=Path("out/evidence/shadeform-readonly-plan.json"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--hours", type=float, default=0.0)
    parser.add_argument("--record-ledger", type=Path)
    args = parser.parse_args()
    try:
        values = parse_env(args.env_file)
        missing = sorted(key for key in REQUIRED_KEYS if not values.get(key))
        if missing:
            raise ValueError("required Shadeform controls missing: " + ", ".join(missing))
        if args.hours < 0:
            raise ValueError("hours cannot be negative")
        if args.catalogue and args.catalogue_url:
            raise ValueError("choose one local catalogue or HTTPS catalogue URL")
        if args.catalogue:
            catalogue = read_catalogue(args.catalogue)
            catalogue_source = "local-snapshot"
        elif args.catalogue_url:
            catalogue = fetch_catalogue(args.catalogue_url, values.get("SHADEFORM_API_KEY"))
            catalogue_source = urllib.parse.urlparse(args.catalogue_url).hostname
        else:
            raise ValueError("a catalogue snapshot or explicit HTTPS catalogue URL is required")
        selected = select_profiles(catalogue, values)
        run_id = args.run_id or f"preflight-{uuid.uuid4().hex[:12]}"
        created = dt.datetime.now(dt.timezone.utc).isoformat()
        plan = {
            "schema_version": "1.0.0", "plan_kind": "shadeform-read-only-preflight", "created_at_utc": created,
            "owner": args.owner, "run_id": run_id, "operation": "read-only-catalogue-and-plan", "catalogue_source": catalogue_source,
            "policy": {"create": False, "provision": False, "terminate": False, "delete": False, "account_wide_mutation": False, "orphan_backstop_required": True, "salvage_before_teardown": True, "hours_requested": args.hours, "max_hourly_cost_usd": cap_float(values, "SHADEFORM_MAX_HOURLY_COST_USD"), "max_total_cost_usd": cap_float(values, "SHADEFORM_MAX_TOTAL_COST_USD")},
            "candidates": selected, "secret_presence": redact({key: values.get(key, "") for key in SECRET_KEYS}),
            "next_action": "Sol review required before J1M or any instance mutation",
        }
        write_json(args.output, plan)
        if args.record_ledger:
            event = {"timestamp_utc": created, "owner": args.owner, "run_id": run_id, "event": "readonly_preflight", "hours": args.hours, "estimated_cost_usd": None, "plan": str(args.output), "mutation": False}
            args.record_ledger.parent.mkdir(parents=True, exist_ok=True)
            with args.record_ledger.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        print(json.dumps({"plan": str(args.output), "candidate_count": len(selected), "operation": "read-only"}, sort_keys=True))
        return 0
    except ValueError as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
