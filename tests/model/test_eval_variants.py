"""Contract tests for the deterministic tool-call variant fixture.

No model, engine, network or subprocess beyond a local ``--check`` rebuild.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.test import build_eval_variants as bev
from scripts.test.evaluate_tool_calls import (
    CATEGORIES,
    MAX_EVAL_CASES,
    QUALITY_CODES,
    evaluate_case,
    load_fixture,
    validate_fixture,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SHIPPING = HERE / "production_tool_call_eval.json"
VARIANTS = HERE / "tool_call_eval_variants.json"

EXPECTED_KIND_COUNTS = {"para": 74, "bool": 8, "enum": 11, "int": 36, "nearmiss": 16,
                        "optarg": 17, "order": 4, "abstain": 14}
EXPECTED_CATEGORY_COUNTS = {"abstention": 9, "argument_fidelity": 76, "confirmation_sensitive": 30,
                            "no_tool": 8, "prompt_injection": 3, "schema_edge": 2, "tool_selection": 52}


def render_call(name, arguments, boolean_style="json"):
    """Render a call the way the pinned Qwen3.5 template writes it."""

    lines = ["<tool_call>", f"<function={name}>"]
    for key, value in arguments.items():
        if isinstance(value, bool):
            text = ("true" if value else "false") if boolean_style == "json" else ("True" if value else "False")
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value)
        lines += [f"<parameter={key}>", text, "</parameter>"]
    lines += ["</function>", "</tool_call>"]
    return "\n".join(lines)


def _words(text):
    return set(re.findall(r"[a-z0-9_@.:/-]+", text.casefold()))


def _normal(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


class EvalVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shipping_raw = SHIPPING.read_bytes()
        cls.shipping = load_fixture(SHIPPING)
        cls.container = bev.load_container(VARIANTS)
        cls.cases = [case for chunk in cls.container["chunks"] for case in chunk["cases"]]
        cls.by_id = {case["id"]: case for case in cls.cases}
        cls.originals = {case["id"]: case for case in cls.shipping["cases"]}
        cls.tools = cls.shipping["tools"]
        cls.functions = {tool["function"]["name"]: tool["function"] for tool in cls.tools}

    def kind(self, case):
        return bev.parse_variant_id(case["id"])[1]

    def of_kind(self, kind):
        return [case for case in self.cases if self.kind(case) == kind]

    # -- determinism -------------------------------------------------------
    def test_regeneration_is_byte_identical(self):
        first = bev.build_container_text()
        second = bev.build_container_text()
        self.assertEqual(first, second)
        self.assertEqual(first.encode("utf-8"), VARIANTS.read_bytes())

    def test_check_mode_is_independent_of_hash_seed(self):
        for seed in ("0", "4242"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "test" / "build_eval_variants.py"), "--check"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_container_pins_the_current_shipping_fixture(self):
        self.assertEqual(self.container["source_sha256"], hashlib.sha256(self.shipping_raw).hexdigest())
        self.assertEqual(self.container["source_fixture"], "tests/model/production_tool_call_eval.json")

    # -- validator compatibility -------------------------------------------
    def test_every_chunk_validates_and_respects_the_case_ceiling(self):
        self.assertEqual(MAX_EVAL_CASES, 64)
        self.assertGreater(len(self.cases), MAX_EVAL_CASES, "the set needs chunking at all")
        with tempfile.TemporaryDirectory() as scratch:
            for index, chunk in enumerate(self.container["chunks"], start=1):
                validate_fixture(chunk)
                self.assertLessEqual(len(chunk["cases"]), MAX_EVAL_CASES)
                path = Path(scratch) / f"chunk-{index}.json"
                path.write_text(bev.extract_chunk_text(index, VARIANTS), encoding="utf-8")
                loaded = load_fixture(path)
                self.assertEqual(loaded["cases"], chunk["cases"])
        merged = dict(self.container["chunks"][0])
        merged["cases"] = self.cases
        with self.assertRaisesRegex(ValueError, "fixture_cases_invalid|fixture_array_unbounded"):
            validate_fixture(merged)

    def test_ids_are_unique_stable_and_carry_lineage(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for case_id in ids:
            origin, kind, detail = bev.parse_variant_id(case_id)
            self.assertIn(kind, bev.KINDS, case_id)
            self.assertTrue(detail, case_id)
            if kind == "abstain":
                self.assertEqual(origin, bev.NEW_ORIGIN)
            else:
                self.assertIn(origin, self.originals, case_id)
        self.assertEqual(set(self.cases[0]), {"id", "category", "messages", "expected"})

    def test_tool_definitions_are_byte_identical_to_shipping(self):
        _, shipping_tool_lines = bev._source_layout(self.shipping_raw)
        self.assertEqual(len(shipping_tool_lines), 33)
        shipping_compact = json.dumps(self.tools, separators=(",", ":"))
        for index, chunk in enumerate(self.container["chunks"], start=1):
            self.assertEqual(json.dumps(chunk["tools"], separators=(",", ":")), shipping_compact)
            self.assertEqual(json.dumps(chunk["tools"]), json.dumps(self.tools))  # wire form
            for key in ("schema", "model", "protocol", "limits"):
                self.assertEqual(chunk[key], self.shipping[key])
            extracted = bev.extract_chunk_text(index, VARIANTS).encode("utf-8")
            _, extracted_tool_lines = bev._source_layout(extracted)
            self.assertEqual(extracted_tool_lines, shipping_tool_lines)
            self.assertEqual(extracted.split(b'  "cases": [')[0], self.shipping_raw.split(b'  "cases": [')[0])

    # -- scorer consistency ------------------------------------------------
    def test_every_expected_call_scores_exact_when_rendered(self):
        calls = [case for case in self.cases if "call" in case["expected"]]
        self.assertGreater(len(calls), 100)
        for case in calls:
            call = case["expected"]["call"]
            output = render_call(call["name"], call["arguments"])
            self.assertEqual(evaluate_case(case, output, self.tools), (True, "exact_call"), case["id"])

    def test_python_boolean_spelling_is_coerced_and_both_values_pass(self):
        seen = set()
        for case in self.cases:
            call = case["expected"].get("call")
            if not call or not any(isinstance(value, bool) for value in call["arguments"].values()):
                continue
            output = render_call(call["name"], call["arguments"], boolean_style="python")
            self.assertTrue("True" in output or "False" in output)
            self.assertEqual(evaluate_case(case, output, self.tools), (True, "exact_call"), case["id"])
            for key, value in call["arguments"].items():
                if isinstance(value, bool):
                    seen.add((call["name"], key, value))
                    flipped = render_call(call["name"], {**call["arguments"], key: not value})
                    self.assertEqual(evaluate_case(case, flipped, self.tools), (False, "argument_value_mismatch"))
        for name, key in (("mail.list_messages", "unread_only"), ("mail.mark_read", "is_read")):
            self.assertIn((name, key, True), seen)
            self.assertIn((name, key, False), seen)

    def test_no_call_cases_pass_on_silence_and_fail_on_any_call(self):
        no_calls = [case for case in self.cases if "no_call" in case["expected"]]
        donor = {case["expected"]["call"]["name"]: case["expected"]["call"]["arguments"]
                 for case in self.shipping["cases"] if "call" in case["expected"]}
        for case in no_calls:
            self.assertEqual(evaluate_case(case, "", self.tools), (True, "no_call"), case["id"])
            self.assertEqual(evaluate_case(case, "I can't do that with the available tools.", self.tools), (True, "no_call"))
            emitted = render_call("clipboard.read", {})
            self.assertEqual(evaluate_case(case, emitted, self.tools), (False, "unexpected_call"), case["id"])
            for name in case["expected"].get("forbid_names", []):
                output = render_call(name, donor[name])
                self.assertEqual(evaluate_case(case, output, self.tools), (False, "forbidden_tool_name"), case["id"])

    def test_abstention_kind_has_ten_to_fifteen_cases_of_each_shape(self):
        abstain = self.of_kind("abstain")
        self.assertTrue(10 <= len(abstain) <= 15)
        self.assertTrue(all(case["expected"].get("no_call") is True for case in abstain))
        categories = {case["category"] for case in abstain}
        self.assertTrue({"no_tool", "abstention", "prompt_injection"} <= categories)
        mentions_tool = [case for case in abstain if any(name in case["messages"][0]["content"] for name in self.functions)]
        self.assertTrue(mentions_tool, "a request naming a tool but needing nothing")

    # -- content quality ---------------------------------------------------
    def test_paraphrases_are_genuinely_different(self):
        prompts = [case["messages"][0]["content"] for case in self.cases]
        self.assertEqual(len(prompts), len(set(map(_normal, prompts))), "every prompt is distinct")
        shipping_prompts = {_normal(case["messages"][0]["content"]) for case in self.shipping["cases"]}
        self.assertFalse(shipping_prompts & set(map(_normal, prompts)))
        para = self.of_kind("para")
        by_origin = {}
        for case in para:
            by_origin.setdefault(bev.parse_variant_id(case["id"])[0], []).append(case)
        self.assertEqual(set(by_origin), set(self.originals))
        for origin, variants in by_origin.items():
            self.assertGreaterEqual(len(variants), 2)
            original = self.originals[origin]
            texts = [original["messages"][0]["content"]] + [case["messages"][0]["content"] for case in variants]
            for i, left in enumerate(texts):
                for right in texts[i + 1:]:
                    a, b = _words(left), _words(right)
                    self.assertLess(len(a & b) / len(a | b), 0.8, (origin, left, right))
            for case in variants:
                self.assertEqual(case["expected"], original["expected"], case["id"])
                self.assertEqual(case["category"], original["category"], case["id"])

    def test_prompts_state_every_expected_value(self):
        for case in self.cases:
            call = case["expected"].get("call")
            if not call:
                continue
            prompt = case["messages"][0]["content"]
            for key, value in call["arguments"].items():
                if isinstance(value, bool):
                    # Where the prompt names the parameter, the word it gives
                    # must be the expected JSON boolean.
                    for word in re.findall(rf"{re.escape(key)}(?: is| set to)? (true|false)", prompt):
                        self.assertEqual(word, "true" if value else "false", (case["id"], key))
                    continue
                if isinstance(value, int):
                    self.assertRegex(prompt, rf"(?<![0-9]){value}(?![0-9])", (case["id"], key))
                elif isinstance(value, str):
                    if value:
                        self.assertIn(value, prompt, (case["id"], key))
                elif isinstance(value, list):
                    for item in value:
                        self.assertIn(item, prompt, (case["id"], key))
                else:
                    self.assertIn(json.dumps(value), prompt, (case["id"], key))

    def test_near_misses_discriminate_named_confusions(self):
        pairs = set()
        for case in self.of_kind("nearmiss"):
            wanted = case["expected"]["call"]["name"]
            forbidden = case["expected"]["forbid_names"]
            self.assertEqual(len(forbidden), 1)
            self.assertNotEqual(wanted, forbidden[0])
            self.assertIn(forbidden[0], self.functions)
            pairs.add((wanted, forbidden[0]))
            wrong = render_call(forbidden[0], {})
            self.assertFalse(evaluate_case(case, wrong, self.tools)[0])
        for left, right in (("fs.list", "fs.search_text"), ("mail.list_messages", "mail.search_messages")):
            self.assertIn((left, right), pairs)
            self.assertIn((right, left), pairs)
        self.assertIn(("browser.open_url", "browser.inspect_page"), pairs)
        self.assertIn(("browser.inspect_page", "browser.open_url"), pairs)

    def test_every_boolean_enum_and_integer_parameter_is_swept(self):
        observed = {}
        for case in self.cases:
            call = case["expected"].get("call")
            if call and self.kind(case) in {"bool", "enum", "int"}:
                for key, value in call["arguments"].items():
                    observed.setdefault((call["name"], key), set()).add(json.dumps(value))
        for name, function in self.functions.items():
            for key, schema in function["parameters"]["properties"].items():
                if schema.get("type") == "boolean":
                    wanted = {"true", "false"}
                elif "enum" in schema:
                    wanted = {json.dumps(value) for value in schema["enum"]}
                elif schema.get("type") == "integer":
                    wanted = {json.dumps(schema["minimum"]), json.dumps(schema["maximum"])}
                else:
                    continue
                self.assertLessEqual(wanted, observed.get((name, key), set()), (name, key))

    def test_optional_argument_shapes_are_present(self):
        optarg = self.of_kind("optarg")
        absent_optional = extra_optional = 0
        for case in optarg:
            call = case["expected"]["call"]
            original = next(c for c in self.shipping["cases"] if c["expected"].get("call", {}).get("name") == call["name"])
            base = set(original["expected"]["call"]["arguments"])
            absent_optional += bool(base - set(call["arguments"]))
            extra_optional += bool(set(call["arguments"]) - base)
        self.assertGreaterEqual(absent_optional, 5)
        self.assertGreaterEqual(extra_optional, 5)

    def test_counts_and_category_mix_are_as_documented(self):
        self.assertEqual(len(self.cases), 180)
        self.assertEqual(self.container["case_count"], 180)
        self.assertEqual({kind: len(self.of_kind(kind)) for kind in bev.KINDS}, EXPECTED_KIND_COUNTS)
        self.assertEqual(self.container["kind_counts"], EXPECTED_KIND_COUNTS)
        categories = {}
        for case in self.cases:
            categories[case["category"]] = categories.get(case["category"], 0) + 1
        self.assertEqual(categories, EXPECTED_CATEGORY_COUNTS)
        self.assertLessEqual(set(categories), CATEGORIES)
        self.assertEqual([len(chunk["cases"]) for chunk in self.container["chunks"]], [60, 60, 60])
        for chunk in self.container["chunks"]:
            self.assertEqual({self.kind(case) for case in chunk["cases"]}, set(bev.KINDS))

    # -- attribution -------------------------------------------------------
    def test_summarize_by_origin_attributes_failures(self):
        aggregate = {"failed_cases": [
            {"id": "v.prod-mail-list-001.bool.unread_only-false-01", "category": "argument_fidelity", "reason": "argument_type_mismatch"},
            {"id": "v.prod-mail-list-001.para.02", "category": "tool_selection", "reason": "wrong_tool"},
            {"id": "v.new.abstain.03", "category": "no_tool", "reason": "unexpected_call"},
            {"id": "prod-mail-read-state-001", "category": "confirmation_sensitive", "reason": "malformed_call"},
        ]}
        summary = bev.summarize_by_origin(aggregate)
        self.assertEqual(summary["failed_count"], 4)
        self.assertEqual(summary["cases"]["v.prod-mail-list-001.bool.unread_only-false-01"],
                         {"origin": "prod-mail-list-001", "kind": "bool", "detail": "unread_only-false-01",
                          "reason": "argument_type_mismatch"})
        self.assertEqual(summary["by_origin"]["prod-mail-list-001"], {"bool": 1, "para": 1})
        self.assertEqual(summary["by_origin"]["new"], {"abstain": 1})
        self.assertEqual(summary["by_kind"]["original"], {"malformed_call": 1})
        self.assertEqual(summary["by_kind"]["abstain"], {"unexpected_call": 1})
        run_local_shape = {"cases": [
            {"id": "v.prod-time-001.enum.format-iso", "status": "fail", "reason": "argument_value_mismatch"},
            {"id": "v.prod-time-001.enum.format-utc", "status": "pass", "reason": "exact_call"},
            {"id": "v.prod-time-001.enum.format-local", "status": "error", "reason": "http_500"},
        ]}
        summary = bev.summarize_by_origin(run_local_shape)
        self.assertEqual(summary["failed_count"], 2)
        self.assertEqual(summary["by_kind"], {"enum": {"argument_value_mismatch": 1, "http_500": 1}})
        self.assertEqual(bev.summarize_by_origin({"failed_cases": []})["failed_count"], 0)

    def test_scorer_failure_taxonomy_is_the_one_summarized(self):
        for code in ("wrong_tool", "missing_argument", "extra_argument", "argument_type_mismatch",
                     "argument_value_mismatch", "malformed_call", "missing_call", "unexpected_call",
                     "forbidden_tool_name"):
            self.assertIn(code, QUALITY_CODES)


if __name__ == "__main__":
    unittest.main()
