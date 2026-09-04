#!/usr/bin/env python3
"""Ownership-bound, read-only Shadeform catalogue and cost-plan preflight.

There are deliberately no create, terminate, delete, or account-wide operations
in this module. Rates from the official API are cents per hour and are converted
to USD exactly once during normalization.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SECRET_KEYS = {"SHADEFORM_API_KEY", "SHADEFORM_SSH", "SHADEFORM_SSH_KEY_ID"}
CAP_KEYS = {"SHADEFORM_MAX_HOURLY_COST_USD", "SHADEFORM_MAX_TOTAL_COST_USD"}
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


def csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def int_policy(values: dict[str, str], key: str, default: int = 0) -> int:
    raw = values.get(key)
    if not raw:
        return default
    try:
        result = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{key} cannot be negative")
    return result


def float_policy(values: dict[str, str], keys: tuple[str, ...], default: float = 0.0) -> float:
    for key in keys:
        if values.get(key):
            try:
                result = float(values[key])
            except ValueError as exc:
                raise ValueError(f"{key} must be numeric") from exc
            if result < 0:
                raise ValueError(f"{key} cannot be negative")
            return result
    return default


def normalize_placements(item: dict) -> list[dict]:
    config = item.get("configuration") if isinstance(item.get("configuration"), dict) else {}
    raw_regions = item.get("availability") if isinstance(item.get("availability"), list) else []
    regions = [entry for entry in raw_regions if isinstance(entry, dict) and entry.get("available") is not False]
    if not regions:
        regions = [{"region": item.get("region"), "available": item.get("available", True)}]
    # Shadeform's API documents hourly_price as cents. Keep the source cents
    # alongside converted USD to make an accidental unit regression visible.
    raw_cents = item.get("hourly_price")
    try:
        hourly_cents = int(raw_cents)
    except (TypeError, ValueError):
        hourly_cents = None
    if hourly_cents is None or hourly_cents <= 0:
        return []
    base = {
        "id": item.get("shade_instance_type") or item.get("cloud_instance_type"),
        "name": item.get("shade_instance_type"),
        "hourly_price_cents": hourly_cents,
        "hourly_usd": hourly_cents / 100.0,
        "vcpus": item.get("vcpus", config.get("vcpus")),
        "memory_gib": item.get("memory_in_gb", config.get("memory_in_gb")),
        "gpu": item.get("gpu_type", config.get("gpu_type")),
        "gpu_manufacturer": config.get("gpu_manufacturer", item.get("gpu_manufacturer")),
        "gpu_count": item.get("num_gpus", config.get("num_gpus", 0)),
        "vram_gib": item.get("vram_per_gpu_in_gb", config.get("vram_per_gpu_in_gb")),
        "cloud": item.get("cloud"),
        "interruptible": bool(item.get("interruptible", config.get("interruptible", False))) or str(item.get("deployment_type", "")).lower() == "interruptible",
    }
    return [{**base, "region": region.get("region"), "available": region.get("available", True)} for region in regions]


def normalize_catalogue(catalogue: dict) -> dict:
    if not isinstance(catalogue, dict):
        raise ValueError("catalogue must be a JSON object")
    if isinstance(catalogue.get("profiles"), list):
        return catalogue
    if isinstance(catalogue.get("instance_types"), list):
        profiles: list[dict] = []
        for item in catalogue["instance_types"]:
            if isinstance(item, dict):
                profiles.extend(normalize_placements(item))
        return {"profiles": profiles, "source": "shadeform-instances-types"}
    raise ValueError("catalogue must have a profiles or instance_types array")


def read_catalogue(path: Path) -> dict:
    try:
        return normalize_catalogue(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid catalogue: {exc}") from exc


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
        return normalize_catalogue(json.loads(body))
    except json.JSONDecodeError as exc:
        raise ValueError(f"catalogue was not JSON: {exc}") from exc


def policy(values: dict[str, str]) -> dict[str, object]:
    return {
        "gpu_family_prefixes": csv_values(values.get("SHADEFORM_GPU_TYPES")),
        "gpu_count": int_policy(values, "SHADEFORM_GPU_COUNT"),
        "min_vram_gib": float_policy(values, ("SHADEFORM_MIN_VRAM_GB", "SHADEFORM_GPU_MIN_VRAM_GB")),
        "cloud_allowlist": csv_values(values.get("SHADEFORM_CLOUD")),
        "excluded_clouds": csv_values(values.get("SHADEFORM_EXCLUDED_CLOUDS")),
        "excluded_clouds_defaulted": not bool(csv_values(values.get("SHADEFORM_EXCLUDED_CLOUDS"))),
        "region": values.get("SHADEFORM_REGION") or None,
        "prefer_interruptible": values.get("SHADEFORM_PREFER_INTERRUPTIBLE", "false").lower() in {"1", "true", "yes", "on"},
        "max_hourly_cost_usd": cap_float(values, "SHADEFORM_MAX_HOURLY_COST_USD"),
        "max_total_cost_usd": cap_float(values, "SHADEFORM_MAX_TOTAL_COST_USD"),
    }


def identity(profile: dict) -> dict:
    return {key: profile.get(key) for key in ("id", "name", "cloud", "region", "gpu", "gpu_manufacturer", "gpu_count", "vram_gib", "hourly_price_cents", "hourly_usd", "interruptible")}


def profile_reasons(profile: dict, rules: dict[str, object], *, include_rate: bool = True) -> list[str]:
    reasons: list[str] = []
    prefixes = [str(value).lower() for value in rules["gpu_family_prefixes"]]
    gpu = str(profile.get("gpu") or "").lower()
    if prefixes and not any(gpu.startswith(prefix) for prefix in prefixes):
        reasons.append("gpu_family_not_preferred")
    if rules["gpu_count"] and profile.get("gpu_count") != rules["gpu_count"]:
        reasons.append("gpu_count_mismatch")
    vram = profile.get("vram_gib")
    if rules["min_vram_gib"] and (not isinstance(vram, (int, float)) or vram < rules["min_vram_gib"]):
        reasons.append("vram_below_minimum")
    clouds = [str(value).lower() for value in rules["cloud_allowlist"]]
    cloud = str(profile.get("cloud") or "").lower()
    if clouds and cloud not in clouds:
        reasons.append("cloud_not_allowlisted")
    excluded = [str(value).lower() for value in rules["excluded_clouds"]]
    if cloud in excluded:
        reasons.append("cloud_excluded")
    if rules["region"] and str(profile.get("region") or "").lower() != str(rules["region"]).lower():
        reasons.append("region_mismatch")
    if profile.get("available") is False:
        reasons.append("not_available")
    rate = profile.get("hourly_usd")
    if include_rate and (not isinstance(rate, (int, float)) or rate <= 0 or rate > rules["max_hourly_cost_usd"]):
        reasons.append("hourly_cost_over_cap")
    return reasons


def read_ledger(path: Path | None) -> tuple[float, list[dict], bool]:
    if not path or not path.exists():
        return 0.0, [], False
    total = 0.0
    pending: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read cost ledger: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ledger line {number} is invalid JSON") from exc
        status = str(event.get("status", event.get("cost_status", ""))).lower()
        if status == "pending" or event.get("pending") is True:
            pending.append({"line": number, "run_id": event.get("run_id"), "status": "pending"})
            continue
        for key in ("actual_cost_usd", "estimated_cost_usd"):
            value = event.get(key)
            if isinstance(value, (int, float)):
                total += float(value)
                break
    return total, pending, bool(pending)


def select_profiles(catalogue: dict, values: dict[str, str], hours: float, ledger_path: Path | None = None) -> tuple[list[dict], dict]:
    if hours <= 0:
        raise ValueError("requested runtime hours must be positive")
    rules = policy(values)
    spent, pending, has_pending = read_ledger(ledger_path)
    remaining = rules["max_total_cost_usd"] - spent
    all_profiles = [profile for profile in catalogue["profiles"] if isinstance(profile, dict)]
    eligible: list[dict] = []
    excluded: list[dict] = []
    for profile in all_profiles:
        reasons = profile_reasons(profile, rules)
        worst_case = float(profile.get("hourly_usd", 0) or 0) * hours
        if worst_case > remaining:
            reasons.append("worst_case_runtime_over_remaining_budget")
        if reasons or has_pending:
            excluded.append({"identity": identity(profile), "reasons": reasons + (["pending_ledger_cost"] if has_pending else [])})
        else:
            eligible.append({**identity(profile), "worst_case_runtime_hours": hours, "worst_case_runtime_cost_usd": round(worst_case, 6)})
    eligible.sort(key=lambda item: (not rules["prefer_interruptible"] or not item["interruptible"], item["hourly_usd"]))
    threshold = eligible[0]["hourly_usd"] if eligible else float("inf")
    forgone = [item for item in excluded if isinstance(item["identity"].get("hourly_usd"), (int, float)) and item["identity"]["hourly_usd"] < threshold]
    return eligible, {"rules": rules, "ledger_spent_usd": round(spent, 6), "remaining_total_project_budget_usd": round(remaining, 6), "pending_ledger_cost": has_pending, "pending_events": pending, "excluded_profiles": excluded, "forgone_cheaper_options": forgone}


def mutation_readiness(values: dict[str, str]) -> dict[str, object]:
    # SHADEFORM_SSH is the operator-owned private key material/path from which
    # the lifecycle creates a fresh, per-attempt provider key. A pre-existing
    # SHADEFORM_SSH_KEY_ID is intentionally not required (and is never reused).
    required = ("SHADEFORM_API_KEY", "SHADEFORM_SSH", "SHADEFORM_INSTANCE_NAME", "SHADEFORM_AUTO_TERMINATE_HOURS")
    missing = [key for key in required if not values.get(key)]
    invalid: list[str] = []
    key_id = values.get("SHADEFORM_SSH_KEY_ID", "")
    if key_id and (len(key_id) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", key_id)):
        invalid.append("SHADEFORM_SSH_KEY_ID_format")
    ssh_value = values.get("SHADEFORM_SSH", "")
    if ssh_value and any(character in ssh_value for character in "\r\n"):
        invalid.append("SHADEFORM_SSH_format")
    instance_name = values.get("SHADEFORM_INSTANCE_NAME", "")
    if instance_name and (len(instance_name) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", instance_name)):
        invalid.append("SHADEFORM_INSTANCE_NAME_format")
    auto_hours = values.get("SHADEFORM_AUTO_TERMINATE_HOURS")
    if auto_hours:
        try:
            if float(auto_hours) <= 0:
                missing.append("SHADEFORM_AUTO_TERMINATE_HOURS_positive")
        except ValueError:
            invalid.append("SHADEFORM_AUTO_TERMINATE_HOURS_numeric")
    missing_or_invalid = missing + invalid
    return {"read_only_catalogue_ready": bool(values.get("SHADEFORM_API_KEY")), "mutation_ready": not missing_or_invalid, "missing_or_invalid_inputs": missing_or_invalid, "ssh_key_mode": "ephemeral_from_SHADEFORM_SSH", "preexisting_ssh_key_id_required": False, "reason": "read-only GET may pass without SSH; future mutation requires validated API, ephemeral SSH material, instance name, and auto-terminate backstop"}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Shadeform preflight; never provisions or tears down instances")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--catalogue", type=Path, help="local JSON snapshot with a profiles or instance_types array")
    parser.add_argument("--catalogue-url", help="explicit HTTPS GET-only catalogue endpoint")
    parser.add_argument("--output", type=Path, default=Path("out/evidence/shadeform-readonly-plan.json"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--hours", type=float, required=True)
    parser.add_argument("--ledger", type=Path, help="append-only cost ledger to inspect before planning")
    parser.add_argument("--record-ledger", type=Path)
    args = parser.parse_args()
    try:
        values = parse_env(args.env_file)
        missing = sorted(key for key in REQUIRED_KEYS if not values.get(key))
        if missing:
            raise ValueError("required Shadeform controls missing: " + ", ".join(missing))
        if args.catalogue and args.catalogue_url:
            raise ValueError("choose one local catalogue or HTTPS catalogue URL")
        if not args.catalogue and not args.catalogue_url:
            raise ValueError("a catalogue snapshot or explicit HTTPS catalogue URL is required")
        if args.catalogue:
            catalogue, catalogue_source = read_catalogue(args.catalogue), "local-snapshot"
        else:
            catalogue, catalogue_source = fetch_catalogue(args.catalogue_url, values.get("SHADEFORM_API_KEY")), urllib.parse.urlparse(args.catalogue_url).hostname
        selected, report = select_profiles(catalogue, values, args.hours, args.ledger)
        run_id = args.run_id or f"preflight-{uuid.uuid4().hex[:12]}"
        created = dt.datetime.now(dt.timezone.utc).isoformat()
        plan = {"schema_version": "1.1.0", "plan_kind": "shadeform-read-only-preflight", "created_at_utc": created, "owner": args.owner, "run_id": run_id, "operation": "read-only-catalogue-and-plan", "catalogue_source": catalogue_source, "policy": {"create": False, "provision": False, "terminate": False, "delete": False, "account_wide_mutation": False, "orphan_backstop_required": True, "salvage_before_teardown": True, "requested_runtime_hours": args.hours}, "mutation_readiness": mutation_readiness(values), "selected_candidates": selected, "selection_report": report, "secret_presence": redact({key: values.get(key, "") for key in SECRET_KEYS}), "next_action": "Sol review required before J1M or any instance mutation"}
        write_json(args.output, plan)
        if report["pending_ledger_cost"]:
            print("preflight refused: pending ledger cost requires reconciliation", file=sys.stderr)
            return 3
        if args.record_ledger:
            event = {"timestamp_utc": created, "owner": args.owner, "run_id": run_id, "event": "readonly_preflight", "hours": 0.0, "estimated_cost_usd": None, "plan": "<redacted>", "mutation": False, "status": "complete"}
            args.record_ledger.parent.mkdir(parents=True, exist_ok=True)
            with args.record_ledger.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        print(json.dumps({"plan_written": True, "candidate_count": len(selected), "operation": "read-only"}, sort_keys=True))
        return 0
    except ValueError as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
