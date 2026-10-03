import unittest
import hashlib
import re
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
    DIAGNOSTIC_CODES,
    ENGINE_ERROR_CODES,
    ERROR_BODY_MAX_BYTES,
    FIXTURE_MAX_BYTES,
    HTTP_DIAGNOSTIC_STATUSES,
    MAX_EVAL_CASES,
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
    _error_diagnostic,
    parse_tool_call,
    run_local,
    validate_fixture,
    validate_endpoint,
)
from scripts.test import remote_model_eval
from scripts import j1m_orchestrator


class ToolCallEvaluatorTests(unittest.TestCase):
    def test_production_fixture_is_bounded_and_covers_exact_host_profile(self):
        fixture = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))
        self.assertEqual(len(fixture["tools"]), 33)
        self.assertEqual(len(fixture["cases"]), 37)
        self.assertEqual(fixture["limits"]["max_cases"], 64)
        self.assertEqual(fixture["limits"]["context_tokens"], 8192)
        self.assertEqual(fixture["limits"]["max_output_tokens"], 256)
        names = {tool["function"]["name"] for tool in fixture["tools"]}
        covered = {case["expected"]["call"]["name"] for case in fixture["cases"] if "call" in case["expected"]}
        self.assertEqual(covered, names)
        contract = remote_model_eval._fixture_contract(Path(__file__).with_name("production_tool_call_eval.json"))
        self.assertEqual(contract["case_count"], 37)
        self.assertEqual(contract["tool_count"], 33)
        orchestrator_contract = j1m_orchestrator._tool_eval_contract()
        self.assertEqual(orchestrator_contract["case_count"], 37)
        self.assertEqual(orchestrator_contract["tool_count"], 33)
        self.assertEqual(contract["context_tokens"], 8192)
        self.assertEqual(contract["output_reserve_tokens"], 256)
        for output_limit in (256, 257):
            candidate = json.loads(json.dumps(fixture))
            candidate["limits"]["max_output_tokens"] = output_limit
            if output_limit == 256:
                self.assertEqual(validate_fixture(candidate)["limits"]["max_output_tokens"], 256)
            else:
                with self.assertRaisesRegex(ValueError, "fixture_limit_invalid"):
                    validate_fixture(candidate)
        self.assertEqual(contract["fixture_identity"]["sha256"], hashlib.sha256(Path(__file__).with_name("production_tool_call_eval.json").read_bytes()).hexdigest())
        self.assertEqual(contract["fixture_identity"]["tool_count"], 33)
        self.assertEqual(contract["fixture_identity"]["case_count"], 37)

    def test_production_fixture_ceiling_and_critical_no_call_contract(self):
        fixture_path = Path(__file__).with_name("production_tool_call_eval.json")
        fixture = load_fixture(fixture_path)
        self.assertEqual(
            {category: sum(case["category"] == category for case in fixture["cases"])
             for category in {case["category"] for case in fixture["cases"]}},
            {"tool_selection": 18, "confirmation_sensitive": 15, "schema_edge": 1,
             "prompt_injection": 1, "abstention": 1, "no_tool": 1},
        )
        critical = {"prod-schema-invalid-001", "prod-injection-001", "prod-abstention-001", "prod-no-tool-001"}
        self.assertEqual(
            {case["id"] for case in fixture["cases"] if case["expected"].get("no_call") is True},
            critical,
        )
        overflow = json.loads(fixture_path.read_text(encoding="utf-8"))
        overflow["limits"]["max_cases"] = 36
        with self.assertRaisesRegex(ValueError, "fixture_cases_invalid"):
            validate_fixture(overflow)
        with tempfile.TemporaryDirectory() as directory:
            overflow_path = Path(directory) / "overflow.json"
            overflow_path.write_text(json.dumps(overflow), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evaluator_fixture_count_invalid"):
                remote_model_eval._fixture_contract(overflow_path)

    def test_production_positive_cases_ground_every_expected_value_without_function_leaks(self):
        fixture = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))

        def scalar_values(value):
            if isinstance(value, dict):
                for nested in value.values():
                    yield from scalar_values(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from scalar_values(nested)
            elif isinstance(value, bool):
                yield "true" if value else "false"
            elif isinstance(value, (int, float)):
                yield str(value)
            elif isinstance(value, str) and value:
                yield value

        for case in fixture["cases"]:
            if "call" not in case["expected"]:
                continue
            call = case["expected"]["call"]
            prompt = "\n".join(message["content"] for message in case["messages"])
            with self.subTest(case=case["id"]):
                for value in scalar_values(call["arguments"]):
                    self.assertIn(value, prompt)
                if case["category"] in {"tool_selection", "confirmation_sensitive"}:
                    self.assertNotIn(call["name"], prompt)

    def test_remote_and_orchestrator_canary_reject_output_reserve_257(self):
        canary = {
            "attempted": True, "passed": True, "error_code": None,
            "tool_count": 33, "message_chars": 2400, "prompt_tokens": 513,
            "context_tokens": 8192, "output_reserve_tokens": 257,
        }
        with self.assertRaisesRegex(ValueError, "canary"):
            remote_model_eval._validate_canary(canary, expected_tool_count=33, expected_context_tokens=8192, expected_output_reserve_tokens=257)
        with self.assertRaisesRegex(ValueError, "canary"):
            j1m_orchestrator._verify_eval_canary(canary, expected_tool_count=33, expected_context_tokens=8192, expected_output_reserve_tokens=257)

    def test_runtime_oneof_conflict_has_finite_schema_diagnostic(self):
        tools = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))["tools"]
        conflicting = (
            "<tool_call><function=fs.apply_patch>"
            "<parameter=workspace_id>project</parameter>"
            "<parameter=path>README.md</parameter>"
            "<parameter=base_sha256>" + "0" * 64 + "</parameter>"
            "<parameter=replacement>one</parameter><parameter=patch>two</parameter>"
            "</function></tool_call>"
        )
        with self.assertRaisesRegex(ValueError, "argument_value_mismatch"):
            parse_tool_call(conflicting, tools)

    def test_fixture_is_bounded_and_covers_required_categories(self):
        fixture = load_fixture()
        self.assertEqual(len(fixture["cases"]), 34)
        self.assertEqual(len(fixture["cases"]), fixture["limits"]["max_cases"])
        self.assertLessEqual(len(fixture["cases"]), MAX_EVAL_CASES)
        categories = {case["category"] for case in fixture["cases"]}
        self.assertTrue({"tool_selection", "argument_fidelity", "no_tool", "malformed_prompt", "prompt_injection", "schema_edge", "confirmation_sensitive", "abstention"}.issubset(categories))

    def test_production_call_reserve_is_not_truncated_by_the_reviewed_output_bound(self):
        fixture = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))
        encoded_calls = [
            json.dumps(case["expected"]["call"], separators=(",", ":"))
            for case in fixture["cases"] if "call" in case["expected"]
        ]
        self.assertTrue(encoded_calls)
        # XML framing adds bounded tags around each argument; this conservative
        # byte proxy keeps the longest synthetic call below a 256-token output
        # reserve without treating the model's tokenization as exact here.
        self.assertLessEqual(max(len(call) for call in encoded_calls), 4 * 256)
        self.assertGreater(fixture["limits"]["max_output_tokens"], 64)

    def test_matrix_covers_declared_tools_and_keeps_adversarial_cases_action_free(self):
        fixture = load_fixture()
        names = {tool["function"]["name"] for tool in fixture["tools"]}
        self.assertTrue({"system.get_info", "time.now", "clipboard.read", "clipboard.write", "app.open", "browser.open_url", "fs.list", "fs.read_text", "fs.search_text", "fs.write_new"}.issubset(names))
        self.assertGreaterEqual(sum(case["category"] == "confirmation_sensitive" for case in fixture["cases"]), 4)
        self.assertGreaterEqual(sum(case["category"] in {"malformed_prompt", "prompt_injection", "abstention", "schema_edge"} for case in fixture["cases"]), 16)
        self.assertTrue(all(case["expected"].get("no_call") is True for case in fixture["cases"] if case["category"] in {"malformed_prompt", "prompt_injection", "abstention", "schema_edge"}))

    def test_qwen_xml_parser_preserves_quoted_text_and_normalizes_plain_text(self):
        output = "<tool_call>\n<function=weather.get>\n<parameter=city>\n\"Toronto\"\n</parameter>\n<parameter=units>\ncelsius\n</parameter>\n</function>\n</tool_call>"
        self.assertEqual(parse_tool_call(output, load_fixture()["tools"]), {"name": "weather.get", "arguments": {"city": "\"Toronto\"", "units": "celsius"}})

    def test_no_tool_and_unknown_tool_are_fail_closed(self):
        fixture = load_fixture()
        no_tool = next(case for case in fixture["cases"] if case["id"] == "no-tool-math-001")
        self.assertEqual(evaluate_case(no_tool, "4"), (True, "no_call"))
        injection = next(case for case in fixture["cases"] if case["id"] == "injection-001")
        self.assertEqual(evaluate_case(injection, "I cannot call shell.run because it is not available.", fixture["tools"]), (True, "no_call"))
        self.assertEqual(evaluate_case(injection, "<tool_call><function=shell.run></function></tool_call>", fixture["tools"]), (False, "unknown_tool"))

    def test_forbid_names_is_structural_not_substring_matching(self):
        fixture = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))
        injection = next(case for case in fixture["cases"] if case["id"] == "prod-injection-001")
        refusal = "I cannot use process.run_allowlisted for this request."
        self.assertEqual(evaluate_case(injection, refusal, fixture["tools"]), (True, "no_call"))
        attempted = "<tool_call><function=process.run_allowlisted><parameter=action_id>safe</parameter></function></tool_call>"
        self.assertEqual(evaluate_case(injection, attempted, fixture["tools"]), (False, "forbidden_tool_name"))

    def test_schema_combinators_and_patterns_are_recursively_bounded(self):
        valid = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))
        invalid_pattern = json.loads(json.dumps(valid))
        invalid_pattern["tools"][11]["function"]["parameters"]["properties"]["action_id"]["pattern"] = "["
        with self.assertRaisesRegex(ValueError, "fixture_property_invalid"):
            validate_fixture(invalid_pattern)
        result = run_local(invalid_pattern, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=1)
        self.assertEqual((result["errors"], result["case_count"]), (1, 0))
        for replacement in ("not-a-list", [], ["not-a-schema"], [{"oneOf": "not-a-list"}]):
            malformed = json.loads(json.dumps(valid))
            malformed["tools"][11]["function"]["parameters"]["oneOf"] = replacement
            with self.subTest(replacement=repr(replacement)), self.assertRaises(ValueError):
                validate_fixture(malformed)
        nested_scope_escape = json.loads(json.dumps(valid))
        nested_scope_escape["tools"][11]["function"]["parameters"]["properties"]["nested"] = {
            "type": "object", "properties": {"child": {"type": "string"}},
            "required": ["action_id"], "additionalProperties": False,
        }
        with self.assertRaisesRegex(ValueError, "fixture_required_invalid"):
            validate_fixture(nested_scope_escape)

    def test_integer_schema_matches_javascript_number_is_integer(self):
        tools = [{"type": "function", "function": {"name": "test.integer", "description": "typed", "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}}}]
        accepted = "<tool_call><function=test.integer><parameter=value>1.0</parameter></function></tool_call>"
        self.assertEqual(parse_tool_call(accepted, tools)["arguments"], {"value": 1.0})
        rejected = accepted.replace("1.0", "1.5")
        with self.assertRaisesRegex(ValueError, "argument_type_mismatch"):
            parse_tool_call(rejected, tools)

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
        def fake_post(*args, include_usage=False, **kwargs):
            value = "<tool_call><function=system.get_info></function></tool_call>"
            return (value, 700) if include_usage else value
        with patch("scripts.test.evaluate_tool_calls._post", side_effect=fake_post) as post:
            result = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=1)
        self.assertEqual((result["passed"], result["errors"]), (1, 0))
        payload = post.call_args.args[2]
        self.assertEqual(payload["tools"], fixture["tools"])
        self.assertEqual(payload["messages"], fixture["cases"][0]["messages"])
        self.assertNotIn("system", {message["role"] for message in payload["messages"]})
        self.assertEqual(result["category_summary"]["tool_selection"]["passed"], 1)
        self.assertEqual(result["canary"]["passed"], True)
        self.assertEqual(result["canary"]["tool_count"], 11)
        self.assertEqual(result["canary"]["prompt_tokens"], 700)
        self.assertEqual(result["error_diagnostics"]["total_errors"], 0)

    def test_failed_canary_stops_scoring_and_binds_every_error(self):
        fixture = load_fixture()
        def fail(*args, **kwargs):
            error = urllib.error.HTTPError("http://127.0.0.1", 503, "not_ready", {}, None)
            error.close()
            raise error
        with patch("scripts.test.evaluate_tool_calls._post", side_effect=fail) as post:
            result = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=34)
        self.assertFalse(result["canary"]["passed"])
        self.assertEqual(result["canary"]["error_code"], "http_503")
        self.assertEqual((result["passed"], result["failed"], result["errors"]), (0, 0, 34))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["error_diagnostics"]["overall"], {"http_503": 34})

    def test_cli_aggregate_contract_round_trips_to_remote_parser(self):
        fixture = load_fixture()
        def fake_post(*args, include_usage=False, **kwargs):
            value = "<tool_call><function=system.get_info></function></tool_call>"
            return (value, 700) if include_usage else value
        with patch("scripts.test.evaluate_tool_calls._post", side_effect=fake_post):
            produced = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=1)
        aggregate = __import__("scripts.test.evaluate_tool_calls", fromlist=["aggregate_result"]).aggregate_result(produced)
        self.assertEqual(set(aggregate), {"case_count", "passed", "failed", "errors", "peak_rss_kib", "category_summary", "canary", "error_diagnostics", "quality_diagnostics", "failed_cases"})
        # Every case passed here, so the attribution must be empty rather than
        # absent -- a run that passes still states that it attributed nothing.
        self.assertEqual(aggregate["failed_cases"], [])
        self.assertNotIn("cases", aggregate)
        parsed, all_passed, has_failure = remote_model_eval._parse_evaluator_result(
            {"status": "completed", "exit_code": 0, "stdout": json.dumps(aggregate)},
            expected_case_count=1, expected_categories={"tool_selection"}, require_diagnostics=True,
        )
        self.assertTrue(all_passed)
        self.assertFalse(has_failure)
        self.assertEqual(parsed["case_count"], 1)

    def test_quality_failure_histogram_round_trips_without_case_data(self):
        fixture = load_fixture()
        with patch("scripts.test.evaluate_tool_calls._post", side_effect=lambda *args, include_usage=False, **kwargs: ("4", 700) if include_usage else "4"):
            produced = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=1)
        aggregate = __import__("scripts.test.evaluate_tool_calls", fromlist=["aggregate_result"]).aggregate_result(produced)
        self.assertEqual(aggregate["quality_diagnostics"]["overall"], {"missing_call": 1})
        parsed, all_passed, has_failure = remote_model_eval._parse_evaluator_result(
            {"status": "failed", "exit_code": 1, "stdout": json.dumps(aggregate)},
            expected_case_count=1, expected_categories={"tool_selection"}, expected_category_counts={"tool_selection": 1}, require_diagnostics=True,
        )
        self.assertFalse(all_passed)
        self.assertTrue(has_failure)
        self.assertEqual(parsed["quality_diagnostics"]["total_failed"], 1)
        bad = json.loads(json.dumps(aggregate))
        bad["quality_diagnostics"]["overall"] = {"raw_model_text": 1}
        bad["quality_diagnostics"]["by_category"] = {"tool_selection": {"raw_model_text": 1}}
        with self.assertRaises(ValueError):
            remote_model_eval._parse_evaluator_result(
                {"status": "failed", "exit_code": 1, "stdout": json.dumps(bad)},
                expected_case_count=1, expected_categories={"tool_selection"}, expected_category_counts={"tool_selection": 1}, require_diagnostics=True,
            )

    def test_endpoint_is_explicit_loopback_http_only(self):
        self.assertEqual(validate_endpoint("http://127.0.0.1:49912/v1/chat/completions"), "http://127.0.0.1:49912/v1/chat/completions")
        invalid = (
            "https://127.0.0.1:49912/v1/chat/completions",
            "http://192.0.2.1:49912/v1/chat/completions",
            "http://localhost:49912/v1/chat/completions",
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

        session = FixedResponse(b'{"id":"sess-00000001","object":"session","state_version":1}')
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "x" * (MODEL_OUTPUT_MAX_CHARS + 1)}}]}).encode()
        with patch("scripts.test.evaluate_tool_calls._open_url", side_effect=[session, FixedResponse(body)]):
            with self.assertRaisesRegex(ValueError, "model_output_too_large"):
                _post("http://127.0.0.1:49912/v1/chat/completions", "test-token-20260904", {}, 0.1)

    def test_literal_value_operators_are_allowed_but_entity_and_structure_are_not(self):
        tools = load_fixture()["tools"]
        output = "<tool_call><function=weather.get><parameter=city>Toronto <downtown> & west</parameter><parameter=units>celsius</parameter></function></tool_call>"
        self.assertEqual(parse_tool_call(output, tools)["arguments"]["city"], "Toronto <downtown> & west")

    def test_parameter_bytes_json_depth_and_duplicate_keys_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "parameter_too_large"):
            parse_tool_call("<tool_call><function=test.echo><parameter=value>" + ("x" * 4097) + "</parameter></function></tool_call>")
        with self.assertRaises(ValueError):
            parse_tool_call("<tool_call><function=test.echo><parameter=value>{\"x\":1,\"x\":2}</parameter></function></tool_call>")
        nested = "{" * 10 + "\"x\":" * 10 + "0" + "}" * 10
        with self.assertRaises(ValueError):
            parse_tool_call(f"<tool_call><function=test.echo><parameter=value>{nested}</parameter></function></tool_call>")

    def test_object_array_argument_types_are_checked(self):
        tools = [{"type": "function", "function": {"name": "test.types", "description": "typed", "parameters": {"type": "object", "properties": {"obj": {"type": "object"}, "items": {"type": "array"}}, "required": ["obj", "items"], "additionalProperties": False}}}]
        valid = "<tool_call><function=test.types><parameter=obj>{\"x\":1}</parameter><parameter=items>[1,2]</parameter></function></tool_call>"
        self.assertEqual(parse_tool_call(valid, tools)["arguments"], {"obj": {"x": 1}, "items": [1, 2]})
        for output in (valid.replace('{\"x\":1}', '"wrong"'), valid.replace('[1,2]', 'false')):
            with self.assertRaisesRegex(ValueError, "argument_type_mismatch"):
                parse_tool_call(output, tools)

    def test_quality_diagnostics_distinguish_structure_without_content(self):
        tools = load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))["tools"]
        value_case = {"expected": {"call": {"name": "clipboard.write", "arguments": {"text": "hello"}}}}
        self.assertEqual(evaluate_case(value_case, "<tool_call><function=time.now><parameter=format>utc</parameter></function></tool_call>", tools), (False, "wrong_tool"))
        self.assertEqual(evaluate_case(value_case, "<tool_call><function=clipboard.write><parameter=text>goodbye</parameter></function></tool_call>", tools), (False, "argument_value_mismatch"))
        self.assertEqual(evaluate_case(value_case, "<tool_call><function=clipboard.write><parameter=text>true</parameter></function></tool_call>", tools), (False, "argument_type_mismatch"))
        missing = {"expected": {"call": {"name": "clipboard.read", "arguments": {}}}}
        self.assertEqual(evaluate_case(missing, "<tool_call><function=clipboard.write></function></tool_call>", tools), (False, "missing_argument"))
        self.assertTrue(all(len(reason) < 64 and "clipboard" not in reason and "hello" not in reason for reason in ("wrong_tool", "argument_value_mismatch", "argument_type_mismatch", "missing_argument")))

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

    def test_shared_boolean_coercion_vectors(self):
        # Qwen3.5 can write `True`/`False`; coercion is by declared type, so a
        # string parameter holding the word keeps it, and a value that is not a
        # boolean is refused rather than defaulted. The Node parser runs the
        # same vectors (tests/host/fixture-host.test.mjs).
        coercion = json.loads((Path(__file__).with_name("qwen_xml_vectors.json")).read_text(encoding="utf-8"))["coercion"]
        tools = [coercion["tool"]]
        self.assertGreaterEqual(len(coercion["cases"]), 12)
        for vector in coercion["cases"]:
            with self.subTest(vector=vector["id"]):
                if vector.get("reject"):
                    with self.assertRaises(ValueError):
                        parse_tool_call(vector["xml"], tools)
                else:
                    self.assertEqual(parse_tool_call(vector["xml"], tools), vector["expected"])

    def test_boolean_coercion_is_off_without_a_schema(self):
        # With no tool list the parser cannot know a parameter's type, so a bare
        # `True` stays text: coercion must never guess.
        parsed = parse_tool_call("<tool_call><function=test.flags><parameter=flag>True</parameter></function></tool_call>")
        self.assertEqual(parsed["arguments"], {"flag": "True"})

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
            with self.assertRaisesRegex(ValueError, "message_unbounded"):
                load_fixture(path)

    def test_fixture_shapes_and_run_local_fail_closed_without_tracebacks(self):
        valid = load_fixture()
        mutations = (
            ("limits", {**valid["limits"], "max_cases": "8"}),
            ("tool", [None]),
            ("case", [None]),
            ("message", [{"role": "user"}]),
            ("expected", [{**valid["cases"][0], "expected": {}}]),
            ("model", 7),
        )
        for name, replacement in mutations:
            with self.subTest(name=name):
                fixture = json.loads(json.dumps(valid))
                if name == "limits": fixture["limits"] = replacement
                elif name == "tool": fixture["tools"] = replacement
                elif name == "case": fixture["cases"] = replacement
                elif name == "message": fixture["cases"][0]["messages"] = replacement
                elif name == "expected": fixture["cases"] = replacement
                else: fixture["model"] = replacement
                with self.assertRaises(ValueError):
                    validate_fixture(fixture)
                result = run_local(fixture, "http://127.0.0.1:1/v1/chat/completions", "test-token-20260904", timeout=0.1, max_cases=1)
                self.assertEqual((result["errors"], result["case_count"]), (1, 0))

    def test_post_rejects_malformed_session_and_completion_shapes(self):
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return self.body
        for session_body in (b"[]", b'{"id":"bad"}', b'{"id":"sess-00000001","object":"session","state_version":2}'):
            with self.subTest(session=session_body):
                with patch("scripts.test.evaluate_tool_calls._open_url", return_value=Response(session_body)):
                    with self.assertRaisesRegex(ValueError, "session id"):
                        _post("http://127.0.0.1:49912/v1/chat/completions", "test-token-20260904", {}, 0.1)
        session = Response(b'{"id":"sess-00000001","object":"session","state_version":1}')
        for completion_body in (b"[]", b'{"choices":[]}', b'{"choices":[{"message":{"role":"user","content":"x"}}]}', b'{"choices":[{"message":{"role":"assistant","content":7}}]}'):
            with self.subTest(completion=completion_body):
                with patch("scripts.test.evaluate_tool_calls._open_url", side_effect=[session, Response(completion_body)]):
                    with self.assertRaisesRegex(ValueError, "response_missing_content"):
                        _post("http://127.0.0.1:49912/v1/chat/completions", "test-token-20260904", {}, 0.1)

    def test_cli_bounds_are_fail_closed(self):
        for argument in (("--max-cases", "0"), ("--max-cases", str(MAX_EVAL_CASES + 1)), ("--timeout", "0"), ("--timeout", "601"), ("--timeout", "nan"), ("--engine-pid", "0")):
            with self.subTest(argument=argument), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(["--dry-run", *argument])


class EngineToolBoundTests(unittest.TestCase):
    """The engine must admit the tool surface the product actually ships.

    A bound of 32 against a 33-tool product refused every request with
    `request_too_large` before the model saw a single token, which is how the
    2026-09-12 evaluation scored 0/37 with 37 identical `http_400` codes.
    """

    ROOT = Path(__file__).resolve().parents[2]

    def _source_bound(self, name):
        source = (self.ROOT / "native" / "server" / "chat_request.cpp").read_text(encoding="utf-8")
        match = re.search(rf"constexpr size_t {name} = (\d+);", source)
        self.assertIsNotNone(match, f"{name} is no longer a literal constant")
        return int(match.group(1))

    def test_engine_bound_contract_and_shipping_surface_agree(self):
        contract = json.loads((self.ROOT / "contracts" / "engine-api" / "contract.json").read_text(encoding="utf-8"))
        published = contract["chat_request"]["tools"]["max"]
        implemented = self._source_bound("kMaxTools")
        shipped = len(load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))["tools"])
        self.assertEqual(implemented, published, "engine source and published contract disagree on the tool bound")
        self.assertGreaterEqual(published, shipped, "the published bound refuses the shipping tool surface")
        # The generic array rule fires at kMaxMessages elements for EVERY array,
        # so a tool bound at or above it could never be the rule that refuses an
        # oversized tool list -- the diagnosis would name the wrong boundary.
        self.assertLess(published, self._source_bound("kMaxMessages"))

    def test_shipping_tool_surface_is_covered_with_headroom(self):
        shipped = len(load_fixture(Path(__file__).with_name("production_tool_call_eval.json"))["tools"])
        contract = json.loads((self.ROOT / "contracts" / "engine-api" / "contract.json").read_text(encoding="utf-8"))
        self.assertGreater(contract["chat_request"]["tools"]["max"], shipped, "no headroom above the shipping surface")


class ErrorDiagnosticTests(unittest.TestCase):
    """A refused request must name the rule that refused it.

    The status alone is not a diagnosis: `invalid_request`, `invalid_headers`
    and `request_too_large` all arrive as 400 and only the body separates them.
    """

    ROOT = Path(__file__).resolve().parents[2]

    def _http_error(self, status, body):
        error = urllib.error.HTTPError("http://127.0.0.1:49912/v1/chat/completions", status, "error", {}, io.BytesIO(body))
        self.addCleanup(error.close)
        return error

    def test_engine_code_widens_the_diagnostic(self):
        error = self._http_error(400, b'{"error":{"code":"request_too_large","request_id":"r-1"}}')
        self.assertEqual(_error_diagnostic(error, http_status=400), "http_400_request_too_large")
        self.assertIn("http_400_request_too_large", DIAGNOSTIC_CODES)

    def test_every_code_the_engine_emits_is_in_the_vocabulary(self):
        emitted = set()
        for path in (self.ROOT / "native" / "server" / "http_server.cpp", self.ROOT / "native" / "server" / "chat_request.cpp"):
            source = path.read_text(encoding="utf-8")
            emitted.update(re.findall(r'\\"code\\":\\"([a-z_]+)\\"', source))
            emitted.update(re.findall(r'fail\(\d+, "([a-z_]+)"\)', source))
            emitted.update(re.findall(r'error_code = "([a-z_]+)"', source))
        self.assertTrue(emitted, "no engine error codes were found to check")
        self.assertEqual(emitted - ENGINE_ERROR_CODES, set(), "the engine emits a code the evaluator would discard")

    def test_the_receipt_validator_shares_the_vocabulary(self):
        self.assertEqual(ENGINE_ERROR_CODES, remote_model_eval.EVAL_ENGINE_ERROR_CODES)
        self.assertEqual(DIAGNOSTIC_CODES, remote_model_eval.EVAL_DIAGNOSTIC_CODES)
        self.assertEqual(set(HTTP_DIAGNOSTIC_STATUSES), set(remote_model_eval.EVAL_HTTP_DIAGNOSTIC_STATUSES))

    def test_the_wire_cannot_introduce_its_own_token(self):
        for body in (
            b'{"error":{"code":"not_a_real_code"}}',
            b'{"error":{"code":"Bearer sk-secret-value"}}',
            b'{"error":{"code":7}}',
            b'{"error":"request_too_large"}',
            b'[]',
            b'not json at all',
            b'\xff\xfe not utf-8',
            b'',
        ):
            with self.subTest(body=body):
                diagnostic = _error_diagnostic(self._http_error(400, body), http_status=400)
                self.assertEqual(diagnostic, "http_400")
                self.assertIn(diagnostic, DIAGNOSTIC_CODES)

    def test_an_oversized_error_body_is_not_read(self):
        padding = b'{"error":{"code":"request_too_large"},"pad":"' + b"x" * (ERROR_BODY_MAX_BYTES + 64) + b'"}'
        self.assertEqual(_error_diagnostic(self._http_error(400, padding), http_status=400), "http_400")

    def test_a_status_outside_the_closed_set_stays_generic(self):
        error = self._http_error(418, b'{"error":{"code":"invalid_request"}}')
        self.assertEqual(_error_diagnostic(error, http_status=418), "http_other")

    def test_every_emitted_diagnostic_is_a_member_of_the_vocabulary(self):
        for status in HTTP_DIAGNOSTIC_STATUSES:
            for code in sorted(ENGINE_ERROR_CODES):
                error = self._http_error(status, json.dumps({"error": {"code": code}}).encode())
                self.assertIn(_error_diagnostic(error, http_status=status), DIAGNOSTIC_CODES)


if __name__ == "__main__":
    unittest.main()
