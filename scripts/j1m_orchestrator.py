#!/usr/bin/env python3
"""Audited future J1M lifecycle; dry-run unless explicitly unlocked.

The mutating path is present for Sol's later review, but this turn invokes only
the default dry-run. It owns one nonce-bound instance, never enumerates the
account, and tears down the exact resource in ``finally`` after salvage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import j1m_runner, shadeform_lifecycle as sf
from scripts.shadeform_teardown import teardown_exact

ROOT = Path(__file__).resolve().parents[1]


class OperatorCancelled(Exception):
    pass


def _remote(command: list[str], *, timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "transport_timeout", "exit_code": None}
    return {"status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode}


def _salvage(
    info: dict[str, Any],
    identity: Path,
    known_hosts: Path,
    destination: Path,
    names: list[str],
    *,
    deadline: float | None = None,
    q4_expected_gib: float = 6.0,
) -> list[dict[str, Any]]:
    """Attempt each allowlisted receipt independently; one missing file cannot stop cleanup."""

    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        try:
            sf._preflight(info["phase_id"])
            command = sf.scp_base(info["instance_info"], identity, known_hosts) + [
                f"{info['instance_info']['ssh_user']}@{info['instance_info']['ip']}:/scratch/j1m/artifacts/{name}", str(destination / name),
            ]
            # Receipts are small, but the sole deployable Q4 artifact is not.
            # Give its transfer a size-aware floor while still honoring the
            # provider deadline and retaining a cleanup reserve.
            timeout = max(120.0, float(q4_expected_gib) * 60.0) if name.endswith("Q4_K_M.gguf") else 120.0
            if deadline is not None:
                timeout = min(timeout, max(30.0, deadline - time.monotonic() - 30.0))
            receipt = _remote(command, timeout=timeout)
        except Exception as exc:
            receipt = {"status": "salvage_failed", "error_type": type(exc).__name__}
        receipt["name"] = name
        results.append(receipt)
    return results


def execute(env_file: Path, *, config_path: Path, phase_id: str, run_id: str, artifact_destination: Path, mode: str = "prove") -> dict[str, Any]:
    config = j1m_runner.load_config(config_path)
    env = sf.load_env(env_file)
    api_key = sf.require_env(env, "SHADEFORM_API_KEY")
    runtime = float(config["modes"][mode]["runtime_hours"])
    # Candidate selection is policy- and budget-bound; identity is checked
    # again before create so a catalogue reorder cannot change the target.
    candidates = sf.list_candidates(api_key, env, phase_id=phase_id, min_vram_gb=80, max_runtime_hours=runtime)
    target = config["shadeform_target"]
    candidate = next((item for item in candidates if item.cloud.lower() == target["cloud"] and item.region.lower() == target["region"].lower() and item.gpu == target["gpu"] and item.hourly_usd == target["hourly_usd"]), None)
    if candidate is None:
        raise sf.ShadeformError("approved J1M target is not an eligible current catalogue candidate")
    nonce = sf.new_ownership_nonce()
    with tempfile.TemporaryDirectory(prefix=f"j1m-{phase_id}-") as temp:
        temp_root = Path(temp)
        progress_path = ROOT / config["resources"]["progress_path"]
        j1m_runner.write_progress(progress_path, "provider-create-starting", phase_id=phase_id)
        identity, public_key = sf.create_ephemeral_ssh_key(env, temp_root / "ssh")
        key_id: str | None = None
        instance_id: str | None = None
        ambiguous_create = False
        attempt_reserved = False
        settle_attempt_after_cleanup = False
        recorded = False
        record: sf.OwnedResource | None = None
        known_hosts = temp_root / "known_hosts"
        lifecycle: dict[str, Any] = {"phase_id": phase_id, "status": "starting", "mode": mode}
        def cancel(_signum: int, _frame: Any) -> None:
            raise OperatorCancelled("operator cancellation signal")
        previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        signal.signal(signal.SIGINT, cancel)
        signal.signal(signal.SIGTERM, cancel)
        watchdog: subprocess.Popen[bytes] | None = None
        # Reserve the possible provider POST before uploading the key or
        # creating an instance. A failed append is a hard stop: no mutation is
        # allowed without a durable budget/ownership reservation.
        attempt_id = sf.reserve_create_attempt(phase_id, nonce, candidate, backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]), public_key_sha256=hashlib.sha256(public_key.encode("utf-8")).hexdigest())
        attempt_reserved = True
        try:
            key_id = sf.add_ssh_key(api_key, phase_id, f"j1m-{nonce}", public_key)
            sf.verify_ssh_key_ownership(api_key, phase_id, key_id, expected_name=f"j1m-{nonce}", expected_public_key=public_key)
            try:
                instance_id = sf.create_instance(api_key, env, phase_id=phase_id, run_id=run_id, candidate=candidate, ssh_key_id=key_id, nonce=nonce, max_runtime_hours=runtime)
                created_monotonic = time.monotonic()
                provider_deadline = created_monotonic + float(config["modes"][mode]["provider_backstop_hours"]) * 3600
            except Exception as exc:
                incident = {
                    "phase_id": phase_id,
                    "incident": "create-response-ambiguous" if sf.is_ambiguous_transport(exc) else "create-definitive-failure",
                    "nonce": nonce,
                    "ssh_key_id": key_id,
                    "ssh_key_name": f"j1m-{nonce}",
                    "ssh_public_key_sha256": hashlib.sha256(public_key.encode("utf-8")).hexdigest(),
                    "error_type": type(exc).__name__,
                }
                if sf.is_ambiguous_transport(exc):
                    ambiguous_create = True
                else:
                    # Definitive rejection/local failure is reconciled only
                    # after the exact key cleanup in the outer finally.
                    settle_attempt_after_cleanup = True
                try:
                    sf.append_incident(incident)
                except Exception:
                    # The pre-create reservation, not this optional incident,
                    # is what blocks an unsafe subsequent launch.
                    pass
                raise
            lifecycle["instance_id"] = instance_id
            launcher_pid = os.getpid()
            launcher_start_marker = sf.process_start_marker(launcher_pid)
            record = sf.OwnedResource(phase_id=phase_id, run_id=run_id, instance_id=instance_id, ownership_nonce=nonce, ssh_key_id=key_id, ssh_key_name=f"j1m-{nonce}", gpu=candidate.gpu, cloud=candidate.cloud, region=candidate.region, hourly_usd=candidate.hourly_usd, created_at_utc=sf.utc_now().isoformat(), active_deadline_utc=(sf.utc_now() + sf.timedelta(minutes=30)).isoformat(), run_deadline_utc=(sf.utc_now() + sf.timedelta(hours=runtime)).isoformat(), instance_type=candidate.instance_type, launcher_pid=launcher_pid, launcher_start_marker=launcher_start_marker)
            # Ownership record is written before any poll/upload. If this
            # fails, the fallback below still deletes the exact returned ID.
            sf.write_owned_resource(record)
            recorded = True
            # Start the external watchdog immediately after ownership and
            # before any fallible cost-ledger append. It protects the long
            # pending_provider interval as well as later SSH/build stages.
            watchdog_command = [
                os.sys.executable, str(ROOT / "scripts" / "shadeform_watchdog.py"),
                "--phase-id", phase_id, "--instance-id", instance_id,
                "--launcher-pid", str(launcher_pid), "--max-seconds", str(config["modes"][mode]["external_watchdog_seconds"]),
                "--env-file", str(env_file),
            ]
            if launcher_start_marker is not None:
                watchdog_command.extend(["--launcher-start-marker", launcher_start_marker])
            watchdog = subprocess.Popen(watchdog_command)
            lifecycle["watchdog_pid"] = watchdog.pid
            sf.append_cost_event({"instance_id": instance_id, "phase_id": phase_id, "status": "pending", "estimated_cost_usd": round(candidate.hourly_usd * float(config["modes"][mode]["provider_backstop_hours"]), 6)})
            # The instance reservation is now superseded by its exact
            # ownership/billing row. Keep the pre-create reservation history
            # but settle it to zero only after both durable writes and the
            # watchdog are in place.
            sf.append_cost_event({"instance_id": attempt_id, "phase_id": phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
            j1m_runner.write_progress(progress_path, "wait-active-starting", phase_id=phase_id)
            wait_budget = max(30, int(min(1800, provider_deadline - time.monotonic() - 120)))
            info = sf.wait_active(api_key, phase_id, instance_id, timeout_seconds=wait_budget)
            lifecycle["instance_info"] = info
            sf.verify_instance_ownership(info, instance_id=instance_id, phase_id=phase_id, nonce=nonce, expected_name=sf.owned_instance_name(run_id, nonce), ssh_key_id=key_id, expected_cloud=candidate.cloud, expected_region=candidate.region, expected_instance_type=candidate.instance_type, expected_hourly_usd=candidate.hourly_usd, expected_gpu=candidate.gpu, expected_gpu_count=1, expected_vram_gb=candidate.vram_gb, expected_os_image=candidate.os_image)
            lifecycle["host_key"] = sf.acquire_pinned_host_key(info, known_hosts)
            lifecycle["status"] = "active"
            remote_root = "/scratch/j1m"
            lifecycle["remote_workspace"] = _remote(sf.ssh_base(info, identity, known_hosts) + ["mkdir", "-p", remote_root], timeout=30)
            if lifecycle["remote_workspace"]["status"] != "completed":
                raise sf.ShadeformError("remote workspace setup failed")
            shutdown_minutes = str(config["modes"][mode]["host_shutdown_delay_minutes"])
            lifecycle["host_shutdown_backstop"] = _remote(sf.ssh_base(info, identity, known_hosts) + ["sudo", "shutdown", "-h", f"+{shutdown_minutes}"], timeout=30)
            if lifecycle["host_shutdown_backstop"]["status"] != "completed":
                raise sf.ShadeformError("host shutdown backstop could not be armed")
            upload = sf.scp_base(info, identity, known_hosts) + [str(config_path), f"{info['ssh_user']}@{info['ip']}:{remote_root}/j1m-config.json"]
            lifecycle["upload"] = _remote(upload, timeout=120)
            source_lock = ROOT / config["source"]["lock"]
            for local, remote in ((ROOT / "scripts" / "j1m_runner.py", f"{remote_root}/j1m_runner.py"), (source_lock, f"{remote_root}/qwen35-9b.source-lock.json")):
                upload_receipt = _remote(sf.scp_base(info, identity, known_hosts) + [str(local), f"{info['ssh_user']}@{info['ip']}:{remote}"], timeout=120)
                lifecycle.setdefault("uploads", []).append(upload_receipt)
                if upload_receipt["status"] != "completed":
                    raise sf.ShadeformError("required J1M upload failed")
            if mode == "prove":
                lifecycle["job"] = _remote(sf.ssh_base(info, identity, known_hosts) + ["python3", f"{remote_root}/j1m_runner.py", "--prove", "--scratch", "/scratch", "--min-scratch-gib", str(config["resources"]["required_scratch_gib"]), "--output", f"{remote_root}/artifacts/proving-receipt.json"], timeout=120)
            else:
                # Qwen3.5-9B is public at the pinned revision. Do not place
                # HF_TOKEN on the ephemeral host; the runner downloads it
                # unauthenticated and child environments remain sanitized.
                remote_job = sf.ssh_base(info, identity, known_hosts) + ["python3", f"{remote_root}/j1m_runner.py", "--run", "--config", f"{remote_root}/j1m-config.json"]
                j1m_runner.write_progress(progress_path, "remote-build-starting", phase_id=phase_id)
                transfer_reserve = float(config["modes"][mode].get("transfer_reserve_seconds", 0))
                lifecycle["job"] = _remote(remote_job, timeout=max(30, provider_deadline - time.monotonic() - transfer_reserve - 120))
            if lifecycle["job"]["status"] != "completed":
                lifecycle["status"] = lifecycle["job"]["status"]
                raise sf.ShadeformError("J1M remote job did not complete")
            lifecycle["status"] = "completed"
        except (KeyboardInterrupt, OperatorCancelled):
            lifecycle["status"] = "cancelled_by_operator"
            if instance_id is None and not ambiguous_create:
                settle_attempt_after_cleanup = True
            raise
        except Exception:
            # Key upload/ownership validation and other definitive local
            # failures occur outside the create-specific handler. They still
            # reconcile the pre-create reservation after finally cleanup when
            # no instance POST could have succeeded.
            if instance_id is None and not ambiguous_create:
                settle_attempt_after_cleanup = True
            raise
        finally:
            if watchdog is not None and watchdog.poll() is None:
                try:
                    watchdog.terminate()
                except OSError:
                    pass
                try:
                    watchdog.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        watchdog.kill()
                    except OSError:
                        pass
                    try:
                        watchdog.wait(timeout=10)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            fetch_allowlist = config["artifacts"]["local_fetch_allowlist"] if mode == "build" else config["artifacts"]["prove_fetch_allowlist"]
            try:
                lifecycle["salvage"] = _salvage(
                    {"phase_id": phase_id, "instance_info": lifecycle.get("instance_info", {})},
                    identity,
                    known_hosts,
                    artifact_destination,
                    fetch_allowlist,
                    deadline=provider_deadline if "provider_deadline" in locals() else None,
                    q4_expected_gib=float(config["resources"].get("expected_q4_gib", 6.0)),
                ) if lifecycle.get("instance_info") else []
            except Exception as exc:
                # Even an unexpected salvage/setup failure must leave the
                # exact deletion and key cleanup paths reachable.
                lifecycle["salvage"] = [{"status": "salvage_failed", "error_type": type(exc).__name__}]
            if mode == "prove" and lifecycle.get("job", {}).get("status") == "completed" and not any(item.get("name") == "proving-receipt.json" and item.get("status") == "completed" for item in lifecycle["salvage"]):
                lifecycle["receipt_error"] = "proving receipt was not salvaged before teardown"
            # The shared teardown performs exact deletion before cost/key
            # bookkeeping and emits a receipt, while remote salvage above is
            # best-effort and independent for each allowlisted artifact.
            if instance_id is not None:
                if recorded:
                    lifecycle["deletion"] = teardown_exact(phase_id, instance_id, env_file=env_file)
                else:
                    # Ledger write failed: exact ID is still known, so delete
                    # it before attempting any key/bookkeeping cleanup.
                    try:
                        lifecycle["deletion"] = sf._delete_instance(api_key, phase_id, instance_id)
                    except Exception as exc:
                        lifecycle["deletion"] = {"success": False, "error_type": type(exc).__name__}
                        try:
                            sf.append_incident({"phase_id": phase_id, "incident": "post-instance-delete-failed", "instance_id": instance_id, "ssh_key_id": key_id, "nonce": nonce, "error_type": type(exc).__name__})
                        except Exception:
                            pass
                    finally:
                        if key_id is not None:
                            try:
                                lifecycle["key_cleanup"] = sf.delete_ssh_key(api_key, phase_id, key_id)
                            except Exception as exc:
                                lifecycle["key_cleanup"] = {"status": "failed", "error_type": type(exc).__name__}
                                try:
                                    sf.append_incident({"phase_id": phase_id, "incident": "post-instance-key-delete-failed", "instance_id": instance_id, "ssh_key_id": key_id, "nonce": nonce, "error_type": type(exc).__name__})
                                except Exception:
                                    pass
            elif key_id is not None and not ambiguous_create:
                # Key creation succeeded but instance creation did not.
                try:
                    lifecycle["key_cleanup"] = sf.delete_ssh_key(api_key, phase_id, key_id)
                except Exception as exc:
                    lifecycle["key_cleanup"] = {"status": "failed", "error_type": type(exc).__name__}
                    try:
                        sf.append_incident({"phase_id": phase_id, "incident": "create-key-delete-failed", "ssh_key_id": key_id, "nonce": nonce, "error_type": type(exc).__name__})
                    except Exception:
                        pass
            if attempt_reserved and settle_attempt_after_cleanup:
                try:
                    sf.append_cost_event({"instance_id": attempt_id, "phase_id": phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
                except Exception as exc:
                    try:
                        sf.append_incident({"phase_id": phase_id, "incident": "attempt-reservation-settlement-failed", "nonce": nonce, "error_type": type(exc).__name__})
                    except Exception:
                        pass
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
        return lifecycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, default=j1m_runner.DEFAULT_CONFIG)
    parser.add_argument("--phase-id", default="j1m-proving-run")
    parser.add_argument("--run-id", default="J1M")
    parser.add_argument("--artifact-destination", type=Path, default=ROOT / "artifacts" / "qwen35-9b")
    parser.add_argument("--mode", choices=("prove", "build"), default="prove")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    config = j1m_runner.load_config(args.config)
    plan = j1m_runner.build_plan(config, args.mode)
    plan["mode"] = args.mode
    plan["mode_runtime_hours"] = config["modes"][args.mode]["runtime_hours"]
    plan["mode_active_cost_usd"] = config["modes"][args.mode]["active_cost_usd"]
    if not args.execute:
        plan["orchestrator"] = "dry-run; no provider API mutation"
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if os.environ.get("SOL_J1M_REVIEWED") != "1":
        raise SystemExit("refusing mutation: Sol must set SOL_J1M_REVIEWED=1 after reviewing the plan")
    print(json.dumps(execute(args.env_file, config_path=args.config, phase_id=args.phase_id, run_id=args.run_id, artifact_destination=args.artifact_destination, mode=args.mode), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
