#!/usr/bin/env python3
"""Bounded local Qwen3.5 XML tool-call evaluation.

The native engine renders the supplied ``tools`` field through the pinned
Qwen chat template. Prompts, responses, and bearer tokens are never written
to the result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "model" / "tool_call_eval.json"
TOKEN_ENV = "LAE_EVAL_TOKEN"
TOKEN_MAX_BYTES = 4096
RESPONSE_MAX_BYTES = 1024 * 1024
MODEL_OUTPUT_MAX_CHARS = 65536
TOOL_CALL = re.compile(
    r"\A\s*<tool_call>\s*<function=([a-z][a-z0-9_.-]{1,95})>"
    r"(.*?)</function>\s*</tool_call>\s*\Z",
    re.DOTALL,
)
PARAMETER = re.compile(
    r"<parameter=([a-z][a-z0-9_.-]{0,95})>(.*?)</parameter>", re.DOTALL
)


def validate_endpoint(endpoint: str) -> str:
    """Allow only the local native completion endpoint before auth is used."""
    if not isinstance(endpoint, str) or endpoint != endpoint.strip():
        raise ValueError("endpoint_must_be_loopback_http")
    try:
        parts = urllib.parse.urlsplit(endpoint)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("endpoint_must_be_loopback_http") from exc
    if (
        parts.scheme.lower() != "http"
        or hostname not in {"127.0.0.1", "localhost"}
        or parts.username is not None
        or parts.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parts.path != "/v1/chat/completions"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("endpoint_must_be_loopback_http")
    return endpoint


def _read_response(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response_too_large")
    return data


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("schema") != "local_bmo.tool-call-eval.v1" or not isinstance(fixture.get("cases"), list):
        raise ValueError("invalid tool-call evaluation fixture")
    if len(fixture["cases"]) > int(fixture.get("limits", {}).get("max_cases", 8)):
        raise ValueError("fixture exceeds bounded case limit")
    return fixture


def _tool_map(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for tool in tools:
        function = tool.get("function", {})
        if tool.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("invalid_tool_schema")
        name = function.get("name")
        if not isinstance(name, str) or name in result:
            raise ValueError("invalid_tool_schema")
        result[name] = function
    return result


def _validate_arguments(function: dict[str, Any], arguments: dict[str, Any]) -> None:
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("invalid_tool_schema")
    properties = parameters.get("properties", {})
    required = parameters.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("invalid_tool_schema")
    if any(key not in properties for key in arguments) or any(key not in arguments for key in required):
        raise ValueError("invalid_arguments")
    for key, value in arguments.items():
        schema = properties[key]
        if not isinstance(schema, dict):
            raise ValueError("invalid_tool_schema")
        expected_type = schema.get("type")
        if expected_type == "string" and not isinstance(value, str):
            raise ValueError("invalid_arguments")
        if expected_type == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError("invalid_arguments")
        if expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError("invalid_arguments")
        if expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError("invalid_arguments")
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise ValueError("invalid_arguments")


def parse_tool_call(text: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Parse one complete pinned Qwen XML call; malformed output never scores."""
    match = TOOL_CALL.fullmatch(text)
    if not match:
        if "<tool_call" in text or "</tool_call>" in text:
            raise ValueError("malformed_call")
        return None
    name, body = match.groups()
    known_tools = _tool_map(tools) if tools is not None else None
    if known_tools is not None and name not in known_tools:
        raise ValueError("unknown_tool")
    arguments: dict[str, Any] = {}
    position = 0
    while position < len(body):
        whitespace = re.match(r"\s*", body[position:])
        assert whitespace is not None
        position += len(whitespace.group(0))
        if position == len(body):
            break
        parameter = PARAMETER.match(body, position)
        if parameter is None:
            raise ValueError("malformed_parameter")
        key, raw = parameter.groups()
        if key in arguments or any(character in raw for character in "<>&"):
            raise ValueError("malformed_parameter")
        value = raw.strip()
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
        arguments[key] = value
        position = parameter.end()
    if known_tools is not None:
        _validate_arguments(known_tools[name], arguments)
    return {"name": name, "arguments": arguments}


def evaluate_case(case: dict[str, Any], output: str, tools: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
    expected = case.get("expected", {})
    if any(name in output for name in expected.get("forbid_names", [])):
        return False, "forbidden_tool_name"
    try:
        call = parse_tool_call(output, tools)
    except ValueError as exc:
        return False, str(exc)
    if expected.get("no_call") is True:
        return call is None, "no_call" if call is None else "unexpected_call"
    wanted = expected.get("call")
    if not isinstance(wanted, dict) or call is None:
        return False, "missing_call"
    return call == wanted, "exact_call" if call == wanted else "call_mismatch"


def _post(endpoint: str, token: str, payload: dict[str, Any], timeout: float) -> str:
    validate_endpoint(endpoint)
    session_endpoint = endpoint.removesuffix("/v1/chat/completions") + "/v1/sessions"
    session_request = urllib.request.Request(
        session_endpoint,
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(session_request, timeout=timeout) as response:
        session = json.loads(_read_response(response, RESPONSE_MAX_BYTES).decode("utf-8"))
    if not isinstance(session.get("id"), str):
        raise ValueError("native engine returned no session id")
    payload = {**payload, "session_id": session["id"]}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(_read_response(response, RESPONSE_MAX_BYTES).decode("utf-8"))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("native engine response_missing_content") from exc
    if not isinstance(content, str) or len(content) > MODEL_OUTPUT_MAX_CHARS:
        raise ValueError("model_output_too_large")
    return content


def _rss_kib(pid: int) -> int | None:
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2, check=True)
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run_local(
    fixture: dict[str, Any], endpoint: str, token: str, *, timeout: float,
    max_cases: int, engine_pid: int | None = None,
) -> dict[str, Any]:
    validate_endpoint(endpoint)
    cases = fixture["cases"][:max_cases]
    records = []
    peak_rss = _rss_kib(engine_pid) if engine_pid is not None else None
    for case in cases:
        payload = {
            "model": fixture["model"], "session_id": f"eval-{case['id']}",
            "messages": case["messages"], "tools": fixture["tools"],
            "stream": False, "max_tokens": int(fixture["limits"]["max_output_tokens"]), "mode": "normal",
        }
        started = time.monotonic()
        try:
            output = _post(endpoint, token, payload, timeout)
            passed, reason = evaluate_case(case, output, fixture["tools"])
            status = "pass" if passed else "fail"
        except urllib.error.HTTPError as exc:
            status, reason = "error", f"HTTP_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            status, reason = "error", type(exc).__name__
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if engine_pid is not None:
            rss = _rss_kib(engine_pid)
            if rss is not None:
                peak_rss = max(peak_rss or 0, rss)
        records.append({"id": case["id"], "category": case["category"], "status": status, "reason": reason, "latency_ms": elapsed_ms})
    return {
        "schema": "local_bmo.tool-call-eval-result.v1", "model": fixture["model"],
        "case_count": len(records), "passed": sum(item["status"] == "pass" for item in records),
        "failed": sum(item["status"] == "fail" for item in records), "errors": sum(item["status"] == "error" for item in records),
        "peak_rss_kib": peak_rss, "cases": records,
    }


def load_bearer_token(token_file: Path | None, token_env: str) -> str:
    """Read a protected token without accepting token material as an argument."""
    if token_file is not None and token_env in os.environ:
        raise ValueError("choose_token_file_or_env")
    if token_file is not None:
        try:
            info = token_file.lstat()
        except OSError as exc:
            raise ValueError("token_file_unreadable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("token_file_must_be_regular")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("token_file_permissions")
        if info.st_size > TOKEN_MAX_BYTES:
            raise ValueError("token_file_too_large")
        try:
            with token_file.open("rb") as stream:
                raw = stream.read(TOKEN_MAX_BYTES + 1)
            if len(raw) > TOKEN_MAX_BYTES:
                raise ValueError("token_file_too_large")
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("token_file_unreadable") from exc
        except OSError as exc:
            raise ValueError("token_file_unreadable") from exc
    else:
        token = os.environ.get(token_env, "").strip()
    if len(token.encode("utf-8")) > TOKEN_MAX_BYTES:
        raise ValueError("token_too_large")
    if len(token) < 16:
        raise ValueError("token_missing_or_too_short")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--endpoint", help="native engine endpoint, e.g. http://127.0.0.1:49912/v1/chat/completions")
    parser.add_argument("--token-file", type=Path, help="protected regular file containing the native bearer token")
    parser.add_argument("--token-env", default=TOKEN_ENV, help="inherited environment variable name (presence only)")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-cases", type=int, default=8)
    parser.add_argument("--engine-pid", type=int, help="optional local engine PID for bounded RSS sampling")
    parser.add_argument("--dry-run", action="store_true", help="validate fixture and print case IDs only")
    args = parser.parse_args(argv)
    fixture = load_fixture(args.fixture)
    if args.dry_run:
        print(json.dumps({"schema": "local_bmo.tool-call-eval-dry-run.v1", "model": fixture["model"], "case_ids": [case["id"] for case in fixture["cases"][:args.max_cases]], "limits": fixture["limits"]}, sort_keys=True))
        return 0
    if not args.endpoint:
        parser.error("--endpoint is required unless --dry-run")
    try:
        endpoint = validate_endpoint(args.endpoint)
        token = load_bearer_token(args.token_file, args.token_env)
    except ValueError as exc:
        parser.error(str(exc))
    result = run_local(fixture, endpoint, token, timeout=args.timeout, max_cases=min(args.max_cases, int(fixture["limits"]["max_cases"])), engine_pid=args.engine_pid)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["errors"] == 0 and result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
