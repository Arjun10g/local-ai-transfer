import unittest
import os
import io
import json
import contextlib
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts.test.evaluate_tool_calls import (
    FIXTURE_MAX_BYTES,
    MODEL_OUTPUT_MAX_CHARS,
    RESPONSE_MAX_BYTES,
    TOKEN_MAX_BYTES,
    RejectRedirectHandler,
    _build_no_proxy_opener,
    _post,
    evaluate_case,
    load_bearer_token,
    load_fixture,
    main,
    parse_tool_call,
    run_local,
    validate_endpoint,
)


class ToolCallEvaluatorTests(unittest.TestCase):
    def test_fixture_is_bounded_and_covers_required_categories(self):
        fixture = load_fixture()
        self.assertLessEqual(len(fixture["cases"]), 8)
        categories = {case["category"] for case in fixture["cases"]}
        self.assertTrue({"tool_selection", "argument_fidelity", "no_tool", "malformed_prompt", "prompt_injection"}.issubset(categories))

    def test_qwen_xml_parser_preserves_quoted_text_and_normalizes_plain_text(self):
        output = "<tool_call>\n<function=weather.get>\n<parameter=city>\n\"Toronto\"\n</parameter>\n<parameter=units>\ncelsius\n</parameter>\n</function>\n</tool_call>"
        self.assertEqual(parse_tool_call(output, load_fixture()["tools"]), {"name": "weather.get", "arguments": {"city": "\"Toronto\"", "units": "celsius"}})

    def test_no_tool_and_unknown_tool_are_fail_closed(self):
        fixture = load_fixture()
        no_tool = next(case for case in fixture["cases"] if case["id"] == "no-tool-math-001")
        self.assertEqual(evaluate_case(no_tool, "4"), (True, "no_call"))
        injection = next(case for case in fixture["cases"] if case["id"] == "injection-001")
        self.assertEqual(evaluate_case(injection, "<tool_call><function=shell.run></function></tool_call>", fixture["tools"]), (False, "forbidden_tool_name"))

    def test_parser_rejects_suffix_duplicate_nested_entity_unknown_and_missing(self):
        tools = load_fixture()["tools"]
        valid = "<tool_call><function=weather.get><parameter=city>Toronto</parameter><parameter=units>celsius</parameter></function></tool_call>"
        for malformed in (
            valid + " trailing",
            valid.replace("</function>", "<parameter=city>again</parameter></function>"),
            valid.replace("Toronto", "<parameter=evil>Toronto</parameter>"),
            valid.replace("weather.get", "shell.run"),
            valid.replace("<parameter=units>celsius</parameter>", ""),
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    parse_tool_call(malformed, tools)

    def test_parser_rejects_extra_argument_and_accepts_no_arg_call(self):
        tools = load_fixture()["tools"]
        extra = "<tool_call><function=weather.get><parameter=city>Toronto</parameter><parameter=units>celsius</parameter><parameter>x>1</parameter></function></tool_call>"
        with self.assertRaises(ValueError):
            parse_tool_call(extra, tools)
        self.assertEqual(parse_tool_call("<tool_call><function=system.get_info></function></tool_call>", tools), {"name": "system.get_info", "arguments": {}})

    def test_token_file_requires_regular_private_file_and_env_is_presence_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("local-eval-token-20260904\n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(load_bearer_token(path, "__NO_TOKEN_ENV__"), "local-eval-token-20260904")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                load_bearer_token(path, "__NO_TOKEN_ENV__")
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "regular"):
                load_bearer_token(link, "__NO_TOKEN_ENV__")
            path.unlink()
            path.write_bytes(b"x" * (TOKEN_MAX_BYTES + 1))
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "too_large"):
                load_bearer_token(path, "__NO_TOKEN_ENV__")
        old = os.environ.get("__EVAL_TOKEN_TEST")
        os.environ["__EVAL_TOKEN_TEST"] = "inherited-token-20260904"
        try:
            self.assertEqual(load_bearer_token(None, "__EVAL_TOKEN_TEST"), "inherited-token-20260904")
        finally:
            if old is None:
                os.environ.pop("__EVAL_TOKEN_TEST", None)
            else:
                os.environ["__EVAL_TOKEN_TEST"] = old

    def test_runtime_payload_uses_native_tools_field_without_handwritten_prompt(self):
        fixture = load_fixture()
        with patch("scripts.test.evaluate_tool_calls._post", return_value="<tool_call><function=system.get_info></function></tool_call>") as post:
            result = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=1)
        self.assertEqual((result["passed"], result["errors"]), (1, 0))
        payload = post.call_args.args[2]
        self.assertEqual(payload["tools"], fixture["tools"])
        self.assertEqual(payload["messages"], fixture["cases"][0]["messages"])
        self.assertNotIn("system", {message["role"] for message in payload["messages"]})

    def test_endpoint_is_explicit_loopback_http_only(self):
        self.assertEqual(validate_endpoint("http://127.0.0.1:49912/v1/chat/completions"), "http://127.0.0.1:49912/v1/chat/completions")
        self.assertEqual(validate_endpoint("http://localhost:49912/v1/chat/completions"), "http://localhost:49912/v1/chat/completions")
        invalid = (
            "https://127.0.0.1:49912/v1/chat/completions",
            "http://192.0.2.1:49912/v1/chat/completions",
            "http://[::1]:49912/v1/chat/completions",
            "http://user:password@localhost:49912/v1/chat/completions",
            "http://localhost/v1/chat/completions",
            "http://localhost:49912/v1/other",
            "http://localhost:49912/v1/chat/completions?redirect=remote",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    validate_endpoint(endpoint)

    def test_http_and_decoded_model_output_are_bounded(self):
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                self.limit = limit
                return b"x" * (RESPONSE_MAX_BYTES + 1)

        with patch("scripts.test.evaluate_tool_calls._open_url", return_value=OversizedResponse()):
            with self.assertRaisesRegex(ValueError, "response_too_large"):
                _post("http://127.0.0.1:49912/v1/chat/completions", "test-token-20260904", {}, 0.1)

        class FixedResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return self.body

        session = FixedResponse(b'{"id":"s"}')
        body = json.dumps({"choices": [{"message": {"content": "x" * (MODEL_OUTPUT_MAX_CHARS + 1)}}]}).encode()
        with patch("scripts.test.evaluate_tool_calls._open_url", side_effect=[session, FixedResponse(body)]):
            with self.assertRaisesRegex(ValueError, "model_output_too_large"):
                _post("http://127.0.0.1:49912/v1/chat/completions", "test-token-20260904", {}, 0.1)

    def test_literal_value_operators_are_allowed_but_entity_and_structure_are_not(self):
        tools = load_fixture()["tools"]
        output = "<tool_call><function=weather.get><parameter=city>Toronto <downtown> & west</parameter><parameter=units>celsius</parameter></function></tool_call>"
        self.assertEqual(parse_tool_call(output, tools)["arguments"]["city"], "Toronto <downtown> & west")

    def test_shared_runtime_value_vectors(self):
        vectors = json.loads((Path(__file__).with_name("qwen_xml_vectors.json")).read_text(encoding="utf-8"))
        self.assertEqual(vectors["schema"], "local_bmo.qwen-xml-vectors.v1")
        self.assertLessEqual(len(vectors["vectors"]), 16)
        for vector in vectors["vectors"]:
            with self.subTest(vector=vector["id"]):
                if "reject" in vector:
                    with self.assertRaises(ValueError):
                        parse_tool_call(vector["xml"])
                else:
                    self.assertEqual(parse_tool_call(vector["xml"]), vector["expected"])

    def test_proxy_environment_is_ignored_and_redirects_fail_for_both_requests(self):
        with patch.dict(os.environ, {"http_proxy": "http://attacker.invalid:8080", "HTTPS_PROXY": "http://attacker.invalid:8080"}):
            with patch("scripts.test.evaluate_tool_calls.urllib.request.getproxies", side_effect=AssertionError("proxy lookup")):
                opener = _build_no_proxy_opener()
        self.assertFalse(any(hasattr(handler, "proxies") for handler in opener.handlers))
        handler = RejectRedirectHandler()
        for path in ("/v1/sessions", "/v1/chat/completions"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    handler.redirect_request(urllib.request.Request("http://127.0.0.1:49912" + path), io.BytesIO(), 302, "found", {}, "http://remote.invalid")
                self.assertEqual(raised.exception.code, 302)
                if raised.exception.fp is not None:
                    raised.exception.fp.close()
                raised.exception.close()

    def test_fixture_file_and_nested_input_sizes_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_bytes(b"x" * (FIXTURE_MAX_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "fixture_too_large"):
                load_fixture(path)
            fixture = load_fixture()
            fixture["cases"][0]["messages"][0]["content"] = "x" * 4097
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "message is unbounded"):
                load_fixture(path)

    def test_cli_bounds_are_fail_closed(self):
        for argument in (("--max-cases", "0"), ("--max-cases", "9"), ("--timeout", "0"), ("--timeout", "601"), ("--timeout", "nan"), ("--engine-pid", "0")):
            with self.subTest(argument=argument), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(["--dry-run", *argument])


if __name__ == "__main__":
    unittest.main()
