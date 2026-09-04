import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.test.evaluate_tool_calls import evaluate_case, load_bearer_token, load_fixture, parse_tool_call, run_local


class ToolCallEvaluatorTests(unittest.TestCase):
    def test_fixture_is_bounded_and_covers_required_categories(self):
        fixture = load_fixture()
        self.assertLessEqual(len(fixture["cases"]), 8)
        categories = {case["category"] for case in fixture["cases"]}
        self.assertTrue({"tool_selection", "argument_fidelity", "no_tool", "malformed_prompt", "prompt_injection"}.issubset(categories))

    def test_qwen_xml_parser_normalizes_json_parameter_values(self):
        output = "<tool_call>\n<function=weather.get>\n<parameter=city>\n\"Toronto\"\n</parameter>\n<parameter=units>\ncelsius\n</parameter>\n</function>\n</tool_call>"
        self.assertEqual(parse_tool_call(output, load_fixture()["tools"]), {"name": "weather.get", "arguments": {"city": "Toronto", "units": "celsius"}})

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
            valid.replace("Toronto", "<nested>Toronto</nested>"),
            valid.replace("Toronto", "&lt;Toronto&gt;"),
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


if __name__ == "__main__":
    unittest.main()
