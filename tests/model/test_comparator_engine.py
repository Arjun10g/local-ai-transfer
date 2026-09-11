#!/usr/bin/env python3
"""COMPARATOR-ENGINE-001: the upstream transport, the arm driver, the phase.

Everything here is offline: no provider, no model, no network beyond a
loopback ``http.server`` fixture bound to 127.0.0.1, no credential, no spend.
The suite pins the five things the slice promises:

* the default product-engine transport is byte-identical to the pre-change
  source, request body and wire bytes alike;
* the upstream transport puts exactly the reviewed request on the wire, over
  loopback, with the bearer in a header and never in a log or a receipt;
* an upstream answer scores identically whether it arrives as the pinned
  template's XML or as structured ``tool_calls``;
* every transport and driver failure is a typed code from a closed set;
* the comparator plan, uploads and budgets appear only when the flag is set.
"""

import hashlib
import io
import json
import contextlib
import importlib.util
import os
import re
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts.test import evaluate_tool_calls as evaluator
from scripts.test import remote_comparator_eval as driver
from scripts.test.evaluate_tool_calls import load_fixture, run_local

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_FIXTURE = ROOT / "tests" / "model" / "production_tool_call_eval.json"
TOKEN = "comparator-test-token-20260911"
XML_CALL = "<tool_call><function=system.get_info></function></tool_call>"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingHandler(BaseHTTPRequestHandler):
    """Records every request and answers from the server's scripted queue."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # never write request lines to stderr
        return

    def _respond(self, status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append({
            "method": "GET", "path": self.path,
            "headers": dict(self.headers), "body": b"",
        })
        status, payload = self.server.next_response(self.path)
        self._respond(status, payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append({
            "method": "POST", "path": self.path,
            "headers": dict(self.headers), "body": body,
        })
        status, payload = self.server.next_response(self.path)
        self._respond(status, payload)


class LoopbackServer:
    """A bounded loopback HTTP fixture. Binds 127.0.0.1 and nothing else."""

    def __init__(self, responses):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.httpd.requests = []
        self.httpd.responses = list(responses)
        self.httpd.next_response = self._next
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _next(self, _path):
        if len(self.httpd.responses) > 1:
            return self.httpd.responses.pop(0)
        return self.httpd.responses[0]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def port(self):
        return self.httpd.server_address[1]

    @property
    def requests(self):
        return self.httpd.requests


def completion(content, tool_calls=None, prompt_tokens=700):
    message = {"role": "assistant", "content": content, "reasoning_content": ""}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "model": "test",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 12,
                  "total_tokens": prompt_tokens + 12},
    }


class DefaultTransportIdentityTests(unittest.TestCase):
    """The Q4 acceptance path must not have moved by one byte."""

    def test_product_case_body_matches_the_pre_transport_construction(self):
        # Both the default fixture and the 37-case production profile the
        # comparator arms actually score.
        for fixture in (load_fixture(), load_fixture(PRODUCTION_FIXTURE)):
          for case in fixture["cases"]:
            # Literal copy of the request body the pre-change run_local built.
            expected = {
                "model": fixture["model"], "session_id": f"eval-{case['id']}",
                "messages": case["messages"], "tools": fixture["tools"],
                "stream": False, "max_tokens": int(fixture["limits"]["max_output_tokens"]),
                "mode": "normal",
            }
            built = evaluator._case_payload(fixture, case, evaluator.TRANSPORT_PRODUCT_ENGINE)
            self.assertEqual(built, expected, case["id"])
            self.assertEqual(json.dumps(built), json.dumps(expected), case["id"])

    def test_product_canary_body_matches_the_pre_transport_construction(self):
        for fixture in (load_fixture(), load_fixture(PRODUCTION_FIXTURE)):
            self.assertEqual(json.dumps(evaluator._canary_payload(fixture)),
                             json.dumps(self.canary_literal(fixture)))

    @staticmethod
    def canary_literal(fixture):
        text = ("canary " + ("bounded-context ") * (evaluator.CANARY_MESSAGE_CHARS // 16))[:evaluator.CANARY_MESSAGE_CHARS]
        return {
            "model": fixture["model"], "messages": [{"role": "user", "content": text}],
            "tools": fixture["tools"], "stream": False,
            "max_tokens": int(fixture["limits"]["max_output_tokens"]), "mode": "normal",
        }

    def test_product_transport_still_performs_the_session_handshake(self):
        session = (200, {"id": "session-abcdefgh", "object": "session", "state_version": 1})
        with LoopbackServer([session, (200, {
                "choices": [{"message": {"role": "assistant", "content": XML_CALL}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 3}})]) as server:
            endpoint = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            content, prompt_tokens = evaluator._post(endpoint, TOKEN, {"model": "m"}, 5.0, include_usage=True)
        self.assertEqual((content, prompt_tokens), (XML_CALL, 700))
        self.assertEqual([item["path"] for item in server.requests],
                         ["/v1/sessions", "/v1/chat/completions"])
        self.assertEqual(json.loads(server.requests[1]["body"]),
                         {"model": "m", "session_id": "session-abcdefgh"})

    def test_run_local_defaults_to_the_product_transport(self):
        fixture = load_fixture()
        with mock.patch.object(evaluator, "_post", side_effect=lambda *a, include_usage=False, **k: (XML_CALL, 700) if include_usage else XML_CALL) as post:
            result = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", TOKEN, timeout=0.1, max_cases=1)
        self.assertEqual((result["passed"], result["errors"]), (1, 0))
        self.assertIn("session_id", post.call_args.args[2])
        self.assertNotIn("temperature", post.call_args.args[2])


class UpstreamRequestShapeTests(unittest.TestCase):
    def test_request_url_headers_and_body_are_exactly_the_reviewed_shape(self):
        fixture = load_fixture()
        case = fixture["cases"][0]
        with LoopbackServer([(200, completion(XML_CALL))]) as server:
            endpoint = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            payload = evaluator._case_payload(fixture, case, evaluator.TRANSPORT_UPSTREAM_OPENAI)
            outcome = evaluator._post_upstream(endpoint, TOKEN, payload, 5.0)
        self.assertEqual(outcome["content"], XML_CALL)
        self.assertIsNone(outcome["tool_calls"])
        request = server.requests[0]
        # One request, no session handshake, loopback path only.
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["headers"]["Host"], f"127.0.0.1:{server.port}")
        body = json.loads(request["body"])
        self.assertEqual(set(body), {
            "model", "messages", "tools", "stream", "max_tokens",
            "temperature", "cache_prompt", "chat_template_kwargs"})
        self.assertEqual(body["tools"], fixture["tools"])
        self.assertEqual(body["temperature"], 0)
        self.assertIs(body["stream"], False)
        self.assertIs(body["cache_prompt"], False)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["max_tokens"], fixture["limits"]["max_output_tokens"])
        # Product-private fields must not reach the upstream server.
        self.assertNotIn("session_id", body)
        self.assertNotIn("mode", body)

    def test_the_policy_message_is_the_engine_constant_verbatim(self):
        source = (ROOT / "native/backend/llama_chat_template.cpp").read_text()
        literal = re.search(r"kSchemaAbstentionPolicy\[\]\s*=\s*((?:\s*\"(?:[^\"\\\\]|\\\\.)*\")+)\s*;", source)
        self.assertIsNotNone(literal)
        rendered = "".join(re.findall(r'"((?:[^"\\\\]|\\\\.)*)"', literal.group(1)))
        self.assertEqual(rendered, evaluator.SCHEMA_ABSTENTION_POLICY)

    def test_the_policy_merge_matches_the_engine_branching(self):
        fixture = load_fixture()
        tools = fixture["tools"]
        # No tools: the history is untouched, exactly as the engine returns it.
        history = [{"role": "user", "content": "hello"}]
        self.assertEqual(evaluator.apply_schema_abstention_policy(history, []), history)
        self.assertEqual(evaluator.apply_schema_abstention_policy(history, None), history)
        # Tools, no system message: the policy is inserted as one.
        inserted = evaluator.apply_schema_abstention_policy(history, tools)
        self.assertEqual(inserted[0], {"role": "system", "content": evaluator.SCHEMA_ABSTENTION_POLICY})
        self.assertEqual(inserted[1:], history)
        # Tools, leading system message: the policy is prepended to it.
        seeded = [{"role": "system", "content": "house rules"}, {"role": "user", "content": "hi"}]
        merged = evaluator.apply_schema_abstention_policy(seeded, tools)
        self.assertEqual(merged[0]["content"], f"{evaluator.SCHEMA_ABSTENTION_POLICY}\n\nhouse rules")
        self.assertEqual(len(merged), 2)
        # The caller's own list is never mutated.
        self.assertEqual(seeded[0]["content"], "house rules")

    def test_every_fixture_case_renders_the_policy_exactly_once(self):
        merged = inserted = 0
        for fixture in (load_fixture(), load_fixture(PRODUCTION_FIXTURE)):
            for case in fixture["cases"]:
                body = evaluator._case_payload(fixture, case, evaluator.TRANSPORT_UPSTREAM_OPENAI)
                systems = [item for item in body["messages"] if item["role"] == "system"]
                self.assertEqual(len(systems), 1, case["id"])
                self.assertTrue(systems[0]["content"].startswith(evaluator.SCHEMA_ABSTENTION_POLICY), case["id"])
                if case["messages"] and case["messages"][0]["role"] == "system":
                    # Merged into the case's own system message, never duplicated.
                    merged += 1
                    self.assertEqual(len(body["messages"]), len(case["messages"]), case["id"])
                    self.assertTrue(systems[0]["content"].endswith(case["messages"][0]["content"]), case["id"])
                    self.assertEqual(body["messages"][1:], case["messages"][1:], case["id"])
                else:
                    inserted += 1
                    self.assertEqual(len(body["messages"]), len(case["messages"]) + 1, case["id"])
                    self.assertEqual(body["messages"][1:], case["messages"], case["id"])
        # Between them the two shipped fixtures exercise both branches of the
        # port. The 37-case production profile is all-user-first today, so the
        # merge branch is covered only by the default fixture.
        self.assertGreater(merged, 0)
        self.assertGreater(inserted, 0)


class UpstreamNormalizationTests(unittest.TestCase):
    def test_structured_tool_calls_normalize_to_the_xml_parser_shape(self):
        fixture = load_fixture()
        tools = fixture["tools"]
        structured = [{"id": "call_1", "type": "function", "function": {
            "name": "system.get_info", "arguments": "{}"}}]
        self.assertEqual(evaluator.normalize_structured_tool_calls(structured, tools),
                         evaluator.parse_tool_call(XML_CALL, tools))

    def test_structured_and_xml_answers_score_identically(self):
        fixture = load_fixture()
        case = next(item for item in fixture["cases"] if item["expected"].get("call"))
        wanted = case["expected"]["call"]
        xml_parameters = "".join(
            f"<parameter={key}>{value if isinstance(value, str) else json.dumps(value)}</parameter>"
            for key, value in wanted["arguments"].items())
        xml = f"<tool_call><function={wanted['name']}>{xml_parameters}</function></tool_call>"
        structured = [{"type": "function", "function": {
            "name": wanted["name"], "arguments": json.dumps(wanted["arguments"])}}]
        self.assertEqual(evaluator.evaluate_case(case, xml, fixture["tools"]), (True, "exact_call"))
        self.assertEqual(evaluator.evaluate_structured_case(case, structured, fixture["tools"]),
                         (True, "exact_call"))

    def test_a_malformed_structured_call_is_a_quality_failure_not_an_error(self):
        fixture = load_fixture()
        case = fixture["cases"][0]
        for structured, reason in (
            ([{"function": {"name": "not.a.real.tool", "arguments": "{}"}}], "unknown_tool"),
            ([{"function": {"name": "system.get_info", "arguments": "{oops"}}], "invalid_json_argument"),
            ([{"function": {"name": "system.get_info", "arguments": "[]"}}], "invalid_json_argument"),
            ([{"function": {"name": "SYSTEM.GET_INFO", "arguments": "{}"}}], "malformed_call"),
            ([], "malformed_call"),
            ([{"function": {"name": "system.get_info"}}, {"function": {"name": "time.now"}}], "malformed_call"),
        ):
            passed, code = evaluator.evaluate_structured_case(case, structured, fixture["tools"])
            self.assertFalse(passed, structured)
            self.assertEqual(code, reason, structured)
            self.assertIn(evaluator._quality_code(code), evaluator.QUALITY_CODES)

    def test_structured_arguments_are_bounded_like_xml_parameters(self):
        fixture = load_fixture()
        oversize = [{"function": {"name": "fs.read_text", "arguments": json.dumps(
            {"path": "x" * (evaluator.MAX_STRUCTURED_ARGUMENT_BYTES + 1)})}}]
        with self.assertRaises(ValueError) as raised:
            evaluator.normalize_structured_tool_calls(oversize, fixture["tools"])
        self.assertEqual(str(raised.exception), "parameter_too_large")

    def test_a_null_content_with_structured_calls_is_scored_not_refused(self):
        fixture = load_fixture()
        structured = [{"type": "function", "function": {"name": "system.get_info", "arguments": "{}"}}]
        with LoopbackServer([(200, completion(None, structured))]) as server:
            endpoint = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            outcome = evaluator._post_upstream(endpoint, TOKEN, {"tools": fixture["tools"]}, 5.0)
        self.assertEqual(outcome["content"], "")
        self.assertEqual(outcome["tool_calls"], structured)

    def test_reasoning_content_and_total_tokens_do_not_break_the_contract(self):
        with LoopbackServer([(200, completion(XML_CALL))]) as server:
            endpoint = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            outcome = evaluator._post_upstream(endpoint, TOKEN, {}, 5.0, include_usage=True)
        self.assertEqual(outcome["prompt_tokens"], 700)


class UpstreamFailureTests(unittest.TestCase):
    def diagnose(self, responses, *, include_usage=False):
        with LoopbackServer(responses) as server:
            endpoint = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            try:
                evaluator._post_upstream(endpoint, TOKEN, {}, 5.0, include_usage=include_usage)
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                return evaluator._error_diagnostic(exc, http_status=code)
            except Exception as exc:  # noqa: BLE001 - the diagnostic is the assertion
                return evaluator._error_diagnostic(exc)
        return None

    def test_http_status_and_body_failures_map_to_typed_codes(self):
        self.assertEqual(self.diagnose([(503, {"error": "loading"})]), "http_503")
        self.assertEqual(self.diagnose([(401, {"error": "unauthorized"})]), "http_401")
        self.assertEqual(self.diagnose([(418, {"error": "teapot"})]), "http_other")
        self.assertEqual(self.diagnose([(200, b"{not json")]), "parse_json")
        self.assertEqual(self.diagnose([(200, {"choices": []})]), "parse_response_shape")
        self.assertEqual(self.diagnose([(200, completion(XML_CALL, prompt_tokens=-1))],
                                       include_usage=True), "parse_response_shape")
        for code in ("http_503", "http_401", "http_other", "parse_json", "parse_response_shape"):
            self.assertIn(code, evaluator.DIAGNOSTIC_CODES)

    def test_a_timeout_is_a_typed_transport_failure(self):
        with mock.patch.object(evaluator, "_open_url", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError) as raised:
                evaluator._post_upstream("http://127.0.0.1:1/v1/chat/completions", TOKEN, {}, 0.01)
        self.assertEqual(evaluator._error_diagnostic(raised.exception), "transport_timeout")

    def test_the_upstream_transport_refuses_a_non_loopback_endpoint(self):
        for bad in ("http://127.0.0.2:8080/v1/chat/completions",
                    "https://127.0.0.1:8080/v1/chat/completions",
                    "http://127.0.0.1:8080/health"):
            with self.assertRaises(ValueError) as raised:
                evaluator._post_upstream(bad, TOKEN, {}, 1.0)
            self.assertEqual(evaluator._error_diagnostic(raised.exception), "endpoint")

    def test_an_unknown_transport_name_is_refused(self):
        with self.assertRaises(ValueError):
            evaluator._transport("upstream-anything")
        result = run_local(load_fixture(), "http://127.0.0.1:1/v1/chat/completions", TOKEN,
                           timeout=0.1, max_cases=1, transport="upstream-anything")
        self.assertEqual((result["case_count"], result["errors"]), (0, 1))


class UpstreamEndToEndTests(unittest.TestCase):
    def test_a_whole_run_over_the_loopback_fixture_scores_and_hides_the_token(self):
        fixture = load_fixture()
        with LoopbackServer([(200, completion(XML_CALL))]) as server:
            endpoint = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = run_local(fixture, endpoint, TOKEN, timeout=5.0, max_cases=2,
                                   transport=evaluator.TRANSPORT_UPSTREAM_OPENAI)
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["errors"], 0)
        self.assertTrue(result["canary"]["passed"])
        indicators = evaluator.case_indicators(result)
        self.assertEqual(len(indicators), 2)
        self.assertEqual(set(indicators[0]), {"id", "category", "passed"})
        # Neither the aggregate, the indicators nor stdout may carry the token
        # or any prompt or response text.
        blob = json.dumps([evaluator.aggregate_result(result), indicators]) + stdout.getvalue()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(XML_CALL, blob)
        self.assertNotIn(fixture["cases"][0]["messages"][0]["content"], blob)

    def test_the_cli_emits_indicators_only_when_asked(self):
        fixture_path = ROOT / "tests" / "model" / "production_tool_call_eval.json"
        with LoopbackServer([(200, completion(XML_CALL))]) as server, \
                tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(TOKEN)
            token_file.chmod(0o600)
            base = ["--fixture", str(fixture_path), "--endpoint",
                    f"http://127.0.0.1:{server.port}/v1/chat/completions",
                    "--token-file", str(token_file), "--transport", "upstream-openai",
                    "--max-cases", "1", "--timeout", "10"]
            plain, marked = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(plain):
                evaluator.main(base)
            with contextlib.redirect_stdout(marked):
                evaluator.main(base + ["--emit-case-indicators"])
        without = json.loads(plain.getvalue())
        with_vector = json.loads(marked.getvalue())
        self.assertNotIn("case_indicators", without)
        self.assertEqual(set(with_vector) - set(without), {"case_indicators"})
        self.assertNotIn(TOKEN, plain.getvalue() + marked.getvalue())


class ServerLaunchTests(unittest.TestCase):
    def argv(self, **overrides):
        import argparse

        args = argparse.Namespace(
            server=Path("/scratch/llama-server-build/bin/llama-server"),
            model=Path("/scratch/j1m/artifacts/Qwen3.5-9B-Q8_0.gguf"),
            port=18082, context=8192, gpu_layers=99)
        for key, value in overrides.items():
            setattr(args, key, value)
        return driver.server_launch_argv(args, Path("/scratch/j1m/comparator-token-q8_0"))

    def test_launch_argv_is_exact_loopback_only_and_carries_no_bearer(self):
        self.assertEqual(self.argv(), [
            "/scratch/llama-server-build/bin/llama-server",
            "--model", "/scratch/j1m/artifacts/Qwen3.5-9B-Q8_0.gguf",
            "--host", "127.0.0.1",
            "--port", "18082",
            "--api-key-file", "/scratch/j1m/comparator-token-q8_0",
            "--ctx-size", "8192",
            "--n-gpu-layers", "99",
            "--parallel", "1",
            "--threads", "1",
            "--jinja",
            "--temp", "0",
            "--no-webui",
        ])

    def test_no_argument_is_a_bearer_and_the_bind_is_loopback(self):
        argv = self.argv()
        self.assertIn("--host", argv)
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertNotIn("--api-key", argv)
        self.assertTrue(all("0.0.0.0" not in part and "::" not in part for part in argv))

    def test_jinja_is_mandatory_because_tools_reach_the_model_through_it(self):
        # Without --jinja the upstream server drops the tools field entirely,
        # so the arm would score a prompt with no catalog in it.
        self.assertIn("--jinja", self.argv())

    def test_the_token_file_is_created_private_and_never_returned_in_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            token = driver.write_token(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(path.read_text(), token)
            with self.assertRaises(driver.ComparatorFailure):
                driver.write_token(path)


class HealthCheckTests(unittest.TestCase):
    def test_health_becomes_ready_after_a_loading_answer(self):
        with LoopbackServer([(503, {"status": "loading model"}), (200, {"status": "ok"})]) as server:
            elapsed = driver.wait_for_health(server.port, TOKEN, __import__("time").monotonic() + 20)
        self.assertGreaterEqual(elapsed, 0.0)
        self.assertTrue(all(item["path"] == "/health" for item in server.requests))
        self.assertEqual(server.requests[0]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_health_that_never_becomes_ready_is_a_typed_timeout(self):
        with LoopbackServer([(200, {"status": "loading model"})]) as server:
            with self.assertRaises(driver.ComparatorFailure) as raised:
                driver.wait_for_health(server.port, TOKEN, __import__("time").monotonic() + 1.5)
        self.assertEqual(str(raised.exception), "comparator_server_ready_timeout")

    def test_a_closed_port_is_a_typed_timeout_not_a_crash(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(driver.ComparatorFailure) as raised:
            driver.wait_for_health(port, TOKEN, __import__("time").monotonic() + 1.0)
        self.assertEqual(str(raised.exception), "comparator_server_ready_timeout")


class ArmIdentityTests(unittest.TestCase):
    def setUp(self):
        self.receipt = ROOT / "artifacts" / "qwen35-9b" / "scan-receipt.json"
        self.anchor = hashlib.sha256(self.receipt.read_bytes()).hexdigest()

    def test_the_anchor_is_the_checked_in_scan_receipt(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "comparator_engine_anchor")
        self.assertEqual(self.anchor, orchestrator._APPROVED_SCAN_RECEIPT_SHA256)
        self.assertEqual(set(driver.ARM_ARTIFACTS), {"q4_k_m", "q8_0", "bf16"})
        recorded = {item["name"] for item in json.loads(self.receipt.read_text())["artifacts"]}
        self.assertEqual({name for name, _ in driver.ARM_ARTIFACTS.values()}, recorded)

    def test_a_substituted_scan_receipt_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            forged = Path(directory) / "scan-receipt.json"
            forged.write_text(json.dumps({"artifacts": [
                {"name": "Qwen3.5-9B-Q8_0.gguf", "sha256": "0" * 64, "size_bytes": 4}]}))
            model = Path(directory) / "Qwen3.5-9B-Q8_0.gguf"
            model.write_bytes(b"fake")
            with self.assertRaises(driver.ComparatorFailure) as raised:
                driver.verify_arm_artifact("q8_0", model, forged, self.anchor)
            self.assertEqual(str(raised.exception), "comparator_anchor_mismatch")

    def test_an_artifact_that_does_not_rehash_to_the_anchor_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "Qwen3.5-9B-Q8_0.gguf"
            model.write_bytes(b"not the real weights")
            with self.assertRaises(driver.ComparatorFailure) as raised:
                driver.verify_arm_artifact("q8_0", model, self.receipt, self.anchor)
            self.assertEqual(str(raised.exception), "comparator_artifact_identity_mismatch")
            wrong_name = Path(directory) / "Qwen3.5-9B-Q5_0.gguf"
            wrong_name.write_bytes(b"x")
            with self.assertRaises(driver.ComparatorFailure) as raised:
                driver.verify_arm_artifact("q8_0", wrong_name, self.receipt, self.anchor)
            self.assertEqual(str(raised.exception), "comparator_artifact_identity_mismatch")

    def test_an_identity_that_matches_the_anchor_is_accepted(self):
        payload = b'{"artifacts": [{"name": "Qwen3.5-9B-Q8_0.gguf", "sha256": "%s", "size_bytes": 5}]}'
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "Qwen3.5-9B-Q8_0.gguf"
            model.write_bytes(b"bytes")
            digest = hashlib.sha256(b"bytes").hexdigest().encode()
            receipt = Path(directory) / "anchor.json"
            receipt.write_bytes(payload % digest)
            anchor = hashlib.sha256(receipt.read_bytes()).hexdigest()
            self.assertEqual(driver.verify_arm_artifact("q8_0", model, receipt, anchor), {
                "name": "Qwen3.5-9B-Q8_0.gguf", "size_bytes": 5,
                "sha256": digest.decode(), "quantization": "Q8_0"})

    def test_an_unknown_arm_is_refused(self):
        with self.assertRaises(driver.ComparatorFailure) as raised:
            driver.verify_arm_artifact("q5_k_m", Path("/nonexistent"), self.receipt, self.anchor)
        self.assertEqual(str(raised.exception), "comparator_arm_unknown")

    def test_every_typed_failure_is_in_the_closed_vocabulary(self):
        self.assertEqual(str(driver.ComparatorFailure("not_a_real_code")), "comparator_result_invalid")
        for code in driver.ERROR_CODES:
            self.assertEqual(str(driver.ComparatorFailure(code)), code)


class ArmResultTests(unittest.TestCase):
    def setUp(self):
        self.contract = driver.fixture_contract(ROOT / "tests/model/production_tool_call_eval.json")

    def test_the_fixture_contract_is_the_pinned_production_profile(self):
        self.assertEqual(self.contract["sha256"],
                         "c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c")
        self.assertEqual(self.contract["case_count"], 37)
        self.assertEqual(self.contract["tool_count"], 33)
        self.assertEqual(self.contract["context_tokens"], 8192)
        self.assertEqual(self.contract["output_reserve_tokens"], 256)
        self.assertEqual(self.contract["category_counts"], {
            "abstention": 1, "confirmation_sensitive": 15, "no_tool": 1,
            "prompt_injection": 1, "schema_edge": 1, "tool_selection": 18})

    def aggregate(self, *, passed, failed=0, errors=0):
        counts = dict(self.contract["category_counts"])
        summary, indicators, remaining = {}, [], passed
        for category, count in counts.items():
            hits = min(count, remaining)
            remaining -= hits
            summary[category] = {"case_count": count, "passed": hits,
                                 "failed": count - hits, "errors": 0}
            indicators.extend({"id": f"{category}-{index}", "category": category,
                               "passed": index < hits} for index in range(count))
        payload = {
            "case_count": self.contract["case_count"], "passed": passed,
            "failed": self.contract["case_count"] - passed, "errors": errors,
            "peak_rss_kib": None, "category_summary": summary,
            "canary": {}, "error_diagnostics": {}, "quality_diagnostics": {},
            "case_indicators": indicators,
        }
        return {"status": "completed", "exit_code": 0 if passed == self.contract["case_count"] else 1,
                "stdout": json.dumps(payload)}

    def test_a_complete_aggregate_and_indicator_vector_is_accepted(self):
        metrics, indicators, status = driver.parse_evaluator_output(
            self.aggregate(passed=37), self.contract)
        self.assertEqual(status, "verified")
        self.assertEqual(metrics["passed"], 37)
        self.assertEqual(len(indicators), 37)
        self.assertNotIn("case_indicators", metrics)
        partial = driver.parse_evaluator_output(self.aggregate(passed=30), self.contract)
        self.assertEqual(partial[2], "completed_with_failures")

    def test_an_indicator_vector_that_disagrees_with_the_totals_is_refused(self):
        result = self.aggregate(passed=30)
        payload = json.loads(result["stdout"])
        payload["case_indicators"][0]["passed"] = not payload["case_indicators"][0]["passed"]
        result["stdout"] = json.dumps(payload)
        with self.assertRaises(driver.ComparatorFailure) as raised:
            driver.parse_evaluator_output(result, self.contract)
        self.assertEqual(str(raised.exception), "comparator_result_invalid")

    def test_a_missing_vector_short_output_or_crash_is_typed(self):
        result = self.aggregate(passed=37)
        payload = json.loads(result["stdout"])
        payload.pop("case_indicators")
        with self.assertRaises(driver.ComparatorFailure):
            driver.parse_evaluator_output({**result, "stdout": json.dumps(payload)}, self.contract)
        with self.assertRaises(driver.ComparatorFailure) as raised:
            driver.parse_evaluator_output({"status": "timeout", "exit_code": None, "stdout": ""}, self.contract)
        self.assertEqual(str(raised.exception), "comparator_evaluator_failed")
        with self.assertRaises(driver.ComparatorFailure) as raised:
            driver.parse_evaluator_output({"status": "completed", "exit_code": 0, "stdout": "{oops"}, self.contract)
        self.assertEqual(str(raised.exception), "comparator_result_invalid")
        with self.assertRaises(driver.ComparatorFailure) as raised:
            driver.parse_evaluator_output({"status": "completed", "exit_code": 2, "stdout": "{}"}, self.contract)
        self.assertEqual(str(raised.exception), "comparator_evaluator_failed")

    def test_an_arm_receipt_is_readable_by_the_comparison_module(self):
        from scripts.test import compare_model_quality as comparison

        metrics, indicators, status = driver.parse_evaluator_output(
            self.aggregate(passed=30), self.contract)
        receipt = {"schema": driver.RECEIPT_SCHEMA, "status": status, "arm": "q8_0",
                   "metrics": metrics, "case_indicators": indicators}
        loaded = comparison.arm_metrics_from_receipt(receipt)
        self.assertEqual(loaded["passed"], 30)
        self.assertEqual(len(loaded["case_indicators"]), 37)


class ComparatorPlanTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "comparator_engine_plan")
        self.runner = load(ROOT / "scripts/j1m_runner.py", "comparator_engine_runner")
        self.config = self.runner.load_config()

    def test_no_comparator_stage_or_upload_exists_without_the_flag(self):
        self.assertEqual(self.orchestrator._comparator_remote_commands(self.config, "/scratch/j1m", ()), [])
        commands = self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        self.assertEqual(commands, self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m", ()))
        self.assertNotIn("--retain-comparators", [part for command in commands for part in command])
        manifest = ROOT / "artifacts/qwen35-9b/model-manifest.json"
        plain = self.orchestrator._eval_uploads(self.config, "/scratch/j1m", None, manifest)
        self.assertEqual(plain, self.orchestrator._eval_uploads(self.config, "/scratch/j1m", None, manifest, ()))
        self.assertEqual(len(plain), 15)

    def test_the_flag_adds_exactly_the_retention_switch_and_two_uploads(self):
        base = self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        with_flag = self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m", ("q8_0",))
        self.assertEqual(len(base), len(with_flag))
        differing = [index for index, pair in enumerate(zip(base, with_flag)) if pair[0] != pair[1]]
        self.assertEqual(len(differing), 1)
        self.assertEqual(with_flag[differing[0]], base[differing[0]] + ["--retain-comparators"])
        self.assertIn("j1m_runner.py", with_flag[differing[0]][1])
        manifest = ROOT / "artifacts/qwen35-9b/model-manifest.json"
        extra = self.orchestrator._eval_uploads(self.config, "/scratch/j1m", None, manifest, ("q8_0",))
        self.assertEqual(extra[:15], self.orchestrator._eval_uploads(self.config, "/scratch/j1m", None, manifest))
        self.assertEqual([remote for _, remote, _ in extra[15:]], [
            "/scratch/j1m/remote_comparator_eval.py",
            "/scratch/j1m/comparator-anchor-scan-receipt.json"])
        self.assertFalse(any(str(local).endswith(".gguf") for local, _, _ in extra))

    def test_the_deadline_ceiling_is_untouched_by_the_comparator_phase(self):
        envelope = self.orchestrator._eval_deadline_ceiling(self.config)
        self.assertEqual(envelope["upload_count"], 15.0)
        self.assertEqual(round(envelope["run_seconds"] - envelope["ceiling_seconds"], 3), 309.0)

    def test_the_arm_stages_are_argv_arrays_with_no_bearer_and_a_loopback_port(self):
        commands = self.orchestrator._comparator_remote_commands(self.config, "/scratch/j1m", ("q8_0", "bf16"))
        # One verify, one configure, one build, then one stage per arm.
        self.assertEqual(len(commands), 3 + 3)
        self.assertEqual(commands[0][:2], ["python3", "/scratch/j1m/j1m_runner.py"])
        self.assertIn("--verify-llama", commands[0])
        self.assertEqual(commands[1][:2], ["cmake", "-S"])
        self.assertIn("-DGGML_CUDA=ON", commands[1])
        self.assertIn("-DLLAMA_BUILD_SERVER=ON", commands[1])
        self.assertIn("-DLLAMA_OPENSSL=OFF", commands[1])
        self.assertIn("-DLLAMA_USE_PREBUILT_UI=OFF", commands[1])
        self.assertEqual(commands[2][:4], ["cmake", "--build", "/scratch/llama-server-build", "--target"])
        self.assertEqual(commands[2][4], "llama-server")
        arms = [command[command.index("--arm") + 1] for command in commands[3:]]
        self.assertEqual(arms, ["q4_k_m", "q8_0", "bf16"])
        ports = [int(command[command.index("--port") + 1]) for command in commands[3:]]
        self.assertEqual(ports, [18081, 18082, 18083])
        self.assertEqual(len(set(ports)), 3)
        for command in commands[3:]:
            self.assertTrue(all(isinstance(part, str) for part in command))
            self.assertNotIn("--api-key", command)
            self.assertEqual(command[command.index("--scan-receipt-sha256") + 1],
                             self.orchestrator._APPROVED_SCAN_RECEIPT_SHA256)
            self.assertEqual(command[command.index("--gpu-layers") + 1], "99")
            self.assertEqual(command[command.index("--context") + 1], "8192")
            self.assertEqual(command[command.index("--backend") + 1], "cuda")
            # No token-file operand at all: it named a path on a host this
            # process has not contacted, which `validate_persisted_argv` can
            # never accept as a private handle, so every arm stage was refused
            # before it could spawn. The arm driver mints its own bearer.
            self.assertNotIn("--token-file", command)
            self.assertFalse([part for part in command if "comparator-token" in part])
            self.assertEqual(self.runner.validate_persisted_argv(command), command)

    def test_the_server_build_flags_are_the_engine_compiler_identity(self):
        flags = self.runner.comparator_server_configure_flags(self.config)
        eval_commands = self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        engine_configure = next(command for command in eval_commands if command[0:2] == ["cmake", "-S"])
        for shared in ("-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CUDA_ARCHITECTURES=80",
                       "-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc"):
            self.assertIn(shared, flags)
            self.assertIn(shared, engine_configure)
        # Recorded verbatim in the arm receipt so a number is auditable.
        command = self.orchestrator._comparator_remote_commands(self.config, "/scratch/j1m", ("q8_0",))[-1]
        recorded = [command[index + 1] for index, part in enumerate(command) if part == "--server-build-flag"]
        self.assertEqual(recorded, flags)

    def test_the_deferred_cleanup_tail_is_the_runner_tail_with_remote_paths(self):
        tail = self.orchestrator._comparator_cleanup_commands(self.config, "/scratch/j1m")
        self.assertEqual(tail, self.runner.comparator_cleanup_plan(
            "/scratch/j1m/artifacts", "/scratch/j1m/j1m_runner.py",
            "/scratch/j1m/j1m-config.json", "/scratch/j1m/qwen35-9b.source-lock.json"))
        self.assertEqual(len(tail), 3)
        self.assertEqual(tail[0][:2], ["rm", "-f"])
        self.assertIn("/scratch/j1m/artifacts/Qwen3.5-9B-bf16.gguf", tail[0])
        self.assertIn("/scratch/j1m/artifacts/Qwen3.5-9B-Q8_0.gguf", tail[0])
        self.assertNotIn("/scratch/j1m/artifacts/Qwen3.5-9B-Q4_K_M.gguf", tail[0])

    def test_comparator_stage_budgets_fit_the_declared_setup_and_arm_budgets(self):
        commands = self.orchestrator._comparator_remote_commands(self.config, "/scratch/j1m", ("q8_0",))
        budgets = [self.orchestrator._comparator_stage_timeout(self.config, command) for command in commands]
        setup = sum(budgets[1:3])
        self.assertEqual(setup, self.orchestrator._COMPARATOR_SETUP_BUDGET_SECONDS)
        for arm_budget in budgets[3:]:
            self.assertEqual(arm_budget, self.orchestrator._COMPARATOR_ARM_BUDGET_SECONDS)
        declared = self.orchestrator._comparator_budget(self.config, ("q8_0",))["required_seconds"]
        self.assertEqual(declared, setup + 2 * self.orchestrator._COMPARATOR_ARM_BUDGET_SECONDS)

    def test_the_oracle_arm_is_budgeted_and_fetched_like_the_others(self):
        budget = self.orchestrator._comparator_budget(self.config, ("q4_k_m",))
        self.assertEqual(budget["arms"], ["q4_k_m"])
        self.assertEqual(budget["required_seconds"],
                         self.orchestrator._COMPARATOR_SETUP_BUDGET_SECONDS
                         + self.orchestrator._COMPARATOR_ARM_BUDGET_SECONDS)
        hourly = float(self.config["shadeform_target"]["hourly_usd"])
        self.assertEqual(budget["projected_marginal_cost_usd"],
                         round(hourly * budget["required_seconds"] / 3600.0, 6))
        self.assertFalse(budget["raises_authorized_cost"])
        allowlist = self.orchestrator._eval_fetch_allowlist(self.config, ("q4_k_m",))
        self.assertEqual(allowlist[5:], ["comparator-receipt-q4_k_m.json", "scan-receipt.json"])
        self.assertFalse(any(name.lower().endswith(".gguf") for name in allowlist))
        commands = self.orchestrator._comparator_remote_commands(self.config, "/scratch/j1m", ("q4_k_m",))
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[3][commands[3].index("--arm") + 1], "q4_k_m")
        self.assertIn("Qwen3.5-9B-Q4_K_M.gguf", commands[3][commands[3].index("--model") + 1])

    def test_a_present_but_invalid_arm_receipt_keeps_its_distinct_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "comparator-receipt-q4_k_m.json").write_text(json.dumps({
                "schema": "local_bmo.j1m.comparator-eval-receipt.v1", "status": "verified",
                "arm": "q4_k_m", "metrics": {"case_count": 1, "passed": 1, "failed": 0,
                                             "errors": 0, "category_summary": {
                                                 "no_tool": {"case_count": 1, "passed": 1,
                                                             "failed": 0, "errors": 0}}}}))
            (destination / "comparator-receipt-q8_0.json").write_text("{\"schema\": \"wrong\"}")
            receipt = self.orchestrator._write_comparison_receipt(destination, ("q8_0", "bf16"))
        reasons = {item["comparator"]: item["reason"] for item in receipt["skipped"]}
        self.assertEqual(reasons["q8_0"], "comparator_receipt_invalid")
        self.assertEqual(reasons["bf16"], "comparator_receipt_missing")

    def test_the_oracle_request_produces_a_baseline_with_no_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            receipt = self.orchestrator._write_comparison_receipt(
                destination, ("q4_k_m",), phase_reason="comparator_clock_insufficient")
        self.assertEqual(receipt["requested"], [])
        # A refused oracle request records the baseline arm's typed reason;
        # an empty list would be indistinguishable from asking for nothing.
        self.assertEqual(receipt["skipped"],
                         [{"comparator": "q4_k_m", "reason": "comparator_clock_insufficient"}])
        self.assertEqual(receipt["comparisons"], [])
        self.assertEqual(receipt["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
