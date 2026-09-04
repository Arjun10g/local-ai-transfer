#!/usr/bin/env python3
"""External wall-clock watchdog for one phase-owned resource."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _remove_remote_token(phase_id: str, instance_id: str, env_file: Path, identity: Path | None, known_hosts: Path | None) -> dict[str, object]:
    """Best-effort token removal before delegating exact teardown."""

    if identity is None or known_hosts is None:
        return {"status": "not_attempted", "reason": "missing SSH custody paths"}
    try:
        from scripts import shadeform_lifecycle as shadeform
        record = shadeform.read_owned_resource(phase_id)
        if record is None or record.instance_id != shadeform.validate_resource_id(instance_id):
            return {"status": "not_attempted", "reason": "ownership ledger mismatch"}
        env = shadeform.load_env(env_file)
        info = shadeform.instance_info(shadeform.require_env(env, "SHADEFORM_API_KEY"), phase_id, instance_id)
        shadeform.verify_instance_ownership(info, instance_id=instance_id, phase_id=phase_id, nonce=record.ownership_nonce, expected_name=shadeform.owned_instance_name(record.run_id, record.ownership_nonce), ssh_key_id=record.ssh_key_id, expected_cloud=record.cloud, expected_region=record.region, expected_instance_type=record.instance_type, expected_hourly_usd=record.hourly_usd)
        result = subprocess.run(
            shadeform.ssh_base(info, identity, known_hosts) + ["rm", "-f", "/scratch/j1m/hf-token.env"],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
        return {"status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--launcher-pid", required=True, type=int)
    parser.add_argument("--max-seconds", required=True, type=float)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--launcher-start-marker")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.max_seconds
    while identity_alive(args.launcher_pid, args.launcher_start_marker) and time.monotonic() < deadline:
        time.sleep(args.poll_seconds)
    record_path = ROOT / "experiments" / "runtime" / f"{args.phase_id}.json"
    if not alive(args.launcher_pid) and not record_path.exists():
        return 0
    # Remove the token while the exact ledger, endpoint, private key, and
    # pinned host-key file are still available. Teardown remains the final
    # provider backstop even when this SSH attempt fails.
    _remove_remote_token(args.phase_id, args.instance_id, args.env_file, args.identity, args.known_hosts)
    result_code = 1
    try:
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "shadeform_teardown.py"),
            "--phase-id", args.phase_id, "--instance-id", args.instance_id,
            "--env-file", str(args.env_file),
        ], timeout=900)
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
