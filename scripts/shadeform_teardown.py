#!/usr/bin/env python3
"""Best-effort salvage followed by exact-resource deletion."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import shadeform_lifecycle as shadeform

ROOT = Path(__file__).resolve().parents[1]


def _write_deletion_receipt(phase_id: str, payload: dict[str, object]) -> None:
    path = shadeform.RUNTIME_ROOT / f"{phase_id}.deletion-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def salvage_local(source: Path | None, destination: Path) -> dict[str, object]:
    if source is None or not source.exists():
        return {"status": "nothing_available"}
    try:
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / source.name
        target.write_bytes(source.read_bytes())
    except OSError as exc:
        return {"status": "salvage_failed", "error_type": type(exc).__name__}
    return {"status": "salvaged", "name": target.name, "size_bytes": target.stat().st_size}


def teardown_exact(phase_id: str, instance_id: str, *, env_file: Path = ROOT / ".env", salvage: Path | None = None, salvage_destination: Path = ROOT / "experiments" / "results", deadline: float | None = None) -> dict[str, object]:
    phase_id = shadeform.validate_phase_id(phase_id)
    exact = shadeform.validate_resource_id(instance_id, field="requested instance id")
    with shadeform.phase_cleanup_lock(phase_id):
        # Re-read the ledger under the per-phase lock; a peer that already
        # confirmed deletion may have cleared it and is an idempotent success.
        current = shadeform.read_owned_resource(phase_id)
        if current is None:
            receipt_path = shadeform.RUNTIME_ROOT / f"{phase_id}.deletion-receipt.json"
            try:
                prior = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior = None
            if isinstance(prior, dict) and prior.get("instance_id") == exact and prior.get("deletion", {}).get("success") is True:
                return {"schema": "local_bmo.shadeform.deletion-receipt.v1", "phase_id": phase_id, "instance_id": exact, "status": "already-cleaned"}
            raise RuntimeError("refusing teardown: no owned ledger or matching confirmed deletion receipt")
        if current.instance_id != exact:
            raise RuntimeError("refusing teardown: requested ID is not the exact phase-owned resource")
        return _teardown_exact_locked(phase_id, exact, env_file=env_file, salvage=salvage, salvage_destination=salvage_destination, deadline=deadline)


def _teardown_exact_locked(phase_id: str, exact: str, *, env_file: Path, salvage: Path | None, salvage_destination: Path, deadline: float | None) -> dict[str, object]:
    record = shadeform.read_owned_resource(phase_id)
    if record is None or record.instance_id != exact:
        raise RuntimeError("refusing teardown: requested ID is not the exact phase-owned resource")
    try:
        salvage_receipt = salvage_local(salvage, salvage_destination)
    except Exception as exc:
        # Salvage setup is never allowed to bypass exact deletion. Keep a
        # bounded receipt and continue through the provider/key cleanup paths.
        salvage_receipt = {"status": "salvage_failed", "error_type": type(exc).__name__}
    env = shadeform.load_env(env_file)
    api_key = shadeform.require_env(env, "SHADEFORM_API_KEY")
    # Once ownership is validated, exact deletion is attempted first. A
    # bookkeeping write must never become a precondition for cleanup.
    try:
        deletion = shadeform._delete_instance(api_key, phase_id, exact, deadline=deadline)
    except Exception as exc:
        deletion = {"success": False, "error_type": type(exc).__name__}
    if deletion.get("success") is not True:
        record.status = "delete-failed"
        try:
            shadeform.write_owned_resource(record)
        except Exception:
            pass
        # Key cleanup is deliberately independent. The ownership ledger is
        # retained because deletion was not confirmed, but the ephemeral key
        # must still be revoked when the provider accepts that exact request.
        try:
            shadeform.delete_ssh_key(api_key, phase_id, record.ssh_key_id)
        except Exception:
            pass
        _write_deletion_receipt(phase_id, {"schema": "local_bmo.shadeform.deletion-receipt.v1", "phase_id": phase_id, "instance_id": exact, "status": "delete-failed", "deletion": deletion, "salvage": salvage_receipt, "retry_required": True})
        raise RuntimeError("provider did not confirm exact-resource deletion")
    receipt = {"schema": "local_bmo.shadeform.deletion-receipt.v1", "phase_id": phase_id, "instance_id": exact, "deletion": deletion, "salvage": salvage_receipt}
    record.status = "deleted"
    try:
        created_at = datetime.fromisoformat(record.created_at_utc)
        elapsed_hours = max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600.0)
        settled_cost = round(record.hourly_usd * elapsed_hours, 6)
        if not shadeform._valid_cost(settled_cost):
            raise ValueError("computed settled cost is not finite and nonnegative")
        shadeform.append_cost_event({"instance_id": exact, "phase_id": phase_id, "status": "settled", "actual_cost_usd": settled_cost})
        receipt["actual_cost_usd"] = settled_cost
        record.cost_usd = settled_cost
    except Exception as exc:
        # Deletion already succeeded. Preserve the failure in the receipt and
        # leave a visible settled-row update for the operator to reconcile.
        receipt["cost_bookkeeping_error_type"] = type(exc).__name__
        record.status = "deleted-cost-bookkeeping-failed"
    # All post-delete bookkeeping is best effort; key cleanup and ledger clear
    # are independently attempted after confirmed instance deletion.
    try:
        shadeform.write_owned_resource(record)
    except Exception as exc:
        receipt["record_bookkeeping_error_type"] = type(exc).__name__
    key_cleanup_ok = True
    try:
        shadeform.delete_ssh_key(api_key, phase_id, record.ssh_key_id)
    except Exception as exc:  # receipt retains the deletion even if bookkeeping fails
        receipt["ssh_key_cleanup_error_type"] = type(exc).__name__
        key_cleanup_ok = False
    if not key_cleanup_ok:
        record.status = "deleted-key-cleanup-failed"
        receipt["status"] = "deleted-key-cleanup-failed"
        receipt["retry_required"] = True
        try:
            shadeform.write_owned_resource(record)
        except Exception as exc:
            receipt["record_bookkeeping_error_type"] = type(exc).__name__
    # Persist only after key cleanup has been attempted, so a receipt/ledger
    # write failure can never bypass exact key revocation. If this fails,
    # retain ownership for a safe retry rather than claiming an already-cleaned
    # phase.
    receipt_persisted = False
    try:
        _write_deletion_receipt(phase_id, receipt)
        receipt_persisted = True
    except Exception as exc:
        receipt["deletion_receipt_error_type"] = type(exc).__name__
        receipt["retry_required"] = True
    if key_cleanup_ok and receipt_persisted:
        try:
            shadeform.clear_owned_resource(phase_id, exact)
        except Exception as exc:
            receipt["clear_bookkeeping_error_type"] = type(exc).__name__
            # The deletion receipt is already durable; leave the ledger for a
            # subsequent exact, idempotent cleanup attempt.
            try:
                _write_deletion_receipt(phase_id, receipt)
            except Exception:
                pass
    if not receipt_persisted:
        raise RuntimeError("deletion confirmed but deletion receipt could not be persisted")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--salvage", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(teardown_exact(args.phase_id, args.instance_id, env_file=args.env_file, salvage=args.salvage), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
