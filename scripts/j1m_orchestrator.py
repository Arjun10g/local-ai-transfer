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
import math
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
_EVAL_FIXTURE_MAX_BYTES = 256 * 1024
_EVAL_RECEIPT_MAX_BYTES = 64 * 1024
_PREFLIGHT_RECEIPT_MAX_BYTES = 1024
_DELETION_RESERVE_SECONDS = 480.0


class OperatorCancelled(Exception):
    pass


def _redacted_output_tail(value: object) -> str:
    """Return bounded command evidence without allowing credential-shaped text."""

    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+", r"\1\2<redacted>", text)
    return text[-_STDERR_TAIL_LIMIT:]


def _bounded_bytes(path: Path, limit: int) -> bytes:
    """Read one bounded descriptor snapshot, rejecting replacement."""
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit:
            raise ValueError("receipt exceeds bound")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.stat()
        if (len(raw) > limit or before.st_size != after.st_size or after.st_size != len(raw) or
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("receipt changed during bounded read")
        return raw


def _bounded_json(path: Path, limit: int) -> Any:
    """Decode a small receipt without duplicate keys or unbounded reads."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("receipt contains duplicate key")
            result[key] = value
        return result

    raw = _bounded_bytes(path, limit)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)


def _persist_lifecycle(phase_id: str, lifecycle: dict[str, Any]) -> None:
    """Durably retain bounded local failure/progress evidence before teardown."""

    path = sf.runtime_ledger_path(phase_id).with_name(f"{phase_id}.lifecycle-receipt.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps({"schema": "local_bmo.j1m.lifecycle-receipt.v1", **lifecycle}, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _progress(path: Path, event: str, **details: Any) -> None:
    """Best-effort atomic progress marker; never masks cleanup failures."""

    try:
        j1m_runner.write_progress(path, event, **details)
    except Exception:
        pass


def _tool_eval_contract() -> tuple[int, set[str]]:
    """Return the bounded case/category contract shipped with the evaluator."""

    fixture_path = ROOT / "tests" / "model" / "tool_call_eval.json"
    fixture = _bounded_json(fixture_path, _EVAL_FIXTURE_MAX_BYTES)
    limits = fixture.get("limits") if isinstance(fixture, dict) else None
    cases = fixture.get("cases") if isinstance(fixture, dict) else None
    count = limits.get("max_cases") if isinstance(limits, dict) else None
    if (not isinstance(fixture, dict) or set(fixture) != {"schema", "model", "protocol", "limits", "tools", "cases"} or
            fixture.get("schema") != "local_bmo.tool-call-eval.v1" or isinstance(count, bool) or not isinstance(count, int) or
            not isinstance(cases, list) or len(cases) != count or not 1 <= count <= 40):
        raise ValueError("eval fixture count invalid")
    if any(not isinstance(case, dict) or set(case) != {"id", "category", "messages", "expected"} or
           not isinstance(case.get("id"), str) or not 1 <= len(case["id"]) <= 128 or
           not isinstance(case.get("category"), str) or not 1 <= len(case["category"]) <= 64 or
           not isinstance(case.get("messages"), list) or not isinstance(case.get("expected"), dict)
           for case in cases):
        raise ValueError("eval fixture case invalid")
    categories = {case["category"] for case in cases}
    if not categories or not all(isinstance(category, str) and category.isascii() for category in categories):
        raise ValueError("eval fixture categories invalid")
    return count, categories


def _remote(command: list[str], *, timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"status": "transport_timeout", "exit_code": None, "error_type": type(exc).__name__, "stderr_tail": _redacted_output_tail(exc.stderr)}
    receipt = {"status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "stderr_tail": _redacted_output_tail(result.stderr)}
    if result.returncode != 0:
        receipt["error_type"] = "remote_exit"
        # Never retain provider/model prompt or response material from a
        # failed command. The finite error type plus bounded stderr tail are
        # sufficient for lifecycle diagnosis.
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


def _verify_eval_artifact(path: Path | None, manifest_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Verify the accepted Q4 identity without requiring local model bytes."""

    expected_name = "Qwen3.5-9B-Q4_K_M.gguf"
    if path is not None and (path.name != expected_name or not path.is_file()):
        raise ValueError("eval artifact has the wrong name or is missing")
    manifest_bytes = _bounded_bytes(manifest_path, _EVAL_FIXTURE_MAX_BYTES)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("eval model manifest contains duplicate key")
            result[key] = value
        return result
    manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=reject_duplicates)
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
    if manifest_lock.stat().st_size > 4096:
        raise ValueError("eval model manifest checksum lock is invalid")
    with manifest_lock.open("rb") as lock_stream:
        lock_bytes = lock_stream.read(4097)
    if len(lock_bytes) > 4096:
        raise ValueError("eval model manifest checksum lock is invalid")
    try:
        lock_parts = lock_bytes.decode("utf-8").strip().split()
    except UnicodeDecodeError as exc:
        raise ValueError("eval model manifest checksum lock is invalid") from exc
    if len(lock_parts) != 2 or lock_parts[1] != manifest_path.name or len(lock_parts[0]) != 64 or any(character not in "0123456789abcdef" for character in lock_parts[0]):
        raise ValueError("eval model manifest checksum lock is invalid")
    if manifest_digest != lock_parts[0]:
        raise ValueError("eval model manifest checksum mismatch")
    size = artifact.get("expected_size_bytes")
    digest = artifact.get("sha256")
    if not isinstance(size, int) or size <= 0 or not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("eval artifact manifest size or SHA-256 is invalid")
    if path is not None and (size != path.stat().st_size or digest != j1m_runner._sha256(path)):
        raise ValueError("eval artifact size or SHA-256 does not match the approved manifest")
    return {
        "name": expected_name,
        "size_bytes": size,
        "sha256": digest,
        "source_revision": config["source"]["revision"],
        "llama_cpp_revision": config["llama_cpp"]["revision"],
        "modality": artifact["modality_profile"],
        "quantization": artifact["quantization_profile"],
    }


def _eval_uploads(config: dict[str, Any], remote_root: str, artifact_path: Path | None, manifest_path: Path) -> list[tuple[Path, str, bool]]:
    """Local files to upload for eval; the GGUF and receipts stay allowlisted."""

    uploads: list[tuple[Path, str, bool]] = []
    if artifact_path is not None:
        uploads.append((artifact_path, f"{remote_root}/model/Qwen3.5-9B-Q4_K_M.gguf", False))
    uploads.extend([
        (manifest_path, f"{remote_root}/model-manifest.json", False),
        (manifest_path.with_name("model-manifest.sha256"), f"{remote_root}/model-manifest.sha256", False),
        (ROOT / "scripts" / "test" / "remote_model_eval.py", f"{remote_root}/remote_model_eval.py", False),
        (ROOT / "scripts" / "test" / "remote_eval_prepare.py", f"{remote_root}/remote_eval_prepare.py", False),
        (ROOT / "scripts" / "test" / "evaluate_tool_calls.py", f"{remote_root}/evaluate_tool_calls.py", False),
        (ROOT / "scripts" / "test" / "cuda_device_probe.py", f"{remote_root}/cuda_device_probe.py", False),
        (ROOT / "scripts" / "test" / "remote_toolchain_probe.py", f"{remote_root}/remote_toolchain_probe.py", False),
        (ROOT / "scripts" / "cuda_source_closure.py", f"{remote_root}/engine/scripts/cuda_source_closure.py", False),
        # The pinned checkout directory is created remotely after these
        # uploads; the lock is copied into its final vendor path by an argv
        # stage after the checkout copy.
        (ROOT / "vendor" / "llama.cpp" / "ggml-cuda-source-lock.json", f"{remote_root}/ggml-cuda-source-lock.json", False),
        # Preserve the product's immutable upstream build-metadata guard. The
        # remote HF checkout is pristine and therefore does not contain this
        # reviewed CMake patch.
        (ROOT / "vendor" / "llama.cpp" / "ggml" / "CMakeLists.txt", f"{remote_root}/ggml-CMakeLists.txt", False),
        (ROOT / "tests" / "model" / "tool_call_eval.json", f"{remote_root}/tool_call_eval.json", False),
        (ROOT / "CMakeLists.txt", f"{remote_root}/engine/CMakeLists.txt", False),
        # Recursive scp copies the source directory beneath its destination;
        # target the engine parent so the result is exactly engine/native.
        (ROOT / "native", f"{remote_root}/engine", True),
        # These sources are referenced unconditionally by native/CMakeLists;
        # they are needed at configure time even when only lae-engine builds.
        (ROOT / "tests" / "native" / "runtime_tests.cpp", f"{remote_root}/engine/tests/native/runtime_tests.cpp", False),
        (ROOT / "tests" / "native" / "model_validator_tests.cpp", f"{remote_root}/engine/tests/native/model_validator_tests.cpp", False),
    ])
    return uploads


def _eval_remote_commands(config: dict[str, Any], remote_root: str) -> list[list[str]]:
    """Build reviewed non-shell argv stages for the authenticated CUDA eval."""

    llama = config["llama_cpp"]
    eval_mode = config["modes"]["eval"]
    device_name = eval_mode["cuda_device_name"]
    cuda_compiler = eval_mode["cuda_compiler"]
    # J1M's command plan owns the checkout location; use that validated
    # config value rather than inventing a remote-root-relative path.
    checkout = llama["checkout"]
    engine_root = f"{remote_root}/engine"
    build_root = f"{remote_root}/engine-build"
    return [
        ["mkdir", "-p", f"{remote_root}/model", f"{engine_root}/native", f"{engine_root}/vendor", f"{engine_root}/scripts", f"{engine_root}/tests/native", f"{remote_root}/artifacts"],
        ["sudo", "apt-get", "update"],
        ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends", "ca-certificates", "cmake", "build-essential", "git", "python3", "python3-venv"],
        # Fail before the expensive HF checkout/conversion when the CUDA
        # compiler is unavailable to a noninteractive SSH process.
        ["python3", f"{remote_root}/remote_toolchain_probe.py", "--nvcc", cuda_compiler, "--output", f"{remote_root}/artifacts/toolchain-receipt.json"],
        # J1M performs the immutable HF download, source verification,
        # conversion and Q4 quantization remotely. This keeps the 5--6 GiB
        # model off the operator laptop and makes the accepted manifest the
        # sole integrity boundary for the generated deployable artifact.
        ["python3", f"{remote_root}/j1m_runner.py", "--run", "--config", f"{remote_root}/j1m-config.json"],
        ["cp", "-a", checkout, f"{engine_root}/vendor/llama.cpp"],
        ["cp", f"{remote_root}/ggml-cuda-source-lock.json", f"{engine_root}/vendor/llama.cpp/ggml-cuda-source-lock.json"],
        ["cp", f"{remote_root}/ggml-CMakeLists.txt", f"{engine_root}/vendor/llama.cpp/ggml/CMakeLists.txt"],
        ["python3", f"{remote_root}/remote_eval_prepare.py", "--artifact", f"{remote_root}/artifacts/Qwen3.5-9B-Q4_K_M.gguf", "--manifest", f"{remote_root}/model-manifest.json", "--output", f"{remote_root}/artifacts/eval-artifact-receipt.json"],
        # This is intentionally after J1M's apt/pip/bootstrap work. The
        # receipt must describe the final environment used by CUDA build/eval.
        ["python3", f"{remote_root}/remote_toolchain_probe.py", "--nvcc", cuda_compiler, "--output", f"{remote_root}/artifacts/toolchain-receipt.json"],
        ["python3", f"{remote_root}/cuda_device_probe.py", "--output", f"{remote_root}/artifacts/cuda-device-receipt.json"],
        ["cmake", "-S", engine_root, "-B", build_root, "-DCMAKE_BUILD_TYPE=Release", "-DLAE_ENABLE_LLAMA_CPP=ON", "-DLAE_ENABLE_LLAMA_CUDA=ON", f"-DCMAKE_CUDA_ARCHITECTURES={eval_mode['cuda_architecture']}", f"-DCMAKE_CUDA_COMPILER={cuda_compiler}"],
        ["cmake", "--build", build_root, "--target", "lae-engine", "--parallel", str(eval_mode["build_parallelism"])],
        ["python3", f"{remote_root}/remote_model_eval.py", "--model", f"{remote_root}/artifacts/Qwen3.5-9B-Q4_K_M.gguf", "--model-manifest", f"{remote_root}/model-manifest.json", "--model-manifest-lock", f"{remote_root}/model-manifest.sha256", "--source-revision", config["source"]["revision"], "--llama-revision", llama["revision"], "--llama-checkout", checkout, "--engine", f"{build_root}/native/lae-engine", "--evaluator", f"{remote_root}/evaluate_tool_calls.py", "--fixture", f"{remote_root}/tool_call_eval.json", "--token-file", f"{remote_root}/engine-token", "--backend", eval_mode["backend"], "--cuda-device-name", device_name, "--cuda-device-receipt", f"{remote_root}/artifacts/cuda-device-receipt.json", "--toolchain-receipt", f"{remote_root}/artifacts/toolchain-receipt.json", "--receipt", f"{remote_root}/artifacts/eval-receipt.json", "--preflight-receipt", f"{remote_root}/artifacts/startup-preflight-receipt.json", "--timeout", "420"],
    ]


def _eval_timeout(provider_deadline: float, requested: float, *, reserve: float = _DELETION_RESERVE_SECONDS) -> float:
    """Return a stage timeout that cannot extend beyond the provider clock."""

    remaining = provider_deadline - time.monotonic() - reserve
    if remaining < 1.0:
        raise sf.ShadeformError("eval provider deadline has no cleanup-safe stage budget remaining")
    return min(float(requested), remaining)


def _eval_stage_timeout(config: dict[str, Any], command: list[str]) -> float:
    """Return a bounded request for one post-upload eval stage."""

    budgets = config["modes"]["eval"]["stage_budgets_seconds"]
    if command[0:2] == ["cmake", "-S"]:
        return float(budgets["configure"])
    if command[0:2] == ["cmake", "--build"]:
        return float(budgets["build"])
    if command[0] == "python3" and any("j1m_runner.py" in part for part in command) and "--run" in command:
        return float(budgets["source_prepare"])
    if command[0] == "python3" and any("remote_eval_prepare.py" in part for part in command):
        return 120.0
    if command[0:2] == ["git", "clone"]:
        return 300.0
    if len(command) >= 2 and command[0:2] == ["git", "-C"]:
        return 60.0
    if command[0] == "cp":
        return 300.0 if len(command) > 1 and command[1] == "-a" else 30.0
    if command[0] == "python3" and any("remote_toolchain_probe.py" in part for part in command):
        return 120.0
    if command[0] == "python3" and any("cuda_device_probe.py" in part for part in command):
        return 120.0
    return float(budgets["evaluation"])


def _eval_stage_label(command: list[str]) -> str:
    """Return a bounded nonsecret label that distinguishes audited stages."""

    if not command:
        return "eval-stage:invalid"
    if command[0] == "python3" and len(command) > 1:
        return f"eval-stage:{Path(command[1]).stem}"
    if command[0:2] == ["cmake", "-S"]:
        return "eval-stage:cmake-configure"
    if command[0:2] == ["cmake", "--build"]:
        return "eval-stage:cmake-build"
    return f"eval-stage:{command[0]}"


def _eval_deadline_ceiling(config: dict[str, Any]) -> dict[str, float]:
    """Compute the remote-only eval's sequential worst-case clock envelope.

    This includes bounded activation/host-key/workspace setup, the three
    initial J1M uploads, all small eval uploads, every post-upload command,
    and the cleanup reserve.  It is deliberately conservative and is checked
    before any provider API mutation.
    """

    mode = config["modes"]["eval"]
    commands = _eval_remote_commands(config, "/scratch/j1m")
    bootstrap = sum((30.0, 120.0, 270.0))
    post_upload = sum(_eval_stage_timeout(config, command) for command in commands[3:])
    eval_uploads = _eval_uploads(
        config, "/scratch/j1m", None,
        ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json",
    )
    small_uploads = float(mode["stage_budgets_seconds"]["small_uploads"])
    # Remote-only has no model upload; every upload is in the small bucket.
    upload_ceiling = small_uploads
    fixed_setup = 3 * 120.0 + 5 * 30.0 + 30.0  # config/runner/lock + workspace + shutdown arm
    host_key = 120.0 + 3 * 15.0  # bounded two-scan acquisition + key fingerprints
    cleanup = float(mode["stage_budgets_seconds"]["cleanup_reserve"])
    work = float(mode.get("activation_timeout_seconds", 600)) + host_key + fixed_setup + bootstrap + upload_ceiling + post_upload
    run_seconds = float(mode["runtime_hours"]) * 3600.0
    watchdog_seconds = float(mode["external_watchdog_seconds"])
    provider_seconds = float(mode["provider_backstop_hours"]) * 3600.0
    host_shutdown_seconds = float(mode.get("activation_timeout_seconds", 600)) + host_key + 5 * 30.0 + 30.0 + float(mode["host_shutdown_delay_minutes"]) * 60.0
    ceiling = work + cleanup
    if not (ceiling < run_seconds < host_shutdown_seconds < watchdog_seconds < provider_seconds):
        raise ValueError("eval sequential budget does not fit run/cleanup/host/watchdog/provider clocks")
    if host_shutdown_seconds - run_seconds < 120.0:
        raise ValueError("eval host shutdown backstop lacks cleanup margin")
    if watchdog_seconds - host_shutdown_seconds < 120.0:
        raise ValueError("eval watchdog backstop lacks host-shutdown jitter margin")
    return {
        "work_seconds": work,
        "cleanup_reserve_seconds": cleanup,
        "ceiling_seconds": ceiling,
        "host_shutdown_from_create_seconds": host_shutdown_seconds,
        "watchdog_seconds": watchdog_seconds,
        "run_seconds": run_seconds,
        "provider_seconds": provider_seconds,
        "upload_count": float(len(eval_uploads)),
    }


def _verify_eval_receipt(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Accept only the bounded aggregate receipt produced by remote eval."""

    payload = _bounded_json(path, _EVAL_RECEIPT_MAX_BYTES)
    allowed_top_level = {"schema", "status", "artifact", "engine", "model_preflight", "cuda_device", "toolchain", "metrics", "duration_ms", "prompt_response_logging", "token_logging"}
    required_top_level = {"schema", "status", "artifact", "engine", "model_preflight", "toolchain", "metrics", "prompt_response_logging", "token_logging"}
    if not isinstance(payload, dict) or payload.get("schema") != "local_bmo.j1m.real-tool-eval-receipt.v1":
        raise ValueError("eval receipt schema mismatch")
    # Keep the diagnostic specific for a missing mandatory evidence section;
    # other missing/unknown top-level fields remain a schema failure.
    if "toolchain" not in payload:
        raise ValueError("eval receipt toolchain evidence invalid")
    if not required_top_level <= set(payload) or set(payload) - allowed_top_level:
        raise ValueError("eval receipt schema mismatch")
    if "duration_ms" in payload and (isinstance(payload["duration_ms"], bool) or not isinstance(payload["duration_ms"], (int, float)) or not math.isfinite(payload["duration_ms"]) or payload["duration_ms"] < 0):
        raise ValueError("eval receipt duration invalid")
    recorded = payload.get("artifact")
    artifact_keys = {"name", "size_bytes", "sha256", "source_revision", "llama_cpp_revision", "modality", "quantization"}
    if (not isinstance(recorded, dict) or set(recorded) != artifact_keys or
            recorded.get("name") != artifact["name"] or recorded.get("size_bytes") != artifact["size_bytes"] or recorded.get("sha256") != artifact["sha256"]):
        raise ValueError("eval receipt artifact mismatch")
    for key in ("source_revision", "llama_cpp_revision", "modality", "quantization"):
        if key in artifact and recorded.get(key) != artifact[key]:
            raise ValueError("eval receipt artifact identity mismatch")
    model_preflight = payload.get("model_preflight")
    if (not isinstance(model_preflight, dict) or set(model_preflight) != {"valid", "code", "status", "size_bytes", "sha256", "gguf_version"} or
            model_preflight.get("valid") is not True or model_preflight.get("code") != "ok" or
            model_preflight.get("status") != "verified" or model_preflight.get("size_bytes") != artifact["size_bytes"] or
            model_preflight.get("sha256") != artifact["sha256"] or model_preflight.get("gguf_version") != 3):
        raise ValueError("eval receipt model preflight invalid")
    metrics = payload.get("metrics")
    if (not isinstance(metrics, dict) or set(metrics) - {"case_count", "passed", "failed", "errors", "peak_rss_kib", "category_summary"} or
            payload.get("status") not in {"verified", "completed_with_failures"} or
            any(isinstance(metrics.get(key), bool) or not isinstance(metrics.get(key), int) or metrics[key] < 0 for key in ("case_count", "passed", "failed", "errors"))):
        raise ValueError("eval receipt metrics invalid")
    summary = metrics.get("category_summary")
    expected_count, expected_categories = _tool_eval_contract()
    if metrics["case_count"] != expected_count or not isinstance(summary, dict) or set(summary) != expected_categories:
        raise ValueError("eval receipt metrics invalid")
    category_total = 0
    for category in expected_categories:
        item = summary[category]
        if not isinstance(item, dict) or set(item) != {"case_count", "passed", "failed", "errors"}:
            raise ValueError("eval receipt category summary invalid")
        if any(isinstance(item.get(key), bool) or not isinstance(item.get(key), int) or item[key] < 0 for key in ("case_count", "passed", "failed", "errors")):
            raise ValueError("eval receipt category summary invalid")
        if item["passed"] + item["failed"] + item["errors"] != item["case_count"]:
            raise ValueError("eval receipt category summary invalid")
        category_total += item["case_count"]
    if category_total != expected_count:
        raise ValueError("eval receipt category summary total invalid")
    if metrics["case_count"] != expected_count or sum(metrics[key] for key in ("passed", "failed", "errors")) != expected_count:
        raise ValueError("eval receipt metric totals invalid")
    status = payload["status"]
    all_passed = metrics["passed"] == expected_count and metrics["failed"] == 0 and metrics["errors"] == 0
    has_failure = metrics["failed"] > 0 or metrics["errors"] > 0
    if (status == "verified") != all_passed or (status == "completed_with_failures") != has_failure:
        raise ValueError("eval receipt status does not match metrics")
    engine = payload.get("engine")
    if (not isinstance(engine, dict) or set(engine) != {"engine_version", "api_version", "compiled_backend", "llama_cpp_revision", "model"} or
            engine.get("llama_cpp_revision") != artifact.get("llama_cpp_revision") or engine.get("compiled_backend") != f"llama.cpp/{artifact.get('llama_cpp_revision', '')[:8]}/cuda"):
        raise ValueError("eval receipt engine identity mismatch")
    if engine.get("engine_version") != "0.1.0" or engine.get("api_version") != "0.1.0" or engine.get("model") != "qwen35-9b-q4-k-m":
        raise ValueError("eval receipt engine identity mismatch")
    for key in ("engine_version", "api_version", "model"):
        if key in engine and (not isinstance(engine[key], str) or not 1 <= len(engine[key]) <= 256 or any(ord(char) < 0x20 for char in engine[key])):
            raise ValueError("eval receipt engine identity mismatch")
    cuda_device = payload.get("cuda_device")
    device = cuda_device.get("device") if isinstance(cuda_device, dict) else None
    if (not isinstance(cuda_device, dict) or set(cuda_device) != {"schema", "status", "selector", "device_count", "device", "source"} or
            cuda_device.get("schema") != "local_bmo.j1m.cuda-device-receipt.v1" or cuda_device.get("status") != "verified" or cuda_device.get("selector") != "CUDA0" or cuda_device.get("device_count") != 1 or
            not isinstance(device, dict) or set(device) != {"index", "name", "memory_total_mib", "driver_version"} or "a100" not in str(device.get("name", "")).lower() or not isinstance(device.get("memory_total_mib"), int) or device["memory_total_mib"] < 70000):
        raise ValueError("eval receipt CUDA placement attestation invalid")
    if cuda_device.get("source") != "nvidia-smi bounded query":
        raise ValueError("eval receipt CUDA placement attestation invalid")
    if (not isinstance(device.get("index", 0), int) or isinstance(device.get("index", 0), bool) or device.get("index", 0) < 0 or
            not isinstance(device.get("name"), str) or not 1 <= len(device["name"]) <= 160 or
            ("driver_version" in device and (not isinstance(device["driver_version"], str) or not 1 <= len(device["driver_version"]) <= 80)) or
            ("source" in cuda_device and (not isinstance(cuda_device["source"], str) or len(cuda_device["source"]) > 160))):
        raise ValueError("eval receipt CUDA placement attestation invalid")
    toolchain = payload.get("toolchain")
    versions = toolchain.get("versions") if isinstance(toolchain, dict) else None
    minimums = {"python3": (3, 8), "git": (2, 30), "cmake": (3, 18), "g++": (9, 0), "nvcc": (12, 0)}
    if (not isinstance(toolchain, dict) or set(toolchain) != {"schema", "status", "required", "versions", "packages", "package_install"} or
            toolchain.get("schema") != "local_bmo.j1m.remote-toolchain-receipt.v1" or toolchain.get("status") != "verified" or
            not isinstance(versions, dict) or set(versions) != set(minimums)):
        raise ValueError("eval receipt toolchain evidence invalid")
    expected_required = {"python3": ">=3.8", "git": ">=2.30", "cmake": ">=3.18", "g++": ">=9.0", "nvcc": ">=12.0"}
    if toolchain.get("required") != expected_required or toolchain.get("package_install") != "ubuntu apt repositories; exact resolved package versions captured by dpkg-query":
        raise ValueError("eval receipt toolchain evidence invalid")
    for name, minimum in minimums.items():
        version = versions.get(name)
        if (not isinstance(version, dict) or set(version) != {"major", "minor", "reported", "executable"} or
                isinstance(version.get("major"), bool) or not isinstance(version.get("major"), int) or isinstance(version.get("minor"), bool) or not isinstance(version.get("minor"), int) or (version["major"], version["minor"]) < minimum):
                raise ValueError("eval receipt toolchain version invalid")
        for key in ("reported", "executable"):
            if key in version and (not isinstance(version[key], str) or not 1 <= len(version[key]) <= 256 or any(ord(char) < 0x20 for char in version[key])):
                raise ValueError("eval receipt toolchain version invalid")
    if versions["nvcc"].get("executable") != "/usr/local/cuda/bin/nvcc":
        raise ValueError("eval receipt CUDA compiler path invalid")
    packages = toolchain.get("packages")
    expected_packages = {"ca-certificates", "cmake", "build-essential", "git", "python3", "python3-venv"}
    if not isinstance(packages, dict) or set(packages) != expected_packages or any(not isinstance(value, str) or not value or len(value) > 160 for value in packages.values()):
        raise ValueError("eval receipt package evidence invalid")
    peak_rss = metrics.get("peak_rss_kib")
    if peak_rss is not None and (isinstance(peak_rss, bool) or not isinstance(peak_rss, int) or peak_rss < 0):
        raise ValueError("eval receipt RSS metric invalid")
    if payload.get("prompt_response_logging") is not False or payload.get("token_logging") is not False:
        raise ValueError("eval receipt logging policy missing")
    selected_metrics = {key: metrics[key] for key in ("case_count", "passed", "failed", "errors", "peak_rss_kib")}
    if summary is not None:
        selected_metrics["category_summary"] = summary
    selected_versions = {name: dict(versions[name]) for name in minimums}
    selected_packages = {name: packages[name] for name in expected_packages}
    return {
        "status": status,
        "artifact": dict(recorded),
        "engine": dict(engine),
        "model_preflight": dict(model_preflight),
        "cuda_device": {**cuda_device, "device": dict(device)},
        "metrics": selected_metrics,
        "toolchain": {"schema": toolchain["schema"], "status": toolchain["status"], "required": dict(toolchain["required"]), "versions": selected_versions, "packages": selected_packages, "package_install": toolchain["package_install"]},
    }


_STARTUP_PREFLIGHT_VALIDATOR_CODES = frozenset({
    "ok", "model_path_not_absolute", "model_path_unsafe", "model_symlink_forbidden", "model_not_regular_file",
    "model_path_invalid", "model_filename_mismatch", "model_mmproj_forbidden", "model_stat_failed", "model_lock_failed",
    "model_open_failed", "model_changed_during_validation", "model_read_failed", "model_size_mismatch", "model_hash_mismatch",
    "model_architecture_mismatch", "model_architecture_profile_mismatch", "model_chat_template_mismatch", "model_metadata_profile_mismatch",
    "model_quantization_profile_mismatch", "model_tensor_profile_mismatch", "model_tokenizer_profile_mismatch", "gguf_alignment_invalid",
    "gguf_chat_template_invalid", "gguf_count_invalid", "gguf_count_overflow", "gguf_magic_invalid", "gguf_metadata_array_invalid",
    "gguf_metadata_key_invalid", "gguf_metadata_too_large", "gguf_metadata_type_invalid", "gguf_metadata_type_unsupported",
    "gguf_string_invalid", "gguf_tensor_data_size_mismatch", "gguf_tensor_name_invalid", "gguf_tensor_offset_invalid",
    "gguf_tensor_out_of_bounds", "gguf_tensor_overlap", "gguf_tensor_shape_invalid", "gguf_tensor_size_overflow",
    "gguf_tensor_type_unsupported", "gguf_truncated", "gguf_version_unsupported",
})


def _verify_startup_preflight_receipt(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Validate the separately salvaged startup identity outcome."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("startup preflight receipt duplicate key")
            result[key] = value
        return result

    payload = _bounded_json(path, _PREFLIGHT_RECEIPT_MAX_BYTES)
    if not isinstance(payload, dict) or payload.get("schema") != "local_bmo.j1m.startup-preflight-receipt.v1":
        raise ValueError("startup preflight receipt schema invalid")
    status = payload.get("status")
    if status == "verified":
        if (set(payload) != {"schema", "status", "size_bytes", "sha256", "gguf_version"} or
                payload.get("size_bytes") != artifact["size_bytes"] or payload.get("sha256") != artifact["sha256"] or
                payload.get("gguf_version") != 3):
            raise ValueError("startup preflight receipt identity invalid")
        return {"status": "verified", "size_bytes": payload["size_bytes"], "sha256": payload["sha256"], "gguf_version": 3}
    elif status == "not_started":
        if set(payload) != {"schema", "status", "error_code"} or payload.get("error_code") != "engine_model_preflight_not_started":
            raise ValueError("startup preflight receipt not_started outcome invalid")
        return {"status": "not_started", "error_code": payload["error_code"]}
    elif status in {"rejected", "timeout", "oversize", "terminated", "failed"}:
        if not set(payload) <= {"schema", "status", "error_code", "validator_code", "child"}:
            raise ValueError("startup preflight receipt outcome invalid")
        error_code = payload.get("error_code")
        allowed_errors = {
            "engine_model_preflight_failed", "engine_model_preflight_invalid", "engine_model_preflight_exit",
            "engine_model_preflight_output_too_large", "engine_model_preflight_timeout", "engine_model_preflight_terminated_by_signal",
        }
        if not isinstance(error_code, str) or error_code not in allowed_errors:
            raise ValueError("startup preflight receipt outcome invalid")
        if status == "timeout" and error_code != "engine_model_preflight_timeout":
            raise ValueError("startup preflight receipt outcome invalid")
        if status == "oversize" and error_code != "engine_model_preflight_output_too_large":
            raise ValueError("startup preflight receipt outcome invalid")
        if status == "terminated" and error_code != "engine_model_preflight_terminated_by_signal":
            raise ValueError("startup preflight receipt outcome invalid")
        if "validator_code" in payload and (not isinstance(payload["validator_code"], str) or payload["validator_code"] not in _STARTUP_PREFLIGHT_VALIDATOR_CODES or status != "rejected"):
            raise ValueError("startup preflight receipt outcome invalid")
        child = payload.get("child")
        if child is not None and (not isinstance(child, dict) or set(child) not in ({"exit_code"}, {"signal"}) or
                                  not isinstance(next(iter(child.values())), int) or isinstance(next(iter(child.values())), bool) or
                                  not 0 <= next(iter(child.values())) <= 255 or
                                  ("signal" in child and child["signal"] < 1)):
            raise ValueError("startup preflight receipt child invalid")
        if status == "terminated" and (not isinstance(child, dict) or "signal" not in child):
            raise ValueError("startup preflight receipt child invalid")
        if status in {"rejected", "failed"} and not isinstance(child, dict):
            raise ValueError("startup preflight receipt child invalid")
        if isinstance(child, dict) and "signal" in child and status != "terminated":
            raise ValueError("startup preflight receipt child invalid")
        if status == "failed" and error_code != "engine_model_preflight_failed":
            raise ValueError("startup preflight receipt outcome invalid")
        if status == "rejected" and error_code == "engine_model_preflight_failed":
            raise ValueError("startup preflight receipt outcome invalid")
    else:
        raise ValueError("startup preflight receipt outcome invalid")
    selected = {"status": status, "error_code": error_code}
    if "validator_code" in payload:
        selected["validator_code"] = payload["validator_code"]
    if isinstance(child, dict):
        selected["child"] = dict(child)
    return selected


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
        if deadline is not None and deadline - time.monotonic() - _DELETION_RESERVE_SECONDS <= 0.0:
            results.append({"name": name, "status": "salvage_failed", "error_code": "salvage_deadline_reserve"})
            continue
        try:
            sf._preflight(info["phase_id"])
            command = sf.scp_base(info["instance_info"], identity, known_hosts) + [
                f"{info['instance_info']['ssh_user']}@{info['instance_info']['ip']}:/scratch/j1m/artifacts/{name}", str(destination / name),
            ]
            # Receipts are small, but the sole deployable Q4 artifact is not.
            # Give its transfer a size-aware floor while still honoring the
            # provider deadline and retaining a cleanup reserve.
            timeout = max(120.0, float(q4_expected_gib) * 60.0) if name.endswith("Q4_K_M.gguf") else 30.0
            if deadline is not None:
                remaining = deadline - time.monotonic() - _DELETION_RESERVE_SECONDS
                if remaining <= 0.0:
                    results.append({"name": name, "status": "salvage_failed", "error_code": "salvage_deadline_reserve"})
                    continue
                timeout = min(timeout, remaining)
            receipt = _remote(command, timeout=timeout)
        except Exception as exc:
            receipt = {"status": "salvage_failed", "error_type": type(exc).__name__}
        receipt["name"] = name
        results.append(receipt)
    return results


def execute(env_file: Path, *, config_path: Path, phase_id: str, run_id: str, artifact_destination: Path, mode: str = "prove", model_artifact: Path | None = None, model_manifest: Path | None = None) -> dict[str, Any]:
    config = j1m_runner.load_config(config_path)
    if mode == "eval":
        # The evaluation lane is remote-only: the host downloads the pinned
        # public HF source and deterministically rebuilds Q4. A local artifact
        # is refused so this path cannot consume laptop disk or bandwidth.
        if model_artifact is not None:
            raise ValueError("eval is remote-only; --model-artifact is refused to protect local disk")
        model_manifest = model_manifest or (
            ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json"
        )
        eval_artifact = _verify_eval_artifact(None, model_manifest, config)
        _eval_deadline_ceiling(config)
        # Eval is explicitly CUDA-only on the approved A100. A CPU binary or
        # missing CUDA placement receipt is rejected by the remote verifier.
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
        attempt_id: str | None = None
        cleanup_failure: BaseException | None = None
        def stop_watchdog() -> None:
            nonlocal watchdog
            if watchdog is None or watchdog.poll() is not None:
                return
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

        def deletion_confirmed() -> bool:
            """Return true only for the provider's explicit success receipt."""
            deletion = lifecycle.get("deletion")
            if not isinstance(deletion, dict):
                return False
            if deletion.get("status") == "already-cleaned":
                return deletion.get("retry_required") is not True
            nested = deletion.get("deletion")
            if isinstance(nested, dict):
                return nested.get("success") is True and deletion.get("retry_required") is not True
            return deletion.get("success") is True and deletion.get("retry_required") is not True
        try:
            # Reserve the possible provider POST before uploading the key or
            # creating an instance. Keeping this in the protected region means
            # a ledger/filesystem failure still restores signals and persists
            # a terminal lifecycle marker without making a provider call.
            attempt_id = sf.reserve_create_attempt(phase_id, nonce, candidate, backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]), public_key_sha256=hashlib.sha256(public_key.encode("utf-8")).hexdigest())
            attempt_reserved = True
            try:
                key_id = sf.add_ssh_key(api_key, phase_id, f"j1m-{nonce}", public_key)
            except sf.AmbiguousProviderOutcome as exc:
                # A timed-out or malformed key-create response may have
                # succeeded remotely. Reconcile only the nonce-bound name and
                # exact public-key fingerprint; unresolved ambiguity blocks
                # instance creation and is retained as a finite incident.
                lifecycle["ssh_key_create_ambiguous"] = True
                try:
                    key_id = sf.reconcile_ssh_key(
                        api_key, phase_id, expected_name=f"j1m-{nonce}",
                        expected_public_key=public_key,
                    )
                    lifecycle["ssh_key_reconciliation"] = {"status": "exact_match"}
                except Exception as reconcile_exc:
                    lifecycle["ssh_key_reconciliation"] = {
                        "status": "unresolved",
                        "retry_required": True,
                        "error_type": type(reconcile_exc).__name__,
                    }
                    try:
                        sf.append_incident({
                            "phase_id": phase_id,
                            "incident": "ssh-key-create-ambiguous-unresolved",
                            "nonce": nonce,
                            "ssh_key_name": f"j1m-{nonce}",
                            "ssh_public_key_sha256": hashlib.sha256(public_key.encode("utf-8")).hexdigest(),
                            "error_type": type(reconcile_exc).__name__,
                        })
                    except Exception:
                        pass
                    raise sf.AmbiguousProviderOutcome("SSH key create could not be safely reconciled") from exc
                # Even an exact reconciliation is revoked by the normal exact
                # key-cleanup branch; this attempt must never proceed to create.
                raise sf.AmbiguousProviderOutcome("SSH key create was reconciled; instance creation refused") from exc
            sf.verify_ssh_key_ownership(api_key, phase_id, key_id, expected_name=f"j1m-{nonce}", expected_public_key=public_key)
            # Bind the exact provider key ID into the already-pending attempt
            # reservation before allowing the instance create POST.
            sf.reserve_create_attempt(phase_id, nonce, candidate, backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]), public_key_sha256=hashlib.sha256(public_key.encode("utf-8")).hexdigest(), ssh_key_id=key_id)
            try:
                instance_id = sf.create_instance(api_key, env, phase_id=phase_id, run_id=run_id, candidate=candidate, ssh_key_id=key_id, nonce=nonce, max_runtime_hours=runtime)
                created_monotonic = time.monotonic()
                provider_deadline = created_monotonic + float(config["modes"][mode]["provider_backstop_hours"]) * 3600
                run_deadline = created_monotonic + runtime * 3600
                watchdog_deadline = created_monotonic + float(config["modes"][mode]["external_watchdog_seconds"])
                execution_deadline = min(provider_deadline, run_deadline, watchdog_deadline)
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
            activation_seconds = int(config["modes"][mode].get("activation_timeout_seconds", 1800))
            record = sf.OwnedResource(phase_id=phase_id, run_id=run_id, instance_id=instance_id, ownership_nonce=nonce, ssh_key_id=key_id, ssh_key_name=f"j1m-{nonce}", gpu=candidate.gpu, cloud=candidate.cloud, region=candidate.region, hourly_usd=candidate.hourly_usd, created_at_utc=sf.utc_now().isoformat(), active_deadline_utc=(sf.utc_now() + sf.timedelta(seconds=activation_seconds)).isoformat(), run_deadline_utc=(sf.utc_now() + sf.timedelta(hours=runtime)).isoformat(), instance_type=candidate.instance_type, launcher_pid=launcher_pid, launcher_start_marker=launcher_start_marker)
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
            watchdog_started = time.monotonic()
            watchdog = subprocess.Popen(watchdog_command)
            watchdog_deadline = watchdog_started + float(config["modes"][mode]["external_watchdog_seconds"])
            execution_deadline = min(provider_deadline, run_deadline, watchdog_deadline)
            lifecycle["watchdog_pid"] = watchdog.pid
            sf.append_cost_event({"instance_id": instance_id, "phase_id": phase_id, "status": "pending", "estimated_cost_usd": round(candidate.hourly_usd * float(config["modes"][mode]["provider_backstop_hours"]), 6)})
            # The instance reservation is now superseded by its exact
            # ownership/billing row. Keep the pre-create reservation history
            # but settle it to zero only after both durable writes and the
            # watchdog are in place.
            sf.append_cost_event({"instance_id": attempt_id, "phase_id": phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
            j1m_runner.write_progress(progress_path, "wait-active-starting", phase_id=phase_id)
            wait_budget = int(_eval_timeout(execution_deadline, float(config["modes"][mode].get("activation_timeout_seconds", 1800))))
            info = sf.wait_active(api_key, phase_id, instance_id, timeout_seconds=wait_budget)
            lifecycle["instance_info"] = info
            sf.verify_instance_ownership(info, instance_id=instance_id, phase_id=phase_id, nonce=nonce, expected_name=sf.owned_instance_name(run_id, nonce), ssh_key_id=key_id, expected_cloud=candidate.cloud, expected_region=candidate.region, expected_instance_type=candidate.instance_type, expected_hourly_usd=candidate.hourly_usd, expected_gpu=candidate.gpu, expected_gpu_count=1, expected_vram_gb=candidate.vram_gb, expected_os_image=candidate.os_image)
            ssh_user = sf.validate_ssh_user(info["ssh_user"])
            lifecycle["host_key"] = sf.acquire_pinned_host_key(info, known_hosts)
            lifecycle["status"] = "active"
            lifecycle["stage"] = "active"
            remote_root = "/scratch/j1m"
            workspace_stages = (
                ("scratch_root", ["sudo", "mkdir", "-p", "/scratch"]),
                ("scratch_owner", ["sudo", "chown", ssh_user, "/scratch"]),
                ("remote_workspace", ["mkdir", "-p", remote_root]),
                ("scratch_df", ["df", "-P", "-k", "/scratch"]),
                ("scratch_writable", ["test", "-w", "/scratch"]),
            )
            for stage_name, stage_argv in workspace_stages:
                result = _remote(sf.ssh_base(info, identity, known_hosts) + stage_argv, timeout=_eval_timeout(execution_deadline, 30.0))
                lifecycle[stage_name] = result
                if result["status"] != "completed":
                    raise sf.ShadeformError(f"remote {stage_name} preflight failed")
            shutdown_minutes = str(config["modes"][mode]["host_shutdown_delay_minutes"])
            lifecycle["host_shutdown_backstop"] = _remote(sf.ssh_base(info, identity, known_hosts) + ["sudo", "shutdown", "-h", f"+{shutdown_minutes}"], timeout=_eval_timeout(execution_deadline, 30.0))
            if lifecycle["host_shutdown_backstop"]["status"] != "completed":
                raise sf.ShadeformError("host shutdown backstop could not be armed")
            upload = sf.scp_base(info, identity, known_hosts) + [str(config_path), f"{ssh_user}@{info['ip']}:{remote_root}/j1m-config.json"]
            lifecycle["upload"] = _remote(upload, timeout=_eval_timeout(execution_deadline, 120.0))
            source_lock = ROOT / config["source"]["lock"]
            for local, remote in ((ROOT / "scripts" / "j1m_runner.py", f"{remote_root}/j1m_runner.py"), (source_lock, f"{remote_root}/qwen35-9b.source-lock.json")):
                upload_receipt = _remote(sf.scp_base(info, identity, known_hosts) + [str(local), f"{ssh_user}@{info['ip']}:{remote}"], timeout=_eval_timeout(execution_deadline, 120.0))
                lifecycle.setdefault("uploads", []).append(upload_receipt)
                if upload_receipt["status"] != "completed":
                    raise sf.ShadeformError("required J1M upload failed")
            if mode == "prove":
                lifecycle["job"] = _remote(
                    sf.ssh_base(info, identity, known_hosts)
                    + _remote_job_command(mode, remote_root, int(config["resources"]["required_scratch_gib"])),
                    timeout=_eval_timeout(execution_deadline, 120.0),
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
                lifecycle["job"] = _remote(remote_job, timeout=_eval_timeout(execution_deadline, float("inf"), reserve=transfer_reserve + 120.0))
            else:
                eval_commands = _eval_remote_commands(config, remote_root)
                _progress(progress_path, "eval-bootstrap-starting", phase_id=phase_id)
                # Install and upload the bounded bootstrap before any source
                # work. The remaining stages consume those uploads and copy
                # the lock into the now-existing pinned vendor tree.
                bootstrap_timeouts = (30.0, 120.0, 270.0)
                for index, command in enumerate(eval_commands[:3]):
                    lifecycle["stage"] = f"eval-bootstrap:{command[0]}"
                    _progress(progress_path, "eval-bootstrap-stage-starting", phase_id=phase_id, operation_stage=lifecycle["stage"])
                    stage = _remote(sf.ssh_base(info, identity, known_hosts) + command, timeout=_eval_timeout(execution_deadline, bootstrap_timeouts[index]))
                    lifecycle.setdefault("eval_stages", []).append(stage)
                    _progress(progress_path, "eval-bootstrap-stage-result", phase_id=phase_id, operation_stage=lifecycle["stage"], status=stage["status"], exit_code=stage.get("exit_code"))
                    if stage["status"] != "completed":
                        raise sf.ShadeformError("eval source preparation failed")
                eval_uploads = _eval_uploads(config, remote_root, model_artifact, model_manifest)
                small_upload_divisor = len(eval_uploads) - 1 if model_artifact is not None else len(eval_uploads)
                small_upload_timeout = float(config["modes"]["eval"]["stage_budgets_seconds"]["small_uploads"]) / max(1, small_upload_divisor)
                for local, remote, recursive in eval_uploads:
                    lifecycle["stage"] = f"eval-upload:{local.name}"
                    _progress(progress_path, "eval-upload-starting", phase_id=phase_id, operation_stage=lifecycle["stage"])
                    scp = sf.scp_base(info, identity, known_hosts)
                    if recursive:
                        scp = [scp[0], "-r", *scp[1:]]
                    destination = f"{ssh_user}@{info['ip']}:{remote}"
                    requested_timeout = float(config["modes"]["eval"]["stage_budgets_seconds"]["model_upload"]) if local == model_artifact else small_upload_timeout
                    timeout = _eval_timeout(execution_deadline, requested_timeout)
                    upload_receipt = _remote(scp + [str(local), destination], timeout=timeout)
                    lifecycle.setdefault("eval_uploads", []).append({"name": local.name, **upload_receipt})
                    _progress(progress_path, "eval-upload-result", phase_id=phase_id, operation_stage=lifecycle["stage"], status=upload_receipt["status"], exit_code=upload_receipt.get("exit_code"))
                    if upload_receipt["status"] != "completed":
                        raise sf.ShadeformError("required eval upload failed")
                for command in eval_commands[3:]:
                    lifecycle["stage"] = _eval_stage_label(command)
                    if any("remote_model_eval.py" in part for part in command):
                        lifecycle["remote_model_eval_attempted"] = True
                    _progress(progress_path, "eval-stage-starting", phase_id=phase_id, operation_stage=lifecycle["stage"])
                    stage = _remote(sf.ssh_base(info, identity, known_hosts) + command, timeout=_eval_timeout(execution_deadline, _eval_stage_timeout(config, command)))
                    lifecycle.setdefault("eval_stages", []).append(stage)
                    _progress(progress_path, "eval-stage-result", phase_id=phase_id, operation_stage=lifecycle["stage"], status=stage["status"], exit_code=stage.get("exit_code"))
                    if stage["status"] != "completed":
                        raise sf.ShadeformError("eval build or evaluation failed")
                lifecycle["job"] = lifecycle["eval_stages"][-1]
            if lifecycle["job"]["status"] != "completed":
                lifecycle["status"] = lifecycle["job"]["status"]
                raise sf.ShadeformError("J1M remote job did not complete")
            lifecycle["status"] = "completed"
            _progress(progress_path, "completed", phase_id=phase_id, mode=mode)
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
            lifecycle["failed_stage"] = lifecycle.get("stage", "unknown")
            _progress(progress_path, "failed", phase_id=phase_id, failed_stage=lifecycle["failed_stage"], error_type=type(exc).__name__)
            raise
        finally:
            try:
                _persist_lifecycle(phase_id, lifecycle)
            except Exception:
                pass
            _progress(progress_path, "teardown-salvage-starting", phase_id=phase_id, failed_stage=lifecycle.get("failed_stage"))
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
                    deadline=execution_deadline if "execution_deadline" in locals() else None,
                    q4_expected_gib=float(config["resources"].get("expected_q4_gib", 6.0)),
                ) if lifecycle.get("instance_info") else []
            except Exception as exc:
                # Even an unexpected salvage/setup failure must leave the
                # exact deletion and key cleanup paths reachable.
                lifecycle["salvage"] = [{"status": "salvage_failed", "error_type": type(exc).__name__}]
            if mode == "prove" and lifecycle.get("job", {}).get("status") == "completed" and not any(item.get("name") == "proving-receipt.json" and item.get("status") == "completed" for item in lifecycle["salvage"]):
                lifecycle["receipt_error"] = "proving receipt was not salvaged before teardown"
            if mode == "eval":
                preflight_saved = next((item for item in lifecycle["salvage"] if item.get("name") == "startup-preflight-receipt.json" and item.get("status") == "completed"), None)
                if lifecycle.get("remote_model_eval_attempted"):
                    if preflight_saved is None:
                        lifecycle["receipt_error"] = "startup preflight receipt was not salvaged"
                    else:
                        try:
                            lifecycle["preflight_receipt"] = _verify_startup_preflight_receipt(artifact_destination / "startup-preflight-receipt.json", eval_artifact)
                        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                            lifecycle["receipt_error"] = type(exc).__name__
                    if lifecycle.get("preflight_receipt", {}).get("status") != "verified":
                        lifecycle["receipt_error"] = lifecycle.get("receipt_error", "startup preflight did not verify")
                saved_receipt = next((item for item in lifecycle["salvage"] if item.get("name") == "eval-receipt.json" and item.get("status") == "completed"), None)
                if saved_receipt is None:
                    lifecycle["receipt_error"] = "eval receipt was not salvaged before teardown"
                else:
                    try:
                        lifecycle["eval_receipt"] = _verify_eval_receipt(artifact_destination / "eval-receipt.json", eval_artifact)
                    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        lifecycle["receipt_error"] = type(exc).__name__
                if lifecycle.get("receipt_error") or lifecycle.get("eval_receipt", {}).get("status") != "verified":
                    lifecycle["status"] = "failed"
            # The shared teardown performs exact deletion before cost/key
            # bookkeeping and emits a receipt, while remote salvage above is
            # best-effort and independent for each allowlisted artifact.
            if instance_id is not None:
                if recorded:
                    # teardown_exact raises or returns an unsuccessful receipt
                    # when ownership/deletion is not confirmed. In either
                    # case retain the external watchdog so it can continue
                    # the exact cleanup retry; stopping it here would orphan
                    # the provider resource during the exception path.
                    try:
                        lifecycle["deletion"] = teardown_exact(
                            phase_id, instance_id, env_file=env_file,
                            deadline=(execution_deadline + _DELETION_RESERVE_SECONDS)
                            if "execution_deadline" in locals() else None,
                        )
                    except Exception as exc:
                        cleanup_failure = RuntimeError("exact instance teardown was not confirmed")
                        lifecycle["deletion"] = {
                            "status": "delete-failed",
                            "retry_required": True,
                            "error_type": type(exc).__name__,
                        }
                        lifecycle["status"] = "failed"
                        try:
                            sf.append_incident({
                                "phase_id": phase_id,
                                "incident": "post-instance-teardown-unconfirmed",
                                "instance_id": instance_id,
                                "ssh_key_id": key_id,
                                "nonce": nonce,
                                "error_type": type(exc).__name__,
                            })
                        except Exception:
                            pass
                else:
                    # Ledger write failed: exact ID is still known, so delete
                    # it before attempting any key/bookkeeping cleanup.
                    try:
                        lifecycle["deletion"] = sf._delete_instance(
                            api_key, phase_id, instance_id,
                            deadline=(execution_deadline + _DELETION_RESERVE_SECONDS)
                            if "execution_deadline" in locals() else None,
                        )
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
                    if not deletion_confirmed():
                        cleanup_failure = RuntimeError("exact instance deletion was not confirmed")
                        lifecycle["status"] = "failed"
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
            # Keep the watchdog alive through salvage and exact instance
            # deletion. It is stopped only after the provider explicitly
            # confirms deletion; an unconfirmed/raised cleanup leaves it
            # running for its own exact retry path.
            if deletion_confirmed():
                stop_watchdog()
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
            _progress(progress_path, "completed" if lifecycle.get("status") == "completed" else "failed", phase_id=phase_id, failed_stage=lifecycle.get("failed_stage"), teardown="finished")
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
        if cleanup_failure is not None:
            raise cleanup_failure
        return lifecycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, default=j1m_runner.DEFAULT_CONFIG)
    parser.add_argument("--phase-id", default="j1m-proving-run")
    parser.add_argument("--run-id", default="J1M")
    parser.add_argument("--artifact-destination", type=Path, default=ROOT / "artifacts" / "qwen35-9b")
    parser.add_argument("--mode", choices=("prove", "build", "eval"), default="prove")
    parser.add_argument("--model-artifact", type=Path, help="refused for eval; the Q4 artifact is always built remotely")
    parser.add_argument("--model-manifest", type=Path, help="approved manifest; defaults to the checked-in Q4 acceptance manifest")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    config = j1m_runner.load_config(args.config)
    plan = j1m_runner.build_plan(config, args.mode)
    plan["mode"] = args.mode
    plan["mode_runtime_hours"] = config["modes"][args.mode]["runtime_hours"]
    plan["mode_active_cost_usd"] = config["modes"][args.mode]["active_cost_usd"]
    if args.mode == "eval":
        plan["commands"] = _eval_remote_commands(config, "/scratch/j1m")
        plan["artifact"] = "remote HF download/conversion/quantization; local Q4 is never required or copied"
        plan["quality_only"] = True
        plan["execution_backend"] = "cuda"
        plan["cuda_architecture"] = 80
        plan["gpu_layers"] = 99
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
