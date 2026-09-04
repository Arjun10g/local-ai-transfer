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
from typing import Any, Callable

Validator = Callable[[Any], bool]


def _exact_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _engine(value: Any) -> bool:
    return isinstance(value, dict) and value.get("bind") == "127.0.0.1" and value.get("authenticated") is True


def _events(value: Any) -> bool:
    return isinstance(value, list) and value[:2] == ["message.started", "message.completed"] and len(value) >= 2


def _tool(value: Any) -> bool:
    return _exact_keys(value, {"id", "name", "arguments"}) and isinstance(value["id"], str) and isinstance(value["name"], str) and isinstance(value["arguments"], dict)


def _model(value: Any) -> bool:
    return isinstance(value, dict) and value.get("model") == "Qwen3.5-9B" and value.get("quantization") == "Q4_K_M" and value.get("modality") == "text"


def _config(value: Any) -> bool:
    return isinstance(value, dict) and set(value).issubset({"model_path", "context_tokens", "backend_profile"}) and isinstance(value.get("context_tokens"), int) and 1 <= value["context_tokens"] <= 16384


def _metrics(value: Any) -> bool:
    return isinstance(value, dict) and set(value).issubset({"request_id", "prompt_tokens", "completion_tokens", "duration_ms"}) and all(isinstance(value.get(key), (str, int)) for key in ("request_id", "prompt_tokens", "completion_tokens", "duration_ms"))


def _error(value: Any) -> bool:
    return isinstance(value, dict) and value.get("code") in {"invalid_request", "unauthorized", "cancelled", "provider_unconfigured", "network_unavailable"} and isinstance(value.get("message"), str)


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
    "model-manifest": _model,
    "config-schema": _config,
    "metrics-schema": _metrics,
    "error-codes": _error,
    "cancellation": _cancel,
    "session-lifecycle": _session,
    "release-manifest": _release,
}


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
    return {
        "schema_version": "test-result.v1",
        "test_id": f"QA-CONFORMANCE-{contract.upper().replace('-', '_')}",
        "status": "PASS" if positive_ok and negative_rejected else "FAIL",
        "kind": "fixture",
        "duration_ms": 0,
        "details": {"contract": contract, "positive_accepted": positive_ok, "negative_rejected": negative_rejected},
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
