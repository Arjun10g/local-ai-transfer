#!/usr/bin/env python3
"""Run the independent contract skeleton against positive and negative fixtures.

The validators are intentionally conservative.  They check the safety shape
that consumers can rely on without taking ownership of producer
implementation details.  Producer contracts can replace fixture payloads and
extend validators through a reviewed contract-version change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any, Callable

Validator = Callable[[Any], bool]


def _exact_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _engine(value: Any) -> bool:
    return isinstance(value, dict) and value.get("api_version") == "0.1.0" and bool(value.get("routes")) and value.get("origin_policy", {}).get("allow_cors_wildcard") is False


def _events(value: Any) -> bool:
    return isinstance(value, dict) and value.get("version") == "0.1.0" and value.get("event") in {"message.started", "message.delta", "message.completed", "request.cancelled"} and isinstance(value.get("data"), dict)


def _tool(value: Any) -> bool:
    return _exact_keys(value, {"id", "name", "arguments"}) and isinstance(value["id"], str) and isinstance(value["name"], str) and isinstance(value["arguments"], dict)


def _tool_result(value: Any) -> bool:
    return _exact_keys(value, {"id", "name", "status", "content", "metadata"}) and value.get("status") in {"ok", "denied", "cancelled", "failed"} and isinstance(value.get("content"), list) and isinstance(value.get("metadata"), dict)


def _model(value: Any) -> bool:
    return isinstance(value, dict) and value.get("model") == "Qwen3.5-9B" and value.get("quantization") == "Q4_K_M" and value.get("modality") == "text"


def _config(value: Any) -> bool:
    return isinstance(value, dict) and set(value).issubset({"host", "engine", "workspace_roots", "network"}) and value.get("host", {}).get("bind", "127.0.0.1") == "127.0.0.1"


def _metrics(value: Any) -> bool:
    return isinstance(value, dict) and set(value).issubset({"request_id", "prompt_tokens", "completion_tokens", "duration_ms"}) and all(isinstance(value.get(key), (str, int)) for key in ("request_id", "prompt_tokens", "completion_tokens", "duration_ms"))


def _error(value: Any) -> bool:
    return isinstance(value, dict) and value.get("code") in {"unauthorized", "not_found", "method_not_allowed", "invalid_json", "invalid_request", "request_too_large", "not_ready", "busy", "request_cancelled", "shutdown", "internal_error"} and isinstance(value.get("http_status"), int)


def _cancel(value: Any) -> bool:
    return isinstance(value, dict) and value.get("state") in {"requested", "acknowledged", "completed"} and isinstance(value.get("request_id"), str)


def _session(value: Any) -> bool:
    return isinstance(value, dict) and value.get("event") in {"created", "reset", "deleted"} and isinstance(value.get("session_id"), str)


def _release(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        return False
    names = {str(path).lower() for path in value["files"]}
    return bool(names) and not any(name.endswith((".gguf", ".safetensors", ".pt", ".pth", ".bin")) or "node_modules" in name for name in names)


VALIDATORS: dict[str, Validator] = {
    "engine-api": _engine,
    "assistant-events": _events,
    "tool-envelope": _tool,
    "tool-result": _tool_result,
    "model-manifest": _model,
    "config-schema": _config,
    "metrics-schema": _metrics,
    "error-codes": _error,
    "cancellation": _cancel,
    "session-lifecycle": _session,
    "release-manifest": _release,
}

SCHEMA_FILES = {
    "engine-api": "contracts/engine-api/engine-api.schema.json",
    "assistant-events": "contracts/assistant-events/v0.1.0.json",
    "config-schema": "contracts/config-schema/v0.1.0.json",
    "tool-envelope": "contracts/tool-envelope/v0.1.0.json",
}
SCHEMA_FILES["tool-result"] = "contracts/tool-envelope/result-v0.1.0.json"


def schema_errors(value: Any, schema: dict[str, Any], path: str = "$", root: dict[str, Any] | None = None) -> list[str]:
    """Small dependency-free JSON Schema subset used by checked-in contracts."""
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/"):
            target: Any = root
            for part in ref[2:].split("/"):
                target = target[part]
            return schema_errors(value, target, path, root)
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}:const")
    if "not" in schema and not schema_errors(value, schema["not"], path, root):
        errors.append(f"{path}:not")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}:enum")
    types = schema.get("type")
    if types:
        allowed = set(types) if isinstance(types, list) else {types}
        actual = "null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) and not isinstance(value, bool) else "number" if isinstance(value, (int, float)) and not isinstance(value, bool) else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "unknown"
        if actual not in allowed:
            return errors + [f"{path}:type"]
    if isinstance(value, dict):
        required = schema.get("required", [])
        errors.extend(f"{path}.{key}:required" for key in required if key not in value)
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{key}:additional" for key in value if key not in properties)
        for key, child in properties.items():
            if key in value:
                errors.extend(schema_errors(value[key], child, f"{path}.{key}", root))
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append(f"{path}:maxProperties")
    elif isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}:maxItems")
        if "items" in schema:
            for index, child in enumerate(value):
                errors.extend(schema_errors(child, schema["items"], f"{path}[{index}]", root))
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}:minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}:maxLength")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path}:pattern")
    elif isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}:minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}:maximum")
    return errors


def run_fixture_file(path: Path) -> dict[str, Any]:
    contract = path.stem
    validator = VALIDATORS.get(contract)
    if validator is None:
        raise ValueError(f"unknown contract fixture: {contract}")
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict) or set(fixture) != {"positive", "negative"}:
        raise ValueError(f"{path}: expected positive and negative cases")
    positive_ok = validator(fixture["positive"])
    negative_rejected = not validator(fixture["negative"])
    schema_path = Path(__file__).parents[2] / SCHEMA_FILES[contract] if contract in SCHEMA_FILES else None
    schema_checks = None
    if schema_path and schema_path.is_file():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        positive_schema_errors = schema_errors(fixture["positive"], schema)
        negative_schema_errors = schema_errors(fixture["negative"], schema)
        positive_ok = positive_ok and not positive_schema_errors
        negative_rejected = negative_rejected or bool(negative_schema_errors)
        schema_checks = {"positive_errors": positive_schema_errors, "negative_errors": negative_schema_errors, "schema": str(schema_path)}
    return {
        "schema_version": "test-result.v1",
        "test_id": f"QA-CONFORMANCE-{contract.upper().replace('-', '_')}",
        "status": "PASS" if positive_ok and negative_rejected else "FAIL",
        "kind": "fixture",
        "duration_ms": 0,
        "details": {"contract": contract, "positive_accepted": positive_ok, "negative_rejected": negative_rejected, "schema_checks": schema_checks},
        "artifact": str(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QA contract positive/negative fixtures")
    parser.add_argument("--fixtures", default=str(Path(__file__).parents[1] / "fixtures" / "conformance"))
    parser.add_argument("--output")
    args = parser.parse_args()
    paths = sorted(Path(args.fixtures).glob("*.json"))
    expected = set(VALIDATORS)
    actual = {path.stem for path in paths}
    if actual != expected:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise SystemExit(f"fixture set mismatch; missing={missing} extra={extra}")
    results = [run_fixture_file(path) for path in paths]
    summary = {"schema_version": "conformance.v1", "kind": "fixture", "results": results, "passed": all(item["status"] == "PASS" for item in results)}
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
