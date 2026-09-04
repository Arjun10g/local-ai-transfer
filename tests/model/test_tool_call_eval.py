import unittest

from scripts.test.evaluate_tool_calls import evaluate_case, load_fixture, parse_tool_call


class ToolCallEvaluatorTests(unittest.TestCase):
    def test_fixture_is_bounded_and_covers_required_categories(self):
        fixture = load_fixture()
        self.assertLessEqual(len(fixture["cases"]), 8)
        categories = {case["category"] for case in fixture["cases"]}
        self.assertTrue({"tool_selection", "argument_fidelity", "no_tool", "malformed_prompt", "prompt_injection"}.issubset(categories))

    def test_qwen_xml_parser_normalizes_json_parameter_values(self):
        output = "<tool_call>\n<function=weather.get>\n<parameter=city>\n\"Toronto\"\n</parameter>\n<parameter=units>\ncelsius\n</parameter>\n</function>\n</tool_call>"
        self.assertEqual(parse_tool_call(output), {"name": "weather.get", "arguments": {"city": "Toronto", "units": "celsius"}})

    def test_no_tool_and_unknown_tool_are_fail_closed(self):
        fixture = load_fixture()
        no_tool = next(case for case in fixture["cases"] if case["id"] == "no-tool-math-001")
        self.assertEqual(evaluate_case(no_tool, "4"), (True, "no_call"))
        injection = next(case for case in fixture["cases"] if case["id"] == "injection-001")
        self.assertEqual(evaluate_case(injection, "<tool_call><function=shell.run></function></tool_call>"), (False, "forbidden_tool_name"))


if __name__ == "__main__":
    unittest.main()
