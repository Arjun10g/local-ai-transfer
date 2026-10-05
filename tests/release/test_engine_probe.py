"""The engine probe's verdicts (`local/bmo_engine_probe.py`), without an engine."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_engine_probe as probe  # noqa: E402


def step(restored=0, count=2, reply="x", prompt=50):
    return {"reply": reply, "prompt_tokens": prompt, "seconds": 1.0, "lifecycle": "READY",
            "runtime": {"last_restored_snapshot_tokens": restored, "snapshot_count": count}}


class SnapshotVerdictTests(unittest.TestCase):
    def good(self):
        return {"A1": step(0, 1), "B1": step(0, 2), "A2": step(15), "B2": step(15), "A3": step(34, reply="echo", prompt=57)}

    def test_all_restored_and_equal_to_cold_passes(self):
        verdict = probe.verdict_snapshots(self.good(), step(0, reply="echo", prompt=57))
        self.assertTrue(all(verdict.values()), verdict)

    def test_the_old_single_snapshot_behaviour_fails(self):
        steps = self.good()
        steps["A2"] = step(0)
        steps["B1"] = step(0, 1)
        verdict = probe.verdict_snapshots(steps, step(0, reply="echo", prompt=57))
        self.assertFalse(verdict["A2_restored_after_B_used_the_engine"])
        self.assertFalse(verdict["two_snapshots_held"])

    def test_a_different_reply_than_the_cold_run_fails(self):
        verdict = probe.verdict_snapshots(self.good(), step(0, reply="different", prompt=57))
        self.assertFalse(verdict["A3_reply_equals_a_cold_run"])


class IdleVerdictTests(unittest.TestCase):
    def idle(self, loaded=False, lifecycle="READY"):
        return {"runtime": {"model_loaded": loaded}, "lifecycle": lifecycle}

    def after(self, reloads=1, restored=15, reply="beta", loaded=True):
        return {"reply": reply, "runtime": {"model_loaded": loaded, "reloads": reloads, "last_restored_snapshot_tokens": restored}}

    def test_a_clean_cycle_passes_including_memory(self):
        verdict = probe.verdict_idle(None, self.idle(), self.after(), 6000, 300, 65)
        self.assertTrue(all(verdict.values()), verdict)
        self.assertIn("memory_dropped_over_2_GB", verdict)

    def test_never_unloaded_fails(self):
        verdict = probe.verdict_idle(None, self.idle(loaded=True), self.after(), 6000, 6000, None)
        self.assertFalse(verdict["unloaded_while_idle"])
        self.assertFalse(probe.verdict_idle(None, self.idle(), self.after(), 6000, 5900, 65)["memory_dropped_over_2_GB"])

    def test_a_reload_that_lost_the_saved_state_or_the_answer_fails(self):
        self.assertFalse(probe.verdict_idle(None, self.idle(), self.after(restored=0), None, None, 65)["saved_state_survived_the_unload"])
        self.assertFalse(probe.verdict_idle(None, self.idle(), self.after(reply=" "), None, None, 65)["next_message_reloaded_and_answered"])
        self.assertFalse(probe.verdict_idle(None, self.idle(lifecycle="FAILED"), self.after(), None, None, 65)["engine_stayed_ready"])

    def test_an_unreadable_or_implausible_memory_figure_is_not_judged(self):
        self.assertNotIn("memory_dropped_over_2_GB", probe.verdict_idle(None, self.idle(), self.after(), 60, 50, 65))
        self.assertNotIn("memory_dropped_over_2_GB", probe.verdict_idle(None, self.idle(), self.after(), None, None, 65))


if __name__ == "__main__":
    unittest.main()
