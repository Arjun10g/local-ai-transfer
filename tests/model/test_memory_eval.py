"""The memory-note evaluator, driven end to end against a deterministic fake engine.

Nothing here measures the real model. It proves the instrument: the prompt
it measures is the host's own file (same bytes, same rendering), facts land
in the dropped or retained part as the receipt says, grading is exact, the
note arm and the dropping control differ exactly where a perfect note would
help, a stale or invented value is caught, markup in a note is stripped
before use, a failed note falls back like the host does, and nothing the
model saw or said -- including the note -- reaches the receipt.
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.test import long_context_eval as lce
from scripts.test import memory_eval as me
from tests.model.long_context_fake_engine import FACT, SECRET_OUTPUT_MARKER, FakeLongContextEngine

ROOT = Path(__file__).resolve().parents[2]
PROMPT_MARKER = "MEMX-SECRET-PROMPT-FILLER-7d21"
NOTE_MARKER = "MEMX-SECRET-NOTE-TEXT-3a9f"
PROMPTS, _ = me.load_prompts()
NOTE_FACT = re.compile(r"the (on-call engineer|badge number|preferred report format|chosen release branch|meeting room) "
                       r"for project (\w+) is (?:now )?([^.]+?)\.")
TOOL_FACT = re.compile(r'"path": ?"([^"]+)".{0,200}?"invoice_total": ?"([0-9.]+)"')
NOTE_TOOL = re.compile(r"the invoice_total in (\S+) is ([0-9.]+)\.")


class FakeMemoryEngine(FakeLongContextEngine):
    """Adds a deterministic summariser and the two extra attributes to the long-context fake.

    The "summariser" copies every fact it can read from the current note and
    the excerpt into the note (perfect by default). Variants model the
    failures the evaluator must catch: ``stale`` keeps the FIRST value of a
    corrected fact, ``hallucinate`` invents a badge number, ``markup`` wraps
    the note in reasoning and tool-call markup, ``fail_notes`` refuses every
    summary request.
    """

    def __init__(self, *, stale=False, hallucinate=False, markup=False, fail_notes=False, **kwargs):
        super().__init__(**kwargs)
        self.stale, self.hallucinate, self.markup, self.fail_notes = stale, hallucinate, markup, fail_notes
        self.summary_requests = 0

    def complete(self, body):
        messages = body["messages"]
        if self.fail_notes and messages[0]["role"] == "system" and messages[0]["content"] == PROMPTS["system"]:
            self.requests.append(body)
            return 500, {"error": {"code": "internal_error"}}
        return super().complete(body)

    def summarise(self, user):
        note = user[user.index("Current memory note:\n"):user.index("<<<\n")]
        excerpt = user[user.index("<<<\n"):]
        facts: dict[tuple, str] = {}

        def put(key, value):
            if not (self.stale and key in facts):
                facts[key] = value
        for text in (note, excerpt):
            for m in NOTE_FACT.finditer(text):
                put((m.group(1), m.group(2)), m.group(3))
            for m in TOOL_FACT.finditer(text):
                put(("invoice_total", m.group(1)), m.group(2))
            for m in NOTE_TOOL.finditer(text):
                put(("invoice_total", m.group(1)), m.group(2))
        lines = [f"- the invoice_total in {k[1]} is {v}." if k[0] == "invoice_total" else f"- the {k[0]} for project {k[1]} is {v}."
                 for k, v in facts.items()]
        lines.append(f"- {NOTE_MARKER}")
        out = "\n".join(lines)
        if self.markup:
            out = f"<think>planning the note</think>\n<tool_call>\n<function=fs.read_text>\n</function>\n</tool_call>\n<|im_start|>{out}"
        return out

    def answer(self, messages, tools, rendered):
        if messages[0]["role"] == "system" and messages[0]["content"] == PROMPTS["system"]:
            self.summary_requests += 1
            return self.summarise(messages[1]["content"])
        question = messages[-1]["content"]
        if self.hallucinate and "If it was never mentioned" in question:
            return f"48213 ({SECRET_OUTPUT_MARKER})"
        m = re.search(r"What is the (preferred report format|chosen release branch) for project (\w+)", question)
        if m:
            found = [x.group(3) for x in NOTE_FACT.finditer(rendered) if x.group(1) == m.group(1) and x.group(2) == m.group(2)]
            return f"{found[-1]} ({SECRET_OUTPUT_MARKER})" if found else f"I am not sure. ({SECRET_OUTPUT_MARKER})"
        out = super().answer(messages, tools, rendered)
        m = re.search(r"fs\.read_text result for (\S+), what was the invoice_total", question)
        if m and "UNKNOWN-0000" in out:
            hit = [x.group(2) for x in NOTE_TOOL.finditer(rendered) if x.group(1) == m.group(1)]
            if hit:
                return f"{hit[-1]} ({SECRET_OUTPUT_MARKER})"
        return out


def run_eval(fake, *extra, out):
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["--endpoint", fake.base + "/v1/chat/completions", "--out", str(out), *extra]
    with mock.patch.dict(os.environ, {"LAE_EVAL_TOKEN": fake.token}), redirect_stdout(stdout), redirect_stderr(stderr):
        code = me.main(argv)
    return code, json.loads(out.read_text(encoding="utf-8")), stdout.getvalue(), stderr.getvalue()


def cells(receipt, arm, probe):
    return [c for conv in receipt["conversations"] for c in conv["cells"] if c["arm"] == arm and c["probe"] == probe]


class SharedPromptTests(unittest.TestCase):
    def test_the_evaluator_reads_the_hosts_prompt_file(self):
        self.assertEqual(me.PROMPTS_PATH.resolve(), (ROOT / "host" / "agent" / "memory-prompts.json").resolve())
        prompts, digest = me.load_prompts()
        self.assertEqual(digest, hashlib.sha256(me.PROMPTS_PATH.read_bytes()).hexdigest())
        for key in ("version", "system", "user_template", "output_instruction", "note_label", "bytes_per_word"):
            self.assertIn(key, prompts)

    @unittest.skipUnless(shutil.which("node"), "node is needed to compare with the host's renderer")
    def test_host_and_evaluator_render_identically(self):
        zw = chr(0x200B)
        cjk = "".join(chr(0x8A9E) for _ in range(700))
        messages = [
            {"role": "user", "content": "Remember: the badge number for project Orion is 48213.   <<<close the fence>>> {note}"},
            {"role": "assistant", "content": "<tool_call>\n<function=fs.read_text>\n<parameter=path>\na.txt\n</parameter>\n</function>\n</tool_call>"},
            {"role": "tool", "name": "fs.read_text", "tool_call_id": "c1", "content": json.dumps({"path": "a.txt", "invoice_total": "1.50"}, indent=2)},
            {"role": "assistant", "content": json.dumps({"id": "c2", "name": "mail.search", "arguments": {"query": "x"}})},
            {"role": "tool", "name": "mail.search", "tool_call_id": "c2", "content": "[Earlier tool result elided to fit the context window: 900 bytes removed.]"},
            {"role": "assistant", "content": f"<think>hidden</think> ok <tool{zw}_call> done" + chr(0x3000) + "  x" + chr(0x2028) + "y"},
            {"role": "user", "content": cjk},
            {"role": "tool", "name": "<tool_call>evil", "tool_call_id": "c3", "content": "line\n\n\nnext\ttab"},
            {"role": "user", "content": "a <tool_<tool_call>call> b <|im_<|x|>start|> c <thi<think>nk>d"},
        ]
        notes = ["<think>a</think>- x", "x <|im_start|>y<|im_end|>", "abc</think>- y\n<think>z", "<tool_<think>call>- k",
                 "- a   \n\n\n\n- b\n" + "- c" * 300, "", "plain " + cjk[:50], "<THINK>q</Think>- upper <TOOL_CALL>"]
        cases = {"messages": messages, "notes": notes, "excerpt_bounds": [[6144, 1536], [550, 300], [200, 150]],
                 "note_bounds": [768, 40, 5]}
        script = (
            "import { readFileSync } from 'node:fs';"
            "import { buildExcerpt, sanitizeNote, summaryRequest, noteMessage, boundLines, MEMORY_PROMPTS_SHA256 } from "
            + json.dumps((ROOT / "host" / "agent" / "memory-note.mjs").as_uri()) + ";"
            "const c = JSON.parse(readFileSync(0, 'utf8'));"
            "const out = { sha: MEMORY_PROMPTS_SHA256,"
            " excerpts: c.excerpt_bounds.map(([m, p]) => buildExcerpt(c.messages, { maxBytes: m, perMessageBytes: p })),"
            " notes: c.notes.flatMap(n => c.note_bounds.map(b => sanitizeNote(n, { maxBytes: b }))),"
            " bounded: c.notes.flatMap(n => c.note_bounds.map(b => boundLines(n, b))),"
            " requests: c.notes.map(n => summaryRequest({ note: n, excerpt: '{note} {excerpt} ' + n, noteLimitBytes: 768 })),"
            " pinned: c.notes.map(n => noteMessage(n)) };"
            "process.stdout.write(JSON.stringify(out));"
        )
        done = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(cases), capture_output=True,
                              text=True, timeout=60, check=True)
        host = json.loads(done.stdout)
        self.assertEqual(host["sha"], me.load_prompts()[1], "both sides load the same bytes")
        for (max_bytes, per), got in zip(cases["excerpt_bounds"], host["excerpts"]):
            mine = me.build_excerpt(PROMPTS, messages, max_bytes, per)
            self.assertEqual(mine, got, (max_bytes, per))
        self.assertEqual([me.sanitize_note(n, b) for n in notes for b in cases["note_bounds"]], host["notes"])
        self.assertEqual([me.bound_lines(n, b) for n in notes for b in cases["note_bounds"]], host["bounded"])
        self.assertEqual([me.summary_request(PROMPTS, n, "{note} {excerpt} " + n, 768) for n in notes], host["requests"])
        self.assertEqual([me.note_message(PROMPTS, n) for n in notes], host["pinned"])
        # The parity is not vacuous: the cases do exercise the stripping and bounds.
        self.assertNotIn("<tool", host["excerpts"][0]["text"])
        self.assertGreater(host["excerpts"][2]["omitted"], 0)
        self.assertEqual(host["excerpts"][1]["omitted"], 0, "the middle bound is met by lowering the cap (water filling)")
        self.assertGreater(me.build_excerpt(PROMPTS, messages, 100000, 300)["bytes"], 550, "a plain 300-byte cap would not have fit")
        self.assertIn(" [...]", host["excerpts"][0]["text"])


class RecallParityTests(unittest.TestCase):
    """The evaluator's recall mirror reproduces the host module's output on the shared vectors."""

    VECTORS = json.loads((ROOT / "tests" / "model" / "memory_recall_vectors.json").read_text(encoding="utf-8"))

    def test_words_match_the_host(self):
        for vector in self.VECTORS["words"]:
            with self.subTest(text=vector["text"]):
                self.assertEqual(me.recall_words(vector["text"]), vector["words"])

    def test_entries_match_the_host(self):
        for vector in self.VECTORS["entries"]:
            with self.subTest(message=json.dumps(vector["message"])[:60]):
                self.assertEqual(me.recall_entries(PROMPTS, vector["message"]), vector["entries"])

    def test_searches_match_the_host(self):
        for vector in self.VECTORS["searches"]:
            options = vector.get("options", {})
            archive = me.RecallArchive(PROMPTS, recall_bytes=options.get("recallBytes", me.RECALL_BYTES),
                                       recall_entries=options.get("recallEntries", me.RECALL_ENTRIES))
            archive.add(vector["messages"])
            with self.subTest(question=vector["question"]):
                self.assertEqual(archive.search(me.recall_queries(vector["question"], vector.get("previous"))),
                                 vector["lines"])

    def test_a_recalled_block_is_never_archived_again(self):
        block = me.recall_message(PROMPTS, ["User: something worth remembering"])
        self.assertEqual(me.recall_entries(PROMPTS, block), [])
        self.assertIsNone(me.recall_message(PROMPTS, []))


class ConversationTests(unittest.TestCase):
    def test_facts_land_where_the_receipt_says(self):
        conv = me.build_conversation("memory|0", 2500, 1500)
        dropped = "\n".join(m["content"] for m in conv["dropped"])
        retained = "\n".join(m["content"] for m in conv["retained"])
        for fact in conv["facts"]:
            self.assertIn(fact["value"], dropped)
            self.assertNotIn(fact["value"], retained)
        stale = next(f for f in conv["facts"] if f["type"] == "updated")
        self.assertLess(dropped.index(stale["stale"]), dropped.index(stale["value"]), "the correction comes later")
        half = (len(conv["dropped"]) + 1) // 2
        first, second = ("\n".join(m["content"] for m in part) for part in (conv["dropped"][:half], conv["dropped"][half:]))
        self.assertIn(stale["stale"], first)
        self.assertIn(stale["value"], second, "with two chunks the merge must replace the value")
        self.assertIn(conv["control"]["value"], retained)
        self.assertNotIn(conv["absent_project"], dropped + retained)
        self.assertEqual(conv["retained"][0]["role"], "user", "the host never sends a history opening with anything else")
        self.assertEqual(len(FACT.findall(dropped + retained)), 4 + 1, "only the planted facts state a base-fake fact")
        ages = [f["age_turns"] for f in conv["facts"]]
        self.assertTrue(all(a > conv["control"]["age_turns"] for a in ages))

    def test_grading_is_exact(self):
        fact = {"type": "number", "value": "48213"}
        self.assertEqual(me.grade(fact, "It is 48,213."), (True, "recalled"))
        self.assertEqual(me.grade(fact, "482130"), (False, "missing_fact"))
        updated = {"type": "updated", "value": "R-938", "stale": "R-412"}
        self.assertEqual(me.grade(updated, "R-938"), (True, "latest_value"))
        self.assertEqual(me.grade(updated, "R-412, now R-938"), (False, "stale_value"))
        self.assertEqual(me.grade_hallucination("I do not know."), (True, "abstained"))
        self.assertEqual(me.grade_hallucination("It is 48,213."), (False, "invented_value"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "mem.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_note_keeps_what_dropping_loses_and_nothing_private_reaches_the_receipt(self):
        secret_filler = tuple(f"{PROMPT_MARKER} sentence number {i} about the weekly sync." for i in range(4))
        with FakeMemoryEngine() as fake, mock.patch.object(lce, "FILLER_SENTENCES", secret_filler):
            code, receipt, stdout, stderr = run_eval(fake, "--conversations", "2", "--arms", "note,drop,full",
                                                     "--show-output", out=self.out)
            sent = json.dumps(fake.requests)
        raw = self.out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(receipt["complete"])
        self.assertIn(PROMPT_MARKER, sent)
        self.assertIn(NOTE_MARKER, sent, "the note really was sent back as context")
        self.assertIn(NOTE_MARKER, stderr, "--show-output prints raw text to stderr only")
        for text in (raw, stdout):
            for marker in (PROMPT_MARKER, NOTE_MARKER, SECRET_OUTPUT_MARKER, PROMPTS["note_label"][:40], "<tool_call>"):
                self.assertNotIn(marker, text)
            for value in {m[2] for m in FACT.findall(sent)}:
                self.assertNotIn(value, text, "a planted value is prompt content")
        self.assertIs(receipt["prompt_response_logging"], False)
        self.assertEqual(receipt["schema"], "local_bmo.memory-eval.v1")
        self.assertEqual(receipt["memory_prompts"]["sha256"], hashlib.sha256(me.PROMPTS_PATH.read_bytes()).hexdigest())
        self.assertEqual(receipt["memory_prompts"]["path"], "host/agent/memory-prompts.json")
        self.assertEqual(receipt["requests_sent_this_run"], receipt["plan"]["planned_requests"])
        by = receipt["summary"]["by_arm_probe"]
        for probe in me.FACT_TYPES:
            with self.subTest(probe=probe):
                self.assertEqual(by[f"note/{probe}"]["accuracy"], 1.0)
                self.assertEqual(by[f"full/{probe}"]["accuracy"], 1.0)
                self.assertEqual(by[f"drop/{probe}"]["accuracy"], 0.0, "plain dropping loses every dropped fact")
                self.assertEqual(receipt["summary"]["note_minus_drop"][probe], 1.0)
        for arm in ("note", "drop", "full"):
            self.assertEqual(by[f"{arm}/retained_control"]["accuracy"], 1.0)
            self.assertEqual(by[f"{arm}/hallucination"]["accuracy"], 1.0)
        notes = receipt["summary"]["notes"]
        self.assertEqual(notes["generated"], 2)
        self.assertEqual(notes["facts_kept_by_type"], {t: 2 for t in me.FACT_TYPES})
        self.assertEqual(notes["stale_value_kept"], 0)
        self.assertEqual(fake.summary_requests, 2 * 2, "two chunks per note: the merge path ran")
        for conv in receipt["conversations"]:
            for cell in conv["cells"]:
                self.assertIn(cell["outcome"], me.OUTCOMES)
                self.assertIn(cell["reason"], me.REASONS)
        ages = receipt["summary"]["by_arm_age"]
        self.assertTrue(ages and all(key.split("/")[0] in ("note", "drop", "full") for key in ages))
        self.assertEqual({k: v["accuracy"] for k, v in ages.items() if k.startswith("drop/")}, {k: 0.0 for k in ages if k.startswith("drop/")})

    def test_recall_arms_find_what_dropping_loses_and_keep_text_out_of_the_receipt(self):
        secret_filler = tuple(f"{PROMPT_MARKER} sentence number {i} about the weekly sync." for i in range(4))
        with FakeMemoryEngine() as fake, mock.patch.object(lce, "FILLER_SENTENCES", secret_filler):
            code, receipt, stdout, _ = run_eval(fake, "--conversations", "2", "--arms", "drop,recall,both,recall_gap", out=self.out)
            sent = json.dumps(fake.requests)
        raw = self.out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["requests_sent_this_run"], receipt["plan"]["planned_requests"])
        self.assertIn(PROMPTS["recall_label"][:40], sent, "a recalled block really was sent")
        for text in (raw, stdout):
            for marker in (PROMPT_MARKER, NOTE_MARKER, PROMPTS["recall_label"][:40], "User:"):
                self.assertNotIn(marker, text)
        by = receipt["summary"]["by_arm_probe"]
        for probe in ("name", "number", "preference", "decision", "updated"):
            with self.subTest(probe=probe):
                self.assertEqual(by[f"recall/{probe}"]["accuracy"], 1.0)
                self.assertEqual(by[f"both/{probe}"]["accuracy"], 1.0)
                self.assertEqual(by[f"drop/{probe}"]["accuracy"], 0.0)
        for arm in ("drop", "recall", "both", "recall_gap"):
            self.assertEqual(by[f"{arm}/retained_control"]["accuracy"], 1.0)
            self.assertEqual(by[f"{arm}/hallucination"]["accuracy"], 1.0, "an absent project retrieves nothing, so nothing invites an invented value")
        for conv in receipt["conversations"]:
            retrieved = conv["recall"]["facts_retrieved"]
            self.assertEqual(set(retrieved), set(me.FACT_TYPES))
            self.assertTrue(all(retrieved.values()), retrieved)
            self.assertTrue(all(conv["recall"]["facts_retrieved_gap"].values()), conv["recall"]["facts_retrieved_gap"])
            self.assertGreater(conv["recall"]["archive_entries"], 20)
            absent = [c for c in conv["cells"] if c["arm"] == "recall" and c["probe"] == "hallucination"][0]
            self.assertEqual(absent["recall_lines"], 0)
        self.assertEqual(receipt["summary"]["notes"]["generated"], 2, "the both arm makes a note")
        self.assertEqual(me.planned_requests(me.argparse.Namespace(conversations=2, chunks=2, arms=("drop", "recall", "recall_gap"))), 2 * 24)
        self.assertEqual(me.planned_requests(me.argparse.Namespace(conversations=2, chunks=2, arms=("both",))), 2 * (2 + 8))

    def test_a_stale_note_and_an_invented_value_are_caught(self):
        with FakeMemoryEngine(stale=True, hallucinate=True) as fake:
            _, receipt, _, _ = run_eval(fake, "--conversations", "1", out=self.out)
        stale = cells(receipt, "note", "updated")[0]
        self.assertEqual((stale["outcome"], stale["reason"]), ("fail", "stale_value"))
        self.assertEqual(receipt["summary"]["notes"]["stale_value_kept"], 1)
        for arm in ("note", "drop"):
            invented = cells(receipt, arm, "hallucination")[0]
            self.assertEqual((invented["outcome"], invented["reason"]), ("fail", "invented_value"))

    def test_markup_in_a_note_is_stripped_before_it_is_used(self):
        with FakeMemoryEngine(markup=True) as fake:
            _, receipt, _, _ = run_eval(fake, "--conversations", "1", out=self.out)
            questions = [r for r in fake.requests if r["messages"][0]["role"] != "system"]
        pinned = [r["messages"][0]["content"] for r in questions if r["messages"][0]["content"].startswith(PROMPTS["note_label"])]
        self.assertTrue(pinned)
        for text in pinned:
            for token in ("<tool_call>", "<think>", "planning the note", "<|im_start|>", "<function="):
                self.assertNotIn(token, text)
        self.assertEqual(receipt["summary"]["notes"]["markup_removed"], 1)
        self.assertEqual(receipt["summary"]["by_arm_probe"]["note/name"]["accuracy"], 1.0)

    def test_a_failed_note_falls_back_to_dropping_and_is_not_graded(self):
        with FakeMemoryEngine(fail_notes=True) as fake:
            code, receipt, _, _ = run_eval(fake, "--conversations", "1", out=self.out)
        self.assertEqual(code, 0)
        conv = receipt["conversations"][0]
        self.assertEqual(conv["note"]["outcome"], "error")
        for cell in cells(receipt, "note", "name") + cells(receipt, "note", "hallucination"):
            self.assertEqual((cell["outcome"], cell["passed"], cell["reason"]), ("skipped", None, "note_failed"))
        self.assertEqual(receipt["summary"]["by_arm_probe"]["drop/retained_control"]["accuracy"], 1.0)
        self.assertIsNone(receipt["summary"]["by_arm_probe"]["note/name"]["accuracy"])
        self.assertEqual(receipt["summary"]["notes"]["failed"], 1)

    def test_request_budget_is_refused_before_any_request(self):
        with FakeMemoryEngine() as fake:
            with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
                run_eval(fake, "--conversations", "10", "--max-requests", "50", out=self.out)
            self.assertIn("more than --max-requests", str(caught.exception))
            self.assertEqual(fake.requests, [])

    def test_resume_reruns_only_unfinished_conversations(self):
        with FakeMemoryEngine() as fake:
            run_eval(fake, "--conversations", "1", out=self.out)
            first = len(fake.requests)
            _, receipt, _, _ = run_eval(fake, "--conversations", "2", "--resume", out=self.out)
            second = len(fake.requests) - first
        self.assertEqual(first, second, "the second run sent only conversation 2")
        self.assertEqual(receipt["resumed_conversations"], 1)
        self.assertTrue(receipt["complete"])
        with mock.patch.object(me, "load_prompts", return_value=(PROMPTS, "0" * 64)), FakeMemoryEngine() as fake:
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                run_eval(fake, "--conversations", "2", "--resume", out=self.out)

    def test_endpoint_must_be_loopback(self):
        with self.assertRaises(ValueError):
            me.CountingClient("http://10.0.0.5:8080", "t" * 43, 10)


if __name__ == "__main__":
    unittest.main()
