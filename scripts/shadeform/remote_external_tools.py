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
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
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
MAX_LIFECYCLE_BYTES = 64 * 1024
MAX_REMOTE_ROOT = 96
DEFAULT_RUNTIME_HOURS = 0.25
DEFAULT_FUZZ_CASES = 64
DEFAULT_SOAK_ITERATIONS = 25
ACTIVATION_TIMEOUT_SECONDS = 300
# Exact teardown budget: fresh instance proof (90s), instance delete request
# (90s), deletion poll (240s), fresh SSH-key proof (90s), key revoke (90s),
# and a 60s persistence/jitter margin. All schedules derive from this value.
INSTANCE_VERIFY_TIMEOUT_SECONDS = 90.0
DELETE_REQUEST_TIMEOUT_SECONDS = 90.0
DELETE_POLL_TIMEOUT_SECONDS = 240.0
SSH_KEY_VERIFY_TIMEOUT_SECONDS = 90.0
SSH_KEY_DELETE_TIMEOUT_SECONDS = 90.0
TEARDOWN_MARGIN_SECONDS = 60.0
DELETION_RESERVE_SECONDS = (
    INSTANCE_VERIFY_TIMEOUT_SECONDS + DELETE_REQUEST_TIMEOUT_SECONDS
    + DELETE_POLL_TIMEOUT_SECONDS + SSH_KEY_VERIFY_TIMEOUT_SECONDS
    + SSH_KEY_DELETE_TIMEOUT_SECONDS + TEARDOWN_MARGIN_SECONDS
)
MIN_REMOTE_WORK_SECONDS = 120.0
SALVAGE_TIMEOUT_SECONDS = 60.0
FIXED_QA_CASE_IDS = (
    "graph.strict-arguments", "graph.bounded-hostile-projection", "graph.malformed-response",
    "graph.write-replay", "graph.draft-toctou", "graph.cancel-and-timeout", "graph.egress-boundary",
    "browser.private-and-malformed-destination", "browser.production-mutation-gate",
    "browser.hostile-resolver-and-bounds", "browser.cdp-response-bound",
    "copilot.legacy-fail-closed", "copilot.acp-session-binding", "copilot.acp-oversize",
    "grants.revoke-generation",
)
FIXED_QA_CASES = len(FIXED_QA_CASE_IDS)
RESULTS_ROOT = ROOT / "experiments" / "results"
FINITE_RECEIPT_ERRORS = frozenset({
    "harness_failure", "provider_failed", "provider_timeout", "provider_cancelled",
    "provider_invalid_response", "provider_permission_insufficient",
    "provider_destination_rejected", "provider_response_too_large",
    "invalid_tool_arguments", "schema_mismatch", "transport_failed",
})
# The lifecycle is statically/mock hardened but has not passed an authorized
# live-provider review. Keep planning and self-tests available while failing
# closed before credentials, candidate access, or provider mutation.
REMOTE_EXECUTION_ENABLED = False

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


class OperatorCancelled(Exception):
    pass


def bounded_id(value: object, *, field: str, pattern: str = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}") -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {field}")
    return value


def bounded_nonnegative(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be an integer from 0 through {maximum}")
    return value


def _safe_failure_code(exc: BaseException) -> str:
    """Map failures to a finite, credential-free lifecycle vocabulary."""

    if isinstance(exc, KeyboardInterrupt):
        return "operator_interrupt"
    if isinstance(exc, SystemExit):
        return "system_exit"
    if isinstance(exc, OperatorCancelled):
        return "operator_cancelled"
    if isinstance(exc, TimeoutError):
        return "deadline_exceeded"
    if isinstance(exc, shadeform.AmbiguousProviderOutcome):
        return "provider_outcome_ambiguous"
    if isinstance(exc, shadeform.ShadeformError):
        return "provider_lifecycle_error"
    if isinstance(exc, RunnerError):
        return "runner_error"
    if isinstance(exc, OSError):
        return "local_io_error"
    return "unexpected_error"


def _remaining_before_cleanup(deadline: float) -> float:
    return deadline - time.monotonic() - DELETION_RESERVE_SECONDS


def _operation_timeout(deadline: float, requested: float) -> float:
    """Bound one fallible operation while preserving exact-delete time."""

    remaining = _remaining_before_cleanup(deadline)
    if remaining < 1.0:
        raise TimeoutError("provider-clock deadline reached with teardown reserve intact")
    return min(requested, remaining)


def _provider_backstop_seconds(value: object) -> float:
    """Convert the exact auto-delete threshold into a conservative duration."""

    if not isinstance(value, dict) or set(value) != {"date_threshold", "spend_threshold"}:
        raise RunnerError("provider backstop contract is invalid")
    threshold = value.get("date_threshold")
    if not isinstance(threshold, str) or len(threshold) > 64:
        raise RunnerError("provider backstop date is invalid")
    try:
        parsed = datetime.fromisoformat(threshold)
    except ValueError as exc:
        raise RunnerError("provider backstop date is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunnerError("provider backstop date is not timezone-aware")
    seconds = (parsed - shadeform.utc_now()).total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        raise RunnerError("provider backstop has already expired")
    return seconds


def _lifecycle_path(phase_id: str) -> Path:
    return shadeform.runtime_ledger_path(phase_id).with_name(
        f"{shadeform.validate_phase_id(phase_id)}.remote-external-tools-lifecycle.json"
    )


def _persist_lifecycle(phase_id: str, lifecycle: dict[str, object]) -> None:
    """Atomically retain a bounded, source-controlled lifecycle projection."""

    payload = json.dumps(lifecycle, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_LIFECYCLE_BYTES:
        raise RunnerError("lifecycle receipt exceeds byte bound")
    path = _lifecycle_path(phase_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _bounded_file(path: Path, limit: int) -> bytes:
    """Read one stable descriptor snapshot and reject path replacement."""

    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit:
            raise RunnerError("remote receipt exceeds byte bound")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.stat()
        if (
            len(data) > limit
            or before.st_size != after.st_size
            or after.st_size != len(data)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RunnerError("remote receipt changed during bounded read")
        return data


def _safe_receipt_destination(value: Path) -> Path:
    destination = value if value.is_absolute() else ROOT / value
    if "\x00" in str(destination) or len(str(destination)) > 1024:
        raise RunnerError("receipt destination is invalid")
    root = RESULTS_ROOT.resolve()
    parent = destination.parent
    # Resolve only existing ancestors; reject symlinked ancestors instead of
    # silently permitting a path race to redirect the salvage tree.
    cursor = parent
    while cursor != cursor.parent:
        if cursor.exists() and cursor.is_symlink():
            raise RunnerError("receipt destination has a symlinked ancestor")
        cursor = cursor.parent
    resolved_parent = parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise RunnerError("receipt destination escapes the results root")
    return resolved_parent / destination.name


def closure_manifest(root: Path = ROOT) -> list[dict[str, object]]:
    """Return the exact source closure with bounded metadata and SHA-256."""

    entries: list[dict[str, object]] = []
    for relative in UPLOAD_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise RunnerError(f"closure member is not a regular non-symlink file: {relative}")
        data = _stable_source_bytes(path, MAX_RECEIPT_BYTES)
        size = len(data)
        digest = hashlib.sha256(data).hexdigest()
        entries.append({"path": relative, "size_bytes": size, "sha256": digest})
    return entries


def _stable_source_bytes(path: Path, limit: int) -> bytes:
    """Read a regular source from one descriptor and verify its identity."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RunnerError("closure member could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        if not stat_is_regular(before.st_mode) or before.st_size > limit:
            raise RunnerError("closure member is not bounded regular data")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise RunnerError("closure member is too large")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or total != after.st_size:
            raise RunnerError("closure member changed during bounded read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def stat_is_regular(mode: int) -> bool:
    import stat
    return stat.S_ISREG(mode)


def freeze_closure(root: Path, destination: Path) -> list[dict[str, object]]:
    """Copy the audited closure into an owned immutable staging directory."""

    entries: list[dict[str, object]] = []
    destination.mkdir(parents=True, exist_ok=True)
    for relative in UPLOAD_FILES:
        source = root / relative
        data = _stable_source_bytes(source, MAX_RECEIPT_BYTES)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o400)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        entries.append({"path": relative, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return entries


def _repository_commit(root: Path = ROOT) -> str:
    try:
        git_marker = (root / ".git").read_text(encoding="utf-8").strip()
        git_dir = Path(git_marker[7:].strip()).resolve() if git_marker.startswith("gitdir:") else root / ".git"
        common = (git_dir / (git_dir / "commondir").read_text(encoding="ascii").strip()).resolve() if (git_dir / "commondir").exists() else git_dir
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: "):
            revision = (common / head[5:]).read_text(encoding="ascii").strip()
        else:
            revision = head
    except (OSError, UnicodeError) as exc:
        raise RunnerError("repository revision could not be bound") from exc
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RunnerError("repository revision is not an exact commit")
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=True, timeout=30,
            env=_minimal_local_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError("repository cleanliness could not be verified") from exc
    if status.stdout:
        raise RunnerError("repository worktree must be clean before remote execution")
    return revision


def _commit_closure_manifest(root: Path, commit: str) -> list[dict[str, object]]:
    """Hash the exact committed blobs, binding uploaded bytes to ``commit``."""

    entries: list[dict[str, object]] = []
    for relative in UPLOAD_FILES:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{commit}:{relative}"],
                capture_output=True, check=True, timeout=30,
                env=_minimal_local_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("committed closure member could not be read") from exc
        data = result.stdout
        if len(data) > MAX_RECEIPT_BYTES:
            raise RunnerError("committed closure member is too large")
        entries.append({"path": relative, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return entries


def build_plan(*, phase_id: str, run_id: str, runtime_hours: float, fuzz_cases: int, soak_iterations: int, env: dict[str, str], git_commit: str | None = None) -> dict[str, object]:
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
    reviewed_commit = _repository_commit()
    if git_commit is not None and git_commit != reviewed_commit:
        raise RunnerError("requested commit does not match the clean reviewed worktree")
    git_commit = reviewed_commit
    if re.fullmatch(r"[0-9a-f]{40}", git_commit) is None:
        raise ValueError("git commit must be an exact 40-character revision")
    closure = closure_manifest()
    committed_closure = _commit_closure_manifest(ROOT, git_commit)
    if committed_closure != closure:
        raise RunnerError("working-tree closure does not match the reviewed commit")
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
        "git_commit": git_commit,
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


def remote_qa_argv(remote_root: str, *, run_id: str, fuzz_cases: int, soak_iterations: int, seeds: tuple[int, ...], git_commit: str) -> list[str]:
    node = f"{remote_root}/node/bin/node"
    script = f"{remote_root}/qa/scripts/test/remote_external_tools_qa.mjs"
    output = f"{remote_root}/artifacts/remote-external-tools.json"
    return [
        "env", f"LAE_REMOTE_QA_MARKER={QA_MARKER}", f"LAE_REMOTE_RUN_ID={run_id}", f"LAE_REMOTE_GIT_COMMIT={git_commit}",
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


def validate_receipt(
    data: bytes, *, expected_run_id: str, expected_seeds: tuple[int, ...],
    expected_fuzz_cases: int, expected_soak_iterations: int,
    expected_git_commit: str | None = None,
) -> dict[str, object]:
    """Validate the complete producer schema, not a caller-selected subset."""

    if len(data) > MAX_RECEIPT_BYTES:
        raise RunnerError("remote receipt exceeds byte bound")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RunnerError("remote receipt contains a duplicate key")
            result[key] = value
        return result
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("remote receipt is not bounded UTF-8 JSON") from exc
    required = {
        "schema_version", "run_id", "remote_marker_verified", "status", "git_revision",
        "seeds", "bounds", "cases", "aggregate", "secret_free", "limitations",
    }
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != "remote-external-tools-qa.v1":
        raise RunnerError("unexpected remote QA receipt schema")
    if payload.get("run_id") != expected_run_id or payload.get("remote_marker_verified") is not True:
        raise RunnerError("remote receipt is not bound to this run and marker")
    if payload.get("secret_free") is not True or payload.get("status") not in {"PASS", "FAIL"}:
        raise RunnerError("remote receipt has unsafe or incomplete status")
    revision = payload.get("git_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RunnerError("remote receipt revision is invalid")
    if expected_git_commit is not None and revision != expected_git_commit:
        raise RunnerError("remote receipt revision does not match the requested commit")
    seeds = payload.get("seeds")
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 32 or any(
        isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 0x7FFFFFFF for seed in seeds
    ):
        raise RunnerError("remote receipt seeds are invalid")
    if seeds != list(expected_seeds):
        raise RunnerError("remote receipt seeds do not match the requested job")
    bounds = payload.get("bounds")
    expected_bounds = {
        "max_fuzz_cases": 256,
        "max_soak_iterations": 1000,
        "max_cases": 1536,
        "max_response_bytes": 262144,
    }
    if bounds != expected_bounds:
        raise RunnerError("remote receipt bounds do not match the shipped harness")
    cases = payload.get("cases")
    expected_case_count = FIXED_QA_CASES + expected_fuzz_cases + expected_soak_iterations
    if not isinstance(cases, list) or len(cases) != expected_case_count or len(cases) > expected_bounds["max_cases"]:
        raise RunnerError("remote receipt cases are invalid")
    passed = failed = 0
    case_ids: set[str] = set()
    for item in cases:
        if not isinstance(item, dict) or set(item) not in (
            {"id", "status", "assertions", "duration_ms"},
            {"id", "status", "assertions", "duration_ms", "error"},
        ):
            raise RunnerError("remote receipt case schema is invalid")
        case_id = item.get("id")
        status = item.get("status")
        if not isinstance(case_id, str) or not 1 <= len(case_id) <= 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", case_id) is None:
            raise RunnerError("remote receipt case id is invalid")
        if case_id in case_ids:
            raise RunnerError("remote receipt case IDs are not unique")
        case_ids.add(case_id)
        if status not in {"PASS", "FAIL"}:
            raise RunnerError("remote receipt case status is invalid")
        for field in ("assertions", "duration_ms"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0x7FFFFFFF:
                raise RunnerError(f"remote receipt case {field} is invalid")
        if status == "FAIL":
            failed += 1
            if item.get("error") not in FINITE_RECEIPT_ERRORS:
                raise RunnerError("remote receipt failed case has no finite error code")
        else:
            passed += 1
            if "error" in item:
                raise RunnerError("remote receipt passing case has an error")
    if any(case_id not in case_ids for case_id in FIXED_QA_CASE_IDS):
        raise RunnerError("remote receipt is missing a fixed QA case")
    aggregate = payload.get("aggregate")
    aggregate_keys = {"passed", "failed", "case_count", "requests", "assertion_count", "formula", "fuzz_cases", "soak_iterations", "seeds"}
    if not isinstance(aggregate, dict) or set(aggregate) != aggregate_keys or aggregate.get("seeds") != seeds:
        raise RunnerError("remote receipt aggregate schema is invalid")
    for field, maximum in (
        ("passed", 1536), ("failed", 1536), ("case_count", 1536), ("requests", 1_000_000),
        ("assertion_count", 1_000_000_000), ("fuzz_cases", 256), ("soak_iterations", 1000),
    ):
        value = aggregate.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise RunnerError(f"remote receipt aggregate {field} is invalid")
    if aggregate["passed"] != passed or aggregate["failed"] != failed or aggregate["case_count"] != len(cases) or passed + failed != len(cases):
        raise RunnerError("remote receipt aggregate does not match its cases")
    if aggregate["formula"] != "case-evidence-v1" or aggregate["requests"] != len(cases) or aggregate["assertion_count"] != sum(item["assertions"] for item in cases):
        raise RunnerError("remote receipt aggregate transport totals are incoherent")
    if aggregate["fuzz_cases"] != expected_fuzz_cases or aggregate["soak_iterations"] != expected_soak_iterations:
        raise RunnerError("remote receipt aggregate does not match the requested job")
    if (payload["status"] == "PASS") != (failed == 0):
        raise RunnerError("remote receipt status contradicts its cases")
    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not 1 <= len(limitations) <= 8 or any(
        not isinstance(item, str) or not 1 <= len(item) <= 256 or any(ord(char) < 0x20 for char in item)
        for item in limitations
    ):
        raise RunnerError("remote receipt limitations are invalid")
    text = data.decode("utf-8", errors="strict").lower()
    for forbidden in ("access_token", "client_secret", "authorization", "bearer ", "synthetic-token"):
        if forbidden in text:
            raise RunnerError("remote receipt contains forbidden credential material")
    return {
        "status": payload["status"], "run_id": expected_run_id, "secret_free": True,
        "passed": passed, "failed": failed, "case_count": len(cases),
    }


def validate_receipt_file(
    path: Path, *, expected_run_id: str, expected_seeds: tuple[int, ...],
    expected_fuzz_cases: int, expected_soak_iterations: int, expected_git_commit: str | None = None,
) -> dict[str, object]:
    return validate_receipt(
        _bounded_file(path, MAX_RECEIPT_BYTES), expected_run_id=expected_run_id,
        expected_seeds=expected_seeds, expected_fuzz_cases=expected_fuzz_cases,
        expected_soak_iterations=expected_soak_iterations, expected_git_commit=expected_git_commit,
    )


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


def _transport_projection(result: dict[str, object]) -> dict[str, object]:
    status = result.get("status")
    exit_code = result.get("exit_code")
    if status not in {"completed", "timeout", "output_overflow"}:
        raise RunnerError("transport returned an unknown status")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -255 <= exit_code <= 255):
        raise RunnerError("transport returned an invalid exit code")
    projected: dict[str, object] = {"status": status, "exit_code": exit_code}
    for field in ("stdout_bytes", "stderr_bytes"):
        value = result.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_OUTPUT_BYTES:
            raise RunnerError("transport returned invalid byte accounting")
        projected[field] = value
    return projected


def _run_checked(
    argv: list[str], *, deadline: float, requested_timeout: float,
    stage: str, lifecycle: dict[str, object], capture_stdout: bool = False,
) -> dict[str, object]:
    lifecycle["stage"] = stage
    result = run_argv(
        argv,
        timeout=_operation_timeout(deadline, requested_timeout),
        capture_stdout=capture_stdout,
    )
    projected = {"stage": stage, **_transport_projection(result)}
    stages = lifecycle.setdefault("remote_stages", [])
    if not isinstance(stages, list) or len(stages) >= 64:
        raise RunnerError("remote stage evidence exceeds bound")
    stages.append(projected)
    if result.get("status") != "completed" or result.get("exit_code") != 0:
        raise RunnerError("remote stage failed")
    return result


def _salvage_receipt(
    *, scp: list[str], info: dict[str, Any], remote_root: str,
    destination: Path, run_id: str, seeds: tuple[int, ...], fuzz_cases: int,
    soak_iterations: int, deadline: float, expected_git_commit: str | None = None,
    allowed_root: Path | None = None,
) -> dict[str, object]:
    """Salvage and validate one allowlisted receipt without delaying deletion."""

    try:
        timeout = _operation_timeout(deadline, SALVAGE_TIMEOUT_SECONDS)
        root = (allowed_root or RESULTS_ROOT).resolve()
        destination = _safe_receipt_destination(destination) if allowed_root is None else (destination if destination.is_absolute() else root / destination)
        parent = destination.parent.resolve()
        if root != parent and root not in parent.parents:
            raise RunnerError("receipt destination escapes the results root")
        parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise RunnerError("receipt destination already exists")
        temporary = parent / f".{destination.name}.{os.getpid()}.incoming"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        user = shadeform.validate_ssh_user(info["ssh_user"])
        remote = f"{user}@{info['ip']}:{remote_root}/artifacts/remote-external-tools.json"
        result = run_argv(scp + [remote, str(temporary)], timeout=timeout)
        projection = _transport_projection(result)
        if result.get("status") != "completed" or result.get("exit_code") != 0:
            return {"status": "salvage_failed", "code": "transport_failed", "transport": projection}
        verified = validate_receipt_file(
            temporary, expected_run_id=run_id, expected_seeds=seeds,
            expected_fuzz_cases=fuzz_cases, expected_soak_iterations=soak_iterations,
            expected_git_commit=expected_git_commit,
        )
        if destination.exists() or destination.is_symlink():
            raise RunnerError("receipt destination appeared during salvage")
        _publish_receipt_no_replace(temporary, destination)
        return {"status": "verified", "transport": projection, "receipt": verified}
    except BaseException as exc:
        return {"status": "salvage_failed", "code": _safe_failure_code(exc)}
    finally:
        with contextlib.suppress(UnboundLocalError, FileNotFoundError):
            temporary.unlink()


def _publish_receipt_no_replace(temporary: Path, destination: Path) -> None:
    """Publish through a directory FD without replacing an existing target."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = os.open(destination.parent, flags)
    try:
        os.link(
            temporary.name, destination.name,
            src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise RunnerError("receipt destination already exists") from exc
    finally:
        os.close(parent_fd)
    temporary.unlink()


def _deletion_confirmed(value: object) -> bool:
    if not isinstance(value, dict) or value.get("retry_required") is True:
        return False
    if value.get("status") == "already-cleaned":
        return True
    nested = value.get("deletion")
    if isinstance(nested, dict):
        return nested.get("success") is True
    return value.get("success") is True


def _teardown_complete(value: object) -> bool:
    """Require deletion plus cost/key/receipt bookkeeping to be complete."""

    return _deletion_confirmed(value) and isinstance(value, dict) and not any(
        key.endswith("_error_type") for key in value
    )


def execute(args: argparse.Namespace) -> dict[str, object]:
    if not REMOTE_EXECUTION_ENABLED:
        raise RunnerError("remote external-tools QA execution is gated pending explicit activation approval")
    env = shadeform.load_env(args.env_file)
    _assert_review_markers(env)
    plan = build_plan(phase_id=args.phase_id, run_id=args.run_id, runtime_hours=args.runtime_hours, fuzz_cases=args.fuzz_cases, soak_iterations=args.soak_iterations, env=env)
    # Use the exact provider auto-delete threshold rather than re-deriving it
    # from plan defaults; an operator ceiling may conservatively shorten it.
    auto_delete = shadeform._auto_delete(env, args.runtime_hours)
    provider_backstop_seconds = _provider_backstop_seconds(auto_delete)
    provider_deadline_epoch = datetime.fromisoformat(str(auto_delete["date_threshold"])).timestamp()
    provider_backstop_hours = provider_backstop_seconds / 3600.0
    available_work = min(provider_backstop_seconds, args.runtime_hours * 3600.0) - DELETION_RESERVE_SECONDS
    if available_work < MIN_REMOTE_WORK_SECONDS:
        raise RunnerError("runtime cannot preserve the required teardown reserve")
    api_key = shadeform.require_env(env, "SHADEFORM_API_KEY")
    candidates = shadeform.list_candidates(api_key, env, phase_id=args.phase_id, min_vram_gb=80, max_runtime_hours=args.runtime_hours)
    candidate = _approved_target(env, candidates)
    nonce = shadeform.new_ownership_nonce()
    instance_id: str | None = None
    key_id: str | None = None
    recorded = False
    ambiguous_create = False
    key_ambiguity_unresolved = False
    attempt_reserved = False
    attempt_settled = False
    watchdog: subprocess.Popen[bytes] | None = None
    info: dict[str, Any] | None = None
    ssh: list[str] | None = None
    scp: list[str] | None = None
    exact_created_at: float | None = None
    execution_deadline: float | None = None
    execution_deadline_epoch: float | None = None
    primary_failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    direct_cost_settled = False
    direct_key_cleanup_ok = False
    persistence_failure: BaseException | None = None
    remote_root = str(plan["remote_root"])
    receipt_local = _safe_receipt_destination(args.output or (RESULTS_ROOT / f"{args.run_id}.remote-external-tools.json"))
    lifecycle: dict[str, object] = {
        "schema": "local_bmo.shadeform.remote-external-tools-lifecycle.v2",
        "phase_id": args.phase_id,
        "run_id": args.run_id,
        "status": "starting",
        "stage": "pre_create",
        "node": plan["node"],
        "git_commit": plan["git_commit"],
        "closure_sha256": plan["closure_sha256"],
        "cleanup": {"status": "not_started"},
    }
    # Establish durable intent before generating a key or any provider
    # mutation.  If this fails, fail closed immediately rather than relying on
    # a later finally block that may never be reached.
    _persist_lifecycle(args.phase_id, lifecycle)
    with tempfile.TemporaryDirectory(prefix=f"remote-tools-{args.phase_id}-") as temporary:
        temporary_root = Path(temporary)
        staged_root = temporary_root / "closure"
        staged_closure = freeze_closure(ROOT, staged_root)
        staged_digest = hashlib.sha256(json.dumps(staged_closure, sort_keys=True).encode()).hexdigest()
        if staged_digest != plan["closure_sha256"]:
            raise RunnerError("source closure changed after plan creation")
        identity, public_key = shadeform.create_ephemeral_ssh_key(env, temporary_root / "ssh")
        known_hosts = temporary_root / "known_hosts"
        attempt_id: str | None = None
        key_name = f"j1m-{nonce}"  # reserve_create_attempt uses this exact binding
        public_key_sha256 = hashlib.sha256(public_key.encode("utf-8")).hexdigest()
        key_fingerprint = shadeform.ssh_public_key_fingerprint(public_key)
        previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        def cancel(_signum: int, _frame: Any) -> None:
            raise OperatorCancelled("operator cancellation signal")
        signal.signal(signal.SIGINT, cancel)
        signal.signal(signal.SIGTERM, cancel)
        try:
            attempt_id = shadeform.reserve_create_attempt(
                args.phase_id, nonce, candidate,
                backstop_hours=provider_backstop_hours,
                public_key_sha256=public_key_sha256,
                public_key_fingerprint=key_fingerprint,
            )
            attempt_reserved = True
            # Arm recovery before the first provider mutation, including the
            # SSH-key POST.  The watcher starts from the durable nonce/name/
            # fingerprint intent and can reconcile either a key-only outcome
            # or a subsequently-created instance without an ID supplied by a
            # crashed launcher.
            launcher_pid = os.getpid()
            launcher_marker = shadeform.process_start_marker(launcher_pid)
            execution_deadline = time.monotonic() + min(provider_backstop_seconds, args.runtime_hours * 3600.0)
            execution_deadline_epoch = min(provider_deadline_epoch, time.time() + args.runtime_hours * 3600.0)
            watchdog_seconds = _remaining_before_cleanup(execution_deadline)
            expected_instance_name = shadeform.owned_instance_name(args.run_id, nonce)
            watchdog_command = [
                sys.executable, str(ROOT / "scripts" / "shadeform_watchdog.py"),
                "--phase-id", args.phase_id, "--launcher-pid", str(launcher_pid),
                "--max-seconds", str(watchdog_seconds), "--deadline-epoch", str(execution_deadline_epoch),
                "--env-file", str(args.env_file), "--ownership-nonce", nonce,
                "--ssh-key-name", key_name, "--ssh-key-fingerprint", key_fingerprint,
                "--allow-unrecorded-exact", "--key-only-recovery",
                "--instance-name", expected_instance_name, "--precreate-recovery",
                "--cloud", candidate.cloud, "--region", candidate.region,
                "--instance-type", candidate.instance_type,
                "--hourly-usd", str(candidate.hourly_usd), "--gpu", candidate.gpu,
                "--gpu-count", "1", "--vram-gb", str(candidate.vram_gb),
                "--os-image", candidate.os_image,
            ]
            if launcher_marker:
                watchdog_command += ["--launcher-start-marker", launcher_marker]
            watchdog = subprocess.Popen(
                watchdog_command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=_minimal_local_env(),
            )
            lifecycle["watchdog"] = {"status": "prearmed", "pid": watchdog.pid, "max_seconds": round(watchdog_seconds, 3), "recovery": "exact_nonce_name"}
            lifecycle["stage"] = "ssh_key_create"
            try:
                key_id = shadeform.add_ssh_key(api_key, args.phase_id, key_name, public_key)
            except shadeform.AmbiguousProviderOutcome as exc:
                key_ambiguity_unresolved = True
                try:
                    key_id = shadeform.reconcile_ssh_key(
                        api_key, args.phase_id, expected_name=key_name,
                        expected_public_key=public_key, expected_fingerprint=key_fingerprint,
                    )
                    key_ambiguity_unresolved = False
                    lifecycle["ssh_key_reconciliation"] = {"status": "exact_match"}
                except BaseException as reconcile_exc:
                    lifecycle["ssh_key_reconciliation"] = {
                        "status": "unresolved", "retry_required": True,
                        "code": _safe_failure_code(reconcile_exc),
                    }
                    with contextlib.suppress(Exception):
                        shadeform.append_incident({
                            "phase_id": args.phase_id,
                            "incident": "ssh-key-create-ambiguous-unresolved",
                            "nonce": nonce,
                            "ssh_key_name": key_name,
                            "ssh_public_key_sha256": public_key_sha256,
                            "error_type": _safe_failure_code(reconcile_exc),
                        })
                    raise shadeform.AmbiguousProviderOutcome("SSH key create could not be safely reconciled") from exc
                # A transport-ambiguous key create is never allowed to flow
                # onward to an instance POST, even after exact reconciliation.
                raise shadeform.AmbiguousProviderOutcome("SSH key create was reconciled; instance creation refused") from exc
            shadeform.verify_ssh_key_ownership(api_key, args.phase_id, key_id, expected_name=key_name, expected_public_key=public_key)
            shadeform.reserve_create_attempt(
                args.phase_id, nonce, candidate,
                backstop_hours=provider_backstop_hours,
                public_key_sha256=public_key_sha256,
                public_key_fingerprint=key_fingerprint,
                ssh_key_id=key_id,
            )
            lifecycle["stage"] = "instance_create"
            try:
                instance_id = shadeform.create_instance(api_key, env, phase_id=args.phase_id, run_id=args.run_id, candidate=candidate, ssh_key_id=key_id, nonce=nonce, max_runtime_hours=args.runtime_hours)
                exact_created_at = time.monotonic()
                lifecycle["instance_id"] = instance_id
                lifecycle["deadline"] = {
                    "work_seconds": round(_remaining_before_cleanup(execution_deadline), 3),
                    "teardown_reserve_seconds": DELETION_RESERVE_SECONDS,
                }
                shadeform.append_cost_event({
                    "instance_id": instance_id, "phase_id": args.phase_id,
                    "status": "pending",
                    "estimated_cost_usd": round(candidate.hourly_usd * provider_backstop_hours, 6),
                })
            except BaseException as exc:
                ambiguous_create = shadeform.is_ambiguous_transport(exc)
                with contextlib.suppress(Exception):
                    shadeform.append_incident({"phase_id": args.phase_id, "incident": "create-response-ambiguous" if ambiguous_create else "create-definitive-failure", "nonce": nonce, "ssh_key_id": key_id, "error_type": _safe_failure_code(exc)})
                raise
            record = shadeform.OwnedResource(phase_id=args.phase_id, run_id=args.run_id, instance_id=instance_id, ownership_nonce=nonce, ssh_key_id=key_id, ssh_key_name=key_name, gpu=candidate.gpu, cloud=candidate.cloud, region=candidate.region, hourly_usd=candidate.hourly_usd, created_at_utc=shadeform.utc_now().isoformat(), active_deadline_utc=(shadeform.utc_now() + shadeform.timedelta(seconds=ACTIVATION_TIMEOUT_SECONDS)).isoformat(), run_deadline_utc=(shadeform.utc_now() + shadeform.timedelta(seconds=min(provider_backstop_seconds, args.runtime_hours * 3600.0))).isoformat(), instance_type=candidate.instance_type, gpu_count=1, vram_gb=candidate.vram_gb, os_image=candidate.os_image, ssh_public_key=public_key, launcher_pid=launcher_pid, launcher_start_marker=launcher_marker, ssh_public_key_fingerprint=key_fingerprint)
            # Arm the watchdog before the owned-resource write.  The durable
            # create reservation plus exact instance ID lets it recover the
            # narrow crash window between provider success and ledger record.
            shadeform.write_owned_resource(record)
            recorded = True
            shadeform.append_cost_event({"instance_id": attempt_id, "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
            attempt_settled = True
            lifecycle["watchdog"]["status"] = "running"
            lifecycle["stage"] = "activation"
            activation_timeout = int(_operation_timeout(execution_deadline, ACTIVATION_TIMEOUT_SECONDS))
            info = shadeform.wait_active(api_key, args.phase_id, instance_id, timeout_seconds=activation_timeout)
            shadeform.verify_instance_ownership(info, instance_id=instance_id, phase_id=args.phase_id, nonce=nonce, expected_name=shadeform.owned_instance_name(args.run_id, nonce), ssh_key_id=key_id, expected_cloud=candidate.cloud, expected_region=candidate.region, expected_instance_type=candidate.instance_type, expected_hourly_usd=candidate.hourly_usd, expected_gpu=candidate.gpu, expected_gpu_count=1, expected_vram_gb=candidate.vram_gb, expected_os_image=candidate.os_image)
            if _operation_timeout(execution_deadline, 120.0) < 119.0:
                raise TimeoutError("insufficient deadline for bounded host-key acquisition")
            lifecycle["stage"] = "host_key_pin"
            lifecycle["host_key"] = shadeform.acquire_pinned_host_key(info, known_hosts)
            ssh = shadeform.ssh_base(info, identity, known_hosts)
            scp = shadeform.scp_base(info, identity, known_hosts)
            ssh_user = shadeform.validate_ssh_user(info["ssh_user"])
            remote_destination = f"{ssh_user}@{info['ip']}"
            # Schedule the host backstop before the hard deletion deadline;
            # rounding up here can arm shutdown after the provider deadline.
            shutdown_minutes = max(1, math.floor(_remaining_before_cleanup(execution_deadline) / 60.0))
            _run_checked(ssh + ["sudo", "shutdown", "-h", f"+{shutdown_minutes}"], deadline=execution_deadline, requested_timeout=30, stage="host_shutdown", lifecycle=lifecycle)
            for command in _remote_bootstrap_commands(remote_root, ssh_user):
                result = _run_checked(ssh + command, deadline=execution_deadline, requested_timeout=180, stage=f"setup:{command[0]}", lifecycle=lifecycle, capture_stdout=(command[0] == "sha256sum"))
                if command[0] == "sha256sum":
                    fields = str(result.get("stdout", "")).split()
                    if len(fields) < 2 or fields[0].lower() != NODE_SHA256 or Path(fields[-1]).name != NODE_ARCHIVE:
                        raise RunnerError("remote Node archive hash did not match the pinned release")
            for relative in UPLOAD_FILES:
                remote_file = f"{remote_root}/qa/{relative}"
                destination = f"{remote_destination}:{remote_file}"
                _run_checked(ssh + ["mkdir", "-p", str(Path(remote_file).parent)], deadline=execution_deadline, requested_timeout=30, stage="upload:mkdir", lifecycle=lifecycle)
                staged_file = staged_root / relative
                _run_checked(scp + [str(staged_file), destination], deadline=execution_deadline, requested_timeout=120, stage=f"upload:{relative}", lifecycle=lifecycle)
                hash_result = _run_checked(ssh + ["sha256sum", "--", remote_file], deadline=execution_deadline, requested_timeout=30, stage=f"verify-upload:{relative}", lifecycle=lifecycle, capture_stdout=True)
                fields = str(hash_result.get("stdout", "")).split()
                expected = next(item["sha256"] for item in staged_closure if item["path"] == relative)
                if len(fields) != 2 or fields[0].lower() != expected or Path(fields[1]).name != Path(remote_file).name:
                    raise RunnerError("remote upload hash did not match the frozen closure")
            lifecycle["stage"] = "qa_job"
            qa_result = run_argv(ssh + remote_qa_argv(remote_root, run_id=args.run_id, fuzz_cases=args.fuzz_cases, soak_iterations=args.soak_iterations, seeds=tuple(args.seeds), git_commit=str(plan["git_commit"])), timeout=_operation_timeout(execution_deadline, args.runtime_hours * 3600.0))
            lifecycle["qa_transport"] = _transport_projection(qa_result)
            if qa_result.get("status") != "completed" or qa_result.get("exit_code") not in {0, 1}:
                raise RunnerError("remote QA transport did not complete")
            if qa_result.get("exit_code") != 0:
                raise RunnerError("remote QA reported failing cases")
            lifecycle["status"] = "awaiting_receipt"
        except BaseException as exc:
            primary_failure = exc
            lifecycle["status"] = "cancelled" if isinstance(exc, (KeyboardInterrupt, OperatorCancelled, SystemExit)) else "failed"
            lifecycle["failure"] = {"code": _safe_failure_code(exc), "stage": lifecycle.get("stage", "unknown")}
        finally:
            try:
                _persist_lifecycle(args.phase_id, lifecycle)
            except BaseException as exc:
                persistence_failure = exc
            if scp is not None and info is not None and execution_deadline is not None:
                lifecycle["salvage"] = _salvage_receipt(scp=scp, info=info, remote_root=remote_root, destination=receipt_local, run_id=args.run_id, seeds=tuple(args.seeds), fuzz_cases=args.fuzz_cases, soak_iterations=args.soak_iterations, deadline=execution_deadline, expected_git_commit=str(plan["git_commit"]))
                if lifecycle["salvage"].get("status") == "verified":
                    lifecycle["qa_receipt"] = lifecycle["salvage"].get("receipt")
                    if lifecycle["qa_receipt"].get("status") != "PASS":
                        primary_failure = primary_failure or RunnerError("remote QA receipt is not a pass")
                elif primary_failure is None:
                    primary_failure = RunnerError("remote QA receipt could not be salvaged and verified")
            elif instance_id is not None:
                lifecycle["salvage"] = {"status": "not_available"}
            if instance_id is not None:
                if recorded:
                    try:
                        lifecycle["deletion"] = shadeform_teardown(args.phase_id, instance_id, args.env_file, deadline=execution_deadline)
                        if not _teardown_complete(lifecycle["deletion"]):
                            cleanup_failure = RunnerError("exact teardown bookkeeping was not confirmed")
                    except BaseException as exc:
                        lifecycle["deletion"] = {"status": "delete_failed", "retry_required": True, "code": _safe_failure_code(exc)}
                        cleanup_failure = RunnerError("exact instance teardown was not confirmed")
                else:
                    try:
                        remaining = execution_deadline - time.monotonic() if execution_deadline is not None else 90.0
                        if remaining < 1.0:
                            raise TimeoutError("instance ownership verification deadline exhausted")
                        info_before_delete = shadeform.instance_info(
                            api_key, args.phase_id, instance_id,
                            timeout=min(90.0, remaining),
                        )
                        shadeform.verify_instance_ownership(
                            info_before_delete,
                            instance_id=instance_id,
                            phase_id=args.phase_id,
                            nonce=nonce,
                            expected_name=shadeform.owned_instance_name(args.run_id, nonce),
                            ssh_key_id=key_id,
                            expected_cloud=candidate.cloud,
                            expected_region=candidate.region,
                            expected_instance_type=candidate.instance_type,
                            expected_hourly_usd=candidate.hourly_usd,
                            expected_gpu=candidate.gpu,
                            expected_gpu_count=1,
                            expected_vram_gb=candidate.vram_gb,
                            expected_os_image=candidate.os_image,
                        )
                        lifecycle["deletion"] = shadeform._delete_instance(api_key, args.phase_id, instance_id, deadline=execution_deadline)
                    except BaseException as exc:
                        lifecycle["deletion"] = {"success": False, "code": _safe_failure_code(exc)}
                    if not _deletion_confirmed(lifecycle["deletion"]):
                        cleanup_failure = RunnerError("exact instance deletion was not confirmed")
                    else:
                        # A failed pending append after a successful create
                        # must not erase the incurred cost. A direct exact
                        # cleanup appends a settled event even when no prior
                        # instance row made it to disk.
                        if exact_created_at is not None:
                            try:
                                shadeform.append_cost_event({"instance_id": instance_id, "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": round(candidate.hourly_usd * max(0.0, time.monotonic() - exact_created_at) / 3600.0, 6)})
                                direct_cost_settled = True
                            except BaseException as exc:
                                cleanup_failure = RunnerError("instance cost settlement was not persisted")
                                lifecycle["cost_settlement"] = {"status": "failed", "code": _safe_failure_code(exc)}
                        if key_id is not None:
                            try:
                                key_timeout = min(90.0, execution_deadline - time.monotonic())
                                if key_timeout < 1.0:
                                    raise TimeoutError("SSH key ownership verification deadline exhausted")
                                shadeform.verify_ssh_key_ownership(
                                    api_key, args.phase_id, key_id,
                                    expected_name=key_name, expected_public_key=public_key,
                                    timeout=key_timeout,
                                )
                                shadeform.delete_ssh_key(api_key, args.phase_id, key_id, deadline=execution_deadline)
                                lifecycle["key_cleanup"] = {"status": "deleted"}
                                direct_key_cleanup_ok = True
                            except BaseException as exc:
                                cleanup_failure = RunnerError("exact SSH key deletion was not confirmed")
                                lifecycle["key_cleanup"] = {"status": "failed", "code": _safe_failure_code(exc)}
                        if direct_cost_settled and direct_key_cleanup_ok:
                            # A provider POST can succeed before the runtime
                            # ownership file is durably written (for example,
                            # when Markdown bookkeeping fails).  Remove only
                            # a matching partial record; never clear a guessed
                            # or mismatched phase record.
                            try:
                                partial = shadeform.read_owned_resource(args.phase_id)
                                if partial is not None:
                                    if partial.instance_id != instance_id:
                                        raise RunnerError("partial ownership record does not match deleted instance")
                                    shadeform.clear_owned_resource(args.phase_id, instance_id)
                                    lifecycle["partial_ownership"] = {"status": "cleared_after_confirmed_deletion"}
                            except BaseException as exc:
                                cleanup_failure = RunnerError("partial ownership record could not be safely cleared")
                                lifecycle["partial_ownership"] = {"status": "cleanup_failed", "code": _safe_failure_code(exc)}
                if _teardown_complete(lifecycle.get("deletion")) and cleanup_failure is None:
                    _stop_watchdog(watchdog)
                    if isinstance(lifecycle.get("watchdog"), dict):
                        lifecycle["watchdog"]["status"] = "stopped_after_confirmed_deletion"
            elif key_id is not None and not ambiguous_create and not key_ambiguity_unresolved:
                try:
                    key_timeout = min(90.0, execution_deadline - time.monotonic()) if execution_deadline is not None else 90.0
                    if key_timeout < 1.0:
                        raise TimeoutError("SSH key ownership verification deadline exhausted")
                    shadeform.verify_ssh_key_ownership(
                        api_key, args.phase_id, key_id,
                        expected_name=key_name, expected_public_key=public_key,
                        timeout=key_timeout,
                    )
                    shadeform.delete_ssh_key(api_key, args.phase_id, key_id, deadline=execution_deadline)
                    lifecycle["key_cleanup"] = {"status": "deleted"}
                except BaseException as exc:
                    lifecycle["key_cleanup"] = {"status": "failed", "code": _safe_failure_code(exc)}
                    cleanup_failure = RunnerError("exact SSH key deletion was not confirmed")
            if attempt_reserved and not attempt_settled and not ambiguous_create and not key_ambiguity_unresolved and cleanup_failure is None:
                try:
                    shadeform.append_cost_event({"instance_id": attempt_id, "phase_id": args.phase_id, "status": "settled", "actual_cost_usd": 0.0, "reservation": "pre-create-attempt-reconciled"})
                    attempt_settled = True
                except BaseException as exc:
                    cleanup_failure = RunnerError("create-attempt reservation was not settled")
                    lifecycle["attempt_settlement"] = {"status": "failed", "code": _safe_failure_code(exc)}
            if instance_id is None and key_ambiguity_unresolved:
                lifecycle["cleanup"] = {"status": "ambiguous_key", "retry_required": True}
                lifecycle["status"] = "failed"
            elif instance_id is None and ambiguous_create:
                lifecycle["cleanup"] = {"status": "ambiguous_create", "retry_required": True}
                lifecycle["status"] = "failed"
            elif cleanup_failure is not None:
                lifecycle["cleanup"] = {"status": "failed", "retry_required": True, "code": _safe_failure_code(cleanup_failure)}
                lifecycle["status"] = "failed"
                with contextlib.suppress(Exception):
                    shadeform.append_incident({"phase_id": args.phase_id, "incident": "remote-external-tools-cleanup-unconfirmed", "nonce": nonce, "instance_id": instance_id, "ssh_key_id": key_id, "error_type": _safe_failure_code(cleanup_failure)})
            elif instance_id is None and watchdog is not None:
                _stop_watchdog(watchdog)
                lifecycle["watchdog"]["status"] = "stopped_no_instance"
                lifecycle["cleanup"] = {"status": "confirmed"}
            else:
                lifecycle["cleanup"] = {"status": "confirmed"}
            if primary_failure is None and cleanup_failure is None and persistence_failure is None:
                lifecycle["status"] = "completed"
            try:
                _persist_lifecycle(args.phase_id, lifecycle)
                # A previous persistence failure is durable evidence and may
                # not be erased merely because a later retry succeeded.
            except BaseException as exc:
                persistence_failure = exc
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
        if cleanup_failure is not None:
            raise cleanup_failure
        if primary_failure is not None:
            raise primary_failure
        if persistence_failure is not None:
            raise RunnerError("lifecycle receipt could not be persisted") from persistence_failure
    return lifecycle


def shadeform_teardown(phase_id: str, instance_id: str, env_file: Path, *, deadline: float | None) -> dict[str, object]:
    from scripts.shadeform_teardown import teardown_exact
    return teardown_exact(phase_id, instance_id, env_file=env_file, deadline=deadline)


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
    assert remote_qa_argv(str(plan["remote_root"]), run_id="self-test", fuzz_cases=1, soak_iterations=1, seeds=(17,), git_commit=str(plan["git_commit"]))[1].startswith("LAE_REMOTE_QA_MARKER=")
    bootstrap = _remote_bootstrap_commands(str(plan["remote_root"]), "runner")
    assert bootstrap[0] == ["sudo", "mkdir", "-p", "/scratch"]
    assert bootstrap[-1][-1].endswith("/node/bin/node")
    transport = run_argv([sys.executable, "-c", "print('transport-ok')"], timeout=5, capture_stdout=True)
    assert transport["status"] == "completed" and transport["exit_code"] == 0 and transport["stdout"] == "transport-ok\n"
    valid_receipt = json.dumps({
        "schema_version": "remote-external-tools-qa.v1", "run_id": "self-test",
        "remote_marker_verified": True, "status": "PASS", "git_revision": str(plan["git_commit"]),
        "seeds": [17],
        "bounds": {"max_fuzz_cases": 256, "max_soak_iterations": 1000, "max_cases": 1536, "max_response_bytes": 262144},
        "cases": [{"id": f"self-test-{index}", "status": "PASS", "assertions": 1, "duration_ms": 0} for index in range(FIXED_QA_CASES)],
        "aggregate": {"passed": FIXED_QA_CASES, "failed": 0, "case_count": FIXED_QA_CASES, "requests": FIXED_QA_CASES, "assertion_count": FIXED_QA_CASES, "formula": "case-evidence-v1", "fuzz_cases": 0, "soak_iterations": 0, "seeds": [17]},
        "secret_free": True, "limitations": ["Synthetic self-test evidence only."],
    }).encode()
    assert validate_receipt(valid_receipt, expected_run_id="self-test", expected_seeds=(17,), expected_fuzz_cases=0, expected_soak_iterations=0)["status"] == "PASS"
    try:
        validate_receipt(b"{}", expected_run_id="self-test", expected_seeds=(17,), expected_fuzz_cases=0, expected_soak_iterations=0)
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
        # This gate must precede environment loading and planning: the
        # inherited wrapper is not an approved mutation lifecycle and must not
        # even touch credentials when an operator accidentally supplies
        # ``--execute``.
        if args.execute and not REMOTE_EXECUTION_ENABLED:
            raise RunnerError("remote external-tools QA execution is gated pending explicit activation approval")
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
