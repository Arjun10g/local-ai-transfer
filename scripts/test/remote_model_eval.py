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
import shutil
import tempfile
from pathlib import Path
from typing import Any


MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
PIN_RE = set("0123456789abcdef")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
# Opt-in suite members (``j1m_orchestrator.py --suite extended`` and
# ``--scorer-ablation no-boolean-coercion``). With ``--suite-member`` absent
# nothing below is reachable and the shipping receipt is byte-for-byte what it
# was. Each member writes its OWN schema, so a member receipt can never be
# published or verified as the shipping ``eval-receipt.json``.
VARIANT_MEMBERS = ("variants-chunk-1", "variants-chunk-2", "variants-chunk-3")
ABLATION_MEMBER = "ablation-no-boolean-coercion"
LONG_CONTEXT_MEMBER = "long-context"
MEMORY_MEMBER = "memory"
SUITE_MEMBER_SCHEMAS = {
    **{member: "local_bmo.j1m.variants-eval-receipt.v1" for member in VARIANT_MEMBERS},
    ABLATION_MEMBER: "local_bmo.j1m.scorer-ablation-eval-receipt.v1",
    LONG_CONTEXT_MEMBER: "local_bmo.j1m.long-context-receipt.v1",
    MEMORY_MEMBER: "local_bmo.j1m.memory-eval-receipt.v1",
}
# Recorded in the ablation receipt so a no-coercion score can never be read as
# the shipping one.
SCORER_ABLATION_RECORD = {"name": ABLATION_MEMBER, "boolean_coercion": False}
SUITE_ERROR_CODES = frozenset({
    "suite_member_invalid", "evaluator_fixture_identity_mismatch",
    "long_context_harness_missing", "long_context_harness_failed",
    "long_context_timeout", "long_context_receipt_missing",
    "long_context_receipt_invalid",
    "memory_harness_missing", "memory_harness_failed", "memory_timeout",
    "memory_receipt_missing", "memory_receipt_invalid", "memory_prompts_identity_mismatch",
})
# The long-context member's whole budget: engine start plus the harness. It is
# deliberately separate from (and larger than) EVAL_TOTAL_TIMEOUT, which stays
# the shipping eval's cap; the orchestrator budgets the stage explicitly.
LONG_CONTEXT_TOTAL_TIMEOUT = 840.0
LONG_CONTEXT_RAW_MAX_BYTES = 4 * 1024 * 1024
LONG_CONTEXT_HARNESS_MAX_BYTES = 512 * 1024
# The one conservative profile this lane runs. Fixed in source, not argv, so a
# paid stage cannot be widened by configuration; the orchestrator mirrors it
# for the plan and its verifier requires the receipt to repeat it exactly.
LONG_CONTEXT_PROFILE = {
    "sizes": [1000, 2000, 4000, 6000],
    "styles": ["turns", "tool_loop"],
    "depths": [0.1, 0.5, 0.9],
    "trials": 1,
    "probes": ["needle", "multi_needle", "latest_value", "tool_result", "tool_call"],
    "max_output": 96,
    "request_timeout_seconds": 60,
    "stop_after_failures": 40,
}
# The memory member (``scripts/test/memory_eval.py`` with the host's shared
# ``host/agent/memory-prompts.json``). One fixed profile, like long context:
# six synthetic conversations, all five arms (note, drop, full, recall, both),
# two summarisation chunks per note, the host's own note/excerpt bounds.
# 6 x (2 + 8 x 5) = 252 requests, and ``max_requests`` is exactly that, so the
# harness refuses a larger plan before its first request.
MEMORY_TOTAL_TIMEOUT = 600.0
MEMORY_RAW_MAX_BYTES = 4 * 1024 * 1024
MEMORY_PROMPTS_MAX_BYTES = 64 * 1024
MEMORY_PROFILE = {
    "conversations": 6,
    "arms": ["note", "drop", "full", "recall", "both"],
    "chunks": 2,
    "dropped_tokens": 2500,
    "retained_tokens": 1500,
    "note_tokens": 512,
    "note_bytes": 1536,
    "max_input_bytes": 6144,
    "per_message_bytes": 1536,
    "max_output": 64,
    "request_timeout_seconds": 60,
    "max_requests": 252,
}
# Mirrors of scripts/test/memory_eval.py's closed vocabularies; the extended
# suite tests pin that the two agree.
MEMORY_FACT_TYPES = ("name", "number", "preference", "decision", "tool_result", "updated")
MEMORY_PROBES = MEMORY_FACT_TYPES + ("retained_control", "hallucination")
MEMORY_OUTCOMES = ("context_overflow", "engine_busy", "error", "fail", "pass", "request_too_large", "skipped", "timeout")
MEMORY_AGE_BUCKETS = ((1, 5), (6, 10), (11, 20), (21, 40), (41, 1000))
MEMORY_CONVERSATION_ID = re.compile(r"^conv[0-9]{1,2}$")
LONG_CONTEXT_OUTCOMES = ("context_overflow", "engine_busy", "error", "fail", "pass", "request_too_large", "timeout")
LONG_CONTEXT_CELL_ID = re.compile(r"^[a-z_]{1,16}/[a-z_]{1,16}/s[0-9]{3,5}/d[0-9.]{1,6}/t[0-9]{1,2}$")
LONG_CONTEXT_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
SAFE_ERROR_CODES = frozenset({
    "artifact_manifest_invalid", "cuda_device_receipt_invalid", "engine_binary_missing",
    "engine_build_info_failed", "engine_build_info_invalid", "engine_llama_identity_mismatch",
    "engine_model_preflight_failed", "engine_model_preflight_invalid", "engine_not_ready",
    "engine_model_preflight_exit", "engine_model_preflight_output_too_large",
    "engine_model_preflight_timeout", "engine_model_preflight_terminated_by_signal",
    "engine_model_preflight_not_started",
    "engine_startup_failed", "engine_cli_invalid", "engine_config_invalid", "engine_cuda_unavailable",
    "engine_cuda_ambiguous", "engine_vulkan_unavailable", "engine_vulkan_ambiguous",
    "engine_model_lease_invalid", "engine_model_load_failed", "engine_model_identity_changed",
    "engine_template_unavailable", "engine_template_parse_failed", "engine_template_invalid",
    "engine_context_failed", "engine_sampler_failed", "engine_socket_failed", "engine_bind_failed",
    "engine_generation_failed",
    "engine_ready_eof", "engine_ready_identity_invalid", "engine_ready_receipt_invalid",
    "engine_ready_timeout", "engine_stdout_unavailable", "engine_token_file_not_private",
    "engine_terminated_by_signal",
    "eval_timeout_invalid", "evaluation_backend_invalid", "evaluator_category_summary_invalid",
    "evaluator_category_summary_total_invalid", "evaluator_expected_count_invalid",
    "evaluator_fixture_categories_invalid", "evaluator_fixture_count_invalid",
    "evaluator_fixture_invalid", "evaluator_fixture_too_large", "evaluator_fixture_unreadable",
    "evaluator_metrics_invalid", "evaluator_metrics_total_invalid", "evaluator_process_failed",
    "evaluator_failed_cases_invalid",
    "evaluator_exit_status_mismatch", "evaluator_receipt_invalid", "evaluator_rss_invalid",
    "evaluator_diagnostics_invalid", "evaluator_diagnostics_total_invalid", "evaluator_diagnostics_code_invalid",
    "evaluator_quality_diagnostics_invalid", "evaluator_quality_diagnostics_total_invalid", "evaluator_quality_diagnostics_code_invalid",
    "evaluator_canary_invalid", "engine_exited_during_evaluation",
    "llama_checkout_revision_mismatch",
    "llama_pin_invalid", "llama_revision_mismatch", "model_manifest_invalid",
    "model_manifest_lock_invalid", "model_manifest_lock_mismatch", "q4_artifact_hash_mismatch",
    "q4_artifact_missing_or_wrong_name", "source_pin_invalid", "source_revision_mismatch",
    "toolchain_receipt_invalid",
}) | SUITE_ERROR_CODES
EVAL_HTTP_DIAGNOSTIC_STATUSES = (400, 401, 404, 408, 409, 413, 415, 429, 500, 503)
# Mirrors ENGINE_ERROR_CODES in scripts/test/evaluate_tool_calls.py; the two
# move together, and tests/model/test_tool_call_eval.py pins that they agree.
EVAL_ENGINE_ERROR_CODES = frozenset({
    "unauthorized", "not_found", "method_not_allowed", "invalid_json",
    "invalid_request", "request_too_large", "not_ready", "busy",
    "request_cancelled", "shutdown", "internal_error",
    "model_path_not_absolute", "model_symlink_forbidden", "model_size_mismatch",
    "model_hash_mismatch", "model_mmproj_forbidden", "gguf_magic_invalid",
    "gguf_version_unsupported", "model_architecture_mismatch",
    "invalid_request_line", "invalid_headers", "invalid_content_length",
    "unsupported_transfer_encoding", "missing_content_length", "unexpected_body",
    "surplus_body", "invalid_content_type", "headers_too_large",
    "invalid_session_id", "invalid_request_id", "request_timeout",
    "response_too_large",
})
EVAL_DIAGNOSTIC_CODES = frozenset({
    "http_400", "http_401", "http_404", "http_408", "http_409", "http_413",
    "http_415", "http_429", "http_500", "http_503", "http_other",
    "transport_url", "transport_timeout", "transport_os", "parse_json",
    "parse_session_shape", "parse_response_shape", "context_overflow",
    "endpoint", "token", "unknown",
} | {f"http_{status}_{code}" for status in EVAL_HTTP_DIAGNOSTIC_STATUSES for code in EVAL_ENGINE_ERROR_CODES})
EVAL_QUALITY_CODES = frozenset({
    "forbidden_tool_name", "malformed_call", "unknown_tool", "malformed_parameter",
    "parameter_too_large", "invalid_json_argument", "invalid_tool_schema",
    "invalid_arguments", "missing_argument", "extra_argument",
    "argument_type_mismatch", "argument_value_mismatch", "missing_call",
    "unexpected_call", "wrong_tool", "call_mismatch",
    "quality_unknown",
})
TAIL_LIMIT = 1200
MAX_ENGINE_LINE = 8192
MAX_EVAL_OUTPUT = 256 * 1024
FIXTURE_MAX_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
# Raised from 1024 to leave room for the required run-identity binding.
MAX_PREFLIGHT_RECEIPT_BYTES = 2048
MAX_METADATA_BYTES = 256 * 1024
# Bounded evaluator capacity for the current production profile. Exact
# membership and ordering remain part of the fixture identity.
MAX_EVAL_TOOLS = 33
MAX_EVAL_CASES = 64
MAX_OUTPUT_RESERVE_TOKENS = 256
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


def _deadline_check(deadline: float | None, error_code: str) -> None:
    if deadline is not None and time.monotonic() + CLEANUP_RESERVE_SECONDS >= deadline:
        raise ValueError(error_code)


def _read_bounded(path: Path, limit: int, *, deadline: float | None = None, error_code: str) -> bytes:
    """Read a local receipt/manifest with both size and outer-clock bounds."""

    _deadline_check(deadline, error_code)
    try:
        # Open first, then bind the descriptor identity and size. A path
        # replacement during validation must not cause us to hash one file and
        # parse another; the post-read lstat catches replacement before close.
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > limit:
                raise ValueError(error_code)
            data = bytearray()
            while True:
                _deadline_check(deadline, error_code)
                chunk = stream.read(min(64 * 1024, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError(error_code)
            after = os.fstat(stream.fileno())
            current = path.stat()
            identity_before = (before.st_dev, before.st_ino)
            identity_after = (after.st_dev, after.st_ino)
            identity_current = (current.st_dev, current.st_ino)
            if (before.st_size != after.st_size or after.st_size != len(data) or
                    identity_before != identity_after or identity_after != identity_current):
                raise ValueError(error_code)
            return bytes(data)
    except ValueError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ValueError(error_code) from exc


def sha256(path: Path, *, deadline: float | None = None, error_code: str = "q4_artifact_hash_mismatch") -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                _deadline_check(deadline, error_code)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise ValueError(error_code) from exc
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


def _reap_child_status(process: Any, deadline: float | None = None) -> dict[str, int] | None:
    """Give an exiting child one short, outer-clock-bounded chance to reap."""

    timeout = 1.0
    if deadline is not None:
        timeout = max(0.0, min(timeout, deadline - time.monotonic() - CLEANUP_RESERVE_SECONDS))
    try:
        process.wait(timeout=timeout)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        pass
    return _bounded_child_status(process)


SERVE_STDERR_CODES = {
    "unknown argument": "engine_cli_invalid",
    "invalid numeric argument": "engine_cli_invalid",
    "config load failed": "engine_config_invalid",
    "verify-model requires --model": "engine_model_preflight_invalid",
    "serve requires exactly one readable bearer token source (16-512 printable bytes)": "engine_token_file_not_private",
    "context must be between 1 and 16384 tokens": "engine_not_ready",
    "fixture backend does not accept a model path": "engine_cli_invalid",
    "fixture backend is not compiled into product engines": "engine_not_ready",
    "cpu backend requires --model": "engine_cli_invalid",
    "cpu backend forbids accelerated device or offload settings": "engine_cli_invalid",
    "intel-vulkan requires 1..99 gpu layers": "engine_cli_invalid",
    "intel-vulkan requires only an exact Vulkan device name": "engine_cli_invalid",
    "cuda requires 1..99 gpu layers": "engine_cli_invalid",
    "cuda requires only an exact CUDA device name": "engine_cli_invalid",
    "unsupported backend profile": "engine_not_ready",
    "product cannot be built with both CUDA and Vulkan": "engine_cli_invalid",
    "intel-vulkan requested but product was not built with LAE_ENABLE_LLAMA_VULKAN": "engine_vulkan_unavailable",
    "cuda requested but product was not built with LAE_ENABLE_LLAMA_CUDA": "engine_cuda_unavailable",
    "multiple Vulkan devices match the exact configured name": "engine_vulkan_ambiguous",
    "exact configured Vulkan integrated device is unavailable": "engine_vulkan_unavailable",
    "multiple CUDA devices match the exact configured name": "engine_cuda_ambiguous",
    "exact configured CUDA device is unavailable": "engine_cuda_unavailable",
    "model validation lease is missing, mismatched, or stale": "engine_model_lease_invalid",
    "model validation lease became stale before backend load": "engine_model_lease_invalid",
    "llama model load failed": "engine_model_load_failed",
    "model identity changed during backend load": "engine_model_identity_changed",
    "llama chat template unavailable; raw prompt mode is not accepted": "engine_template_unavailable",
    "llama chat template parse failed": "engine_template_parse_failed",
    "llama chat template is not loaded": "engine_template_unavailable",
    "invalid tool parameter schema": "engine_template_invalid",
    "tool parameter schema must be an object": "engine_template_invalid",
    "llama chat template rendered an empty prompt": "engine_template_invalid",
    "llama context creation failed": "engine_context_failed",
    "llama sampler creation failed": "engine_sampler_failed",
    "llama backend is not initialized": "engine_startup_failed",
    "real backend disabled; configure LAE_ENABLE_LLAMA_CPP=ON": "engine_startup_failed",
    "real backend disabled": "engine_startup_failed",
    "server already started": "engine_socket_failed",
    "winsock initialization failed": "engine_socket_failed",
    "socket creation failed": "engine_socket_failed",
    "loopback bind/listen failed": "engine_bind_failed",
    "engine initialization failed": "engine_startup_failed",
    "server start failed": "engine_startup_failed",
    "generation failed": "engine_generation_failed",
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


def _classify_serve_eof(process: Any, stderr_tail: object = None, deadline: float | None = None) -> EngineStartupFailure:
    """Classify a finite ready-stream EOF without retaining child diagnostics."""

    child_status = _reap_child_status(process, deadline)
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
                if data[newline + 1:]:
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



# Required run-identity binding.  The orchestrator uploads this file next to the
# uploaded config before the first receipt-producing command; the path is
# source-fixed on both sides so no caller, configuration value, or remote
# response can redirect it.  Every receipt this script publishes carries the
# binding, and the salvage transport refuses any receipt whose binding is
# missing or does not match the run that is fetching it -- that is what stops a
# receipt left behind by an earlier run being published as this run's evidence.
_RUN_IDENTITY_PATH = Path("/scratch/j1m/run-identity.json")
_RUN_IDENTITY_SCHEMA = "local_bmo.j1m.run-identity.v1"
_RUN_IDENTITY_FIELDS = ("run_id", "instance_id")
_RUN_IDENTITY_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _without_run_identity(payload: dict[str, Any]) -> set[str]:
    """Return a receipt's key set with the run-identity binding removed.

    Every host-side receipt has carried `run_id` and `instance_id` since the
    run-identity slice, but the exact-key-set checks below were written before
    it and were never updated. `cuda-device-receipt.json` and
    `toolchain-receipt.json` are written by sibling probes in THIS run and both
    gained the two fields, so `set(payload) != {...}` failed on receipts that
    were perfectly valid: run j1m-eval-20260912-b reached the last of 16 eval
    stages, with the model rebuilt and hash-verified and CUDA verified, and
    then refused its own CUDA receipt as `cuda_device_receipt_invalid`.

    The orchestrator already resolves this the same way
    (`j1m_orchestrator._without_run_identity`): the binding is REQUIRED at the
    salvage fetch boundary, where an untrusted receipt is first published, and
    accepted by verifiers downstream of that proof. Strictness is otherwise
    unchanged -- any key that is not part of the binding still fails.
    """

    return set(payload) - set(_RUN_IDENTITY_FIELDS)


def _run_identity() -> dict[str, str]:
    """Return this run's receipt binding, or ``unbound`` when unprovable."""

    unbound = {field: "unbound" for field in _RUN_IDENTITY_FIELDS}
    try:
        raw = _RUN_IDENTITY_PATH.read_bytes()
        if len(raw) > 4096:
            return unbound
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return unbound
    if not isinstance(payload, dict) or payload.get("schema") != _RUN_IDENTITY_SCHEMA:
        return unbound
    resolved = {}
    for field in _RUN_IDENTITY_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not _RUN_IDENTITY_VALUE.match(value):
            return unbound
        resolved[field] = value
    return resolved


# Host-side receipts are fetched by the bounded salvage transport, which
# refuses anything that is not an owner-private single-link regular file
# (``salvage_not_private_regular_file``).  Run ``j1m-eval-20260911-remote-d``
# published its probe receipt ``0644`` under the image's default ``umask 022``
# and the salvage refused it, correctly, at teardown.  The mode is therefore
# set explicitly here instead of being inherited from whatever umask the
# remote shell happened to carry, and the parent directory is made ``0700`` so
# no other account can observe or replace a receipt between publication and
# fetch.  Publication stays atomic: a private temporary file in the same
# directory, fsynced, then ``os.replace``d over the final name.
def _publish_private_receipt(output: Path, encoded: bytes) -> None:
    """Atomically publish one receipt as a 0600 file in a 0700 directory."""

    output.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(output.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=os.fspath(output.parent))
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


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
    elif status in {"not_started", "rejected", "timeout", "oversize", "terminated", "failed"}:
        expected_not_started = "engine_model_preflight_not_started"
        if status == "not_started" and error_code != expected_not_started:
            raise ValueError("engine_model_preflight_invalid")
        if status != "not_started" and error_code not in {
            "engine_model_preflight_failed", "engine_model_preflight_invalid", "engine_model_preflight_exit",
            "engine_model_preflight_output_too_large", "engine_model_preflight_timeout", "engine_model_preflight_terminated_by_signal",
        }:
            raise ValueError("engine_model_preflight_invalid")
        summary = {"status": status}
        if error_code in SAFE_ERROR_CODES:
            summary["error_code"] = error_code
        if validator_code in MODEL_VALIDATOR_CODES and validator_code != "ok":
            summary["validator_code"] = validator_code
        valid_child = False
        if isinstance(child_status, dict) and set(child_status) in ({"exit_code"}, {"signal"}):
            value = next(iter(child_status.values()))
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255 and ("signal" not in child_status or value >= 1):
                summary["child"] = {next(iter(child_status)): value}
                valid_child = True
        if status in {"rejected", "failed"} and not valid_child:
            raise ValueError("engine_model_preflight_invalid")
        if status == "terminated" and (not valid_child or "signal" not in summary.get("child", {})):
            raise ValueError("engine_model_preflight_invalid")
    else:
        raise ValueError("engine_model_preflight_invalid")
    payload = {"schema": MODEL_PREFLIGHT_RECEIPT_SCHEMA, **summary, **_run_identity()}
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_PREFLIGHT_RECEIPT_BYTES:
        raise ValueError("engine_model_preflight_invalid")
    _publish_private_receipt(path, encoded)
    return summary


def _pin(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or set(value) - PIN_RE:
        raise ValueError(f"{label}_pin_invalid")
    return value


def _safe_error_code(error: BaseException) -> str:
    """Retain a stable reason without persisting dynamic exception detail."""

    candidate = str(error).partition(":")[0]
    return candidate if candidate in SAFE_ERROR_CODES else "evaluation_failed"


def verify_artifact(model: Path, manifest_path: Path, *, source_revision: str, llama_revision: str, manifest_lock_path: Path | None = None, deadline: float | None = None) -> dict[str, Any]:
    _deadline_check(deadline, "q4_artifact_hash_mismatch")
    if model.name != MODEL_NAME or not model.is_file():
        raise ValueError("q4_artifact_missing_or_wrong_name")
    source_revision = _pin(source_revision, "source")
    llama_revision = _pin(llama_revision, "llama")
    manifest_lock_path = manifest_lock_path or manifest_path.with_name("model-manifest.sha256")
    lock_parts = _read_bounded(manifest_lock_path, 4096, deadline=deadline, error_code="model_manifest_lock_invalid").decode("utf-8").strip().split()
    if len(lock_parts) != 2 or lock_parts[1] != manifest_path.name or len(lock_parts[0]) != 64 or any(character not in PIN_RE for character in lock_parts[0]):
        raise ValueError("model_manifest_lock_invalid")
    manifest_bytes = _read_bounded(manifest_path, MAX_METADATA_BYTES, deadline=deadline, error_code="model_manifest_invalid")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_digest != lock_parts[0]:
        raise ValueError("model_manifest_lock_mismatch")
    try:
        manifest = _strict_json_object(manifest_bytes)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("model_manifest_invalid") from exc
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
    _deadline_check(deadline, "q4_artifact_hash_mismatch")
    size = model.stat().st_size
    digest = sha256(model, deadline=deadline)
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


def _resolve_token_file(args: Any) -> tuple[Path, Path | None]:
    """Return the bearer-token path, creating a private directory if needed.

    A caller-supplied ``--token-file`` is honoured unchanged.  Otherwise the
    token lives in a ``0700`` directory owned by this process, so it never
    lands in the salvageable artifact tree and no credential path has to
    travel through a persisted command line.
    """

    if getattr(args, "token_file", ""):
        return Path(args.token_file), None
    directory = Path(tempfile.mkdtemp(prefix="lae-engine-token-"))
    os.chmod(directory, 0o700)
    return directory / "engine-token", directory


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
        payload = _strict_json_object(result.get("stdout", ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
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
    if error_code == "engine_model_preflight_not_started":
        return "not_started"
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
            payload = _strict_json_object(result.get("stdout", ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
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
        # No result means the child was never successfully observed. Preserve
        # the explicit typed no-subprocess outcome rather than fabricating a
        # failed child receipt without exit/signal evidence.
        if result is None:
            error_code = "engine_model_preflight_not_started"
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


def _engine_launch_argv(args: argparse.Namespace, token_file: Path, backend: str, context_tokens: int = 2048) -> list[str]:
    launch = [
        os.fspath(args.engine), "serve", "--port", "0", "--backend", backend,
        "--model", args.model, "--context", str(context_tokens),
        "--token-file", os.fspath(token_file),
    ]
    if backend == "cuda":
        launch.extend(["--gpu-layers", "99", "--cuda-device-name", args.cuda_device_name])
    return launch


def _fixture_contract(path: Path, *, deadline: float | None = None) -> dict[str, Any]:
    """Read the bounded contract; max_cases caps actual cases, never pads them."""

    try:
        raw = _read_bounded(path, FIXTURE_MAX_BYTES, deadline=deadline, error_code="evaluator_fixture_unreadable")
    except ValueError as exc:
        if str(exc) == "evaluator_fixture_unreadable" and path.exists() and path.stat().st_size > FIXTURE_MAX_BYTES:
            raise ValueError("evaluator_fixture_too_large") from exc
        raise
    try:
        fixture = _strict_json_object(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("evaluator_fixture_invalid") from exc
    if (not isinstance(fixture, dict) or set(fixture) != {"schema", "model", "protocol", "limits", "tools", "cases"} or
            fixture.get("schema") != "local_bmo.tool-call-eval.v1" or fixture.get("model") != "Qwen3.5-9B-Q4_K_M" or
            fixture.get("protocol") != "qwen35-xml-tool-call-v1" or not isinstance(fixture.get("limits"), dict) or
            not isinstance(fixture.get("tools"), list) or not isinstance(fixture.get("cases"), list)):
        raise ValueError("evaluator_fixture_invalid")
    limits = fixture.get("limits")
    if set(limits) != {"context_tokens", "max_output_tokens", "temperature", "max_cases"}:
        raise ValueError("evaluator_fixture_invalid")
    count = limits.get("max_cases") if isinstance(limits, dict) else None
    cases = fixture["cases"]
    tools = fixture.get("tools")
    context_tokens = limits.get("context_tokens") if isinstance(limits, dict) else None
    output_reserve_tokens = limits.get("max_output_tokens") if isinstance(limits, dict) else None
    tool_names = [tool.get("function", {}).get("name") if isinstance(tool, dict) and isinstance(tool.get("function"), dict) else None for tool in tools]
    case_count = len(cases) if isinstance(cases, list) else 0
    if (isinstance(count, bool) or not isinstance(count, int) or not 1 <= case_count <= count <= MAX_EVAL_CASES or
            not isinstance(tools, list) or not 1 <= len(tools) <= MAX_EVAL_TOOLS or isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or not 1 <= context_tokens <= 16384 or
            isinstance(output_reserve_tokens, bool) or not isinstance(output_reserve_tokens, int) or not 1 <= output_reserve_tokens <= 256 or output_reserve_tokens >= context_tokens or
            isinstance(limits.get("temperature"), bool) or not isinstance(limits.get("temperature"), (int, float)) or not math.isfinite(limits.get("temperature")) or not 0 <= limits["temperature"] <= 2 or
            any(not isinstance(name, str) or not TOOL_NAME.fullmatch(name) for name in tool_names) or len(set(tool_names)) != len(tool_names)):
        raise ValueError("evaluator_fixture_count_invalid")
    if any(not isinstance(case, dict) or set(case) != {"id", "category", "messages", "expected"} or
           not isinstance(case.get("id"), str) or not 1 <= len(case["id"]) <= 128 or
           not isinstance(case.get("category"), str) or not 1 <= len(case["category"]) <= 64 or
           not isinstance(case.get("messages"), list) or not isinstance(case.get("expected"), dict)
           for case in cases):
        raise ValueError("evaluator_fixture_invalid")
    categories = {case["category"] for case in cases}
    if len(categories) == 0 or any(not category.isascii() for category in categories):
        raise ValueError("evaluator_fixture_categories_invalid")
    category_counts = {category: sum(case["category"] == category for case in cases) for category in categories}
    fixture_identity = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema": fixture["schema"],
        "model": fixture["model"],
        "protocol": fixture["protocol"],
        "limits": dict(limits),
        "tool_names": list(tool_names),
        "tool_count": len(tools),
        "case_count": case_count,
        "category_counts": dict(sorted(category_counts.items())),
    }
    return {"case_count": case_count, "categories": categories, "category_counts": category_counts,
            "fixture_identity": fixture_identity,
            "tool_count": len(tools), "context_tokens": context_tokens,
            "output_reserve_tokens": output_reserve_tokens}


def _validate_diagnostics(value: Any, *, expected_errors: int, expected_categories: set[str], category_errors: dict[str, int]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "total_errors", "overall", "by_category"} or value.get("schema") != "local_bmo.tool-call-eval-diagnostics.v1":
        raise ValueError("evaluator_diagnostics_invalid")
    total = value.get("total_errors")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != expected_errors:
        raise ValueError("evaluator_diagnostics_total_invalid")
    overall = value.get("overall")
    by_category = value.get("by_category")
    if not isinstance(overall, dict) or not isinstance(by_category, dict) or set(by_category) != expected_categories:
        raise ValueError("evaluator_diagnostics_invalid")
    def check_histogram(histogram: Any) -> int:
        if not isinstance(histogram, dict):
            raise ValueError("evaluator_diagnostics_invalid")
        count = 0
        for code, amount in histogram.items():
            if code not in EVAL_DIAGNOSTIC_CODES or isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > MAX_EVAL_CASES:
                raise ValueError("evaluator_diagnostics_code_invalid")
            count += amount
        return count
    if check_histogram(overall) != expected_errors:
        raise ValueError("evaluator_diagnostics_total_invalid")
    category_total = 0
    combined: dict[str, int] = {}
    for category in expected_categories:
        category_histogram = by_category[category]
        category_count = check_histogram(category_histogram)
        if category_count != category_errors[category]:
            raise ValueError("evaluator_diagnostics_total_invalid")
        category_total += category_count
        for code, amount in category_histogram.items():
            combined[code] = combined.get(code, 0) + amount
    if category_total != expected_errors:
        raise ValueError("evaluator_diagnostics_total_invalid")
    if combined != overall:
        raise ValueError("evaluator_diagnostics_total_invalid")
    return {"schema": value["schema"], "total_errors": total, "overall": dict(overall), "by_category": {category: dict(by_category[category]) for category in sorted(expected_categories)}}


def _validate_quality_diagnostics(value: Any, *, expected_failed: int, expected_categories: set[str], category_failed: dict[str, int]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "total_failed", "overall", "by_category"} or value.get("schema") != "local_bmo.tool-call-quality-diagnostics.v1":
        raise ValueError("evaluator_quality_diagnostics_invalid")
    total = value.get("total_failed")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != expected_failed:
        raise ValueError("evaluator_quality_diagnostics_total_invalid")
    overall = value.get("overall")
    by_category = value.get("by_category")
    if not isinstance(overall, dict) or not isinstance(by_category, dict) or set(by_category) != expected_categories:
        raise ValueError("evaluator_quality_diagnostics_invalid")
    def histogram(item: Any) -> int:
        if not isinstance(item, dict):
            raise ValueError("evaluator_quality_diagnostics_invalid")
        count = 0
        for code, amount in item.items():
            if code not in EVAL_QUALITY_CODES or isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > MAX_EVAL_CASES:
                raise ValueError("evaluator_quality_diagnostics_code_invalid")
            count += amount
        return count
    if histogram(overall) != expected_failed:
        raise ValueError("evaluator_quality_diagnostics_total_invalid")
    combined: dict[str, int] = {}
    category_total = 0
    for category in expected_categories:
        category_histogram = by_category[category]
        count = histogram(category_histogram)
        if count != category_failed[category]:
            raise ValueError("evaluator_quality_diagnostics_total_invalid")
        category_total += count
        for code, amount in category_histogram.items():
            combined[code] = combined.get(code, 0) + amount
    if category_total != expected_failed or combined != overall:
        raise ValueError("evaluator_quality_diagnostics_total_invalid")
    return {"schema": value["schema"], "total_failed": total, "overall": dict(overall), "by_category": {category: dict(by_category[category]) for category in sorted(expected_categories)}}


def _validate_canary(value: Any, *, expected_tool_count: int = 11, expected_context_tokens: int = 2048, expected_output_reserve_tokens: int = 64) -> dict[str, Any]:
    if (isinstance(expected_output_reserve_tokens, bool) or not isinstance(expected_output_reserve_tokens, int) or
            not 1 <= expected_output_reserve_tokens <= MAX_OUTPUT_RESERVE_TOKENS):
        raise ValueError("evaluator_canary_invalid")
    if not isinstance(value, dict) or set(value) != {"attempted", "passed", "error_code", "tool_count", "message_chars", "prompt_tokens", "context_tokens", "output_reserve_tokens"} or value.get("attempted") is not True:
        raise ValueError("evaluator_canary_invalid")
    if (not isinstance(value.get("passed"), bool) or value.get("tool_count") != expected_tool_count or
            value.get("message_chars") != 2400 or value.get("context_tokens") != expected_context_tokens or
            value.get("output_reserve_tokens") != expected_output_reserve_tokens):
        raise ValueError("evaluator_canary_invalid")
    code = value.get("error_code")
    prompt_tokens = value.get("prompt_tokens")
    if value["passed"] and (code is not None or isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or not 513 <= prompt_tokens <= value["context_tokens"] - value["output_reserve_tokens"]):
        raise ValueError("evaluator_canary_invalid")
    if not value["passed"] and (code not in EVAL_DIAGNOSTIC_CODES or prompt_tokens is not None):
        raise ValueError("evaluator_canary_invalid")
    return dict(value)


def _validate_canary_coherence(canary: dict[str, Any], *, metrics: dict[str, Any], summary: dict[str, Any], diagnostics: dict[str, Any], expected_case_count: int, expected_categories: set[str]) -> None:
    if canary["passed"]:
        return
    code = canary["error_code"]
    if metrics["passed"] != 0 or metrics["failed"] != 0 or metrics["errors"] != expected_case_count:
        raise ValueError("evaluator_canary_invalid")
    for category in expected_categories:
        item = summary[category]
        if item["passed"] != 0 or item["failed"] != 0 or item["errors"] != item["case_count"] or diagnostics["by_category"][category] != {code: item["errors"]}:
            raise ValueError("evaluator_canary_invalid")
    if diagnostics["overall"] != {code: expected_case_count}:
        raise ValueError("evaluator_canary_invalid")


def _validate_failed_cases(value: Any, *, expected_failed: int, expected_errors: int, expected_categories: set[str]) -> list[dict[str, Any]]:
    """Accept the bounded attribution of WHICH cases did not pass.

    Deliberately narrow: the fixture's own case id, its category, and a finite
    reason code. Anything else in an entry is refused, so this can never become
    a channel for prompt or response text. The count must equal failed+errors,
    which ties the attribution to the aggregate it explains.
    """

    if not isinstance(value, list) or len(value) != expected_failed + expected_errors or len(value) > MAX_EVAL_CASES:
        raise ValueError("evaluator_failed_cases_invalid")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "category", "reason"}:
            raise ValueError("evaluator_failed_cases_invalid")
        case_id = item["id"]
        if not isinstance(case_id, str) or not 1 <= len(case_id) <= 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", case_id) or case_id in seen:
            raise ValueError("evaluator_failed_cases_invalid")
        seen.add(case_id)
        if expected_categories and item["category"] not in expected_categories:
            raise ValueError("evaluator_failed_cases_invalid")
        if item["reason"] not in EVAL_QUALITY_CODES and item["reason"] not in EVAL_DIAGNOSTIC_CODES:
            raise ValueError("evaluator_failed_cases_invalid")
    return [dict(item) for item in value]


def _validate_metrics(metrics: Any, *, expected_case_count: int = 8, expected_categories: set[str] | None = None, expected_category_counts: dict[str, int] | None = None, expected_tool_count: int = 11, expected_context_tokens: int = 2048, expected_output_reserve_tokens: int = 64, require_diagnostics: bool = False) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        raise ValueError("evaluator_metrics_invalid")
    counts = ("case_count", "passed", "failed", "errors")
    # `failed_cases` is optional so a receipt written before the attribution
    # change, or by an older evaluator, is not retroactively invalid. Any OTHER
    # unexpected key is still refused.
    if require_diagnostics and set(metrics) - {"failed_cases"} != set(counts) | {"peak_rss_kib", "category_summary", "canary", "error_diagnostics", "quality_diagnostics"}:
        raise ValueError("evaluator_metrics_invalid")
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
            if expected_category_counts is not None and (set(expected_category_counts) != expected_categories or item["case_count"] != expected_category_counts.get(category)):
                raise ValueError("evaluator_category_summary_invalid")
            if item["passed"] + item["failed"] + item["errors"] != item["case_count"]:
                raise ValueError("evaluator_category_summary_invalid")
            category_total += item["case_count"]
        if category_total != expected_case_count:
            raise ValueError("evaluator_category_summary_total_invalid")
        if any(sum(summary[category][field] for category in expected_categories) != metrics[field] for field in ("case_count", "passed", "failed", "errors")):
            raise ValueError("evaluator_metrics_total_invalid")
    result = {key: metrics.get(key) for key in (*counts, "peak_rss_kib")} | ({"category_summary": summary} if summary is not None else {})
    if require_diagnostics:
        if "error_diagnostics" not in metrics or "canary" not in metrics or "quality_diagnostics" not in metrics:
            raise ValueError("evaluator_diagnostics_invalid")
        result["error_diagnostics"] = _validate_diagnostics(metrics["error_diagnostics"], expected_errors=metrics["errors"], expected_categories=expected_categories or set(), category_errors={category: summary[category]["errors"] for category in (expected_categories or set())})
        result["canary"] = _validate_canary(metrics["canary"], expected_tool_count=expected_tool_count, expected_context_tokens=expected_context_tokens, expected_output_reserve_tokens=expected_output_reserve_tokens)
        _validate_canary_coherence(result["canary"], metrics=metrics, summary=summary, diagnostics=result["error_diagnostics"], expected_case_count=expected_case_count, expected_categories=expected_categories or set())
        result["quality_diagnostics"] = _validate_quality_diagnostics(metrics["quality_diagnostics"], expected_failed=metrics["failed"], expected_categories=expected_categories or set(), category_failed={category: summary[category]["failed"] for category in (expected_categories or set())})
        if "failed_cases" in metrics:
            result["failed_cases"] = _validate_failed_cases(metrics["failed_cases"], expected_failed=metrics["failed"], expected_errors=metrics["errors"], expected_categories=expected_categories or set())
    elif "error_diagnostics" in metrics or "canary" in metrics or "quality_diagnostics" in metrics:
        if "error_diagnostics" in metrics:
            result["error_diagnostics"] = _validate_diagnostics(metrics["error_diagnostics"], expected_errors=metrics["errors"], expected_categories=expected_categories or set(), category_errors={category: summary[category]["errors"] for category in (expected_categories or set())})
        if "canary" in metrics:
            result["canary"] = _validate_canary(metrics["canary"], expected_tool_count=expected_tool_count, expected_context_tokens=expected_context_tokens, expected_output_reserve_tokens=expected_output_reserve_tokens)
        if "quality_diagnostics" in metrics:
            result["quality_diagnostics"] = _validate_quality_diagnostics(metrics["quality_diagnostics"], expected_failed=metrics["failed"], expected_categories=expected_categories or set(), category_failed={category: summary[category]["failed"] for category in (expected_categories or set())})
    return result


def _parse_evaluator_result(result: dict[str, Any], *, expected_case_count: int, expected_categories: set[str], expected_category_counts: dict[str, int] | None = None, expected_tool_count: int = 11, expected_context_tokens: int = 2048, expected_output_reserve_tokens: int = 64, require_diagnostics: bool = False) -> tuple[dict[str, Any], bool, bool]:
    exit_code = result.get("exit_code")
    if result.get("status") not in {"completed", "failed"} or isinstance(exit_code, bool) or exit_code not in {0, 1}:
        raise ValueError("evaluator_process_failed")
    try:
        metrics = _strict_json_object(result.get("stdout", ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("evaluator_receipt_invalid") from exc
    metrics = _validate_metrics(metrics, expected_case_count=expected_case_count, expected_categories=expected_categories, expected_category_counts=expected_category_counts, expected_tool_count=expected_tool_count, expected_context_tokens=expected_context_tokens, expected_output_reserve_tokens=expected_output_reserve_tokens, require_diagnostics=require_diagnostics)
    all_passed = metrics["passed"] == expected_case_count and metrics["failed"] == 0 and metrics["errors"] == 0
    has_failure = metrics["failed"] > 0 or metrics["errors"] > 0
    if (exit_code == 0) != all_passed or (exit_code == 1) != has_failure:
        raise ValueError("evaluator_exit_status_mismatch")
    return metrics, all_passed, has_failure


def _launch_and_evaluate(args: argparse.Namespace, artifact: dict[str, Any], deadline: float | None = None, *,
                         extra_evaluator_args: tuple[str, ...] = (), session: Any = None) -> dict[str, Any]:
    """Start the engine once, score the fixture, and always stop the engine.

    ``extra_evaluator_args`` and ``session`` exist only for the opt-in suite
    members; both default to doing nothing, which is the shipping path. A
    ``session`` replaces the evaluator call with a different client of the SAME
    running engine (the long-context harness) and returns its own receipt; the
    engine and its bearer are torn down by the ``finally`` below either way.
    """

    started = time.monotonic()
    deadline = deadline if deadline is not None else started + float(getattr(args, "timeout", EVAL_TOTAL_TIMEOUT))
    engine = Path(args.engine)
    if not engine.is_file():
        raise ValueError("engine_binary_missing")
    checkout = Path(args.llama_checkout)
    rev = _run_bounded(["git", "-C", os.fspath(checkout), "rev-parse", "HEAD"], timeout=_stage_timeout(deadline, 30.0, "llama_checkout_revision_mismatch"), output_limit=128)
    if rev.get("status") != "completed" or rev.get("stdout", "").strip() != artifact["llama_cpp_revision"]:
        raise ValueError("llama_checkout_revision_mismatch")
    backend = getattr(args, "backend", "cpu")
    if backend not in {"cpu", "cuda"}:
        raise ValueError("evaluation_backend_invalid")
    fixture_contract = _fixture_contract(Path(args.fixture), deadline=deadline)
    expected_case_count = fixture_contract["case_count"]
    expected_categories = fixture_contract["categories"]
    expected_category_counts = fixture_contract["category_counts"]
    expected_tool_count = fixture_contract["tool_count"]
    expected_context_tokens = fixture_contract["context_tokens"]
    expected_output_reserve_tokens = fixture_contract["output_reserve_tokens"]
    cuda_receipt = None
    if backend == "cuda":
        receipt_path = Path(getattr(args, "cuda_device_receipt", ""))
        try:
            cuda_receipt = _strict_json_object(_read_bounded(receipt_path, MAX_METADATA_BYTES, deadline=deadline, error_code="cuda_device_receipt_invalid"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("cuda_device_receipt_invalid") from exc
        device = cuda_receipt.get("device") if isinstance(cuda_receipt, dict) else None
        if (not isinstance(cuda_receipt, dict) or _without_run_identity(cuda_receipt) != {"schema", "status", "selector", "device_count", "device", "source"} or
                cuda_receipt.get("schema") != "local_bmo.j1m.cuda-device-receipt.v1" or cuda_receipt.get("status") != "verified" or cuda_receipt.get("selector") != getattr(args, "cuda_device_name", "") or cuda_receipt.get("device_count") != 1 or cuda_receipt.get("source") != "nvidia-smi bounded query" or
                not isinstance(device, dict) or set(device) != {"index", "name", "memory_total_mib", "driver_version"} or "a100" not in str(device.get("name", "")).lower() or not isinstance(device.get("index"), int) or isinstance(device.get("index"), bool) or device["index"] < 0 or not isinstance(device.get("name"), str) or not 1 <= len(device["name"]) <= 160 or not isinstance(device.get("driver_version"), str) or not 1 <= len(device["driver_version"]) <= 80 or not isinstance(device.get("memory_total_mib"), int) or device["memory_total_mib"] < 70000):
            raise ValueError("cuda_device_receipt_invalid")
    try:
        toolchain = _strict_json_object(_read_bounded(Path(args.toolchain_receipt), MAX_METADATA_BYTES, deadline=deadline, error_code="toolchain_receipt_invalid"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("toolchain_receipt_invalid") from exc
    versions = toolchain.get("versions") if isinstance(toolchain, dict) else None
    minimums = {"python3": (3, 8), "git": (2, 30), "cmake": (3, 18), "g++": (9, 0), "nvcc": (12, 0)}
    if (not isinstance(toolchain, dict) or toolchain.get("schema") != "local_bmo.j1m.remote-toolchain-receipt.v1" or
            toolchain.get("status") != "verified" or _without_run_identity(toolchain) != {"schema", "status", "required", "versions", "packages", "package_install"} or
            not isinstance(versions, dict) or set(versions) != set(minimums) or
            toolchain.get("required") != {name: f">={major}.{minor}" for name, (major, minor) in minimums.items()} or
            toolchain.get("package_install") != "ubuntu apt repositories; exact resolved package versions captured by dpkg-query"):
        raise ValueError("toolchain_receipt_invalid")
    for name, minimum in minimums.items():
        version = versions.get(name)
        if (not isinstance(version, dict) or set(version) != {"major", "minor", "reported", "executable"} or
                isinstance(version.get("major"), bool) or not isinstance(version.get("major"), int) or isinstance(version.get("minor"), bool) or not isinstance(version.get("minor"), int) or (version["major"], version["minor"]) < minimum):
            raise ValueError("toolchain_receipt_invalid")
    if versions["nvcc"].get("executable") != "/usr/local/cuda/bin/nvcc":
        raise ValueError("toolchain_receipt_invalid")
    packages = toolchain.get("packages")
    expected_packages = {"ca-certificates", "cmake", "build-essential", "git", "python3", "python3-venv"}
    if not isinstance(packages, dict) or set(packages) != expected_packages or any(not isinstance(value, str) or not value or len(value) > 160 or any(ord(char) < 0x20 for char in value) for value in packages.values()):
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
    token_file, token_directory = _resolve_token_file(args)
    _write_token(token_file)
    process: subprocess.Popen[str] | None = None
    engine_stderr: Any = None
    try:
        # The protected token-file option is deliberately explicit.  Passing
        # a bearer as a command-line argument would expose it through process
        # inspection and is forbidden by the evaluation contract.
        launch = _engine_launch_argv(args, token_file, backend, expected_context_tokens)
        engine_stderr = tempfile.TemporaryFile()
        process = subprocess.Popen(launch, stdout=subprocess.PIPE, stderr=engine_stderr, text=False)
        if process.stdout is None:
            raise EngineStartupFailure("engine_stdout_unavailable", _bounded_child_status(process))
        try:
            ready_budget = _stage_timeout(deadline, 120.0, "engine_ready_timeout")
            ready_line = _read_ready_line(process.stdout, time.monotonic() + ready_budget)
        except ValueError as exc:
            code = str(exc)
            if code not in SAFE_ERROR_CODES or not code.startswith("engine_ready_"):
                code = "engine_ready_receipt_invalid"
            raise EngineStartupFailure(code, _reap_child_status(process, deadline)) from exc
        if ready_line is None:
            raise _classify_serve_eof(process, _file_tail(engine_stderr), deadline)
        try:
            ready = _strict_json_object(ready_line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EngineStartupFailure("engine_ready_receipt_invalid", _reap_child_status(process, deadline)) from exc
        port = ready.get("port") if isinstance(ready, dict) else None
        if (not isinstance(ready, dict) or set(ready) != {"event", "port", "bind", "token_required"} or
                ready.get("event") != "ready" or ready.get("token_required") is not True or
                isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or ready.get("bind") != "127.0.0.1"):
            raise EngineStartupFailure("engine_ready_identity_invalid", _reap_child_status(process, deadline))
        if session is not None:
            return session(
                args=args, artifact=artifact, port=port, token_file=token_file,
                process=process, deadline=deadline, started=started,
                fixture_identity=fixture_contract["fixture_identity"],
                context_tokens=expected_context_tokens, build_info=build_info,
                model_preflight={**model_preflight, **preflight_summary},
            )
        evaluate = [
            sys.executable, args.evaluator, "--fixture", args.fixture,
            "--endpoint", f"http://127.0.0.1:{port}/v1/chat/completions",
            "--token-file", os.fspath(token_file), "--timeout", "120",
            "--max-cases", str(expected_case_count), "--engine-pid", str(process.pid),
        ]
        if extra_evaluator_args:
            evaluate.extend(extra_evaluator_args)
        evaluator_timeout = _stage_timeout(deadline, float(args.timeout), "evaluator_timeout_invalid")
        evaluate[evaluate.index("--timeout") + 1] = str(max(1, math.floor(evaluator_timeout)))
        result = _run_bounded(evaluate, timeout=evaluator_timeout, output_limit=MAX_EVAL_OUTPUT)
        metrics, all_passed, has_failure = _parse_evaluator_result(
            result,
            expected_case_count=expected_case_count,
            expected_categories=expected_categories,
            expected_category_counts=expected_category_counts,
            expected_tool_count=expected_tool_count,
            expected_context_tokens=expected_context_tokens,
            expected_output_reserve_tokens=expected_output_reserve_tokens,
            require_diagnostics=True,
        )
        child_status = _bounded_child_status(process)
        # Retain the already validated aggregate as failure evidence if the
        # engine disappeared after the evaluator printed it. A dead child can
        # never support either quality-completion status.
        status = "failed" if child_status is not None else "verified" if all_passed else "completed_with_failures" if has_failure else "failed"
        return {
            "schema": "local_bmo.j1m.real-tool-eval-receipt.v1",
            "status": status,
            "artifact": artifact,
            "fixture": fixture_contract["fixture_identity"],
            "engine": build_info,
            # Keep the original identity fields for receipt consumers while
            # adding the explicit verified status used by salvage.
            "model_preflight": {**model_preflight, **preflight_summary},
            **({"cuda_device": cuda_receipt} if cuda_receipt is not None else {}),
            "toolchain": toolchain,
            "metrics": {key: metrics[key] for key in ("case_count", "passed", "failed", "errors", "peak_rss_kib", "category_summary", "canary", "error_diagnostics", "quality_diagnostics", "failed_cases")},
            **({"child": child_status} if child_status is not None else {}),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "prompt_response_logging": False,
            "tokens_logged": False,
        }
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=max(0.0, min(15.0, deadline - time.monotonic())))
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=max(0.0, min(15.0, deadline - time.monotonic())))
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if process.stderr is not None:
                process.stderr.close()
            if process.stdout is not None:
                process.stdout.close()
        if engine_stderr is not None:
            engine_stderr.close()
        token_file.unlink(missing_ok=True)
        if token_directory is not None:
            try:
                os.rmdir(token_directory)
            except OSError:
                pass


def _run_with_stdin(command: list[str], stdin_bytes: bytes, *, timeout: float) -> dict[str, Any]:
    """Run one child with a secret on stdin only; never argv, never env."""

    environment = {key: value for key, value in os.environ.items() if key != "LAE_EVAL_TOKEN"}
    with tempfile.TemporaryFile() as stdout_log, tempfile.TemporaryFile() as stderr_log:
        try:
            result = subprocess.run(command, input=stdin_bytes, stdout=stdout_log, stderr=stderr_log,
                                    timeout=timeout, check=False, env=environment)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "exit_code": None}
        return {"status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode}


def _bounded_int_value(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _bounded_float_value(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        return None
    return round(float(value), 3)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return round(ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2, 3)


def _distill_long_context(raw: Any) -> dict[str, Any]:
    """Rebuild bounded aggregates from the harness receipt, field by field.

    The harness receipt already excludes prompts and replies; this goes
    further and copies NOTHING by reference. Every value below is recomputed
    from typed cell fields (identifiers matched by regex, outcomes from a
    closed set, numbers range-checked), so even a changed harness cannot route
    text into the salvageable receipt, and no key carries a credential-shaped
    name the persisted-receipt screen would refuse.
    """

    if not isinstance(raw, dict) or raw.get("schema") != "local_bmo.long-context-eval.v1" or raw.get("prompt_response_logging") is not False:
        raise ValueError("long_context_receipt_invalid")
    cells = raw.get("cells", [])
    planned = (len(LONG_CONTEXT_PROFILE["sizes"]) * len(LONG_CONTEXT_PROFILE["styles"]) *
               len(LONG_CONTEXT_PROFILE["depths"]) * len(LONG_CONTEXT_PROFILE["probes"]) * LONG_CONTEXT_PROFILE["trials"])
    if not isinstance(cells, list) or len(cells) > planned:
        raise ValueError("long_context_receipt_invalid")
    sizes = set(LONG_CONTEXT_PROFILE["sizes"])
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("long_context_receipt_invalid")
        cell_id, outcome, reason = cell.get("id"), cell.get("outcome"), cell.get("reason")
        size, depth = cell.get("target_tokens"), cell.get("depth")
        probe, style = cell.get("probe"), cell.get("style")
        if (not isinstance(cell_id, str) or not LONG_CONTEXT_CELL_ID.fullmatch(cell_id) or cell_id in seen or
                outcome not in LONG_CONTEXT_OUTCOMES or not isinstance(reason, str) or not LONG_CONTEXT_REASON.fullmatch(reason) or
                probe not in LONG_CONTEXT_PROFILE["probes"] or style not in LONG_CONTEXT_PROFILE["styles"] or
                size not in sizes or isinstance(size, bool) or depth not in LONG_CONTEXT_PROFILE["depths"]):
            raise ValueError("long_context_receipt_invalid")
        seen.add(cell_id)
        clean.append({
            "id": cell_id, "probe": probe, "style": style, "size": size, "depth": depth,
            "outcome": outcome, "reason": reason,
            "coherent": cell.get("coherent") if isinstance(cell.get("coherent"), bool) else None,
            "prompt_tokens": _bounded_int_value(cell.get("prompt_tokens"), 0, 1_000_000),
            "seconds": _bounded_float_value(cell.get("seconds"), 0.0, 86_400.0),
            "prefill": _bounded_float_value(cell.get("prefill_seconds_est"), 0.0, 86_400.0),
            "reused": _bounded_int_value(cell.get("reused_prefix_tokens"), 0, 1_000_000) or 0,
        })

    def bucket(key: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for item in clean:
            entry = out.setdefault(key(item), {outcome: 0 for outcome in LONG_CONTEXT_OUTCOMES})
            entry[item["outcome"]] += 1
        for entry in out.values():
            graded = entry["pass"] + entry["fail"]
            entry["accuracy"] = round(entry["pass"] / graded, 3) if graded else None
        return dict(sorted(out.items()))

    latency: dict[str, dict[str, Any]] = {}
    for size in sorted(sizes):
        answered = [item for item in clean if item["size"] == size and item["prompt_tokens"] is not None]
        if not answered:
            continue
        latency[str(size)] = {
            "cells": len(answered),
            "median_prompt_tokens": _median([float(item["prompt_tokens"]) for item in answered]),
            "median_seconds": _median([item["seconds"] for item in answered if item["seconds"] is not None]),
            "median_prefill_seconds_est": _median([item["prefill"] for item in answered if item["prefill"] is not None]),
            "max_reused_prefix": max(item["reused"] for item in answered),
            "incoherent": sum(1 for item in answered if item["coherent"] is False),
        }
    stopped = raw.get("stopped_early")
    stopped_early = None
    if isinstance(stopped, dict):
        stopped_early = {
            "reason": "failure_limit" if stopped.get("reason") == "failure_limit" else "other",
            "failures": _bounded_int_value(stopped.get("failures"), 0, planned),
            "cells_not_run": _bounded_int_value(stopped.get("cells_not_run"), 0, planned),
        }
    outcomes = {outcome: sum(1 for item in clean if item["outcome"] == outcome) for outcome in LONG_CONTEXT_OUTCOMES}
    return {
        "cells_planned": planned,
        "cells_run": len(clean),
        "complete": raw.get("complete") is True and len(clean) == planned and stopped_early is None,
        "stopped_early": stopped_early,
        "engine_context": _bounded_int_value(raw.get("engine_context_tokens"), 1, 16384),
        "outcomes": outcomes,
        "by_size": bucket(lambda item: str(item["size"])),
        "by_style_size": bucket(lambda item: f"{item['style']}@{item['size']}"),
        "by_depth_size": bucket(lambda item: f"d{item['depth']:g}@{item['size']}"),
        "by_probe_size": bucket(lambda item: f"{item['probe']}@{item['size']}"),
        "latency_by_size": latency,
        "tool_loop_reuse": {
            "cells": sum(1 for item in clean if item["style"] == "tool_loop" and item["prompt_tokens"] is not None),
            "reused": sum(1 for item in clean if item["style"] == "tool_loop" and item["reused"] > 0),
        },
        "not_passed": [{"id": item["id"], "outcome": item["outcome"], "reason": item["reason"]}
                       for item in clean if item["outcome"] != "pass"],
    }


def _long_context_session(*, args: argparse.Namespace, artifact: dict[str, Any], port: int, token_file: Path,
                          process: Any, deadline: float, started: float, fixture_identity: dict[str, Any],
                          context_tokens: int, build_info: dict[str, Any],
                          model_preflight: dict[str, Any]) -> dict[str, Any]:
    """Drive ``long_context_eval.py`` against the engine ``_launch_and_evaluate`` started.

    The harness imports ``scripts.test.evaluate_tool_calls`` and reads the
    shipping fixture from ``tests/model``, so the already-uploaded evaluator and
    fixture are copied into that layout inside an owner-private directory of
    this process. The bearer goes to the child on stdin, never argv or env. Its
    raw receipt never leaves the private directory: only the distilled,
    recomputed aggregate below is returned for publication.
    """

    harness = Path(getattr(args, "long_context_harness", "") or "")
    if harness.name != "long_context_eval.py" or not harness.is_file():
        raise ValueError("long_context_harness_missing")
    harness_bytes = _read_bounded(harness, LONG_CONTEXT_HARNESS_MAX_BYTES, deadline=deadline, error_code="long_context_harness_missing")
    evaluator_bytes = _read_bounded(Path(args.evaluator), LONG_CONTEXT_HARNESS_MAX_BYTES, deadline=deadline, error_code="long_context_harness_missing")
    fixture_bytes = _read_bounded(Path(args.fixture), FIXTURE_MAX_BYTES, deadline=deadline, error_code="evaluator_fixture_unreadable")
    if hashlib.sha256(fixture_bytes).hexdigest() != fixture_identity.get("sha256"):
        raise ValueError("evaluator_fixture_identity_mismatch")
    workspace = Path(tempfile.mkdtemp(prefix="lae-long-context-"))
    try:
        os.chmod(workspace, 0o700)
        tree = workspace / "tree"
        (tree / "scripts" / "test").mkdir(parents=True, mode=0o700)
        (tree / "tests" / "model").mkdir(parents=True, mode=0o700)
        (workspace / "out").mkdir(mode=0o700)
        entry = tree / "scripts" / "test" / "long_context_eval.py"
        entry.write_bytes(harness_bytes)
        (tree / "scripts" / "test" / "evaluate_tool_calls.py").write_bytes(evaluator_bytes)
        (tree / "tests" / "model" / "production_tool_call_eval.json").write_bytes(fixture_bytes)
        raw_path = workspace / "out" / "long-context-raw.json"
        profile = LONG_CONTEXT_PROFILE
        command = [
            sys.executable, os.fspath(entry),
            "--endpoint", f"http://127.0.0.1:{port}/v1/chat/completions",
            "--token-stdin", "--out", os.fspath(raw_path),
            "--sizes", ",".join(str(value) for value in profile["sizes"]),
            "--styles", ",".join(profile["styles"]),
            "--depths", ",".join(f"{value:g}" for value in profile["depths"]),
            "--trials", str(profile["trials"]),
            "--probes", ",".join(profile["probes"]),
            "--max-tokens", str(profile["max_output"]),
            "--timeout", str(profile["request_timeout_seconds"]),
            "--stop-after-failures", str(profile["stop_after_failures"]),
            "--context", str(context_tokens),
        ]
        bearer = token_file.read_bytes()
        harness_timeout = _stage_timeout(deadline, LONG_CONTEXT_TOTAL_TIMEOUT, "long_context_timeout")
        result = _run_with_stdin(command, bearer, timeout=harness_timeout)
        raw: Any = None
        if raw_path.is_file():
            try:
                raw = _strict_json_object(_read_bounded(raw_path, LONG_CONTEXT_RAW_MAX_BYTES, error_code="long_context_receipt_invalid"))
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("long_context_receipt_invalid") from exc
        if raw is None:
            raise ValueError("long_context_timeout" if result.get("status") == "timeout" else
                             "long_context_harness_failed" if result.get("status") == "failed" else
                             "long_context_receipt_missing")
        results = _distill_long_context(raw)
        child_status = _bounded_child_status(process)
        if child_status is not None:
            raise EngineStartupFailure("engine_exited_during_evaluation", child_status)
        complete = results["complete"] and result.get("status") == "completed"
        harness_outcome = {"status": result.get("status"), "exit_code": result.get("exit_code")}
        return {
            "schema": SUITE_MEMBER_SCHEMAS[LONG_CONTEXT_MEMBER],
            "status": "completed" if complete else "partial",
            "artifact": artifact,
            "fixture": fixture_identity,
            "engine": build_info,
            "model_preflight": model_preflight,
            "harness": {"schema": raw["schema"], "sha256": hashlib.sha256(harness_bytes).hexdigest(),
                        "process": harness_outcome},
            "plan": {key: (list(value) if isinstance(value, (list, tuple)) else value) for key, value in profile.items()},
            "results": results,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "prompt_response_logging": False,
            "tokens_logged": False,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _memory_planned_requests(profile: dict[str, Any]) -> int:
    questions = len(MEMORY_FACT_TYPES) + 2
    return profile["conversations"] * (profile["chunks"] * bool({"note", "both"} & set(profile["arms"])) + questions * len(profile["arms"]))


def _memory_age_bucket(age: int) -> str:
    for low, high in MEMORY_AGE_BUCKETS:
        if low <= age <= high:
            return f"{low}-{high}" if high < 1000 else f"{low}+"
    return "0"


def _distill_memory(raw: Any, *, prompts_sha256: str) -> dict[str, Any]:
    """Rebuild bounded aggregates from the memory harness receipt, field by field.

    As for long context, nothing is copied by reference: the harness receipt
    carries a key (``credential_masking``) the persisted-receipt screen would
    refuse, and its notes metadata is checked rather than trusted. Every value
    below is recomputed from typed cell fields; identifiers are matched by
    regex, outcomes and probes come from closed sets, numbers are
    range-checked. No note, prompt or model text can reach the result.
    """

    profile = MEMORY_PROFILE
    if (not isinstance(raw, dict) or raw.get("schema") != "local_bmo.memory-eval.v1" or
            raw.get("prompt_response_logging") is not False):
        raise ValueError("memory_receipt_invalid")
    prompts = raw.get("memory_prompts")
    if not isinstance(prompts, dict) or prompts.get("sha256") != prompts_sha256:
        raise ValueError("memory_prompts_identity_mismatch")
    plan = raw.get("plan")
    expected_plan = {
        "conversations": profile["conversations"], "arms": profile["arms"], "chunks": profile["chunks"],
        "dropped_tokens": profile["dropped_tokens"], "retained_tokens": profile["retained_tokens"],
        "note_tokens": profile["note_tokens"], "note_bytes": profile["note_bytes"],
        "max_input_bytes": profile["max_input_bytes"], "per_message_bytes": profile["per_message_bytes"],
        "max_tokens": profile["max_output"], "planned_requests": _memory_planned_requests(profile),
    }
    if not isinstance(plan, dict) or any(plan.get(key) != value for key, value in expected_plan.items()):
        raise ValueError("memory_receipt_invalid")
    conversations = raw.get("conversations", [])
    if not isinstance(conversations, list) or len(conversations) > profile["conversations"]:
        raise ValueError("memory_receipt_invalid")
    arms = set(profile["arms"])
    cells: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_conversation = (len(MEMORY_FACT_TYPES) + 2) * len(profile["arms"])
    complete_conversations = 0
    for conversation in conversations:
        if not isinstance(conversation, dict):
            raise ValueError("memory_receipt_invalid")
        cid = conversation.get("id")
        rows = conversation.get("cells")
        if (not isinstance(cid, str) or not MEMORY_CONVERSATION_ID.fullmatch(cid) or cid in seen or
                not isinstance(rows, list) or len(rows) > per_conversation):
            raise ValueError("memory_receipt_invalid")
        seen.add(cid)
        complete_conversations += conversation.get("complete") is True and len(rows) == per_conversation
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("memory_receipt_invalid")
            arm, probe, outcome, reason = row.get("arm"), row.get("probe"), row.get("outcome"), row.get("reason")
            age = row.get("age_turns")
            in_dropped = row.get("in_dropped")
            if (arm not in arms or probe not in MEMORY_PROBES or outcome not in MEMORY_OUTCOMES or
                    not isinstance(reason, str) or not LONG_CONTEXT_REASON.fullmatch(reason) or
                    (age is not None and _bounded_int_value(age, 0, 100_000) is None) or
                    (in_dropped is not None and not isinstance(in_dropped, bool))):
                raise ValueError("memory_receipt_invalid")
            cells.append({
                "conversation": cid, "arm": arm, "probe": probe, "outcome": outcome, "reason": reason,
                "age": age, "in_dropped": in_dropped is True,
                "coherent": row.get("coherent") if isinstance(row.get("coherent"), bool) else None,
                "prompt_tokens": _bounded_int_value(row.get("prompt_tokens"), 0, 1_000_000),
                "seconds": _bounded_float_value(row.get("seconds"), 0.0, 86_400.0),
            })
        note = conversation.get("note")
        if note is not None:
            if not isinstance(note, dict):
                raise ValueError("memory_receipt_invalid")
            facts = note.get("facts_in_note") if isinstance(note.get("facts_in_note"), dict) else {}
            notes.append({
                "ok": note.get("outcome") == "ok",
                "facts": {fact: facts.get(fact) is True for fact in MEMORY_FACT_TYPES},
                "stale": note.get("stale_value_in_note") is True,
                "markup_removed": note.get("markup_removed") is True,
                "truncated": note.get("truncated") is True,
                "bytes": _bounded_int_value(note.get("note_bytes"), 0, 1_000_000),
                "seconds": _bounded_float_value(note.get("seconds"), 0.0, 86_400.0),
            })

    def bucket(key: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for item in cells:
            name = key(item)
            if name is None:
                continue
            entry = out.setdefault(name, {"pass": 0, "fail": 0, "not_graded": 0})
            entry["pass" if item["outcome"] == "pass" else "fail" if item["outcome"] == "fail" else "not_graded"] += 1
        for entry in out.values():
            graded = entry["pass"] + entry["fail"]
            entry["accuracy"] = round(entry["pass"] / graded, 3) if graded else None
        return dict(sorted(out.items()))

    by_arm_probe = bucket(lambda item: f"{item['arm']}/{item['probe']}")
    by_arm = bucket(lambda item: item["arm"] if item["probe"] in MEMORY_FACT_TYPES else None)
    benefit = {}
    for probe in MEMORY_PROBES:
        note_accuracy = by_arm_probe.get(f"note/{probe}", {}).get("accuracy")
        drop_accuracy = by_arm_probe.get(f"drop/{probe}", {}).get("accuracy")
        benefit[probe] = (round(note_accuracy - drop_accuracy, 3)
                          if note_accuracy is not None and drop_accuracy is not None else None)
    ok_notes = [note for note in notes if note["ok"]]
    answered = [item for item in cells if item["prompt_tokens"] is not None]
    run = len(conversations)
    return {
        "conversations_planned": profile["conversations"],
        "conversations_run": run,
        "requests_planned": _memory_planned_requests(profile),
        "cells_run": len(cells),
        "complete": raw.get("complete") is True and run == profile["conversations"] and complete_conversations == run,
        "outcomes": {outcome: sum(1 for item in cells if item["outcome"] == outcome) for outcome in MEMORY_OUTCOMES},
        "by_arm": by_arm,
        "by_arm_probe": by_arm_probe,
        "by_arm_age": bucket(lambda item: f"{item['arm']}/{_memory_age_bucket(item['age'])}"
                             if item["in_dropped"] and item["age"] is not None else None),
        "note_minus_drop": benefit,
        "notes": {
            "generated": len(ok_notes), "failed": len(notes) - len(ok_notes),
            "facts_kept_by_type": {fact: sum(1 for note in ok_notes if note["facts"][fact]) for fact in MEMORY_FACT_TYPES},
            "stale_value_kept": sum(1 for note in ok_notes if note["stale"]),
            "markup_removed": sum(1 for note in ok_notes if note["markup_removed"]),
            "truncated": sum(1 for note in ok_notes if note["truncated"]),
            "median_note_bytes": _median([float(note["bytes"]) for note in ok_notes if note["bytes"] is not None]),
            "median_note_seconds": _median([note["seconds"] for note in ok_notes if note["seconds"] is not None]),
        },
        "latency": {
            "answered": len(answered),
            "median_prompt_tokens": _median([float(item["prompt_tokens"]) for item in answered]),
            "max_prompt_tokens": max((item["prompt_tokens"] for item in answered), default=None),
            "median_seconds": _median([item["seconds"] for item in answered if item["seconds"] is not None]),
            "incoherent": sum(1 for item in answered if item["coherent"] is False),
        },
        "not_passed": [{"conversation": item["conversation"], "arm": item["arm"], "probe": item["probe"],
                        "outcome": item["outcome"], "reason": item["reason"]}
                       for item in cells if item["outcome"] != "pass"],
    }


def _memory_session(*, args: argparse.Namespace, artifact: dict[str, Any], port: int, token_file: Path,
                    process: Any, deadline: float, started: float, fixture_identity: dict[str, Any],
                    context_tokens: int, build_info: dict[str, Any],
                    model_preflight: dict[str, Any]) -> dict[str, Any]:
    """Drive ``memory_eval.py`` against the engine ``_launch_and_evaluate`` started.

    ``memory_eval`` imports ``scripts.test.long_context_eval`` and
    ``scripts.test.evaluate_tool_calls`` and reads ``host/agent/memory-prompts.json``
    relative to its own tree, so the uploaded files are laid out that way in an
    owner-private directory of this process. The prompt file's digest was
    proved against the orchestrator's before the model was hashed, and is
    proved again here against the copy the harness actually reads.
    """

    harness = Path(getattr(args, "memory_harness", "") or "")
    long_context = Path(getattr(args, "long_context_harness", "") or "")
    prompts = Path(getattr(args, "memory_prompts", "") or "")
    if (harness.name != "memory_eval.py" or not harness.is_file() or long_context.name != "long_context_eval.py" or
            not long_context.is_file() or prompts.name != "memory-prompts.json" or not prompts.is_file()):
        raise ValueError("memory_harness_missing")
    harness_bytes = _read_bounded(harness, LONG_CONTEXT_HARNESS_MAX_BYTES, deadline=deadline, error_code="memory_harness_missing")
    long_context_bytes = _read_bounded(long_context, LONG_CONTEXT_HARNESS_MAX_BYTES, deadline=deadline, error_code="memory_harness_missing")
    evaluator_bytes = _read_bounded(Path(args.evaluator), LONG_CONTEXT_HARNESS_MAX_BYTES, deadline=deadline, error_code="memory_harness_missing")
    prompts_bytes = _read_bounded(prompts, MEMORY_PROMPTS_MAX_BYTES, deadline=deadline, error_code="memory_harness_missing")
    fixture_bytes = _read_bounded(Path(args.fixture), FIXTURE_MAX_BYTES, deadline=deadline, error_code="evaluator_fixture_unreadable")
    prompts_sha256 = hashlib.sha256(prompts_bytes).hexdigest()
    if prompts_sha256 != getattr(args, "memory_prompts_sha256", ""):
        raise ValueError("memory_prompts_identity_mismatch")
    try:
        prompts_version = _strict_json_object(prompts_bytes).get("version")
    except (ValueError, UnicodeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("memory_prompts_identity_mismatch") from exc
    if not isinstance(prompts_version, str) or not LONG_CONTEXT_REASON.fullmatch(prompts_version.replace(".", "_").replace("-", "_")):
        raise ValueError("memory_prompts_identity_mismatch")
    workspace = Path(tempfile.mkdtemp(prefix="lae-memory-eval-"))
    try:
        os.chmod(workspace, 0o700)
        tree = workspace / "tree"
        for directory in (tree / "scripts" / "test", tree / "tests" / "model", tree / "host" / "agent"):
            directory.mkdir(parents=True, mode=0o700)
        (workspace / "out").mkdir(mode=0o700)
        entry = tree / "scripts" / "test" / "memory_eval.py"
        entry.write_bytes(harness_bytes)
        (tree / "scripts" / "test" / "long_context_eval.py").write_bytes(long_context_bytes)
        (tree / "scripts" / "test" / "evaluate_tool_calls.py").write_bytes(evaluator_bytes)
        (tree / "tests" / "model" / "production_tool_call_eval.json").write_bytes(fixture_bytes)
        (tree / "host" / "agent" / "memory-prompts.json").write_bytes(prompts_bytes)
        raw_path = workspace / "out" / "memory-raw.json"
        profile = MEMORY_PROFILE
        command = [
            sys.executable, os.fspath(entry),
            "--endpoint", f"http://127.0.0.1:{port}/v1/chat/completions",
            "--token-stdin", "--out", os.fspath(raw_path),
            "--conversations", str(profile["conversations"]),
            "--arms", ",".join(profile["arms"]),
            "--chunks", str(profile["chunks"]),
            "--dropped-tokens", str(profile["dropped_tokens"]),
            "--retained-tokens", str(profile["retained_tokens"]),
            "--note-tokens", str(profile["note_tokens"]),
            "--note-bytes", str(profile["note_bytes"]),
            "--max-input-bytes", str(profile["max_input_bytes"]),
            "--per-message-bytes", str(profile["per_message_bytes"]),
            "--max-tokens", str(profile["max_output"]),
            "--max-requests", str(profile["max_requests"]),
            "--timeout", str(profile["request_timeout_seconds"]),
        ]
        bearer = token_file.read_bytes()
        harness_timeout = _stage_timeout(deadline, MEMORY_TOTAL_TIMEOUT, "memory_timeout")
        result = _run_with_stdin(command, bearer, timeout=harness_timeout)
        raw: Any = None
        if raw_path.is_file():
            try:
                raw = _strict_json_object(_read_bounded(raw_path, MEMORY_RAW_MAX_BYTES, error_code="memory_receipt_invalid"))
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("memory_receipt_invalid") from exc
        if raw is None:
            raise ValueError("memory_timeout" if result.get("status") == "timeout" else
                             "memory_harness_failed" if result.get("status") == "failed" else
                             "memory_receipt_missing")
        results = _distill_memory(raw, prompts_sha256=prompts_sha256)
        sent = raw.get("requests_sent_this_run")
        if sent is not None and _bounded_int_value(sent, 0, profile["max_requests"]) is None:
            raise ValueError("memory_receipt_invalid")
        results["requests_sent"] = sent
        child_status = _bounded_child_status(process)
        if child_status is not None:
            raise EngineStartupFailure("engine_exited_during_evaluation", child_status)
        complete = results["complete"] and result.get("status") == "completed"
        return {
            "schema": SUITE_MEMBER_SCHEMAS[MEMORY_MEMBER],
            "status": "completed" if complete else "partial",
            "artifact": artifact,
            "fixture": fixture_identity,
            "engine": build_info,
            "model_preflight": model_preflight,
            "harness": {"schema": raw["schema"], "sha256": hashlib.sha256(harness_bytes).hexdigest(),
                        "long_context_sha256": hashlib.sha256(long_context_bytes).hexdigest(),
                        "process": {"status": result.get("status"), "exit_code": result.get("exit_code")}},
            "memory_prompts": {"sha256": prompts_sha256, "version": prompts_version},
            "plan": dict(profile, arms=list(profile["arms"])),
            "results": results,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "prompt_response_logging": False,
            "tokens_logged": False,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _validate_suite_member(args: argparse.Namespace) -> str:
    """Return the opt-in member, or ``""``; refuse an incoherent request."""

    member = getattr(args, "suite_member", "") or ""
    memory_options = (getattr(args, "memory_harness", ""), getattr(args, "memory_prompts", ""),
                      getattr(args, "memory_prompts_sha256", ""))
    if not member:
        if getattr(args, "expected_fixture_sha256", "") or getattr(args, "long_context_harness", "") or any(memory_options):
            raise ValueError("suite_member_invalid")
        return ""
    if member == MEMORY_MEMBER:
        digest = memory_options[2]
        # The memory harness imports the long-context module, so it needs that
        # file too; every one of its options is required and none may appear
        # on any other member.
        if (not all(memory_options) or not getattr(args, "long_context_harness", "") or
                not isinstance(digest, str) or len(digest) != 64 or set(digest) - PIN_RE):
            raise ValueError("suite_member_invalid")
    elif any(memory_options):
        raise ValueError("suite_member_invalid")
    expected = getattr(args, "expected_fixture_sha256", "")
    preflight = Path(getattr(args, "preflight_receipt", "") or "")
    if (member not in SUITE_MEMBER_SCHEMAS or not isinstance(expected, str) or len(expected) != 64 or set(expected) - PIN_RE or
            # A member must never overwrite the shipping eval's own preflight
            # or receipt evidence on the host.
            not getattr(args, "preflight_receipt", "") or preflight.name == "startup-preflight-receipt.json" or
            Path(args.receipt).name in {"eval-receipt.json", "startup-preflight-receipt.json"} or
            (member in {LONG_CONTEXT_MEMBER, MEMORY_MEMBER}) != bool(getattr(args, "long_context_harness", ""))):
        raise ValueError("suite_member_invalid")
    return member


def _stamp_suite_member(receipt: dict[str, Any], member: str) -> dict[str, Any]:
    """Give a member receipt its own schema and say which member it is."""

    if not member:
        return receipt
    stamped = {**receipt, "schema": SUITE_MEMBER_SCHEMAS[member], "suite_member": member}
    if member == ABLATION_MEMBER:
        stamped["scorer_ablation"] = dict(SCORER_ABLATION_RECORD)
    return stamped


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
    # Optional: the orchestrator cannot name a remote credential path in a
    # persisted argv (see ``_eval_remote_commands``), so the default is an
    # owner-private temporary directory created and removed by this process.
    parser.add_argument("--token-file", default="")
    parser.add_argument("--backend", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--cuda-device-name", default="")
    parser.add_argument("--cuda-device-receipt", default="")
    parser.add_argument("--toolchain-receipt", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--preflight-receipt", default="")
    parser.add_argument("--timeout", type=float, default=EVAL_TOTAL_TIMEOUT)
    # Opt-in suite members. All three default to empty, which is the shipping
    # path exactly; see SUITE_MEMBER_SCHEMAS.
    parser.add_argument("--suite-member", default="", choices=("", *SUITE_MEMBER_SCHEMAS))
    parser.add_argument("--expected-fixture-sha256", default="")
    parser.add_argument("--long-context-harness", default="")
    parser.add_argument("--memory-harness", default="")
    parser.add_argument("--memory-prompts", default="")
    parser.add_argument("--memory-prompts-sha256", default="")
    args = parser.parse_args(argv)
    receipt: dict[str, Any]
    process_started = time.monotonic()
    fixture_identity: dict[str, Any] | None = None
    member = ""
    try:
        member = _validate_suite_member(args)
        if not 1 <= args.timeout <= (LONG_CONTEXT_TOTAL_TIMEOUT if member == LONG_CONTEXT_MEMBER else
                                     MEMORY_TOTAL_TIMEOUT if member == MEMORY_MEMBER else EVAL_TOTAL_TIMEOUT):
            raise ValueError("eval_timeout_invalid")
        deadline = process_started + args.timeout
        try:
            fixture_identity = _fixture_contract(Path(args.fixture), deadline=deadline)["fixture_identity"]
        except ValueError:
            fixture_identity = None
        if member and (fixture_identity is None or fixture_identity.get("sha256") != args.expected_fixture_sha256):
            # Refused before the model is hashed or the engine started: a
            # wrong chunk file must not cost GPU time or score as the right one.
            raise ValueError("evaluator_fixture_identity_mismatch")
        if member == MEMORY_MEMBER:
            # Same rule for the memory prompt file: a different prompt version
            # is refused before the model is hashed or the engine started.
            try:
                prompts_bytes = _read_bounded(Path(args.memory_prompts), MEMORY_PROMPTS_MAX_BYTES, deadline=deadline,
                                              error_code="memory_harness_missing")
            except ValueError:
                raise ValueError("memory_harness_missing") from None
            if hashlib.sha256(prompts_bytes).hexdigest() != args.memory_prompts_sha256:
                raise ValueError("memory_prompts_identity_mismatch")
        preflight_path = Path(args.preflight_receipt or Path(args.receipt).with_name("startup-preflight-receipt.json"))
        try:
            _write_preflight_receipt(preflight_path, status="not_started", error_code="engine_model_preflight_not_started")
            setattr(args, "_preflight_summary", {"status": "not_started", "error_code": "engine_model_preflight_not_started"})
        except (OSError, ValueError):
            # The normal failure receipt remains the source of truth if the
            # preflight destination itself is unavailable.
            pass
        artifact = verify_artifact(Path(args.model), Path(args.model_manifest), source_revision=args.source_revision, llama_revision=args.llama_revision, manifest_lock_path=Path(args.model_manifest_lock), deadline=deadline)
        if not member:
            receipt = _launch_and_evaluate(args, artifact, deadline=deadline)
            status = 0 if receipt["status"] in {"verified", "completed_with_failures"} else 1
        else:
            if member == LONG_CONTEXT_MEMBER:
                receipt = _launch_and_evaluate(args, artifact, deadline=deadline, session=_long_context_session)
            elif member == MEMORY_MEMBER:
                receipt = _launch_and_evaluate(args, artifact, deadline=deadline, session=_memory_session)
            elif member == ABLATION_MEMBER:
                receipt = _launch_and_evaluate(args, artifact, deadline=deadline, extra_evaluator_args=("--no-boolean-coercion",))
            else:
                receipt = _launch_and_evaluate(args, artifact, deadline=deadline)
            status = 0 if receipt["status"] in {"verified", "completed_with_failures", "completed", "partial"} else 1
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, OverflowError, subprocess.SubprocessError) as exc:
        receipt = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "failed", "error_type": type(exc).__name__, "error_code": _safe_error_code(exc), "prompt_response_logging": False, "tokens_logged": False}
        if fixture_identity is not None:
            receipt["fixture"] = fixture_identity
        preflight = getattr(args, "_preflight_summary", None)
        if isinstance(preflight, dict):
            receipt["preflight"] = preflight
        child_status = getattr(exc, "child_status", None)
        if isinstance(child_status, dict) and set(child_status) in ({"exit_code"}, {"signal"}):
            receipt["child"] = child_status
        status = 1
    output = Path(args.receipt)
    # A member whose own arguments were refused still publishes under its
    # member schema, so the refusal can never be salvaged as a shipping receipt.
    member = member or (args.suite_member if args.suite_member in SUITE_MEMBER_SCHEMAS else "")
    receipt = {**_stamp_suite_member(receipt, member), **_run_identity()}
    encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        receipt = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "failed", "error_code": "evaluator_receipt_invalid", "prompt_response_logging": False, "tokens_logged": False, **_run_identity()}
        receipt = {**_stamp_suite_member(receipt, member), **_run_identity()}
        encoded = (json.dumps(receipt, sort_keys=True) + "\n").encode("ascii")
    _publish_private_receipt(output, encoded)
    # The orchestrator records this line as the failed stage's stdout tail, and
    # it was the only channel that survived teardown on run j1m-eval-20260912-b
    # -- where it reported status "failed" and metrics null while the reason sat
    # in `error_code`, which this summary dropped. Both fields are drawn from
    # the finite SAFE_ERROR_CODES vocabulary and carry no host or model text.
    summary = {"schema": receipt["schema"], "status": receipt["status"], "metrics": receipt.get("metrics")}
    for field in ("error_code", "error_type"):
        if receipt.get(field) is not None:
            summary[field] = receipt[field]
    if isinstance(receipt.get("preflight"), dict):
        summary["preflight"] = receipt["preflight"]
    if isinstance(receipt.get("child"), dict):
        summary["child"] = receipt["child"]
    print(json.dumps(summary, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
