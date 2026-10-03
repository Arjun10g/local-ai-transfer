#!/usr/bin/env python3
"""Does the memory note keep what plain dropping loses? A real-model probe.

The host keeps a long conversation inside the 8,192-token window by eliding
old tool results and dropping whole oldest turns. With ``memory.mode:
'summary'`` (host/agent/memory-note.mjs) the dropped part is first summarised
into a short note that is pinned at the start of every later prompt. This
script measures that note with the real model, using the SAME prompt file the
host ships (host/agent/memory-prompts.json; its sha256 is in the receipt):

1. build a synthetic conversation with planted facts (a name, a number, a
   preference, a decision, a value inside a tool result, and a value that is
   later corrected), split it into a DROPPED part and the RETAINED recent
   turns;
2. generate the note from the dropped part with the shared prompt, in
   ``--chunks`` successive calls so the incremental merge (current note +
   new excerpt) is exercised, and sanitise/bound it exactly as the host does;
3. ask one recall question per fact with ONLY the note plus the retained
   turns as context (arm ``note``), and the same questions with the retained
   turns alone (arm ``drop``: today's plain dropping, the control), and
   optionally with the whole conversation (arm ``full``: the ceiling);
4. grade by exact/regex match against the planted values -- never a model
   judge -- per fact type and fact age, plus a ``hallucination`` probe (a
   value that was never stated; a good answer invents nothing) and a
   ``stale`` probe (the corrected value; the answer must give the newer one).

A fact planted in the retained part is asked too (``retained_control``): it
must pass in every arm, which shows the harness, not the note, is sound.

Receipts (schema ``local_bmo.memory-eval.v1``) carry counts, booleans and
timings only: ``prompt_response_logging`` is false and no prompt, note or
model text is ever written (``--show-output`` prints raw text to stderr
only). Requests are bounded (``--max-requests``, refused before the first
request), each has a timeout, and a run resumes with ``--resume``: whole
conversations are the unit, so a half-finished one is re-run from its note.

The string handling below mirrors memory-note.mjs (fill, sanitise, excerpt,
bound) except credential masking, which this script's synthetic content never
needs; tests/model/test_memory_eval.py checks both render identically.

Run it against an engine that is already serving (bearer token in
``LAE_EVAL_TOKEN``, or on stdin with ``--token-stdin``)::

    LAE_EVAL_TOKEN=... python3 scripts/test/memory_eval.py \\
        --endpoint http://127.0.0.1:PORT/v1/chat/completions \\
        --out records/memory-eval.json --conversations 6

The engine is ``lae-engine serve`` with the pinned Qwen3.5-9B Q4_K_M and an
8,192-token context. Requests are about ``conversations x (chunks + 2 x 8)``
(about 110 with the defaults); on a laptop CPU each is minutes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test import evaluate_tool_calls as ev  # noqa: E402
from scripts.test import long_context_eval as lce  # noqa: E402

SCHEMA = "local_bmo.memory-eval.v1"
PROMPTS_PATH = ROOT / "host" / "agent" / "memory-prompts.json"
# note: the model-written note; drop: plain dropping (the control); full: no compaction (the ceiling);
# recall: the dropped turns searched by keyword (host/agent/memory-recall.mjs); both: note plus recall.
# recall_gap: recall, but each question is worded to share NOTHING with the fact except its subject.
ARMS = ("note", "drop", "full", "recall", "both", "recall_gap")
DEFAULT_ARMS = ("note", "drop")
FACT_TYPES = ("name", "number", "preference", "decision", "tool_result", "updated")
PROBES = FACT_TYPES + ("retained_control", "hallucination")
AGE_BUCKETS = ((1, 5), (6, 10), (11, 20), (21, 40), (41, 1000))
TIMEOUT_CEILING = 3600
# Host defaults (host/agent/memory-note.mjs MEMORY_DEFAULTS) and the host's
# conservative bytes-per-token estimate (context-budget.mjs).
HOST_BYTES_PER_TOKEN = 3.0
DEFAULT_NOTE_TOKENS = 512
DEFAULT_NOTE_BYTES = 1536
DEFAULT_MAX_INPUT_BYTES = 6144
DEFAULT_PER_MESSAGE_BYTES = 1536
DONE_OUTCOMES = frozenset({"pass", "fail", "context_overflow", "request_too_large", "skipped"})
OUTCOMES = DONE_OUTCOMES | {"timeout", "engine_busy", "error"}
REASONS = frozenset({
    "recalled", "missing_fact", "distractor_answer", "latest_value", "stale_value", "abstained",
    "invented_value", "note_failed", "engine_invalid_request", "engine_request_too_large",
    "client_timeout", "engine_busy",
})


# ---------------------------------------------------------------------------
# The shared prompt file, and mirrors of host/agent/memory-note.mjs
# ---------------------------------------------------------------------------

def load_prompts(path: Path = PROMPTS_PATH) -> tuple[dict, str]:
    raw = path.read_bytes()
    prompts = json.loads(raw.decode("utf-8"))
    for key in ("version", "system", "user_template", "output_instruction", "empty_note", "note_label",
                "recall_label", "note_message_template"):
        if not isinstance(prompts.get(key), str) or not prompts[key]:
            raise ValueError(f"memory-prompts.json: {key} must be a non-empty string")
    return prompts, hashlib.sha256(raw).hexdigest()


# JavaScript's String.prototype.trim() set, spelled out: Python's str.strip()
# also strips U+001C..U+001F and U+0085, which JavaScript keeps.
_JS_SPACE = "".join(chr(c) for c in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
                                       0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF))
# memory-note.mjs SPACE_CODES (collapseSpaces).
_SPACES = frozenset(chr(c) for c in (0x20, 0x09, 0x0A, 0x0D, 0x0C, 0x0B, 0xA0, 0x1680, *range(0x2000, 0x200B),
                                     0x2028, 0x2029, 0x202F, 0x205F, 0x3000))
_CONTROL_TOKEN = re.compile(r"<\|[^<>|]{0,64}\|>")
# JavaScript `$` without the m flag is the end of the input: Python `\Z`.
_MARKUP_TAG = re.compile(r"<[ \t\n\r\f\v]*/?[ \t\n\r\f\v]*(?:tool_call|tool_response|tools|think|function|parameter)\b"
                         r"[^<>]{0,256}(?:>|\Z)", re.IGNORECASE | re.ASCII)
_CONTROL_PIECE = re.compile(r"<\||\|>|\b(?:im_start|im_end|endoftext)\b", re.IGNORECASE | re.ASCII)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_ELIDED = re.compile(r"^\[Earlier tool result elided to fit the context window: \d+ bytes removed\.\]$")
_TOOL_CALL_HINT = re.compile(r"<tool_call>|<function=", re.IGNORECASE | re.ASCII)
_CALL_NAMES = re.compile(r"<(?:function|parameter)=([^<>\n]{1,96})>")


def js_trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def fill_template(template: str, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def strip_markup(text: str) -> str:
    out = "".join(ch for ch in str(text or "") if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))
    for _ in range(16):
        nxt = _CONTROL_PIECE.sub("", _MARKUP_TAG.sub("", _CONTROL_TOKEN.sub("", out)))
        if nxt == out:
            break
        out = nxt
    return out


def drop_reasoning(text: str) -> str:
    out = str(text or "")
    closes = list(re.finditer(r"</think>", out, re.IGNORECASE | re.ASCII))
    if closes:
        out = out[closes[-1].end():]
    opened = re.search(r"<think>", out, re.IGNORECASE | re.ASCII)
    return out[:opened.start()] if opened else out


def cut_utf8(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    return text if len(data) <= max_bytes else data[:max(0, max_bytes)].decode("utf-8", errors="ignore")


def bound_lines(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    kept: list[str] = []
    size = 0
    for line in text.split("\n"):
        cost = len(line.encode("utf-8")) + (1 if kept else 0)
        if size + cost > max_bytes:
            break
        kept.append(line)
        size += cost
    if kept:
        return "\n".join(kept).rstrip(_JS_SPACE)
    return cut_utf8(text.split("\n")[0], max_bytes).rstrip(_JS_SPACE)


def sanitize_note(text: str, max_bytes: int | None = None) -> str:
    """memory-note.mjs sanitizeNote, without the credential mask."""
    lines = [line.rstrip(" \t") for line in strip_markup(drop_reasoning(text)).split("\n")]
    out = js_trim(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)))
    return out if max_bytes is None else bound_lines(out, max_bytes)


def collapse_spaces(text: str) -> str:
    out, run = [], False
    for ch in text:
        if ch in _SPACES:
            if not run:
                out.append(" ")
            run = True
        else:
            out.append(ch)
            run = False
    return "".join(out)


def _is_tool_call_text(message: dict) -> bool:
    content = message.get("content")
    if message.get("role") != "assistant" or not isinstance(content, str):
        return False
    if _TOOL_CALL_HINT.search(content):
        return True
    trimmed = js_trim(content)
    if not trimmed.startswith("{"):
        return False
    try:
        value = json.loads(trimmed)
    except ValueError:
        return False
    return isinstance(value, dict) and isinstance(value.get("name"), str) and "arguments" in value


def _prepare_excerpt(prompts: dict, message: dict) -> tuple[str, str] | None:
    content = message.get("content")
    role = message.get("role")
    if not isinstance(content, str) or (role == "tool" and _ELIDED.match(content)):
        return None
    labels = prompts["excerpt"]
    call = _is_tool_call_text(message)
    if role == "tool":
        label = fill_template(labels["tool"], {"name": strip_markup(message.get("name") or "tool")})
    elif role == "assistant":
        label = labels["assistant_tool_call"] if call else labels["assistant"]
    elif role == "user":
        label = labels["user"]
    else:
        return None
    text = _CALL_NAMES.sub(lambda m: f" {m.group(1)}: ", content) if call else content
    text = strip_markup(text)
    text = js_trim(collapse_spaces(text).replace("<<<", "< < <").replace(">>>", "> > >"))
    return (label, text) if text else None


def _render_excerpt(prompts: dict, prepared: tuple[str, str], max_bytes: int) -> str:
    label, text = prepared
    marker = prompts["excerpt"]["truncation_marker"]
    if len(text.encode("utf-8")) > max_bytes:
        text = cut_utf8(text, max_bytes - len(marker.encode("utf-8"))).rstrip(_JS_SPACE) + marker
    return fill_template(prompts["excerpt"]["line_template"], {"role": label, "content": text})


def excerpt_line(prompts: dict, message: dict, per_message_bytes: int = DEFAULT_PER_MESSAGE_BYTES) -> str | None:
    prepared = _prepare_excerpt(prompts, message)
    return _render_excerpt(prompts, prepared, per_message_bytes) if prepared else None


MIN_LINE_BYTES = 96


def build_excerpt(prompts: dict, messages: list[dict], max_bytes: int = DEFAULT_MAX_INPUT_BYTES,
                  per_message_bytes: int = DEFAULT_PER_MESSAGE_BYTES) -> dict:
    """memory-note.mjs buildExcerpt: water-fill the per-message cap, then drop oldest lines."""
    separator = prompts["excerpt"]["separator"]
    sep_bytes = len(separator.encode("utf-8"))
    prepared = [p for p in (_prepare_excerpt(prompts, m) for m in messages) if p is not None]

    def at(cap: int) -> list[str]:
        return [_render_excerpt(prompts, p, cap) for p in prepared]

    def joined(lines: list[str]) -> int:
        return sum(len(line.encode("utf-8")) for line in lines) + max(0, len(lines) - 1) * sep_bytes
    hi = min(per_message_bytes, max_bytes)
    lo = min(MIN_LINE_BYTES, hi)
    lines = at(hi)
    if joined(lines) > max_bytes:
        while lo < hi:
            mid = (lo + hi + 1) >> 1
            if joined(at(mid)) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        lines = at(lo)
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        cost = len(line.encode("utf-8")) + (sep_bytes if kept else 0)
        if size + cost > max_bytes:
            break
        kept.insert(0, line)
        size += cost
    return {"text": separator.join(kept), "lines": len(lines), "included": len(kept),
            "omitted": len(lines) - len(kept), "bytes": size}


def note_byte_limit(note_tokens: int = DEFAULT_NOTE_TOKENS, note_bytes: int = DEFAULT_NOTE_BYTES) -> int:
    return min(note_bytes, math.floor(note_tokens * HOST_BYTES_PER_TOKEN))


def summary_request(prompts: dict, note: str, excerpt: str, note_limit_bytes: int) -> list[dict]:
    max_words = max(16, note_limit_bytes // prompts["bytes_per_word"])
    instruction = fill_template(prompts["output_instruction"], {"max_words": max_words})
    user = fill_template(prompts["user_template"], {"note": note or prompts["empty_note"], "excerpt": excerpt,
                                                    "output_instruction": instruction})
    return [{"role": "system", "content": prompts["system"]}, {"role": "user", "content": user}]


def note_message(prompts: dict, note: str) -> dict:
    return {"role": "user", "content": fill_template(prompts["note_message_template"],
                                                     {"label": prompts["note_label"], "note": note})}


# ---------------------------------------------------------------------------
# Recall: a mirror of host/agent/memory-recall.mjs (the reference), kept in
# this one file because the remote harness is uploaded and hashed as a single
# file. Credential masking is omitted, as above. tests/model/
# memory_recall_vectors.json holds shared vectors both implementations must
# reproduce (tests/model/test_memory_eval.py, tests/host/memory-recall.test.mjs).
# ---------------------------------------------------------------------------

RECALL_ENTRY_BYTES = 320
RECALL_BYTES = 1280
RECALL_ENTRIES = 8
RECALL_ARCHIVE_BYTES = 2097152
RECALL_MAX_ENTRIES = 24000
RECALL_MAX_ENTRIES_PER_MESSAGE = 40
RECALL_MAX_LEAVES = 80
RECALL_MIN_ENTRY_CHARS = 8
RECALL_MARKER = " [...]"
_WORD = re.compile(r"[^\W_]+")
_STOP = frozenset(
    "a an the is was were are be been am what which who whom whose when where how why do does did for of to in on at by "
    "from with about as and or but if then than that this these those it its i me my we our you your he she they them "
    "there here please tell remind earlier before previously said told mentioned again now can could would will just so "
    "not no yes ok okay thanks thank reply only one any all".split())
_JS_WS = "[" + re.escape(_JS_SPACE) + "]"
_SENTENCE_BREAK = re.compile(rf"(?<=[.!?]){_JS_WS}+(?=[A-Z0-9\"'(\[])")
_NAME_KEYS = frozenset({"path", "file", "filename", "name", "id", "url", "title", "key", "workspace_id"})
_CORRECTION = re.compile(r"\b(?:correction|corrected|actually|instead|no longer|changed|updated|update|now)\b",
                         re.IGNORECASE | re.ASCII)
_CALL_NAME = re.compile(r"<function=([^<>\n]{1,96})>")
_CALL_PARAM = re.compile(r"<parameter=([^<>\n]{1,96})>\s*(.*?)\s*</parameter>", re.DOTALL)
_RECALL_K1, _RECALL_B = 1.2, 0.75
_RECALL_MIN_SCORE, _RECALL_RELATIVE = 1.0, 0.35


def recall_stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def recall_words(text: str) -> list[str]:
    return [recall_stem(w) for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


def recall_anchor_words(text: str) -> list[str]:
    out: list[str] = []
    for index, match in enumerate(_WORD.finditer(str(text or ""))):
        raw = match.group(0)
        lower = raw.lower()
        if lower in _STOP:
            continue
        has_digit = any(unicodedata.category(ch).startswith("N") for ch in raw)
        if (index > 0 and len(raw) > 1 and raw[0].isupper()) or has_digit:
            out.append(recall_stem(lower))
    return list(dict.fromkeys(out))


def _recall_clean(text: str) -> str:
    return js_trim(collapse_spaces(strip_markup(text)))


def _recall_bound(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    return cut_utf8(text, max_bytes - len(RECALL_MARKER.encode("utf-8"))).rstrip(_JS_SPACE) + RECALL_MARKER


def _recall_sentences(text: str, max_bytes: int) -> list[str]:
    out: list[str] = []
    for part in _SENTENCE_BREAK.split(text):
        rest = js_trim(part)
        while len(rest) >= RECALL_MIN_ENTRY_CHARS:
            if len(rest.encode("utf-8")) <= max_bytes:
                out.append(rest)
                break
            head = cut_utf8(rest, max_bytes)
            space = head.rfind(" ")
            if space > len(head) / 2:
                head = head[:space]
            if not head:
                break
            out.append(js_trim(head))
            rest = js_trim(rest[len(head):])
    return out


def _recall_prose(text: str, max_bytes: int) -> list[str]:
    return [s for line in re.split(r"[\n\r]+", strip_markup(text)) for s in _recall_sentences(_recall_clean(line), max_bytes)]


def _recall_leaves(value: Any, prefix: str, out: list, depth: int = 0) -> None:
    if len(out) >= RECALL_MAX_LEAVES or value is None:
        return
    if isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
        return
    if isinstance(value, (int, float)):
        out.append((prefix, str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)))
        return
    if isinstance(value, str):
        out.append((prefix, value))
        return
    if depth >= 6 or not isinstance(value, (dict, list)):
        return
    items = list(enumerate(value)) if isinstance(value, list) else list(value.items())
    for key, item in items:
        _recall_leaves(item, f"{prefix}.{key}" if prefix else str(key), out, depth + 1)


def _recall_tool_entries(message: dict, entry_bytes: int) -> list[str]:
    name = _recall_clean(message.get("name") or "tool")
    text = js_trim(message["content"])
    parsed: Any = None
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    label = f"Tool result ({name})"
    if not isinstance(parsed, (dict, list)):
        return [f"{label}: {line}" for line in _recall_prose(text, entry_bytes - len(label.encode()) - 2)]
    flat: list[tuple[str, str]] = []
    _recall_leaves(parsed, "", flat)
    identity = next((leaf for leaf in flat if leaf[0].split(".")[-1].lower() in _NAME_KEYS), None)
    head = _recall_clean(f"{label}{' ' + identity[1] if identity else ''}:")
    room = max(32, entry_bytes - len(head.encode("utf-8")) - 1)
    out: list[str] = []
    current = ""
    for key, value in flat:
        if identity and key == identity[0]:
            continue
        pair = _recall_clean(f"{key}={value}")
        if not pair:
            continue
        piece = _recall_bound(pair, room)
        if current and len(f"{current}; {piece}".encode("utf-8")) > room:
            out.append(f"{head} {current}")
            current = ""
        current = f"{current}; {piece}" if current else piece
    if current:
        out.append(f"{head} {current}")
    return out


def recall_entries(prompts: dict, message: dict, entry_bytes: int = RECALL_ENTRY_BYTES) -> list[str]:
    content = message.get("content")
    role = message.get("role")
    if (not isinstance(content, str) or (role == "tool" and _ELIDED.match(content)) or
            (role == "user" and content.startswith(prompts["recall_label"]))):
        return []
    if role == "tool":
        return _recall_tool_entries(message, entry_bytes)
    if role == "assistant" and _TOOL_CALL_HINT.search(content):
        match = _CALL_NAME.search(content)
        if not match:
            return []
        params = "; ".join(f"{m.group(1)}={m.group(2)}" for m in _CALL_PARAM.finditer(content))
        return [_recall_bound(_recall_clean(f"Assistant called {match.group(1)}{' with ' + params if params else ''}"),
                              entry_bytes)]
    if role not in ("user", "assistant"):
        return []
    label = "User" if role == "user" else "Assistant"
    return [f"{label}: {line}" for line in _recall_prose(content, entry_bytes - len(label.encode()) - 2)]


class RecallArchive:
    """memory-recall.mjs RecallArchive: entries, BM25 search with name anchors, byte and line bounds."""

    def __init__(self, prompts: dict, recall_bytes: int = RECALL_BYTES, recall_entries: int = RECALL_ENTRIES,
                 entry_bytes: int = RECALL_ENTRY_BYTES, archive_bytes: int = RECALL_ARCHIVE_BYTES) -> None:
        self.prompts, self.recall_bytes, self.recall_entries = prompts, recall_bytes, recall_entries
        self.entry_bytes, self.archive_bytes = entry_bytes, archive_bytes
        self.entries: list[dict] = []
        self.bytes = 0
        self.seq = 0

    def add(self, messages: list[dict]) -> int:
        added = 0
        for message in messages:
            for text in recall_entries(self.prompts, message, self.entry_bytes)[:RECALL_MAX_ENTRIES_PER_MESSAGE]:
                tokens = recall_words(text)
                if not tokens:
                    continue
                size = len(text.encode("utf-8"))
                self.entries.append({"seq": self.seq, "text": text, "tokens": tokens, "bytes": size})
                self.seq += 1
                self.bytes += size
                added += 1
        while len(self.entries) > RECALL_MAX_ENTRIES or self.bytes > self.archive_bytes:
            self.bytes -= self.entries.pop(0)["bytes"]
        return added

    def search(self, queries: list[tuple[str, float]]) -> list[str]:
        entries = self.entries
        n = len(entries)
        if not n:
            return []
        weights: dict[str, float] = {}
        for text, weight in queries:
            for word in dict.fromkeys(recall_words(text)):
                weights[word] = max(weights.get(word, 0.0), weight)
        if not weights:
            return []
        df: dict[str, int] = {}
        for entry in entries:
            for word in dict.fromkeys(entry["tokens"]):
                if word in weights:
                    df[word] = df.get(word, 0) + 1
        if not df:
            return []
        anchors = recall_anchor_words(queries[0][0] if queries else "")
        present = [w for w in anchors if w in df]
        if anchors and not present:
            return []
        average = sum(len(e["tokens"]) for e in entries) / n
        scored: list[tuple[float, dict]] = []
        for entry in entries:
            tf: dict[str, int] = {}
            for word in entry["tokens"]:
                if word in weights:
                    tf[word] = tf.get(word, 0) + 1
            if not tf or (present and not any(w in tf for w in present)):
                continue
            score = 0.0
            for word, count in tf.items():
                idf = math.log(1 + (n - df[word] + 0.5) / (df[word] + 0.5))
                score += (weights[word] * idf * (count * (_RECALL_K1 + 1)) /
                          (count + _RECALL_K1 * (1 - _RECALL_B + _RECALL_B * len(entry["tokens"]) / average)))
            if _CORRECTION.search(entry["text"]):
                score *= 1.1
            scored.append((score, entry))
        if not scored:
            return []
        scored.sort(key=lambda item: (-item[0], -item[1]["seq"]))
        top = scored[0][0]
        if not present and top < _RECALL_MIN_SCORE:
            return []
        picked: list[dict] = []
        size = len(self.prompts["recall_label"].encode("utf-8")) + 1
        for score, entry in scored:
            if (len(picked) >= self.recall_entries or score < top * _RECALL_RELATIVE or
                    (not present and score < _RECALL_MIN_SCORE * 0.5)):
                break
            cost = len(entry["text"].encode("utf-8")) + 3
            if size + cost > self.recall_bytes:
                continue
            picked.append(entry)
            size += cost
        return [e["text"] for e in sorted(picked, key=lambda e: e["seq"])]


def recall_queries(message: str, previous: str | None = None) -> list[tuple[str, float]]:
    queries = [(str(message or ""), 1.0)]
    if previous:
        queries.append((str(previous), 0.5))
    return queries


def recall_message(prompts: dict, lines: list[str]) -> dict | None:
    if not lines:
        return None
    return {"role": "user", "content": prompts["recall_label"] + "\n" + "\n".join(f"- {line}" for line in lines)}


# ---------------------------------------------------------------------------
# Synthetic conversations
# ---------------------------------------------------------------------------

# attribute phrase, value generator, the sentence that states it
FACTS: dict[str, tuple[str, Callable[[random.Random], str], str]] = {
    "name": ("on-call engineer", lce._name, "By the way, the {a} for project {p} is {v}."),
    "number": ("badge number", lambda rng: str(rng.randint(10000, 99999)), "For the access form: the {a} for project {p} is {v}."),
    "preference": ("preferred report format",
                   lambda rng: f"{rng.choice(('two-column', 'one-page-memo', 'bulleted-brief', 'table-first'))}-{rng.randint(10, 99)}",
                   "Please remember my preference: the {a} for project {p} is {v}."),
    "decision": ("chosen release branch", lce._code_word, "We decided it in the meeting: the {a} for project {p} is {v}."),
    "updated": ("meeting room", lambda rng: f"R-{rng.randint(100, 999)}", "Note this down: the {a} for project {p} is {v}."),
}
UPDATE_SENTENCE = "Correction to what I said earlier: the {a} for project {p} is now {v}."
NUMBER_SHAPE = re.compile(r"(?<![A-Za-z0-9])\d{5}(?![A-Za-z0-9])")


def _fact_unit(text: str) -> list[dict]:
    return [{"role": "user", "content": text + " Please keep it in mind."},
            {"role": "assistant", "content": "Got it, I will remember that."}]


def build_conversation(seed: str, dropped_tokens: int, retained_tokens: int) -> dict:
    """Facts in the dropped part at spread positions; one control in the retained part."""
    rng = random.Random(seed)
    projects = rng.sample(lce.PROJECTS, len(lce.PROJECTS))
    facts: list[dict] = []
    for i, ftype in enumerate(FACT_TYPES):
        project = projects[i]
        if ftype == "tool_result":
            needle = lce.ToolResultNeedle(f"invoices/{project.lower()}-{rng.randint(1000, 9999)}.json",
                                          f"{rng.randint(10000, 99999)}.{rng.randint(10, 99)}", 0.0, "target")
            facts.append({"type": ftype, "project": project, "value": needle.total, "path": needle.path,
                          "unit": needle.units()})
            continue
        attribute, make, sentence = FACTS[ftype]
        value = make(rng)
        fact = {"type": ftype, "project": project, "attribute": attribute, "value": value,
                "unit": _fact_unit(sentence.format(a=attribute, p=project, v=value))}
        if ftype == "updated":
            newer = value
            while newer == value:
                newer = make(rng)
            fact.update({"stale": value, "value": newer,
                         "update_unit": _fact_unit(UPDATE_SENTENCE.format(a=attribute, p=project, v=newer))})
        facts.append(fact)
    control_value = str(rng.randint(10000, 99999))
    control = {"type": "retained_control", "project": projects[len(FACT_TYPES)], "attribute": "badge number",
               "value": control_value,
               "unit": _fact_unit(FACTS["number"][2].format(a="badge number", p=projects[len(FACT_TYPES)], v=control_value))}
    absent = projects[len(FACT_TYPES) + 1]  # never mentioned: the hallucination probe asks about it

    def filler(budget_tokens: int, tag: str) -> list[list[dict]]:
        frng = random.Random(seed + "|" + tag)
        n_units = max(3, min(14, budget_tokens // 180))
        per_unit = max(200, int(budget_tokens / n_units * lce.DEFAULT_CHARS_PER_TOKEN))
        units = [lce.FILLER_KINDS[i % len(lce.FILLER_KINDS)](frng, per_unit) for i in range(n_units)]
        frng.shuffle(units)
        return units

    dropped = filler(dropped_tokens, "dropped")
    # Spread the dropped facts; the stale value early and its correction
    # late, so with --chunks 2 they land in different summarisation calls.
    slots = {0: [], len(dropped): []}
    order = [f for f in facts if f["type"] != "updated"]
    for k, fact in enumerate(order):
        slots.setdefault(1 + (k * (len(dropped) - 1)) // max(1, len(order)), []).append(fact["unit"])
    updated = next(f for f in facts if f["type"] == "updated")
    slots[0].insert(0, updated["unit"])
    slots[len(dropped)].append(updated["update_unit"])
    units: list[tuple[str, list[dict]]] = []
    for k in range(len(dropped) + 1):
        units += [("fact", u) for u in slots.get(k, [])]
        if k < len(dropped):
            units.append(("filler", dropped[k]))
    retained_units = filler(retained_tokens, "retained")
    retained_units.insert(len(retained_units) // 2, control["unit"])
    # The host never sends a history that opens with anything but a user
    # message; a retained part that would is started at its first user turn.
    while retained_units and retained_units[0][0]["role"] != "user":
        units.append(("filler", retained_units.pop(0)))
    dropped_messages = [m for _, u in units for m in u]
    retained_messages = [m for u in retained_units for m in u]
    everything = dropped_messages + retained_messages
    user_turns_after = lambda index: sum(1 for m in everything[index + 1:] if m["role"] == "user") + 1  # noqa: E731

    def locate(unit: list[dict]) -> int:
        return next(i for i in range(len(everything)) if everything[i] is unit[0])
    for fact in facts:
        fact["age_turns"] = user_turns_after(locate(fact.get("update_unit") or fact["unit"]))
        fact["in_dropped"] = True
    control["age_turns"] = user_turns_after(locate(control["unit"]))
    control["in_dropped"] = False
    return {"facts": facts, "control": control, "absent_project": absent, "dropped": dropped_messages,
            "retained": retained_messages}


def question_for(fact: dict) -> str:
    if fact["type"] == "tool_result":
        return f"In the earlier fs.read_text result for {fact['path']}, what was the invoice_total? Reply with only the number."
    attribute = fact["attribute"]
    now = " now" if fact["type"] == "updated" else ""
    return f"What is the {attribute} for project {fact['project']}{now}? Reply with only the {attribute}."


def gap_question_for(fact: dict) -> str:
    """The same question in words that appear nowhere in the fact (only the subject does)."""
    kind, project = fact["type"], fact.get("project", "")
    if kind == "tool_result":
        return (f"What did that billing file {fact['path']} come to? Reply with only the number.")
    gaps = {
        "name": f"Remind me who covers pager duty for project {project}. Reply with only the name.",
        "number": f"What was the access code I gave you for project {project}? Reply with only the number.",
        "preference": f"How do I like my write-ups laid out for project {project}? Reply with only the layout.",
        "decision": f"Where is the code for project {project} cut from again? Reply with only the name.",
        "updated": f"Which space did we book for project {project} catch-ups these days? Reply with only the room.",
    }
    return gaps[kind]


def grade(fact: dict, output: str) -> tuple[bool, str]:
    if fact["type"] == "updated":
        has_new, has_old = lce._contains(output, fact["value"]), lce._contains(output, fact["stale"])
        if has_new and not has_old:
            return True, "latest_value"
        return (False, "stale_value") if has_old else (False, "missing_fact")
    return lce._grade_single(fact["value"], [])(output)


def grade_hallucination(output: str) -> tuple[bool, str]:
    # Any five-digit number is an invented badge number: none was ever
    # stated for this project.
    return (False, "invented_value") if NUMBER_SHAPE.search(re.sub(r"(?<=\d),(?=\d{3}\b)", "", output)) \
        else (True, "abstained")


def hallucination_question(project: str) -> str:
    return (f"What is the badge number for project {project}? If it was never mentioned, say that you do not know. "
            "Reply with only the badge number.")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _age_bucket(age: int) -> str:
    for lo, hi in AGE_BUCKETS:
        if lo <= age <= hi:
            return f"{lo}-{hi}" if hi < 1000 else f"{lo}+"
    return "0"


def planned_requests(opts: argparse.Namespace) -> int:
    questions = len(FACT_TYPES) + 2
    return opts.conversations * (opts.chunks * bool({"note", "both"} & set(opts.arms)) + questions * len(opts.arms))


def make_note(client: lce.EngineClient, prompts: dict, dropped: list[dict], opts: argparse.Namespace,
              show: Callable[[str, str], None], label: str) -> tuple[str | None, dict]:
    """The host's summarisation, chunk by chunk: each call merges into the running note."""
    limit = note_byte_limit(opts.note_tokens, opts.note_bytes)
    size = math.ceil(len(dropped) / opts.chunks)
    note = ""
    meta: dict[str, Any] = {"chunks": opts.chunks, "prompt_tokens": [], "completion_tokens": 0, "seconds": 0.0,
                            "markup_removed": False, "truncated": False, "omitted_messages": 0}
    for k in range(opts.chunks):
        chunk = dropped[k * size:(k + 1) * size]
        excerpt = build_excerpt(prompts, chunk, opts.max_input_bytes, opts.per_message_bytes)
        meta["omitted_messages"] += excerpt["omitted"]
        session = client.new_session()
        reply = client.chat(session, summary_request(prompts, note, excerpt["text"], limit), None, opts.note_tokens)
        show(f"{label} note chunk {k + 1}", reply.content)
        meta["prompt_tokens"].append(reply.prompt_tokens)
        meta["completion_tokens"] += reply.completion_tokens
        meta["seconds"] = round(meta["seconds"] + reply.seconds, 2)
        cleaned = sanitize_note(reply.content)
        meta["markup_removed"] |= strip_markup(drop_reasoning(reply.content)) != reply.content
        bounded = bound_lines(cleaned, limit)
        meta["truncated"] |= bounded != cleaned
        note = bounded
    meta["note_bytes"] = len(note.encode("utf-8"))
    meta["note_lines"] = note.count("\n") + 1 if note else 0
    return (note or None), meta


def run_conversation(client: lce.EngineClient, prompts: dict, index: int, opts: argparse.Namespace,
                     show: Callable[[str, str], None]) -> dict:
    seed = f"memory|{index}"
    conv = build_conversation(seed, opts.dropped_tokens, opts.retained_tokens)
    result: dict[str, Any] = {
        "id": f"conv{index}", "dropped_messages": len(conv["dropped"]), "retained_messages": len(conv["retained"]),
        "dropped_estimated_tokens": lce.estimate_tokens(conv["dropped"], None, lce.DEFAULT_CHARS_PER_TOKEN),
        "retained_estimated_tokens": lce.estimate_tokens(conv["retained"], None, lce.DEFAULT_CHARS_PER_TOKEN),
        "cells": [],
    }
    note: str | None = None
    if {"note", "both"} & set(opts.arms):
        try:
            note, meta = make_note(client, prompts, conv["dropped"], opts, show, result["id"])
            # Booleans only: whether each planted value made it into the
            # note, never the note itself.
            meta["facts_in_note"] = {f["type"]: bool(note) and lce._contains(note, f["value"]) for f in conv["facts"]}
            stale = next(f for f in conv["facts"] if f["type"] == "updated")["stale"]
            meta["stale_value_in_note"] = bool(note) and lce._contains(note, stale)
            meta["outcome"] = "ok" if note else "empty"
        except lce.RequestFailed as failed:
            meta = {"outcome": failed.outcome, "reason": failed.reason}
            if failed.outcome == "timeout":
                client.wait_idle(max(opts.timeout, lce.IDLE_WAIT_FLOOR))
        result["note"] = meta
    probes: list[tuple[str, dict | None, str, Callable[[str], tuple[bool, str]]]] = []
    for fact in conv["facts"] + [conv["control"]]:
        probes.append((fact["type"], fact, question_for(fact), lambda out, f=fact: grade(f, out)))
    probes.append(("hallucination", None, hallucination_question(conv["absent_project"]), grade_hallucination))
    archive = None
    if {"recall", "both", "recall_gap"} & set(opts.arms):
        archive = RecallArchive(prompts)
        archive.add(conv["dropped"])
        # Whether retrieval found each planted value (booleans only): separates
        # "the lines never came back" from "the model did not use them".
        result["recall"] = {"archive_entries": len(archive.entries), "facts_retrieved": {
            f["type"]: any(lce._contains(line, f["value"]) for line in archive.search(recall_queries(question_for(f))))
            for f in conv["facts"]},
            "facts_retrieved_gap": {
                f["type"]: any(lce._contains(line, f["value"]) for line in archive.search(recall_queries(gap_question_for(f))))
                for f in conv["facts"]}}
    for arm in opts.arms:
        if arm in ("note", "both"):
            context = ([note_message(prompts, note)] if note else []) + conv["retained"]
        elif arm in ("drop", "recall", "recall_gap"):
            context = list(conv["retained"])
        else:
            context = conv["dropped"] + conv["retained"]
        for probe, fact, question, grader in probes:
            cell: dict[str, Any] = {"arm": arm, "probe": probe,
                                    "age_turns": fact["age_turns"] if fact else None,
                                    "in_dropped": fact["in_dropped"] if fact else None}
            if arm in ("note", "both") and not note:
                cell.update({"outcome": "skipped", "passed": None, "reason": "note_failed"})
                result["cells"].append(cell)
                continue
            asked = context
            if arm == "recall_gap" and fact is not None and probe in FACT_TYPES:
                question = gap_question_for(fact)
            if arm in ("recall", "both", "recall_gap"):
                block = recall_message(prompts, archive.search(recall_queries(question)))
                cell["recall_lines"] = block["content"].count("\n") if block else 0
                if block:
                    asked = context + [block]
            try:
                reply = client.chat(client.new_session(), asked + [{"role": "user", "content": question}], None,
                                    opts.max_tokens)
            except lce.RequestFailed as failed:
                cell.update({"outcome": failed.outcome, "passed": None, "reason": failed.reason})
                if failed.outcome == "timeout":
                    client.wait_idle(max(opts.timeout, lce.IDLE_WAIT_FLOOR))
                result["cells"].append(cell)
                continue
            show(f"{result['id']} {arm}/{probe}", reply.content)
            passed, reason = grader(reply.content)
            coherent, _ = lce.coherence(reply.content)
            cell.update({"outcome": "pass" if passed else "fail", "passed": passed,
                         "reason": reason if reason in REASONS else "missing_fact", "coherent": coherent,
                         "prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens,
                         "seconds": round(reply.seconds, 2)})
            result["cells"].append(cell)
    result["complete"] = all(c["outcome"] in DONE_OUTCOMES for c in result["cells"])
    return result


def summarize(conversations: list[dict]) -> dict:
    cells = [c for conv in conversations for c in conv["cells"]]

    def bucket(key: Callable[[dict], str | None]) -> dict:
        out: dict[str, dict] = {}
        for c in cells:
            k = key(c)
            if k is None:
                continue
            b = out.setdefault(k, {"pass": 0, "fail": 0, "not_graded": 0})
            b["pass" if c["outcome"] == "pass" else "fail" if c["outcome"] == "fail" else "not_graded"] += 1
        for b in out.values():
            graded = b["pass"] + b["fail"]
            b["accuracy"] = round(b["pass"] / graded, 3) if graded else None
        return out

    by_arm_probe = bucket(lambda c: f"{c['arm']}/{c['probe']}")
    benefit = {}
    for probe in PROBES:
        a, b = by_arm_probe.get(f"note/{probe}", {}).get("accuracy"), by_arm_probe.get(f"drop/{probe}", {}).get("accuracy")
        benefit[probe] = round(a - b, 3) if a is not None and b is not None else None
    notes = [conv["note"] for conv in conversations if conv.get("note", {}).get("outcome") == "ok"]
    kept = {t: sum(1 for n in notes if n["facts_in_note"].get(t)) for t in FACT_TYPES}
    return {
        "by_arm_probe": by_arm_probe,
        "by_arm_age": bucket(lambda c: f"{c['arm']}/{_age_bucket(c['age_turns'])}" if c["in_dropped"] else None),
        "note_minus_drop": benefit,
        "notes": {"generated": len(notes), "failed": sum(1 for conv in conversations if "note" in conv and conv["note"].get("outcome") != "ok"),
                  "facts_kept_by_type": kept, "stale_value_kept": sum(1 for n in notes if n.get("stale_value_in_note")),
                  "markup_removed": sum(1 for n in notes if n.get("markup_removed")),
                  "truncated": sum(1 for n in notes if n.get("truncated")),
                  "median_note_bytes": sorted(n["note_bytes"] for n in notes)[len(notes) // 2] if notes else None},
    }


def validate_options(opts: argparse.Namespace) -> None:
    if not 1 <= opts.conversations <= 50:
        sys.exit("--conversations must be 1..50")
    if not 1 <= opts.chunks <= 4:
        sys.exit("--chunks must be 1..4")
    if not 1 <= opts.max_tokens <= 256:
        sys.exit("--max-tokens must be 1..256")
    if not 32 <= opts.note_tokens <= 1024 or not 128 <= opts.note_bytes <= 4096:
        sys.exit("--note-tokens must be 32..1024 and --note-bytes 128..4096 (the host's bounds)")
    if not 512 <= opts.max_input_bytes <= 32768 or not 128 <= opts.per_message_bytes <= opts.max_input_bytes:
        sys.exit("--max-input-bytes must be 512..32768 and --per-message-bytes 128..max-input-bytes")
    if not 0 < opts.timeout <= TIMEOUT_CEILING:
        sys.exit(f"--timeout must be between 0 and {TIMEOUT_CEILING} seconds")
    if not 500 <= opts.dropped_tokens <= 6000 or not 300 <= opts.retained_tokens <= 5000:
        sys.exit("--dropped-tokens must be 500..6000 and --retained-tokens 300..5000")
    planned = planned_requests(opts)
    if planned > opts.max_requests:
        sys.exit(f"this plan needs {planned} requests, more than --max-requests {opts.max_requests}")


def run_eval(client: lce.EngineClient, opts: argparse.Namespace, receipt: dict) -> dict:
    validate_options(opts)
    show = (lambda label, text: print(f"  [{label}] raw output: {text!r}", file=sys.stderr)) \
        if opts.show_output else (lambda label, text: None)
    prompts, digest = load_prompts()
    previous: dict = {}
    if opts.resume and opts.out and Path(opts.out).is_file():
        previous = json.loads(Path(opts.out).read_text(encoding="utf-8"))
        if previous.get("schema") != SCHEMA:
            sys.exit(f"--resume: {opts.out} is not a {SCHEMA} receipt")
        if previous.get("memory_prompts", {}).get("sha256") != digest:
            sys.exit("--resume: the receipt was made with a different memory-prompts.json; start a new receipt")
    done = {c["id"]: c for c in previous.get("conversations", []) if c.get("complete")}
    receipt.update({
        "schema": SCHEMA, "prompt_response_logging": False,
        "started_at_utc": previous.get("started_at_utc", receipt.get("started_at_utc", lce._utc())),
        "build_info": client.build_info(),
        "memory_prompts": {"path": str(PROMPTS_PATH.relative_to(ROOT)), "version": prompts["version"], "sha256": digest},
        "plan": {"conversations": opts.conversations, "arms": list(opts.arms), "chunks": opts.chunks,
                 "dropped_tokens": opts.dropped_tokens, "retained_tokens": opts.retained_tokens,
                 "note_tokens": opts.note_tokens, "note_bytes": opts.note_bytes,
                 "note_byte_limit": note_byte_limit(opts.note_tokens, opts.note_bytes),
                 "max_input_bytes": opts.max_input_bytes, "per_message_bytes": opts.per_message_bytes,
                 "max_tokens": opts.max_tokens, "timeout": opts.timeout, "planned_requests": planned_requests(opts),
                 "credential_masking": "not applied (synthetic content has no credentials)"},
        "resumed_conversations": len(done),
    })
    results: list[dict] = []
    for index in range(opts.conversations):
        cid = f"conv{index}"
        if cid in done:
            results.append(done[cid])
            continue
        result = run_conversation(client, prompts, index, opts, show)
        results.append(result)
        graded = [c for c in result["cells"] if c["outcome"] in ("pass", "fail")]
        print(f"[{index + 1}/{opts.conversations}] {cid}: note {result.get('note', {}).get('outcome', '-')}, "
              f"{sum(c['outcome'] == 'pass' for c in graded)}/{len(graded)} graded cells passed", file=sys.stderr)
        receipt.update({"conversations": results, "summary": summarize(results), "updated_at_utc": lce._utc(),
                        "complete": False})
        lce._save(receipt, opts.out)
    receipt.update({"conversations": results, "summary": summarize(results), "updated_at_utc": lce._utc(),
                    "complete": len(results) == opts.conversations and all(c["complete"] for c in results)})
    lce._save(receipt, opts.out)
    return receipt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint", required=True, help="http://127.0.0.1:PORT/v1/chat/completions (loopback only)")
    p.add_argument("--token-stdin", action="store_true", help=f"read the bearer token from stdin (else ${ev.TOKEN_ENV})")
    p.add_argument("--out", help="write (and keep updating) the JSON receipt here")
    p.add_argument("--conversations", type=int, default=6, help="synthetic conversations, each with its own facts (default 6)")
    p.add_argument("--arms", type=lce._csv(str, ARMS), default=DEFAULT_ARMS,
                   help="note (note + retained turns), drop (retained turns only: the control), full (everything: "
                        "the ceiling). Default note,drop")
    p.add_argument("--chunks", type=int, default=2, help="summarisation calls per note; >1 exercises the merge (default 2)")
    p.add_argument("--dropped-tokens", type=int, default=2500, help="size of the dropped part (default 2500)")
    p.add_argument("--retained-tokens", type=int, default=1500, help="size of the retained recent turns (default 1500)")
    p.add_argument("--note-tokens", type=int, default=DEFAULT_NOTE_TOKENS, help="the host's memory.note_tokens (default 256)")
    p.add_argument("--note-bytes", type=int, default=DEFAULT_NOTE_BYTES, help="the host's memory.note_bytes (default 1024)")
    p.add_argument("--max-input-bytes", type=int, default=DEFAULT_MAX_INPUT_BYTES, help="excerpt bound per call (default 6144)")
    p.add_argument("--per-message-bytes", type=int, default=DEFAULT_PER_MESSAGE_BYTES, help="per-message excerpt bound (default 1536)")
    p.add_argument("--max-tokens", type=int, default=64, help="output tokens per recall answer (default 64)")
    p.add_argument("--max-requests", type=int, default=400, help="refuse a plan needing more requests (default 400)")
    p.add_argument("--timeout", type=float, default=1800, help="seconds per request (default 1800)")
    p.add_argument("--show-output", action="store_true", help="print raw model output and notes to stderr (never the receipt)")
    p.add_argument("--resume", action="store_true", help="skip conversations already complete in the --out receipt")
    opts = p.parse_args(argv)
    token = (sys.stdin.read(ev.TOKEN_MAX_BYTES + 1) if opts.token_stdin else os.environ.get(ev.TOKEN_ENV, "")).strip()
    if not token or len(token) > ev.TOKEN_MAX_BYTES:
        sys.exit(f"no bearer token: set {ev.TOKEN_ENV} or pass --token-stdin")
    ev.validate_endpoint(opts.endpoint)
    client = CountingClient(opts.endpoint.removesuffix("/v1/chat/completions"), token, opts.timeout)
    if client.lifecycle() is None:
        sys.exit(f"no engine answering at {opts.endpoint} with this token (GET /readyz failed)")
    receipt = run_eval(client, opts, {"started_at_utc": lce._utc(), "engine_launched_by_tool": False})
    receipt["requests_sent_this_run"] = client.requests_sent
    lce._save(receipt, opts.out)
    print(json.dumps(receipt["summary"], indent=2, sort_keys=True))
    if opts.out:
        print(f"\nreceipt written: {Path(opts.out).resolve()}", file=sys.stderr)
    return 0


class CountingClient(lce.EngineClient):
    """The long-context client, counting chat requests for the receipt."""

    requests_sent = 0

    def chat(self, session: str, messages: list[dict], tools: list[dict] | None, max_tokens: int) -> lce.Reply:
        self.requests_sent += 1
        return super().chat(session, messages, tools, max_tokens)


if __name__ == "__main__":
    raise SystemExit(main())
