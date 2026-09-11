#!/usr/bin/env python3
"""Score one comparator arm on the pinned upstream ``llama-server``.

This is the comparator counterpart of ``scripts/test/remote_model_eval.py``.
It exists because the product engine compiles the Q4 identity in
(``native/model_validation/model_validator.hpp``) and therefore can never
load a Q8_0 or bf16 artifact -- a correct gate that this slice does not
weaken.  ``model/quality-eval/quality-fixture-spec.json``
``comparison.runtime_oracle`` names the pinned upstream same-artifact build as
the runtime oracle, so the comparator arms are hosted there instead.

Everything is bounded, typed and loopback-only:

* the artifact is re-hashed and must match the anchored
  ``artifacts/qwen35-9b/scan-receipt.json`` identity exactly;
* the server binds ``127.0.0.1`` only and requires a random per-launch bearer
  read from a 0600 file, never from ``argv`` and never logged;
* the unmodified fixture is scored by ``scripts/test/evaluate_tool_calls.py``
  with ``--transport upstream-openai``;
* the receipt carries metrics and a bounded ``{id, category, passed}`` vector,
  never a prompt, a response, a model output or a token.

It runs no model on the operator laptop, contacts no provider and downloads
nothing.  See ``model/COMPARATOR_EVAL.md`` sections 2.2a, 5 and 6.1.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import stat
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "local_bmo.j1m.comparator-eval-receipt.v1"
CACHE_STATE = "cold_process_fresh_server_per_arm"
TRANSPORT = "upstream-openai"
HOST_KIND = "pinned-upstream-llama-server"
# The three arms of model/COMPARATOR_EVAL.md section 2.2a and the exact file
# each one must be.  A caller cannot name a fourth.
ARM_ARTIFACTS = {
    "q4_k_m": ("Qwen3.5-9B-Q4_K_M.gguf", "Q4_K_M"),
    "q8_0": ("Qwen3.5-9B-Q8_0.gguf", "Q8_0"),
    "bf16": ("Qwen3.5-9B-bf16.gguf", "bf16"),
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CASE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
CATEGORY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_RECEIPT_BYTES = 256 * 1024
MAX_METADATA_BYTES = 256 * 1024
FIXTURE_MAX_BYTES = 256 * 1024
MAX_EVAL_OUTPUT = 256 * 1024
MAX_EVAL_CASES = 64
MAX_EVAL_TOOLS = 33
MAX_OUTPUT_RESERVE_TOKENS = 256
HASH_BLOCK = 1024 * 1024
SERVER_READY_POLL_SECONDS = 1.0
SERVER_TERM_GRACE_SECONDS = 15.0
# Every refusal is one of these.  The first six are the vocabulary
# scripts/j1m_orchestrator.py and scripts/test/compare_model_quality.py
# already share; the rest are arm-local detail that the orchestrator maps
# onto ``comparator_stage_failed``.
ERROR_CODES = frozenset({
    "comparator_arm_unknown",
    "comparator_artifact_identity_mismatch",
    "comparator_anchor_mismatch",
    "comparator_checkout_revision_mismatch",
    "comparator_server_binary_missing",
    "comparator_server_launch_failed",
    "comparator_server_ready_timeout",
    "comparator_token_file_not_private",
    "comparator_fixture_invalid",
    "comparator_evaluator_failed",
    "comparator_result_invalid",
    "comparator_receipt_write_failed",
    "comparator_deadline_exhausted",
})


class ComparatorFailure(ValueError):
    """A typed, secret-free refusal."""

    def __init__(self, code: str):
        super().__init__(code if code in ERROR_CODES else "comparator_result_invalid")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stage_timeout(deadline: float, cap: float, code: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining < 1.0:
        raise ComparatorFailure(code)
    return min(float(cap), remaining)


def _read_bounded(path: Path, limit: int, *, code: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise ComparatorFailure(code) from exc
    if len(data) > limit:
        raise ComparatorFailure(code)
    return data


def _strict_json_object(raw: bytes, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComparatorFailure(code) from exc
    if not isinstance(value, dict):
        raise ComparatorFailure(code)
    return value


def sha256_file(path: Path, *, code: str) -> tuple[str, int]:
    import hashlib

    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(HASH_BLOCK)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise ComparatorFailure(code) from exc
    return digest.hexdigest(), size


def _build_no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def verify_arm_artifact(arm: str, model: Path, scan_receipt: Path, anchor_sha256: str) -> dict[str, Any]:
    """Re-hash the rebuilt comparator against the source-controlled anchor.

    The anchor is the checked-in ``artifacts/qwen35-9b/scan-receipt.json``,
    identified by the digest the orchestrator pins in source.  A scan receipt
    with any other digest, or an artifact that does not reproduce byte-exactly,
    refuses rather than evaluating unknown bytes.
    """

    if arm not in ARM_ARTIFACTS:
        raise ComparatorFailure("comparator_arm_unknown")
    if not isinstance(anchor_sha256, str) or not SHA256.fullmatch(anchor_sha256):
        raise ComparatorFailure("comparator_anchor_mismatch")
    import hashlib

    raw = _read_bounded(scan_receipt, MAX_METADATA_BYTES, code="comparator_anchor_mismatch")
    if hashlib.sha256(raw).hexdigest() != anchor_sha256:
        raise ComparatorFailure("comparator_anchor_mismatch")
    payload = _strict_json_object(raw, code="comparator_anchor_mismatch")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ComparatorFailure("comparator_anchor_mismatch")
    name, quantization = ARM_ARTIFACTS[arm]
    expected = next((item for item in artifacts if isinstance(item, dict) and item.get("name") == name), None)
    if (not isinstance(expected, dict) or not isinstance(expected.get("sha256"), str) or
            not SHA256.fullmatch(expected["sha256"]) or isinstance(expected.get("size_bytes"), bool) or
            not isinstance(expected.get("size_bytes"), int) or expected["size_bytes"] <= 0):
        raise ComparatorFailure("comparator_anchor_mismatch")
    if model.name != name:
        raise ComparatorFailure("comparator_artifact_identity_mismatch")
    digest, size = sha256_file(model, code="comparator_artifact_identity_mismatch")
    if digest != expected["sha256"] or size != expected["size_bytes"]:
        raise ComparatorFailure("comparator_artifact_identity_mismatch")
    return {"name": name, "size_bytes": size, "sha256": digest, "quantization": quantization}


# Required run-identity binding. The orchestrator uploads this file next to the
# uploaded config before the first receipt-producing command; the path is
# source-fixed on both sides so no caller, configuration value, or remote
# response can redirect it.
_RUN_IDENTITY_PATH = Path("/scratch/j1m/run-identity.json")
_RUN_IDENTITY_SCHEMA = "local_bmo.j1m.run-identity.v1"
_RUN_IDENTITY_FIELDS = ("run_id", "instance_id")
_RUN_IDENTITY_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


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


def fixture_contract(path: Path) -> dict[str, Any]:
    """Read the bounded fixture identity every arm is held to."""

    import hashlib

    raw = _read_bounded(path, FIXTURE_MAX_BYTES, code="comparator_fixture_invalid")
    payload = _strict_json_object(raw, code="comparator_fixture_invalid")
    tools = payload.get("tools")
    cases = payload.get("cases")
    limits = payload.get("limits")
    if (not isinstance(tools, list) or not 1 <= len(tools) <= MAX_EVAL_TOOLS or
            not isinstance(cases, list) or not 1 <= len(cases) <= MAX_EVAL_CASES or
            not isinstance(limits, dict)):
        raise ComparatorFailure("comparator_fixture_invalid")
    context_tokens = limits.get("context_tokens")
    output_reserve = limits.get("max_output_tokens")
    max_cases = limits.get("max_cases")
    temperature = limits.get("temperature")
    if (isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or not 512 <= context_tokens <= 131072 or
            isinstance(output_reserve, bool) or not isinstance(output_reserve, int) or not 1 <= output_reserve <= MAX_OUTPUT_RESERVE_TOKENS or
            isinstance(max_cases, bool) or not isinstance(max_cases, int) or not 1 <= max_cases <= MAX_EVAL_CASES or
            temperature != 0):
        raise ComparatorFailure("comparator_fixture_invalid")
    categories: dict[str, int] = {}
    names: list[str] = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not CASE_ID.fullmatch(case["id"]):
            raise ComparatorFailure("comparator_fixture_invalid")
        category = case.get("category")
        if not isinstance(category, str) or not CATEGORY.fullmatch(category):
            raise ComparatorFailure("comparator_fixture_invalid")
        categories[category] = categories.get(category, 0) + 1
        names.append(case["id"])
    if len(set(names)) != len(names):
        raise ComparatorFailure("comparator_fixture_invalid")
    tool_names = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ComparatorFailure("comparator_fixture_invalid")
        tool_names.append(function["name"])
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "case_count": len(cases),
        "category_counts": dict(sorted(categories.items())),
        "tool_count": len(tools),
        "tool_names": sorted(tool_names),
        "context_tokens": context_tokens,
        "output_reserve_tokens": output_reserve,
        "max_cases": max_cases,
        "temperature": 0,
    }


def resolve_token_file(args: argparse.Namespace) -> tuple[Path, Path | None]:
    """Return this arm's bearer path, creating a private directory if needed.

    ``--token-file`` names a path on the ephemeral host, and
    ``j1m_runner.validate_persisted_argv`` accepts a token-file operand only
    when it is a canonical private handle *on the machine building the argv* --
    a proof that cannot exist for a remote path.  The orchestrator therefore no
    longer names it, and every comparator stage's argv is accepted instead of
    refused before it can spawn.  An explicit ``--token-file`` is still honoured
    for tests; otherwise this process mints its own ``0700`` directory, which is
    also what lets the bearer be removed with it after the arm.
    """

    if getattr(args, "token_file", None):
        return Path(args.token_file), None
    directory = Path(tempfile.mkdtemp(prefix=f"comparator-token-{args.arm}-"))
    os.chmod(directory, 0o700)
    return directory / "comparator-token", directory


def write_token(path: Path) -> str:
    """Create a fresh 0600 bearer file. The value is returned, never logged."""

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = -1
    try:
        descriptor = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ComparatorFailure("comparator_token_file_not_private") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ComparatorFailure("comparator_token_file_not_private")
    return token


def server_launch_argv(args: argparse.Namespace, token_file: Path) -> list[str]:
    """The exact upstream server argv. Loopback only; no bearer in argv.

    ``--api-key-file`` rather than ``--api-key`` is deliberate: a bearer on
    the command line is visible to every process on the host, which
    ``AGENTS.md`` forbids and ``remote_model_eval.py`` already refuses.
    ``--no-webui`` and the OFF web-UI build keep the binary to the API
    surface the evaluator uses.
    """

    return [
        os.fspath(args.server),
        "--model", os.fspath(args.model),
        "--host", "127.0.0.1",
        "--port", str(int(args.port)),
        "--api-key-file", os.fspath(token_file),
        "--ctx-size", str(int(args.context)),
        "--n-gpu-layers", str(int(args.gpu_layers)),
        "--parallel", "1",
        "--threads", "1",
        "--jinja",
        "--temp", "0",
        "--no-webui",
    ]


def wait_for_health(port: int, token: str, deadline: float) -> float:
    """Poll the upstream ``/health`` endpoint until it is ready or time runs out."""

    started = time.monotonic()
    endpoint = f"http://127.0.0.1:{int(port)}/health"
    opener = _build_no_proxy_opener()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ComparatorFailure("comparator_server_ready_timeout")
        request = urllib.request.Request(
            endpoint, headers={"Authorization": f"Bearer {token}"}, method="GET")
        try:
            with opener.open(request, timeout=min(5.0, remaining)) as response:
                body = response.read(4096)
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return round((time.monotonic() - started) * 1000, 1)
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        time.sleep(min(SERVER_READY_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _run_bounded(command: list[str], *, timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(command, stdout=out, stderr=err, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "exit_code": None, "stdout": ""}
        except OSError:
            return {"status": "spawn_failed", "exit_code": None, "stdout": ""}
        out.seek(0)
        data = out.read(MAX_EVAL_OUTPUT + 1)
        if len(data) > MAX_EVAL_OUTPUT:
            return {"status": "output_too_large", "exit_code": result.returncode, "stdout": ""}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"status": "output_invalid", "exit_code": result.returncode, "stdout": ""}
        return {"status": "completed", "exit_code": result.returncode, "stdout": text}


def parse_evaluator_output(result: dict[str, Any], contract: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Validate the evaluator aggregate against the fixture it was handed."""

    if result.get("status") != "completed" or result.get("exit_code") not in {0, 1}:
        raise ComparatorFailure("comparator_evaluator_failed")
    try:
        payload = json.loads(result.get("stdout", ""))
    except json.JSONDecodeError as exc:
        raise ComparatorFailure("comparator_result_invalid") from exc
    if not isinstance(payload, dict):
        raise ComparatorFailure("comparator_result_invalid")
    indicators = payload.pop("case_indicators", None)
    required = {"case_count", "passed", "failed", "errors", "peak_rss_kib",
                "category_summary", "canary", "error_diagnostics", "quality_diagnostics"}
    if set(payload) != required:
        raise ComparatorFailure("comparator_result_invalid")
    for field in ("case_count", "passed", "failed", "errors"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ComparatorFailure("comparator_result_invalid")
    if payload["case_count"] != contract["case_count"]:
        raise ComparatorFailure("comparator_result_invalid")
    if payload["passed"] + payload["failed"] + payload["errors"] != payload["case_count"]:
        raise ComparatorFailure("comparator_result_invalid")
    summary = payload["category_summary"]
    if not isinstance(summary, dict) or {
            key: value["case_count"] for key, value in summary.items()
            if isinstance(value, dict) and isinstance(value.get("case_count"), int)
    } != contract["category_counts"]:
        raise ComparatorFailure("comparator_result_invalid")
    if not isinstance(indicators, list) or len(indicators) != contract["case_count"]:
        raise ComparatorFailure("comparator_result_invalid")
    seen: set[str] = set()
    passed_by_category: dict[str, int] = {key: 0 for key in contract["category_counts"]}
    for item in indicators:
        if (not isinstance(item, dict) or set(item) != {"id", "category", "passed"} or
                not isinstance(item.get("id"), str) or not CASE_ID.fullmatch(item["id"]) or
                item["id"] in seen or item.get("category") not in passed_by_category or
                not isinstance(item.get("passed"), bool)):
            raise ComparatorFailure("comparator_result_invalid")
        seen.add(item["id"])
        passed_by_category[item["category"]] += 1 if item["passed"] else 0
    if sum(passed_by_category.values()) != payload["passed"]:
        raise ComparatorFailure("comparator_result_invalid")
    for category, count in passed_by_category.items():
        if summary[category]["passed"] != count:
            raise ComparatorFailure("comparator_result_invalid")
    status = "verified" if payload["failed"] == 0 and payload["errors"] == 0 else "completed_with_failures"
    return payload, indicators, status


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    """SIGTERM, then a bounded wait, then SIGKILL. No orphan is left behind."""

    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
    except OSError:
        return
    try:
        process.wait(timeout=SERVER_TERM_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
        process.wait(timeout=SERVER_TERM_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def write_receipt(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ComparatorFailure("comparator_receipt_write_failed")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise ComparatorFailure("comparator_receipt_write_failed") from exc
    return receipt


def evaluate_arm(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + float(args.timeout)
    artifact = verify_arm_artifact(args.arm, Path(args.model), Path(args.scan_receipt), args.scan_receipt_sha256)
    server = Path(args.server)
    if not server.is_file():
        raise ComparatorFailure("comparator_server_binary_missing")
    revision = _run_bounded(
        ["git", "-C", os.fspath(args.llama_checkout), "rev-parse", "HEAD"],
        timeout=_stage_timeout(deadline, 30.0, "comparator_deadline_exhausted"))
    if revision.get("status") != "completed" or revision.get("stdout", "").strip() != args.llama_revision:
        raise ComparatorFailure("comparator_checkout_revision_mismatch")
    contract = fixture_contract(Path(args.fixture))
    if int(args.context) < contract["context_tokens"]:
        raise ComparatorFailure("comparator_fixture_invalid")
    token_file, token_directory = resolve_token_file(args)
    token = write_token(token_file)
    launch = server_launch_argv(args, token_file)
    process: subprocess.Popen[bytes] | None = None
    server_log: Any = None
    try:
        server_log = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(launch, stdout=server_log, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise ComparatorFailure("comparator_server_launch_failed") from exc
        ready_budget = _stage_timeout(deadline, float(args.ready_timeout), "comparator_deadline_exhausted")
        ready_ms = wait_for_health(int(args.port), token, time.monotonic() + ready_budget)
        if process.poll() is not None:
            raise ComparatorFailure("comparator_server_launch_failed")
        evaluator_budget = _stage_timeout(deadline, float(args.evaluator_timeout), "comparator_deadline_exhausted")
        evaluate = [
            sys.executable, os.fspath(args.evaluator),
            "--fixture", os.fspath(args.fixture),
            "--endpoint", f"http://127.0.0.1:{int(args.port)}/v1/chat/completions",
            "--token-file", os.fspath(token_file),
            "--transport", TRANSPORT,
            "--emit-case-indicators",
            "--timeout", str(max(1, math.floor(min(120.0, evaluator_budget)))),
            "--max-cases", str(contract["case_count"]),
        ]
        evaluation_started = time.monotonic()
        result = _run_bounded(evaluate, timeout=evaluator_budget)
        evaluation_ms = round((time.monotonic() - evaluation_started) * 1000, 1)
        metrics, indicators, status = parse_evaluator_output(result, contract)
    finally:
        _terminate(process)
        if server_log is not None:
            server_log.close()
        # The bearer outlives nothing.  Remove it as soon as the server it
        # authenticated is gone, and take the private directory with it when
        # this process created one.
        try:
            token_file.unlink()
        except OSError:
            pass
        if token_directory is not None:
            try:
                os.rmdir(token_directory)
            except OSError:
                pass
    return {
        "schema": RECEIPT_SCHEMA,
        "status": status,
        "arm": args.arm,
        "artifact": artifact,
        # Flat, required identity. The salvage transport binds every receipt it
        # publishes to the run that produced it, and an arm receipt additionally
        # has to say which artifact it scored and against which fixture -- a
        # retention number computed from an arm that ran on the wrong file or a
        # different fixture is worse than no number. These mirror
        # ``artifact.sha256`` and ``fixture.sha256`` so the binding can be
        # checked without reaching into a nested object.
        "artifact_sha256": artifact["sha256"],
        "fixture_sha256": contract["sha256"],
        **_run_identity(),
        "anchor": {"scan_receipt_sha256": args.scan_receipt_sha256},
        "fixture": contract,
        "host": {
            "kind": HOST_KIND,
            "llama_cpp_revision": args.llama_revision,
            "backend": args.backend,
            "gpu_layers": int(args.gpu_layers),
            "server_build_flags": list(args.server_build_flag),
            "server_argv": launch,
            "transport": TRANSPORT,
        },
        "settings": {
            "context_tokens": contract["context_tokens"],
            "output_reserve_tokens": contract["output_reserve_tokens"],
            "temperature": 0,
            "max_cases": contract["case_count"],
        },
        "cache_state": CACHE_STATE,
        "sample_count": contract["case_count"],
        "metrics": metrics,
        "case_indicators": indicators,
        "timings": {
            "server_ready_ms": ready_ms,
            "evaluation_ms": evaluation_ms,
            "total_ms": round((time.monotonic() - started) * 1000, 1),
        },
        "generated_at_utc": utc_now(),
        "prompt_response_logging": False,
        # ``tokens_logged``, not ``token_logging``: a field name delimited as
        # ``_token_`` is credential-shaped and makes ``validate_persisted_output``
        # reject the whole serialized receipt, so the receipt could never be
        # published.  The name was the defect, not the detector.
        "tokens_logged": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, choices=sorted(ARM_ARTIFACTS))
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--scan-receipt", required=True, type=Path)
    parser.add_argument("--scan-receipt-sha256", required=True)
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--llama-revision", required=True)
    parser.add_argument("--llama-checkout", required=True, type=Path)
    parser.add_argument("--server-build-flag", action="append", default=[],
                        help="one configure flag of the upstream server build, recorded in the receipt")
    parser.add_argument("--evaluator", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    # Optional: see ``resolve_token_file``.  A remote path here can never be
    # proved a local private handle, so the orchestrator does not supply one.
    parser.add_argument("--token-file", default=None, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--gpu-layers", type=int, default=99)
    parser.add_argument("--backend", default="cuda", choices=("cuda", "cpu"))
    parser.add_argument("--ready-timeout", type=float, default=240.0)
    parser.add_argument("--evaluator-timeout", type=float, default=480.0)
    parser.add_argument("--timeout", type=float, default=720.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1024 <= args.port <= 65535:
        args_error = "comparator_result_invalid"
        print(json.dumps({"schema": RECEIPT_SCHEMA, "status": "failed", "arm": args.arm, "error_code": args_error}, sort_keys=True))
        return 2
    if len(args.llama_revision) != 40 or any(character not in "0123456789abcdef" for character in args.llama_revision):
        print(json.dumps({"schema": RECEIPT_SCHEMA, "status": "failed", "arm": args.arm, "error_code": "comparator_checkout_revision_mismatch"}, sort_keys=True))
        return 2
    for bound in (args.ready_timeout, args.evaluator_timeout, args.timeout):
        if not math.isfinite(bound) or not 0 < bound <= 3600:
            print(json.dumps({"schema": RECEIPT_SCHEMA, "status": "failed", "arm": args.arm, "error_code": "comparator_result_invalid"}, sort_keys=True))
            return 2
    try:
        receipt = evaluate_arm(args)
    except ComparatorFailure as exc:
        failure = {"schema": RECEIPT_SCHEMA, "status": "failed", "arm": args.arm, "error_code": str(exc)}
        try:
            write_receipt(Path(args.receipt), failure)
        except ComparatorFailure:
            pass
        print(json.dumps(failure, sort_keys=True))
        return 1
    write_receipt(Path(args.receipt), receipt)
    print(json.dumps({"schema": RECEIPT_SCHEMA, "status": receipt["status"], "arm": receipt["arm"],
                      "case_count": receipt["metrics"]["case_count"], "passed": receipt["metrics"]["passed"]},
                     sort_keys=True))
    return 0 if receipt["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
