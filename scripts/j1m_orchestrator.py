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
import re
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
_STDERR_TAIL_LIMIT = 1200


class OperatorCancelled(Exception):
    pass


def _redacted_stderr_tail(value: object) -> str:
    """Return bounded stderr evidence without allowing credential-shaped text."""

    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+", r"\1\2<redacted>", text)
    return text[-_STDERR_TAIL_LIMIT:]


def _persist_lifecycle(phase_id: str, lifecycle: dict[str, Any]) -> None:
    """Durably retain bounded local failure/progress evidence before teardown."""

    path = sf.runtime_ledger_path(phase_id).with_name(f"{phase_id}.lifecycle-receipt.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps({"schema": "local_bmo.j1m.lifecycle-receipt.v1", **lifecycle}, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _remote(command: list[str], *, timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"status": "transport_timeout", "exit_code": None, "error_type": type(exc).__name__, "stderr_tail": _redacted_stderr_tail(exc.stderr)}
    receipt = {"status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "stderr_tail": _redacted_stderr_tail(result.stderr)}
    if result.returncode != 0:
        receipt["error_type"] = "remote_exit"
    return receipt


def _remote_job_command(mode: str, remote_root: str, required_scratch_gib: int) -> list[str]:
    """Build the exact remote argv against files uploaded to ``remote_root``."""

    runner = f"{remote_root}/j1m_runner.py"
    config = f"{remote_root}/j1m-config.json"
    if mode == "prove":
        return [
            "python3", runner,
            "--config", config,
            "--prove",
            "--scratch", "/scratch",
            "--min-scratch-gib", str(required_scratch_gib),
            "--output", f"{remote_root}/artifacts/proving-receipt.json",
        ]
    if mode == "build":
        return ["python3", runner, "--run", "--config", config]
    raise ValueError(f"unsupported J1M mode: {mode}")


def _verify_eval_artifact(path: Path, manifest_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Verify the local Q4 identity before any provider call is possible."""

    expected_name = "Qwen3.5-9B-Q4_K_M.gguf"
    if path.name != expected_name or not path.is_file():
        raise ValueError("eval requires the exact local Q4_K_M artifact")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.1.0":
        raise ValueError("eval model manifest is invalid")
    source = manifest.get("source")
    conversion = manifest.get("conversion")
    artifact = manifest.get("artifact")
    if not isinstance(source, dict) or source.get("revision") != config["source"]["revision"]:
        raise ValueError("eval source revision does not match the pinned source")
    if source.get("organization") != "Qwen" or source.get("repository") != "Qwen3.5-9B":
        raise ValueError("eval source identity is not the approved Qwen repository")
    if not isinstance(conversion, dict) or conversion.get("llama_cpp_revision") != config["llama_cpp"]["revision"]:
        raise ValueError("eval llama.cpp revision does not match the pinned converter")
    if not isinstance(artifact, dict) or artifact.get("expected_file_name") != expected_name:
        raise ValueError("eval artifact manifest has the wrong model")
    if artifact.get("modality_profile") != "text_only_no_mmproj" or artifact.get("quantization_profile") != "Q4_K_M":
        raise ValueError("eval artifact modality or quantization is not approved")
    manifest_lock = manifest_path.with_name("model-manifest.sha256")
    if not manifest_lock.is_file():
        raise ValueError("eval model manifest has no checksum lock")
    lock_parts = manifest_lock.read_text(encoding="utf-8").strip().split()
    if len(lock_parts) != 2 or lock_parts[1] != manifest_path.name or len(lock_parts[0]) != 64 or any(character not in "0123456789abcdef" for character in lock_parts[0]):
        raise ValueError("eval model manifest checksum lock is invalid")
    if j1m_runner._sha256(manifest_path) != lock_parts[0]:
        raise ValueError("eval model manifest checksum mismatch")
    size = path.stat().st_size
    digest = j1m_runner._sha256(path)
    if artifact.get("expected_size_bytes") != size or artifact.get("sha256") != digest:
        raise ValueError("eval artifact size or SHA-256 does not match the approved manifest")
    return {"name": expected_name, "size_bytes": size, "sha256": digest}


def _eval_uploads(config: dict[str, Any], remote_root: str, artifact_path: Path, manifest_path: Path) -> list[tuple[Path, str, bool]]:
    """Local files to upload for eval; the GGUF and receipts stay allowlisted."""

    return [
        (artifact_path, f"{remote_root}/model/Qwen3.5-9B-Q4_K_M.gguf", False),
        (manifest_path, f"{remote_root}/model-manifest.json", False),
        (ROOT / "scripts" / "test" / "remote_model_eval.py", f"{remote_root}/remote_model_eval.py", False),
        (ROOT / "scripts" / "test" / "evaluate_tool_calls.py", f"{remote_root}/evaluate_tool_calls.py", False),
        (ROOT / "tests" / "model" / "tool_call_eval.json", f"{remote_root}/tool_call_eval.json", False),
        (ROOT / "CMakeLists.txt", f"{remote_root}/engine/CMakeLists.txt", False),
        # Recursive scp copies the source directory beneath its destination;
        # target the engine parent so the result is exactly engine/native.
        (ROOT / "native", f"{remote_root}/engine", True),
    ]


def _eval_remote_commands(config: dict[str, Any], remote_root: str) -> list[list[str]]:
    """Build the reviewed, non-shell argv stages for native CPU eval."""

    llama = config["llama_cpp"]
    checkout = f"{remote_root}/llama.cpp"
    engine_root = f"{remote_root}/engine"
    build_root = f"{remote_root}/engine-build"
    return [
        ["mkdir", "-p", f"{remote_root}/model", f"{engine_root}/native", f"{engine_root}/vendor", f"{remote_root}/artifacts"],
        ["git", "clone", "--filter=blob:none", llama["repository"], checkout],
        ["git", "-C", checkout, "checkout", "--detach", llama["revision"]],
        ["git", "-C", checkout, "rev-parse", "HEAD"],
        ["cp", "-a", checkout, f"{engine_root}/vendor/llama.cpp"],
        ["cmake", "-S", engine_root, "-B", build_root, "-DCMAKE_BUILD_TYPE=Release", "-DLAE_ENABLE_LLAMA_CPP=ON"],
        ["cmake", "--build", build_root, "--target", "lae-engine", "--parallel", "2"],
        ["python3", f"{remote_root}/remote_model_eval.py", "--model", f"{remote_root}/model/Qwen3.5-9B-Q4_K_M.gguf", "--model-manifest", f"{remote_root}/model-manifest.json", "--source-revision", config["source"]["revision"], "--llama-revision", llama["revision"], "--llama-checkout", checkout, "--engine", f"{build_root}/native/lae-engine", "--evaluator", f"{remote_root}/evaluate_tool_calls.py", "--fixture", f"{remote_root}/tool_call_eval.json", "--token-file", f"{remote_root}/engine-token", "--receipt", f"{remote_root}/artifacts/eval-receipt.json", "--timeout", "600"],
    ]


def _eval_timeout(provider_deadline: float, requested: float, *, reserve: float = 120.0) -> float:
    """Return a stage timeout that cannot extend beyond the provider clock."""

    remaining = provider_deadline - time.monotonic() - reserve
    if remaining < 30.0:
        raise sf.ShadeformError("eval provider deadline has no cleanup-safe stage budget remaining")
    return min(float(requested), remaining)


def _verify_eval_receipt(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Accept only the bounded aggregate receipt produced by remote eval."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "local_bmo.j1m.real-tool-eval-receipt.v1":
        raise ValueError("eval receipt schema mismatch")
    recorded = payload.get("artifact")
    if not isinstance(recorded, dict) or recorded.get("name") != artifact["name"] or recorded.get("size_bytes") != artifact["size_bytes"] or recorded.get("sha256") != artifact["sha256"]:
        raise ValueError("eval receipt artifact mismatch")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or payload.get("status") not in {"verified", "completed_with_failures"} or metrics.get("case_count") != 8 or any(isinstance(metrics.get(key), bool) or not isinstance(metrics.get(key), int) or metrics[key] < 0 for key in ("passed", "failed", "errors")):
        raise ValueError("eval receipt metrics invalid")
    if sum(metrics[key] for key in ("passed", "failed", "errors")) != 8:
        raise ValueError("eval receipt metric totals invalid")
    peak_rss = metrics.get("peak_rss_kib")
    if peak_rss is not None and (isinstance(peak_rss, bool) or not isinstance(peak_rss, int) or peak_rss < 0):
        raise ValueError("eval receipt RSS metric invalid")
    if payload.get("prompt_response_logging") is not False or payload.get("token_logging") is not False:
        raise ValueError("eval receipt logging policy missing")
    return {"status": payload.get("status"), "metrics": {key: metrics[key] for key in ("case_count", "passed", "failed", "errors", "peak_rss_kib")}}


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


def execute(env_file: Path, *, config_path: Path, phase_id: str, run_id: str, artifact_destination: Path, mode: str = "prove", model_artifact: Path | None = None, model_manifest: Path | None = None) -> dict[str, Any]:
    config = j1m_runner.load_config(config_path)
    if mode == "eval":
        if model_artifact is None:
            raise ValueError("eval requires --model-artifact; no implicit or alternate model is accepted")
        model_manifest = model_manifest or model_artifact.parent / "model-manifest.json"
        eval_artifact = _verify_eval_artifact(model_artifact, model_manifest, config)
    else:
        eval_artifact = None
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
        if eval_artifact is not None:
            lifecycle["artifact"] = eval_artifact
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
            # Bind the exact provider key ID into the already-pending attempt
            # reservation before allowing the instance create POST.
            sf.reserve_create_attempt(phase_id, nonce, candidate, backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]), public_key_sha256=hashlib.sha256(public_key.encode("utf-8")).hexdigest(), ssh_key_id=key_id)
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
            ssh_user = sf.validate_ssh_user(info["ssh_user"])
            lifecycle["host_key"] = sf.acquire_pinned_host_key(info, known_hosts)
            lifecycle["status"] = "active"
            remote_root = "/scratch/j1m"
            workspace_stages = (
                ("scratch_root", ["sudo", "mkdir", "-p", "/scratch"]),
                ("scratch_owner", ["sudo", "chown", ssh_user, "/scratch"]),
                ("remote_workspace", ["mkdir", "-p", remote_root]),
                ("scratch_df", ["df", "-P", "-k", "/scratch"]),
                ("scratch_writable", ["test", "-w", "/scratch"]),
            )
            for stage_name, stage_argv in workspace_stages:
                result = _remote(sf.ssh_base(info, identity, known_hosts) + stage_argv, timeout=30)
                lifecycle[stage_name] = result
                if result["status"] != "completed":
                    raise sf.ShadeformError(f"remote {stage_name} preflight failed")
            shutdown_minutes = str(config["modes"][mode]["host_shutdown_delay_minutes"])
            lifecycle["host_shutdown_backstop"] = _remote(sf.ssh_base(info, identity, known_hosts) + ["sudo", "shutdown", "-h", f"+{shutdown_minutes}"], timeout=30)
            if lifecycle["host_shutdown_backstop"]["status"] != "completed":
                raise sf.ShadeformError("host shutdown backstop could not be armed")
            upload = sf.scp_base(info, identity, known_hosts) + [str(config_path), f"{ssh_user}@{info['ip']}:{remote_root}/j1m-config.json"]
            lifecycle["upload"] = _remote(upload, timeout=120)
            source_lock = ROOT / config["source"]["lock"]
            for local, remote in ((ROOT / "scripts" / "j1m_runner.py", f"{remote_root}/j1m_runner.py"), (source_lock, f"{remote_root}/qwen35-9b.source-lock.json")):
                upload_receipt = _remote(sf.scp_base(info, identity, known_hosts) + [str(local), f"{ssh_user}@{info['ip']}:{remote}"], timeout=120)
                lifecycle.setdefault("uploads", []).append(upload_receipt)
                if upload_receipt["status"] != "completed":
                    raise sf.ShadeformError("required J1M upload failed")
            if mode == "prove":
                lifecycle["job"] = _remote(
                    sf.ssh_base(info, identity, known_hosts)
                    + _remote_job_command(mode, remote_root, int(config["resources"]["required_scratch_gib"])),
                    timeout=120,
                )
            elif mode == "build":
                # Qwen3.5-9B is public at the pinned revision. Do not place
                # HF_TOKEN on the ephemeral host; the runner downloads it
                # unauthenticated and child environments remain sanitized.
                remote_job = sf.ssh_base(info, identity, known_hosts) + _remote_job_command(
                    mode, remote_root, int(config["resources"]["required_scratch_gib"])
                )
                j1m_runner.write_progress(progress_path, "remote-build-starting", phase_id=phase_id)
                transfer_reserve = float(config["modes"][mode].get("transfer_reserve_seconds", 0))
                lifecycle["job"] = _remote(remote_job, timeout=max(30, provider_deadline - time.monotonic() - transfer_reserve - 120))
            else:
                eval_commands = _eval_remote_commands(config, remote_root)
                # The clone and immutable revision check precede uploads; the
                # remaining stages consume the uploaded source/evaluator.
                for command in eval_commands[:4]:
                    stage = _remote(sf.ssh_base(info, identity, known_hosts) + command, timeout=_eval_timeout(provider_deadline, 300))
                    lifecycle.setdefault("eval_stages", []).append(stage)
                    if stage["status"] != "completed":
                        raise sf.ShadeformError("eval source preparation failed")
                for local, remote, recursive in _eval_uploads(config, remote_root, model_artifact, model_manifest):
                    scp = sf.scp_base(info, identity, known_hosts)
                    if recursive:
                        scp = [scp[0], "-r", *scp[1:]]
                    destination = f"{ssh_user}@{info['ip']}:{remote}"
                    requested_timeout = max(600.0, float(eval_artifact["size_bytes"]) / (1024**3) * 180.0) if local == model_artifact else 180.0
                    timeout = _eval_timeout(provider_deadline, requested_timeout)
                    upload_receipt = _remote(scp + [str(local), destination], timeout=timeout)
                    lifecycle.setdefault("eval_uploads", []).append({"name": local.name, **upload_receipt})
                    if upload_receipt["status"] != "completed":
                        raise sf.ShadeformError("required eval upload failed")
                for command in eval_commands[4:]:
                    stage = _remote(sf.ssh_base(info, identity, known_hosts) + command, timeout=_eval_timeout(provider_deadline, 600))
                    lifecycle.setdefault("eval_stages", []).append(stage)
                    if stage["status"] != "completed":
                        raise sf.ShadeformError("eval build or evaluation failed")
                lifecycle["job"] = lifecycle["eval_stages"][-1]
            if lifecycle["job"]["status"] != "completed":
                lifecycle["status"] = lifecycle["job"]["status"]
                raise sf.ShadeformError("J1M remote job did not complete")
            lifecycle["status"] = "completed"
        except (KeyboardInterrupt, OperatorCancelled):
            lifecycle["status"] = "cancelled_by_operator"
            if instance_id is None and not ambiguous_create:
                settle_attempt_after_cleanup = True
            raise
        except Exception as exc:
            # Key upload/ownership validation and other definitive local
            # failures occur outside the create-specific handler. They still
            # reconcile the pre-create reservation after finally cleanup when
            # no instance POST could have succeeded.
            if instance_id is None and not ambiguous_create:
                settle_attempt_after_cleanup = True
            lifecycle["status"] = lifecycle.get("status") if lifecycle.get("status") not in {None, "starting", "active"} else "failed"
            lifecycle["failure"] = {"error_type": type(exc).__name__}
            raise
        finally:
            try:
                _persist_lifecycle(phase_id, lifecycle)
            except Exception:
                pass
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
            fetch_allowlist = (
                config["artifacts"]["local_fetch_allowlist"] if mode == "build"
                else config["artifacts"]["eval_fetch_allowlist"] if mode == "eval"
                else config["artifacts"]["prove_fetch_allowlist"]
            )
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
            if mode == "eval" and lifecycle.get("job", {}).get("status") == "completed":
                saved_receipt = next((item for item in lifecycle["salvage"] if item.get("name") == "eval-receipt.json" and item.get("status") == "completed"), None)
                if saved_receipt is None:
                    lifecycle["receipt_error"] = "eval receipt was not salvaged before teardown"
                else:
                    try:
                        lifecycle["eval_receipt"] = _verify_eval_receipt(artifact_destination / "eval-receipt.json", eval_artifact)
                    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        lifecycle["receipt_error"] = type(exc).__name__
                if lifecycle.get("receipt_error"):
                    lifecycle["status"] = "failed"
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
            try:
                _persist_lifecycle(phase_id, lifecycle)
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
    parser.add_argument("--mode", choices=("prove", "build", "eval"), default="prove")
    parser.add_argument("--model-artifact", type=Path, help="exact approved local Q4_K_M artifact required by --mode eval")
    parser.add_argument("--model-manifest", type=Path, help="approved model manifest paired with --model-artifact")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    config = j1m_runner.load_config(args.config)
    plan = j1m_runner.build_plan(config, args.mode)
    plan["mode"] = args.mode
    plan["mode_runtime_hours"] = config["modes"][args.mode]["runtime_hours"]
    plan["mode_active_cost_usd"] = config["modes"][args.mode]["active_cost_usd"]
    if args.mode == "eval":
        plan["commands"] = _eval_remote_commands(config, "/scratch/j1m")
        plan["artifact"] = "--model-artifact is required at execution; no model is copied during planning"
    if not args.execute:
        plan["orchestrator"] = "dry-run; no provider API mutation"
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if os.environ.get("SOL_J1M_REVIEWED") != "1":
        raise SystemExit("refusing mutation: Sol must set SOL_J1M_REVIEWED=1 after reviewing the plan")
    print(json.dumps(execute(args.env_file, config_path=args.config, phase_id=args.phase_id, run_id=args.run_id, artifact_destination=args.artifact_destination, mode=args.mode, model_artifact=args.model_artifact, model_manifest=args.model_manifest), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
