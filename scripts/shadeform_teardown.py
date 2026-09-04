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


def salvage_local(source: Path | None, destination: Path) -> dict[str, object]:
    if source is None or not source.exists():
        return {"status": "nothing_available"}
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    try:
        target.write_bytes(source.read_bytes())
    except OSError as exc:
        return {"status": "salvage_failed", "error_type": type(exc).__name__}
    return {"status": "salvaged", "name": target.name, "size_bytes": target.stat().st_size}


def teardown_exact(phase_id: str, instance_id: str, *, env_file: Path = ROOT / ".env", salvage: Path | None = None, salvage_destination: Path = ROOT / "experiments" / "results") -> dict[str, object]:
    phase_id = shadeform.validate_phase_id(phase_id)
    exact = shadeform.validate_resource_id(instance_id, field="requested instance id")
    record = shadeform.read_owned_resource(phase_id)
    if record is None or record.instance_id != exact:
        raise RuntimeError("refusing teardown: requested ID is not the exact phase-owned resource")
    salvage_receipt = salvage_local(salvage, salvage_destination)
    env = shadeform.load_env(env_file)
    api_key = shadeform.require_env(env, "SHADEFORM_API_KEY")
    # Once ownership is validated, exact deletion is attempted first. A
    # bookkeeping write must never become a precondition for cleanup.
    deletion = shadeform._delete_instance(api_key, phase_id, exact)
    if deletion.get("success") is not True:
        record.status = "delete-failed"
        shadeform.write_owned_resource(record)
        raise RuntimeError("provider did not confirm exact-resource deletion")
    receipt = {"schema": "local_bmo.shadeform.deletion-receipt.v1", "phase_id": phase_id, "instance_id": exact, "deletion": deletion, "salvage": salvage_receipt}
    record.status = "deleted"
    elapsed_hours = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(record.created_at_utc)).total_seconds() / 3600.0)
    settled_cost = round(record.hourly_usd * elapsed_hours, 6)
    try:
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
    try:
        shadeform.delete_ssh_key(api_key, phase_id, record.ssh_key_id)
    except Exception as exc:  # receipt retains the deletion even if bookkeeping fails
        receipt["ssh_key_cleanup_error_type"] = type(exc).__name__
    try:
        shadeform.clear_owned_resource(phase_id, exact)
    except Exception as exc:
        receipt["clear_bookkeeping_error_type"] = type(exc).__name__
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
