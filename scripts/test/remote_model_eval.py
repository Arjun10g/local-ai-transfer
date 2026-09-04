#!/usr/bin/env python3
"""Run the bounded real Qwen tool evaluator on an already-prepared host.

This helper is intentionally provider-agnostic.  The lifecycle uploads it
with the exact Q4 artifact and repository sources, and invokes it only after
the exact instance has passed ownership checks.  It requires the native
engine's protected ``--token-file`` interface; it never puts a bearer token in
an argv vector or an environment inherited by a child.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
import sys
import time
import re
import selectors
import tempfile
from pathlib import Path
from typing import Any


MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
PIN_RE = set("0123456789abcdef")
SAFE_ERROR_CODES = frozenset({
    "artifact_manifest_invalid", "cuda_device_receipt_invalid", "engine_binary_missing",
    "engine_build_info_failed", "engine_build_info_invalid", "engine_llama_identity_mismatch",
    "engine_model_preflight_failed", "engine_model_preflight_invalid", "engine_not_ready",
    "engine_model_preflight_exit", "engine_model_preflight_output_too_large",
    "engine_model_preflight_timeout", "engine_model_preflight_terminated_by_signal",
    "engine_startup_failed",
    "engine_ready_eof", "engine_ready_identity_invalid", "engine_ready_receipt_invalid",
    "engine_ready_timeout", "engine_stdout_unavailable", "engine_token_file_not_private",
    "engine_terminated_by_signal",
    "eval_timeout_invalid", "evaluation_backend_invalid", "evaluator_category_summary_invalid",
    "evaluator_category_summary_total_invalid", "evaluator_expected_count_invalid",
    "evaluator_fixture_categories_invalid", "evaluator_fixture_count_invalid",
    "evaluator_fixture_invalid", "evaluator_fixture_too_large", "evaluator_fixture_unreadable",
    "evaluator_metrics_invalid", "evaluator_metrics_total_invalid", "evaluator_process_failed",
    "evaluator_exit_status_mismatch", "evaluator_receipt_invalid", "evaluator_rss_invalid",
    "llama_checkout_revision_mismatch",
    "llama_pin_invalid", "llama_revision_mismatch", "model_manifest_invalid",
    "model_manifest_lock_invalid", "model_manifest_lock_mismatch", "q4_artifact_hash_mismatch",
    "q4_artifact_missing_or_wrong_name", "source_pin_invalid", "source_revision_mismatch",
    "toolchain_receipt_invalid",
})
TAIL_LIMIT = 1200
MAX_ENGINE_LINE = 8192
MAX_EVAL_OUTPUT = 256 * 1024
FIXTURE_MAX_BYTES = 256 * 1024
MAX_EVAL_CASES = 40
EVAL_TOTAL_TIMEOUT = 480.0
CLEANUP_RESERVE_SECONDS = 30.0
MODEL_PREFLIGHT_RECEIPT_SCHEMA = "local_bmo.j1m.startup-preflight-receipt.v1"

# Finite native validator vocabulary. Never copy an arbitrary native error
# string into a remote receipt.
MODEL_VALIDATOR_CODES = frozenset({
    "ok", "model_path_not_absolute", "model_path_unsafe", "model_symlink_forbidden",
    "model_not_regular_file", "model_path_invalid", "model_filename_mismatch",
    "model_mmproj_forbidden", "model_stat_failed", "model_lock_failed",
    "model_open_failed", "model_changed_during_validation", "model_read_failed",
    "model_size_mismatch", "model_hash_mismatch", "model_architecture_mismatch",
    "model_architecture_profile_mismatch", "model_chat_template_mismatch",
    "model_metadata_profile_mismatch", "model_quantization_profile_mismatch",
    "model_tensor_profile_mismatch", "model_tokenizer_profile_mismatch",
    "gguf_alignment_invalid", "gguf_chat_template_invalid", "gguf_count_invalid",
    "gguf_count_overflow", "gguf_magic_invalid", "gguf_metadata_array_invalid",
    "gguf_metadata_key_invalid", "gguf_metadata_too_large", "gguf_metadata_type_invalid",
    "gguf_metadata_type_unsupported", "gguf_string_invalid", "gguf_tensor_data_size_mismatch",
    "gguf_tensor_name_invalid", "gguf_tensor_offset_invalid", "gguf_tensor_out_of_bounds",
    "gguf_tensor_overlap", "gguf_tensor_shape_invalid", "gguf_tensor_size_overflow",
    "gguf_tensor_type_unsupported", "gguf_truncated", "gguf_version_unsupported",
})


class EngineStartupFailure(ValueError):
    """Bounded startup failure carrying no engine stderr or filesystem paths."""

    def __init__(self, error_code: str, child_status: dict[str, int] | None = None):
        super().__init__(error_code)
        self.child_status = child_status


def _stage_timeout(deadline: float, cap: float, error_code: str) -> float:
    """Reserve the final bounded cleanup window from the outer deadline."""

    remaining = deadline - time.monotonic() - CLEANUP_RESERVE_SECONDS
    if remaining < 1.0:
        raise ValueError(error_code)
    return min(float(cap), remaining)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tail(value: object) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+", r"\1\2<redacted>", text)
    return text[-TAIL_LIMIT:]


def _bounded_child_status(process: Any) -> dict[str, int] | None:
    """Expose only a small exit/signal identity, never child diagnostics."""

    value = process.poll()
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        return None
    return {"signal": -value} if value < 0 else {"exit_code": value}


SERVE_STDERR_CODES = {
    "unknown argument": "engine_startup_failed",
    "verify-model requires --model": "engine_model_preflight_invalid",
    "serve requires exactly one readable bearer token source (16-512 printable bytes)": "engine_token_file_not_private",
    "context must be between 1 and 16384 tokens": "engine_not_ready",
    "fixture backend does not accept a model path": "engine_startup_failed",
    "fixture backend is not compiled into product engines": "engine_not_ready",
    "cpu backend requires --model": "engine_startup_failed",
    "cpu backend forbids accelerated device or offload settings": "engine_startup_failed",
    "intel-vulkan requires 1..99 gpu layers": "engine_startup_failed",
    "intel-vulkan requires only an exact Vulkan device name": "engine_startup_failed",
    "cuda requires 1..99 gpu layers": "engine_startup_failed",
    "cuda requires only an exact CUDA device name": "engine_startup_failed",
    "unsupported backend profile": "engine_not_ready",
    "product cannot be built with both CUDA and Vulkan": "engine_startup_failed",
    "intel-vulkan requested but product was not built with LAE_ENABLE_LLAMA_VULKAN": "engine_startup_failed",
    "cuda requested but product was not built with LAE_ENABLE_LLAMA_CUDA": "engine_startup_failed",
    "multiple Vulkan devices match the exact configured name": "engine_startup_failed",
    "exact configured Vulkan integrated device is unavailable": "engine_startup_failed",
    "multiple CUDA devices match the exact configured name": "engine_startup_failed",
    "exact configured CUDA device is unavailable": "engine_startup_failed",
    "model validation lease is missing, mismatched, or stale": "engine_startup_failed",
    "model validation lease became stale before backend load": "engine_startup_failed",
    "llama model load failed": "engine_startup_failed",
    "model identity changed during backend load": "engine_startup_failed",
    "llama chat template unavailable; raw prompt mode is not accepted": "engine_startup_failed",
    "llama chat template parse failed": "engine_startup_failed",
    "llama chat template is not loaded": "engine_startup_failed",
    "invalid tool parameter schema": "engine_startup_failed",
    "tool parameter schema must be an object": "engine_startup_failed",
    "llama chat template rendered an empty prompt": "engine_startup_failed",
    "llama context creation failed": "engine_startup_failed",
    "llama sampler creation failed": "engine_startup_failed",
    "llama backend is not initialized": "engine_startup_failed",
    "real backend disabled; configure LAE_ENABLE_LLAMA_CPP=ON": "engine_startup_failed",
    "real backend disabled": "engine_startup_failed",
    "server already started": "engine_startup_failed",
    "winsock initialization failed": "engine_startup_failed",
    "socket creation failed": "engine_startup_failed",
    "loopback bind/listen failed": "engine_startup_failed",
}


def _exact_serve_stderr_code(value: object) -> str | None:
    """Map only complete app-owned stderr literals; all other text is generic."""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str) or len(value) > MAX_ENGINE_LINE:
        return None
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return None
    final = lines[-1]
    if final.startswith("model validation failed: "):
        validator_code = final.partition(": ")[2]
        return "engine_model_preflight_invalid" if validator_code in MODEL_VALIDATOR_CODES else None
    return SERVE_STDERR_CODES.get(final)


def _classify_serve_eof(process: Any, stderr_tail: object = None) -> EngineStartupFailure:
    """Classify a finite ready-stream EOF without retaining child diagnostics."""

    try:
        process.wait(timeout=1)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        pass
    child_status = _bounded_child_status(process)
    stderr_code = _exact_serve_stderr_code(stderr_tail)
    if child_status and "signal" in child_status:
        code = "engine_terminated_by_signal"
    elif stderr_code is not None:
        code = stderr_code
    elif child_status:
        code = "engine_not_ready"
    else:
        code = "engine_ready_eof"
    return EngineStartupFailure(code, child_status)


def _read_ready_line(stream: Any, deadline: float) -> bytes | None:
    """Read one bounded JSON line without blocking on a partial pipe line."""

    data = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream, selectors.EVENT_READ)
        while len(data) <= MAX_ENGINE_LINE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("engine_ready_timeout")
            if not selector.select(timeout=remaining):
                raise ValueError("engine_ready_timeout")
            chunk = os.read(stream.fileno(), min(4096, MAX_ENGINE_LINE + 1 - len(data)))
            if not chunk:
                if not data:
                    return None
                raise ValueError("engine_ready_receipt_invalid")
            data.extend(chunk)
            newline = data.find(b"\n")
            if newline >= 0:
                if newline > MAX_ENGINE_LINE:
                    raise ValueError("engine_ready_receipt_invalid")
                return bytes(data[:newline])
        raise ValueError("engine_ready_receipt_invalid")
    finally:
        selector.close()


def _strict_json_object(value: str | bytes) -> object:
    """Decode one small JSON object while rejecting duplicate keys."""

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=reject_duplicates)


def _preflight_summary(preflight: dict[str, Any]) -> dict[str, Any]:
    """Keep only safe identity fields in a final failure receipt."""

    if (not isinstance(preflight, dict) or set(preflight) != {"valid", "code", "size_bytes", "sha256", "gguf_version"} or
            preflight.get("valid") is not True or
            preflight.get("code") != "ok" or preflight.get("gguf_version") != 3 or
            isinstance(preflight.get("size_bytes"), bool) or not isinstance(preflight.get("size_bytes"), int) or
            not isinstance(preflight.get("sha256"), str) or len(preflight["sha256"]) != 64 or
            set(preflight["sha256"]) - PIN_RE):
        raise ValueError("engine_model_preflight_invalid")
    return {"status": "verified", "size_bytes": preflight["size_bytes"], "sha256": preflight["sha256"], "gguf_version": 3}


def _write_preflight_receipt(
    path: Path,
    preflight: dict[str, Any] | None = None,
    *,
    status: str = "verified",
    error_code: str | None = None,
    validator_code: str | None = None,
    child_status: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Atomically publish a tiny, secret-free receipt for every outcome."""

    if status == "verified":
        if preflight is None:
            raise ValueError("engine_model_preflight_invalid")
        summary = _preflight_summary(preflight)
    elif status in {"rejected", "timeout", "oversize", "terminated", "failed"}:
        summary = {"status": status}
        if error_code in SAFE_ERROR_CODES:
            summary["error_code"] = error_code
        if validator_code in MODEL_VALIDATOR_CODES and validator_code != "ok":
            summary["validator_code"] = validator_code
        if isinstance(child_status, dict) and set(child_status) in ({"exit_code"}, {"signal"}):
            value = next(iter(child_status.values()))
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255:
                summary["child"] = {next(iter(child_status)): value}
    else:
        raise ValueError("engine_model_preflight_invalid")
    payload = {"schema": MODEL_PREFLIGHT_RECEIPT_SCHEMA, **summary}
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > 1024:
        raise ValueError("engine_model_preflight_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=os.fspath(path.parent))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return summary


def _pin(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or set(value) - PIN_RE:
        raise ValueError(f"{label}_pin_invalid")
    return value


def _safe_error_code(error: BaseException) -> str:
    """Retain a stable reason without persisting dynamic exception detail."""

    candidate = str(error).partition(":")[0]
    return candidate if candidate in SAFE_ERROR_CODES else "evaluation_failed"


def verify_artifact(model: Path, manifest_path: Path, *, source_revision: str, llama_revision: str, manifest_lock_path: Path | None = None) -> dict[str, Any]:
    if model.name != MODEL_NAME or not model.is_file():
        raise ValueError("q4_artifact_missing_or_wrong_name")
    source_revision = _pin(source_revision, "source")
    llama_revision = _pin(llama_revision, "llama")
    manifest_lock_path = manifest_lock_path or manifest_path.with_name("model-manifest.sha256")
    lock_parts = manifest_lock_path.read_text(encoding="utf-8").strip().split()
    if len(lock_parts) != 2 or lock_parts[1] != manifest_path.name or len(lock_parts[0]) != 64 or any(character not in PIN_RE for character in lock_parts[0]):
        raise ValueError("model_manifest_lock_invalid")
    if sha256(manifest_path) != lock_parts[0]:
        raise ValueError("model_manifest_lock_mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.1.0":
        raise ValueError("model_manifest_invalid")
    source = manifest.get("source")
    conversion = manifest.get("conversion")
    artifact = manifest.get("artifact")
    if not isinstance(source, dict) or source.get("organization") != "Qwen" or source.get("repository") != "Qwen3.5-9B" or source.get("revision") != source_revision:
        raise ValueError("source_revision_mismatch")
    if not isinstance(conversion, dict) or conversion.get("llama_cpp_revision") != llama_revision:
        raise ValueError("llama_revision_mismatch")
    if not isinstance(artifact, dict) or artifact.get("expected_file_name") != MODEL_NAME or artifact.get("modality_profile") != "text_only_no_mmproj" or artifact.get("quantization_profile") != "Q4_K_M":
        raise ValueError("artifact_manifest_invalid")
    size = model.stat().st_size
    digest = sha256(model)
    if artifact.get("expected_size_bytes") != size or artifact.get("sha256") != digest:
        raise ValueError("q4_artifact_hash_mismatch")
    return {
        "name": MODEL_NAME,
        "size_bytes": size,
        "sha256": digest,
        "source_revision": source_revision,
        "llama_cpp_revision": llama_revision,
        "modality": artifact.get("modality_profile"),
        "quantization": artifact.get("quantization_profile"),
    }


def _write_token(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(os.fspath(path), flags, 0o600)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(secrets.token_urlsafe(32))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("engine_token_file_not_private")


def _run_bounded(command: list[str], *, timeout: float, output_limit: int = MAX_EVAL_OUTPUT) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout_log, tempfile.TemporaryFile() as stderr_log:
        try:
            result = subprocess.run(command, stdout=stdout_log, stderr=stderr_log, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "exit_code": None, "stderr_tail": _file_tail(stderr_log)}
        stdout_log.seek(0)
        stdout = stdout_log.read(output_limit + 1)
        if len(stdout) > output_limit:
            return {"status": "output_too_large", "exit_code": result.returncode, "stderr_tail": _file_tail(stderr_log)}
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr_tail": _file_tail(stderr_log),
        }


def _file_tail(stream: Any) -> str:
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - TAIL_LIMIT * 4))
    return _tail(stream.read())


def _engine_build_info(engine: Path, expected_llama: str, expected_backend: str, *, timeout: float = 30.0) -> dict[str, Any]:
    result = _run_bounded([os.fspath(engine), "print-build-info"], timeout=timeout, output_limit=MAX_ENGINE_LINE)
    if result.get("status") != "completed":
        raise ValueError("engine_build_info_failed")
    try:
        payload = json.loads(result.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("engine_build_info_invalid") from exc
    if not isinstance(payload, dict) or payload.get("llama_cpp_revision") != expected_llama or payload.get("compiled_backend") != f"llama.cpp/{expected_llama[:8]}/{expected_backend}":
        raise ValueError("engine_llama_identity_mismatch")
    return {key: payload[key] for key in ("engine_version", "api_version", "compiled_backend", "llama_cpp_revision", "model") if key in payload}


def _preflight_child_status(result: object) -> dict[str, int] | None:
    if not isinstance(result, dict):
        return None
    value = result.get("exit_code")
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        return None
    return {"signal": -value} if value < 0 else {"exit_code": value}


def _preflight_outcome_status(error_code: str) -> str:
    if error_code == "engine_model_preflight_timeout":
        return "timeout"
    if error_code == "engine_model_preflight_output_too_large":
        return "oversize"
    if error_code == "engine_model_preflight_terminated_by_signal":
        return "terminated"
    return "rejected" if error_code != "engine_model_preflight_failed" else "failed"


def _engine_model_preflight(
    args: argparse.Namespace,
    artifact: dict[str, Any],
    *,
    timeout: float = 30.0,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Verify native compiled model identity before attempting server startup."""

    result: object = None
    payload: object = None
    try:
        result = _run_bounded([os.fspath(args.engine), "verify-model", "--model", args.model], timeout=timeout, output_limit=MAX_ENGINE_LINE)
        if not isinstance(result, dict):
            raise ValueError("engine_model_preflight_failed")
        result_status = result.get("status")
        exit_code = result.get("exit_code")
        if result_status == "timeout":
            raise ValueError("engine_model_preflight_timeout")
        if result_status == "output_too_large":
            raise ValueError("engine_model_preflight_output_too_large")
        if result_status not in {"completed", "failed"} or isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("engine_model_preflight_failed")
        if exit_code < -255 or exit_code > 255:
            raise ValueError("engine_model_preflight_exit")
        if exit_code < 0:
            raise ValueError("engine_model_preflight_terminated_by_signal")
        if exit_code not in {0, 2}:
            raise ValueError("engine_model_preflight_exit")
        if (exit_code == 0 and result_status != "completed") or (exit_code == 2 and result_status != "failed"):
            raise ValueError("engine_model_preflight_failed")
        try:
            payload = json.loads(result.get("stdout", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("engine_model_preflight_invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {"valid", "code", "size_bytes", "sha256", "gguf_version"}:
            raise ValueError("engine_model_preflight_invalid")
        if not isinstance(payload.get("code"), str) or payload["code"] not in MODEL_VALIDATOR_CODES:
            raise ValueError("engine_model_preflight_invalid")
        if isinstance(payload.get("size_bytes"), bool) or not isinstance(payload.get("size_bytes"), int) or payload["size_bytes"] < 0:
            raise ValueError("engine_model_preflight_invalid")
        if not isinstance(payload.get("sha256"), str) or (payload["sha256"] and (len(payload["sha256"]) != 64 or set(payload["sha256"]) - PIN_RE)):
            raise ValueError("engine_model_preflight_invalid")
        if isinstance(payload.get("gguf_version"), bool) or not isinstance(payload.get("gguf_version"), int) or not 0 <= payload["gguf_version"] <= 3:
            raise ValueError("engine_model_preflight_invalid")
        if exit_code == 0:
            if payload.get("valid") is not True or payload.get("code") != "ok" or payload["size_bytes"] != artifact.get("size_bytes") or payload["sha256"] != artifact.get("sha256") or payload["gguf_version"] != 3:
                raise ValueError("engine_model_preflight_invalid")
        elif payload.get("valid") is not False or payload.get("code") == "ok":
            raise ValueError("engine_model_preflight_invalid")
        else:
            raise ValueError("engine_model_preflight_invalid")
        preflight = {"valid": payload["valid"], "code": payload["code"], "size_bytes": payload["size_bytes"], "sha256": payload["sha256"], "gguf_version": payload["gguf_version"]}
        summary = _write_preflight_receipt(receipt_path, preflight) if receipt_path is not None else _preflight_summary(preflight)
        setattr(args, "_preflight_summary", summary)
        return preflight
    except (OSError, TypeError, ValueError, KeyError, IndexError, RecursionError, OverflowError, subprocess.SubprocessError) as exc:
        error_code = _safe_error_code(exc)
        if error_code not in SAFE_ERROR_CODES or not error_code.startswith("engine_model_preflight_"):
            error_code = "engine_model_preflight_failed"
        validator_code = payload.get("code") if isinstance(payload, dict) and payload.get("code") in MODEL_VALIDATOR_CODES else None
        summary = _write_preflight_receipt(
            receipt_path,
            status=_preflight_outcome_status(error_code),
            error_code=error_code,
            validator_code=validator_code,
            child_status=_preflight_child_status(result),
        ) if receipt_path is not None else {"status": _preflight_outcome_status(error_code), "error_code": error_code}
        setattr(args, "_preflight_summary", summary)
        raise ValueError(error_code) from exc


def _engine_launch_argv(args: argparse.Namespace, token_file: Path, backend: str) -> list[str]:
    launch = [
        os.fspath(args.engine), "serve", "--port", "0", "--backend", backend,
        "--model", args.model, "--context", "2048",
        "--token-file", os.fspath(token_file),
    ]
    if backend == "cuda":
        launch.extend(["--gpu-layers", "99", "--cuda-device-name", args.cuda_device_name])
    return launch


def _fixture_contract(path: Path) -> tuple[int, set[str]]:
    """Read only the bounded fixture contract; never echo its prompts."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("evaluator_fixture_unreadable") from exc
    if len(raw) > FIXTURE_MAX_BYTES:
        raise ValueError("evaluator_fixture_too_large")
    try:
        fixture = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("evaluator_fixture_invalid") from exc
    if not isinstance(fixture, dict) or not isinstance(fixture.get("limits"), dict) or not isinstance(fixture.get("cases"), list):
        raise ValueError("evaluator_fixture_invalid")
    count = fixture["limits"].get("max_cases")
    cases = fixture["cases"]
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_EVAL_CASES or len(cases) != count:
        raise ValueError("evaluator_fixture_count_invalid")
    categories = {case.get("category") for case in cases if isinstance(case, dict)}
    if len(categories) == 0 or not all(isinstance(category, str) for category in categories):
        raise ValueError("evaluator_fixture_categories_invalid")
    return count, categories


def _validate_metrics(metrics: Any, *, expected_case_count: int = 8, expected_categories: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        raise ValueError("evaluator_metrics_invalid")
    counts = ("case_count", "passed", "failed", "errors")
    if any(isinstance(metrics.get(key), bool) or not isinstance(metrics.get(key), int) or metrics[key] < 0 for key in counts):
        raise ValueError("evaluator_metrics_invalid")
    if isinstance(expected_case_count, bool) or not isinstance(expected_case_count, int) or not 1 <= expected_case_count <= MAX_EVAL_CASES:
        raise ValueError("evaluator_expected_count_invalid")
    if metrics["case_count"] != expected_case_count or sum(metrics[key] for key in counts[1:]) != expected_case_count:
        raise ValueError("evaluator_metrics_total_invalid")
    peak = metrics.get("peak_rss_kib")
    if peak is not None and (isinstance(peak, bool) or not isinstance(peak, int) or peak < 0):
        raise ValueError("evaluator_rss_invalid")
    summary = metrics.get("category_summary")
    if expected_categories is not None:
        if not isinstance(summary, dict) or set(summary) != expected_categories:
            raise ValueError("evaluator_category_summary_invalid")
        category_total = 0
        for category in expected_categories:
            item = summary[category]
            if not isinstance(item, dict) or set(item) != {"case_count", "passed", "failed", "errors"}:
                raise ValueError("evaluator_category_summary_invalid")
            if any(isinstance(item.get(key), bool) or not isinstance(item.get(key), int) or item[key] < 0 for key in ("case_count", "passed", "failed", "errors")):
                raise ValueError("evaluator_category_summary_invalid")
            if item["passed"] + item["failed"] + item["errors"] != item["case_count"]:
                raise ValueError("evaluator_category_summary_invalid")
            category_total += item["case_count"]
        if category_total != expected_case_count:
            raise ValueError("evaluator_category_summary_total_invalid")
    return {key: metrics.get(key) for key in (*counts, "peak_rss_kib")} | ({"category_summary": summary} if summary is not None else {})


def _parse_evaluator_result(result: dict[str, Any], *, expected_case_count: int, expected_categories: set[str]) -> tuple[dict[str, Any], bool, bool]:
    exit_code = result.get("exit_code")
    if result.get("status") not in {"completed", "failed"} or isinstance(exit_code, bool) or exit_code not in {0, 1}:
        raise ValueError("evaluator_process_failed")
    try:
        metrics = json.loads(result.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("evaluator_receipt_invalid") from exc
    metrics = _validate_metrics(metrics, expected_case_count=expected_case_count, expected_categories=expected_categories)
    all_passed = metrics["passed"] == expected_case_count and metrics["failed"] == 0 and metrics["errors"] == 0
    has_failure = metrics["failed"] > 0 or metrics["errors"] > 0
    if (exit_code == 0) != all_passed or (exit_code == 1) != has_failure:
        raise ValueError("evaluator_exit_status_mismatch")
    return metrics, all_passed, has_failure


def _launch_and_evaluate(args: argparse.Namespace, artifact: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
    started = time.monotonic()
    deadline = deadline if deadline is not None else started + float(getattr(args, "timeout", EVAL_TOTAL_TIMEOUT))
    engine = Path(args.engine)
    if not engine.is_file():
        raise ValueError("engine_binary_missing")
    checkout = Path(args.llama_checkout)
    rev = _run_bounded(["git", "-C", os.fspath(checkout), "rev-parse", "HEAD"], timeout=30, output_limit=128)
    if rev.get("status") != "completed" or rev.get("stdout", "").strip() != artifact["llama_cpp_revision"]:
        raise ValueError("llama_checkout_revision_mismatch")
    backend = getattr(args, "backend", "cpu")
    if backend not in {"cpu", "cuda"}:
        raise ValueError("evaluation_backend_invalid")
    expected_case_count, expected_categories = _fixture_contract(Path(args.fixture))
    cuda_receipt = None
    if backend == "cuda":
        receipt_path = Path(getattr(args, "cuda_device_receipt", ""))
        try:
            cuda_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("cuda_device_receipt_invalid") from exc
        device = cuda_receipt.get("device") if isinstance(cuda_receipt, dict) else None
        if (not isinstance(cuda_receipt, dict) or cuda_receipt.get("schema") != "local_bmo.j1m.cuda-device-receipt.v1" or cuda_receipt.get("status") != "verified" or cuda_receipt.get("selector") != getattr(args, "cuda_device_name", "") or cuda_receipt.get("device_count") != 1 or not isinstance(device, dict) or "a100" not in str(device.get("name", "")).lower() or not isinstance(device.get("memory_total_mib"), int) or device["memory_total_mib"] < 70000):
            raise ValueError("cuda_device_receipt_invalid")
    try:
        toolchain = json.loads(Path(args.toolchain_receipt).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("toolchain_receipt_invalid") from exc
    versions = toolchain.get("versions") if isinstance(toolchain, dict) else None
    minimums = {"python3": (3, 8), "git": (2, 30), "cmake": (3, 18), "g++": (9, 0), "nvcc": (12, 0)}
    if (not isinstance(toolchain, dict) or toolchain.get("schema") != "local_bmo.j1m.remote-toolchain-receipt.v1" or toolchain.get("status") != "verified" or not isinstance(versions, dict)):
        raise ValueError("toolchain_receipt_invalid")
    for name, minimum in minimums.items():
        version = versions.get(name)
        if (not isinstance(version, dict) or isinstance(version.get("major"), bool) or not isinstance(version.get("major"), int) or isinstance(version.get("minor"), bool) or not isinstance(version.get("minor"), int) or (version["major"], version["minor"]) < minimum):
            raise ValueError("toolchain_receipt_invalid")
    if versions["nvcc"].get("executable") != "/usr/local/cuda/bin/nvcc":
        raise ValueError("toolchain_receipt_invalid")
    packages = toolchain.get("packages")
    expected_packages = {"ca-certificates", "cmake", "build-essential", "git", "python3", "python3-venv"}
    if not isinstance(packages, dict) or set(packages) != expected_packages or any(not isinstance(value, str) or not value or len(value) > 160 for value in packages.values()):
        raise ValueError("toolchain_receipt_invalid")
    preflight_path = Path(getattr(args, "preflight_receipt", "") or Path(args.receipt).with_name("startup-preflight-receipt.json"))
    try:
        preflight_timeout = _stage_timeout(deadline, 120.0, "engine_model_preflight_timeout")
    except ValueError as exc:
        summary = _write_preflight_receipt(preflight_path, status="timeout", error_code="engine_model_preflight_timeout")
        setattr(args, "_preflight_summary", summary)
        raise
    model_preflight = _engine_model_preflight(args, artifact, timeout=preflight_timeout, receipt_path=preflight_path)
    preflight_summary = getattr(args, "_preflight_summary", _preflight_summary(model_preflight))
    build_info = _engine_build_info(engine, artifact["llama_cpp_revision"], backend, timeout=_stage_timeout(deadline, 30.0, "engine_build_info_failed"))
    token_file = Path(args.token_file)
    _write_token(token_file)
    process: subprocess.Popen[str] | None = None
    engine_stderr: Any = None
    try:
        # The protected token-file option is deliberately explicit.  Passing
        # a bearer as a command-line argument would expose it through process
        # inspection and is forbidden by the evaluation contract.
        launch = _engine_launch_argv(args, token_file, backend)
        engine_stderr = tempfile.TemporaryFile()
        process = subprocess.Popen(launch, stdout=subprocess.PIPE, stderr=engine_stderr, text=False)
        if process.stdout is None:
            raise ValueError("engine_stdout_unavailable")
        ready_line = _read_ready_line(process.stdout, min(deadline, time.monotonic() + 120.0))
        if ready_line is None:
            raise _classify_serve_eof(process, _file_tail(engine_stderr))
        try:
            ready = _strict_json_object(ready_line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("engine_ready_receipt_invalid") from exc
        port = ready.get("port") if isinstance(ready, dict) else None
        if (not isinstance(ready, dict) or set(ready) != {"event", "port", "bind", "token_required"} or
                ready.get("event") != "ready" or ready.get("token_required") is not True or
                isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or ready.get("bind") != "127.0.0.1"):
            raise ValueError("engine_ready_identity_invalid")
        evaluate = [
            sys.executable, args.evaluator, "--fixture", args.fixture,
            "--endpoint", f"http://127.0.0.1:{port}/v1/chat/completions",
            "--token-file", os.fspath(token_file), "--timeout", "120",
            "--max-cases", str(expected_case_count), "--engine-pid", str(process.pid),
        ]
        evaluator_timeout = _stage_timeout(deadline, float(args.timeout), "evaluator_timeout_invalid")
        evaluate[evaluate.index("--timeout") + 1] = str(max(1, math.floor(evaluator_timeout)))
        result = _run_bounded(evaluate, timeout=evaluator_timeout, output_limit=MAX_EVAL_OUTPUT)
        metrics, all_passed, has_failure = _parse_evaluator_result(
            result,
            expected_case_count=expected_case_count,
            expected_categories=expected_categories,
        )
        status = "verified" if all_passed else "completed_with_failures" if has_failure else "failed"
        return {
            "schema": "local_bmo.j1m.real-tool-eval-receipt.v1",
            "status": status,
            "artifact": artifact,
            "engine": build_info,
            # Keep the original identity fields for receipt consumers while
            # adding the explicit verified status used by salvage.
            "model_preflight": {**model_preflight, **preflight_summary},
            **({"cuda_device": cuda_receipt} if cuda_receipt is not None else {}),
            "toolchain": toolchain,
            "metrics": {key: metrics[key] for key in ("case_count", "passed", "failed", "errors", "peak_rss_kib", "category_summary")},
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "prompt_response_logging": False,
            "token_logging": False,
        }
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
            if process.stderr is not None:
                process.stderr.close()
            if process.stdout is not None:
                process.stdout.close()
        if engine_stderr is not None:
            engine_stderr.close()
        token_file.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-manifest", required=True)
    parser.add_argument("--model-manifest-lock", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--llama-revision", required=True)
    parser.add_argument("--llama-checkout", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--evaluator", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--backend", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--cuda-device-name", default="")
    parser.add_argument("--cuda-device-receipt", default="")
    parser.add_argument("--toolchain-receipt", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--preflight-receipt", default="")
    parser.add_argument("--timeout", type=float, default=EVAL_TOTAL_TIMEOUT)
    args = parser.parse_args(argv)
    receipt: dict[str, Any]
    process_started = time.monotonic()
    try:
        if not 1 <= args.timeout <= EVAL_TOTAL_TIMEOUT:
            raise ValueError("eval_timeout_invalid")
        deadline = process_started + args.timeout
        artifact = verify_artifact(Path(args.model), Path(args.model_manifest), source_revision=args.source_revision, llama_revision=args.llama_revision, manifest_lock_path=Path(args.model_manifest_lock))
        receipt = _launch_and_evaluate(args, artifact, deadline=deadline)
        status = 0 if receipt["status"] in {"verified", "completed_with_failures"} else 1
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, OverflowError, subprocess.SubprocessError) as exc:
        receipt = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "failed", "error_type": type(exc).__name__, "error_code": _safe_error_code(exc), "prompt_response_logging": False, "token_logging": False}
        preflight = getattr(args, "_preflight_summary", None)
        if isinstance(preflight, dict):
            receipt["preflight"] = preflight
        child_status = getattr(exc, "child_status", None)
        if isinstance(child_status, dict) and set(child_status) in ({"exit_code"}, {"signal"}):
            receipt["child"] = child_status
        status = 1
    output = Path(args.receipt)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schema": receipt["schema"], "status": receipt["status"], "metrics": receipt.get("metrics")}, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
