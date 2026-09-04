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
TAIL_LIMIT = 1200
MAX_ENGINE_LINE = 8192
MAX_EVAL_OUTPUT = 256 * 1024


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


def _pin(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or set(value) - PIN_RE:
        raise ValueError(f"{label}_pin_invalid")
    return value


def verify_artifact(model: Path, manifest_path: Path, *, source_revision: str, llama_revision: str) -> dict[str, Any]:
    if model.name != MODEL_NAME or not model.is_file():
        raise ValueError("q4_artifact_missing_or_wrong_name")
    source_revision = _pin(source_revision, "source")
    llama_revision = _pin(llama_revision, "llama")
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


def _engine_build_info(engine: Path, expected_llama: str) -> dict[str, Any]:
    result = _run_bounded([os.fspath(engine), "print-build-info"], timeout=30, output_limit=MAX_ENGINE_LINE)
    if result.get("status") != "completed":
        raise ValueError("engine_build_info_failed")
    try:
        payload = json.loads(result.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("engine_build_info_invalid") from exc
    if not isinstance(payload, dict) or payload.get("llama_cpp_revision") != expected_llama or payload.get("compiled_backend") != f"llama.cpp/{expected_llama[:8]}/cpu":
        raise ValueError("engine_llama_identity_mismatch")
    return {key: payload[key] for key in ("engine_version", "api_version", "compiled_backend", "llama_cpp_revision", "model") if key in payload}


def _launch_and_evaluate(args: argparse.Namespace, artifact: dict[str, Any]) -> dict[str, Any]:
    engine = Path(args.engine)
    if not engine.is_file():
        raise ValueError("engine_binary_missing")
    checkout = Path(args.llama_checkout)
    rev = _run_bounded(["git", "-C", os.fspath(checkout), "rev-parse", "HEAD"], timeout=30, output_limit=128)
    if rev.get("status") != "completed" or rev.get("stdout", "").strip() != artifact["llama_cpp_revision"]:
        raise ValueError("llama_checkout_revision_mismatch")
    build_info = _engine_build_info(engine, artifact["llama_cpp_revision"])
    token_file = Path(args.token_file)
    _write_token(token_file)
    process: subprocess.Popen[str] | None = None
    engine_stderr: Any = None
    started = time.monotonic()
    try:
        # The protected token-file option is deliberately explicit.  Passing
        # a bearer as a command-line argument would expose it through process
        # inspection and is forbidden by the evaluation contract.
        launch = [
            os.fspath(engine), "serve", "--port", "0", "--backend", "cpu",
            "--model", args.model, "--size", str(artifact["size_bytes"]),
            "--sha256", artifact["sha256"], "--context", "2048",
            "--token-file", os.fspath(token_file),
        ]
        engine_stderr = tempfile.TemporaryFile()
        process = subprocess.Popen(launch, stdout=subprocess.PIPE, stderr=engine_stderr, text=True)
        if process.stdout is None:
            raise ValueError("engine_stdout_unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        events = selector.select(timeout=30)
        selector.close()
        if not events:
            raise ValueError("engine_ready_timeout")
        ready_line = process.stdout.readline(MAX_ENGINE_LINE)
        if not ready_line:
            raise ValueError(f"engine_not_ready:{_file_tail(engine_stderr)}")
        try:
            ready = json.loads(ready_line)
        except json.JSONDecodeError as exc:
            raise ValueError("engine_ready_receipt_invalid") from exc
        port = ready.get("port") if isinstance(ready, dict) else None
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or ready.get("bind") != "127.0.0.1":
            raise ValueError("engine_ready_identity_invalid")
        evaluate = [
            sys.executable, args.evaluator, "--fixture", args.fixture,
            "--endpoint", f"http://127.0.0.1:{port}/v1/chat/completions",
            "--token-file", os.fspath(token_file), "--timeout", "120",
            "--max-cases", "8", "--engine-pid", str(process.pid),
        ]
        result = _run_bounded(evaluate, timeout=float(args.timeout), output_limit=MAX_EVAL_OUTPUT)
        if result.get("status") != "completed":
            raise ValueError("evaluator_process_failed")
        try:
            metrics = json.loads(result.get("stdout", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("evaluator_receipt_invalid") from exc
        if not isinstance(metrics, dict) or metrics.get("case_count") != 8 or not all(isinstance(metrics.get(key), int) for key in ("passed", "failed", "errors")):
            raise ValueError("evaluator_metrics_invalid")
        return {
            "schema": "local_bmo.j1m.real-tool-eval-receipt.v1",
            "status": "verified" if metrics["errors"] == 0 and metrics["failed"] == 0 else "completed_with_failures",
            "artifact": artifact,
            "engine": build_info,
            "metrics": {key: metrics[key] for key in ("case_count", "passed", "failed", "errors", "peak_rss_kib")},
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
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--llama-revision", required=True)
    parser.add_argument("--llama-checkout", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--evaluator", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    receipt: dict[str, Any]
    try:
        if not 1 <= args.timeout <= 900:
            raise ValueError("eval_timeout_invalid")
        artifact = verify_artifact(Path(args.model), Path(args.model_manifest), source_revision=args.source_revision, llama_revision=args.llama_revision)
        receipt = _launch_and_evaluate(args, artifact)
        status = 0 if receipt["status"] == "verified" else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "failed", "error_type": type(exc).__name__, "prompt_response_logging": False, "token_logging": False}
        status = 1
    output = Path(args.receipt)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schema": receipt["schema"], "status": receipt["status"], "metrics": receipt.get("metrics")}, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
