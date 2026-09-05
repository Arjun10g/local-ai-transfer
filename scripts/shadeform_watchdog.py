#!/usr/bin/env python3
"""External wall-clock watchdog for one phase-owned resource."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEARDOWN_RESERVE_SECONDS = 660.0
INSTANCE_RECONCILIATION_WINDOW_SECONDS = 120.0
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def identity_alive(pid: int, marker: str | None) -> bool:
    # The watchdog is a direct child of the launcher. Once reparented after a
    # launcher death, a reused PID must never be treated as the owner.
    if os.getppid() != pid:
        return False
    if not alive(pid):
        return False
    if marker is None:
        return True
    from scripts import shadeform_lifecycle as shadeform
    return shadeform.process_start_marker(pid) == marker


def _pending_intent(shadeform, phase_id: str, nonce: str | None) -> dict[str, object] | None:
    """Return the latest bounded pending reservation/create event."""
    if nonce is None or not shadeform.NONCE.fullmatch(nonce):
        return None
    latest: dict[str, dict[str, object]] = {}
    try:
        if shadeform.COST_LEDGER.stat().st_size > 1_048_576:
            return None
        with shadeform.COST_LEDGER.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= 4096:
                    return None
                event = json.loads(line)
                if not isinstance(event, dict):
                    return None
                if (event.get("phase_id") == phase_id
                        and event.get("ownership_nonce") == nonce
                        and isinstance(event.get("instance_id"), str)):
                    latest[event["instance_id"]] = event
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    pending = [event for event in latest.values() if event.get("status") == "pending"]
    if not pending:
        return None
    return next((event for event in pending if event.get("instance_create_intent") is True), pending[-1])


def _has_pending_intent(shadeform, phase_id: str, nonce: str | None) -> bool:
    return _pending_intent(shadeform, phase_id, nonce) is not None


def _deadline_windows(*, now_monotonic: float, now_epoch: float, max_seconds: float, deadline_epoch: float | None) -> tuple[float, float]:
    """Return separate wake/work and hard teardown deadlines."""

    work_deadline = now_monotonic + max_seconds
    hard_deadline = now_monotonic + max_seconds + TEARDOWN_RESERVE_SECONDS if deadline_epoch is None else now_monotonic + max(0.0, deadline_epoch - now_epoch)
    if hard_deadline <= work_deadline:
        raise ValueError("hard provider deadline does not preserve watchdog teardown reserve")
    return work_deadline, hard_deadline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-id", required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--launcher-pid", required=True, type=int)
    parser.add_argument("--max-seconds", required=True, type=float)
    parser.add_argument("--deadline-epoch", type=float)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--launcher-start-marker")
    parser.add_argument("--ownership-nonce")
    parser.add_argument("--ssh-key-id")
    parser.add_argument("--ssh-key-name")
    parser.add_argument("--ssh-key-fingerprint")
    parser.add_argument("--allow-unrecorded-exact", action="store_true")
    parser.add_argument("--key-only-recovery", action="store_true")
    parser.add_argument("--instance-name")
    parser.add_argument("--precreate-recovery", action="store_true")
    parser.add_argument("--cloud")
    parser.add_argument("--region")
    parser.add_argument("--instance-type")
    parser.add_argument("--hourly-usd", type=float)
    parser.add_argument("--gpu")
    parser.add_argument("--gpu-count", type=int)
    parser.add_argument("--vram-gb", type=int)
    parser.add_argument("--os-image")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    from scripts import shadeform_lifecycle as shadeform
    try:
        shadeform.validate_phase_id(args.phase_id)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    if not args.instance_id and not args.instance_name:
        parser.error("one of --instance-id or --instance-name is required")
    if args.precreate_recovery and not args.instance_name:
        parser.error("precreate recovery requires --instance-name")
    if not (math.isfinite(args.max_seconds) and args.max_seconds > 0) or not (math.isfinite(args.poll_seconds) and 0 < args.poll_seconds <= 60):
        parser.error("watchdog durations are outside their bounded range")
    if args.deadline_epoch is not None:
        if not (math.isfinite(args.deadline_epoch) and args.deadline_epoch > 0):
            parser.error("--deadline-epoch must be a finite positive epoch")
        pass
    else:
        args.deadline_epoch = None
    if args.allow_unrecorded_exact:
        # An unrecorded path is permitted only for nonce reconciliation.  A
        # caller-supplied instance ID would bypass the provider info/profile
        # proof and turn this watchdog into an account-scoped delete primitive.
        if args.instance_id is not None:
            parser.error("--allow-unrecorded-exact requires nonce reconciliation; direct instance IDs are forbidden")
        required_profile = (args.cloud, args.region,
                            args.instance_type, args.hourly_usd, args.gpu,
                            args.gpu_count, args.vram_gb, args.os_image)
        if any(value is None for value in required_profile):
            parser.error("unrecorded recovery requires the complete approved instance profile")
        if (not math.isfinite(args.hourly_usd) or args.hourly_usd <= 0 or
                args.gpu_count <= 0 or args.vram_gb <= 0 or
                any(not isinstance(value, str) or not value or len(value) > 256
                    for value in (args.cloud, args.region, args.instance_type, args.gpu, args.os_image))):
            parser.error("unrecorded recovery profile is malformed")
        if args.key_only_recovery:
            if args.instance_id is not None or not args.ssh_key_name or not args.ssh_key_fingerprint:
                parser.error("key-only recovery requires no instance ID and exact key identity")
        elif args.ssh_key_id is None:
            parser.error("instance recovery requires an SSH key ID")
    # max-seconds is the work/wake deadline.  The absolute epoch is the hard
    # provider teardown deadline and is intentionally kept separate so the
    # reserve remains available after the launcher disappears.
    try:
        work_deadline, hard_deadline = _deadline_windows(
            now_monotonic=time.monotonic(), now_epoch=time.time(),
            max_seconds=args.max_seconds, deadline_epoch=args.deadline_epoch,
        )
    except ValueError as exc:
        parser.error(str(exc))
    while identity_alive(args.launcher_pid, args.launcher_start_marker) and time.monotonic() < work_deadline:
        time.sleep(args.poll_seconds)
    record_path = ROOT / "experiments" / "runtime" / f"{args.phase_id}.json"
    launcher_owned = identity_alive(args.launcher_pid, args.launcher_start_marker)
    if not launcher_owned and not record_path.exists():
        if not args.allow_unrecorded_exact:
            return 0
        intent = _pending_intent(shadeform, args.phase_id, args.ownership_nonce)
        if intent is None:
            return 1
        try:
            env = shadeform.load_env(args.env_file)
            api_key = shadeform.require_env(env, "SHADEFORM_API_KEY")
            reconciled_key_id = shadeform.reconcile_ssh_key(
                api_key, args.phase_id, expected_name=args.ssh_key_name or "",
                expected_fingerprint=args.ssh_key_fingerprint,
            ) if args.key_only_recovery else args.ssh_key_id
            if reconciled_key_id is None:
                return 1
            # A key-only watcher also checks for an instance that may have
            # been created after the key POST. Zero is the safe key-only
            # outcome; an exact nonce/profile match is cleaned in full.
            exact_instance_id = args.instance_id
            if exact_instance_id is None:
                consistency_deadline = min(
                    time.monotonic() + INSTANCE_RECONCILIATION_WINDOW_SECONDS,
                    hard_deadline - TEARDOWN_RESERVE_SECONDS,
                )
                requires_eventual_reconciliation = intent.get("instance_create_intent") is True or intent.get("instance_id") != "attempt-" + (args.ownership_nonce or "")
                while True:
                    exact_instance_id = shadeform.reconcile_instance_by_nonce(
                        api_key, args.phase_id, expected_name=args.instance_name,
                        nonce=args.ownership_nonce,
                        ssh_key_id=reconciled_key_id, expected_cloud=args.cloud,
                        expected_region=args.region, expected_instance_type=args.instance_type,
                        expected_hourly_usd=args.hourly_usd, expected_gpu=args.gpu,
                        expected_gpu_count=args.gpu_count, expected_vram_gb=args.vram_gb,
                        expected_os_image=args.os_image, allow_absent=True,
                    )
                    if exact_instance_id is not None or not requires_eventual_reconciliation:
                        break
                    if time.monotonic() >= consistency_deadline:
                        # One absent account response after an instance POST
                        # is not negative proof. Keep the pending intent/key
                        # recovery alive for a later exact retry.
                        return 1
                    time.sleep(min(5.0, consistency_deadline - time.monotonic()))
            if exact_instance_id is None:
                remaining = hard_deadline - time.monotonic()
                if remaining <= 0:
                    return 1
                shadeform.verify_ssh_key_fingerprint(
                    api_key, args.phase_id, reconciled_key_id,
                    expected_name=args.ssh_key_name or "",
                    expected_fingerprint=args.ssh_key_fingerprint or "",
                    timeout=min(90.0, remaining),
                )
                shadeform.delete_ssh_key(api_key, args.phase_id, reconciled_key_id, deadline=hard_deadline)
                shadeform.append_cost_event({
                    "instance_id": "attempt-" + (args.ownership_nonce or ""),
                    "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": 0.0,
                    "reservation": "pre-create-key-reconciled",
                })
                return 0
            remaining = hard_deadline - time.monotonic()
            if remaining <= 0:
                return 1
            info = shadeform.instance_info(api_key, args.phase_id, exact_instance_id, timeout=min(90.0, remaining))
            shadeform.verify_instance_ownership(
                info, instance_id=exact_instance_id, phase_id=args.phase_id,
                nonce=args.ownership_nonce or "", expected_name=args.instance_name,
                ssh_key_id=reconciled_key_id, expected_cloud=args.cloud,
                expected_region=args.region, expected_instance_type=args.instance_type,
                expected_hourly_usd=args.hourly_usd, expected_gpu=args.gpu,
                expected_gpu_count=args.gpu_count, expected_vram_gb=args.vram_gb,
                expected_os_image=args.os_image,
            )
            started_at = intent.get("create_started_at_utc")
            try:
                started_dt = datetime.fromisoformat(started_at) if isinstance(started_at, str) else None
                if started_dt is None or started_dt.tzinfo is None:
                    raise ValueError("missing create intent timestamp")
                elapsed_hours = max(0.0, (datetime.now(timezone.utc) - started_dt).total_seconds() / 3600.0)
                hourly_usd = float(intent.get("hourly_usd", args.hourly_usd))
                backstop_hours = float(intent.get("backstop_hours", 0.0))
                if not math.isfinite(hourly_usd) or hourly_usd <= 0 or not math.isfinite(backstop_hours) or backstop_hours <= 0:
                    raise ValueError("invalid create intent cost binding")
                shadeform.append_cost_event({
                    "instance_id": exact_instance_id, "phase_id": args.phase_id,
                    "ownership_nonce": args.ownership_nonce, "status": "pending",
                    "estimated_cost_usd": round(hourly_usd * max(backstop_hours, elapsed_hours), 6),
                    "reservation": "watchdog-reconciled-instance",
                })
            except (TypeError, ValueError, OverflowError):
                return 1
            deleted = shadeform._delete_instance(api_key, args.phase_id, exact_instance_id, deadline=hard_deadline)
            if deleted.get("success") is not True:
                return 1
            if reconciled_key_id:
                shadeform.verify_ssh_key_fingerprint(
                    api_key, args.phase_id, reconciled_key_id,
                    expected_name=args.ssh_key_name or "",
                    expected_fingerprint=args.ssh_key_fingerprint or "",
                    timeout=min(90.0, hard_deadline - time.monotonic()),
                )
                shadeform.delete_ssh_key(api_key, args.phase_id, reconciled_key_id, deadline=hard_deadline)
            shadeform.append_cost_event({
                "instance_id": exact_instance_id, "phase_id": args.phase_id,
                "ownership_nonce": args.ownership_nonce, "status": "settled",
                "actual_cost_usd": round(hourly_usd * elapsed_hours, 6),
                "reservation": "watchdog-reconciled-instance",
            })
            shadeform.append_cost_event({
                "instance_id": "attempt-" + (args.ownership_nonce or ""),
                "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": 0.0,
                "reservation": "pre-create-instance-reconciled",
            })
            return 0
        except Exception:
            return 1
    result_code = 1
    try:
        record = shadeform.read_owned_resource(args.phase_id)
        if record is None:
            return 1
        if args.instance_id is not None and args.instance_id != record.instance_id:
            return 1
        exact_instance_id = record.instance_id
        teardown_argv = [
            sys.executable, str(ROOT / "scripts" / "shadeform_teardown.py"),
            "--phase-id", args.phase_id, "--instance-id", exact_instance_id,
            "--env-file", str(args.env_file),
        ]
        if args.deadline_epoch is not None:
            teardown_argv.extend(["--deadline-epoch", str(args.deadline_epoch)])
        remaining = hard_deadline - time.monotonic()
        if remaining <= 0:
            return 1
        result = subprocess.run(teardown_argv, timeout=remaining)
        result_code = result.returncode
    finally:
        if identity_alive(args.launcher_pid, args.launcher_start_marker):
            try:
                os.kill(args.launcher_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
