"""The host-level recall probe (`local/bmo_host_probe.py`): its grading, its event parsing and its verdict.

It does not start an engine. A scripted fake host plays the part of the model, so these tests prove the
instrument: a correct recall passes, a stale or invented value fails, a conversation that never overflowed
the window is refused as proof, and the off-mode control is expected to fail.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_host_probe as probe  # noqa: E402


def sse(*events):
    return [("data: " + json.dumps(e) + "\n\n").encode() for e in events]


class FakeHost:
    """Answers each probe question from a table; `compact` makes the early turns report a compaction."""

    def __init__(self, answers, recall_lines=2, compact=True, fail_turn=None):
        self.answers, self.recall_lines, self.compact, self.fail_turn, self.calls = answers, recall_lines, compact, fail_turn, 0

    def turn(self, session, number, message):
        self.calls += 1
        label = next((q[0] for q in probe.QUESTIONS if q[1] == message), None)
        text = self.answers.get(label, "OK")
        asked = label is not None and label != "absent_project"
        return {"text": text, "recall": {"lines": self.recall_lines} if asked and self.recall_lines else None,
                "compactions": 1 if self.compact and number == 8 else 0, "failed": "engine_error" if number == self.fail_turn else None,
                "prompt_tokens": 500, "seconds": 1.0}


GOOD = {"number_exact": "48213", "name_gap": "Priya Raman", "room_corrected": "R-777", "absent_project": "I do not know"}


def run(host, mode="recall", filler=3):
    with redirect_stdout(io.StringIO()):
        outcome = probe.run_probe(host, mode, filler, lambda *_: None)
    return outcome, probe.verdict(outcome, mode)


class GradingTests(unittest.TestCase):
    def test_recalled_stale_missing_and_invented(self):
        self.assertEqual(probe.grade("number_exact", "The badge is 48213.", ("48213",), ()), (True, "recalled"))
        self.assertEqual(probe.grade("name_gap", "PRIYA RAMAN", ("priya", "raman"), ()), (True, "recalled"))
        self.assertEqual(probe.grade("room_corrected", "R-777 (was R-412)", ("r-777",), ("r-412",)), (False, "stale_value"))
        self.assertEqual(probe.grade("room_corrected", "I am not sure", ("r-777",), ("r-412",)), (False, "missing_fact"))
        self.assertEqual(probe.grade("absent_project", "I do not know", (), ()), (True, "abstained"))
        self.assertEqual(probe.grade("absent_project", "It is 48213", (), ()), (False, "invented_value"))
        self.assertEqual(probe.grade("absent_project", "ticket 123456 is open", (), ()), (True, "abstained"), "six digits is not a badge number")

    def test_filler_scales_with_the_window(self):
        self.assertGreater(probe.filler_turns_for(8192), probe.filler_turns_for(2048))
        self.assertGreaterEqual(probe.filler_turns_for(512), 6)
        self.assertNotEqual(probe.filler_message(0), probe.filler_message(1))
        self.assertNotIn("48213", " ".join(probe.filler_message(i) for i in range(20)))


class EventTests(unittest.TestCase):
    def test_a_stream_is_summarised(self):
        events = probe.parse_sse(sse(
            {"event": "message.delta", "data": {"text": "48"}},
            {"event": "metrics.snapshot", "data": {"context_compaction": {"reason": "budget"}}},
            {"event": "metrics.snapshot", "data": {"memory_recall": {"lines": 3, "bytes": 400}}},
            {"event": "message.completed", "data": {"text": "48213", "usage": {"prompt_tokens": 777}}}))
        summary = probe.summarize_turn(events)
        self.assertEqual((summary["text"], summary["compactions"], summary["recall"]["lines"], summary["prompt_tokens"]),
                         ("48213", 1, 3, 777))
        failed = probe.summarize_turn(probe.parse_sse(sse({"event": "request.failed", "data": {"code": "context_overflow"}})))
        self.assertEqual(failed["failed"], "context_overflow")

    def test_a_frame_split_across_chunks_and_junk_are_handled(self):
        raw = b'data: {"event": "message.delta", "da'
        rest = b'ta": {"text": "ok"}}\n\ndata: not json\n\n'
        self.assertEqual(len(probe.parse_sse([raw, rest])), 1)


class VerdictTests(unittest.TestCase):
    def test_a_correct_run_passes(self):
        outcome, (ok, problems) = run(FakeHost(GOOD))
        self.assertTrue(ok, problems)
        self.assertEqual(outcome["turns"], 4 + 3 + 4)

    def test_a_stale_or_invented_answer_fails(self):
        _, (ok, problems) = run(FakeHost({**GOOD, "room_corrected": "R-412"}))
        self.assertFalse(ok)
        self.assertTrue(any("room_corrected" in p for p in problems))
        _, (ok, problems) = run(FakeHost({**GOOD, "absent_project": "48213"}))
        self.assertTrue(any("invented_value" in p for p in problems))

    def test_a_run_that_never_overflowed_proves_nothing(self):
        _, (ok, problems) = run(FakeHost(GOOD, compact=False))
        self.assertFalse(ok)
        self.assertTrue(any("never overflowed" in p for p in problems))

    def test_recall_that_never_appears_is_a_failure_even_if_answers_are_right(self):
        _, (ok, problems) = run(FakeHost(GOOD, recall_lines=0))
        self.assertFalse(ok)
        self.assertTrue(any("recalled block" in p for p in problems))

    def test_a_failed_turn_is_reported(self):
        _, (ok, problems) = run(FakeHost(GOOD, fail_turn=2))
        self.assertFalse(ok)
        self.assertTrue(any("turns failed" in p for p in problems))

    def test_the_off_control_is_expected_to_fail_and_passing_it_is_suspicious(self):
        wrong = {"number_exact": "I do not know", "name_gap": "I do not know", "room_corrected": "unknown", "absent_project": "I do not know"}
        _, (ok, problems) = run(FakeHost(wrong, recall_lines=0), mode="off")
        self.assertTrue(ok, problems)
        _, (ok, problems) = run(FakeHost(GOOD, recall_lines=0), mode="off")
        self.assertFalse(ok)
        self.assertTrue(any("expected to fail" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
