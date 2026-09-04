#!/usr/bin/env python3
"""Run the hostile external-tools QA harness on one owned Shadeform host.

This is deliberately a plan-first wrapper.  ``--execute`` is refused unless
both the Sol review markers and an explicit A100 target tuple are present in
the environment.  The remote command is still only a synthetic loopback QA
run; no Microsoft, browser, Copilot, or model credentials are uploaded.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import shadeform_lifecycle as shadeform


ROOT = Path(__file__).resolve().parents[2]
QA_SCRIPT = ROOT / "scripts" / "test" / "remote_external_tools_qa.mjs"
NODE_VERSION = "24.20.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_URL = f"https://nodejs.org/download/release/v{NODE_VERSION}/{NODE_ARCHIVE}"
NODE_SHA256 = "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2"
QA_MARKER = "REMOTE-EXTERNAL-TOOLS-V1"
MAX_OUTPUT_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 1_048_576
MAX_REMOTE_ROOT = 96
DEFAULT_RUNTIME_HOURS = 0.25
DEFAULT_FUZZ_CASES = 64
DEFAULT_SOAK_ITERATIONS = 25

# This is an audited transitive closure, not a repository upload.  Keep this
# list explicit so a new provider import cannot accidentally broaden the
# remote source boundary.
UPLOAD_FILES = (
    "scripts/test/remote_external_tools_qa.mjs",
    "host/agent/tool-envelope.mjs",
    "host/providers/provider-common.mjs",
    "host/providers/microsoft-graph.mjs",
    "host/providers/microsoft-graph-auth.mjs",
    "host/providers/operator-grants.mjs",
    "host/providers/browser-actions.mjs",
    "host/providers/copilot-cli.mjs",
    "host/providers/copilot-context.mjs",
    "host/tools/local/workspace-policy.mjs",
)


class RunnerError(RuntimeError):
    pass


def bounded_id(value: object, *, field: str, pattern: str = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}") -> str:
    import re

    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {field}")
    return value


def bounded_nonnegative(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be an integer from 0 through {maximum}")
    return value


def closure_manifest(root: Path = ROOT) -> list[dict[str, object]]:
    """Return the exact source closure with bounded metadata and SHA-256."""

    entries: list[dict[str, object]] = []
    for relative in UPLOAD_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise RunnerError(f"closure member is not a regular non-symlink file: {relative}")
        size = path.stat().st_size
        if size > MAX_RECEIPT_BYTES:
            raise RunnerError(f"closure member is too large: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append({"path": relative, "size_bytes": size, "sha256": digest})
    return entries


def build_plan(*, phase_id: str, run_id: str, runtime_hours: float, fuzz_cases: int, soak_iterations: int, env: dict[str, str]) -> dict[str, object]:
    """Build a secret-free plan without reading credentials or calling Shadeform."""

    phase_id = shadeform.validate_phase_id(phase_id)
    run_id = bounded_id(run_id, field="run id")
    if not 0.05 <= runtime_hours <= DEFAULT_RUNTIME_HOURS:
        raise ValueError("runtime must be between 0.05 and 0.25 hours")
    fuzz_cases = bounded_nonnegative(fuzz_cases, field="fuzz cases", maximum=256)
    soak_iterations = bounded_nonnegative(soak_iterations, field="soak iterations", maximum=1000)
    approved = {
        "gpu": env.get("SHADEFORM_QA_APPROVED_GPU", "").strip(),
        "cloud": env.get("SHADEFORM_QA_APPROVED_CLOUD", "").strip(),
        "region": env.get("SHADEFORM_QA_APPROVED_REGION", "").strip(),
        "instance_type": env.get("SHADEFORM_QA_APPROVED_INSTANCE_TYPE", "").strip(),
    }
    closure = closure_manifest()
    remote_root = f"/scratch/lae-remote-tools-{phase_id}-{run_id[:20]}"
    if len(remote_root) > MAX_REMOTE_ROOT:
        raise ValueError("remote root exceeds bounded length")
    return {
        "schema": "local_bmo.shadeform.remote-external-tools-plan.v1",
        "phase_id": phase_id,
        "run_id": run_id,
        "target_policy": {
            "required_gpu_family": "A100",
            "approved_tuple_present": all(approved.values()),
            "approved": {key: value or None for key, value in approved.items()},
        },
        "runtime_hours": runtime_hours,
        "provider_backstop_hours": max(0.25, runtime_hours * 1.25),
        "qa": {"marker": QA_MARKER, "fuzz_cases": fuzz_cases, "soak_iterations": soak_iterations},
        "node": {"version": NODE_VERSION, "archive": NODE_ARCHIVE, "sha256": NODE_SHA256, "url": NODE_URL},
        "remote_root": remote_root,
        "upload_count": len(closure),
        "upload_bytes": sum(int(item["size_bytes"]) for item in closure),
        "closure_sha256": hashlib.sha256(json.dumps(closure, sort_keys=True).encode()).hexdigest(),
        "provider_mutation": "refused_without_SOL_SHADEFORM_REVIEWED_and_SOL_REMOTE_EXTERNAL_TOOLS_REVIEWED",
    }


def _remote_bootstrap_commands(remote_root: str, ssh_user: str) -> list[list[str]]:
    archive = f"{remote_root}/{NODE_ARCHIVE}"
    return [
        ["sudo", "mkdir", "-p", "/scratch"],
        ["sudo", "chown", ssh_user, "/scratch"],
        ["mkdir", "-p", f"{remote_root}/node", f"{remote_root}/qa", f"{remote_root}/artifacts"],
        ["curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2", "--max-time", "120", "--max-filesize", "104857600", "--output", archive, NODE_URL],
        ["sha256sum", archive],
        ["tar", "-xJf", archive, "-C", f"{remote_root}/node", "--strip-components=1"],
        ["chmod", "--", "755", f"{remote_root}/node/bin/node"],
    ]


def remote_qa_argv(remote_root: str, *, run_id: str, fuzz_cases: int, soak_iterations: int, seeds: tuple[int, ...]) -> list[str]:
    node = f"{remote_root}/node/bin/node"
    script = f"{remote_root}/qa/scripts/test/remote_external_tools_qa.mjs"
    output = f"{remote_root}/artifacts/remote-external-tools.json"
    return [
        "env", f"LAE_REMOTE_QA_MARKER={QA_MARKER}", f"LAE_REMOTE_RUN_ID={run_id}",
        node, script, "--seed=" + ",".join(str(seed) for seed in seeds),
        f"--fuzz-cases={fuzz_cases}", f"--soak-iterations={soak_iterations}", f"--output={output}",
    ]


def _minimal_local_env() -> dict[str, str]:
    """Keep provider credentials out of SSH/SCP child environments."""

    # Do not propagate PATH, HOME, proxy variables, or SSH_AUTH_SOCK.  The
    # transport binaries are the only intended local child capabilities.
    return {"PATH": "/usr/bin:/bin:/usr/local/bin", "LANG": "C", "LC_ALL": "C"}


def _terminate_transport(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            process.kill()


def run_argv(argv: list[str], *, timeout: float, capture_stdout: bool = False) -> dict[str, object]:
    """Run an argv-only local transport with bounded output and timeout."""

    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise RunnerError("invalid transport argv")
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_minimal_local_env(), start_new_session=(os.name != "nt"))
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    totals = {"stdout": 0, "stderr": 0}
    captured = bytearray()
    started = time.monotonic()
    overflow = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                _terminate_transport(process)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                return {"status": "timeout", "exit_code": None, "stdout_bytes": totals["stdout"], "stderr_bytes": totals["stderr"]}
            for key, _ in selector.select(min(1.0, remaining)):
                chunk = key.fileobj.read(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = key.data
                totals[stream] += len(chunk)
                if capture_stdout and stream == "stdout":
                    captured.extend(chunk[: max(0, MAX_OUTPUT_BYTES - len(captured))])
                if sum(totals.values()) > MAX_OUTPUT_BYTES:
                    overflow = True
                    _terminate_transport(process)
                    break
            if overflow:
                break
        process.wait(timeout=5)
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_transport(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
    result: dict[str, object] = {"status": "output_overflow" if overflow else "completed", "exit_code": process.returncode, "stdout_bytes": totals["stdout"], "stderr_bytes": totals["stderr"]}
    if capture_stdout:
        result["stdout"] = bytes(captured).decode("utf-8", errors="strict")
    return result


def validate_receipt(data: bytes, *, expected_run_id: str) -> dict[str, object]:
    if len(data) > MAX_RECEIPT_BYTES:
        raise RunnerError("remote receipt exceeds byte bound")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("remote receipt is not bounded UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "remote-external-tools-qa.v1":
        raise RunnerError("unexpected remote QA receipt schema")
    if payload.get("run_id") != expected_run_id or payload.get("remote_marker_verified") is not True:
        raise RunnerError("remote receipt is not bound to this run and marker")
    if payload.get("secret_free") is not True or payload.get("status") not in {"PASS", "FAIL"}:
        raise RunnerError("remote receipt has unsafe or incomplete status")
    text = data.decode("utf-8", errors="strict").lower()
    for forbidden in ("access_token", "client_secret", "authorization", "bearer ", "synthetic-token"):
        if forbidden in text:
            raise RunnerError("remote receipt contains forbidden credential material")
    return {"status": payload["status"], "run_id": expected_run_id, "secret_free": True}


def _assert_review_markers(env: dict[str, str]) -> None:
    if env.get("SOL_SHADEFORM_REVIEWED") != "1" or env.get("SOL_REMOTE_EXTERNAL_TOOLS_REVIEWED") != "1":
        raise RunnerError("provider mutation requires both Sol review markers")


def _approved_target(env: dict[str, str], candidates: Iterable[shadeform.Candidate]) -> shadeform.Candidate:
    expected = {key: env.get(f"SHADEFORM_QA_APPROVED_{key.upper()}", "").strip() for key in ("gpu", "cloud", "region", "instance_type")}
    if not all(expected.values()) or not expected["gpu"].lower().startswith("a100"):
        raise RunnerError("execute requires an explicit approved A100 GPU/cloud/region/instance tuple")
    for candidate in candidates:
        if all(getattr(candidate, key) == expected[key] for key in expected):
            return candidate
    raise RunnerError("approved target is not an eligible current catalogue candidate")


def _stop_watchdog(watchdog: subprocess.Popen[bytes] | None) -> None:
    if watchdog is None or watchdog.poll() is not None:
        return
    with contextlib.suppress(OSError):
        watchdog.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        watchdog.wait(timeout=10)
    if watchdog.poll() is None:
        with contextlib.suppress(OSError):
            watchdog.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            watchdog.wait(timeout=10)


def execute(args: argparse.Namespace) -> dict[str, object]:
    env = shadeform.load_env(args.env_file)
    _assert_review_markers(env)
    plan = build_plan(phase_id=args.phase_id, run_id=args.run_id, runtime_hours=args.runtime_hours, fuzz_cases=args.fuzz_cases, soak_iterations=args.soak_iterations, env=env)
    api_key = shadeform.require_env(env, "SHADEFORM_API_KEY")
    candidates = shadeform.list_candidates(api_key, env, phase_id=args.phase_id, min_vram_gb=80, max_runtime_hours=args.runtime_hours)
    candidate = _approved_target(env, candidates)
    nonce = shadeform.new_ownership_nonce()
    instance_id: str | None = None
    key_id: str | None = None
    recorded = False
    ambiguous_create = False
    watchdog: subprocess.Popen[bytes] | None = None
    info: dict[str, Any] | None = None
    ssh: list[str] | None = None
    scp: list[str] | None = None
    remote_root = str(plan["remote_root"])
    receipt_local = args.output or (ROOT / "experiments" / "results" / f"{args.run_id}.remote-external-tools.json")
    lifecycle: dict[str, object] = {"schema": "local_bmo.shadeform.remote-external-tools-receipt.v1", "phase_id": args.phase_id, "run_id": args.run_id, "status": "starting", "node": plan["node"], "closure_sha256": plan["closure_sha256"]}
    with tempfile.TemporaryDirectory(prefix=f"remote-tools-{args.phase_id}-") as temporary:
        temporary_root = Path(temporary)
        identity, public_key = shadeform.create_ephemeral_ssh_key(env, temporary_root / "ssh")
        known_hosts = temporary_root / "known_hosts"
        attempt_id = shadeform.reserve_create_attempt(args.phase_id, nonce, candidate, backstop_hours=float(plan["provider_backstop_hours"]), public_key_sha256=hashlib.sha256(public_key.encode()).hexdigest())
        try:
            key_id = shadeform.add_ssh_key(api_key, args.phase_id, f"qa-{nonce}", public_key)
            shadeform.verify_ssh_key_ownership(api_key, args.phase_id, key_id, expected_name=f"qa-{nonce}", expected_public_key=public_key)
            shadeform.reserve_create_attempt(args.phase_id, nonce, candidate, backstop_hours=float(plan["provider_backstop_hours"]), public_key_sha256=hashlib.sha256(public_key.encode()).hexdigest(), ssh_key_id=key_id)
            try:
                instance_id = shadeform.create_instance(api_key, env, phase_id=args.phase_id, run_id=args.run_id, candidate=candidate, ssh_key_id=key_id, nonce=nonce, max_runtime_hours=args.runtime_hours)
            except Exception as exc:
                ambiguous_create = shadeform.is_ambiguous_transport(exc)
                with contextlib.suppress(Exception):
                    shadeform.append_incident({"phase_id": args.phase_id, "incident": "create-response-ambiguous" if ambiguous_create else "create-definitive-failure", "nonce": nonce, "ssh_key_id": key_id, "error_type": type(exc).__name__})
                raise
            record = shadeform.OwnedResource(phase_id=args.phase_id, run_id=args.run_id, instance_id=instance_id, ownership_nonce=nonce, ssh_key_id=key_id, ssh_key_name=f"qa-{nonce}", gpu=candidate.gpu, cloud=candidate.cloud, region=candidate.region, hourly_usd=candidate.hourly_usd, created_at_utc=shadeform.utc_now().isoformat(), active_deadline_utc=(shadeform.utc_now() + shadeform.timedelta(seconds=900)).isoformat(), run_deadline_utc=(shadeform.utc_now() + shadeform.timedelta(hours=args.runtime_hours)).isoformat(), instance_type=candidate.instance_type, launcher_pid=os.getpid(), launcher_start_marker=shadeform.process_start_marker(os.getpid()))
            shadeform.write_owned_resource(record)
            recorded = True
            watchdog_command = [sys.executable, str(ROOT / "scripts" / "shadeform_watchdog.py"), "--phase-id", args.phase_id, "--instance-id", instance_id, "--launcher-pid", str(os.getpid()), "--max-seconds", str(max(900, int(float(plan["provider_backstop_hours"]) * 3600) + 120)), "--env-file", str(args.env_file)]
            if record.launcher_start_marker:
                watchdog_command += ["--launcher-start-marker", record.launcher_start_marker]
            watchdog = subprocess.Popen(watchdog_command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_minimal_local_env())
            shadeform.append_cost_event({"instance_id": instance_id, "phase_id": args.phase_id, "status": "pending", "estimated_cost_usd": round(candidate.hourly_usd * float(plan["provider_backstop_hours"]), 6)})
            shadeform.append_cost_event({"instance_id": attempt_id, "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
            info = shadeform.wait_active(api_key, args.phase_id, instance_id, timeout_seconds=900)
            shadeform.verify_instance_ownership(info, instance_id=instance_id, phase_id=args.phase_id, nonce=nonce, expected_name=shadeform.owned_instance_name(args.run_id, nonce), ssh_key_id=key_id, expected_cloud=candidate.cloud, expected_region=candidate.region, expected_instance_type=candidate.instance_type, expected_hourly_usd=candidate.hourly_usd, expected_gpu=candidate.gpu, expected_gpu_count=1, expected_vram_gb=candidate.vram_gb, expected_os_image=candidate.os_image)
            shadeform.acquire_pinned_host_key(info, known_hosts)
            ssh = shadeform.ssh_base(info, identity, known_hosts)
            scp = shadeform.scp_base(info, identity, known_hosts)
            ssh_user = shadeform.validate_ssh_user(info["ssh_user"])
            remote_destination = f"{ssh_user}@{info['ip']}"
            shutdown_minutes = max(5, math.ceil(float(plan["provider_backstop_hours"]) * 60))
            shutdown = run_argv(ssh + ["sudo", "shutdown", "-h", f"+{shutdown_minutes}"], timeout=30)
            lifecycle["host_shutdown_backstop"] = {"status": shutdown["status"], "exit_code": shutdown.get("exit_code")}
            if shutdown["status"] != "completed" or shutdown.get("exit_code") != 0:
                raise RunnerError("host shutdown backstop could not be armed")
            for command in _remote_bootstrap_commands(remote_root, ssh_user):
                result = run_argv(ssh + command, timeout=180, capture_stdout=(command[0] == "sha256sum"))
                lifecycle.setdefault("remote_stages", []).append({"stage": command[0], **result})
                if result["status"] != "completed" or result.get("exit_code") != 0:
                    raise RunnerError(f"remote bootstrap stage failed: {command[0]}")
                if command[0] == "sha256sum":
                    fields = str(result.get("stdout", "")).split()
                    if len(fields) < 2 or fields[0].lower() != NODE_SHA256 or Path(fields[-1]).name != NODE_ARCHIVE:
                        raise RunnerError("remote Node archive hash did not match the pinned release")
            for relative in UPLOAD_FILES:
                remote_file = f"{remote_root}/qa/{relative}"
                destination = f"{remote_destination}:{remote_file}"
                result = run_argv(ssh + ["mkdir", "-p", str(Path(remote_file).parent)], timeout=30)
                if result["status"] != "completed" or result.get("exit_code") != 0:
                    raise RunnerError("remote closure directory setup failed")
                result = run_argv(scp + [str(ROOT / relative), destination], timeout=120)
                if result["status"] != "completed" or result.get("exit_code") != 0:
                    raise RunnerError(f"remote closure upload failed: {relative}")
            qa_result = run_argv(ssh + remote_qa_argv(remote_root, run_id=args.run_id, fuzz_cases=args.fuzz_cases, soak_iterations=args.soak_iterations, seeds=tuple(args.seeds)), timeout=max(120, int(args.runtime_hours * 3600)))
            lifecycle["qa_transport"] = qa_result
            lifecycle["status"] = "completed" if qa_result.get("exit_code") == 0 else "failed"
        except Exception as exc:
            lifecycle["status"] = "failed"
            lifecycle["error_type"] = type(exc).__name__
            raise
        finally:
            _stop_watchdog(watchdog)
            # Salvage is attempted before exact deletion even when bootstrap
            # or QA failed after the remote receipt was created.
            if scp is not None and info is not None:
                with contextlib.suppress(Exception):
                    receipt_local.parent.mkdir(parents=True, exist_ok=True)
                    remote_receipt = f"{shadeform.validate_ssh_user(info['ssh_user'])}@{info['ip']}:{remote_root}/artifacts/remote-external-tools.json"
                    salvage = run_argv(scp + [remote_receipt, str(receipt_local)], timeout=120)
                    lifecycle["salvage"] = {"status": salvage["status"], "exit_code": salvage.get("exit_code")}
                    if salvage["status"] == "completed" and salvage.get("exit_code") == 0:
                        lifecycle["qa_receipt"] = validate_receipt(receipt_local.read_bytes(), expected_run_id=args.run_id)
            if instance_id is not None:
                if recorded:
                    with contextlib.suppress(Exception):
                        lifecycle["deletion"] = shadeform_teardown(args.phase_id, instance_id, args.env_file)
                else:
                    with contextlib.suppress(Exception):
                        shadeform._delete_instance(api_key, args.phase_id, instance_id)
            if key_id is not None and not recorded:
                with contextlib.suppress(Exception):
                    shadeform.delete_ssh_key(api_key, args.phase_id, key_id)
            if instance_id is None and not ambiguous_create:
                with contextlib.suppress(Exception):
                    shadeform.append_cost_event({"instance_id": attempt_id, "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
    return lifecycle


def shadeform_teardown(phase_id: str, instance_id: str, env_file: Path) -> dict[str, object]:
    from scripts.shadeform_teardown import teardown_exact
    return teardown_exact(phase_id, instance_id, env_file=env_file)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-id", default="qa-remote-tools")
    parser.add_argument("--run-id", default="remote-tools-001")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-hours", type=float, default=DEFAULT_RUNTIME_HOURS)
    parser.add_argument("--fuzz-cases", type=int, default=DEFAULT_FUZZ_CASES)
    parser.add_argument("--soak-iterations", type=int, default=DEFAULT_SOAK_ITERATIONS)
    parser.add_argument("--seed", default="17,31,73")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    args.phase_id = shadeform.validate_phase_id(args.phase_id)
    args.run_id = bounded_id(args.run_id, field="run id")
    args.seeds = tuple(bounded_nonnegative(int(item), field="seed", maximum=0x7FFFFFFF) for item in args.seed.split(",") if item != "")
    if not args.seeds or len(args.seeds) > 32:
        raise ValueError("one to 32 seeds are required")
    return args


def self_test() -> None:
    plan = build_plan(phase_id="qa-remote-tools", run_id="self-test", runtime_hours=0.25, fuzz_cases=1, soak_iterations=1, env={"SHADEFORM_QA_APPROVED_GPU": "A100 80GB", "SHADEFORM_QA_APPROVED_CLOUD": "fake", "SHADEFORM_QA_APPROVED_REGION": "test", "SHADEFORM_QA_APPROVED_INSTANCE_TYPE": "fake-a100"})
    assert plan["node"]["sha256"] == NODE_SHA256
    assert remote_qa_argv(str(plan["remote_root"]), run_id="self-test", fuzz_cases=1, soak_iterations=1, seeds=(17,))[1].startswith("LAE_REMOTE_QA_MARKER=")
    bootstrap = _remote_bootstrap_commands(str(plan["remote_root"]), "runner")
    assert bootstrap[0] == ["sudo", "mkdir", "-p", "/scratch"]
    assert bootstrap[-1][-1].endswith("/node/bin/node")
    transport = run_argv([sys.executable, "-c", "print('transport-ok')"], timeout=5, capture_stdout=True)
    assert transport["status"] == "completed" and transport["exit_code"] == 0 and transport["stdout"] == "transport-ok\n"
    valid_receipt = json.dumps({"schema_version": "remote-external-tools-qa.v1", "run_id": "self-test", "remote_marker_verified": True, "status": "PASS", "secret_free": True}).encode()
    assert validate_receipt(valid_receipt, expected_run_id="self-test")["status"] == "PASS"
    try:
        validate_receipt(b"{}", expected_run_id="self-test")
    except RunnerError:
        pass
    else:
        raise AssertionError("malformed receipt accepted")
    assert len(closure_manifest()) == len(UPLOAD_FILES)
    print(json.dumps({"status": "PASS", "checks": ["closure", "pinned-node", "marker", "receipt-schema"]}, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse(argv or sys.argv[1:])
        if args.self_test:
            self_test()
            return 0
        env = shadeform.load_env(args.env_file) if args.env_file.is_file() else {}
        plan = build_plan(phase_id=args.phase_id, run_id=args.run_id, runtime_hours=args.runtime_hours, fuzz_cases=args.fuzz_cases, soak_iterations=args.soak_iterations, env=env)
        if not args.execute:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        result = execute(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") == "completed" else 1
    except (RunnerError, shadeform.ShadeformError, ValueError, OSError) as exc:
        print(json.dumps({"status": "refused", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
