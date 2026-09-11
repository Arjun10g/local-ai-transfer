#!/usr/bin/env python3
"""Audited future J1M lifecycle; dry-run unless explicitly unlocked.

The mutating path is present for Sol's later review, but this turn invokes only
the default dry-run. It owns one nonce-bound instance, never enumerates the
account, and tears down the exact resource in ``finally`` after salvage.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import stat
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
from scripts.shadeform_teardown import teardown_exact, teardown_recovered_exact
from scripts.test import compare_model_quality

ROOT = Path(__file__).resolve().parents[1]
_STDERR_TAIL_LIMIT = 1200
# ``_persist_lifecycle`` referenced an undefined ``MAX_RECEIPT_BYTES`` since
# 8e3f599, so it raised ``NameError`` on every call.  Both call sites swallow
# exceptions so cleanup can never be stranded by an evidence failure, which is
# right -- and meant the orchestrator has never actually written a lifecycle
# receipt.  Found by the offline dry run, which exercises the real writer.
MAX_RECEIPT_BYTES = 512 * 1024
_EVAL_FIXTURE_MAX_BYTES = 256 * 1024
_EVAL_RECEIPT_MAX_BYTES = 64 * 1024
_EVAL_ARTIFACT_RECEIPT_MAX_BYTES = 8 * 1024
# Raised from 1024 to leave room for the required run-identity binding that
# every salvageable receipt now carries.
_PREFLIGHT_RECEIPT_MAX_BYTES = 2048
_MAX_OUTPUT_RESERVE_TOKENS = 256
# Bounded capacity for the current production fixture; identity checks still
# require the exact catalog supplied by the fixture.
_MAX_EVAL_TOOLS = 33
_DELETION_RESERVE_SECONDS = 660.0
# Commit 2d7db4f refused external salvage because the pathname handed to SCP
# could not remain bound to the *validated destination* across an untrusted
# remote transfer.  That concern is addressed here by never giving SCP the
# validated destination at all: the transfer lands in a fresh private staging
# directory whose contents are untrusted, and the only path that ever reaches
# the run's artifact directory is the descriptor-safe publisher in
# ``j1m_runner``.  The transport below is therefore a real, bounded capability
# and this flag is source-level truth rather than a dormant switch.
_EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE = True
# The single remote directory the orchestrator itself named at launch.  Salvage
# never lists a remote directory, never globs, and never accepts a remote path
# from a caller: every fetched path is this constant joined with one
# source-fixed allowlist entry.
_SALVAGE_REMOTE_DIRECTORY = "/scratch/j1m/artifacts"
# Receipt name -> the exact schema string that receipt must declare.  This is
# source-controlled acceptance data.  A name absent from this mapping cannot be
# fetched even when a configuration file lists it.
_SALVAGE_RECEIPT_ALLOWLIST: dict[str, str] = {
    "eval-receipt.json": "local_bmo.j1m.real-tool-eval-receipt.v1",
    "eval-artifact-receipt.json": "local_bmo.j1m.remote-eval-artifact-receipt.v1",
    "startup-preflight-receipt.json": "local_bmo.j1m.startup-preflight-receipt.v1",
    "toolchain-receipt.json": "local_bmo.j1m.remote-toolchain-receipt.v1",
    "cuda-device-receipt.json": "local_bmo.j1m.cuda-device-receipt.v1",
    "proving-receipt.json": "local_bmo.j1m.proving-receipt.v1",
    # Build-mode receipts.  Every one is a schema-bound JSON object written by
    # ``j1m_runner`` on the host and therefore carryable by exactly the same
    # bounded transport: a paid conversion must not return zero evidence.  The
    # deployable weights are deliberately absent -- see
    # ``_SALVAGE_NON_RECEIPT_NAMES``.
    "manifest.json": "local_bmo.j1m.artifact-manifest.v1",
    "tensor-metadata.json": "local_bmo.j1m.tensor-metadata.v1",
    "source-model-receipt.json": "local_bmo.j1m.source-model-receipt.v1",
    "conversion-receipt.json": "local_bmo.j1m.conversion-receipt.v1",
    "model-receipt.json": "local_bmo.j1m.model-receipt.v1",
    "toolchain.json": "local_bmo.j1m.toolchain.v1",
    "scan-receipt.json": "local_bmo.j1m.scan-receipt.v1",
    "post-cleanup-receipt.json": "local_bmo.j1m.post-cleanup-receipt.v1",
    # Comparator arm receipts. These are receipts like any other -- bounded,
    # schema-bound JSON objects written by ``remote_comparator_eval.py`` on the
    # host -- and without them the retention measurement the comparator phase
    # is paid for degrades to a typed skip. The three arm names are enumerated
    # here, not derived from a pattern: the fetchable set stays exactly as
    # source-fixed as it was, and a fourth arm name is still refused.
    "comparator-receipt-q4_k_m.json": "local_bmo.j1m.comparator-eval-receipt.v1",
    "comparator-receipt-q8_0.json": "local_bmo.j1m.comparator-eval-receipt.v1",
    "comparator-receipt-bf16.json": "local_bmo.j1m.comparator-eval-receipt.v1",
}
# The enumerated comparator receipt names, in arm order, for the tests and the
# design note to assert against rather than re-deriving.
_SALVAGE_COMPARATOR_RECEIPTS = (
    "comparator-receipt-q4_k_m.json",
    "comparator-receipt-q8_0.json",
    "comparator-receipt-bf16.json",
)
# Names a configuration may legitimately list that this transport deliberately
# cannot carry, and why.  They are refused with their own typed reason rather
# than the generic "not allowlisted", so a build run's receipt says plainly
# that the weights were never salvageable and the two non-object outputs are
# not schema-bound, instead of implying a configuration mistake.
_SALVAGE_NON_RECEIPT_NAMES: dict[str, str] = {
    # Weights: a multi-GiB artifact is not evidence and is never transferred to
    # the operator laptop.  This is the ``2d7db4f`` scope decision, retained.
    "Qwen3.5-9B-bf16.gguf": "weights",
    "Qwen3.5-9B-Q8_0.gguf": "weights",
    "Qwen3.5-9B-Q4_K_M.gguf": "weights",
    # Not JSON at all; nothing to schema-check or bind to this run.
    "checksums.sha256": "not_schema_bound",
    # A JSON *array* of command records, so it can carry neither a top-level
    # schema string nor the required run-identity binding.
    "command-receipt.json": "not_schema_bound",
}
# Identity every allowlisted receipt MUST carry before it can be published.
# This is required, not opportunistic: a receipt left in the remote artifact
# directory by an earlier run carries that run's binding and is refused rather
# than published as this run's evidence.  ``j1m_runner.RUN_IDENTITY_FIELDS`` is
# what the host-side writers emit, from the ``run-identity.json`` this
# orchestrator uploads before the first receipt-producing command.
_SALVAGE_REQUIRED_IDENTITY: frozenset[str] = frozenset(j1m_runner.RUN_IDENTITY_FIELDS)
# Receipts that additionally assert the approved artifact's identity must carry
# those fields and match; a receipt that simply omits them is refused rather
# than silently accepted.
_SALVAGE_REQUIRED_ARTIFACT_CLAIMS: dict[str, tuple[str, ...]] = {
    "eval-artifact-receipt.json": ("name", "size_bytes", "sha256"),
    "eval-receipt.json": ("name", "size_bytes", "sha256"),
    # The Q4 arm scores the run's own approved artifact, so its
    # ``artifact_sha256`` must equal it. The higher-precision arms score
    # rebuilt files whose digests this process has no way to know, so they are
    # required to *declare* an artifact digest and to agree with the fixture,
    # which is the binding that is actually provable here.
    "comparator-receipt-q4_k_m.json": ("sha256",),
}
# Minimum top-level keys each receipt must carry before it may be published.
# The full per-receipt verification still runs in ``_verify_*`` after publish;
# this is the bounded structural gate the transport applies to untrusted bytes.
_SALVAGE_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "eval-receipt.json": frozenset({
        "schema", "status", "artifact", "fixture", "engine", "model_preflight",
        "toolchain", "metrics",
    }),
    "eval-artifact-receipt.json": frozenset({
        "schema", "status", "name", "size_bytes", "sha256", "manifest_sha256",
        "manifest_lock_sha256",
    }),
    "startup-preflight-receipt.json": frozenset({"schema", "status"}),
    "toolchain-receipt.json": frozenset({"schema", "status"}),
    "cuda-device-receipt.json": frozenset({"schema", "status"}),
    "proving-receipt.json": frozenset({"schema", "host", "python", "text_only"}),
    "manifest.json": frozenset({
        "schema", "created_at_utc", "inventory_scope", "deployable_model_artifacts",
        "text_only", "artifacts", "tensor_metadata",
    }),
    "tensor-metadata.json": frozenset({
        "schema", "status", "text_only", "tensor_count", "tensors", "gguf_metadata",
        "vision_projection_present", "chat_template_sha256",
    }),
    "source-model-receipt.json": frozenset({
        "schema", "status", "model_id", "revision", "checked_files", "file_hashes",
        "license_sha256", "tokenizer_sha256", "chat_template_sha256", "verified_at_utc",
    }),
    "conversion-receipt.json": frozenset({
        "schema", "status", "text_only", "source_revision", "llama_cpp_revision",
        "artifacts", "command_receipt_sha256", "toolchain",
    }),
    "model-receipt.json": frozenset({
        "schema", "status", "text_only", "q4_artifact", "tensor_metadata_sha256",
        "vision_projection_present", "scan_receipt_sha256",
    }),
    "toolchain.json": frozenset({
        "schema", "llama_cpp_head", "python", "cmake", "compiler", "os_packages",
        "pip_freeze",
    }),
    "scan-receipt.json": frozenset({
        "schema", "status", "inventory_scope", "text_only", "artifacts",
        "vision_projection_present",
    }),
    "post-cleanup-receipt.json": frozenset({
        "schema", "status", "inventory_scope", "intermediates_absent",
        "remaining_gguf", "forbidden_artifacts", "q4",
    }),
    **{
        name: frozenset({
            "schema", "status", "arm", "artifact", "artifact_sha256",
            "fixture", "fixture_sha256", "host", "settings", "metrics",
        })
        for name in _SALVAGE_COMPARATOR_RECEIPTS
    },
}
# The locally written salvage evidence file.  It is deliberately not fetchable:
# it describes the transfer and must never be supplied by the remote host.
_SALVAGE_RECEIPT_NAME = "salvage-receipt.json"
_SALVAGE_RECEIPT_SCHEMA = "local_bmo.j1m.salvage-receipt.v1"
# These are the *operative* bounds, not decorative wider ones.  The earlier
# 4 MiB per-file and 32 MiB total caps could never bind, because the strict
# receipt decoder refuses anything over ``_RECEIPT_MAX_BYTES`` first and
# dedup caps distinct fetches at the allowlist size; the receipt's ``caps``
# block therefore advertised numbers that no input could ever reach.  Each cap
# below is now the number that actually decides.
_SALVAGE_MAX_FILE_BYTES = j1m_runner._RECEIPT_MAX_BYTES
_SALVAGE_MAX_FILES = len(_SALVAGE_RECEIPT_ALLOWLIST)
_SALVAGE_MAX_TOTAL_BYTES = _SALVAGE_MAX_FILES * _SALVAGE_MAX_FILE_BYTES
# Refuse the whole call rather than stage a transfer with nowhere to put it.
# ``scp`` writes into the private staging directory before any size check can
# run, so the worst case must fit twice over: once staged, once published.
_SALVAGE_MIN_FREE_BYTES = _SALVAGE_MAX_TOTAL_BYTES * 2
_SALVAGE_WALL_CLOCK_SECONDS = 300.0
_SALVAGE_FILE_TIMEOUT_SECONDS = 60.0
# A request naming more than this many candidates is a configuration fault, not
# a transfer to bound one file at a time.
_SALVAGE_MAX_REQUESTED_NAMES = 64
_SALVAGE_HOST_KEY_MARKERS = (
    "host key verification failed",
    "remote host identification has changed",
    "host identification has changed",
    "key verification failed",
)
# This is source-controlled acceptance data, not a value supplied by a run
# configuration.  The config repeats it for operator visibility/parity checks,
# but a caller cannot turn an arbitrary manifest plus a self-authored lock into
# an accepted eval artifact by changing JSON configuration.
_APPROVED_EVAL_MANIFEST_RELATIVE = Path("artifacts/qwen35-9b/model-manifest.json")
_APPROVED_EVAL_MANIFEST_SHA256 = "3bcfe1796e2ec24c556c2455d583bb3763039aeaf9b5119ddc1c498459fbeb99"
# The comparator arms need a host that can load non-Q4 weights. The product
# engine compiles the Q4 identity in
# (``native/model_validation/model_validator.hpp``) and that gate is correct
# and untouched, so the arms are hosted on the pinned upstream
# ``llama-server`` instead -- the runtime oracle
# ``model/quality-eval/quality-fixture-spec.json``
# ``comparison.runtime_oracle`` already names. COMPARATOR-ENGINE-001 supplies
# that host (``j1m_runner.comparator_server_plan``), the transport that speaks
# to it (``evaluate_tool_calls.py --transport upstream-openai``) and the arm
# driver (``scripts/test/remote_comparator_eval.py``), so the phase can now
# produce a number. The flag itself remains default OFF:
# ``--evaluate-comparators`` defaults to ``""``. See
# ``model/COMPARATOR_EVAL.md`` section 2.2a.
_COMPARATOR_ENGINE_AVAILABLE = True
# Identity anchor for the rebuilt comparators. Like the eval manifest anchor
# above this is source-controlled acceptance data: a caller cannot turn an
# arbitrary scan receipt into an accepted comparator through configuration.
_APPROVED_SCAN_RECEIPT_RELATIVE = Path("artifacts/qwen35-9b/scan-receipt.json")
_APPROVED_SCAN_RECEIPT_SHA256 = "0857899bf86527702743dd12b62ae5740cbb27fae2005da7066d1070cb341066"
_COMPARATOR_BASELINE_ARM = "q4_k_m"
# The exact file each arm must be. Names only; the identity that matters is
# the size/SHA-256 pair the arm driver re-hashes against the anchor.
_COMPARATOR_ARM_FILENAMES = {
    "q4_k_m": "Qwen3.5-9B-Q4_K_M.gguf",
    "q8_0": "Qwen3.5-9B-Q8_0.gguf",
    "bf16": "Qwen3.5-9B-bf16.gguf",
}
# Retention is only meaningful with the runtime held constant, so the Q4 arm
# is always evaluated on the same host as the comparator it is divided by.
_COMPARATOR_SELECTIONS = {
    "": (),
    # The Q4 artifact on the pinned upstream server, with no higher-precision
    # denominator. This is the reachable measurement for
    # ``execution/ACCEPTANCE_CRITERIA.md`` section 11 MUST 1 ("within 2
    # aggregate points of the pinned upstream same-artifact Q4 oracle"): the
    # comparison receipt records it as ``runtime_parity_delta_points`` against
    # the product engine's own Q4 receipt.
    "q4-oracle": ("q4_k_m",),
    "q8": ("q8_0",),
    "q8,bf16": ("q8_0", "bf16"),
}
# Loopback port for the per-arm upstream server. Each arm gets its own port
# so a lingering socket from a previous arm cannot be mistaken for a ready
# server; nothing binds beyond 127.0.0.1.
_COMPARATOR_BASE_PORT = 18081
# The comparator server build is budgeted out of the setup budget above:
# configure 300 + CUDA build 1200 == _COMPARATOR_SETUP_BUDGET_SECONDS.
_COMPARATOR_CONFIGURE_BUDGET_SECONDS = 300.0
_COMPARATOR_BUILD_BUDGET_SECONDS = 1200.0
# One-time comparator-engine configure plus CUDA build, and one bounded
# evaluation per arm (the existing 480 s evaluation budget plus 240 s of
# engine start and model load).
_COMPARATOR_SETUP_BUDGET_SECONDS = 1500.0
_COMPARATOR_ARM_BUDGET_SECONDS = 720.0
_COMPARATOR_SKIP_REASONS = frozenset({
    "comparator_not_requested", "comparator_engine_unavailable",
    "comparator_clock_insufficient", "comparator_stage_failed",
    "comparator_receipt_missing", "comparator_receipt_invalid",
    "comparator_artifact_identity_mismatch", "comparator_baseline_missing",
})
_EVAL_DIAGNOSTIC_CODES = frozenset({
    "http_400", "http_401", "http_404", "http_408", "http_409", "http_413",
    "http_415", "http_429", "http_500", "http_503", "http_other",
    "transport_url", "transport_timeout", "transport_os", "parse_json",
    "parse_session_shape", "parse_response_shape", "context_overflow",
    "endpoint", "token", "unknown",
})
_EVAL_QUALITY_CODES = frozenset({
    "forbidden_tool_name", "malformed_call", "unknown_tool", "malformed_parameter",
    "parameter_too_large", "invalid_json_argument", "invalid_tool_schema",
    "invalid_arguments", "missing_argument", "extra_argument",
    "argument_type_mismatch", "argument_value_mismatch", "missing_call",
    "unexpected_call", "wrong_tool", "call_mismatch",
    "quality_unknown",
})


class OperatorCancelled(Exception):
    pass


def _redacted_output_tail(value: object) -> str:
    """Return bounded command evidence, rejecting credential-shaped text."""

    text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else str(value or "")
    j1m_runner.validate_persisted_output(text)
    return text[-_STDERR_TAIL_LIMIT:]


def _bounded_bytes(path: Path, limit: int) -> bytes:
    """Read one bounded descriptor snapshot, rejecting replacement."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("descriptor-safe bounded read is unavailable")
    ancestors = j1m_runner._private_ancestor_snapshot(
        path, j1m_runner.PRIVATE_OUTPUT_ROOT, strict_permissions=False,
    )
    parent_descriptor = -1
    descriptor = -1
    try:
        parent_descriptor = j1m_runner._open_private_parent_descriptor(
            path, j1m_runner.PRIVATE_OUTPUT_ROOT, ancestors=ancestors,
        )
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW |
            getattr(os, "O_CLOEXEC", 0), dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise ValueError("receipt is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022 or
                before.st_size > limit):
            raise ValueError("receipt is not bounded regular data")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (len(raw) > limit or before.st_size != after.st_size or
                after.st_size != len(raw) or
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino) or
                not j1m_runner._private_ancestors_stable(ancestors)):
            raise ValueError("receipt changed during bounded read")
        return raw
    except OSError:
        raise ValueError("receipt read refused") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _decode_bounded_json(raw: bytes) -> Any:
    """Decode bounded JSON without duplicate keys."""
    return j1m_runner._bounded_json_loads(raw)


def _bounded_json(path: Path, limit: int) -> Any:
    """Decode a small receipt without duplicate keys or unbounded reads."""

    payload = _decode_bounded_json(_bounded_bytes(path, limit))
    j1m_runner.validate_persisted_receipt(payload)
    return payload


def _persist_lifecycle(phase_id: str, lifecycle: dict[str, Any]) -> None:
    """Durably retain bounded local failure/progress evidence before teardown."""

    if not isinstance(lifecycle, dict):
        raise ValueError("lifecycle receipt is invalid")
    record = {"schema": "local_bmo.j1m.lifecycle-receipt.v1", **lifecycle}
    j1m_runner.validate_persisted_receipt(record)
    path = sf.runtime_ledger_path(phase_id).with_name(f"{phase_id}.lifecycle-receipt.json")
    try:
        payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("lifecycle receipt serialization refused") from exc
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ValueError("lifecycle receipt exceeds bound")
    j1m_runner.validate_persisted_output(payload)
    sf.private_durable_atomic_write(path, payload, label="J1M lifecycle receipt")


def prepare_artifact_destination(destination: Path) -> Path:
    """Prove the run's artifact destination can actually receive a receipt.

    Salvage publishes only through a no-follow descriptor walk from the trusted
    root, so every component below that root must be owner-private.  The
    checked-in ``artifacts/qwen35-9b`` is an ordinary ``0755`` directory and Git
    does not record directory modes, so the default destination fails that walk
    on a fresh checkout -- and it failed *inside* the teardown ``finally``,
    where the whole salvage call was swallowed and the run reported no receipts
    without saying why.

    Missing components are created ``0700``.  An existing component that is not
    owner-private is refused here, before the first billable provider call,
    with the exact command that fixes it: a permission decision on the
    operator's own tree is theirs to make, not something to change silently
    underneath them.
    """

    destination = Path(destination)
    root = j1m_runner.PRIVATE_OUTPUT_ROOT
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise sf.ShadeformError(
            "artifact destination must live under the trusted output root"
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            current.mkdir(mode=0o700)
    try:
        j1m_runner._private_ancestor_snapshot(destination / "probe.json", root)
        info = os.lstat(destination)
    except (OSError, ValueError) as exc:
        unsafe = _first_unsafe_component(root, relative)
        raise sf.ShadeformError(
            "artifact destination is not privately publishable "
            f"({exc}); run: chmod 700 {unsafe}"
        ) from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise sf.ShadeformError(
            f"artifact destination is not owner-private; run: chmod 700 {destination}"
        )
    return destination


def _first_unsafe_component(root: Path, relative: Path) -> Path:
    """Name the outermost component whose mode blocks a private publication."""

    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return current
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return current
    return current


def _progress(path: Path, event: str, **details: Any) -> None:
    """Best-effort atomic progress marker; never masks cleanup failures."""

    try:
        j1m_runner.write_progress(path, event, **details)
    except Exception:
        pass


def _tool_eval_contract() -> dict[str, Any]:
    """Return the bounded contract; ``limits.max_cases`` is a ceiling."""

    fixture_path = ROOT / "tests" / "model" / "production_tool_call_eval.json"
    fixture_raw = _bounded_bytes(fixture_path, _EVAL_FIXTURE_MAX_BYTES)
    fixture = _decode_bounded_json(fixture_raw)
    limits = fixture.get("limits") if isinstance(fixture, dict) else None
    cases = fixture.get("cases") if isinstance(fixture, dict) else None
    count = limits.get("max_cases") if isinstance(limits, dict) else None
    tools = fixture.get("tools") if isinstance(fixture, dict) else None
    tool_names = [tool.get("function", {}).get("name") if isinstance(tool, dict) and isinstance(tool.get("function"), dict) else None for tool in tools] if isinstance(tools, list) else []
    if (not isinstance(fixture, dict) or set(fixture) != {"schema", "model", "protocol", "limits", "tools", "cases"} or
            not isinstance(limits, dict) or
            set(limits) != {"context_tokens", "max_output_tokens", "temperature", "max_cases"} or
            fixture.get("schema") != "local_bmo.tool-call-eval.v1" or fixture.get("model") != "Qwen3.5-9B-Q4_K_M" or
            fixture.get("protocol") != "qwen35-xml-tool-call-v1" or isinstance(count, bool) or not isinstance(count, int) or
            not isinstance(cases, list) or not 1 <= len(cases) <= count <= 64 or
            not isinstance(tools, list) or not 1 <= len(tools) <= _MAX_EVAL_TOOLS or len(set(tool_names)) != len(tool_names) or
            any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,95}", name) for name in tool_names) or
            not isinstance(limits.get("context_tokens"), int) or not 1 <= limits["context_tokens"] <= 16384 or
            isinstance(limits.get("max_output_tokens"), bool) or not isinstance(limits.get("max_output_tokens"), int) or not 1 <= limits["max_output_tokens"] <= 256 or limits["max_output_tokens"] >= limits["context_tokens"]):
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
    category_counts = {category: sum(case["category"] == category for case in cases) for category in categories}
    return {
        "case_count": len(cases), "categories": categories, "category_counts": category_counts,
        "tool_count": len(tools), "context_tokens": limits["context_tokens"],
        "output_reserve_tokens": limits["max_output_tokens"],
        "fixture_identity": {
            "sha256": hashlib.sha256(fixture_raw).hexdigest(),
            "schema": fixture["schema"], "model": fixture["model"], "protocol": fixture["protocol"],
            "limits": dict(limits), "tool_names": list(tool_names), "tool_count": len(tools),
            "case_count": len(cases), "category_counts": dict(sorted(category_counts.items())),
        },
    }


def _remote(command: list[str], *, timeout: float) -> dict[str, Any]:
    j1m_runner.validate_persisted_argv(command)
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout,
            env=sf._secure_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        try:
            stderr_tail = _redacted_output_tail(exc.stderr)
        except (ValueError, UnicodeError):
            stderr_tail = "<redacted>"
        return {"status": "transport_timeout", "exit_code": None, "error_type": "transport_timeout", "stderr_tail": stderr_tail}
    except OSError:
        return {"status": "transport_os", "exit_code": None, "error_type": "transport_os", "stderr_tail": "<redacted>"}
    try:
        stderr_tail = _redacted_output_tail(result.stderr)
    except (ValueError, UnicodeError):
        # Preserve only a finite diagnostic; the credential-bearing output is
        # rejected and never enters the receipt.
        stderr_tail = "<redacted>"
    receipt = {"status": "completed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "stderr_tail": stderr_tail}
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
    if mode == "canary":
        raise ValueError("canary commands are supplied by the no-model probe plan")
    raise ValueError(f"unsupported J1M mode: {mode}")


def _canary_remote_commands(config: dict[str, Any], remote_root: str) -> list[list[str]]:
    """Expose the pure no-model remote probe plan for review/tests."""

    return j1m_runner.canary_command_plan(config, remote_root)


def _verify_eval_artifact(path: Path | None, manifest_path: Path, config: dict[str, Any], *, expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    """Verify the accepted Q4 identity without requiring local model bytes."""

    expected_name = "Qwen3.5-9B-Q4_K_M.gguf"
    if path is not None and (path.name != expected_name or not path.is_file()):
        raise ValueError("eval artifact has the wrong name or is missing")
    manifest_bytes = _bounded_bytes(manifest_path, _EVAL_FIXTURE_MAX_BYTES)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_sha256 is None:
        configured_artifacts = config.get("artifacts")
        if (not isinstance(configured_artifacts, dict) or
                configured_artifacts.get("eval_manifest_path") != str(_APPROVED_EVAL_MANIFEST_RELATIVE) or
                configured_artifacts.get("eval_manifest_sha256") != _APPROVED_EVAL_MANIFEST_SHA256 or
                manifest_path.resolve() != (ROOT / _APPROVED_EVAL_MANIFEST_RELATIVE).resolve()):
            raise ValueError("eval manifest path is not the approved trust anchor")
        trusted_digest = _APPROVED_EVAL_MANIFEST_SHA256
    else:
        trusted_digest = expected_manifest_sha256
    if not isinstance(trusted_digest, str) or len(trusted_digest) != 64 or set(trusted_digest) - set("0123456789abcdef") or manifest_digest != trusted_digest:
        raise ValueError("eval manifest trust anchor mismatch")
    manifest = j1m_runner._bounded_json_loads(manifest_bytes)
    j1m_runner.validate_persisted_receipt(manifest)
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


def _eval_uploads(config: dict[str, Any], remote_root: str, artifact_path: Path | None, manifest_path: Path, selection: tuple[str, ...] = ()) -> list[tuple[Path, str, bool]]:
    """Local files to upload for eval; the GGUF and receipts stay allowlisted.

    With no comparator selection the list is byte-identical to the shipped
    one.  A selection appends exactly two files: the arm driver, and the
    source-controlled scan receipt that anchors the comparator identities
    (``model/COMPARATOR_EVAL.md`` section 4).  It is uploaded under a
    distinct name so it can never be confused with the scan receipt the
    remote ``--scan`` stage produces for the same run.
    """

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
        (ROOT / "tests" / "model" / "production_tool_call_eval.json", f"{remote_root}/production_tool_call_eval.json", False),
        (ROOT / "CMakeLists.txt", f"{remote_root}/engine/CMakeLists.txt", False),
        # Recursive scp copies the source directory beneath its destination;
        # target the engine parent so the result is exactly engine/native.
        (ROOT / "native", f"{remote_root}/engine", True),
        # These sources are referenced unconditionally by native/CMakeLists;
        # they are needed at configure time even when only lae-engine builds.
        (ROOT / "tests" / "native" / "runtime_tests.cpp", f"{remote_root}/engine/tests/native/runtime_tests.cpp", False),
        (ROOT / "tests" / "native" / "model_validator_tests.cpp", f"{remote_root}/engine/tests/native/model_validator_tests.cpp", False),
    ])
    if selection:
        uploads.extend([
            (ROOT / "scripts" / "test" / "remote_comparator_eval.py", f"{remote_root}/remote_comparator_eval.py", False),
            (ROOT / _APPROVED_SCAN_RECEIPT_RELATIVE, f"{remote_root}/comparator-anchor-scan-receipt.json", False),
        ])
    return uploads


def _eval_remote_commands(config: dict[str, Any], remote_root: str, selection: tuple[str, ...] = ()) -> list[list[str]]:
    """Build reviewed non-shell argv stages for the authenticated CUDA eval.

    With no comparator selection every stage is byte-identical to the shipped
    plan.  A selection adds one flag to one stage: ``--retain-comparators``,
    which *moves* the intermediate-deletion tail out of the runner's plan so
    the comparators survive long enough to be evaluated.  The orchestrator
    then owes that tail (``_comparator_cleanup_commands``) before teardown.
    """

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
        # ``--token-file`` is deliberately absent.  It names a path on a host
        # this process has not contacted, and ``validate_persisted_argv``
        # accepts a token-file operand only when it is a canonical private
        # handle *on this machine* -- a proof that cannot exist for a remote
        # path.  Rather than weaken that policy or rename the option to dodge
        # its credential detector, the orchestrator stops naming a credential
        # path it cannot prove: ``remote_model_eval.py`` creates its bearer
        # token in an owner-private temporary directory of its own.
        # J1M performs the immutable HF download, source verification,
        # conversion and Q4 quantization remotely. This keeps the 5--6 GiB
        # model off the operator laptop and makes the accepted manifest the
        # sole integrity boundary for the generated deployable artifact.
        ["python3", f"{remote_root}/j1m_runner.py", "--run", "--config", f"{remote_root}/j1m-config.json", "--lock", f"{remote_root}/qwen35-9b.source-lock.json", *(["--retain-comparators"] if selection else [])],
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
        ["python3", f"{remote_root}/remote_model_eval.py", "--model", f"{remote_root}/artifacts/Qwen3.5-9B-Q4_K_M.gguf", "--model-manifest", f"{remote_root}/model-manifest.json", "--model-manifest-lock", f"{remote_root}/model-manifest.sha256", "--source-revision", config["source"]["revision"], "--llama-revision", llama["revision"], "--llama-checkout", checkout, "--engine", f"{build_root}/native/lae-engine", "--evaluator", f"{remote_root}/evaluate_tool_calls.py", "--fixture", f"{remote_root}/production_tool_call_eval.json", "--backend", eval_mode["backend"], "--cuda-device-name", device_name, "--cuda-device-receipt", f"{remote_root}/artifacts/cuda-device-receipt.json", "--toolchain-receipt", f"{remote_root}/artifacts/toolchain-receipt.json", "--receipt", f"{remote_root}/artifacts/eval-receipt.json", "--preflight-receipt", f"{remote_root}/artifacts/startup-preflight-receipt.json", "--timeout", "420"],
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


def _comparator_selection(value: str | None) -> tuple[str, ...]:
    """Parse an approved comparator request; refuse anything else.

    The accepted vocabulary is deliberately a closed set rather than a parsed
    list, so no caller can smuggle an unreviewed arm through the flag.
    """

    if value is None:
        return ()
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("comparator selection is not an approved request")
    selection = _COMPARATOR_SELECTIONS.get(value.strip())
    if selection is None:
        raise ValueError("comparator selection is not an approved request")
    return selection


def _comparator_arms(selection: tuple[str, ...]) -> tuple[str, ...]:
    """Return every arm to evaluate; the same-runtime Q4 arm is never optional."""

    if not selection:
        return ()
    return (_COMPARATOR_BASELINE_ARM, *(arm for arm in selection if arm != _COMPARATOR_BASELINE_ARM))


def _comparator_comparators(selection: tuple[str, ...]) -> tuple[str, ...]:
    """Denominator arms only. The same-runtime Q4 arm is never a comparator.

    ``q4-oracle`` therefore requests one arm and zero comparisons: the
    receipt it produces carries a baseline and a runtime-parity delta, not a
    retention ratio.
    """

    return tuple(arm for arm in selection if arm != _COMPARATOR_BASELINE_ARM)


def _comparator_budget(
    config: dict[str, Any], selection: tuple[str, ...], *,
    setup_seconds: float = _COMPARATOR_SETUP_BUDGET_SECONDS,
    arm_seconds: float = _COMPARATOR_ARM_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Bound the comparator phase against the already-authorised eval clocks.

    The phase runs strictly inside ``execution_deadline``, which is derived
    from the approved ``modes.eval`` clocks, so it adds no authorised spend.
    The projected marginal cost is recorded as evidence, never as a new cap.
    """

    for value in (setup_seconds, arm_seconds):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not 0 < value <= 36000):
            raise ValueError("comparator budget is not a bounded positive duration")
    arms = _comparator_arms(selection)
    envelope = _eval_deadline_ceiling(config)
    static_slack = envelope["run_seconds"] - envelope["ceiling_seconds"]
    required = (float(setup_seconds) + float(arm_seconds) * len(arms)) if arms else 0.0
    hourly = float(config["shadeform_target"]["hourly_usd"])
    return {
        "requested": list(selection),
        "arms": list(arms),
        "setup_budget_seconds": float(setup_seconds),
        "arm_budget_seconds": float(arm_seconds),
        "required_seconds": round(required, 3),
        "static_slack_seconds": round(static_slack, 3),
        "fits_static_worst_case": required <= static_slack,
        "projected_marginal_cost_usd": round(hourly * required / 3600.0, 6),
        "authorized_active_cost_usd": float(config["modes"]["eval"]["active_cost_usd"]),
        "raises_authorized_cost": False,
    }


def _comparator_clock_available(config: dict[str, Any], execution_deadline: float) -> float:
    """Remaining clock that the comparator phase may consume, cleanup reserved."""

    cleanup = float(config["modes"]["eval"]["stage_budgets_seconds"]["cleanup_reserve"])
    return execution_deadline - time.monotonic() - cleanup - _DELETION_RESERVE_SECONDS


def _comparator_phase(
    config: dict[str, Any], selection: tuple[str, ...], *,
    execution_deadline: float | None = None,
) -> dict[str, Any]:
    """Decide the comparator phase. Every refusal carries a typed reason."""

    budget = _comparator_budget(config, selection)
    phase: dict[str, Any] = {
        "engine_available": _COMPARATOR_ENGINE_AVAILABLE,
        "requested": list(selection),
        "arms": list(budget["arms"]),
        "budget": budget,
    }
    if not selection:
        return {**phase, "status": "not_requested", "reason": "comparator_not_requested"}
    if not _COMPARATOR_ENGINE_AVAILABLE:
        return {**phase, "status": "refused", "reason": "comparator_engine_unavailable"}
    if execution_deadline is None:
        return {**phase, "status": "planned"}
    available = _comparator_clock_available(config, execution_deadline)
    if available < budget["required_seconds"]:
        return {**phase, "status": "refused", "reason": "comparator_clock_insufficient",
                "available_seconds": round(available, 3)}
    return {**phase, "status": "approved", "available_seconds": round(available, 3)}


def _comparator_remote_commands(config: dict[str, Any], remote_root: str, selection: tuple[str, ...]) -> list[list[str]]:
    """The comparator phase's remote argv stages, in fixed arm order.

    One upstream ``llama-server`` build from the pinned revision, then one
    bounded arm evaluation per arm.  Every stage is an argv array, nothing is
    shell-concatenated, no bearer appears in any argument, and the only
    network surface any of it opens is ``127.0.0.1`` on the ephemeral host.
    """

    arms = _comparator_arms(selection)
    if not arms:
        return []
    llama = config["llama_cpp"]
    eval_mode = config["modes"]["eval"]
    contract = _tool_eval_contract()
    build_root = j1m_runner.COMPARATOR_SERVER_BUILD_ROOT
    flags = j1m_runner.comparator_server_configure_flags(config)
    commands = j1m_runner.comparator_server_plan(
        config, runner=f"{remote_root}/j1m_runner.py",
        config_path=f"{remote_root}/j1m-config.json", build_root=build_root,
    )
    for index, arm in enumerate(arms):
        commands.append([
            "python3", f"{remote_root}/remote_comparator_eval.py",
            "--arm", arm,
            "--model", f"{remote_root}/artifacts/{_COMPARATOR_ARM_FILENAMES[arm]}",
            "--scan-receipt", f"{remote_root}/comparator-anchor-scan-receipt.json",
            "--scan-receipt-sha256", _APPROVED_SCAN_RECEIPT_SHA256,
            "--server", j1m_runner.comparator_server_binary(build_root),
            "--llama-revision", llama["revision"],
            "--llama-checkout", llama["checkout"],
            *[part for flag in flags for part in ("--server-build-flag", flag)],
            "--evaluator", f"{remote_root}/evaluate_tool_calls.py",
            "--fixture", f"{remote_root}/production_tool_call_eval.json",
            # ``--token-file`` is deliberately absent, for the same reason it is
            # absent from the eval stage: it names a path on a host this process
            # has not contacted, and the argv policy accepts a token-file operand
            # only when it is a canonical private handle *here*.  Naming it
            # refused every comparator stage before it could spawn.  The arm
            # driver mints its bearer in an owner-private directory of its own
            # and removes it with the server.
            "--receipt", f"{remote_root}/artifacts/comparator-receipt-{arm}.json",
            "--port", str(_COMPARATOR_BASE_PORT + index),
            "--context", str(contract["context_tokens"]),
            "--gpu-layers", str(eval_mode["gpu_layers"]),
            "--backend", eval_mode["backend"],
            "--timeout", str(int(_COMPARATOR_ARM_BUDGET_SECONDS)),
        ])
    return commands


def _comparator_cleanup_commands(config: dict[str, Any], remote_root: str) -> list[list[str]]:
    """The deferred intermediate-deletion tail ``--retain-comparators`` moved.

    These are the runner's own final three stages with the runner's own
    remote paths, so deletion, the post-cleanup receipt and the manifest
    still happen on the same host -- only later.  Never dropped.
    """

    return j1m_runner.comparator_cleanup_plan(
        "/scratch/j1m/artifacts", f"{remote_root}/j1m_runner.py",
        f"{remote_root}/j1m-config.json", f"{remote_root}/qwen35-9b.source-lock.json",
    )


def _comparator_stage_timeout(config: dict[str, Any], command: list[str]) -> float:
    """Bound one comparator stage. Kept separate from ``_eval_stage_timeout``.

    The comparator budgets are deliberately *not* summed into
    ``_eval_deadline_ceiling`` (``model/COMPARATOR_EVAL.md`` section 2.4), so
    they live in their own function and that one returns the same numbers it
    returns today.
    """

    if command[0:2] == ["cmake", "-S"]:
        return _COMPARATOR_CONFIGURE_BUDGET_SECONDS
    if command[0:2] == ["cmake", "--build"]:
        return _COMPARATOR_BUILD_BUDGET_SECONDS
    if command[0] == "python3" and any("remote_comparator_eval.py" in part for part in command):
        return _COMPARATOR_ARM_BUDGET_SECONDS
    if command[0] == "rm":
        return 120.0
    return float(config["modes"]["eval"]["stage_budgets_seconds"]["evaluation"])


def _eval_fetch_allowlist(config: dict[str, Any], selection: tuple[str, ...]) -> list[str]:
    """Effective salvage allowlist. Receipts only; weights are never listed."""

    names = list(config["artifacts"]["eval_fetch_allowlist"])
    if selection:
        names.extend(f"comparator-receipt-{arm}.json" for arm in _comparator_arms(selection))
        names.append(_APPROVED_SCAN_RECEIPT_RELATIVE.name)
    if any(not isinstance(name, str) or name.lower().endswith(".gguf") for name in names):
        raise ValueError("salvage allowlist may not name model weights")
    return names


def _product_engine_reference(lifecycle: dict[str, Any]) -> dict[str, Any] | None:
    """Project the verified product-engine Q4 receipt for runtime-parity only."""

    receipt = lifecycle.get("eval_receipt")
    metrics = receipt.get("metrics") if isinstance(receipt, dict) else None
    if not isinstance(metrics, dict):
        return None
    case_count = metrics.get("case_count")
    passed = metrics.get("passed")
    if (isinstance(case_count, bool) or not isinstance(case_count, int) or case_count <= 0 or
            isinstance(passed, bool) or not isinstance(passed, int) or not 0 <= passed <= case_count):
        return None
    return {
        "engine": "lae-engine",
        "case_count": case_count,
        "passed": passed,
        "score_points": compare_model_quality.score_points(passed, case_count),
    }


def _write_comparison_receipt(
    destination: Path, selection: tuple[str, ...], *,
    phase_reason: str | None = None,
    fixture_sha256: str | None = None,
    product_engine_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn whatever arm receipts were salvaged into the comparison verdicts.

    A phase that never ran, or an arm whose receipt is missing or invalid,
    becomes a typed skip. No number is ever computed from an absent receipt.
    """

    if phase_reason is not None and phase_reason not in _COMPARATOR_SKIP_REASONS:
        raise ValueError("comparator skip reason is not typed")
    gate = compare_model_quality.load_gate()
    comparators = _comparator_comparators(selection)
    arms: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    if phase_reason is not None:
        skipped = [{"comparator": arm, "reason": phase_reason} for arm in comparators]
        # ``q4-oracle`` requests one arm and zero comparators, so a refusal used
        # to write `{"requested": [], "skipped": []}` -- byte-indistinguishable
        # from a run that asked for nothing, with the typed reason surviving
        # only in the lifecycle dict and nowhere in the durable artifact. The
        # baseline arm is always evaluated, so record its skip too and the one
        # selection whose refusal was untyped stops being untyped.
        skipped.append({"comparator": _COMPARATOR_BASELINE_ARM, "reason": phase_reason})
    else:
        # A receipt that arrived and failed verification is a different fact
        # from one that never arrived, and an operator must be able to tell
        # them apart, so the two are not collapsed into one reason.
        invalid: set[str] = set()
        for arm in _comparator_arms(selection):
            path = destination / f"comparator-receipt-{arm}.json"
            if not path.is_file():
                continue
            try:
                arms[arm] = compare_model_quality.load_arm_metrics(path)
            except compare_model_quality.ComparisonError:
                invalid.add(arm)
        for arm in comparators:
            if arm in invalid or _COMPARATOR_BASELINE_ARM in invalid:
                skipped.append({"comparator": arm, "reason": "comparator_receipt_invalid"})
        # Same reasoning for the arm that is never a comparator: an absent or
        # unreadable baseline receipt is why there is no number, and the
        # receipt has to say which of the two it was.
        if _COMPARATOR_BASELINE_ARM in invalid:
            skipped.append({"comparator": _COMPARATOR_BASELINE_ARM,
                            "reason": "comparator_receipt_invalid"})
        elif _COMPARATOR_BASELINE_ARM not in arms:
            skipped.append({"comparator": _COMPARATOR_BASELINE_ARM,
                            "reason": "comparator_receipt_missing"})
    receipt = compare_model_quality.build_comparison_receipt(
        arms=arms, requested=list(comparators), gate=gate,
        fixture_sha256=fixture_sha256, skipped=skipped,
        product_engine_reference=product_engine_reference,
    )
    compare_model_quality.write_comparison_receipt(
        destination / "comparison-receipt.json", receipt)
    return receipt


def _verify_eval_diagnostics(value: Any, *, errors: int, categories: set[str], category_errors: dict[str, int]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "total_errors", "overall", "by_category"} or value.get("schema") != "local_bmo.tool-call-eval-diagnostics.v1":
        raise ValueError("eval receipt diagnostics invalid")
    if isinstance(value.get("total_errors"), bool) or not isinstance(value.get("total_errors"), int) or value["total_errors"] != errors:
        raise ValueError("eval receipt diagnostics total invalid")
    overall = value.get("overall")
    by_category = value.get("by_category")
    if not isinstance(overall, dict) or not isinstance(by_category, dict) or set(by_category) != categories:
        raise ValueError("eval receipt diagnostics invalid")
    def histogram(item: Any) -> int:
        if not isinstance(item, dict):
            raise ValueError("eval receipt diagnostics invalid")
        total = 0
        for code, amount in item.items():
            if code not in _EVAL_DIAGNOSTIC_CODES or isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > 40:
                raise ValueError("eval receipt diagnostics code invalid")
            total += amount
        return total
    if histogram(overall) != errors:
        raise ValueError("eval receipt diagnostics total invalid")
    combined: dict[str, int] = {}
    category_total = 0
    for category in categories:
        category_histogram = by_category[category]
        count = histogram(category_histogram)
        if count != category_errors[category]:
            raise ValueError("eval receipt diagnostics total invalid")
        category_total += count
        for code, amount in category_histogram.items():
            combined[code] = combined.get(code, 0) + amount
    if category_total != errors or combined != overall:
        raise ValueError("eval receipt diagnostics total invalid")
    return {"schema": value["schema"], "total_errors": value["total_errors"], "overall": dict(overall), "by_category": {category: dict(by_category[category]) for category in sorted(categories)}}


def _verify_eval_quality_diagnostics(value: Any, *, failed: int, categories: set[str], category_failed: dict[str, int]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "total_failed", "overall", "by_category"} or value.get("schema") != "local_bmo.tool-call-quality-diagnostics.v1":
        raise ValueError("eval receipt quality diagnostics invalid")
    total = value.get("total_failed")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != failed:
        raise ValueError("eval receipt quality diagnostics total invalid")
    overall = value.get("overall")
    by_category = value.get("by_category")
    if not isinstance(overall, dict) or not isinstance(by_category, dict) or set(by_category) != categories:
        raise ValueError("eval receipt quality diagnostics invalid")
    def histogram(item: Any) -> int:
        if not isinstance(item, dict):
            raise ValueError("eval receipt quality diagnostics invalid")
        count = 0
        for code, amount in item.items():
            if code not in _EVAL_QUALITY_CODES or isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > 40:
                raise ValueError("eval receipt quality diagnostics code invalid")
            count += amount
        return count
    if histogram(overall) != failed:
        raise ValueError("eval receipt quality diagnostics total invalid")
    combined: dict[str, int] = {}
    category_total = 0
    for category in categories:
        category_histogram = by_category[category]
        count = histogram(category_histogram)
        if count != category_failed[category]:
            raise ValueError("eval receipt quality diagnostics total invalid")
        category_total += count
        for code, amount in category_histogram.items():
            combined[code] = combined.get(code, 0) + amount
    if category_total != failed or combined != overall:
        raise ValueError("eval receipt quality diagnostics total invalid")
    return {"schema": value["schema"], "total_failed": total, "overall": dict(overall), "by_category": {category: dict(by_category[category]) for category in sorted(categories)}}


def _verify_eval_canary(value: Any, *, expected_tool_count: int = 11, expected_context_tokens: int = 2048, expected_output_reserve_tokens: int = 64) -> dict[str, Any]:
    if (isinstance(expected_output_reserve_tokens, bool) or not isinstance(expected_output_reserve_tokens, int) or
            not 1 <= expected_output_reserve_tokens <= _MAX_OUTPUT_RESERVE_TOKENS):
        raise ValueError("eval receipt canary invalid")
    if not isinstance(value, dict) or set(value) != {"attempted", "passed", "error_code", "tool_count", "message_chars", "prompt_tokens", "context_tokens", "output_reserve_tokens"} or value.get("attempted") is not True:
        raise ValueError("eval receipt canary invalid")
    if (not isinstance(value.get("passed"), bool) or value.get("tool_count") != expected_tool_count or
            value.get("message_chars") != 2400 or value.get("context_tokens") != expected_context_tokens or
            value.get("output_reserve_tokens") != expected_output_reserve_tokens):
        raise ValueError("eval receipt canary invalid")
    code = value.get("error_code")
    prompt_tokens = value.get("prompt_tokens")
    if value["passed"] and (code is not None or isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or not 513 <= prompt_tokens <= value["context_tokens"] - value["output_reserve_tokens"]):
        raise ValueError("eval receipt canary invalid")
    if not value["passed"] and (code not in _EVAL_DIAGNOSTIC_CODES or prompt_tokens is not None):
        raise ValueError("eval receipt canary invalid")
    return dict(value)


def _verify_eval_child(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) not in ({"exit_code"}, {"signal"}):
        raise ValueError("eval receipt child status invalid")
    key = next(iter(value))
    number = value[key]
    if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 255 or (key == "signal" and number == 0):
        raise ValueError("eval receipt child status invalid")
    return {key: number}


def _verify_eval_canary_coherence(canary: dict[str, Any], *, metrics: dict[str, Any], summary: dict[str, Any], diagnostics: dict[str, Any], expected_count: int, categories: set[str]) -> None:
    if canary["passed"]:
        return
    code = canary["error_code"]
    if metrics["passed"] != 0 or metrics["failed"] != 0 or metrics["errors"] != expected_count or diagnostics["overall"] != {code: expected_count}:
        raise ValueError("eval receipt canary invalid")
    for category in categories:
        item = summary[category]
        if item["passed"] != 0 or item["failed"] != 0 or item["errors"] != item["case_count"] or diagnostics["by_category"][category] != {code: item["errors"]}:
            raise ValueError("eval receipt canary invalid")


def _without_run_identity(payload: dict[str, Any]) -> set[str]:
    """Return a receipt's key set with the run-identity binding removed."""

    return set(payload) - set(j1m_runner.RUN_IDENTITY_FIELDS)


def _verify_eval_receipt(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Accept only the bounded aggregate receipt produced by remote eval."""

    payload = _bounded_json(path, _EVAL_RECEIPT_MAX_BYTES)
    allowed_top_level = {"schema", "status", "artifact", "fixture", "engine", "model_preflight", "cuda_device", "toolchain", "metrics", "duration_ms", "prompt_response_logging", "tokens_logged", "child", *j1m_runner.RUN_IDENTITY_FIELDS}
    # The run-identity binding is *required* where it is load-bearing: at the
    # salvage fetch boundary, before an untrusted receipt is ever published
    # (``_salvage_validated_payload``).  These verifiers read a file salvage
    # has already proved and published this run, so they accept the binding
    # without re-demanding it and no receipt fixture has to grow a field that
    # is not part of what is being verified here.
    required_top_level = {"schema", "status", "artifact", "fixture", "engine", "model_preflight", "toolchain", "metrics", "prompt_response_logging", "tokens_logged"}
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
    expected_fixture = _tool_eval_contract()["fixture_identity"]
    recorded_fixture = payload.get("fixture")
    if recorded_fixture != expected_fixture:
        raise ValueError("eval receipt fixture identity mismatch")
    model_preflight = payload.get("model_preflight")
    if (not isinstance(model_preflight, dict) or set(model_preflight) != {"valid", "code", "status", "size_bytes", "sha256", "gguf_version"} or
            model_preflight.get("valid") is not True or model_preflight.get("code") != "ok" or
            model_preflight.get("status") != "verified" or model_preflight.get("size_bytes") != artifact["size_bytes"] or
            model_preflight.get("sha256") != artifact["sha256"] or model_preflight.get("gguf_version") != 3):
        raise ValueError("eval receipt model preflight invalid")
    metrics = payload.get("metrics")
    metric_keys = {"case_count", "passed", "failed", "errors", "peak_rss_kib", "category_summary"}
    optional_metric_keys = {"canary", "error_diagnostics", "quality_diagnostics"}
    if (not isinstance(metrics, dict) or not metric_keys <= set(metrics) or set(metrics) - metric_keys - optional_metric_keys or
            payload.get("status") not in {"verified", "completed_with_failures", "failed"} or
            any(isinstance(metrics.get(key), bool) or not isinstance(metrics.get(key), int) or metrics[key] < 0 for key in ("case_count", "passed", "failed", "errors"))):
        raise ValueError("eval receipt metrics invalid")
    summary = metrics.get("category_summary")
    eval_contract = _tool_eval_contract()
    expected_count = eval_contract["case_count"]
    expected_categories = eval_contract["categories"]
    expected_category_counts = eval_contract["category_counts"]
    if metrics["case_count"] != expected_count or not isinstance(summary, dict) or set(summary) != expected_categories:
        raise ValueError("eval receipt metrics invalid")
    category_total = 0
    for category in expected_categories:
        item = summary[category]
        if not isinstance(item, dict) or set(item) != {"case_count", "passed", "failed", "errors"}:
            raise ValueError("eval receipt category summary invalid")
        if any(isinstance(item.get(key), bool) or not isinstance(item.get(key), int) or item[key] < 0 for key in ("case_count", "passed", "failed", "errors")):
            raise ValueError("eval receipt category summary invalid")
        if item["case_count"] != expected_category_counts[category]:
            raise ValueError("eval receipt category summary invalid")
        if item["passed"] + item["failed"] + item["errors"] != item["case_count"]:
            raise ValueError("eval receipt category summary invalid")
        category_total += item["case_count"]
    if category_total != expected_count:
        raise ValueError("eval receipt category summary total invalid")
    if metrics["case_count"] != expected_count or sum(metrics[key] for key in ("passed", "failed", "errors")) != expected_count:
        raise ValueError("eval receipt metric totals invalid")
    if any(sum(summary[category][field] for category in expected_categories) != metrics[field] for field in ("case_count", "passed", "failed", "errors")):
        raise ValueError("eval receipt metric totals invalid")
    if "error_diagnostics" not in metrics or "canary" not in metrics or "quality_diagnostics" not in metrics:
        raise ValueError("eval receipt diagnostics missing")
    diagnostics = _verify_eval_diagnostics(metrics["error_diagnostics"], errors=metrics["errors"], categories=expected_categories, category_errors={category: summary[category]["errors"] for category in expected_categories})
    canary = _verify_eval_canary(metrics["canary"], expected_tool_count=eval_contract["tool_count"], expected_context_tokens=eval_contract["context_tokens"], expected_output_reserve_tokens=eval_contract["output_reserve_tokens"])
    _verify_eval_canary_coherence(canary, metrics=metrics, summary=summary, diagnostics=diagnostics, expected_count=expected_count, categories=expected_categories)
    quality_diagnostics = _verify_eval_quality_diagnostics(metrics["quality_diagnostics"], failed=metrics["failed"], categories=expected_categories, category_failed={category: summary[category]["failed"] for category in expected_categories})
    child = _verify_eval_child(payload.get("child")) if payload.get("status") == "failed" else None
    if payload.get("status") != "failed" and "child" in payload:
        raise ValueError("eval receipt child status invalid")
    status = payload["status"]
    all_passed = metrics["passed"] == expected_count and metrics["failed"] == 0 and metrics["errors"] == 0
    has_failure = metrics["failed"] > 0 or metrics["errors"] > 0
    if status == "failed":
        if child is None:
            raise ValueError("eval receipt child status invalid")
    elif (status == "verified") != all_passed or (status == "completed_with_failures") != has_failure:
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
    # ``tokens_logged``, not ``token_logging``: the flag asserts the engine
    # did not log *model* tokens, but a field name delimited as ``_token_``
    # is credential-shaped, and ``validate_persisted_output`` -- which the
    # descriptor-safe publisher applies to every byte it writes -- rejected
    # the entire serialized receipt for carrying it.  A paid eval therefore
    # produced an eval receipt that could never be published.  The fix is
    # the field name; loosening the credential detector is not.
    if payload.get("prompt_response_logging") is not False or payload.get("tokens_logged") is not False:
        raise ValueError("eval receipt logging policy missing")
    selected_metrics = {key: metrics[key] for key in ("case_count", "passed", "failed", "errors", "peak_rss_kib")}
    if summary is not None:
        selected_metrics["category_summary"] = summary
    selected_metrics["error_diagnostics"] = diagnostics
    selected_metrics["canary"] = canary
    selected_metrics["quality_diagnostics"] = quality_diagnostics
    selected_versions = {name: dict(versions[name]) for name in minimums}
    selected_packages = {name: packages[name] for name in expected_packages}
    return {
        "status": status,
        **({"child": child} if child is not None else {}),
        "artifact": dict(recorded),
        "fixture": dict(recorded_fixture),
        "engine": dict(engine),
        "model_preflight": dict(model_preflight),
        "cuda_device": {**cuda_device, "device": dict(device)},
        "metrics": selected_metrics,
        "toolchain": {"schema": toolchain["schema"], "status": toolchain["status"], "required": dict(toolchain["required"]), "versions": selected_versions, "packages": selected_packages, "package_install": toolchain["package_install"]},
    }


def _verify_eval_artifact_receipt(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Verify the separately salvaged artifact-preparation attestation."""

    payload = _bounded_json(path, _EVAL_ARTIFACT_RECEIPT_MAX_BYTES)
    required = {"schema", "status", "name", "size_bytes", "sha256", "manifest_sha256", "manifest_lock_sha256"}
    if not isinstance(payload, dict) or _without_run_identity(payload) != required or payload.get("schema") != "local_bmo.j1m.remote-eval-artifact-receipt.v1" or payload.get("status") != "verified":
        raise ValueError("eval artifact receipt schema mismatch")
    if (payload.get("name") != artifact.get("name") or payload.get("size_bytes") != artifact.get("size_bytes") or
            payload.get("sha256") != artifact.get("sha256")):
        raise ValueError("eval artifact receipt identity mismatch")
    for field in ("manifest_sha256", "manifest_lock_sha256"):
        value = payload.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) or value != _APPROVED_EVAL_MANIFEST_SHA256:
            raise ValueError("eval artifact receipt trust anchor mismatch")
    if not isinstance(payload.get("size_bytes"), int) or isinstance(payload.get("size_bytes"), bool) or payload["size_bytes"] <= 0:
        raise ValueError("eval artifact receipt size invalid")
    if not isinstance(payload.get("sha256"), str) or len(payload["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in payload["sha256"]):
        raise ValueError("eval artifact receipt hash invalid")
    return {key: payload[key] for key in ("schema", "status", "name", "size_bytes", "sha256", "manifest_sha256", "manifest_lock_sha256")}


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
        if (_without_run_identity(payload) != {"schema", "status", "size_bytes", "sha256", "gguf_version"} or
                payload.get("size_bytes") != artifact["size_bytes"] or payload.get("sha256") != artifact["sha256"] or
                payload.get("gguf_version") != 3):
            raise ValueError("startup preflight receipt identity invalid")
        return {"status": "verified", "size_bytes": payload["size_bytes"], "sha256": payload["sha256"], "gguf_version": 3}
    elif status == "not_started":
        if _without_run_identity(payload) != {"schema", "status", "error_code"} or payload.get("error_code") != "engine_model_preflight_not_started":
            raise ValueError("startup preflight receipt not_started outcome invalid")
        return {"status": "not_started", "error_code": payload["error_code"]}
    elif status in {"rejected", "timeout", "oversize", "terminated", "failed"}:
        if not _without_run_identity(payload) <= {"schema", "status", "error_code", "validator_code", "child"}:
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


class _SalvageRefusal(Exception):
    """A typed, bounded reason one allowlisted receipt was not published."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _salvage_transport_argv(
    info: dict[str, Any], identity: Path, known_hosts: Path, name: str, staged: Path,
) -> list[str]:
    """Build the exact non-shell argv for one allowlisted receipt fetch.

    ``name`` is a source-fixed allowlist key, so the remote operand is a
    constant directory joined with a constant basename.  No caller value, no
    glob, and no remote directory listing can influence it.
    """

    if name not in _SALVAGE_RECEIPT_ALLOWLIST:
        raise _SalvageRefusal("salvage_name_not_allowlisted")
    # The endpoint is read from the same creation/activation record the run
    # itself used; salvage never re-resolves a host or accepts a new address.
    instance_info = info["instance_info"]
    ip, user, _port = sf._endpoint(instance_info)
    # ``sf._endpoint`` accepts any ``ipaddress.ip_address``.  An IPv6 endpoint
    # would need bracketing in the operand and contradicts the ``-4`` pin, so
    # refuse it explicitly instead of emitting a malformed operand that fails
    # later as an opaque transport error.
    if ipaddress.ip_address(ip).version != 4:
        raise _SalvageRefusal("salvage_endpoint_not_ipv4")
    base = sf.scp_base(instance_info, identity, known_hosts)
    return [
        base[0],
        # IPv4 only -- proved above, not assumed -- no agent forwarding, no
        # port/stream forwarding, and no local command execution.  ``scp_base``
        # already pins BatchMode, StrictHostKeyChecking, the pinned known_hosts
        # file, ConnectTimeout, IdentitiesOnly, and an empty system config.
        "-4",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "PermitLocalCommand=no",
        *base[1:],
        f"{user}@{ip}:{_SALVAGE_REMOTE_DIRECTORY}/{name}",
        str(staged),
    ]


def _salvage_transport_error_code(receipt: dict[str, Any]) -> str:
    """Classify one failed transfer into a finite, value-free reason.

    OpenSSH signals a host-key mismatch only in prose, and ``_remote`` replaces
    ``stderr_tail`` with ``"<redacted>"`` whenever the text fails the persisted
    output policy, so the English-substring match is *evidence*, not proof.
    ``scp`` exits 255 for every transport-layer refusal including a failed host
    key, so an exit code of 255 with unreadable stderr is reported as
    ``salvage_transport_refused`` -- distinct from a plain non-zero exit -- and
    the receipt records which of the two established the classification.
    """

    status = receipt.get("status")
    if status == "transport_timeout":
        return "salvage_timeout"
    if status == "transport_os":
        return "salvage_transport_os"
    tail = str(receipt.get("stderr_tail") or "")
    if any(marker in tail.casefold() for marker in _SALVAGE_HOST_KEY_MARKERS):
        return "salvage_host_key_mismatch"
    if receipt.get("exit_code") == 255:
        return "salvage_transport_refused"
    return "salvage_transport_failed"


def _salvage_staged_bytes(staged: Path, limit: int) -> bytes:
    """Read one staged transfer through a descriptor, refusing oversize data.

    The staged file is untrusted remote output.  It is opened without following
    links, proven to be an ordinary single-link file owned by this process, and
    read to at most ``limit`` bytes; a file at or beyond the bound is refused
    rather than truncated.
    """

    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise _SalvageRefusal("salvage_descriptor_unavailable")
    try:
        descriptor = os.open(
            staged, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise _SalvageRefusal("salvage_missing") from None
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077):
            raise _SalvageRefusal("salvage_not_private_regular_file")
        if before.st_size > limit:
            raise _SalvageRefusal("salvage_oversize")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise _SalvageRefusal("salvage_oversize")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                after.st_nlink != 1 or total != after.st_size):
            raise _SalvageRefusal("salvage_changed_during_read")
        return b"".join(chunks)
    except _SalvageRefusal:
        raise
    except OSError:
        raise _SalvageRefusal("salvage_read_failed") from None
    finally:
        os.close(descriptor)


def _salvage_identity_claims(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the artifact-identity fields this receipt asserts, if any.

    A receipt listed in ``_SALVAGE_REQUIRED_ARTIFACT_CLAIMS`` must carry every
    named field: omitting one is a refusal, not a way to skip the comparison.
    """

    if name == "eval-artifact-receipt.json":
        claims = {key: payload.get(key) for key in ("name", "size_bytes", "sha256")}
    elif name == "eval-receipt.json":
        recorded = payload.get("artifact")
        if not isinstance(recorded, dict):
            raise _SalvageRefusal("salvage_identity_mismatch")
        claims = {key: recorded.get(key) for key in ("name", "size_bytes", "sha256")}
    elif name == "comparator-receipt-q4_k_m.json":
        claims = {"sha256": payload.get("artifact_sha256")}
    elif name == "startup-preflight-receipt.json" and payload.get("status") == "verified":
        claims = {key: payload.get(key) for key in ("size_bytes", "sha256")}
    else:
        return {}
    required = _SALVAGE_REQUIRED_ARTIFACT_CLAIMS.get(name, ())
    if any(claims.get(field) is None for field in required):
        raise _SalvageRefusal("salvage_identity_missing")
    return claims


def _salvage_validated_payload(
    name: str, raw: bytes, run_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    """Prove untrusted remote bytes are the expected receipt for this run."""

    if len(raw) > j1m_runner._RECEIPT_MAX_BYTES:
        # The transport's own per-file cap is wider than the strict receipt
        # decoder's bound; keep the reason precise rather than reporting a
        # parse failure for a file that is simply too large.
        raise _SalvageRefusal("salvage_oversize")
    try:
        payload = j1m_runner._bounded_json_loads(raw)
    except ValueError:
        # Covers malformed JSON, duplicate keys, non-finite numbers, and
        # anything exceeding the strict decoder's own bound.
        raise _SalvageRefusal("salvage_invalid_json") from None
    if not isinstance(payload, dict):
        raise _SalvageRefusal("salvage_not_an_object")
    if payload.get("schema") != _SALVAGE_RECEIPT_ALLOWLIST[name]:
        raise _SalvageRefusal("salvage_schema_mismatch")
    if not _SALVAGE_REQUIRED_KEYS[name] <= set(payload):
        raise _SalvageRefusal("salvage_required_key_missing")
    try:
        j1m_runner.validate_persisted_receipt(payload)
        # Apply the publisher's own gate here too.  ``_private_atomic_write``
        # runs ``validate_persisted_output`` over the exact bytes, and a
        # receipt that fails it was previously recorded as the generic
        # ``salvage_failed`` from the outer handler -- which said nothing about
        # why a paid run came back with no receipt.
        j1m_runner.validate_persisted_output(raw)
    except ValueError:
        raise _SalvageRefusal("salvage_receipt_content_refused") from None
    # Run-identity binding is REQUIRED, not opportunistic.  Every allowlisted
    # receipt must carry every field, and every field must equal what this run
    # uploaded to the host in ``run-identity.json`` before the first
    # receipt-producing command.  A receipt written by an earlier run under the
    # same remote artifact directory therefore carries that run's binding and
    # is refused here rather than published as this run's evidence.
    identity = run_identity if isinstance(run_identity, dict) else {}
    for field in sorted(_SALVAGE_REQUIRED_IDENTITY):
        expected = identity.get(field)
        if not isinstance(expected, str) or not expected:
            # The caller could not state its own identity, so nothing fetched
            # can be proved to belong to this run.
            raise _SalvageRefusal("salvage_run_identity_unavailable")
        if field not in payload:
            raise _SalvageRefusal("salvage_identity_missing")
        if payload[field] != expected:
            raise _SalvageRefusal("salvage_identity_mismatch")
    if name in _SALVAGE_COMPARATOR_RECEIPTS:
        # A retention number computed from an arm that scored a different
        # fixture is worse than no number, so the fixture digest is part of the
        # required binding rather than something checked later. Every arm must
        # also declare the artifact digest it scored, even the two whose files
        # this process cannot independently verify.
        expected_fixture = identity.get("fixture_sha256")
        if not isinstance(expected_fixture, str) or not expected_fixture:
            raise _SalvageRefusal("salvage_run_identity_unavailable")
        declared = payload.get("artifact_sha256")
        if not isinstance(declared, str) or not re.fullmatch(r"[0-9a-f]{64}", declared):
            raise _SalvageRefusal("salvage_identity_missing")
        if payload.get("fixture_sha256") != expected_fixture:
            raise _SalvageRefusal("salvage_identity_mismatch")
        if payload.get("arm") != name[len("comparator-receipt-"):-len(".json")]:
            raise _SalvageRefusal("salvage_identity_mismatch")
    artifact = identity.get("artifact")
    claims = _salvage_identity_claims(name, payload)
    if claims and isinstance(artifact, dict):
        for field, value in claims.items():
            if field in artifact and value != artifact[field]:
                raise _SalvageRefusal("salvage_identity_mismatch")
    return payload


def _salvage_host_key_pin(known_hosts: Path, host_key: dict[str, Any] | None) -> str:
    """Prove the host key pinned at first connection is still the one in use.

    ``acquire_pinned_host_key`` wrote ``known_hosts`` before the first remote
    command, either from a provider fingerprint or from two independent stable
    scans (recorded residual TOFU).  Salvage refuses to run unless that exact
    file is still an owner-private regular file carrying the same number of
    pinned keys, and every transfer then runs with ``StrictHostKeyChecking=yes``
    against it, so a host swapped between the run and teardown fails the
    transfer instead of silently re-pinning.
    """

    try:
        info = os.lstat(known_hosts)
    except OSError:
        raise ValueError("salvage host key pin is unavailable") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != os.getuid() or info.st_nlink != 1 or
            stat.S_IMODE(info.st_mode) & 0o077 or not 0 < info.st_size <= 65536):
        raise ValueError("salvage host key pin is not a private regular file")
    raw = known_hosts.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    lines = [line for line in raw.decode("utf-8", errors="strict").splitlines() if line.strip()]
    if not lines:
        raise ValueError("salvage host key pin is empty")
    if isinstance(host_key, dict):
        if host_key.get("status") != "verified":
            raise ValueError("salvage host key pin was never verified")
        # A line count cannot detect a key swapped for another of the same
        # shape.  ``acquire_pinned_host_key`` records the exact digest of the
        # bytes it pinned, so compare those when they exist and keep the
        # count comparison only as the fallback for an acquisition record that
        # predates the digest.
        recorded = host_key.get("known_hosts_sha256")
        if isinstance(recorded, str) and recorded:
            if recorded != digest:
                raise ValueError("salvage host key pin changed since acquisition")
        else:
            expected = host_key.get("key_count")
            if isinstance(expected, int) and not isinstance(expected, bool) and len(lines) != expected:
                raise ValueError("salvage host key pin changed since acquisition")
    return digest


def _write_salvage_receipt(destination: Path, receipt: dict[str, Any]) -> None:
    """Publish local salvage evidence; never let it break teardown."""

    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    j1m_runner._private_atomic_write(
        destination / _SALVAGE_RECEIPT_NAME, payload,
        trusted_root=j1m_runner.PRIVATE_OUTPUT_ROOT,
    )


def _salvage(
    info: dict[str, Any],
    identity: Path,
    known_hosts: Path,
    destination: Path,
    names: list[str],
    *,
    deadline: float | None = None,
    q4_expected_gib: float = 6.0,
    run_identity: dict[str, Any] | None = None,
    host_key: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch only source-allowlisted receipts, bounded and fail-closed.

    Each allowlisted name is attempted independently so one missing or refused
    receipt can never stop cleanup.  Nothing here raises for a per-file
    failure: the caller's ``finally`` must always reach exact teardown.  The
    destination and host-key preconditions are the only conditions that refuse
    the whole operation, and they are proved before any process is spawned.

    ``q4_expected_gib`` is retained for signature compatibility.  The bounded
    transport carries receipts only, so no size-aware model-transfer budget
    applies; the deployable Q4 artifact is not a salvageable name.
    """

    if not _EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE:
        raise ValueError("external salvage transport is unavailable in this source slice")
    try:
        salvage_ancestors = j1m_runner._private_ancestor_snapshot(
            destination, j1m_runner.PRIVATE_OUTPUT_ROOT,
        )
        destination_stat = os.lstat(destination)
    except OSError:
        raise ValueError("salvage destination is unavailable") from None
    if (stat.S_ISLNK(destination_stat.st_mode) or not stat.S_ISDIR(destination_stat.st_mode) or
            destination_stat.st_uid != os.getuid() or stat.S_IMODE(destination_stat.st_mode) & 0o077):
        raise ValueError("salvage destination is not a private directory")
    if not j1m_runner._private_ancestors_stable(salvage_ancestors):
        raise ValueError("salvage destination changed")
    # Re-prove the exact destination inode immediately after the ancestor
    # check.  A directory replaced by a symlink in that window must not be able
    # to reach a transfer, and publication later re-proves it again through a
    # no-follow parent descriptor.
    try:
        rechecked = os.lstat(destination)
    except OSError:
        raise ValueError("salvage destination changed") from None
    if (stat.S_ISLNK(rechecked.st_mode) or not stat.S_ISDIR(rechecked.st_mode) or
            (rechecked.st_dev, rechecked.st_ino) != (destination_stat.st_dev, destination_stat.st_ino) or
            rechecked.st_uid != os.getuid() or stat.S_IMODE(rechecked.st_mode) & 0o077):
        raise ValueError("salvage destination changed")
    if not isinstance(names, list) or len(names) > _SALVAGE_MAX_REQUESTED_NAMES:
        raise ValueError("salvage request exceeds its bounded candidate count")
    known_hosts_sha256 = _salvage_host_key_pin(known_hosts, host_key)

    # The ephemeral private key is passed to the transfer as a file handle, and
    # ``validate_persisted_argv`` will refuse an argv whose ``-i`` operand is
    # not a canonical private handle.  Prove that here so an unusable key is a
    # typed, value-free refusal per receipt instead of an opaque local error,
    # and so no process is spawned with a key the argv policy rejects.
    identity_usable = j1m_runner._canonical_private_handle_path(str(identity))

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    fetched = 0
    total_bytes = 0
    seen: set[str] = set()

    def remaining_budget() -> float:
        wall = _SALVAGE_WALL_CLOCK_SECONDS - (time.monotonic() - started)
        if deadline is None:
            return wall
        return min(wall, deadline - time.monotonic() - _DELETION_RESERVE_SECONDS)

    with tempfile.TemporaryDirectory(prefix="j1m-salvage-") as staging:
        staging_root = Path(staging)
        os.chmod(staging_root, 0o700)
        # ``scp`` streams into the staging directory before any size check can
        # run, so prove there is room for the whole worst case twice over --
        # once staged, once published -- before a single transfer starts.
        try:
            free_bytes = shutil.disk_usage(staging_root).free
        except OSError:
            free_bytes = 0
        if free_bytes < _SALVAGE_MIN_FREE_BYTES:
            raise ValueError("salvage staging has insufficient free space")
        for name in names:
            record: dict[str, Any] = {"name": name}
            if isinstance(name, str) and name in _SALVAGE_NON_RECEIPT_NAMES:
                # Legitimately configured, deliberately not carryable: the
                # weights are not evidence and never reach the operator laptop,
                # and the two non-object outputs carry neither a schema nor a
                # run binding.  Say so in its own typed reason rather than
                # implying a configuration mistake.
                record.update({
                    "status": "salvage_failed",
                    "error_code": "salvage_refused_non_receipt",
                    "refusal_class": _SALVAGE_NON_RECEIPT_NAMES[name],
                })
                results.append(record)
                continue
            if not isinstance(name, str) or name not in _SALVAGE_RECEIPT_ALLOWLIST:
                # A configuration or caller may name anything; only source can
                # make a name fetchable.  Everything else is recorded, never
                # transferred, and never turned into a remote path.
                record = {"name": str(name)[:128], "status": "salvage_failed",
                          "error_code": "salvage_name_not_allowlisted"}
                results.append(record)
                continue
            if name in seen:
                record.update({"status": "salvage_failed", "error_code": "salvage_duplicate_name"})
                results.append(record)
                continue
            seen.add(name)
            # Cheap source-fixed bounds first, then the cleanup-safety clock,
            # then the key handle.  Exhausted budget must dominate every other
            # reason: teardown is more important than any receipt.
            if fetched >= _SALVAGE_MAX_FILES:
                record.update({"status": "salvage_failed", "error_code": "salvage_file_count_cap"})
                results.append(record)
                continue
            if total_bytes >= _SALVAGE_MAX_TOTAL_BYTES:
                record.update({"status": "salvage_failed", "error_code": "salvage_total_size_cap"})
                results.append(record)
                continue
            budget = remaining_budget()
            if budget <= 1.0:
                record.update({"status": "salvage_failed", "error_code": "salvage_deadline_reserve"})
                results.append(record)
                continue
            if not identity_usable:
                record.update({"status": "salvage_failed",
                               "error_code": "salvage_identity_handle_unusable"})
                results.append(record)
                continue
            staged = staging_root / name
            try:
                sf._preflight(info["phase_id"])
                command = _salvage_transport_argv(info, identity, known_hosts, name, staged)
                fetched += 1
                transfer = _remote(command, timeout=min(_SALVAGE_FILE_TIMEOUT_SECONDS, budget))
                if transfer.get("status") != "completed":
                    record.update({
                        "status": "salvage_failed",
                        "error_code": _salvage_transport_error_code(transfer),
                        "exit_code": transfer.get("exit_code"),
                    })
                    results.append(record)
                    continue
                raw = _salvage_staged_bytes(staged, _SALVAGE_MAX_FILE_BYTES)
                if total_bytes + len(raw) > _SALVAGE_MAX_TOTAL_BYTES:
                    raise _SalvageRefusal("salvage_total_size_cap")
                payload = _salvage_validated_payload(name, raw, run_identity)
                digest = hashlib.sha256(raw).hexdigest()
                # Only now does a path into the validated destination exist, and
                # it is the descriptor-safe publisher rather than a pathname
                # handed to a transfer process.
                j1m_runner._private_atomic_write(
                    destination / name, raw, trusted_root=j1m_runner.PRIVATE_OUTPUT_ROOT,
                )
                total_bytes += len(raw)
                record.update({
                    "status": "completed",
                    "size_bytes": len(raw),
                    "sha256": digest,
                    "schema": payload["schema"],
                    "exit_code": transfer.get("exit_code"),
                })
            except _SalvageRefusal as exc:
                record.update({"status": "salvage_failed", "error_code": exc.code})
            except Exception:
                # Salvage is best effort by contract; an unexpected local
                # failure must still leave exact teardown reachable.
                record.update({"status": "salvage_failed", "error_code": "salvage_failed"})
            results.append(record)

    completed = [item for item in results if item.get("status") == "completed"]
    receipt = {
        "schema": _SALVAGE_RECEIPT_SCHEMA,
        "status": "salvaged" if completed and len(completed) == len(results) else "salvage_failed",
        "transport": "scp-argv-bounded-source-allowlist-v1",
        "remote_directory": _SALVAGE_REMOTE_DIRECTORY,
        "host_key_proof": str((host_key or {}).get("proof", "unrecorded"))[:120],
        "host_key_reproof": ("acquisition-digest" if isinstance((host_key or {}).get("known_hosts_sha256"), str)
                             and (host_key or {}).get("known_hosts_sha256") else "key-count-fallback"),
        "known_hosts_sha256": known_hosts_sha256,
        "caps": {
            "per_file_bytes": _SALVAGE_MAX_FILE_BYTES,
            "total_bytes": _SALVAGE_MAX_TOTAL_BYTES,
            "min_free_bytes": _SALVAGE_MIN_FREE_BYTES,
            "max_files": _SALVAGE_MAX_FILES,
            "wall_clock_seconds": _SALVAGE_WALL_CLOCK_SECONDS,
            "per_file_timeout_seconds": _SALVAGE_FILE_TIMEOUT_SECONDS,
        },
        "allowlist": sorted(_SALVAGE_RECEIPT_ALLOWLIST),
        "requested": len(results),
        "completed": len(completed),
        "failed": len(results) - len(completed),
        "total_bytes": total_bytes,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "files": results,
    }
    try:
        _write_salvage_receipt(destination, receipt)
    except Exception:
        # Evidence is important, but it is never allowed to strand an instance.
        pass
    return results


def execute(env_file: Path, *, config_path: Path, phase_id: str, run_id: str, artifact_destination: Path, mode: str = "prove", model_artifact: Path | None = None, model_manifest: Path | None = None, evaluate_comparators: str = "") -> dict[str, Any]:
    # Parse the comparator vocabulary first: an unapproved request is refused
    # before the legacy deletion preflight, config/env loading, candidate
    # access, key generation or any provider POST. The engine/clock refusal
    # below is later -- after ``preflight_legacy_deletion_evidence`` and
    # ``load_config`` -- but both of those are local and make no provider
    # call, so no refused request can reach a paid resource either way.
    comparator_selection = _comparator_selection(evaluate_comparators)
    if comparator_selection and mode != "eval":
        raise ValueError("comparator evaluation is only available in eval mode")
    comparator_phase: dict[str, Any] = {}
    if mode == "canary":
        # The canary is intentionally plan-only in this source slice.  Its
        # two read-only probes may become executable only after the existing
        # Sol/review/ledger lifecycle gates are explicitly extended.
        raise sf.ShadeformError("no-model remote canary execution remains gated; use the pure plan")
    # Phase-only deletion artifacts predate nonce/owner-bound evidence and
    # cannot safely authorize a new paid run. Check them before config/env
    # loading, candidate access, key generation, reservation, or provider POSTs.
    sf.preflight_legacy_deletion_evidence(phase_id)
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
        comparator_phase = _comparator_phase(config, comparator_selection)
        if comparator_phase["status"] == "refused":
            # Typed, pre-spend refusal. The reason is retained in the plan the
            # operator already printed; nothing has been created at this point.
            raise ValueError("comparator evaluation is refused before any provider call")
        # Eval is explicitly CUDA-only on the approved A100. A CPU binary or
        # missing CUDA placement receipt is rejected by the remote verifier.
    else:
        eval_artifact = None
    # Prove the run can publish what it salvages before anything is billable.
    # This used to fail inside the teardown ``finally``, where the whole
    # salvage call was swallowed, so a paid run ended with an empty artifact
    # directory and no stated reason.  It deliberately sits after the
    # legacy-evidence and mode gates -- those are safety refusals and must stay
    # first -- and before key generation, key upload, or any create POST.
    artifact_destination = prepare_artifact_destination(artifact_destination)
    env = sf.load_env(env_file)
    api_key = sf.require_env(env, "SHADEFORM_API_KEY")
    budget_cap_usd = sf.configured_budget_cap_usd(env)
    runtime = float(config["modes"][mode]["runtime_hours"])
    # Validate the effective provider backstop before even reading the live
    # candidate catalogue. A too-short ceiling must not reach key generation,
    # key upload, or instance creation.
    auto_delete = sf._auto_delete(env, runtime)
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
        # The ephemeral key lives in an owner-private per-run directory under
        # the protected secrets root, not in a system temporary directory: that
        # is the only location ``validate_persisted_argv`` accepts as the ``-i``
        # operand of the ssh/scp argv every remote command is built from.  The
        # run id plus this run's fresh ownership nonce name the directory, so a
        # key is never shared between runs, and the whole directory is securely
        # removed in the ``finally`` below on every exit path.
        key_directory = sf.ephemeral_key_directory(f"{run_id}-{nonce}")
        try:
            identity, public_key = sf.create_ephemeral_ssh_key(env, key_directory)
            # Refuse here, before the first billable provider call, rather than
            # after an instance is running and unreachable.
            sf.assert_persisted_argv_handle(identity)
        except BaseException:
            # Key generation is outside the protected region, so clean up its
            # partial output here rather than leaving material on disk.
            sf.destroy_ephemeral_key_directory(key_directory)
            raise
        key_id: str | None = None
        instance_id: str | None = None
        ambiguous_create = False
        key_ambiguity_unresolved = False
        attempt_reserved = False
        settle_attempt_after_cleanup = False
        attempt_settled_during_cleanup = False
        attempt_cleanup_receipt_ok = True
        recorded = False
        record: sf.OwnedResource | None = None
        known_hosts = temp_root / "known_hosts"
        lifecycle: dict[str, Any] = {"phase_id": phase_id, "status": "starting", "mode": mode}
        if eval_artifact is not None:
            lifecycle["artifact"] = eval_artifact
        if comparator_selection:
            lifecycle["comparator_phase"] = comparator_phase
        def cancel(_signum: int, _frame: Any) -> None:
            raise OperatorCancelled("operator cancellation signal")
        previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        signal.signal(signal.SIGINT, cancel)
        signal.signal(signal.SIGTERM, cancel)
        watchdog: subprocess.Popen[bytes] | None = None
        attempt_id: str | None = None
        cleanup_failure: BaseException | None = None
        key_fingerprint: str | None = None
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

        comparator_cleanup_context: dict[str, Any] | None = None
        comparator_cleanup_done = False

        def run_comparator_cleanup() -> None:
            """Run the deferred intermediate-deletion tail exactly once.

            ``--retain-comparators`` moves ``rm -f``, the post-cleanup receipt
            and the manifest out of the runner's own plan, so the orchestrator
            owes them.  The comparator phase's own ``finally`` sits *below* the
            eval-stage loop's ``raise``, so a failure in any later stage -- or a
            failed upload -- skipped all three and left
            ``comparator_cleanup_error`` unset, which is evidence the default
            path would have produced inline.  The teardown ``finally`` calls
            this too, so every path after the ``--run`` stage reaches it.

            Bounded and non-raising by construction: each stage is budgeted
            through ``_eval_timeout``, which keeps the deletion reserve back, and
            nothing here may delay or prevent exact instance teardown.
            """

            nonlocal comparator_cleanup_done
            if comparator_cleanup_done or comparator_cleanup_context is None:
                return
            comparator_cleanup_done = True
            cleanup_info = comparator_cleanup_context["info"]
            cleanup_root = comparator_cleanup_context["remote_root"]
            try:
                for command in _comparator_cleanup_commands(config, cleanup_root):
                    lifecycle["stage"] = f"comparator-cleanup:{command[0]}"
                    try:
                        timeout = _eval_timeout(execution_deadline, _comparator_stage_timeout(config, command))
                    except sf.ShadeformError:
                        lifecycle["comparator_cleanup_error"] = "deferred intermediate deletion had no cleanup-safe budget"
                        return
                    _progress(progress_path, "comparator-cleanup-starting", phase_id=phase_id, operation_stage=lifecycle["stage"])
                    stage = _remote(sf.ssh_base(cleanup_info, identity, known_hosts) + command, timeout=timeout)
                    lifecycle.setdefault("eval_stages", []).append(stage)
                    _progress(progress_path, "comparator-cleanup-result", phase_id=phase_id, operation_stage=lifecycle["stage"], status=stage["status"], exit_code=stage.get("exit_code"))
                    if stage["status"] != "completed":
                        lifecycle["comparator_cleanup_error"] = "deferred intermediate deletion did not complete"
                        return
            except Exception:
                lifecycle["comparator_cleanup_error"] = "deferred intermediate deletion did not complete"

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
            key_fingerprint = sf.ssh_public_key_fingerprint(public_key)
            lifecycle["ssh_public_key_fingerprint"] = key_fingerprint
            attempt_id = sf.reserve_create_attempt(
                phase_id, nonce, candidate,
                backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]),
                public_key_sha256=hashlib.sha256(public_key.encode("utf-8")).hexdigest(),
                expected_budget_cap_usd=budget_cap_usd,
                public_key_fingerprint=key_fingerprint,
            )
            attempt_reserved = True
            # Prearm exact recovery before the first SSH-key provider POST.
            # The watcher starts from the durable nonce/name/fingerprint and
            # can later reconcile a possibly-created instance.
            launcher_pid = os.getpid()
            launcher_start_marker = sf.process_start_marker(launcher_pid)
            deadline_started = time.monotonic()
            provider_deadline = deadline_started + float(config["modes"][mode]["provider_backstop_hours"]) * 3600
            run_deadline = deadline_started + runtime * 3600
            watchdog_deadline = deadline_started + float(config["modes"][mode]["external_watchdog_seconds"])
            execution_deadline = min(provider_deadline, run_deadline, watchdog_deadline)
            watchdog_seconds = execution_deadline - time.monotonic() - _DELETION_RESERVE_SECONDS
            if watchdog_seconds < 1.0:
                raise TimeoutError("insufficient deadline for pre-create recovery watchdog")
            expected_instance_name = sf.owned_instance_name(run_id, nonce)
            watchdog_command = [
                sf._verified_python_executable(), str(ROOT / "scripts" / "shadeform_watchdog.py"),
                "--phase-id", phase_id, "--launcher-pid", str(launcher_pid),
                "--max-seconds", str(watchdog_seconds),
                "--deadline-epoch", str(time.time() + execution_deadline - time.monotonic()),
                "--provider-delete-deadline-epoch", str(datetime.fromisoformat(str(auto_delete["date_threshold"])).timestamp()),
                "--env-file", str(env_file), "--ownership-nonce", nonce,
                "--ssh-key-name", f"j1m-{nonce}", "--ssh-key-fingerprint", key_fingerprint,
                "--allow-unrecorded-exact", "--key-only-recovery",
                "--instance-name", expected_instance_name, "--precreate-recovery",
                "--cloud", candidate.cloud, "--region", candidate.region,
                "--instance-type", candidate.instance_type, "--hourly-usd", str(candidate.hourly_usd),
                "--gpu", candidate.gpu, "--gpu-count", "1", "--vram-gb", str(candidate.vram_gb),
                "--os-image", candidate.os_image,
            ]
            if launcher_start_marker is not None:
                watchdog_command.extend(["--launcher-start-marker", launcher_start_marker])
            watchdog = subprocess.Popen(
                watchdog_command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=sf._secure_subprocess_env(),
            )
            lifecycle["watchdog_pid"] = watchdog.pid
            try:
                sf.preflight_legacy_deletion_evidence(phase_id)
                key_id = sf.add_ssh_key(api_key, phase_id, f"j1m-{nonce}", public_key)
            except sf.AmbiguousProviderOutcome as exc:
                # A timed-out or malformed key-create response may have
                # succeeded remotely. Reconcile only the nonce-bound name and
                # exact public-key fingerprint; unresolved ambiguity blocks
                # instance creation and is retained as a finite incident.
                lifecycle["ssh_key_create_ambiguous"] = True
                key_ambiguity_unresolved = True
                try:
                    key_id = sf.reconcile_ssh_key(
                        api_key, phase_id, expected_name=f"j1m-{nonce}",
                        expected_public_key=public_key,
                    )
                    lifecycle["ssh_key_reconciliation"] = {"status": "exact_match"}
                    key_ambiguity_unresolved = False
                except Exception as reconcile_exc:
                    lifecycle["ssh_key_reconciliation"] = {
                        "status": "unresolved",
                        "retry_required": True,
                        "error_type": "ssh_key_reconciliation_failed",
                    }
                    try:
                        sf.append_incident({
                            "phase_id": phase_id,
                            "incident": "ssh-key-create-ambiguous-unresolved",
                            "nonce": nonce,
                            "ssh_key_name": f"j1m-{nonce}",
                            "ssh_public_key_sha256": hashlib.sha256(public_key.encode("utf-8")).hexdigest(),
                            "error_type": "ssh_key_reconciliation_failed",
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
            sf.reserve_create_attempt(
                phase_id, nonce, candidate,
                backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]),
                public_key_sha256=hashlib.sha256(public_key.encode("utf-8")).hexdigest(),
                expected_budget_cap_usd=budget_cap_usd,
                public_key_fingerprint=key_fingerprint,
                ssh_key_id=key_id,
            )
            create_intent_started = sf.utc_now().isoformat()
            sf.append_instance_create_intent(
                phase_id, nonce, instance_name=expected_instance_name, ssh_key_id=key_id,
                hourly_usd=candidate.hourly_usd,
                backstop_hours=float(config["modes"][mode]["provider_backstop_hours"]),
                provider_delete_deadline_utc=str(auto_delete["date_threshold"]),
                started_at_utc=create_intent_started,
            )
            try:
                sf.preflight_legacy_deletion_evidence(phase_id)
                instance_id = sf.create_instance(api_key, env, phase_id=phase_id, run_id=run_id, candidate=candidate, ssh_key_id=key_id, nonce=nonce, max_runtime_hours=runtime, auto_delete_contract=auto_delete)
            except Exception as exc:
                incident = {
                    "phase_id": phase_id,
                    "incident": "create-response-ambiguous" if sf.is_ambiguous_transport(exc) else "create-definitive-failure",
                    "nonce": nonce,
                    "ssh_key_id": key_id,
                    "ssh_key_name": f"j1m-{nonce}",
                    "ssh_public_key_sha256": hashlib.sha256(public_key.encode("utf-8")).hexdigest(),
                    "error_type": "remote_stage_failed",
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
            activation_seconds = int(config["modes"][mode].get("activation_timeout_seconds", 1800))
            record = sf.OwnedResource(phase_id=phase_id, run_id=run_id, instance_id=instance_id, instance_name=expected_instance_name, ownership_nonce=nonce, ssh_key_id=key_id, ssh_key_name=f"j1m-{nonce}", gpu=candidate.gpu, cloud=candidate.cloud, region=candidate.region, hourly_usd=candidate.hourly_usd, created_at_utc=create_intent_started, provider_delete_deadline_utc=str(auto_delete["date_threshold"]), active_deadline_utc=(sf.utc_now() + sf.timedelta(seconds=activation_seconds)).isoformat(), run_deadline_utc=(sf.utc_now() + sf.timedelta(hours=runtime)).isoformat(), instance_type=candidate.instance_type, gpu_count=1, vram_gb=candidate.vram_gb, os_image=candidate.os_image, ssh_public_key=public_key, launcher_pid=launcher_pid, launcher_start_marker=launcher_start_marker, ssh_public_key_fingerprint=key_fingerprint)
            # Bind the exact instance cost before the owned record. The
            # pre-armed watchdog plus durable create intent covers a crash in
            # either write, while exact-owner recovery reuses this immutable
            # reservation and settles it before key cleanup.
            sf.append_cost_event({"instance_id": instance_id, "phase_id": phase_id, "ownership_nonce": nonce, "create_started_at_utc": create_intent_started, "status": "pending", "estimated_cost_usd": round(candidate.hourly_usd * float(config["modes"][mode]["provider_backstop_hours"]), 6)})
            # Ownership record is written before any poll/upload. If this
            # fails, the fallback below still deletes the exact returned ID.
            sf.write_owned_resource(record)
            recorded = True
            # The instance reservation is now superseded by its exact
            # ownership/billing row. Keep the pre-create reservation history
            # but settle it to zero only after both durable writes and the
            # watchdog are in place.
            sf.append_cost_event({"instance_id": attempt_id, "phase_id": phase_id, "ownership_nonce": nonce, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
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
            # Bind every receipt this run will produce to this run, before the
            # first receipt-producing command runs.  Each remote writer reads
            # this exact source-fixed path and stamps the fields into its
            # receipt; salvage then refuses any receipt whose binding is absent
            # or belongs to some earlier run under the same artifact directory.
            if str(j1m_runner.RUN_IDENTITY_PATH.parent) != remote_root:
                raise sf.ShadeformError("run identity path does not match the remote workspace")
            run_identity_local = temp_root / "run-identity.json"
            run_identity_local.write_text(
                json.dumps({
                    "schema": j1m_runner.RUN_IDENTITY_SCHEMA,
                    "run_id": run_id,
                    "instance_id": instance_id,
                }, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_identity_local.chmod(0o600)
            lifecycle["run_identity_upload"] = _remote(
                sf.scp_base(info, identity, known_hosts)
                + [str(run_identity_local), f"{ssh_user}@{info['ip']}:{j1m_runner.RUN_IDENTITY_PATH}"],
                timeout=_eval_timeout(execution_deadline, 120.0),
            )
            if lifecycle["run_identity_upload"]["status"] != "completed":
                raise sf.ShadeformError("run identity binding could not be uploaded")
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
                if comparator_selection:
                    # ``--retain-comparators`` has already moved `rm -f`, the
                    # post-cleanup receipt and the manifest out of the runner's
                    # own plan, so from here on this run owes that tail. Arm it
                    # before the first eval stage can fail: the phase's own
                    # `finally` is above the stage loop's `raise`, so a failure
                    # in any later stage used to skip all three silently and
                    # never set ``comparator_cleanup_error``.
                    comparator_cleanup_context = {"info": info, "remote_root": remote_root}
                eval_commands = _eval_remote_commands(config, remote_root, comparator_selection)
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
                eval_uploads = _eval_uploads(config, remote_root, model_artifact, model_manifest, comparator_selection)
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
                if comparator_selection:
                    # The Q4 job is already fixed above: nothing below may
                    # change it, and a comparator failure is recorded as a
                    # typed skip rather than failing the Q4 result.
                    comparator_phase = _comparator_phase(
                        config, comparator_selection, execution_deadline=execution_deadline)
                    lifecycle["comparator_phase"] = comparator_phase
                    try:
                        if comparator_phase["status"] == "approved":
                            for command in _comparator_remote_commands(config, remote_root, comparator_selection):
                                lifecycle["stage"] = _eval_stage_label(command)
                                try:
                                    stage_timeout = _eval_timeout(execution_deadline, _comparator_stage_timeout(config, command))
                                except sf.ShadeformError:
                                    # The clock ran out mid-phase. That is a
                                    # typed comparator skip, never a failure
                                    # of the Q4 run that already completed.
                                    comparator_phase["status"] = "refused"
                                    comparator_phase["reason"] = "comparator_clock_insufficient"
                                    break
                                _progress(progress_path, "comparator-stage-starting", phase_id=phase_id, operation_stage=lifecycle["stage"])
                                stage = _remote(
                                    sf.ssh_base(info, identity, known_hosts) + command,
                                    timeout=stage_timeout)
                                lifecycle.setdefault("eval_stages", []).append(stage)
                                _progress(progress_path, "comparator-stage-result", phase_id=phase_id, operation_stage=lifecycle["stage"], status=stage["status"], exit_code=stage.get("exit_code"))
                                if stage["status"] != "completed":
                                    comparator_phase["status"] = "failed"
                                    comparator_phase["reason"] = "comparator_stage_failed"
                                    break
                    finally:
                        # Intermediate deletion was moved, not dropped. It
                        # runs on every path out of the comparator phase,
                        # including a refusal and a stage failure, and an
                        # unproven deletion is a fail-closed condition.
                        run_comparator_cleanup()
            if lifecycle["job"]["status"] != "completed":
                lifecycle["status"] = lifecycle["job"]["status"]
                raise sf.ShadeformError("J1M remote job did not complete")
            lifecycle["status"] = "completed"
            _progress(progress_path, "completed", phase_id=phase_id, mode=mode)
        except (KeyboardInterrupt, OperatorCancelled):
            lifecycle["status"] = "cancelled_by_operator"
            if instance_id is None and not ambiguous_create and not key_ambiguity_unresolved:
                settle_attempt_after_cleanup = True
            raise
        except Exception as exc:
            # Key upload/ownership validation and other definitive local
            # failures occur outside the create-specific handler. They still
            # reconcile the pre-create reservation after finally cleanup when
            # no instance POST could have succeeded.
            if instance_id is None and not ambiguous_create and not key_ambiguity_unresolved:
                settle_attempt_after_cleanup = True
            lifecycle["status"] = lifecycle.get("status") if lifecycle.get("status") not in {None, "starting", "active"} else "failed"
            lifecycle["failure"] = {"error_type": "lifecycle_failed"}
            lifecycle["failed_stage"] = lifecycle.get("stage", "unknown")
            _progress(progress_path, "failed", phase_id=phase_id, failed_stage=lifecycle["failed_stage"], error_type="lifecycle_failed")
            raise
        finally:
            try:
                _persist_lifecycle(phase_id, lifecycle)
            except Exception:
                pass
            # Every path after the `--run` stage owes the deferred
            # intermediate-deletion tail. This is a no-op when the phase already
            # ran it, and when the run never armed it.
            run_comparator_cleanup()
            _progress(progress_path, "teardown-salvage-starting", phase_id=phase_id, failed_stage=lifecycle.get("failed_stage"))
            fetch_allowlist = (
                config["artifacts"]["local_fetch_allowlist"] if mode == "build"
                else _eval_fetch_allowlist(config, comparator_selection) if mode == "eval"
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
                    # Bind every fetched receipt to this run's own identity, so
                    # a receipt describing some other artifact or instance is
                    # refused rather than published.
                    run_identity={
                        "run_id": run_id,
                        "instance_id": instance_id,
                        "artifact": eval_artifact,
                        # The fixture every comparator arm must have scored.
                        "fixture_sha256": (_tool_eval_contract()["fixture_identity"]["sha256"]
                                           if mode == "eval" else None),
                    },
                    # The host key pinned before the first remote command; a
                    # host swapped before teardown fails the transfer.
                    host_key=lifecycle.get("host_key"),
                ) if lifecycle.get("instance_info") else []
            except Exception as exc:
                # Even an unexpected salvage/setup failure must leave the
                # exact deletion and key cleanup paths reachable.
                lifecycle["salvage"] = [{"status": "salvage_failed", "error_type": "salvage_failed"}]
            if mode == "prove" and lifecycle.get("job", {}).get("status") == "completed" and not any(item.get("name") == "proving-receipt.json" and item.get("status") == "completed" for item in lifecycle["salvage"]):
                lifecycle["receipt_error"] = "proving receipt was not salvaged before teardown"
                # Same fail-closed rule as eval: a completed remote job whose
                # required receipt never arrived is not a successful run.
                lifecycle["status"] = "failed"
            if mode == "build" and lifecycle.get("job", {}).get("status") == "completed":
                # A paid multi-hour conversion that returns no evidence is a
                # failure, not a success with an empty artifact directory.
                # Build mode now gets the same fail-closed semantics eval and
                # prove already had: every receipt the source allowlist can
                # carry must arrive, and the deliberately unsalvageable weights
                # and non-schema-bound outputs are not counted against it.
                salvaged_names = {
                    item.get("name") for item in lifecycle["salvage"]
                    if item.get("status") == "completed"
                }
                expected_names = {
                    name for name in fetch_allowlist
                    if isinstance(name, str) and name in _SALVAGE_RECEIPT_ALLOWLIST
                }
                if not expected_names:
                    lifecycle["receipt_error"] = "build fetch allowlist names no salvageable receipt"
                elif not expected_names <= salvaged_names:
                    lifecycle["receipt_error"] = "build receipts were not salvaged before teardown"
                lifecycle["build_receipts"] = {
                    "expected": sorted(expected_names),
                    "salvaged": sorted(name for name in salvaged_names if isinstance(name, str)),
                    "refused_non_receipt": sorted(
                        item.get("name") for item in lifecycle["salvage"]
                        if item.get("error_code") == "salvage_refused_non_receipt" and isinstance(item.get("name"), str)
                    ),
                }
                if lifecycle.get("receipt_error"):
                    lifecycle["status"] = "failed"
            if mode == "eval":
                artifact_saved = next((item for item in lifecycle["salvage"] if item.get("name") == "eval-artifact-receipt.json" and item.get("status") == "completed"), None)
                if artifact_saved is None:
                    lifecycle["receipt_error"] = "eval artifact receipt was not salvaged"
                else:
                    try:
                        lifecycle["eval_artifact_receipt"] = _verify_eval_artifact_receipt(artifact_destination / "eval-artifact-receipt.json", eval_artifact)
                    except Exception:
                        lifecycle["receipt_error"] = "eval artifact receipt invalid"
                preflight_saved = next((item for item in lifecycle["salvage"] if item.get("name") == "startup-preflight-receipt.json" and item.get("status") == "completed"), None)
                if lifecycle.get("remote_model_eval_attempted"):
                    if preflight_saved is None:
                        lifecycle["receipt_error"] = "startup preflight receipt was not salvaged"
                    else:
                        try:
                            lifecycle["preflight_receipt"] = _verify_startup_preflight_receipt(artifact_destination / "startup-preflight-receipt.json", eval_artifact)
                        except Exception as exc:
                            lifecycle["receipt_error"] = "receipt_verification_failed"
                    if lifecycle.get("preflight_receipt", {}).get("status") != "verified":
                        lifecycle["receipt_error"] = lifecycle.get("receipt_error", "startup preflight did not verify")
                saved_receipt = next((item for item in lifecycle["salvage"] if item.get("name") == "eval-receipt.json" and item.get("status") == "completed"), None)
                if saved_receipt is None:
                    lifecycle["receipt_error"] = "eval receipt was not salvaged before teardown"
                else:
                    try:
                        lifecycle["eval_receipt"] = _verify_eval_receipt(artifact_destination / "eval-receipt.json", eval_artifact)
                    except Exception as exc:
                        lifecycle["receipt_error"] = "receipt_verification_failed"
                if lifecycle.get("comparator_cleanup_error"):
                    # Deferred intermediate deletion that cannot be proven is a
                    # fail-closed condition for a run that opted into it.
                    lifecycle["receipt_error"] = lifecycle.get("receipt_error") or lifecycle["comparator_cleanup_error"]
                if (lifecycle.get("receipt_error") or lifecycle.get("eval_artifact_receipt", {}).get("status") != "verified" or
                        lifecycle.get("eval_receipt", {}).get("status") != "verified"):
                    lifecycle["status"] = "failed"
                if comparator_selection:
                    try:
                        comparison = _write_comparison_receipt(
                            artifact_destination, comparator_selection,
                            phase_reason=comparator_phase.get("reason"),
                            fixture_sha256=_tool_eval_contract()["fixture_identity"]["sha256"],
                            product_engine_reference=_product_engine_reference(lifecycle),
                        )
                        lifecycle["comparison"] = {
                            "status": comparison["status"],
                            "comparisons": len(comparison["comparisons"]),
                            "skipped": len(comparison["skipped"]),
                        }
                    except (compare_model_quality.ComparisonError, ValueError, OSError) as exc:
                        # Keep the typed code compare_model_quality produced;
                        # it is a closed vocabulary and carries no secret.
                        lifecycle["comparison"] = {
                            "status": "refused", "error_type": "comparison_failed",
                            "error_code": str(exc)[:64],
                        }
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
                            deadline=execution_deadline
                            if "execution_deadline" in locals() else None,
                        )
                    except Exception as exc:
                        cleanup_failure = RuntimeError("exact instance teardown was not confirmed")
                        lifecycle["deletion"] = {
                            "status": "delete-failed",
                            "retry_required": True,
                            "error_type": "artifact_salvage_failed",
                        }
                        lifecycle["status"] = "failed"
                        try:
                            sf.append_incident({
                                "phase_id": phase_id,
                                "incident": "post-instance-teardown-unconfirmed",
                                "instance_id": instance_id,
                                "ssh_key_id": key_id,
                                "nonce": nonce,
                                "error_type": "artifact_salvage_failed",
                            })
                        except Exception:
                            pass
                else:
                    fallback_deadline = execution_deadline if "execution_deadline" in locals() else None
                    try:
                        if record is None:
                            raise RuntimeError("unrecorded instance lacks its full owner record")
                        lifecycle["deletion"] = teardown_recovered_exact(
                            record, env_file=env_file, deadline=fallback_deadline,
                        )
                        if not deletion_confirmed():
                            raise RuntimeError("recovered teardown did not confirm exact deletion")
                    except Exception as exc:
                        lifecycle["deletion"] = {
                            "status": "delete-failed", "retry_required": True,
                            "error_type": "artifact_salvage_failed",
                        }
                        cleanup_failure = RuntimeError("exact recovered teardown was not confirmed")
                        lifecycle["status"] = "failed"
            elif key_id is not None and not ambiguous_create:
                # Key creation succeeded but instance creation did not.
                if attempt_reserved and settle_attempt_after_cleanup:
                    try:
                        sf.append_cost_event({"instance_id": attempt_id, "phase_id": phase_id, "ownership_nonce": nonce, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
                        attempt_settled_during_cleanup = True
                        try:
                            sf.append_incident({
                                "phase_id": phase_id,
                                "incident": "attempt-reservation-reconciled",
                                "nonce": nonce,
                                "retry_required": False,
                            })
                        except Exception as receipt_exc:
                            attempt_cleanup_receipt_ok = False
                            cleanup_failure = RuntimeError("attempt settlement receipt was not confirmed")
                            lifecycle["attempt_reservation_receipt_error_type"] = "reservation_receipt_failed"
                    except Exception as exc:
                        lifecycle["attempt_reservation_error_type"] = "reservation_settlement_failed"
                        lifecycle["attempt_reservation_retry_required"] = True
                        cleanup_failure = RuntimeError("attempt reservation settlement was not confirmed")
                        try:
                            sf.append_incident({"phase_id": phase_id, "incident": "attempt-reservation-settlement-failed", "nonce": nonce, "error_type": "reservation_settlement_failed"})
                        except Exception:
                            pass
                if not settle_attempt_after_cleanup or (attempt_settled_during_cleanup and attempt_cleanup_receipt_ok):
                    try:
                        lifecycle["key_cleanup"] = sf.delete_owned_ssh_key_exact(
                            api_key,
                            phase_id,
                            key_id,
                            ownership_nonce=nonce,
                            expected_name=f"j1m-{nonce}",
                            expected_public_key=public_key,
                            expected_fingerprint=key_fingerprint,
                        )
                        if lifecycle["key_cleanup"].get("status") != "confirmed":
                            raise RuntimeError("exact SSH key deletion lacks durable confirmation")
                    except Exception as exc:
                        lifecycle["key_cleanup"] = {"status": "failed", "error_type": "key_cleanup_failed"}
                        try:
                            sf.append_incident({"phase_id": phase_id, "incident": "create-key-delete-failed", "ssh_key_id": key_id, "nonce": nonce, "error_type": "key_cleanup_failed"})
                        except Exception:
                            pass
            # Keep the watchdog alive through salvage and exact instance
            # deletion. It is stopped only after the provider explicitly
            # confirms deletion; an unconfirmed/raised cleanup leaves it
            # running for its own exact retry path.
            if deletion_confirmed():
                stop_watchdog()
            if attempt_reserved and settle_attempt_after_cleanup and not attempt_settled_during_cleanup:
                try:
                    sf.append_cost_event({"instance_id": attempt_id, "phase_id": phase_id, "ownership_nonce": nonce, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
                except Exception as exc:
                    try:
                        sf.append_incident({"phase_id": phase_id, "incident": "attempt-reservation-settlement-failed", "nonce": nonce, "error_type": "reservation_settlement_failed"})
                    except Exception:
                        pass
            # The ephemeral key has now outlived its last use: salvage is done
            # and exact deletion has been attempted.  Remove the whole per-run
            # directory, overwriting the private half first, on the success and
            # every failure path, and record the outcome as metadata in the run
            # receipt.  A cleanup failure is recorded, never raised: it must not
            # be able to strand a provider resource.
            try:
                lifecycle["ephemeral_key_cleanup"] = sf.destroy_ephemeral_key_directory(key_directory)
            except Exception:
                lifecycle["ephemeral_key_cleanup"] = {"status": "incomplete", "error_type": "key_cleanup_failed"}
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
    parser.add_argument("--env-file", type=Path, default=sf.MUTATION_ENV_FILE)
    parser.add_argument("--config", type=Path, default=j1m_runner.DEFAULT_CONFIG)
    parser.add_argument("--phase-id", default="j1m-proving-run")
    parser.add_argument("--run-id", default="J1M")
    parser.add_argument("--artifact-destination", type=Path, default=ROOT / "artifacts" / "qwen35-9b")
    parser.add_argument("--mode", choices=("prove", "build", "eval", "canary"), default="prove")
    parser.add_argument("--model-artifact", type=Path, help="refused for eval; the Q4 artifact is always built remotely")
    parser.add_argument("--model-manifest", type=Path, help="approved manifest; defaults to the checked-in Q4 acceptance manifest")
    parser.add_argument("--evaluate-comparators", default="", choices=sorted(_COMPARATOR_SELECTIONS), help="default OFF; 'q4-oracle' scores the Q4 artifact on the pinned upstream server, 'q8' or 'q8,bf16' also evaluate the rebuilt higher-precision comparators")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    comparator_selection = _comparator_selection(args.evaluate_comparators)
    if comparator_selection and args.mode != "eval":
        raise ValueError("comparator evaluation is only available in eval mode")
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
        if comparator_selection:
            # Additive only: with the flag absent the plan object above is
            # byte-for-byte what it is without this slice.
            plan["comparator_phase"] = _comparator_phase(config, comparator_selection)
            plan["comparator_fetch_allowlist"] = _eval_fetch_allowlist(config, comparator_selection)
            plan["comparator_commands"] = _comparator_remote_commands(config, "/scratch/j1m", comparator_selection)
            plan["comparator_cleanup_commands"] = _comparator_cleanup_commands(config, "/scratch/j1m")
    elif args.mode == "canary":
        plan["commands"] = _canary_remote_commands(config, "/scratch/j1m-canary")
        plan["artifact"] = "no model; bounded toolchain and CUDA prerequisite probes only"
        plan["no_model"] = True
        plan["probe_only"] = True
        plan["salvage_required"] = True
        plan["teardown_required"] = True
        plan["execution_status"] = "disabled_pending_existing_remote_gates_and_explicit_approval"
    if not args.execute:
        plan["orchestrator"] = "dry-run; no provider API mutation"
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if os.environ.get("SOL_J1M_REVIEWED") != "1":
        raise SystemExit("refusing mutation: Sol must set SOL_J1M_REVIEWED=1 after reviewing the plan")
    print(json.dumps(execute(args.env_file, config_path=args.config, phase_id=args.phase_id, run_id=args.run_id, artifact_destination=args.artifact_destination, mode=args.mode, model_artifact=args.model_artifact, model_manifest=args.model_manifest, evaluate_comparators=args.evaluate_comparators), sort_keys=True))
    return 0


def _safe_cli(argv: list[str] | None = None) -> int:
    """Expose only a finite refusal code when invoked as a subprocess."""

    try:
        return main(argv)
    except SystemExit:
        raise
    except Exception:
        print(json.dumps({"status": "refused", "error_code": "input_rejected"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(_safe_cli())
