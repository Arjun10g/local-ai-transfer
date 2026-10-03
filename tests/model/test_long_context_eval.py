"""The long-context evaluator, driven end to end against a deterministic fake engine.

Nothing here measures the real model. It proves the instrument: facts land
where the receipt says they did, grading is exact, the engine's limits are
recorded as findings rather than failures or crashes, nothing the model saw
or said reaches the receipt, and a known "effective window" produces the
accuracy-vs-size curve it should.
"""

import io
import json
import os
import random
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.test import long_context_eval as lce
from tests.model.long_context_fake_engine import FACT, SECRET_OUTPUT_MARKER, FakeLongContextEngine

PROMPT_MARKER = "LCX-SECRET-PROMPT-FILLER-91c4"


def run_eval(fake: FakeLongContextEngine, *extra: str, out: Path) -> tuple[int, dict, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["--endpoint", fake.base + "/v1/chat/completions", "--out", str(out), *extra]
    with mock.patch.dict(os.environ, {"LAE_EVAL_TOKEN": fake.token}), redirect_stdout(stdout), redirect_stderr(stderr):
        code = lce.main(argv)
    return code, json.loads(out.read_text(encoding="utf-8")), stdout.getvalue(), stderr.getvalue()


def build(probe: str, style: str, size: int, depth: float, trial: int = 0):
    tools = lce.load_probe_tools()
    seed = f"{probe}|{style}|{size}|{depth}|{trial}"
    case = lce.build_case(probe, random.Random(seed), depth, tools)
    return case, lce.assemble(case, style, size, seed, lce.DEFAULT_CHARS_PER_TOKEN, tools)


class ProbeConstructionTests(unittest.TestCase):
    def test_facts_are_planted_at_the_requested_depths(self):
        for probe in lce.PROBES:
            for style in lce.STYLES:
                for depth in (0.1, 0.5, 0.9):
                    with self.subTest(probe=probe, style=style, depth=depth):
                        case, built = build(probe, style, 4000, depth)
                        text = "\n".join(m["content"] for m in built.messages)
                        for needle in built.needles:
                            value = needle.value if hasattr(needle, "value") else needle.total
                            self.assertIn(value, text, "every planted value is in the prompt")
                            self.assertLessEqual(abs(needle.planted_depth - needle.depth), 0.1,
                                                 f"{needle.role} planted at {needle.planted_depth}")
                        targets = [n for n in built.needles if n.role in ("target", "stale")]
                        self.assertEqual(targets[0].depth, depth)
                        self.assertLessEqual(len(built.messages), 64 - 2, "engine's 64-message bound, with loop room")

    def test_content_is_deterministic_and_trials_differ(self):
        a = build("needle", "turns", 2000, 0.5)[1].messages
        self.assertEqual(a, build("needle", "turns", 2000, 0.5)[1].messages)
        self.assertNotEqual(a, build("needle", "turns", 2000, 0.5, trial=1)[1].messages)

    def test_filler_is_mixed_and_never_mimics_a_fact(self):
        _, built = build("needle", "turns", 6000, 0.5)
        roles = {m["role"] for m in built.messages}
        self.assertEqual(roles, {"user", "assistant", "tool"})
        text = "\n".join(m["content"] for m in built.messages)
        self.assertIn("```python", text)
        self.assertIn('"routes"', text)
        self.assertEqual(len(FACT.findall(text)), 1, "only the needle states a fact")

    def test_cells_run_smallest_size_first(self):
        opts = mock.Mock(sizes=(4000, 1000, 2000), trials=1, styles=("turns",), depths=(0.5,), probes=("needle",))
        self.assertEqual([c["target_tokens"] for c in lce.plan_cells(opts)], [1000, 2000, 4000])


class ExactGradingTests(unittest.TestCase):
    def test_recall_needs_the_exact_value(self):
        grade = lce._grade_single("AMBER-FALCON-4821", ["COBALT-ONYX-1111"])
        self.assertEqual(grade("AMBER-FALCON-4821"), (True, "recalled"))
        self.assertEqual(grade("The code word is amber-falcon-4821."), (True, "recalled"))
        self.assertEqual(grade("AMBER-FALCON-4822"), (False, "missing_fact"))
        self.assertEqual(grade("AMBER-FALCON-48210"), (False, "missing_fact"))
        self.assertEqual(grade("XAMBER-FALCON-4821"), (False, "missing_fact"))
        self.assertEqual(grade("AMBER-FALCON-4821 or COBALT-ONYX-1111"), (False, "distractor_answer"))
        self.assertEqual(lce._grade_single("73914", [])("It is 73,914."), (True, "recalled"))
        self.assertEqual(lce._grade_single("73914", [])("739145"), (False, "missing_fact"))

    def test_latest_value_rejects_the_stale_one(self):
        case = lce.build_case("latest_value", random.Random("x"), 0.3, [])
        stale, current = case.needles
        self.assertEqual(case.grade(current.value), (True, "latest_value"))
        self.assertEqual(case.grade(stale.value), (False, "stale_value"))
        self.assertEqual(case.grade(f"{stale.value}, now {current.value}"), (False, "stale_value"))
        self.assertEqual(case.grade("R-000"), (False, "missing_fact"))

    def test_multi_needle_needs_all_three_and_no_distractor(self):
        case = lce.build_case("multi_needle", random.Random("y"), 0.5, [])
        t = [n.value for n in case.needles if n.role == "target"]
        d = [n.value for n in case.needles if n.role == "distractor"]
        self.assertEqual(case.grade(f"code word: {t[0]}; badge number: {t[1]}; on-call engineer: {t[2]}"),
                         (True, "all_recalled"))
        self.assertEqual(case.grade(f"code word: {t[0]}; badge number: {t[1]}"), (False, "partial"))
        self.assertEqual(case.grade(f"{t[0]} {t[1]} {t[2]} {d[0]}"), (False, "distractor_answer"))

    def test_tool_call_is_scored_by_the_production_evaluator(self):
        tools = lce.load_probe_tools()
        case = lce.build_case("tool_call", random.Random("z"), 0.5, tools)
        ws = case.needles[0].value
        path = re.search(r"Read the file (\S+) ", case.question).group(1)

        def call(**args):
            return lce._tool_call_text("fs.read_text", args)
        self.assertEqual(case.grade(call(workspace_id=ws, path=path)), (True, "exact_call"))
        self.assertEqual(case.grade(call(workspace_id=case.needles[1].value, path=path)), (False, "argument_value_mismatch"))
        self.assertEqual(case.grade(call(workspace_id=ws, path=path, max_bytes=10)), (False, "extra_argument"))
        self.assertEqual(case.grade("<tool_call><function=fs.read_text>"), (False, "malformed_call"))
        self.assertEqual(case.grade("Sure, reading it now."), (False, "missing_call"))

    def test_coherence_flags_broken_generations_only(self):
        self.assertEqual(lce.coherence("AMBER-FALCON-4821"), (True, "coherent"))
        self.assertEqual(lce.coherence("   "), (False, "empty"))
        self.assertEqual(lce.coherence("the room is " * 6), (False, "repetition_loop"))
        self.assertEqual(lce.coherence("a" * 50), (False, "repetition_loop"))
        self.assertEqual(lce.coherence("�" * 10 + "x"), (False, "garbled"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "lc.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_receipt_never_contains_prompt_or_model_text(self):
        secret_filler = tuple(f"{PROMPT_MARKER} sentence number {i} about the weekly sync." for i in range(4))
        with FakeLongContextEngine() as fake, mock.patch.object(lce, "FILLER_SENTENCES", secret_filler):
            code, receipt, stdout, stderr = run_eval(fake, "--sizes", "1000", "--depths", "0.5", "--show-output",
                                                     out=self.out)
            sent = json.dumps(fake.requests)
        raw = self.out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn(PROMPT_MARKER, sent, "the marker really was in the prompts")
        self.assertIn(SECRET_OUTPUT_MARKER, stderr, "--show-output prints raw output, to stderr only")
        for text in (raw, stdout):
            self.assertNotIn(PROMPT_MARKER, text)
            self.assertNotIn(SECRET_OUTPUT_MARKER, text)
            self.assertNotIn("<tool_call>", text)
            for value in {m[2] for m in FACT.findall(sent)}:
                self.assertNotIn(value, text, "a planted value is prompt content")
        self.assertIs(receipt["prompt_response_logging"], False)
        self.assertEqual(receipt["schema"], "local_bmo.long-context-eval.v1")
        self.assertEqual(len(receipt["cells"]), len(lce.PROBES) * len(lce.STYLES))
        for cell in receipt["cells"]:
            self.assertIn(cell["reason"], lce.REASONS)
            self.assertIn(cell["outcome"], lce.OUTCOMES)

    def test_size_targets_are_respected_with_real_prompt_tokens_recorded(self):
        with FakeLongContextEngine(context_tokens=8192) as fake:
            _, receipt, _, _ = run_eval(fake, "--sizes", "1000,2000,4000,6000", "--depths", "0.5",
                                        "--styles", "turns,single", "--probes", "needle,multi_needle,latest_value,tool_result",
                                        out=self.out)
        self.assertTrue(receipt["complete"])
        for cell in receipt["cells"]:
            with self.subTest(cell=cell["id"]):
                size = cell["target_tokens"]
                self.assertEqual(cell["outcome"], "pass")
                self.assertLessEqual(cell["prompt_tokens"], size * 1.03)
                self.assertGreaterEqual(cell["prompt_tokens"], size * 0.85)
                self.assertFalse(cell["over_target"])
        self.assertEqual(list(receipt["summary"]["by_size"]), ["1000", "2000", "4000", "6000"])

    def test_effective_window_gives_the_expected_accuracy_curve(self):
        with FakeLongContextEngine(effective_window=1500) as fake:
            _, receipt, _, _ = run_eval(fake, "--sizes", "1000,2000,4000", "--probes", "needle",
                                        "--styles", "turns", out=self.out)
        by_size = receipt["summary"]["by_size"]
        self.assertEqual([by_size[s]["accuracy"] for s in ("1000", "2000", "4000")], [1.0, 0.667, 0.333])
        for cell in receipt["cells"]:
            distance = (1 - cell["facts"][0]["planted_depth"]) * cell["prompt_tokens"]
            self.assertEqual(cell["passed"], distance < 1500, cell["id"])
        with FakeLongContextEngine(effective_window=None) as fake:
            _, receipt, _, _ = run_eval(fake, "--sizes", "1000,2000,4000", "--probes", "needle",
                                        "--styles", "turns", out=self.out)
        self.assertEqual({v["accuracy"] for v in receipt["summary"]["by_size"].values()}, {1.0})

    def test_context_overflow_is_a_distinct_outcome_not_a_failure(self):
        with FakeLongContextEngine(context_tokens=2500) as fake:
            code, receipt, _, _ = run_eval(fake, "--sizes", "1000,2500", "--max-size", "2500", "--max-tokens", "256",
                                           "--probes", "needle,tool_call", "--styles", "turns,tool_loop",
                                           out=self.out)
        self.assertEqual(code, 0)
        self.assertTrue(receipt["complete"])
        big = [c for c in receipt["cells"] if c["target_tokens"] == 2500]
        self.assertTrue(big)
        for cell in big:
            self.assertEqual((cell["outcome"], cell["passed"], cell["reason"]),
                             ("context_overflow", None, "engine_invalid_request"))
        summary = receipt["summary"]["by_size"]
        self.assertEqual(summary["2500"]["fail"], 0)
        self.assertEqual(summary["2500"]["context_overflow"], len(big))
        self.assertIsNone(summary["2500"]["accuracy"], "overflow is not graded")
        self.assertEqual(summary["1000"]["accuracy"], 1.0)

    def test_engine_request_bounds_are_their_own_outcome(self):
        # One pasted transcript past the engine's 32 KiB string bound.
        with FakeLongContextEngine(context_tokens=16384) as fake:
            _, receipt, _, _ = run_eval(fake, "--sizes", "1000,12000", "--probes", "needle", "--styles", "single",
                                        "--depths", "0.5", out=self.out)
        small, big = receipt["cells"]
        self.assertEqual(small["outcome"], "pass")
        self.assertEqual((big["outcome"], big["reason"]), ("request_too_large", "engine_request_too_large"))
        self.assertGreater(big["largest_message_bytes"], 32768)

    def test_sizes_beyond_the_budget_are_refused_before_any_request(self):
        with FakeLongContextEngine(context_tokens=8192) as fake:
            with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
                run_eval(fake, "--sizes", "1000,8000", out=self.out)
            self.assertIn("exceed the max size", str(caught.exception))
            self.assertEqual(fake.requests, [])
            with self.assertRaises(SystemExit):
                run_eval(fake, "--sizes", "1000", "--max-size", "9000", out=self.out)

    def test_stop_after_failures_stops_and_says_so(self):
        with FakeLongContextEngine(effective_window=100) as fake:
            _, receipt, _, _ = run_eval(fake, "--sizes", "1000,2000", "--probes", "needle", "--styles", "turns",
                                        "--depths", "0.1,0.5", "--stop-after-failures", "2", out=self.out)
        outcomes = [c["outcome"] for c in receipt["cells"]]
        self.assertEqual(outcomes, ["fail", "fail"])
        self.assertEqual(receipt["stopped_early"], {"reason": "failure_limit", "failures": 2, "cells_not_run": 2})
        self.assertFalse(receipt["complete"])

    def test_tool_loop_continuation_records_prefix_reuse(self):
        with FakeLongContextEngine() as fake:
            _, receipt, _, _ = run_eval(fake, "--sizes", "2000", "--depths", "0.5", "--probes", "needle,tool_call",
                                        out=self.out)
        for cell in receipt["cells"]:
            with self.subTest(cell=cell["id"]):
                self.assertEqual(cell["outcome"], "pass")
                if cell["style"] == "tool_loop":
                    self.assertTrue(cell["reuse"])
                    self.assertGreater(cell["reused_prefix_tokens"], cell["loop_first"]["prompt_tokens"])
                    self.assertGreater(cell["prompt_tokens"], cell["reused_prefix_tokens"])
                else:
                    self.assertFalse(cell["reuse"], "a fresh session never reuses")
        self.assertEqual(receipt["summary"]["tool_loop_reuse"], {"cells": 2, "reused": 2})

    def test_resume_runs_only_the_unfinished_cells(self):
        with FakeLongContextEngine() as fake:
            run_eval(fake, "--sizes", "1000", "--probes", "needle", "--styles", "turns", out=self.out)
            first = len(fake.requests)
            _, receipt, _, _ = run_eval(fake, "--sizes", "1000,2000", "--probes", "needle", "--styles", "turns",
                                        "--resume", out=self.out)
            second = len(fake.requests) - first
        self.assertEqual(first, 1 + 3, "decode calibration plus three depths")
        self.assertEqual(second, 3, "calibration kept; only the new size runs")
        self.assertEqual(receipt["resumed_cells"], 3)
        self.assertEqual(len(receipt["cells"]), 6)
        self.assertTrue(receipt["complete"])

    def test_a_timeout_is_recorded_and_the_run_waits_for_the_engine(self):
        with FakeLongContextEngine(delay_seconds=0.6) as fake:
            code, receipt, _, _ = run_eval(fake, "--sizes", "1000", "--probes", "needle", "--styles", "turns",
                                           "--depths", "0.5", "--timeout", "0.2", out=self.out)
            self.assertFalse(fake.busy, "the run waited for the abandoned request")
        self.assertEqual(code, 0)
        self.assertEqual(receipt["calibration"]["decode_calibration_error"], "client_timeout")
        self.assertEqual((receipt["cells"][0]["outcome"], receipt["cells"][0]["reason"]), ("timeout", "client_timeout"))

    def test_endpoint_must_be_loopback(self):
        with self.assertRaises(ValueError):
            lce.EngineClient("http://10.0.0.5:8080", "t" * 43, 10)


if __name__ == "__main__":
    unittest.main()
