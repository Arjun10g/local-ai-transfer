#!/usr/bin/env python3
"""Bounded local Qwen3.5 XML tool-call evaluation.

The native engine API in this release accepts messages but not an OpenAI
``tools`` field, so the pinned Qwen chat-template tool protocol is supplied in
the system message. Prompts/responses are never written to the result.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "model" / "tool_call_eval.json"
TOOL_CALL = re.compile(r"<tool_call>\s*<function=([A-Za-z0-9_.-]+)>\s*(.*?)\s*</function>\s*</tool_call>", re.DOTALL)
PARAMETER = re.compile(r"<parameter=([A-Za-z0-9_.-]+)>\s*(.*?)\s*</parameter>", re.DOTALL)
SYSTEM_PREAMBLE = """# Tools

You have access to the following functions:

<tools>
{tools}
</tools>

If you choose to call a function ONLY reply in the following format with NO suffix:

<tool_call>
<function=example_function_name>
<parameter=example_parameter_1>
value
</parameter>
</function>
</tool_call>

If there is no function call available, answer normally. Never call an unknown function. Required parameters must be supplied."""


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("schema") != "local_bmo.tool-call-eval.v1" or not isinstance(fixture.get("cases"), list):
        raise ValueError("invalid tool-call evaluation fixture")
    if len(fixture["cases"]) > int(fixture.get("limits", {}).get("max_cases", 8)):
        raise ValueError("fixture exceeds bounded case limit")
    return fixture


def parse_tool_call(text: str) -> dict[str, Any] | None:
    match = TOOL_CALL.search(text)
    if not match:
        return None
    name, body = match.groups()
    arguments: dict[str, Any] = {}
    for parameter in PARAMETER.finditer(body):
        key, raw = parameter.groups()
        value = raw.strip()
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
        arguments[key] = value
    return {"name": name, "arguments": arguments}


def evaluate_case(case: dict[str, Any], output: str) -> tuple[bool, str]:
    call = parse_tool_call(output)
    expected = case.get("expected", {})
    if any(name in output for name in expected.get("forbid_names", [])):
        return False, "forbidden_tool_name"
    if expected.get("no_call") is True:
        return call is None, "no_call" if call is None else "unexpected_call"
    wanted = expected.get("call")
    if not isinstance(wanted, dict) or call is None:
        return False, "missing_call"
    return call == wanted, "exact_call" if call == wanted else "call_mismatch"


def _post(endpoint: str, token: str, payload: dict[str, Any], timeout: float) -> str:
    session_endpoint = endpoint.removesuffix("/v1/chat/completions") + "/v1/sessions"
    session_request = urllib.request.Request(session_endpoint, data=b"{}", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(session_request, timeout=timeout) as response:
        session = json.loads(response.read().decode("utf-8"))
    if not isinstance(session.get("id"), str):
        raise ValueError("native engine returned no session id")
    payload = {**payload, "session_id": session["id"]}
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("choices", [{}])[0].get("message", {}).get("content", ""))


def _rss_kib(pid: int) -> int | None:
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2, check=True)
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run_local(
    fixture: dict[str, Any],
    endpoint: str,
    token: str,
    *,
    timeout: float,
    max_cases: int,
    engine_pid: int | None = None,
) -> dict[str, Any]:
    tools = json.dumps(fixture["tools"], separators=(",", ":"))
    cases = fixture["cases"][:max_cases]
    records = []
    peak_rss = _rss_kib(engine_pid) if engine_pid is not None else None
    for case in cases:
        payload = {"model": fixture["model"], "session_id": f"eval-{case['id']}", "messages": [{"role": "system", "content": SYSTEM_PREAMBLE.format(tools=tools)}, *case["messages"]], "stream": False, "max_tokens": int(fixture["limits"]["max_output_tokens"]), "mode": "normal"}
        started = time.monotonic()
        try:
            output = _post(endpoint, token, payload, timeout)
            passed, reason = evaluate_case(case, output)
            status = "pass" if passed else "fail"
        except urllib.error.HTTPError as exc:
            # Keep the result useful without ever emitting provider/runtime bodies.
            status, reason = "error", f"HTTP_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            status, reason = "error", type(exc).__name__
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if engine_pid is not None:
            rss = _rss_kib(engine_pid)
            if rss is not None:
                peak_rss = max(peak_rss or 0, rss)
        records.append({"id": case["id"], "category": case["category"], "status": status, "reason": reason, "latency_ms": elapsed_ms})
    return {"schema": "local_bmo.tool-call-eval-result.v1", "model": fixture["model"], "case_count": len(records), "passed": sum(item["status"] == "pass" for item in records), "failed": sum(item["status"] == "fail" for item in records), "errors": sum(item["status"] == "error" for item in records), "peak_rss_kib": peak_rss, "cases": records}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--endpoint", help="native engine endpoint, e.g. http://127.0.0.1:49912/v1/chat/completions")
    parser.add_argument("--token", help="native engine bearer token; never printed or written")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-cases", type=int, default=8)
    parser.add_argument("--engine-pid", type=int, help="optional local engine PID for bounded RSS sampling")
    parser.add_argument("--dry-run", action="store_true", help="validate fixture and print case IDs only")
    args = parser.parse_args(argv)
    fixture = load_fixture(args.fixture)
    if args.dry_run:
        print(json.dumps({"schema": "local_bmo.tool-call-eval-dry-run.v1", "model": fixture["model"], "case_ids": [case["id"] for case in fixture["cases"][:args.max_cases]], "limits": fixture["limits"]}, sort_keys=True))
        return 0
    if not args.endpoint or not args.token or len(args.token) < 16:
        parser.error("--endpoint and a non-empty >=16 character --token are required unless --dry-run")
    result = run_local(
        fixture,
        args.endpoint,
        args.token,
        timeout=args.timeout,
        max_cases=min(args.max_cases, int(fixture["limits"]["max_cases"])),
        engine_pid=args.engine_pid,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["errors"] == 0 and result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
