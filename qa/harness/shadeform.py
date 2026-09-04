"""Assertions for auditable Shadeform jobs; no provisioning is performed."""

from __future__ import annotations

from typing import Any


def validate_job_manifest(job: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(job, dict):
        return ["job must be an object"]
    required = ("job_id", "profile_id", "budget", "lifecycle", "machine")
    errors.extend(f"missing:{key}" for key in required if key not in job)
    budget = job.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget must be an object")
    else:
        if not isinstance(budget.get("estimated_usd"), (int, float)) or budget.get("estimated_usd", 0) < 0:
            errors.append("budget.estimated_usd must be non-negative")
        if not isinstance(budget.get("ceiling_usd"), (int, float)) or budget.get("ceiling_usd", 0) <= 0:
            errors.append("budget.ceiling_usd must be positive")
        elif budget.get("estimated_usd", 0) > budget["ceiling_usd"]:
            errors.append("estimated cost exceeds ceiling")
        if budget.get("warnings") != [50, 80, 100]:
            errors.append("warnings must be exactly [50, 80, 100]")
    lifecycle = job.get("lifecycle")
    if not isinstance(lifecycle, dict):
        errors.append("lifecycle must be an object")
    else:
        if lifecycle.get("cleanup_owner") != "S4":
            errors.append("cleanup_owner must be S4")
        if lifecycle.get("ephemeral_credentials_removed") is not True:
            errors.append("ephemeral_credentials_removed must be true")
        if lifecycle.get("state") not in {"planned", "running", "terminated"}:
            errors.append("lifecycle.state invalid")
        if lifecycle.get("state") == "terminated" and lifecycle.get("cleanup_verified") is not True:
            errors.append("terminated jobs require cleanup_verified=true")
        duration = lifecycle.get("max_duration_minutes")
        backstop = lifecycle.get("provider_backstop_minutes")
        if not isinstance(duration, int) or not isinstance(backstop, int) or backstop <= duration:
            errors.append("provider backstop must exceed max duration")
    machine = job.get("machine")
    if not isinstance(machine, dict) or not machine.get("profile_id"):
        errors.append("machine.profile_id required")
    # Keep the schema metadata-only. Values under these names are never valid.
    encoded = repr(job).lower()
    if any(marker in encoded for marker in ("api_key", "access_token", "password", "secret_value")):
        errors.append("credential fields are forbidden")
    return errors


def warning_level(used_percent: float) -> str:
    if used_percent >= 100:
        return "STOP"
    if used_percent >= 80:
        return "URGENT"
    if used_percent >= 50:
        return "WARN"
    return "OK"
