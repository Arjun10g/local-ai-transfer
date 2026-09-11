"""Unit tests for scripts/model-artifact/validate_quality_corpus.py.

Every test builds a miniature corpus root in a temporary directory so no test
depends on the committed corpus growing. The committed corpus is exercised once,
separately, as a regression guard.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Floor written by the seed commit: 4 exemplars in each of the 13 category files.
SEED_CASES_PER_CATEGORY = 4
SEED_CASE_TOTAL = 52

# scripts/model-artifact is not an importable package name, so load the validator
# by path exactly as tests/performance/test_model_specs.py loads validate_specs.py.
_SPEC = importlib.util.spec_from_file_location(
    "validate_quality_corpus", REPO_ROOT / "scripts/model-artifact/validate_quality_corpus.py"
)
assert _SPEC and _SPEC.loader
V = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(V)


def _interactive(**overrides):
    settings = {
        "profile": "interactive",
        "mode": "normal",
        "temperature": 0,
        "max_output_tokens": 64,
        "enable_thinking": False,
    }
    settings.update(overrides)
    return settings


def _tool_settings(**overrides):
    return _interactive(profile="tool_selection", **overrides)


def _case(case_id, category, **overrides):
    case = {
        "id": case_id,
        "category": category,
        "difficulty": "easy",
        "split": V.derive_split(case_id),
        "messages": [{"role": "user", "content": "Return exactly the word READY."}],
        "settings": _interactive(),
        "expected": {
            "metric": "rubric_pass",
            "rubric": [{"id": "r1", "requirement": "Answer is the single word READY.", "weight": 1}],
            "pass_threshold": 1.0,
        },
    }
    case.update(overrides)
    if "split" not in overrides:
        case["split"] = V.derive_split(case["id"])
    return case


def _tool_case(case_id, **overrides):
    case = {
        "id": case_id,
        "category": "tool_selection_no_tool",
        "difficulty": "easy",
        "split": V.derive_split(case_id),
        "messages": [{"role": "user", "content": "Call system.get_info with no arguments."}],
        "tools": ["system.get_info"],
        "settings": _tool_settings(),
        "expected": {
            "metric": "exact_and_false_positive_rate",
            "call": {"name": "system.get_info", "arguments": {}},
        },
    }
    case.update(overrides)
    if "split" not in overrides:
        case["split"] = V.derive_split(case["id"])
    return case


class TemporaryCorpus:
    """A minimal but schema-complete corpus root the validator can be pointed at."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        spec = json.loads((REPO_ROOT / V.SPEC_RELATIVE).read_text(encoding="utf-8"))
        self.spec = spec
        (self.root / "model/quality-eval/schema").mkdir(parents=True)
        (self.root / "model/quality-eval/cases").mkdir(parents=True)
        (self.root / "tests/model").mkdir(parents=True)
        (self.root / V.SPEC_RELATIVE).write_text(json.dumps(spec), encoding="utf-8")
        for relative in (V.SCHEMA_RELATIVE, V.CATALOGUE_RELATIVE):
            (self.root / relative).write_bytes((REPO_ROOT / relative).read_bytes())
        for entry in spec["categories"]:
            self.write(entry["id"], [])

    def write(self, category, cases):
        document = {
            "schema_version": self.spec["schema_version"],
            "fixture_id": self.spec["fixture_id"],
            "category": category,
            "cases": cases,
        }
        path = self.root / V.CASES_RELATIVE / f"{category}.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def write_raw(self, category, document):
        path = self.root / V.CASES_RELATIVE / f"{category}.json"
        path.write_text(document, encoding="utf-8")
        return path

    def errors(self, **kwargs):
        return V.validate_corpus(self.root, **kwargs)[0]

    def close(self):
        self._tmp.cleanup()


class DerivationTests(unittest.TestCase):
    def test_split_is_deterministic_and_bucketed(self):
        self.assertEqual(V.derive_split("tool-xml-001"), V.derive_split("tool-xml-001"))
        self.assertIn(V.derive_split("instruction-001"), {"train", "dev", "test"})

    def test_split_distribution_over_many_ids_is_near_20_20_60(self):
        counts = {"train": 0, "dev": 0, "test": 0}
        for index in range(1, 4001):
            counts[V.derive_split(f"instruction-{index:04d}")] += 1
        self.assertAlmostEqual(100 * counts["train"] / 4000, 20, delta=3)
        self.assertAlmostEqual(100 * counts["dev"] / 4000, 20, delta=3)
        self.assertAlmostEqual(100 * counts["test"] / 4000, 60, delta=3)

    def test_category_slug_and_authoring_target(self):
        self.assertEqual(V.category_slug("tool_selection_no_tool"), "tool-selection-no-tool")
        self.assertEqual(V.authoring_target(200), 220)
        self.assertEqual(V.authoring_target(60), 66)


class JsonEqualityTests(unittest.TestCase):
    def test_booleans_are_never_numbers(self):
        self.assertFalse(V.json_equal(True, 1))
        self.assertFalse(V.json_equal(False, 0))
        self.assertTrue(V.json_equal(True, True))
        self.assertTrue(V.json_equal(0, 0.0))

    def test_nested_structures(self):
        self.assertTrue(V.json_equal({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}))
        self.assertFalse(V.json_equal({"a": [1]}, {"a": [1, 2]}))


class SchemaInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((REPO_ROOT / V.SCHEMA_RELATIVE).read_text(encoding="utf-8"))

    def test_schema_uses_only_supported_keywords(self):
        def walk(node):
            if isinstance(node, dict):
                unsupported = set(node) - V.SUPPORTED_KEYWORDS
                # Nested "properties"/"$defs" maps carry arbitrary member names.
                self.assertEqual(unsupported, set(), msg=f"unsupported keywords {sorted(unsupported)}")
                for keyword, child in node.items():
                    if keyword in {"properties", "$defs"}:
                        for sub in child.values():
                            walk(sub)
                    elif keyword in {"oneOf", "anyOf", "allOf"}:
                        for sub in child:
                            walk(sub)
                    elif keyword in {"items", "if", "then", "else", "propertyNames"}:
                        walk(child)
                    elif keyword == "additionalProperties" and isinstance(child, dict):
                        walk(child)
        walk(self.schema)

    def test_unsupported_keyword_raises(self):
        with self.assertRaises(V.SchemaError):
            V.validate_instance({}, {"multipleOf": 2}, {})

    def test_unresolvable_ref_raises(self):
        with self.assertRaises(V.SchemaError):
            V.validate_instance({}, {"$ref": "#/$defs/nope"}, {"$defs": {}})

    def test_valid_case_passes_the_schema(self):
        case_schema = V._resolve("#/$defs/case", self.schema)
        self.assertEqual(V.validate_instance(_tool_case("tool-xml-001"), case_schema, self.schema), [])

    def test_oneof_rejects_both_call_and_no_call(self):
        case_schema = V._resolve("#/$defs/case", self.schema)
        case = _tool_case("tool-selection-no-tool-001")
        case["expected"]["no_call"] = True
        errors = V.validate_instance(case, case_schema, self.schema)
        self.assertTrue(any("oneOf" in error for error in errors), errors)


class ValidFixtureTests(unittest.TestCase):
    def test_valid_minimal_corpus_passes(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        corpus.write("instruction", [_case("instruction-001", "instruction")])
        corpus.write("tool_selection_no_tool", [_tool_case("tool-xml-001")])
        self.assertEqual(corpus.errors(), [])

    def test_committed_corpus_passes_in_authoring_mode(self):
        # Bounds, not equalities: author lanes grow their own category file between
        # the seed commit and completion, so this test must tolerate growth while
        # still catching a lost file, a lost seed, or runaway generation.
        errors, rows = V.validate_corpus(REPO_ROOT)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 13)
        self.assertGreaterEqual(sum(row["count"] for row in rows), SEED_CASE_TOTAL)
        for row in rows:
            self.assertGreaterEqual(row["count"], SEED_CASES_PER_CATEGORY, msg=row["category"])

    def test_committed_corpus_never_exceeds_twice_the_spec_minimum(self):
        _, rows = V.validate_corpus(REPO_ROOT)
        for row in rows:
            self.assertLessEqual(
                row["count"],
                2 * row["minimum"],
                msg=f"{row['category']}: {row['count']} cases is more than twice the spec "
                    f"minimum of {row['minimum']}; check for runaway generation",
            )

    def test_committed_corpus_keeps_the_three_reserved_fixture_ids(self):
        _, rows = V.validate_corpus(REPO_ROOT)
        del rows
        found = {}
        for path in (REPO_ROOT / V.CASES_RELATIVE).glob("*.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            for case in document["cases"]:
                if case["id"] in V.LEGACY_CASE_IDS:
                    found[case["id"]] = case["category"]
        self.assertEqual(found, V.LEGACY_CASE_IDS)

    def test_committed_corpus_passes_require_complete(self):
        # Stage 2 is authored: every category is at or above its spec minimum and
        # both proportion checks hold, so the completion gate must now be clean.
        errors, _ = V.validate_corpus(REPO_ROOT, require_complete=True)
        self.assertEqual(errors, [])
        with contextlib.redirect_stdout(io.StringIO()):
            code = V.main(["--root", str(REPO_ROOT), "--require-complete"])
        self.assertEqual(code, 0)


class FailureClassTests(unittest.TestCase):
    def setUp(self):
        self.corpus = TemporaryCorpus()
        self.addCleanup(self.corpus.close)

    def assertFails(self, fragment, cases=None, category="instruction", raw=None):
        if raw is not None:
            self.corpus.write_raw(category, raw)
        else:
            self.corpus.write(category, cases)
        errors = self.corpus.errors()
        self.assertTrue(
            any(fragment in error for error in errors),
            msg=f"expected a finding containing {fragment!r}; got {errors}",
        )

    def test_invalid_json_file(self):
        self.assertFails("invalid JSON", raw="{not json")

    def test_unknown_case_property(self):
        case = _case("instruction-001", "instruction")
        case["owner"] = "luna"
        self.assertFails("unknown property 'owner'", [case])

    def test_missing_required_case_property(self):
        case = _case("instruction-001", "instruction")
        del case["difficulty"]
        self.assertFails("missing required property 'difficulty'", [case])

    def test_duplicate_ids_within_a_file(self):
        self.assertFails(
            "duplicate case id",
            [_case("instruction-001", "instruction"), _case("instruction-001", "instruction")],
        )

    def test_duplicate_ids_across_files(self):
        self.corpus.write("instruction", [_case("instruction-001", "instruction")])
        case = _case("instruction-001", "reasoning", expected={"metric": "exact", "answer": "4"})
        self.assertFails("duplicate case id", [case], category="reasoning")

    def test_id_prefix_must_match_category(self):
        self.assertFails("must start with the category slug", [_case("reasoning-001", "instruction")])

    def test_id_ordinal_must_be_zero_padded(self):
        self.assertFails("does not match pattern", [_case("instruction-1", "instruction")])

    def test_case_category_must_match_file(self):
        case = _case("instruction-001", "instruction")
        case["category"] = "reasoning"
        self.assertFails("does not match the file category", [case])

    def test_file_category_must_match_filename(self):
        document = json.dumps({
            "schema_version": self.corpus.spec["schema_version"],
            "fixture_id": self.corpus.spec["fixture_id"],
            "category": "reasoning",
            "cases": [],
        })
        self.assertFails("'category' must equal the file stem", raw=document)

    def test_fixture_id_must_match_the_spec(self):
        document = json.dumps({
            "schema_version": self.corpus.spec["schema_version"],
            "fixture_id": "some-other-fixture",
            "category": "instruction",
            "cases": [],
        })
        self.assertFails("value must equal", raw=document)

    def test_wrong_split_value(self):
        case = _case("instruction-001", "instruction")
        case["split"] = "train" if case["split"] != "train" else "dev"
        self.assertFails("sha256 bucket of the id", [case])

    def test_nonzero_temperature(self):
        case = _case("instruction-001", "instruction", settings=_interactive(temperature=0.7))
        self.assertFails("temperature: value must equal 0", [case])

    def test_missing_enable_thinking(self):
        settings = _interactive()
        del settings["enable_thinking"]
        self.assertFails("missing required property 'enable_thinking'",
                         [_case("instruction-001", "instruction", settings=settings)])

    def test_unbounded_output_tokens(self):
        case = _case("instruction-001", "instruction", settings=_interactive(max_output_tokens=4096))
        self.assertFails("above maximum 256", [case])

    def test_deep_mode_requires_thinking(self):
        case = _case("reasoning-001", "reasoning",
                     settings=_interactive(profile="deep", mode="deep"),
                     expected={"metric": "exact", "answer": "4"})
        self.assertFails("value must equal true", [case], category="reasoning")

    def test_deep_profile_rejected_outside_allowed_categories(self):
        case = _case("instruction-001", "instruction",
                     settings=_interactive(profile="deep", mode="deep", enable_thinking=True))
        self.assertFails("may not use the deep profile", [case])

    def test_tool_category_requires_tool_selection_profile(self):
        case = _tool_case("tool-xml-001", settings=_interactive())
        self.assertFails("requires the tool_selection or deep profile", [case],
                         category="tool_selection_no_tool")

    def test_tool_category_requires_declared_tools(self):
        case = _tool_case("tool-xml-001")
        del case["tools"]
        self.assertFails("missing required property 'tools'", [case], category="tool_selection_no_tool")

    def test_unknown_tool_name_in_tools_array(self):
        case = _tool_case("tool-xml-001", tools=["system.get_info", "web.search"])
        self.assertFails("'web.search' is not a production tool name", [case],
                         category="tool_selection_no_tool")

    def test_unknown_tool_name_in_expected_call(self):
        case = _tool_case("tool-xml-001")
        case["expected"]["call"]["name"] = "web.fetch_public"
        self.assertFails("is not a production tool name", [case], category="tool_selection_no_tool")

    def test_called_tool_must_be_declared(self):
        case = _tool_case("tool-xml-001", tools=["time.now"])
        self.assertFails("is not listed in the case 'tools' array", [case],
                         category="tool_selection_no_tool")

    def test_unknown_argument_name(self):
        case = _tool_case("tool-xml-001")
        case["expected"]["call"]["arguments"] = {"verbose": True}
        self.assertFails("has no argument(s)", [case], category="tool_selection_no_tool")

    def test_missing_required_argument(self):
        case = _tool_case("tool-arguments-001")
        case["category"] = "tool_arguments"
        case["tools"] = ["fs.read_text"]
        case["expected"] = {
            "metric": "exact_and_field_f1",
            "call": {"name": "fs.read_text", "arguments": {"workspace_id": "notes"}},
            "required_fields": ["workspace_id"],
            "min_field_f1": 1.0,
        }
        self.assertFails("requires ['path']", [case], category="tool_arguments")

    def test_required_fields_must_appear_in_expected_arguments(self):
        case = _tool_case("tool-arguments-001")
        case["category"] = "tool_arguments"
        case["tools"] = ["fs.read_text"]
        case["expected"] = {
            "metric": "exact_and_field_f1",
            "call": {"name": "fs.read_text", "arguments": {"workspace_id": "notes", "path": "a.md"}},
            "required_fields": ["workspace_id", "path", "max_bytes"],
            "min_field_f1": 1.0,
        }
        self.assertFails("absent from expected call arguments", [case], category="tool_arguments")

    def test_wrong_metric_for_category(self):
        case = _case("instruction-001", "instruction",
                     expected={"metric": "exact", "answer": "READY"})
        self.assertFails("value must equal", [case])

    def test_expected_shape_must_match_the_metric(self):
        case = _case("instruction-001", "instruction",
                     expected={"metric": "rubric_pass", "pass_threshold": 1.0})
        self.assertFails("missing required property 'rubric'", [case])

    def test_continuity_category_requires_follow_up(self):
        case = _case("continuity-reset-001", "continuity_reset", expected={
            "metric": "state_canary_and_pass",
            "canary": {"value": "ALPHA-17", "must_persist": True},
            "final_turn": {"must_equal": "ALPHA-17"},
        })
        self.assertFails("missing required property 'follow_up'", [case], category="continuity_reset")

    def test_adversarial_case_needs_a_negative_assertion(self):
        case = _case("instruction-001", "instruction", difficulty="adversarial")
        self.assertFails("must carry at least one negative assertion", [case])

    def test_uncompilable_regex_in_must_match(self):
        case = _case("instruction-001", "instruction")
        case["expected"]["must_match"] = ["(unclosed"]
        self.assertFails("not a compilable regular expression", [case])

    def test_long_input_must_fit_the_context_budget(self):
        case = _case("instruction-001", "instruction",
                     settings=_interactive(max_output_tokens=256, context_tokens=8192))
        case["messages"][0]["content"] = "Summarize:\n{{LONG_INPUT}}"
        case["long_input"] = {"generator": "numbered_lines", "target_tokens": 9000,
                              "seed": 1, "placeholder": "{{LONG_INPUT}}"}
        self.assertFails("exceeds context_tokens minus max_output_tokens", [case])

    def test_forbidden_email_domain(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Write to dana@acme-corp.com about the build."
        self.assertFails("is not a reserved example domain", [case])

    def test_allowed_example_email_domain_passes(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Write to dana@example.com about the build."
        self.corpus.write("instruction", [case])
        self.assertEqual(self.corpus.errors(), [])

    def test_forbidden_phone_number(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Call the owner on 415-628-4900 to confirm."
        self.assertFails("telephone-shaped literal", [case])

    def test_reserved_555_number_passes(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Call the fictional desk on 555-0142 to confirm."
        self.corpus.write("instruction", [case])
        self.assertEqual(self.corpus.errors(), [])

    def test_forbidden_credential_patterns(self):
        for secret in ("hf_abcdefghijklmnopqrstuvwx",
                       "sk-abcdefghijklmnopqrstuvwx",
                       "ghp_abcdefghijklmnopqrstuvwxyz01",
                       "AKIAIOSFODNN7EXAMPLE",
                       "-----BEGIN RSA PRIVATE KEY-----"):
            with self.subTest(secret=secret):
                case = _case("instruction-001", "instruction")
                case["messages"][0]["content"] = f"The value is {secret} and must not leak."
                self.assertFails("forbidden credential pattern", [case])

    def test_forbidden_external_url(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Fetch https://internal.acme-corp.net/build for the log."
        self.assertFails("not in the fictional allowlist", [case])

    def test_allowlisted_url_passes(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Fetch https://example.com/build for the log."
        self.corpus.write("instruction", [case])
        self.assertEqual(self.corpus.errors(), [])

    def test_forbidden_real_system_path(self):
        for path in ("C:\\Users\\dana\\notes.md", "/home/dana/notes.md", "%USERPROFILE%\\notes.md"):
            with self.subTest(path=path):
                case = _case("instruction-001", "instruction")
                case["messages"][0]["content"] = f"Open {path} and summarize it."
                self.assertFails("forbidden real-system path pattern", [case])

    def test_absolute_path_outside_the_fictional_root(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Open D:\\builds\\out.log and summarize it."
        self.assertFails("not under a fictional workspace root", [case])

    def test_fictional_workspace_root_passes(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Open W:\\demo-workspace\\notes.md and summarize it."
        self.corpus.write("instruction", [case])
        self.assertEqual(self.corpus.errors(), [])

    def test_non_english_without_lang(self):
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = "Translate this phrase: \u3053\u3093\u306b\u3061\u306f"
        self.assertFails("requires an explicit non-'en' case 'lang'", [case])

    def test_non_english_with_declared_lang_passes(self):
        case = _case("instruction-001", "instruction", lang="ja")
        case["messages"][0]["content"] = "Translate this phrase: \u3053\u3093\u306b\u3061\u306f"
        self.corpus.write("instruction", [case])
        self.assertEqual(self.corpus.errors(), [])

    def test_oversized_case_is_rejected(self):
        case = _case("instruction-001", "instruction")
        case["messages"] = [{"role": "user", "content": "A" * 24000} for _ in range(4)]
        self.assertFails("above the", [case])

    def test_missing_category_file(self):
        (self.corpus.root / V.CASES_RELATIVE / "reasoning.json").unlink()
        errors = self.corpus.errors()
        self.assertTrue(any("missing category file" in error for error in errors), errors)

    def test_unexpected_category_file(self):
        (self.corpus.root / V.CASES_RELATIVE / "writing.json").write_text("{}", encoding="utf-8")
        errors = self.corpus.errors()
        self.assertTrue(any("no such category" in error for error in errors), errors)

    def test_stray_non_json_file(self):
        (self.corpus.root / V.CASES_RELATIVE / "notes.md").write_text("draft", encoding="utf-8")
        errors = self.corpus.errors()
        self.assertTrue(any("only <category>.json files" in error for error in errors), errors)

    def test_reserved_id_pinned_to_its_category(self):
        case = _case("thinking-off-001", "instruction")
        self.assertFails("reserved fixture id belongs to category", [case])


class ProportionTests(unittest.TestCase):
    def setUp(self):
        self.corpus = TemporaryCorpus()
        self.addCleanup(self.corpus.close)

    def _bulk(self, count, difficulty_cycle=("easy", "medium", "hard", "adversarial")):
        cases = []
        for index in range(1, count + 1):
            difficulty = difficulty_cycle[index % len(difficulty_cycle)]
            case = _case(f"instruction-{index:03d}", "instruction", difficulty=difficulty)
            if difficulty == "adversarial":
                case["expected"]["must_not_contain"] = ["Rule disabled."]
            cases.append(case)
        return cases

    def test_natural_hash_split_passes_the_proportion_check(self):
        self.corpus.write("instruction", self._bulk(120))
        errors = [e for e in self.corpus.errors() if "proportion" in e]
        self.assertEqual(errors, [])

    def test_skewed_split_fails_the_proportion_check(self):
        cases = self._bulk(160)
        kept = [case for case in cases if case["split"] == "test"]
        self.corpus.write("instruction", kept)
        errors = [e for e in self.corpus.errors() if "split proportion" in e]
        self.assertTrue(errors, "an all-test category must fail the split proportion check")

    def test_skewed_difficulty_fails_the_mix_check(self):
        self.corpus.write("instruction", self._bulk(60, difficulty_cycle=("easy",)))
        errors = [e for e in self.corpus.errors() if "difficulty proportion" in e]
        self.assertTrue(errors, "an all-easy category must fail the difficulty mix check")

    def test_minimums_are_reported_but_only_enforced_with_require_complete(self):
        self.corpus.write("instruction", [_case("instruction-001", "instruction")])
        self.assertFalse(any("below the spec minimum" in e for e in self.corpus.errors()))
        strict = self.corpus.errors(require_complete=True)
        self.assertTrue(any("below the spec minimum" in e for e in strict))



def _fact(text, **match):
    base = {"any_of": [text, {"regex": "(?=(?s:.)*\\bfact\\w*)"}],
            "normalize": ["lowercase", "collapse_ws", "strip_punct", "numerals"]}
    base.update(match)
    return {"text": text, "match": base}


def _summarization_case(case_id, **overrides):
    case = {
        "id": case_id,
        "category": "summarization",
        "difficulty": "easy",
        "split": V.derive_split(case_id),
        "messages": [{"role": "user", "content": "Summarize the note in at most 20 words."}],
        "settings": _interactive(),
        "expected": {
            "metric": "key_fact_coverage_and_hallucination",
            "key_facts": [_fact("the hash matched"), _fact("the smoke test passed")],
            "min_coverage": 1.0,
            "forbidden_facts": [],
            "max_words": 20,
        },
    }
    case.update(overrides)
    if "split" not in overrides:
        case["split"] = V.derive_split(case["id"])
    return case


class NormalizeTests(unittest.TestCase):
    def test_steps_apply_in_canonical_order_regardless_of_list_order(self):
        text = "  The   Artifact HASH, matched!  "
        forward = V.normalize_text(text, ["lowercase", "collapse_ws", "strip_punct"])
        reverse = V.normalize_text(text, ["strip_punct", "collapse_ws", "lowercase"])
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, "the artifact hash matched")

    def test_each_step_in_isolation(self):
        self.assertEqual(V.normalize_text("AbC", ["lowercase"]), "abc")
        self.assertEqual(V.normalize_text(" a \n b ", ["collapse_ws"]), "a b")
        self.assertEqual(V.normalize_text("a,b.c", ["strip_punct", "collapse_ws"]), "a b c")
        self.assertEqual(V.normalize_text("four crashes", ["numerals"]), "4 crashes")

    def test_no_steps_is_the_identity(self):
        raw = "  Line one\n- Bullet!  "
        self.assertEqual(V.normalize_text(raw, []), raw)

    def test_numerals_respects_word_boundaries(self):
        self.assertEqual(V.normalize_text("someone atoned", ["numerals"]), "someone atoned")


class MatcherSemanticsTests(unittest.TestCase):
    STEPS = ["lowercase", "collapse_ws", "strip_punct", "numerals"]

    def test_string_variant_is_a_substring_match_on_the_normalized_answer(self):
        match = {"any_of": ["hash matched", "digest agreed"], "normalize": self.STEPS}
        self.assertTrue(V.match_item("The HASH, matched cleanly.", match))
        self.assertFalse(V.match_item("The build failed.", match))

    def test_regex_variant_uses_search_and_is_not_normalized(self):
        match = {"any_of": [{"regex": r"(?=(?s:.)*\bhash\w*)(?=(?s:.)*\bmatch\w*)"}],
                 "normalize": self.STEPS}
        self.assertTrue(V.match_item("Matching digests: the hash is fine.", match))
        self.assertFalse(V.match_item("The hash is missing.", match))

    def test_any_of_is_a_disjunction(self):
        match = {"any_of": ["alpha", "beta"], "normalize": ["lowercase"]}
        self.assertTrue(V.match_item("BETA only", match))
        self.assertTrue(V.match_item("ALPHA only", match))
        self.assertFalse(V.match_item("gamma only", match))

    def test_forbidden_semantics_are_the_caller_inverting_match_item(self):
        match = {"any_of": ["approved by the security team"], "normalize": self.STEPS}
        self.assertFalse(V.match_item("The hash matched.", match))       # forbidden item passes
        self.assertTrue(V.match_item("Approved by the Security Team.", match))  # violation

    def test_numeral_word_forms_unify(self):
        match = {"any_of": ["4 crashes"], "normalize": self.STEPS}
        self.assertTrue(V.match_item("There were four crashes.", match))
        self.assertTrue(V.match_item("There were 4 crashes.", match))

    def test_raw_structural_regex_sees_newlines_and_markers(self):
        match = {"any_of": [{"regex": r"\A(?:- [^\n]*(?:\n|\Z)){3}\Z"}], "normalize": []}
        self.assertTrue(V.match_item("- a\n- b\n- c", match))
        self.assertFalse(V.match_item("- a\n- b", match))

    def test_absence_predicate_via_lookahead(self):
        match = {"any_of": [{"regex": r"\A(?!(?s:.)*\bsimply\b)(?!(?s:.)*\bjust\b)(?s:.)*\Z"}],
                 "normalize": ["lowercase"]}
        self.assertTrue(V.match_item("Read the file and report.", match))
        self.assertFalse(V.match_item("Simply read the file.", match))


class MatchObjectValidationTests(unittest.TestCase):
    def test_empty_any_of_is_rejected(self):
        errors = V.check_match_object({"any_of": [], "normalize": []}, "p")
        self.assertTrue(any("at least one variant" in e for e in errors), errors)

    def test_missing_any_of_is_rejected(self):
        self.assertTrue(V.check_match_object({"normalize": []}, "p"))

    def test_uncompilable_regex_is_rejected(self):
        errors = V.check_match_object({"any_of": [{"regex": "(unclosed"}], "normalize": []}, "p")
        self.assertTrue(any("does not compile" in e for e in errors), errors)

    def test_non_string_variant_is_rejected(self):
        errors = V.check_match_object({"any_of": ["  "], "normalize": []}, "p")
        self.assertTrue(any("non-empty string" in e for e in errors), errors)

    def test_normalized_matcher_needs_two_variants(self):
        errors = V.check_match_object({"any_of": ["only one"], "normalize": ["lowercase"]}, "p")
        self.assertTrue(any("at least two variants" in e for e in errors), errors)

    def test_raw_structural_matcher_may_carry_one_variant(self):
        self.assertEqual(V.check_match_object({"any_of": [{"regex": r"\A.\Z"}], "normalize": []}, "p"), [])

    def test_non_object_match_is_rejected(self):
        self.assertTrue(V.check_match_object("nope", "p"))


class FactMatchCorpusTests(unittest.TestCase):
    def test_schema_requires_match_on_every_fact(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        case = _summarization_case("summarization-001")
        case["expected"]["key_facts"] = ["a bare string is no longer a fact"] * 2
        corpus.write("summarization", [case])
        errors = corpus.errors()
        self.assertTrue(any("expected type object" in e for e in errors), errors)

    def test_bad_regex_inside_a_fact_is_reported(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        case = _summarization_case("summarization-001")
        case["expected"]["key_facts"][0]["match"]["any_of"] = [{"regex": "(oops"}, "fallback"]
        corpus.write("summarization", [case])
        self.assertTrue(any("does not compile" in e for e in corpus.errors()))

    def test_valid_fact_shape_passes(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        corpus.write("summarization", [_summarization_case("summarization-001")])
        self.assertEqual(corpus.errors(), [])

    def test_committed_summarization_facts_all_carry_a_self_matching_matcher(self):
        path = REPO_ROOT / V.CASES_RELATIVE / "summarization.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        items = 0
        for case in document["cases"]:
            for key in ("key_facts", "forbidden_facts"):
                for item in case["expected"][key]:
                    self.assertIn("text", item, msg=case["id"])
                    self.assertIn("match", item, msg=case["id"])
                    self.assertEqual(V.check_match_object(item["match"], case["id"]), [])
                    # The proposition itself must satisfy its own matcher, or the
                    # matcher does not describe the proposition.
                    self.assertTrue(V.match_item(item["text"], item["match"]),
                                    msg=f"{case['id']}: {key} matcher does not match its own text")
                    items += 1
        self.assertGreaterEqual(items, 271)

class CatalogueTests(unittest.TestCase):
    def test_production_catalogue_is_thirty_three_tools(self):
        catalogue = V.tool_catalogue(REPO_ROOT)
        self.assertEqual(len(catalogue), 33)
        self.assertIn("system.get_info", catalogue)
        self.assertEqual(catalogue["fs.read_text"]["required"], {"workspace_id", "path"})

    def test_corpus_only_references_catalogue_tools(self):
        catalogue = set(V.tool_catalogue(REPO_ROOT))
        for path in sorted((REPO_ROOT / V.CASES_RELATIVE).glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            for case in document["cases"]:
                for name in case.get("tools", []):
                    self.assertIn(name, catalogue, msg=f"{case['id']}: {name}")


class CliTests(unittest.TestCase):
    def _run(self, argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = V.main(argv)
        return code, stream.getvalue()

    def test_committed_corpus_exits_zero_and_prints_a_table(self):
        code, output = self._run(["--root", str(REPO_ROOT)])
        self.assertEqual(code, 0, output)
        self.assertIn("quality corpus: PASS", output)
        self.assertIn("tool_selection_no_tool", output)
        self.assertIn("TOTAL", output)
        self.assertIn("1180", output)

    def test_require_complete_exits_zero_on_the_authored_corpus(self):
        code, output = self._run(["--root", str(REPO_ROOT), "--require-complete"])
        self.assertEqual(code, 0, output)
        self.assertIn("quality corpus: PASS", output)
        self.assertIn("require-complete", output)

    def test_require_complete_exits_one_on_an_under_filled_corpus(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        code, output = self._run(["--root", str(corpus.root), "--require-complete"])
        self.assertEqual(code, 1)
        self.assertIn("quality corpus: FAIL", output)
        self.assertIn("below the spec minimum", output)

    def test_broken_corpus_exits_one(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        corpus.write_raw("instruction", "{oops")
        code, output = self._run(["--root", str(corpus.root)])
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", output)

    def test_missing_corpus_directory_exits_one(self):
        with tempfile.TemporaryDirectory() as empty:
            root = Path(empty)
            (root / "model/quality-eval").mkdir(parents=True)
            (root / "tests/model").mkdir(parents=True)
            (root / V.SPEC_RELATIVE).write_bytes((REPO_ROOT / V.SPEC_RELATIVE).read_bytes())
            (root / V.CATALOGUE_RELATIVE).write_bytes((REPO_ROOT / V.CATALOGUE_RELATIVE).read_bytes())
            (root / "model/quality-eval/schema").mkdir()
            (root / V.SCHEMA_RELATIVE).write_bytes((REPO_ROOT / V.SCHEMA_RELATIVE).read_bytes())
            code, output = self._run(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("missing corpus directory", output)


class LoggingPolicyTests(unittest.TestCase):
    def test_diagnostics_never_echo_prompt_or_expectation_text(self):
        corpus = TemporaryCorpus()
        self.addCleanup(corpus.close)
        secret_prompt = "PROMPTBODYCANARY that must never be echoed by a diagnostic"
        case = _case("instruction-001", "instruction")
        case["messages"][0]["content"] = secret_prompt
        case["split"] = "train" if case["split"] != "train" else "dev"
        corpus.write("instruction", [case])
        errors = corpus.errors()
        self.assertTrue(errors)
        for error in errors:
            self.assertNotIn("PROMPTBODYCANARY", error)

    def test_spec_never_record_list_is_unchanged(self):
        spec = json.loads((REPO_ROOT / V.SPEC_RELATIVE).read_text(encoding="utf-8"))
        self.assertEqual(
            spec["logging"]["never_record"],
            ["full_prompts", "full_responses", "tool_payloads", "model_weights", "credentials"],
        )
        self.assertTrue(spec["logging"]["record_case_ids_and_metrics"])


if __name__ == "__main__":
    unittest.main()
