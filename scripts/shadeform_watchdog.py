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


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--launcher-pid", required=True, type=int)
    parser.add_argument("--max-seconds", required=True, type=float)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.max_seconds
    while alive(args.launcher_pid) and time.monotonic() < deadline:
        time.sleep(args.poll_seconds)
    record_path = ROOT / "experiments" / "runtime" / f"{args.phase_id}.json"
    if not alive(args.launcher_pid) and not record_path.exists():
        return 0
    # Exact deletion first: killing the launcher first would remove its only
    # cleanup actor. The teardown process salvages before deleting.
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "shadeform_teardown.py"),
        "--phase-id", args.phase_id, "--instance-id", args.instance_id,
        "--env-file", str(args.env_file),
    ], timeout=900)
    if alive(args.launcher_pid):
        os.kill(args.launcher_pid, signal.SIGTERM)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
