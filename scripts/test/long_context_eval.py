#!/usr/bin/env python3
"""Do long conversations hold up? A deterministic long-context probe.

At growing prompt sizes this asks the engine whether the model still

* recalls facts planted earlier in the conversation (``needle``,
  ``multi_needle`` with distractors, ``latest_value`` where a fact is
  corrected mid-conversation),
* uses an old tool result correctly (``tool_result``),
* emits a correct tool call whose argument comes from far back in the
  history (``tool_call``), and
* stays coherent (every answer is also checked for emptiness, loops and
  garbage),

and how latency grows: total seconds, an estimated prefill share, the real
``usage.prompt_tokens``, and how much retained prefix the engine reused
(``/metrics`` ``runtime.last_reused_prefix_tokens``).

Every probe is graded by exact or regex match against values this script
planted itself. There is no model judge. Prompts and model output never
reach the receipt (``prompt_response_logging`` is false); ``--show-output``
prints raw output to stderr only, as ``local/bmo_local.py eval`` does.

Each cell is one (probe, conversation style, size, depth, trial). Styles:

``turns``      many short user/assistant turns (the normal chat shape)
``single``     the whole history pasted into ONE user message
``tool_loop``  an agent loop: the model first calls ``time.now``, then the
               request is continued with its own call plus the tool result.
               That continuation strictly extends the retained prompt, which
               is the one case the engine can reuse
               (``native/backend/llama_backend.cpp`` ``reusable_prefix_tokens``).

A refusal at the context limit (HTTP 400 ``invalid_request``) is recorded as
``context_overflow`` and an engine request bound (``request_too_large``) as
``request_too_large``. Neither is a model failure: both are findings about
the limits. Cells run cheapest first (smallest size first), and the receipt
is rewritten after every cell, so an interrupted run resumes with
``--resume``.

Run it against an engine that is already serving (token in
``LAE_EVAL_TOKEN`` or on stdin with ``--token-stdin``)::

    python3 scripts/test/long_context_eval.py \\
        --endpoint http://127.0.0.1:PORT/v1/chat/completions --out lc.json

or let ``local/bmo_local.py longctx`` start the engine for you.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test import evaluate_tool_calls as ev  # noqa: E402

SCHEMA = "local_bmo.long-context-eval.v1"
MODEL = "qwen35-9b-q4-k-m"
PRODUCTION_FIXTURE = ROOT / "tests" / "model" / "production_tool_call_eval.json"
# A handful of real production tools: enough to make the tool choice a real
# choice, few enough (~800 tokens) that the conversation, not the catalog,
# fills the context. The full 33-tool catalog alone is ~5,800 tokens.
PROBE_TOOL_NAMES = ("time.now", "fs.list", "fs.read_text", "mail.search_messages", "teams.send_message")

PROBES = ("needle", "multi_needle", "latest_value", "tool_result", "tool_call")
STYLES = ("turns", "single", "tool_loop")
DEFAULT_SIZES = (1000, 2000, 4000, 6000)
DEFAULT_DEPTHS = (0.1, 0.5, 0.9)
TIMEOUT_CEILING = 3600
# The engine refuses more than 64 messages (native/server/chat_request.cpp
# kMaxMessages). Filler is packed into at most this many units of two
# messages, which leaves room for needles, the question and a tool loop.
MAX_FILLER_UNITS = 20
# Conservative: real Qwen text runs nearer 3.5-4 characters per token, so the
# first prompts land under their target rather than over the context. Every
# answered request then calibrates the estimate from the real prompt_tokens.
DEFAULT_CHARS_PER_TOKEN = 3.0
PER_MESSAGE_TOKENS = 10
OUTPUT_RESERVE = 256
# After a client timeout the engine is still finishing the abandoned request;
# wait at least this long for it before sending the next one.
IDLE_WAIT_FLOOR = 30

# Outcomes. Only `pass` and `fail` are quality results; the limits are
# findings, and the rest are operational.
DONE_OUTCOMES = frozenset({"pass", "fail", "context_overflow", "request_too_large"})
OUTCOMES = DONE_OUTCOMES | {"timeout", "engine_busy", "error"}
REASONS = frozenset({
    "recalled", "missing_fact", "distractor_answer", "partial", "all_recalled",
    "latest_value", "stale_value", "exact_call", "engine_invalid_request",
    "engine_request_too_large", "client_timeout", "engine_busy",
}) | ev.QUALITY_CODES | ev.DIAGNOSTIC_CODES
COHERENCE_REASONS = frozenset({"coherent", "empty", "repetition_loop", "garbled"})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Deterministic content
# ---------------------------------------------------------------------------

PROJECTS = ("Orion", "Juniper", "Halcyon", "Marlowe", "Kestrel", "Tamarind", "Basalt", "Quillon",
            "Sorrel", "Vantage")
CODE_WORDS = ("AMBER", "FALCON", "COBALT", "MERIDIAN", "LANTERN", "SABLE", "TIDEWATER", "QUARTZ",
              "HEMLOCK", "SPARROW", "ONYX", "BRAMBLE")
FIRST_NAMES = ("Thessaly", "Ignatius", "Marisol", "Oluwaseun", "Bronwyn", "Leopold", "Anouk",
               "Teodoro", "Saoirse", "Kasimir")
LAST_NAMES = ("Okonkwo", "Vasquez-Lindqvist", "Arbuthnot", "Nakashima", "Delacroix", "Rautio",
              "Fairweather", "Szymanski", "Abernathy", "Quaresma")

# Filler must never contain the phrase "for project": planted facts use it,
# and keeping it unique keeps grading (and the test fake) unambiguous.
FILLER_SENTENCES = (
    "I moved the standup to half past nine so the people commuting by train are not rushing.",
    "The build on the release branch went green again after we pinned the compiler version.",
    "Can you remind me why we chose a weekly cadence for the dependency updates?",
    "The weekly cadence keeps each update small enough to review in one sitting.",
    "We still need someone to own the on-boarding checklist for new contractors.",
    "My laptop fan has been loud all morning; I think the indexer is running again.",
    "Lunch is the usual place on the corner, the one with the slow espresso machine.",
    "The design review notes are in the shared folder under the October heading.",
    "If the vendor cannot confirm delivery by Thursday we should switch to the backup supplier.",
    "I prefer short pull requests, even if that means a few more of them each week.",
    "The dashboard numbers looked odd because the timezone was set to the server default.",
    "Please keep the summary under one page; nobody reads the second page.",
    "We agreed to archive the old wiki once the migration script has run twice cleanly.",
    "The printer on the third floor is out of toner again, so use the one by the kitchen.",
    "Most of the flaky tests were waiting on a fixed sleep instead of polling for readiness.",
    "I will be out on Friday afternoon, but reachable by phone if something breaks.",
)
ASSISTANT_REPLIES = (
    "Understood, I will keep that in mind.",
    "That makes sense. Smaller changes are easier to review and to roll back.",
    "Good call. Polling for readiness is more reliable than a fixed sleep.",
    "Noted. I can draft that summary whenever you are ready.",
    "Sounds reasonable; let me know if you want me to check anything else.",
    "Thanks for the context. That explains the odd numbers on the dashboard.",
)
SERVICES = ("billing-api", "search-indexer", "notifier", "auth-gateway", "report-builder")
MAIL_SUBJECTS = ("Quarterly planning follow-up", "Re: travel booking", "Lunch on Thursday?",
                 "Invoice reminder", "Updated agenda", "Shared slides from the review")


def _code_word(rng: random.Random) -> str:
    a, b = rng.sample(CODE_WORDS, 2)
    return f"{a}-{b}-{rng.randint(1000, 9999)}"


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


FACT_KINDS = {
    # kind: (attribute phrase, value generator)
    "code_word": ("code word", _code_word),
    "number": ("badge number", lambda rng: str(rng.randint(10000, 99999))),
    "name": ("on-call engineer", _name),
    "room": ("meeting room", lambda rng: f"R-{rng.randint(100, 999)}"),
    "workspace": ("workspace id", lambda rng: f"ws-{rng.choice(CODE_WORDS).lower()}-{rng.randint(1000, 9999)}"),
}


def _tool_call_text(name: str, arguments: dict[str, str]) -> str:
    params = "".join(f"<parameter={k}>\n{v}\n</parameter>\n" for k, v in arguments.items())
    return f"<tool_call>\n<function={name}>\n{params}</function>\n</tool_call>"


def _chat_unit(rng: random.Random, budget: int) -> list[dict]:
    text = ""
    while len(text) < budget - 60:
        text += rng.choice(FILLER_SENTENCES) + " "
    return [{"role": "user", "content": text.strip() or rng.choice(FILLER_SENTENCES)},
            {"role": "assistant", "content": rng.choice(ASSISTANT_REPLIES)}]


def _json_unit(rng: random.Random, budget: int) -> list[dict]:
    config: dict[str, Any] = {"service": rng.choice(SERVICES), "replicas": rng.randint(2, 9), "routes": []}
    head = "Here is the config block from staging, can you sanity-check it?\n"
    while len(head) + len(json.dumps(config, indent=2)) < budget - 160:
        config["routes"].append({"path": f"/v{rng.randint(1, 3)}/{rng.choice(SERVICES)}/{rng.randint(1, 999)}",
                                 "timeout_ms": rng.choice((250, 500, 1000, 2500)),
                                 "retries": rng.randint(0, 3), "cache": rng.choice((True, False))})
    return [{"role": "user", "content": head + json.dumps(config, indent=2)},
            {"role": "assistant", "content": "The timeouts look consistent; I would cap retries at two for the slow routes."}]


def _tool_unit(rng: random.Random, budget: int) -> list[dict]:
    messages = []
    while len(json.dumps({"messages": messages})) < budget - 120:
        messages.append({"id": f"msg_{rng.randint(10**7, 10**8 - 1)}",
                         "from": f"{rng.choice(FIRST_NAMES).lower()}@example.org",
                         "subject": rng.choice(MAIL_SUBJECTS), "unread": rng.choice((True, False)),
                         "received": f"2026-09-{rng.randint(10, 28)}T{rng.randint(7, 18):02d}:{rng.randint(0, 59):02d}:00Z"})
    return [{"role": "assistant", "content": _tool_call_text("mail.list_messages", {"folder": "inbox"})},
            {"role": "tool", "name": "mail.list_messages", "tool_call_id": f"call-{rng.randint(100, 999)}",
             "content": json.dumps({"messages": messages})}]


def _code_unit(rng: random.Random, budget: int) -> list[dict]:
    lines = ["Can you check this helper? It feels slow.", "```python"]
    n = 0
    while sum(len(x) + 1 for x in lines) < budget - 140:
        n += 1
        lines += [f"def bucket_{n}(rows, width={rng.randint(2, 64)}):",
                  "    out = {}",
                  "    for row in rows:",
                  f"        key = row['{rng.choice(('ts', 'size', 'owner', 'region'))}'] // width",
                  "        out.setdefault(key, []).append(row)",
                  "    return out", ""]
    lines.append("```")
    return [{"role": "user", "content": "\n".join(lines)},
            {"role": "assistant", "content": "It is linear in the rows, so the slowness is probably the caller sorting first."}]


FILLER_KINDS: tuple[Callable[[random.Random, int], list[dict]], ...] = (_chat_unit, _json_unit, _tool_unit, _code_unit)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

@dataclass
class Needle:
    kind: str
    project: str
    value: str
    depth: float
    role: str  # "target", "distractor", "stale"
    planted_depth: float | None = None

    def units(self) -> list[dict]:
        attribute = FACT_KINDS[self.kind][0]
        # Only the latest_value probe's update is phrased as a correction.
        if self.kind == "room" and self.role == "target":
            text = f"Correction to what I said earlier: the {attribute} for project {self.project} is now {self.value}."
        else:
            text = f"Quick note before I forget: the {attribute} for project {self.project} is {self.value}."
        return [{"role": "user", "content": text + " Please keep it in mind."},
                {"role": "assistant", "content": "Got it, I will remember that."}]


@dataclass
class ToolResultNeedle:
    path: str
    total: str
    depth: float
    role: str
    kind: str = "tool_result"
    planted_depth: float | None = None

    def units(self) -> list[dict]:
        content = {"path": self.path, "content": {"customer": "Northwind Traders", "invoice_total": self.total,
                                                   "currency": "EUR", "line_items": 7, "status": "issued"}}
        return [{"role": "user", "content": f"Open {self.path} for me."},
                {"role": "assistant", "content": _tool_call_text("fs.read_text", {"workspace_id": "ws-finance", "path": self.path})},
                {"role": "tool", "name": "fs.read_text", "tool_call_id": "call-77", "content": json.dumps(content)}]


@dataclass
class Case:
    probe: str
    needles: list
    question: str
    grade: Callable[[str], tuple[bool, str]]
    uses_tools: bool = False
    tool_loop_prefix: str = "First check the current UTC time with the time.now tool. Once you have it, answer this: "


def _value_pattern(value: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _contains(output: str, value: str) -> bool:
    # A number may come back as "73,914"; nothing planted contains a comma.
    return bool(_value_pattern(value).search(re.sub(r"(?<=\d),(?=\d{3}\b)", "", output)))


def _grade_single(target: str, distractors: list[str]) -> Callable[[str], tuple[bool, str]]:
    def grade(output: str) -> tuple[bool, str]:
        if any(_contains(output, d) for d in distractors):
            return False, "distractor_answer"
        return (True, "recalled") if _contains(output, target) else (False, "missing_fact")
    return grade


def build_case(probe: str, rng: random.Random, depth: float, tools: list[dict]) -> Case:
    projects = rng.sample(PROJECTS, 5)
    target = projects[0]
    if probe == "needle":
        kind = rng.choice(("code_word", "number", "name"))
        needle = Needle(kind, target, FACT_KINDS[kind][1](rng), depth, "target")
        q = f"What is the {FACT_KINDS[kind][0]} for project {target}? Reply with only the {FACT_KINDS[kind][0]}."
        return Case(probe, [needle], q, _grade_single(needle.value, []))
    if probe == "multi_needle":
        kinds = ("code_word", "number", "name")
        targets = [Needle(k, target, FACT_KINDS[k][1](rng), depth, "target") for k in kinds]
        spots = [d for d in (0.05, 0.35, 0.65, 0.95) if abs(d - depth) >= 0.15][:3]
        distractors = [Needle(k, projects[i + 1], FACT_KINDS[k][1](rng), d, "distractor")
                       for i, (k, d) in enumerate(zip(kinds, spots))]
        q = (f"What are the code word, the badge number and the on-call engineer for project {target}? "
             "Reply in the form: code word: X; badge number: Y; on-call engineer: Z")

        def grade(output: str) -> tuple[bool, str]:
            if any(_contains(output, n.value) for n in distractors):
                return False, "distractor_answer"
            hits = sum(_contains(output, n.value) for n in targets)
            return (True, "all_recalled") if hits == 3 else (False, "partial" if hits else "missing_fact")
        return Case(probe, targets + distractors, q, grade)
    if probe == "latest_value":
        old = FACT_KINDS["room"][1](rng)
        new = old
        while new == old:
            new = FACT_KINDS["room"][1](rng)
        stale = Needle("room", target, old, depth, "stale")
        current = Needle("room", target, new, depth + (1 - depth) / 2, "target")
        q = f"What is the meeting room for project {target} now? Reply with only the room."

        def grade(output: str) -> tuple[bool, str]:
            has_new, has_old = _contains(output, new), _contains(output, old)
            if has_new and not has_old:
                return True, "latest_value"
            if has_old:
                return False, "stale_value"
            return False, "missing_fact"
        return Case(probe, [stale, current], q, grade)
    if probe == "tool_result":
        slug = target.lower()
        mine = ToolResultNeedle(f"invoices/{slug}-{rng.randint(1000, 9999)}.json",
                                f"{rng.randint(10000, 99999)}.{rng.randint(10, 99)}", depth, "target")
        other_depth = 0.95 if depth < 0.8 else 0.3
        other = ToolResultNeedle(f"invoices/{projects[1].lower()}-{rng.randint(1000, 9999)}.json",
                                 f"{rng.randint(10000, 99999)}.{rng.randint(10, 99)}", other_depth, "distractor")
        q = (f"In the earlier fs.read_text result for {mine.path}, what was the invoice_total? "
             "Reply with only the number.")
        return Case(probe, [mine, other], q, _grade_single(mine.total, [other.total]))
    if probe == "tool_call":
        needle = Needle("workspace", target, FACT_KINDS["workspace"][1](rng), depth, "target")
        decoy = Needle("workspace", projects[1], FACT_KINDS["workspace"][1](rng),
                       0.95 if depth < 0.8 else 0.3, "distractor")
        path = f"reports/{rng.choice(('q3-summary', 'budget-notes', 'roadmap'))}.txt"
        expected = {"expected": {"call": {"name": "fs.read_text",
                                          "arguments": {"workspace_id": needle.value, "path": path}}}}
        q = (f"Read the file {path} from my workspace for project {target}. "
             "Use the workspace id I gave you earlier and pass only workspace_id and path.")

        def grade(output: str) -> tuple[bool, str]:
            try:
                call = ev.parse_tool_call(output, tools)
            except (ValueError, re.error) as exc:
                return False, ev._quality_code(str(exc))
            passed, reason = ev.evaluate_call(expected, call)
            return passed, ev._quality_code(reason) if not passed else "exact_call"
        return Case(probe, [needle, decoy], q, grade, uses_tools=True,
                    tool_loop_prefix="First check the current UTC time with the time.now tool. Once you have it, do this: ")
    raise ValueError(f"unknown probe {probe}")


# ---------------------------------------------------------------------------
# Conversation assembly
# ---------------------------------------------------------------------------

@dataclass
class Built:
    messages: list[dict]
    tools: list[dict] | None
    needles: list
    estimated_tokens: int
    content_chars: int


def estimate_tokens(messages: list[dict], tools: list[dict] | None, cpt: float) -> int:
    chars = sum(len(m["content"]) for m in messages) + (len(json.dumps(tools)) if tools else 0)
    return math.ceil(chars / cpt) + PER_MESSAGE_TOKENS * len(messages) + (40 if tools else 0) + 12


def _as_transcript(units: list[list[dict]]) -> str:
    lines = []
    for unit in units:
        for m in unit:
            label = m["role"] if m["role"] != "tool" else f"tool {m['name']} result"
            lines.append(f"[{label}] {m['content']}")
    return "\n\n".join(lines)


def assemble(case: Case, style: str, size: int, seed: str, cpt: float,
             tools: list[dict], scale: float = 1.0) -> Built:
    """Fill the conversation to about `size` tokens with needles at their depths."""
    rng = random.Random(seed + "|filler")
    use_tools = tools if (case.uses_tools or style == "tool_loop") else None
    question = (case.tool_loop_prefix + case.question) if style == "tool_loop" else case.question
    needle_units = [n.units() for n in case.needles]
    fixed_messages = [m for u in needle_units for m in u] + [{"role": "user", "content": question}]
    # Room for the tool loop's own call and result, which the continuation adds.
    loop_reserve = 70 if style == "tool_loop" else 0
    budget_tokens = int((size * 0.97 - estimate_tokens(fixed_messages, use_tools, cpt) - loop_reserve) * scale)
    # Many small units place a needle more precisely; the engine's 64-message
    # bound caps how many there can be.
    n_units = max(4, min(MAX_FILLER_UNITS, budget_tokens // 80))
    unit_overhead = 2 * PER_MESSAGE_TOKENS if style != "single" else 0
    per_unit = max(120, int((budget_tokens / n_units - unit_overhead) * cpt))
    filler = [FILLER_KINDS[i % len(FILLER_KINDS)](rng, per_unit) for i in range(n_units)]
    rng.shuffle(filler)
    # Each needle goes in at the filler boundary whose share of the history
    # is nearest its requested depth.
    sizes = [sum(len(m["content"]) for m in u) for u in filler]
    total_filler = sum(sizes) or 1
    bounds = [sum(sizes[:k]) / total_filler for k in range(len(sizes) + 1)]
    slots: dict[int, list[int]] = {}
    for i in sorted(range(len(case.needles)), key=lambda i: case.needles[i].depth):
        k = min(range(len(bounds)), key=lambda k: abs(bounds[k] - case.needles[i].depth))
        slots.setdefault(k, []).append(i)
    units: list[tuple[Any, list[dict]]] = []
    for k in range(len(filler) + 1):
        units += [(case.needles[i], needle_units[i]) for i in slots.get(k, [])]
        if k < len(filler):
            units.append((None, filler[k]))
    # Measure where each needle actually landed, by characters of history.
    total = sum(len(m["content"]) for _, u in units for m in u)
    seen = 0
    for needle, unit in units:
        if needle is not None:
            needle.planted_depth = round(seen / total, 3) if total else 0.0
        seen += sum(len(m["content"]) for m in unit)
    if style == "single":
        intro = "Here is our conversation so far, pasted as one transcript. Use it to answer the question at the end.\n\n"
        messages = [{"role": "user", "content": intro + _as_transcript([u for _, u in units]) + "\n\n" + question}]
    else:
        messages = [m for _, u in units for m in u] + [{"role": "user", "content": question}]
    return Built(messages, use_tools, case.needles, estimate_tokens(messages, use_tools, cpt),
                 sum(len(m["content"]) for m in messages))


def coherence(output: str) -> tuple[bool, str]:
    """Cheap deterministic signs of a broken generation, independent of the answer."""
    text = output.strip()
    if not text:
        return False, "empty"
    if re.search(r"(.)\1{39,}", text) or re.search(r"(\b\w+\b(?:\W+\b\w+\b){2,5})(?:\W+\1){3,}", text):
        return False, "repetition_loop"
    bad = sum(1 for c in text if c == "�" or (ord(c) < 0x20 and c not in "\n\t\r"))
    if bad > max(2, len(text) // 20):
        return False, "garbled"
    return True, "coherent"


# ---------------------------------------------------------------------------
# Engine client
# ---------------------------------------------------------------------------

@dataclass
class Reply:
    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    seconds: float


class RequestFailed(Exception):
    def __init__(self, outcome: str, reason: str):
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


class EngineClient:
    """The native engine's HTTP API, the way `local/bmo_local.py` drives it."""

    def __init__(self, base: str, token: str, timeout: float):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        ev.validate_endpoint(self.base + "/v1/chat/completions")  # loopback only, before the token is used

    def _request(self, method: str, path: str, body: dict | None, timeout: float) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with ev._open_url(req, timeout) as resp:
            return json.loads(ev._read_response(resp, ev.RESPONSE_MAX_BYTES) or b"{}")

    def metrics(self) -> dict:
        try:
            return self._request("GET", "/metrics", None, 30)
        except Exception:  # metrics are diagnostics; never fail a cell over them
            return {}

    def build_info(self) -> dict:
        try:
            body = self._request("GET", "/build-info", None, 30)
            return {k: body.get(k) for k in ("backend", "engine_version", "llama_cpp_revision")}
        except Exception:
            return {}

    def new_session(self) -> str:
        # A failure here is one cell's error, never the end of a long run.
        try:
            return str(self._request("POST", "/v1/sessions", {}, 30)["id"])
        except urllib.error.HTTPError as exc:
            status = exc.code if exc.code in ev.HTTP_DIAGNOSTIC_STATUSES else None
            raise RequestFailed("error", f"http_{status}" if status else "http_other") from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RequestFailed("error", ev._error_diagnostic(exc) if isinstance(exc, (OSError, ValueError))
                                else "parse_session_shape") from None

    def chat(self, session: str, messages: list[dict], tools: list[dict] | None, max_tokens: int) -> Reply:
        body: dict[str, Any] = {"model": MODEL, "stream": False, "max_tokens": max_tokens, "mode": "normal",
                                "session_id": session, "messages": messages}
        if tools:
            body["tools"] = tools
        t0 = time.monotonic()
        try:
            payload = self._request("POST", "/v1/chat/completions", body, self.timeout)
        except urllib.error.HTTPError as exc:
            code = ev._engine_error_code(exc)
            if exc.code == 400 and code == "invalid_request":
                # The only invalid_request a well-formed request from this
                # script can draw is the prompt+max_tokens context check
                # (native/server/http_server.cpp, "context limit exceeded").
                raise RequestFailed("context_overflow", "engine_invalid_request") from None
            if exc.code in (400, 413) and code == "request_too_large":
                raise RequestFailed("request_too_large", "engine_request_too_large") from None
            if exc.code == 409:
                raise RequestFailed("engine_busy", "engine_busy") from None
            status = exc.code if exc.code in ev.HTTP_DIAGNOSTIC_STATUSES else None
            reason = (f"http_{status}_{code}" if status and code else f"http_{status}") if status else "http_other"
            raise RequestFailed("error", reason) from None
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            inner = getattr(exc, "reason", exc)
            if isinstance(exc, TimeoutError) or isinstance(inner, TimeoutError):
                raise RequestFailed("timeout", "client_timeout") from None
            raise RequestFailed("error", ev._error_diagnostic(exc)) from None
        except ValueError as exc:
            raise RequestFailed("error", ev._error_diagnostic(exc)) from None
        seconds = time.monotonic() - t0
        try:
            choice = payload["choices"][0]
            usage = payload["usage"]
            return Reply(str(choice["message"]["content"]), int(usage["prompt_tokens"]),
                         int(usage["completion_tokens"]), str(choice.get("finish_reason", "")), seconds)
        except (KeyError, IndexError, TypeError, ValueError):
            raise RequestFailed("error", "parse_response_shape") from None

    def lifecycle(self) -> str | None:
        try:
            return self._request("GET", "/readyz", None, 30).get("lifecycle")
        except Exception:
            return None

    def wait_idle(self, limit: float) -> None:
        # A client that gave up leaves the engine finishing the abandoned
        # request, answering 409 until then (ENGINE-ABANDONED-REQUEST-STATE-001).
        # /readyz reads only the lifecycle, under the engine lock; /metrics
        # also reads backend state that the running generation is changing.
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and self.lifecycle() == "BUSY":
            time.sleep(min(5.0, max(0.05, limit / 100)))


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def load_probe_tools(path: Path = PRODUCTION_FIXTURE) -> list[dict]:
    tools = ev.load_fixture(path)["tools"]
    by_name = {t["function"]["name"]: t for t in tools}
    return [by_name[name] for name in PROBE_TOOL_NAMES]


def cell_id(probe: str, style: str, size: int, depth: float, trial: int) -> str:
    return f"{probe}/{style}/s{size}/d{depth:g}/t{trial}"


def plan_cells(opts: argparse.Namespace) -> list[dict]:
    """Cheapest first: every probe and style at the smallest size before any larger size."""
    cells = []
    for size in sorted(opts.sizes):
        for trial in range(opts.trials):
            for style in opts.styles:
                for depth in opts.depths:
                    for probe in opts.probes:
                        cells.append({"id": cell_id(probe, style, size, depth, trial), "probe": probe,
                                      "style": style, "target_tokens": size, "depth": depth, "trial": trial})
    return cells


def _runtime_reuse(runtime: dict) -> dict:
    return {"reused_prefix_tokens": runtime.get("last_reused_prefix_tokens"),
            "common_prefix_tokens": runtime.get("last_common_prefix_tokens"),
            "retained_state_tokens_at_request": runtime.get("last_state_tokens"),
            "reuse": bool(runtime.get("last_reused_prefix_tokens"))}


def _request_shape(messages: list[dict], tools: list[dict] | None, max_tokens: int) -> dict:
    # Sizes only, to read a request_too_large against the engine's bounds:
    # 64 KiB body, 32 KiB per string, 48 KiB of history, 64 messages.
    sizes = [len(m["content"].encode()) for m in messages]
    body = {"model": MODEL, "stream": False, "max_tokens": max_tokens, "mode": "normal",
            "session_id": "sess-00000000", "messages": messages, **({"tools": tools} if tools else {})}
    return {"messages": len(messages), "content_bytes": sum(sizes), "largest_message_bytes": max(sizes),
            "request_bytes": len(json.dumps(body).encode())}


def run_cell(client: EngineClient, cell: dict, opts: argparse.Namespace, calibration: dict,
             tools: list[dict], show: Callable[[str, str], None]) -> dict:
    seed = f"{cell['probe']}|{cell['style']}|{cell['target_tokens']}|{cell['depth']}|{cell['trial']}"
    rng = random.Random(seed)
    case = build_case(cell["probe"], rng, cell["depth"], tools)
    size, scale = cell["target_tokens"], 1.0
    # Generators overshoot or undershoot their budgets a little; rescale the
    # filler until the estimate lands just under the target.
    for _ in range(5):
        built = assemble(case, cell["style"], size, seed, calibration["chars_per_token"], tools, scale)
        if 0.9 * size <= built.estimated_tokens <= size or scale < 0.05:
            break
        scale *= (0.96 * size) / max(1, built.estimated_tokens)
    result = dict(cell)
    result.update({"facts": [{"kind": n.kind, "role": n.role, "requested_depth": round(n.depth, 3),
                              "planted_depth": n.planted_depth} for n in built.needles],
                   "estimated_tokens": built.estimated_tokens,
                   # The tools, needles and question alone can exceed a small
                   # target; such a cell is honestly larger than asked.
                   "over_target": built.estimated_tokens > size * 1.05,
                   "chars_per_token_used": round(calibration["chars_per_token"], 3),
                   **_request_shape(built.messages, built.tools, opts.max_tokens)})
    t0 = time.monotonic()
    try:
        session = client.new_session()
        messages = built.messages
        if cell["style"] == "tool_loop":
            first = client.chat(session, messages, built.tools, opts.max_tokens)
            show(f"{cell['id']} (loop step 1)", first.content)
            result["loop_first"] = {"prompt_tokens": first.prompt_tokens, "seconds": round(first.seconds, 2),
                                    "completion_tokens": first.completion_tokens}
            # The model's own words go back verbatim: anything else would not
            # extend the retained prompt and so could not be reused.
            messages = messages + [
                {"role": "assistant", "content": first.content},
                {"role": "tool", "name": "time.now", "tool_call_id": "call-loop",
                 "content": json.dumps({"utc": "2026-10-03T09:15:00Z", "local": "2026-10-03T10:15:00+01:00",
                                        "offset": "+01:00"})}]
            result.update(_request_shape(messages, built.tools, opts.max_tokens))
        reply = client.chat(session, messages, built.tools, opts.max_tokens)
    except RequestFailed as failed:
        result.update({"outcome": failed.outcome, "passed": None, "reason": failed.reason,
                       "seconds": round(time.monotonic() - t0, 2)})
        if failed.outcome == "timeout":
            client.wait_idle(max(opts.timeout, IDLE_WAIT_FLOOR))
        return result
    show(cell["id"], reply.content)
    passed, reason = case.grade(reply.content)
    coherent, coherence_reason = coherence(reply.content)
    runtime = client.metrics().get("runtime", {}) or {}
    spt = calibration.get("decode_seconds_per_token")
    prefill = max(0.0, reply.seconds - reply.completion_tokens * spt) if spt is not None else None
    fresh = reply.prompt_tokens - (runtime.get("last_reused_prefix_tokens") or 0)
    result.update({
        "outcome": "pass" if passed else "fail", "passed": passed,
        "reason": reason if reason in REASONS else "quality_unknown",
        "coherent": coherent, "coherence_reason": coherence_reason,
        "finish_reason": reply.finish_reason if reply.finish_reason in ("stop", "length", "cancelled") else "other",
        "prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens,
        "seconds": round(reply.seconds, 2),
        "prefill_seconds_est": round(prefill, 2) if prefill is not None else None,
        "prefill_tokens_per_second_est": round(fresh / prefill, 1) if prefill else None,
        **_runtime_reuse(runtime),
    })
    # Calibrate the size estimate from what the tokenizer really did. Only the
    # first request of a cell is comparable to its estimate.
    if cell["style"] != "tool_loop" and reply.prompt_tokens >= 300:
        chars = built.content_chars + (len(json.dumps(built.tools)) if built.tools else 0)
        overhead = PER_MESSAGE_TOKENS * len(built.messages) + (40 if built.tools else 0) + 12
        observed = chars / max(1, reply.prompt_tokens - overhead)
        calibration["chars_per_token"] = min(6.0, max(2.0, 0.5 * calibration["chars_per_token"] + 0.5 * observed * 0.98))
    return result


def summarize(cells: list[dict]) -> dict:
    def bucket(key: Callable[[dict], str]) -> dict:
        out: dict[str, dict] = {}
        for c in cells:
            if "outcome" not in c:
                continue
            b = out.setdefault(key(c), {o: 0 for o in sorted(OUTCOMES)})
            b[c["outcome"]] += 1
        for b in out.values():
            graded = b["pass"] + b["fail"]
            b["accuracy"] = round(b["pass"] / graded, 3) if graded else None
        return out

    latency: dict[str, dict] = {}
    for size in sorted({c["target_tokens"] for c in cells if c.get("prompt_tokens")}):
        ok = [c for c in cells if c["target_tokens"] == size and c.get("prompt_tokens")]
        prefill = [c["prefill_seconds_est"] for c in ok if c.get("prefill_seconds_est") is not None]
        latency[str(size)] = {
            "cells": len(ok),
            "median_prompt_tokens": statistics.median(c["prompt_tokens"] for c in ok),
            "median_seconds": statistics.median(c["seconds"] for c in ok),
            "median_prefill_seconds_est": statistics.median(prefill) if prefill else None,
            "max_reused_prefix_tokens": max((c.get("reused_prefix_tokens") or 0) for c in ok),
            "incoherent": sum(1 for c in ok if c.get("coherent") is False),
        }
    return {
        "by_size": bucket(lambda c: str(c["target_tokens"])),
        "by_probe_size": bucket(lambda c: f"{c['probe']}@{c['target_tokens']}"),
        "by_style_size": bucket(lambda c: f"{c['style']}@{c['target_tokens']}"),
        "by_depth_size": bucket(lambda c: f"d{c['depth']:g}@{c['target_tokens']}"),
        "latency_by_size": latency,
        "tool_loop_reuse": {
            "cells": sum(1 for c in cells if c["style"] == "tool_loop" and c.get("prompt_tokens")),
            "reused": sum(1 for c in cells if c["style"] == "tool_loop" and c.get("reuse")),
        },
    }


def _save(receipt: dict, out: str | None) -> None:
    if not out:
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # never leave a half-written receipt behind


def validate_options(opts: argparse.Namespace, context: int) -> None:
    if not 1 <= opts.max_tokens <= 256:
        # Every probe's answer is a value or one tool call; a large budget
        # would only eat context the measurement needs.
        sys.exit("--max-tokens must be 1..256")
    if not 0 < opts.timeout <= TIMEOUT_CEILING:
        sys.exit(f"--timeout must be between 0 and {TIMEOUT_CEILING} seconds")
    if not 1 <= opts.trials <= 20:
        sys.exit("--trials must be 1..20")
    if any(not 0.0 <= d <= 1.0 for d in opts.depths):
        sys.exit("--depths are fractions of the history, 0..1")
    default_max = context - opts.max_tokens - OUTPUT_RESERVE
    max_size = opts.max_size if opts.max_size is not None else default_max
    if max_size > context:
        sys.exit(f"--max-size {max_size} exceeds the engine context ({context})")
    too_big = [s for s in opts.sizes if s > max_size]
    if too_big:
        sys.exit(f"sizes {too_big} exceed the max size {max_size} (context {context} minus --max-tokens "
                 f"{opts.max_tokens} minus a {OUTPUT_RESERVE}-token reserve). Pass --max-size up to {context} "
                 "to probe the limit on purpose; a refusal there is recorded as context_overflow.")
    if any(s < 256 for s in opts.sizes):
        sys.exit("--sizes must be at least 256 tokens")
    opts.max_size = max_size


def run_eval(client: EngineClient, opts: argparse.Namespace, receipt: dict) -> dict:
    """Run every planned cell not already done, saving after each one."""
    show = (lambda label, text: print(f"  [{label}] raw output: {text!r}", file=sys.stderr)) \
        if opts.show_output else (lambda label, text: None)
    runtime = client.metrics().get("runtime", {}) or {}
    context = int(runtime.get("context_tokens") or opts.context or 8192)
    validate_options(opts, context)
    tools = load_probe_tools()
    previous: dict = {}
    if opts.resume and opts.out and Path(opts.out).is_file():
        previous = json.loads(Path(opts.out).read_text(encoding="utf-8"))
        if previous.get("schema") != SCHEMA:
            sys.exit(f"--resume: {opts.out} is not a {SCHEMA} receipt")
    done = {c["id"]: c for c in previous.get("cells", []) if c.get("outcome") in DONE_OUTCOMES}
    calibration = previous.get("calibration") or {"chars_per_token": opts.chars_per_token}
    receipt.update({
        "schema": SCHEMA, "prompt_response_logging": False,
        "started_at_utc": previous.get("started_at_utc", receipt.get("started_at_utc", _utc())),
        "engine_context_tokens": context, "build_info": client.build_info(),
        "runtime_at_start": {k: runtime.get(k) for k in ("context_tokens", "n_batch", "n_ubatch", "n_threads",
                                                         "n_threads_batch", "speculate_tokens")},
        "plan": {"sizes": sorted(opts.sizes), "depths": list(opts.depths), "trials": opts.trials,
                 "probes": list(opts.probes), "styles": list(opts.styles), "max_tokens": opts.max_tokens,
                 "max_size": opts.max_size, "timeout": opts.timeout,
                 "stop_after_failures": opts.stop_after_failures, "probe_tools": list(PROBE_TOOL_NAMES)},
        "resumed_cells": len(done), "calibration": calibration, "stopped_early": None,
    })
    if "decode_seconds_per_token" not in calibration:
        # A short prompt and a few dozen tokens out: nearly all decode time.
        # Long-prompt prefill is then estimated as total minus this per token.
        try:
            reply = client.chat(client.new_session(),
                                [{"role": "user", "content": "Count from one to forty in words, separated by commas."}],
                                None, 32)
            calibration["decode_seconds_per_token"] = round(reply.seconds / max(1, reply.completion_tokens), 4)
            calibration["decode_tokens"] = reply.completion_tokens
        except RequestFailed as failed:
            calibration["decode_calibration_error"] = failed.reason
            if failed.outcome == "timeout":
                client.wait_idle(max(opts.timeout, IDLE_WAIT_FLOOR))
    cells = plan_cells(opts)
    results: list[dict] = []
    failures = 0
    for index, cell in enumerate(cells, 1):
        if cell["id"] in done:
            results.append(done[cell["id"]])
            continue
        if opts.stop_after_failures and failures >= opts.stop_after_failures:
            receipt["stopped_early"] = {"reason": "failure_limit", "failures": failures,
                                        "cells_not_run": len(cells) - index + 1}
            break
        result = run_cell(client, cell, opts, calibration, tools, show)
        results.append(result)
        if result["outcome"] in ("fail", "error", "timeout"):
            failures += 1
        print(f"[{index}/{len(cells)}] {cell['id']}: {result['outcome'].upper()} ({result['reason']}; "
              f"{result.get('prompt_tokens', '-')} tok, {result.get('seconds')}s, "
              f"reused {result.get('reused_prefix_tokens', '-')})", file=sys.stderr)
        receipt.update({"cells": results, "summary": summarize(results), "updated_at_utc": _utc(),
                        "complete": False})
        _save(receipt, opts.out)
    receipt.update({"cells": results, "summary": summarize(results), "updated_at_utc": _utc(),
                    "complete": receipt["stopped_early"] is None and len(results) == len(cells)})
    _save(receipt, opts.out)
    return receipt


def _csv(kind: Callable[[str], Any], allowed: tuple | None = None) -> Callable[[str], tuple]:
    def parse(text: str) -> tuple:
        items = tuple(kind(x.strip()) for x in text.split(",") if x.strip())
        if not items:
            raise argparse.ArgumentTypeError("empty list")
        if allowed is not None and set(items) - set(allowed):
            raise argparse.ArgumentTypeError(f"unknown: {sorted(set(items) - set(allowed))}; choose from {allowed}")
        return items
    return parse


def add_eval_arguments(p: argparse.ArgumentParser, *, include_context: bool = True) -> None:
    p.add_argument("--sizes", type=_csv(int), default=DEFAULT_SIZES,
                   help="target prompt sizes in tokens, comma-separated (default 1000,2000,4000,6000)")
    p.add_argument("--depths", type=_csv(float), default=DEFAULT_DEPTHS,
                   help="where facts are planted, as fractions of the history (default 0.1,0.5,0.9)")
    p.add_argument("--trials", type=int, default=1, help="repeats with different planted values (default 1)")
    p.add_argument("--probes", type=_csv(str, PROBES), default=PROBES, help=f"subset of {','.join(PROBES)}")
    p.add_argument("--styles", type=_csv(str, STYLES), default=STYLES, help=f"subset of {','.join(STYLES)}")
    p.add_argument("--max-tokens", type=int, default=96, help="output tokens per answer (default 96)")
    p.add_argument("--max-size", type=int,
                   help="largest allowed size; default context - max-tokens - 256. Up to the context to probe the limit.")
    p.add_argument("--chars-per-token", type=float, default=DEFAULT_CHARS_PER_TOKEN,
                   help="initial sizing estimate; recalibrated from real prompt_tokens as the run goes")
    # A laptop CPU can take minutes to read thousands of tokens, and a client
    # that gives up leaves the engine busy until it finishes.
    p.add_argument("--timeout", type=float, default=1800, help="seconds per request (default 1800)")
    p.add_argument("--stop-after-failures", type=int, default=0,
                   help="stop after this many failed/errored cells (default 0 = never)")
    p.add_argument("--show-output", action="store_true", help="print raw model output to stderr (never the receipt)")
    p.add_argument("--resume", action="store_true", help="skip cells already finished in the --out receipt")
    if include_context:
        p.add_argument("--context", type=int, default=None, help="engine context if /metrics does not report it")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint", required=True, help="http://127.0.0.1:PORT/v1/chat/completions")
    p.add_argument("--token-stdin", action="store_true", help=f"read the bearer token from stdin (else ${ev.TOKEN_ENV})")
    p.add_argument("--out", help="write (and keep updating) the JSON receipt here")
    add_eval_arguments(p)
    opts = p.parse_args(argv)
    token = (sys.stdin.read(ev.TOKEN_MAX_BYTES + 1) if opts.token_stdin else os.environ.get(ev.TOKEN_ENV, "")).strip()
    if not token or len(token) > ev.TOKEN_MAX_BYTES:
        sys.exit(f"no bearer token: set {ev.TOKEN_ENV} or pass --token-stdin")
    ev.validate_endpoint(opts.endpoint)
    client = EngineClient(opts.endpoint.removesuffix("/v1/chat/completions"), token, opts.timeout)
    if client.lifecycle() is None:
        sys.exit(f"no engine answering at {opts.endpoint} with this token (GET /readyz failed)")
    receipt = run_eval(client, opts, {"started_at_utc": _utc(), "engine_launched_by_tool": False})
    print(json.dumps(receipt["summary"], indent=2, sort_keys=True))
    if opts.out:
        print(f"\nreceipt written: {Path(opts.out).resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
