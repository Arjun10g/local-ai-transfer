#!/usr/bin/env python3
"""Does memory recall work through the REAL host and the REAL model on this machine?

Starts the engine and the web-UI host exactly as `bmo_app.py` does, then drives one
conversation over the host's own HTTP API (the same calls the browser makes):

  1. plants four facts (a number, a name, a room, and a later correction of the room),
  2. sends enough filler turns that the context window overflows and the first turns
     are compacted away (it checks that a compaction really happened),
  3. asks for the facts back: in the fact's own words, in words that share only the
     project name, the corrected value, and a project that was never mentioned.

With the default `--memory-mode recall` every answer must be right and the
never-mentioned project must not get an invented number. `--memory-mode off` is the
control: plain dropping, where the first three questions are expected to FAIL.

Raw model output is never written anywhere. The receipt (local/out/host-probe-*.json)
holds counts, timings and pass/fail only; `--show-output` prints the replies to the
console. On a CPU the filler turns dominate: about 75 turns of ~170 tokens each at the default
8,192-token window (a few seconds to ~15 s each), so allow 10-30 minutes, or lower `--context`
(2048 needs about 20 turns) to make it quicker; the check is the same.

    python local/bmo_host_probe.py --engine local\\bin\\lae-engine.exe --model C:\\bmo-transfer\\Qwen3.5-9B-Q4_K_M.gguf
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bmo_app  # noqa: E402
import bmo_chat  # noqa: E402

FILLER_SENTENCES = (
    "The dashboard numbers looked odd because the timezone was set to the server default.",
    "Most of the flaky tests were waiting on a fixed sleep instead of polling for readiness.",
    "The design review notes are in the shared folder under the October heading.",
    "If the vendor cannot confirm delivery by Thursday we should switch to the backup supplier.",
    "My laptop fan has been loud all morning; I think the indexer is running again.",
)
FILLER_TOKENS = 170  # what one filler turn costs, roughly (message + a short answer)
OPAQUE = re.compile(r"[^A-Za-z0-9_-]")

# (probe, question, must contain ALL of, must contain NONE of)
FACTS = (
    ("plant_number", "Remember this: the badge number for project Orion is 48213. Reply with just OK."),
    ("plant_name", "Also remember: the on-call engineer for project Juniper is Priya Raman. Reply with just OK."),
    ("plant_room", "The meeting room for project Halcyon is R-412. Reply with just OK."),
    ("plant_correction", "Correction: the meeting room for project Halcyon is now R-777. Reply with just OK."),
)
QUESTIONS = (
    ("number_exact", "What is the badge number for project Orion? Reply with only the number.", ("48213",), ()),
    ("name_gap", "Remind me who covers pager duty for project Juniper. Reply with only the name.", ("priya", "raman"), ()),
    ("room_corrected", "Which room is the Halcyon sync in now? Reply with only the room.", ("r-777",), ("r-412",)),
    ("absent_project", "What is the badge number for project Zebrafish? If it was never mentioned, say you do not know. "
                       "Reply with only the number, or I do not know.", (), ()),
)
FIVE_DIGITS = re.compile(r"(?<![A-Za-z0-9])\d{5}(?![A-Za-z0-9])")


def filler_message(index: int) -> str:
    text = " ".join(FILLER_SENTENCES[(index + k) % len(FILLER_SENTENCES)] for k in range(8))
    return f"Note {index + 1}: {text} Reply with just OK."


def filler_turns_for(context_tokens: int) -> int:
    """Enough filler that the window overflows with margin (about 1.6 windows of text)."""
    return max(6, math.ceil(1.6 * context_tokens / FILLER_TOKENS))


def grade(probe: str, reply: str, must: tuple[str, ...], must_not: tuple[str, ...]) -> tuple[bool, str]:
    low = reply.lower()
    if probe == "absent_project":
        # Any five-digit number is an invented badge number: none was ever given for this project.
        return (False, "invented_value") if FIVE_DIGITS.search(low) else (True, "abstained")
    if any(word in low for word in must_not):
        return False, "stale_value"
    return (True, "recalled") if all(word in low for word in must) else (False, "missing_fact")


def parse_sse(stream) -> list[dict]:
    """The events of one /api/chat response, in order."""
    events, buffer = [], ""
    for raw in stream:
        buffer += raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data = "\n".join(line[5:].strip() for line in frame.splitlines() if line.startswith("data:"))
            if data:
                try:
                    events.append(json.loads(data))
                except ValueError:
                    pass
    return events


def summarize_turn(events: list[dict]) -> dict:
    text, recall, compaction, failed, usage, note = "", None, 0, None, {}, None
    for event in events:
        kind, data = event.get("event") or event.get("type"), event.get("data") or {}
        if kind == "message.delta":
            text += data.get("text", "")
        elif kind == "message.completed":
            text = data.get("text", text)
            usage = data.get("usage") or {}
        elif kind == "request.failed":
            failed = data.get("code") or "failed"
        elif kind == "metrics.snapshot":
            if data.get("context_compaction"):
                compaction += 1
            if data.get("memory_recall"):
                recall = data["memory_recall"]
            if data.get("memory_note"):
                note = data["memory_note"]
    return {"text": text, "recall": recall, "note": note, "compactions": compaction, "failed": failed,
            "prompt_tokens": usage.get("prompt_tokens")}


class Host:
    def __init__(self, base: str, nonce: str):
        self.base = base
        self.token = json.loads(self._call("/bootstrap", {"nonce": nonce}).read())["token"]

    def _call(self, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        request.add_header("Origin", self.base)
        request.add_header("Sec-Fetch-Site", "same-origin")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, timeout=3600)

    def turn(self, session: str, number: int, message: str) -> dict:
        started = time.monotonic()
        body = {"session_id": session, "request_id": f"req_probe_{number:04d}", "message": message, "tools": "off"}
        with self._call("/api/chat", body, token=self.token) as response:
            summary = summarize_turn(parse_sse(response))
        summary["seconds"] = round(time.monotonic() - started, 1)
        return summary


def run_probe(host: Host, memory_mode: str, filler_turns: int, show) -> dict:
    session, number = "ses_probe_0001", 0
    turns, compactions, planted_failed = [], 0, []

    def send(label: str, message: str) -> dict:
        nonlocal number, compactions
        number += 1
        result = host.turn(session, number, message)
        compactions += result["compactions"]
        turns.append({"n": number, "label": label, "seconds": result["seconds"], "prompt_tokens": result["prompt_tokens"],
                      "failed": result["failed"], "recall_lines": (result["recall"] or {}).get("lines", 0),
                      "note_state": (result.get("note") or {}).get("state"), "note_ms": (result.get("note") or {}).get("duration_ms")})
        show(f"turn {number} {label}", result["text"])
        note = result.get("note") or {}
        print(f"  [{number:>2}] {label:<16} {result['seconds']:>6}s prompt={result['prompt_tokens']} "
              f"compactions={result['compactions']} recall={(result['recall'] or {}).get('lines', 0)}"
              f"{'  note=' + str(note.get('state')) + ' ' + str(note.get('duration_ms')) + 'ms' if note else ''}"
              f"{'  FAILED ' + result['failed'] if result['failed'] else ''}", flush=True)
        return result

    for label, message in FACTS:
        if send(label, message)["failed"]:
            planted_failed.append(label)
    for i in range(filler_turns):
        if send("filler", filler_message(i))["failed"]:
            planted_failed.append(f"filler{i}")
    checks = []
    for probe, question, must, must_not in QUESTIONS:
        result = send(probe, question)
        passed, reason = (False, "request_failed") if result["failed"] else grade(probe, result["text"], must, must_not)
        expected_to_fail = memory_mode == "off" and probe != "absent_project"
        checks.append({"probe": probe, "passed": passed, "reason": reason, "expected_to_fail": expected_to_fail,
                       "recall_lines": (result["recall"] or {}).get("lines", 0)})
    return {"turns": len(turns), "compactions": compactions, "turn_failures": planted_failed, "checks": checks,
            "slowest_turn_seconds": max(t["seconds"] for t in turns), "turn_log": turns}


def verdict(outcome: dict, memory_mode: str) -> tuple[bool, list[str]]:
    problems = []
    if outcome["turn_failures"]:
        problems.append(f"turns failed: {outcome['turn_failures']}")
    if outcome["compactions"] < 1:
        problems.append("the context never overflowed, so recall was not exercised: use a smaller --context or more --filler-turns")
    for check in outcome["checks"]:
        ok = (not check["passed"]) if check["expected_to_fail"] else check["passed"]
        if not ok:
            problems.append(f"{check['probe']}: {check['reason']}{' (expected to fail in off mode but passed)' if check['expected_to_fail'] else ''}")
    if memory_mode != "off":
        recalled = [c for c in outcome["checks"] if c["probe"] != "absent_project" and c["recall_lines"] > 0]
        if len(recalled) < 2:
            problems.append("fewer than 2 questions were answered with a recalled block (memory_recall events)")
        absent = next(c for c in outcome["checks"] if c["probe"] == "absent_project")
        if absent["recall_lines"]:
            problems.append("a never-mentioned project retrieved lines")
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    bmo_chat._console_safe_output()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    bmo_chat.add_engine_args(p, "engine-probe.log")
    p.add_argument("--node", help="path to node (default: the one on PATH)")
    p.add_argument("--memory-mode", choices=("off", "recall", "summary"), default="recall")
    p.add_argument("--filler-turns", type=int, help="default: enough to overflow the window about 1.6 times")
    p.add_argument("--out", help="receipt path (default local/out/host-probe-<mode>-<time>.json)")
    p.add_argument("--host-timeout", type=float, default=120)
    p.add_argument("--show-output", action="store_true", help="print the model's replies to the console (never saved)")
    args = p.parse_args(argv)
    node = bmo_app.find_node(args.node)
    show = (lambda label, text: print(f"      {label}: {text.strip()[:200]!r}", file=sys.stderr)) if args.show_output else (lambda *_: None)

    eng = bmo_chat.ChatEngine(args)
    print("starting the engine and loading the model (this can take a minute)...", file=sys.stderr)
    eng.start()
    host = config_path = None
    try:
        model, backend = bmo_app.engine_identity(eng)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump({"version": "0.1.0", "memory": {"mode": args.memory_mode}}, handle)
            config_path = handle.name
        env = bmo_app.host_environment(os.environ, port=eng.port, token=eng.token, model=model, backend=backend,
                                       config=config_path)
        host = bmo_app.start_host(node, env)
        url = bmo_app.read_bootstrap_url(host, args.host_timeout)
        if not url:
            print("the host did not start; its error is above", file=sys.stderr)
            return 1
        bmo_app.forward_host_output(host)
        base, nonce = url.split("/#bootstrap=")
        runtime = eng.get("/metrics")[1].get("runtime", {})
        context = int(runtime.get("context_tokens") or 8192)
        turns = args.filler_turns or filler_turns_for(context)
        print(f"memory mode {args.memory_mode}, context {context} tokens, {turns} filler turns", flush=True)
        outcome = run_probe(Host(base, nonce), args.memory_mode, turns, show)
        ok, problems = verdict(outcome, args.memory_mode)
        for check in outcome["checks"]:
            mark = "PASS" if check["passed"] else "FAIL"
            note = "  (expected FAIL in off mode)" if check["expected_to_fail"] else ""
            print(f"  {mark} {check['probe']:<16} {check['reason']}{note}", flush=True)
        print("HOST PROBE: OK" if ok else "HOST PROBE: FAILED -- " + "; ".join(problems), flush=True)
        out = Path(args.out) if args.out else HERE / "out" / f"host-probe-{args.memory_mode}-{time.strftime('%Y%m%dT%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"schema": "local_bmo.host-probe.v1", "prompt_response_logging": False,
                                   "memory_mode": args.memory_mode, "backend": backend, "context_tokens": context,
                                   "passed": ok, "problems": problems, "outcome": outcome,
                                   "speculate": args.speculate, "snapshots": args.snapshots}, indent=2) + "\n", encoding="utf-8")
        print(f"receipt: {out}", flush=True)
        return 0 if ok else 1
    finally:
        if host is not None:
            bmo_app.stop_host(host, False)
        if config_path:
            try:
                os.unlink(config_path)
            except OSError:
                pass
        eng.stop()


if __name__ == "__main__":
    raise SystemExit(main())
