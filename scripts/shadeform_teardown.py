#!/usr/bin/env python3
"""Best-effort salvage followed by exact-resource deletion."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import shadeform_lifecycle as shadeform

ROOT = Path(__file__).resolve().parents[1]
MAX_LOCAL_SALVAGE_BYTES = 1_048_576
DELETION_INTENT_SCHEMA = "local_bmo.shadeform.deletion-intent.v1"
DELETION_RECEIPT_SCHEMA = "local_bmo.shadeform.deletion-receipt.v1"
DELETION_EVIDENCE_INDEX_SCHEMA = "local_bmo.shadeform.deletion-evidence-index.v1"
RECEIPT_ERROR_FIELDS = (
    "cost_bookkeeping_error_type",
    "attempt_reservation_error_type",
    "record_bookkeeping_error_type",
    "deletion_receipt_error_type",
    "ssh_key_cleanup_error_type",
    "clear_bookkeeping_error_type",
)


def _require_time(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("teardown deadline exhausted")


def _safe_error_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.replace("_", "").isalnum():
        raise ValueError("deletion receipt error type is invalid")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _owner_binding(record: shadeform.OwnedResource) -> dict[str, object]:
    shadeform._validate_owned_resource(record)
    if (
        not (record.instance_type or "").strip()
        or record.gpu_count is None
        or record.vram_gb is None
        or not (record.os_image or "").strip()
        or record.provider_delete_deadline_utc is None
    ):
        raise ValueError("owned resource lacks the complete immutable instance profile")
    created_at = _timestamp(record.created_at_utc, field="resource creation timestamp")
    provider_deadline = _timestamp(
        record.provider_delete_deadline_utc, field="provider delete deadline",
    )
    if not created_at < provider_deadline or (provider_deadline - created_at).total_seconds() > 259_200:
        raise ValueError("provider delete deadline is outside the bounded ownership window")
    fingerprint = record.ssh_public_key_fingerprint
    if fingerprint is None and record.ssh_public_key is not None:
        fingerprint = shadeform.ssh_public_key_fingerprint(record.ssh_public_key)
    if fingerprint is None:
        raise ValueError("owned resource lacks an SSH key fingerprint")
    return {
        "phase_id": record.phase_id,
        "instance_id": record.instance_id,
        "instance_name": record.instance_name or shadeform.owned_instance_name(record.run_id, record.ownership_nonce),
        "ownership_nonce": record.ownership_nonce,
        "ssh_key_id": record.ssh_key_id,
        "ssh_key_name": record.ssh_key_name,
        "ssh_key_fingerprint": fingerprint,
        "cloud": record.cloud,
        "region": record.region,
        "instance_type": record.instance_type,
        "gpu": record.gpu,
        "gpu_count": record.gpu_count,
        "vram_gb": record.vram_gb,
        "os_image": record.os_image,
        "hourly_usd": record.hourly_usd,
        "created_at_utc": record.created_at_utc,
        "provider_delete_deadline_utc": record.provider_delete_deadline_utc,
    }


def _record_from_owner(owner: object) -> shadeform.OwnedResource:
    expected_keys = {
        "phase_id", "instance_id", "instance_name", "ownership_nonce",
        "ssh_key_id", "ssh_key_name", "ssh_key_fingerprint", "cloud", "region",
        "instance_type", "gpu", "gpu_count", "vram_gb", "os_image", "hourly_usd",
        "created_at_utc",
        "provider_delete_deadline_utc",
    }
    if not isinstance(owner, dict) or set(owner) != expected_keys:
        raise ValueError("deletion owner binding has unknown or missing fields")
    return shadeform.OwnedResource(
        phase_id=owner["phase_id"], run_id="deletion-evidence", instance_id=owner["instance_id"],
        instance_name=owner["instance_name"], ownership_nonce=owner["ownership_nonce"],
        ssh_key_id=owner["ssh_key_id"], ssh_key_name=owner["ssh_key_name"],
        ssh_public_key_fingerprint=owner["ssh_key_fingerprint"], gpu=owner["gpu"],
        cloud=owner["cloud"], region=owner["region"], hourly_usd=owner["hourly_usd"],
        created_at_utc=owner["created_at_utc"], instance_type=owner["instance_type"],
        gpu_count=owner["gpu_count"], vram_gb=owner["vram_gb"], os_image=owner["os_image"],
        provider_delete_deadline_utc=owner["provider_delete_deadline_utc"],
    )


def _validate_owner_binding(owner: object) -> dict[str, object]:
    return _owner_binding(_record_from_owner(owner))


def _owner_evidence_namespace(record: shadeform.OwnedResource) -> str:
    """Return a bounded filename component derived from the exact owner."""

    binding = _owner_binding(record)
    encoded = json.dumps(
        binding, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _instance_evidence_locator(instance_id: str) -> str:
    exact = shadeform.validate_resource_id(instance_id, field="deletion evidence instance id")
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()


def _deletion_evidence_index_path(phase_id: str, instance_id: str) -> Path:
    phase = shadeform.validate_phase_id(phase_id)
    return shadeform.RUNTIME_ROOT / (
        f"{phase}.deletion-owner-{_instance_evidence_locator(instance_id)}.json"
    )


def _legacy_deletion_evidence_paths(phase_id: str) -> tuple[Path, Path, Path]:
    return shadeform.legacy_deletion_evidence_paths(phase_id)  # type: ignore[return-value]


def _refuse_legacy_deletion_evidence(phase_id: str) -> None:
    """Fail closed rather than assign phase-only evidence to a new owner."""

    try:
        shadeform.preflight_legacy_deletion_evidence(phase_id)
    except shadeform.ShadeformError as exc:
        raise RuntimeError("legacy deletion evidence requires manual recovery") from exc


def _deletion_evidence_index_payload(
    phase_id: str, record: shadeform.OwnedResource,
) -> dict[str, object]:
    phase = shadeform.validate_phase_id(phase_id)
    owner = _owner_binding(record)
    if owner["phase_id"] != phase:
        raise ValueError("deletion evidence owner is bound to another phase")
    return {
        "schema": DELETION_EVIDENCE_INDEX_SCHEMA,
        "phase_id": phase,
        "instance_id": owner["instance_id"],
        "owner_namespace": _owner_evidence_namespace(record),
        "owner": owner,
    }


def _bind_owner_evidence(
    phase_id: str, record: shadeform.OwnedResource,
) -> dict[str, object]:
    """Durably bind an exact instance locator to one immutable owner."""

    expected = _deletion_evidence_index_payload(phase_id, record)
    _refuse_legacy_deletion_evidence(phase_id)
    path = _deletion_evidence_index_path(phase_id, record.instance_id)
    data = (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_LOCAL_SALVAGE_BYTES:
        raise ValueError("deletion evidence index exceeds bound")

    durable_owner, _ = shadeform.read_phase_ownership(phase_id)
    if durable_owner is not None and _owner_binding(durable_owner) != expected["owner"]:
        raise RuntimeError("deletion evidence owner is not the durable phase owner")

    if durable_owner is None:
        # Once final cleanup clears the reusable phase ledger, the immutable
        # index is the only locator for this owner's retained history. Never
        # manufacture that trust anchor from a caller-supplied record.
        try:
            existing = shadeform.strict_json_object(
                shadeform.private_bounded_stable_bytes(
                    path, MAX_LOCAL_SALVAGE_BYTES, label="deletion evidence index",
                ),
                label="deletion evidence index",
            )
        except FileNotFoundError as exc:
            raise RuntimeError("deletion evidence owner lacks durable ownership proof") from exc
        except (OSError, ValueError, shadeform.ShadeformError) as exc:
            raise RuntimeError("deletion evidence index requires manual recovery") from exc
        if existing != expected or set(existing) != set(expected):
            raise RuntimeError("deletion evidence index belongs to a different owner")
        return expected

    try:
        shadeform.private_durable_create_new(
            path, data, label="deletion evidence index",
        )
    except FileExistsError:
        try:
            existing = shadeform.strict_json_object(
                shadeform.private_bounded_stable_bytes(
                    path, MAX_LOCAL_SALVAGE_BYTES, label="deletion evidence index",
                ),
                label="deletion evidence index",
            )
        except (OSError, ValueError, shadeform.ShadeformError) as exc:
            raise RuntimeError("deletion evidence index requires manual recovery") from exc
        if existing != expected or set(existing) != set(expected):
            raise RuntimeError("deletion evidence index belongs to a different owner")
    return expected


def _load_indexed_owner(
    phase_id: str, instance_id: str,
) -> shadeform.OwnedResource | None:
    """Resolve one exact completed owner without scanning phase history."""

    phase = shadeform.validate_phase_id(phase_id)
    exact = shadeform.validate_resource_id(instance_id, field="deletion evidence instance id")
    _refuse_legacy_deletion_evidence(phase)
    path = _deletion_evidence_index_path(phase, exact)
    try:
        payload = shadeform.strict_json_object(
            shadeform.private_bounded_stable_bytes(
                path, MAX_LOCAL_SALVAGE_BYTES, label="deletion evidence index",
            ),
            label="deletion evidence index",
        )
        owner = _record_from_owner(payload.get("owner"))
        expected = _deletion_evidence_index_payload(phase, owner)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, shadeform.ShadeformError) as exc:
        raise RuntimeError("deletion evidence index requires manual recovery") from exc
    if (
        set(payload) != set(expected)
        or payload != expected
        or payload.get("instance_id") != exact
    ):
        raise RuntimeError("deletion evidence index requires manual recovery")
    return owner


def _owner_evidence_path(
    phase_id: str, record: shadeform.OwnedResource, kind: str,
) -> Path:
    if kind not in {"intent", "confirmation", "receipt"}:
        raise ValueError("deletion evidence kind is invalid")
    phase = shadeform.validate_phase_id(phase_id)
    binding = _owner_binding(record)
    if binding["phase_id"] != phase:
        raise ValueError("deletion evidence owner is bound to another phase")
    namespace = _owner_evidence_namespace(record)
    return shadeform.RUNTIME_ROOT / f"{phase}.{namespace}.deletion-{kind}.json"


def _normalized_deletion(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    success = raw.get("success") is True
    confirmed = raw.get("confirmed_at_utc")
    if confirmed is not None:
        confirmed = _timestamp(confirmed, field="deletion confirmation timestamp").isoformat()
    evidence = raw.get("evidence")
    allowed_evidence = {
        "unconfirmed", "intent-reconciled", "provider-404", "deleted",
        "absent", "provider-confirmed",
    }
    if evidence is not None and evidence not in allowed_evidence:
        raise ValueError("deletion receipt evidence is invalid")
    if evidence is None:
        evidence = "unconfirmed"
    if success and evidence == "unconfirmed":
        if raw.get("reconciled_from_deletion_intent") is True:
            evidence = "intent-reconciled"
        elif raw.get("already_absent") is True:
            evidence = "provider-404"
        elif raw.get("status") in {"deleted", "absent"}:
            evidence = str(raw["status"])
        else:
            evidence = "provider-confirmed"
    if not success and evidence != "unconfirmed":
        raise ValueError("failed deletion receipt cannot claim confirmation evidence")
    return {
        "success": success,
        "evidence": evidence,
        "confirmed_at_utc": confirmed,
        "reconciled_from_deletion_intent": raw.get("reconciled_from_deletion_intent") is True,
        "error_type": _safe_error_type(raw.get("error_type")),
    }


def _normalized_salvage(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    status = raw.get("status")
    if status not in {"nothing_available", "not_available", "salvaged", "salvage_failed"}:
        status = "not_available"
    name = raw.get("name") if status == "salvaged" else None
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 255 or Path(name).name != name):
        raise ValueError("salvage receipt name is invalid")
    size = raw.get("size_bytes") if status == "salvaged" else None
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_LOCAL_SALVAGE_BYTES):
        raise ValueError("salvage receipt size is invalid")
    return {
        "status": status,
        "name": name,
        "size_bytes": size,
        "error_type": _safe_error_type(raw.get("error_type")),
    }


def _canonical_deletion_receipt(phase_id: str, payload: dict[str, object]) -> dict[str, object]:
    owner = _validate_owner_binding(payload.get("owner"))
    if owner["phase_id"] != phase_id or payload.get("phase_id") != phase_id or payload.get("instance_id") != owner["instance_id"]:
        raise ValueError("deletion receipt owner binding does not match its path")
    status = payload.get("status", "recovery-pending")
    if status not in {"delete-failed", "recovery-pending", "deleted-cost-bookkeeping-failed", "deleted-key-cleanup-failed", "complete"}:
        raise ValueError("deletion receipt status is invalid")
    actual = payload.get("actual_cost_usd")
    if actual is not None and not shadeform._valid_cost(actual):
        raise ValueError("deletion receipt cost is invalid")
    canonical: dict[str, object] = {
        "schema": DELETION_RECEIPT_SCHEMA,
        "phase_id": phase_id,
        "instance_id": owner["instance_id"],
        "owner": owner,
        "status": status,
        "deletion": _normalized_deletion(payload.get("deletion")),
        "salvage": _normalized_salvage(payload.get("salvage")),
        "actual_cost_usd": actual,
        "attempt_reservation_settled": payload.get("attempt_reservation_settled") is True,
        "owned_record_persisted": payload.get("owned_record_persisted") is True,
        "key_cleanup_pending": payload.get("key_cleanup_pending") is True,
        "key_cleanup_completed": payload.get("key_cleanup_completed") is True,
        "key_cleanup_deferred": payload.get("key_cleanup_deferred") is True,
        "retry_required": payload.get("retry_required") is True,
    }
    for field in RECEIPT_ERROR_FIELDS:
        canonical[field] = _safe_error_type(payload.get(field))
    if canonical["deletion"]["success"] is True and canonical["deletion"]["confirmed_at_utc"] is None:
        raise ValueError("successful deletion receipt lacks an immutable confirmation timestamp")
    if status == "complete" and (
        canonical["retry_required"] is True
        or canonical["deletion"]["success"] is not True
        or canonical["actual_cost_usd"] is None
        or canonical["attempt_reservation_settled"] is not True
        or canonical["owned_record_persisted"] is not True
        or canonical["key_cleanup_pending"] is True
        or canonical["key_cleanup_completed"] is not True
        or canonical["key_cleanup_deferred"] is True
    ):
        raise ValueError("complete deletion receipt is incoherent")
    if status == "complete" and any(canonical[field] is not None for field in RECEIPT_ERROR_FIELDS):
        raise ValueError("complete deletion receipt retains an unresolved error")
    return canonical


def _write_deletion_receipt(phase_id: str, payload: dict[str, object], *, deadline: float | None = None) -> dict[str, object]:
    _require_time(deadline)
    canonical = _canonical_deletion_receipt(phase_id, payload)
    record = _record_from_owner(canonical["owner"])
    _bind_owner_evidence(phase_id, record)
    path = _deletion_receipt_path(phase_id, record)
    data = (json.dumps(canonical, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_LOCAL_SALVAGE_BYTES:
        raise ValueError("deletion receipt exceeds bound")
    shadeform.private_durable_atomic_write(
        path, data, label="deletion receipt",
    )
    return canonical


def _persist_deletion_receipt(phase_id: str, payload: dict[str, object], *, deadline: float | None = None) -> dict[str, object]:
    """Return canonical state without trusting a monkeypatched writer result."""

    canonical = _canonical_deletion_receipt(phase_id, payload)
    _write_deletion_receipt(phase_id, canonical, deadline=deadline)
    return canonical


def salvage_local(source: Path | None, destination: Path, *, deadline: float | None = None) -> dict[str, object]:
    if source is None:
        return {"status": "nothing_available"}
    try:
        _require_time(deadline)
        data = shadeform.bounded_stable_bytes(source, MAX_LOCAL_SALVAGE_BYTES, label="local salvage source")
        _require_time(deadline)
        target = destination / source.name
        shadeform.durable_create_new(target, data)
        _require_time(deadline)
        saved = shadeform.bounded_stable_bytes(target, MAX_LOCAL_SALVAGE_BYTES, label="local salvage destination")
        if saved != data:
            raise OSError("durable salvage verification mismatch")
    except FileNotFoundError:
        return {"status": "nothing_available"}
    except (OSError, shadeform.ShadeformError) as exc:
        return {"status": "salvage_failed", "error_type": type(exc).__name__}
    return {"status": "salvaged", "name": target.name, "size_bytes": len(data)}


def _load_deletion_receipt(phase_id: str, exact: str, record: shadeform.OwnedResource | None = None) -> dict[str, object] | None:
    exact = shadeform.validate_resource_id(exact, field="deletion receipt instance id")
    if record is None:
        record = _load_indexed_owner(phase_id, exact)
        if record is None:
            return None
    _bind_owner_evidence(phase_id, record)
    if record.instance_id != exact:
        raise RuntimeError("deletion receipt owner does not match the exact resource")
    path = _deletion_receipt_path(phase_id, record)
    try:
        payload = shadeform.strict_json_object(
            shadeform.private_bounded_stable_bytes(
                path, MAX_LOCAL_SALVAGE_BYTES, label="deletion receipt",
            ),
            label="deletion receipt",
        )
        canonical = _canonical_deletion_receipt(phase_id, payload)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, shadeform.ShadeformError) as exc:
        raise RuntimeError("deletion receipt requires manual recovery") from exc
    if set(payload) != set(canonical) or payload != canonical or canonical.get("instance_id") != exact:
        raise RuntimeError("deletion receipt requires manual recovery")
    if record is not None and canonical.get("owner") != _owner_binding(record):
        raise RuntimeError("deletion receipt owner does not match the exact resource")
    return canonical


def _deletion_intent_path(phase_id: str, record: shadeform.OwnedResource) -> Path:
    return _owner_evidence_path(phase_id, record, "intent")


def _deletion_confirmation_path(phase_id: str, record: shadeform.OwnedResource) -> Path:
    return _owner_evidence_path(phase_id, record, "confirmation")


def _deletion_receipt_path(phase_id: str, record: shadeform.OwnedResource) -> Path:
    return _owner_evidence_path(phase_id, record, "receipt")


def _deletion_intent_payload(
    phase_id: str,
    record: shadeform.OwnedResource,
    *,
    status: str,
    dispatched_at_utc: str,
    confirmation_ceiling_utc: str,
    confirmed_at_utc: str | None = None,
) -> dict[str, object]:
    if status not in {"dispatched", "confirmed"}:
        raise ValueError("invalid deletion intent status")
    dispatched = _timestamp(dispatched_at_utc, field="deletion dispatch timestamp")
    ceiling = _timestamp(confirmation_ceiling_utc, field="deletion confirmation ceiling")
    expected_ceiling = _timestamp(
        record.provider_delete_deadline_utc, field="provider delete deadline",
    )
    if ceiling != expected_ceiling or ceiling < dispatched:
        raise ValueError("deletion confirmation ceiling is invalid")
    if status == "confirmed":
        confirmed = _timestamp(confirmed_at_utc, field="deletion confirmation timestamp")
        if not dispatched <= confirmed <= ceiling:
            raise ValueError("deletion confirmation timestamp is outside its immutable bound")
        confirmed_at_utc = confirmed.isoformat()
    elif confirmed_at_utc is not None:
        raise ValueError("dispatched deletion intent cannot contain confirmation")
    payload: dict[str, object] = {
        "schema": DELETION_INTENT_SCHEMA,
        "owner": _owner_binding(record),
        "status": status,
        "dispatched_at_utc": dispatched.isoformat(),
        "confirmation_ceiling_utc": ceiling.isoformat(),
        "confirmed_at_utc": confirmed_at_utc,
    }
    return payload


def _write_deletion_intent(
    phase_id: str,
    record: shadeform.OwnedResource,
    *,
    status: str,
    confirmed_at_utc: str | None = None,
    prior: dict[str, object] | None = None,
    deadline: float | None = None,
) -> dict[str, object]:
    _require_time(deadline)
    if prior is None:
        now = shadeform.utc_now()
        ceiling = _timestamp(
            record.provider_delete_deadline_utc, field="provider delete deadline",
        )
        dispatched_at = now.isoformat()
        ceiling_at = ceiling.isoformat()
    else:
        dispatched_at = str(prior.get("dispatched_at_utc", ""))
        ceiling_at = str(prior.get("confirmation_ceiling_utc", ""))
    payload = _deletion_intent_payload(
        phase_id,
        record,
        status=status,
        dispatched_at_utc=dispatched_at,
        confirmation_ceiling_utc=ceiling_at,
        confirmed_at_utc=confirmed_at_utc,
    )
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_LOCAL_SALVAGE_BYTES:
        raise ValueError("deletion intent exceeds bound")
    _bind_owner_evidence(phase_id, record)
    path = (
        _deletion_intent_path(phase_id, record)
        if status == "dispatched"
        else _deletion_confirmation_path(phase_id, record)
    )
    try:
        shadeform.private_durable_create_new(
            path, data, label="deletion intent" if status == "dispatched" else "deletion confirmation",
        )
    except FileExistsError:
        existing = shadeform.strict_json_object(
            shadeform.private_bounded_stable_bytes(
                path, MAX_LOCAL_SALVAGE_BYTES,
                label="existing deletion evidence",
            ),
            label="existing deletion evidence",
        )
        if existing != payload:
            raise RuntimeError("refusing to replace different immutable deletion evidence")
    return payload


def _load_deletion_intent(phase_id: str, record: shadeform.OwnedResource) -> dict[str, object] | None:
    _bind_owner_evidence(phase_id, record)
    path = _deletion_intent_path(phase_id, record)
    try:
        payload = shadeform.strict_json_object(
            shadeform.private_bounded_stable_bytes(
                path, MAX_LOCAL_SALVAGE_BYTES, label="deletion intent",
            ),
            label="deletion intent",
        )
        dispatched = _deletion_intent_payload(
            phase_id,
            record,
            status="dispatched",
            dispatched_at_utc=str(payload.get("dispatched_at_utc", "")),
            confirmation_ceiling_utc=str(payload.get("confirmation_ceiling_utc", "")),
            confirmed_at_utc=None,
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, shadeform.ShadeformError) as exc:
        raise RuntimeError("deletion intent requires manual recovery") from exc
    if set(payload) != set(dispatched) or payload != dispatched:
        raise RuntimeError("deletion intent requires manual recovery")
    confirmation_path = _deletion_confirmation_path(phase_id, record)
    try:
        confirmation_payload = shadeform.strict_json_object(
            shadeform.private_bounded_stable_bytes(
                confirmation_path, MAX_LOCAL_SALVAGE_BYTES,
                label="deletion confirmation",
            ),
            label="deletion confirmation",
        )
    except FileNotFoundError:
        return dispatched
    try:
        confirmed = _deletion_intent_payload(
            phase_id,
            record,
            status="confirmed",
            dispatched_at_utc=str(confirmation_payload.get("dispatched_at_utc", "")),
            confirmation_ceiling_utc=str(confirmation_payload.get("confirmation_ceiling_utc", "")),
            confirmed_at_utc=confirmation_payload.get("confirmed_at_utc"),
        )
    except (OSError, ValueError, shadeform.ShadeformError) as exc:
        raise RuntimeError("deletion confirmation requires manual recovery") from exc
    if (
        set(confirmation_payload) != set(confirmed)
        or confirmation_payload != confirmed
        or confirmed["owner"] != dispatched["owner"]
        or confirmed["dispatched_at_utc"] != dispatched["dispatched_at_utc"]
        or confirmed["confirmation_ceiling_utc"] != dispatched["confirmation_ceiling_utc"]
    ):
        raise RuntimeError("deletion confirmation requires manual recovery")
    return confirmed


def _deterministic_confirmation_timestamp(intent: dict[str, object]) -> str:
    """Return the precommitted provider backstop, never retry wall time.

    Provider absence can be observed immediately before the immutable
    confirmation write fails or the process exits.  Without a separately
    durable observation, using the wall clock on retry would change settled
    cost.  The owner-bound provider deadline is committed before creation and
    is therefore the conservative, bounded timestamp that survives that
    crash window.
    """

    dispatched = _timestamp(intent.get("dispatched_at_utc"), field="deletion dispatch timestamp")
    ceiling = _timestamp(intent.get("confirmation_ceiling_utc"), field="deletion confirmation ceiling")
    if dispatched > ceiling:
        raise ValueError("deletion dispatch exceeds its immutable provider backstop")
    return ceiling.isoformat()


def _same_owner(left: shadeform.OwnedResource, right: shadeform.OwnedResource) -> bool:
    return _owner_binding(left) == _owner_binding(right)


def _owned_state(phase_id: str, exact: str) -> tuple[shadeform.OwnedResource | None, bool]:
    """Return one exact normal/recovery record, refusing split ownership."""

    record, had_normal = shadeform.read_phase_ownership(phase_id)
    if record is not None and record.instance_id != exact:
        raise RuntimeError("refusing teardown: requested ID is not the exact phase-owned resource")
    return record, had_normal


def _persist_recovery_owner(record: shadeform.OwnedResource) -> shadeform.OwnedResource:
    """Publish an alternate caller's full owner proof before any DELETE."""

    existing, _ = shadeform.read_phase_ownership(record.phase_id)
    if existing is not None and not _same_owner(existing, record):
        raise RuntimeError("refusing recovery teardown for a differently owned phase")
    if existing is None:
        shadeform.write_recovery_owned_resource(record)
    persisted, _ = _owned_state(record.phase_id, record.instance_id)
    if persisted is None or not _same_owner(persisted, record):
        raise RuntimeError("recovery ownership proof was not durably published")
    return persisted


def _ensure_recovery_pending_cost(record: shadeform.OwnedResource) -> dict[str, object]:
    """Reuse or conservatively create one exact-owner pending reservation.

    Both launchers append the exact-instance pending event before publishing the
    normal owner record.  If that later publication fails, recovery must not
    append a slightly lower estimate derived from a later timestamp.  The
    immutable exact-owner row is reused unchanged and is also required to cover
    at least the remaining provider-backed ownership window.
    """

    created_at = _timestamp(record.created_at_utc, field="resource creation timestamp")
    conservative_until = _timestamp(
        record.provider_delete_deadline_utc, field="provider delete deadline",
    )
    minimum_estimate = round(
        record.hourly_usd
        * max(0.0, (conservative_until - created_at).total_seconds())
        / 3600.0,
        6,
    )
    if not shadeform._valid_cost(minimum_estimate):
        raise ValueError("recovery pending cost is invalid")
    existing = shadeform.exact_owner_cost_state(
        record.phase_id, record.ownership_nonce, record.instance_id,
    )
    if existing is not None:
        if existing.get("status") != "pending":
            raise RuntimeError("recovery exact-owner cost is already terminal")
        estimate = existing.get("estimated_cost_usd")
        if not shadeform._valid_cost(estimate) or float(estimate) < minimum_estimate:
            raise RuntimeError("recovery exact-owner pending cost is under-reserved")
        return existing
    shadeform.append_cost_event({
        "instance_id": record.instance_id,
        "phase_id": record.phase_id,
        "ownership_nonce": record.ownership_nonce,
        "status": "pending",
        "estimated_cost_usd": minimum_estimate,
        "reservation": "recovery-owned-instance-before-delete",
    })
    persisted = shadeform.exact_owner_cost_state(
        record.phase_id, record.ownership_nonce, record.instance_id,
    )
    if (
        persisted is None
        or persisted.get("status") != "pending"
        or persisted.get("estimated_cost_usd") != minimum_estimate
    ):
        raise RuntimeError("recovery pending cost was not durably published")
    return persisted


def teardown_recovered_exact(
    record: shadeform.OwnedResource,
    *,
    env_file: Path = ROOT / ".env",
    salvage: Path | None = None,
    salvage_destination: Path = ROOT / "experiments" / "results",
    deadline: float | None = None,
) -> dict[str, object]:
    """Adopt a fully bound unrecorded resource into the same teardown machine."""

    shadeform._validate_owned_resource(record)
    phase_id = shadeform.validate_phase_id(record.phase_id)
    exact = shadeform.validate_resource_id(record.instance_id, field="recovered instance id")
    # Validate the complete immutable provider/key profile before publishing
    # recovery ownership.  An incomplete caller must not leave behind a
    # durable record that can neither be proved nor safely removed.
    _owner_binding(record)
    with shadeform.phase_cleanup_lock(phase_id):
        _persist_recovery_owner(record)
        return _teardown_exact_locked(
            phase_id,
            exact,
            env_file=env_file,
            salvage=salvage,
            salvage_destination=salvage_destination,
            deadline=deadline,
        )


def teardown_exact(phase_id: str, instance_id: str, *, env_file: Path = ROOT / ".env", salvage: Path | None = None, salvage_destination: Path = ROOT / "experiments" / "results", deadline: float | None = None) -> dict[str, object]:
    phase_id = shadeform.validate_phase_id(phase_id)
    exact = shadeform.validate_resource_id(instance_id, field="requested instance id")
    with shadeform.phase_cleanup_lock(phase_id):
        current, _ = _owned_state(phase_id, exact)
        if current is None:
            evidence_owner = _load_indexed_owner(phase_id, exact)
            if evidence_owner is None:
                raise RuntimeError("refusing teardown: no durable exact ownership evidence")
            prior = _load_deletion_receipt(phase_id, exact, evidence_owner)
            if prior is None:
                raise RuntimeError("refusing teardown: no durable exact ownership evidence")
            intent = _load_deletion_intent(phase_id, evidence_owner)
            deletion = prior.get("deletion")
            if (
                prior.get("status") != "complete"
                or prior.get("retry_required") is not False
                or not isinstance(deletion, dict)
                or deletion.get("success") is not True
                or intent is None
                or intent.get("status") != "confirmed"
                or deletion.get("confirmed_at_utc") != intent.get("confirmed_at_utc")
            ):
                raise RuntimeError("deletion evidence is incomplete; manual recovery is required")
            fingerprint = evidence_owner.ssh_public_key_fingerprint
            if fingerprint is None and evidence_owner.ssh_public_key is not None:
                fingerprint = shadeform.ssh_public_key_fingerprint(
                    evidence_owner.ssh_public_key,
                )
            if fingerprint is None or not shadeform.ssh_key_deletion_is_confirmed(
                phase_id,
                evidence_owner.ssh_key_id,
                ownership_nonce=evidence_owner.ownership_nonce,
                expected_name=evidence_owner.ssh_key_name,
                expected_fingerprint=fingerprint,
            ):
                raise RuntimeError("SSH key deletion evidence is incomplete")
            return prior
        return _teardown_exact_locked(phase_id, exact, env_file=env_file, salvage=salvage, salvage_destination=salvage_destination, deadline=deadline)


def _teardown_exact_locked(phase_id: str, exact: str, *, env_file: Path, salvage: Path | None, salvage_destination: Path, deadline: float | None) -> dict[str, object]:
    record, had_normal_record = _owned_state(phase_id, exact)
    if record is None:
        raise RuntimeError("refusing teardown: no exact durable ownership record")
    prior = _load_deletion_receipt(phase_id, exact, record)
    intent = _load_deletion_intent(phase_id, record)
    deletion = dict(prior["deletion"]) if prior is not None else None
    salvage_receipt = dict(prior["salvage"]) if prior is not None else None
    receipt: dict[str, object] = dict(prior) if prior is not None else {
        "schema": DELETION_RECEIPT_SCHEMA,
        "phase_id": phase_id,
        "instance_id": exact,
        "owner": _owner_binding(record),
    }
    deletion_confirmed = bool(
        isinstance(deletion, dict)
        and deletion.get("success") is True
        and intent is not None
        and intent.get("status") == "confirmed"
        and deletion.get("confirmed_at_utc") == intent.get("confirmed_at_utc")
    )
    if isinstance(deletion, dict) and deletion.get("success") is True and not deletion_confirmed:
        raise RuntimeError("successful deletion receipt is not bound to a confirmed intent")

    # A legacy receipt's boolean cannot prove remote key absence. The exact
    # owner-bound key state machine below must find its durable confirmation or
    # reconcile the provider before final completion/ownership clearing.
    api_key = shadeform.require_env(shadeform.load_env(env_file), "SHADEFORM_API_KEY")

    if not deletion_confirmed:
        try:
            salvage_receipt = salvage_local(salvage, salvage_destination, deadline=deadline)
        except Exception as exc:
            salvage_receipt = {"status": "salvage_failed", "error_type": type(exc).__name__}
        if intent is not None and intent.get("status") == "dispatched":
            # Reconcile an exact 404 or full-profile-verified authoritative
            # `deleted` status without a second DELETE.
            try:
                info = shadeform.verify_owned_instance_before_delete(api_key, phase_id, record, deadline=deadline)
                if info.get("status") == "deleted":
                    confirmed_at = _deterministic_confirmation_timestamp(intent)
                    intent = _write_deletion_intent(
                        phase_id, record, status="confirmed", confirmed_at_utc=confirmed_at,
                        prior=intent, deadline=deadline,
                    )
                    deletion = {
                        "success": True,
                        "status": "deleted",
                        "reconciled_from_deletion_intent": True,
                        "confirmed_at_utc": confirmed_at,
                    }
                else:
                    # The crash may have occurred after the durable intent but
                    # before the POST entered the kernel. A still-present,
                    # freshly full-profile-verified exact resource may receive
                    # the same idempotent DELETE; a `deleted` status above may
                    # not.
                    deletion = shadeform._delete_instance(
                        api_key, phase_id, exact, deadline=deadline,
                    )
            except shadeform.ShadeformHTTPError as exc:
                if exc.status == 404:
                    confirmed_at = _deterministic_confirmation_timestamp(intent)
                    intent = _write_deletion_intent(
                        phase_id, record, status="confirmed", confirmed_at_utc=confirmed_at,
                        prior=intent, deadline=deadline,
                    )
                    deletion = {
                        "success": True,
                        "already_absent": True,
                        "reconciled_from_deletion_intent": True,
                        "confirmed_at_utc": confirmed_at,
                    }
                else:
                    deletion = {"success": False, "error_type": type(exc).__name__}
            except Exception as exc:
                deletion = {"success": False, "error_type": type(exc).__name__}
        elif intent is not None and intent.get("status") == "confirmed":
            deletion = {
                "success": True,
                "reconciled_from_deletion_intent": True,
                "confirmed_at_utc": intent.get("confirmed_at_utc"),
            }
        elif intent is None:
            try:
                if not had_normal_record:
                    _ensure_recovery_pending_cost(record)
                fresh_info = shadeform.verify_owned_instance_before_delete(
                    api_key, phase_id, record, deadline=deadline,
                )
                if fresh_info.get("status") in {"deleted", "absent", "deleting"}:
                    raise RuntimeError("provider absence without a prior deletion intent requires manual recovery")
                intent = _write_deletion_intent(phase_id, record, status="dispatched", deadline=deadline)
                deletion = shadeform._delete_instance(api_key, phase_id, exact, deadline=deadline)
            except Exception as exc:
                deletion = {"success": False, "error_type": type(exc).__name__}
        if not isinstance(deletion, dict) or deletion.get("success") is not True:
            record.status = "delete-failed"
            try:
                if had_normal_record:
                    shadeform.write_owned_resource(record)
            except Exception:
                pass
            receipt.update({
                "status": "delete-failed", "deletion": deletion,
                "salvage": salvage_receipt, "retry_required": True,
            })
            _write_deletion_receipt(phase_id, receipt, deadline=deadline)
            raise RuntimeError("provider did not confirm exact-resource deletion")
        if intent is None:
            raise RuntimeError("provider deletion lacks a durable dispatched intent")
        if intent.get("status") != "confirmed":
            try:
                confirmed_at = _deterministic_confirmation_timestamp(intent)
                intent = _write_deletion_intent(
                    phase_id, record, status="confirmed", confirmed_at_utc=confirmed_at,
                    prior=intent, deadline=deadline,
                )
                deletion = {**deletion, "confirmed_at_utc": confirmed_at}
            except Exception as exc:
                deletion = {"success": False, "error_type": type(exc).__name__}
                receipt.update({
                    "status": "delete-failed", "deletion": deletion,
                    "salvage": salvage_receipt, "retry_required": True,
                })
                _write_deletion_receipt(phase_id, receipt, deadline=deadline)
                raise RuntimeError("provider deletion confirmation could not be persisted") from exc
        deletion_confirmed = True
        receipt.update({"deletion": deletion, "salvage": salvage_receipt})

    if intent is None or intent.get("status") != "confirmed":
        raise RuntimeError("deletion confirmation evidence is incomplete")
    if not isinstance(deletion, dict) or deletion.get("confirmed_at_utc") != intent.get("confirmed_at_utc"):
        raise RuntimeError("receipt and intent deletion timestamps disagree")

    if record.status in {"created", "active", "delete-failed"}:
        record.status = "deleted"
    attempt_settled = False
    if shadeform._valid_cost(receipt.get("actual_cost_usd")):
        record.cost_usd = float(receipt["actual_cost_usd"])
        receipt["cost_bookkeeping_error_type"] = None
    else:
        try:
            _require_time(deadline)
            created_at = _timestamp(record.created_at_utc, field="resource creation timestamp")
            confirmed_at = _timestamp(intent.get("confirmed_at_utc"), field="deletion confirmation timestamp")
            if confirmed_at < created_at:
                raise ValueError("deletion confirmation predates resource creation")
            elapsed_hours = max(0.0, (confirmed_at - created_at).total_seconds() / 3600.0)
            settled_cost = round(record.hourly_usd * elapsed_hours, 6)
            if not shadeform._valid_cost(settled_cost):
                raise ValueError("computed settled cost is not finite and nonnegative")
            shadeform.append_cost_event({
                "instance_id": exact,
                "phase_id": phase_id,
                "ownership_nonce": record.ownership_nonce,
                "status": "settled",
                "actual_cost_usd": settled_cost,
            })
            receipt["actual_cost_usd"] = settled_cost
            record.cost_usd = settled_cost
            receipt.pop("cost_bookkeeping_error_type", None)
        except Exception as exc:
            # Deletion already succeeded. Preserve the failure and retry from
            # this exact durable receipt without a second provider delete.
            receipt["cost_bookkeeping_error_type"] = type(exc).__name__
            receipt["retry_required"] = True
            record.status = "deleted-cost-bookkeeping-failed"
    if receipt.get("attempt_reservation_settled") is True:
        attempt_settled = True
        receipt["attempt_reservation_error_type"] = None
    elif "actual_cost_usd" in receipt and record.cost_usd is not None:
        # The pre-create attempt is a separate append-only reservation. A
        # crash after the owned record was written but before the launcher
        # appended its exact-instance/attempt events must not strand that
        # reservation forever. This is deliberately settled immediately
        # after the confirmed instance cost and before SSH-key cleanup.
        try:
            _require_time(deadline)
            shadeform.append_cost_event({
                "instance_id": f"attempt-{record.ownership_nonce}",
                "phase_id": phase_id,
                "ownership_nonce": record.ownership_nonce,
                "status": "settled",
                "actual_cost_usd": 0.0,
                "reservation": "pre-create-attempt-reconciled-by-teardown",
            })
            attempt_settled = True
            receipt["attempt_reservation_settled"] = True
            receipt["attempt_reservation_error_type"] = None
        except Exception as exc:
            receipt["attempt_reservation_error_type"] = type(exc).__name__
            receipt["retry_required"] = True
    # Durable owned state and a pre-key receipt must precede key deletion.
    record_persisted = False
    try:
        _require_time(deadline)
        shadeform.write_owned_resource(record)
        _require_time(deadline)
        record_persisted = True
        receipt["record_bookkeeping_error_type"] = None
    except Exception as exc:
        receipt["record_bookkeeping_error_type"] = type(exc).__name__
    cost_settled = "actual_cost_usd" in receipt and record.cost_usd is not None
    bookkeeping_ready = cost_settled and attempt_settled and record_persisted
    receipt["key_cleanup_pending"] = bookkeeping_ready
    receipt["owned_record_persisted"] = record_persisted
    receipt["status"] = "recovery-pending"
    receipt["retry_required"] = True
    pre_key_receipt_persisted = False
    final_receipt_persisted = False
    try:
        # A recovery receipt must exist before the key can be touched. It
        # records the exact deleted instance and whether key cleanup is still
        # pending, allowing a later retry to remain exact if this process dies.
        receipt["deletion_receipt_error_type"] = None
        receipt = _persist_deletion_receipt(phase_id, receipt, deadline=deadline)
        pre_key_receipt_persisted = True
    except Exception as exc:
        receipt["deletion_receipt_error_type"] = type(exc).__name__
        receipt["retry_required"] = True

    key_cleanup_ok = False
    if not bookkeeping_ready:
        receipt["ssh_key_cleanup_error_type"] = (
            "DeferredUntilCostSettlement" if not cost_settled
            else "DeferredUntilAttemptSettlement" if not attempt_settled
            else "DeferredUntilOwnedRecordPersistence"
        )
        receipt["key_cleanup_deferred"] = True
    elif pre_key_receipt_persisted:
        try:
            _require_time(deadline)
            key_result = shadeform.delete_owned_ssh_key_exact(
                api_key,
                phase_id,
                record.ssh_key_id,
                ownership_nonce=record.ownership_nonce,
                expected_name=record.ssh_key_name,
                expected_public_key=record.ssh_public_key,
                expected_fingerprint=record.ssh_public_key_fingerprint,
                record=record,
                deadline=deadline,
            )
            key_cleanup_ok = key_result.get("status") == "confirmed"
            if not key_cleanup_ok:
                raise RuntimeError("SSH key deletion lacks durable confirmation")
            receipt["ssh_key_cleanup_error_type"] = None
            receipt["key_cleanup_deferred"] = False
        except Exception as exc:  # receipt retains the deletion even if bookkeeping fails
            receipt["ssh_key_cleanup_error_type"] = type(exc).__name__
            key_cleanup_ok = False
    if not key_cleanup_ok:
        record.status = "deleted-key-cleanup-failed"
        receipt["status"] = "deleted-key-cleanup-failed"
        receipt["retry_required"] = True
        try:
            shadeform.write_owned_resource(record)
            record_persisted = True
        except Exception as exc:
            receipt["record_bookkeeping_error_type"] = type(exc).__name__
    # Persist the post-key result as a second durable update. The pre-key
    # receipt above is the invariant boundary; if this refinement fails,
    # retain ownership state and require manual/retry reconciliation.
    try:
        if key_cleanup_ok:
            receipt["key_cleanup_completed"] = True
            receipt["key_cleanup_pending"] = False
        if key_cleanup_ok and cost_settled and attempt_settled and record_persisted:
            receipt["clear_bookkeeping_error_type"] = None
            receipt["deletion_receipt_error_type"] = None
            receipt["status"] = "complete"
            receipt["retry_required"] = False
        receipt = _persist_deletion_receipt(phase_id, receipt, deadline=deadline)
        final_receipt_persisted = True
    except Exception as exc:
        receipt["deletion_receipt_error_type"] = type(exc).__name__
        receipt["retry_required"] = True
    if key_cleanup_ok and final_receipt_persisted and cost_settled and attempt_settled and record_persisted:
        try:
            shadeform.clear_owned_resource(phase_id, exact)
            shadeform.clear_recovery_owned_resource(phase_id, exact)
            receipt["clear_bookkeeping_error_type"] = None
        except Exception as exc:
            receipt["clear_bookkeeping_error_type"] = type(exc).__name__
            receipt["status"] = "recovery-pending"
            receipt["retry_required"] = True
            try:
                receipt = _persist_deletion_receipt(phase_id, receipt, deadline=deadline)
            except Exception:
                pass
    elif final_receipt_persisted and (not cost_settled or not attempt_settled or not record_persisted):
        receipt["status"] = "deleted-cost-bookkeeping-failed"
        receipt["retry_required"] = True
        try:
            receipt = _persist_deletion_receipt(phase_id, receipt, deadline=deadline)
        except Exception:
            pass
    if not final_receipt_persisted:
        raise RuntimeError("deletion confirmed but deletion receipt could not be persisted")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--salvage", type=Path)
    parser.add_argument("--deadline-epoch", type=float)
    args = parser.parse_args(argv)
    deadline = None
    if args.deadline_epoch is not None:
        if not (math.isfinite(args.deadline_epoch) and args.deadline_epoch > 0):
            parser.error("--deadline-epoch must be a finite positive epoch")
        deadline = time.monotonic() + max(0.0, args.deadline_epoch - time.time())
    print(json.dumps(teardown_exact(args.phase_id, args.instance_id, env_file=args.env_file, salvage=args.salvage, deadline=deadline), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
