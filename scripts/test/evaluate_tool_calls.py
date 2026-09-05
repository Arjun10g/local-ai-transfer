#!/usr/bin/env python3
"""Bounded local Qwen3.5 XML tool-call evaluation.

The native engine renders the supplied ``tools`` field through the pinned
Qwen chat template. Prompts, responses, and bearer tokens are never written
to the result.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
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
CANARY_MESSAGE_CHARS = 2400
DIAGNOSTIC_CODES = frozenset({
    "http_400", "http_401", "http_404", "http_408", "http_409", "http_413",
    "http_415", "http_429", "http_500", "http_503", "http_other",
    "transport_url", "transport_timeout", "transport_os", "parse_json",
    "parse_session_shape", "parse_response_shape", "context_overflow",
    "endpoint", "token", "unknown",
})
QUALITY_CODES = frozenset({
    "forbidden_tool_name", "malformed_call", "unknown_tool", "malformed_parameter",
    "parameter_too_large", "invalid_json_argument", "invalid_tool_schema",
    "invalid_arguments", "missing_call", "unexpected_call", "call_mismatch",
    "quality_unknown",
})
FIXTURE_MAX_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 4096
MAX_MESSAGES_PER_CASE = 8
MAX_TOOLS = 16
MAX_EVAL_CASES = 40
MAX_TOOL_SCHEMA_BYTES = 16384
MAX_JSON_DEPTH = 8
ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
ROLE = {"user", "system"}
CATEGORIES = {"tool_selection", "argument_fidelity", "no_tool", "malformed_prompt", "prompt_injection", "schema_edge", "confirmation_sensitive", "abstention"}
FIXTURE_KEYS = {"schema", "model", "protocol", "limits", "tools", "cases"}
LIMIT_KEYS = {"context_tokens", "max_output_tokens", "temperature", "max_cases"}
TOOL_CALL = re.compile(
    r"\A\s*<tool_call>\s*<function=([a-z][a-z0-9_.-]{1,95})>"
    r"(.*?)</function>\s*</tool_call>\s*\Z",
    re.DOTALL,
)
PARAMETER = re.compile(
    r"<parameter=([a-z][a-z0-9_.-]{0,95})>(.*?)</parameter>", re.DOTALL
)
STRUCTURAL_TAG = re.compile(r"</?(?:tool_call|function(?:[=>\s]|$)|parameter(?:[=>\s]|$))")
JSON_VALUE = re.compile(r"^(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)$")


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, "redirect_rejected", headers, fp)


def _build_no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirectHandler())


def _open_url(request: urllib.request.Request, timeout: float):
    return _build_no_proxy_opener().open(request, timeout=timeout)


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
        or hostname != "127.0.0.1"
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
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("response_invalid")
    if len(data) > limit:
        raise ValueError("response_too_large")
    return data


def _exact_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError("fixture_shape")


def _bounded_json(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("fixture_json_too_deep")
    if isinstance(value, str):
        if len(value) > MAX_MESSAGE_CHARS:
            raise ValueError("fixture_string_unbounded")
    elif isinstance(value, list):
        if len(value) > MAX_EVAL_CASES:
            raise ValueError("fixture_array_unbounded")
        for item in value:
            _bounded_json(item, depth + 1)
    elif isinstance(value, dict):
        if len(value) > 32:
            raise ValueError("fixture_object_unbounded")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 96:
                raise ValueError("fixture_key_unbounded")
            _bounded_json(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("fixture_number_invalid")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("fixture_value_invalid")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _parse_json_value(text: str) -> Any:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json_number")))
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("invalid_json_argument") from exc
    _bounded_json(value)
    return value


def _bounded_int(value: Any, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("fixture_limit_invalid")


def _validate_tool_schema(tool: Any) -> None:
    if not isinstance(tool, dict):
        raise ValueError("fixture_tool_invalid")
    _exact_keys(tool, {"type", "function"})
    if tool["type"] != "function" or not isinstance(tool["function"], dict):
        raise ValueError("fixture_tool_invalid")
    function = tool["function"]
    _exact_keys(function, {"name", "description", "parameters"})
    if not isinstance(function["name"], str) or not NAME.fullmatch(function["name"]):
        raise ValueError("fixture_tool_name_invalid")
    if not isinstance(function["description"], str) or not 1 <= len(function["description"]) <= MAX_MESSAGE_CHARS:
        raise ValueError("fixture_tool_description_invalid")
    parameters = function["parameters"]
    if not isinstance(parameters, dict) or set(parameters) - {"type", "properties", "required", "additionalProperties"}:
        raise ValueError("fixture_parameters_invalid")
    if parameters.get("type") != "object" or not isinstance(parameters.get("properties"), dict) or len(parameters["properties"]) > 32:
        raise ValueError("fixture_parameters_invalid")
    if "additionalProperties" in parameters and not isinstance(parameters["additionalProperties"], bool):
        raise ValueError("fixture_parameters_invalid")
    required = parameters.get("required", [])
    if not isinstance(required, list) or len(required) > 32 or any(not isinstance(item, str) for item in required) or len(set(required)) != len(required):
        raise ValueError("fixture_required_invalid")
    for key in required:
        if not isinstance(key, str) or not ID.fullmatch(key) or key not in parameters["properties"]:
            raise ValueError("fixture_required_invalid")
    for key, schema in parameters["properties"].items():
        if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(schema, dict):
            raise ValueError("fixture_property_invalid")
        if set(schema) - {"type", "enum", "description"} or schema.get("type") not in {"string", "number", "integer", "boolean", "object", "array"}:
            raise ValueError("fixture_property_invalid")
        if "description" in schema and (not isinstance(schema["description"], str) or len(schema["description"]) > MAX_MESSAGE_CHARS):
            raise ValueError("fixture_property_invalid")
        if "enum" in schema and (not isinstance(schema["enum"], list) or len(schema["enum"]) > 16):
            raise ValueError("fixture_property_invalid")
        _bounded_json(schema)


def validate_fixture(fixture: Any) -> dict[str, Any]:
    if not isinstance(fixture, dict) or set(fixture) != FIXTURE_KEYS or fixture["schema"] != "local_bmo.tool-call-eval.v1":
        raise ValueError("invalid tool-call evaluation fixture")
    if not isinstance(fixture["model"], str) or not ID.fullmatch(fixture["model"]):
        raise ValueError("fixture_model_invalid")
    if fixture["protocol"] != "qwen35-xml-tool-call-v1" or not isinstance(fixture["limits"], dict):
        raise ValueError("fixture_protocol_invalid")
    limits = fixture["limits"]
    if set(limits) != LIMIT_KEYS:
        raise ValueError("fixture_limits_shape")
    _bounded_int(limits["context_tokens"], 1, 2048)
    _bounded_int(limits["max_output_tokens"], 1, 64)
    _bounded_int(limits["max_cases"], 1, MAX_EVAL_CASES)
    if isinstance(limits["temperature"], bool) or not isinstance(limits["temperature"], (int, float)) or not math.isfinite(limits["temperature"]) or not 0 <= limits["temperature"] <= 2:
        raise ValueError("fixture_temperature_invalid")
    tools = fixture["tools"]
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
        raise ValueError("fixture_tools_invalid")
    names = set()
    functions: dict[str, dict[str, Any]] = {}
    for tool in tools:
        _validate_tool_schema(tool)
        name = tool["function"]["name"]
        if name in names:
            raise ValueError("fixture_duplicate_tool")
        names.add(name)
        functions[name] = tool["function"]
        if len(json.dumps(tool, separators=(",", ":")).encode("utf-8")) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("fixture_tool_schema_unbounded")
    cases = fixture["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= limits["max_cases"]:
        raise ValueError("fixture_cases_invalid")
    case_ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("fixture_case_invalid")
        _exact_keys(case, {"id", "category", "messages", "expected"})
        if not isinstance(case["id"], str) or not ID.fullmatch(case["id"]) or case["id"] in case_ids:
            raise ValueError("fixture_case_id_invalid")
        case_ids.add(case["id"])
        if not isinstance(case["category"], str) or case["category"] not in CATEGORIES or not isinstance(case["messages"], list) or not 1 <= len(case["messages"]) <= MAX_MESSAGES_PER_CASE:
            raise ValueError("fixture_case_invalid")
        if not any(isinstance(message, dict) and message.get("role") == "user" for message in case["messages"]):
            raise ValueError("fixture_user_message_required")
        for message in case["messages"]:
            if not isinstance(message, dict):
                raise ValueError("fixture_message_invalid")
            _exact_keys(message, {"role", "content"})
            if not isinstance(message["role"], str) or message["role"] not in ROLE or not isinstance(message["content"], str) or not message["content"]:
                raise ValueError("fixture_message_invalid")
            if len(message["content"]) > MAX_MESSAGE_CHARS:
                raise ValueError("fixture_message_unbounded")
        expected = case["expected"]
        if not isinstance(expected, dict) or not set(expected).issubset({"call", "no_call", "forbid_names"}) or ("call" in expected) == ("no_call" in expected):
            raise ValueError("fixture_expected_invalid")
        if "no_call" in expected:
            if expected["no_call"] is not True:
                raise ValueError("fixture_expected_invalid")
        else:
            call = expected["call"]
            if not isinstance(call, dict) or set(call) != {"name", "arguments"} or not isinstance(call["name"], str) or not NAME.fullmatch(call["name"]) or call["name"] not in names or not isinstance(call["arguments"], dict):
                raise ValueError("fixture_expected_invalid")
            _validate_arguments(functions[call["name"]], call["arguments"])
        if "forbid_names" in expected:
            forbidden = expected["forbid_names"]
            if not isinstance(forbidden, list) or len(forbidden) > 32 or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in forbidden) or len(set(forbidden)) != len(forbidden):
                raise ValueError("fixture_expected_invalid")
    _bounded_json(fixture)
    return fixture


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(FIXTURE_MAX_BYTES + 1)
    except (OSError, TypeError) as exc:
        raise ValueError("fixture_unreadable") from exc
    if len(raw) > FIXTURE_MAX_BYTES:
        raise ValueError("fixture_too_large")
    try:
        fixture = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_tool-call_fixture") from exc
    return validate_fixture(fixture)


def _tool_map(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("invalid_tool_schema")
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
        if expected_type == "number" and isinstance(value, float) and not math.isfinite(value):
            raise ValueError("invalid_arguments")
        if expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError("invalid_arguments")
        if expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError("invalid_arguments")
        if expected_type == "object" and not isinstance(value, dict):
            raise ValueError("invalid_arguments")
        if expected_type == "array" and not isinstance(value, list):
            raise ValueError("invalid_arguments")
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise ValueError("invalid_arguments")


def parse_tool_call(text: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Parse one complete pinned Qwen XML call; malformed output never scores."""
    if not isinstance(text, str):
        raise ValueError("malformed_call")
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
        # Literal operators and entity-looking text are opaque argument data.
        # Only protocol-shaped tags are structural, matching the runtime.
        if key in arguments or STRUCTURAL_TAG.search(raw):
            raise ValueError("malformed_parameter")
        if raw.startswith("\r\n"):
            raw = raw[2:]
        elif raw.startswith("\n"):
            raw = raw[1:]
        if raw.endswith("\r\n"):
            raw = raw[:-2]
        elif raw.endswith("\n"):
            raw = raw[:-1]
        candidate = raw.strip()
        if len(raw.encode("utf-8")) > 4096:
            raise ValueError("parameter_too_large")
        value: Any = raw
        if JSON_VALUE.fullmatch(candidate) or candidate.startswith(("{", "[")):
            value = _parse_json_value(candidate)
        arguments[key] = value
        position = parameter.end()
    if known_tools is not None:
        _validate_arguments(known_tools[name], arguments)
    return {"name": name, "arguments": arguments}


def evaluate_case(case: dict[str, Any], output: str, tools: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
    expected = case.get("expected", {})
    # Judge attempted actions structurally. A safe refusal may name the
    # unavailable function from the user's request without attempting a call.
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


def _quality_code(reason: str) -> str:
    return reason if reason in QUALITY_CODES else "quality_unknown"


def _post(endpoint: str, token: str, payload: dict[str, Any], timeout: float, *, include_usage: bool = False) -> str | tuple[str, int]:
    validate_endpoint(endpoint)
    session_endpoint = endpoint.removesuffix("/v1/chat/completions") + "/v1/sessions"
    session_request = urllib.request.Request(
        session_endpoint,
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with _open_url(session_request, timeout) as response:
        session = json.loads(_read_response(response, RESPONSE_MAX_BYTES).decode("utf-8"))
    if not isinstance(session, dict) or set(session) != {"id", "object", "state_version"} or session.get("object") != "session" or session.get("state_version") != 1 or not isinstance(session.get("id"), str) or not SESSION_ID.fullmatch(session["id"]):
        raise ValueError("native engine returned no session id")
    payload = {**payload, "session_id": session["id"]}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with _open_url(request, timeout) as response:
        data = json.loads(_read_response(response, RESPONSE_MAX_BYTES).decode("utf-8"))
    try:
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1 or not isinstance(data["choices"][0], dict) or not isinstance(data["choices"][0].get("message"), dict):
            raise ValueError("shape")
        message = data["choices"][0]["message"]
        if set(message) != {"role", "content"} or message.get("role") != "assistant" or not isinstance(message.get("content"), str):
            raise ValueError("shape")
        content = message["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("native engine response_missing_content") from exc
    if len(content) > MODEL_OUTPUT_MAX_CHARS:
        raise ValueError("model_output_too_large")
    if not include_usage:
        return content
    usage = data.get("usage")
    if (not isinstance(usage, dict) or set(usage) != {"prompt_tokens", "completion_tokens"} or
            isinstance(usage.get("prompt_tokens"), bool) or not isinstance(usage.get("prompt_tokens"), int) or
            usage["prompt_tokens"] < 0 or isinstance(usage.get("completion_tokens"), bool) or
            not isinstance(usage.get("completion_tokens"), int) or usage["completion_tokens"] < 0):
        raise ValueError("native engine usage_shape")
    return content, usage["prompt_tokens"]


def _error_diagnostic(error: BaseException, *, http_status: int | None = None) -> str:
    """Map a request failure to a finite, secret-free diagnostic code."""
    if http_status is not None:
        return f"http_{http_status}" if http_status in {400, 401, 404, 408, 409, 413, 415, 429, 500, 503} else "http_other"
    if isinstance(error, json.JSONDecodeError):
        return "parse_json"
    if isinstance(error, TimeoutError):
        return "transport_timeout"
    if isinstance(error, urllib.error.URLError):
        return "transport_url"
    if isinstance(error, OSError):
        return "transport_os"
    if isinstance(error, ValueError):
        exact = {
            "native engine returned no session id": "parse_session_shape",
            "native engine response_missing_content": "parse_response_shape",
            "native engine usage_shape": "parse_response_shape",
            "context limit exceeded": "context_overflow",
            "context_overflow": "context_overflow",
            "endpoint_must_be_loopback_http": "endpoint",
            "token_invalid": "token",
        }.get(str(error))
        if exact is not None:
            return exact
    return "unknown"


def _diagnostics(records: list[dict[str, Any]], categories: set[str], category_summary: dict[str, dict[str, int]]) -> dict[str, Any]:
    overall: dict[str, int] = {}
    by_category: dict[str, dict[str, int]] = {category: {} for category in sorted(categories)}
    for item in records:
        if item["status"] != "error":
            continue
        code = item["reason"] if item["reason"] in DIAGNOSTIC_CODES else "unknown"
        overall[code] = overall.get(code, 0) + 1
        category_counts = by_category[item["category"]]
        category_counts[code] = category_counts.get(code, 0) + 1
    for category in categories:
        if sum(by_category[category].values()) != category_summary[category]["errors"]:
            raise ValueError("diagnostic category total mismatch")
    combined: dict[str, int] = {}
    for histogram in by_category.values():
        for code, amount in histogram.items():
            combined[code] = combined.get(code, 0) + amount
    if combined != overall:
        raise ValueError("diagnostic overall total mismatch")
    return {
        "schema": "local_bmo.tool-call-eval-diagnostics.v1",
        "total_errors": sum(overall.values()),
        "overall": dict(sorted(overall.items())),
        "by_category": by_category,
    }


def _quality_diagnostics(records: list[dict[str, Any]], categories: set[str], category_summary: dict[str, dict[str, int]]) -> dict[str, Any]:
    overall: dict[str, int] = {}
    by_category: dict[str, dict[str, int]] = {category: {} for category in sorted(categories)}
    for item in records:
        if item["status"] != "fail":
            continue
        code = _quality_code(item["reason"])
        overall[code] = overall.get(code, 0) + 1
        category_counts = by_category[item["category"]]
        category_counts[code] = category_counts.get(code, 0) + 1
    for category in categories:
        if sum(by_category[category].values()) != category_summary[category]["failed"]:
            raise ValueError("quality category total mismatch")
    combined: dict[str, int] = {}
    for histogram in by_category.values():
        for code, amount in histogram.items():
            combined[code] = combined.get(code, 0) + amount
    if combined != overall:
        raise ValueError("quality overall total mismatch")
    return {
        "schema": "local_bmo.tool-call-quality-diagnostics.v1",
        "total_failed": sum(overall.values()),
        "overall": dict(sorted(overall.items())),
        "by_category": by_category,
    }


def _canary_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    # Exercise the >512-token prefill path with every declared tool while
    # remaining below the fixture's 2048-token context budget.
    text = ("canary " + ("bounded-context ") * (CANARY_MESSAGE_CHARS // 16))[:CANARY_MESSAGE_CHARS]
    return {
        "model": fixture["model"], "messages": [{"role": "user", "content": text}],
        "tools": fixture["tools"], "stream": False,
        "max_tokens": int(fixture["limits"]["max_output_tokens"]), "mode": "normal",
    }


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
    try:
        validate_endpoint(endpoint)
        validate_fixture(fixture)
        if isinstance(max_cases, bool) or not isinstance(max_cases, int) or not 1 <= max_cases <= MAX_EVAL_CASES:
            raise ValueError("max_cases_invalid")
        if not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError("timeout_invalid")
        if engine_pid is not None and (isinstance(engine_pid, bool) or not isinstance(engine_pid, int) or engine_pid <= 0):
            raise ValueError("engine_pid_invalid")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {"schema": "local_bmo.tool-call-eval-result.v1", "model": "invalid", "case_count": 0, "passed": 0, "failed": 0, "errors": 1, "peak_rss_kib": None, "cases": []}
    cases = fixture["cases"][:max_cases]
    records: list[dict[str, Any]] = []
    peak_rss = _rss_kib(engine_pid) if engine_pid is not None else None
    canary = {"attempted": True, "passed": False, "error_code": None, "tool_count": len(fixture["tools"]), "message_chars": CANARY_MESSAGE_CHARS, "prompt_tokens": None, "context_tokens": int(fixture["limits"]["context_tokens"]), "output_reserve_tokens": int(fixture["limits"]["max_output_tokens"])}
    try:
        canary_result = _post(endpoint, token, _canary_payload(fixture), timeout, include_usage=True)
        if not isinstance(canary_result, tuple) or len(canary_result) != 2:
            raise ValueError("native engine usage_shape")
        canary["prompt_tokens"] = canary_result[1]
        if isinstance(canary_result[1], bool) or not isinstance(canary_result[1], int) or not 513 <= canary_result[1] <= canary["context_tokens"] - canary["output_reserve_tokens"]:
            raise ValueError("context_overflow")
        canary["passed"] = True
    except urllib.error.HTTPError as exc:
        canary["prompt_tokens"] = None
        canary["error_code"] = _error_diagnostic(exc, http_status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        canary["prompt_tokens"] = None
        canary["error_code"] = _error_diagnostic(exc)
    canary_failed = not canary["passed"]
    if canary_failed:
        canary_code = str(canary["error_code"] or "unknown")
        # Preserve the exact fixture totals while stopping before any scoring
        # request. The synthetic canary itself is never represented as a case.
        records = [{"id": case["id"], "category": case["category"], "status": "error", "reason": canary_code, "latency_ms": 0.0} for case in cases]
    for case in cases:
        if canary_failed:
            break
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
            if status == "fail":
                reason = _quality_code(reason)
        except urllib.error.HTTPError as exc:
            status, reason = "error", _error_diagnostic(exc, http_status=exc.code)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            status, reason = "error", _error_diagnostic(exc)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if engine_pid is not None:
            rss = _rss_kib(engine_pid)
            if rss is not None:
                peak_rss = max(peak_rss or 0, rss)
        records.append({"id": case["id"], "category": case["category"], "status": status, "reason": reason, "latency_ms": elapsed_ms})
    category_summary: dict[str, dict[str, int]] = {}
    for item in records:
        summary = category_summary.setdefault(item["category"], {"case_count": 0, "passed": 0, "failed": 0, "errors": 0})
        summary["case_count"] += 1
        result_key = {"pass": "passed", "fail": "failed", "error": "errors"}[item["status"]]
        summary[result_key] += 1
    top_counts = {
        "case_count": len(records),
        "passed": sum(item["status"] == "pass" for item in records),
        "failed": sum(item["status"] == "fail" for item in records),
        "errors": sum(item["status"] == "error" for item in records),
    }
    for field in ("case_count", "passed", "failed", "errors"):
        if sum(item[field] for item in category_summary.values()) != top_counts[field]:
            raise ValueError("category summary component mismatch")
    if not canary["passed"]:
        canary_code = canary["error_code"]
        if any(item["status"] != "error" or item["reason"] != canary_code for item in records):
            raise ValueError("canary result mismatch")
    return {
        "schema": "local_bmo.tool-call-eval-result.v1", "model": fixture["model"],
        "case_count": len(records), "passed": sum(item["status"] == "pass" for item in records),
        "failed": sum(item["status"] == "fail" for item in records), "errors": sum(item["status"] == "error" for item in records),
        "peak_rss_kib": peak_rss, "cases": records, "category_summary": category_summary,
        "canary": canary,
        "error_diagnostics": _diagnostics(records, {case["category"] for case in cases}, category_summary),
        "quality_diagnostics": _quality_diagnostics(records, {case["category"] for case in cases}, category_summary),
    }


def aggregate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return only the remote-safe metrics contract, excluding case details."""
    return {key: result[key] for key in (
        "case_count", "passed", "failed", "errors", "peak_rss_kib",
        "category_summary", "canary", "error_diagnostics",
        "quality_diagnostics",
    )}


def load_bearer_token(token_file: Path | None, token_env: str) -> str:
    """Read a protected token without accepting token material as an argument."""
    if token_file is not None and token_env in os.environ:
        raise ValueError("choose_token_file_or_env")
    if token_file is not None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = -1
        try:
            descriptor = os.open(os.fspath(token_file), flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("token_file_must_be_regular")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("token_file_permissions")
            if info.st_size > TOKEN_MAX_BYTES:
                raise ValueError("token_file_too_large")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(TOKEN_MAX_BYTES + 1)
            if len(raw) > TOKEN_MAX_BYTES:
                raise ValueError("token_file_too_large")
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("token_file_unreadable") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("token_file_must_be_regular") from exc
            raise ValueError("token_file_unreadable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
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
    parser.add_argument("--max-cases", type=int, default=MAX_EVAL_CASES)
    parser.add_argument("--engine-pid", type=int, help="optional local engine PID for bounded RSS sampling")
    parser.add_argument("--dry-run", action="store_true", help="validate fixture and print case IDs only")
    args = parser.parse_args(argv)
    if not 1 <= args.max_cases <= MAX_EVAL_CASES:
        parser.error(f"--max-cases must be between 1 and {MAX_EVAL_CASES}")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 600:
        parser.error("--timeout must be finite and between 0 and 600 seconds")
    if args.engine_pid is not None and args.engine_pid <= 0:
        parser.error("--engine-pid must be positive")
    try:
        fixture = load_fixture(args.fixture)
    except (ValueError, TypeError, OSError):
        parser.error("invalid_fixture")
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
    print(json.dumps(aggregate_result(result), sort_keys=True))
    return 0 if result["errors"] == 0 and result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
