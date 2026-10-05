#!/usr/bin/env python3
"""Engine checks for the features added on 2026-10-03, on the real model and this machine.

  --check snapshots   two conversations interleaved A1 B1 A2 B2 A3 on ONE engine: each must resume
                      from its OWN saved state (not re-read its history), and A3's reply must equal
                      a cold run's (same prompt in a fresh session).
  --check idle        start with --idle-unload-minutes 1, answer one message, stay quiet: the engine
                      must free the model (lifecycle stays READY), the next message must reload and
                      be answered, and the conversation's saved state must survive the unload.
                      Also records the engine process's memory before and after.

Short prompts, five-token answers: on a CPU each request is seconds to a couple of minutes.
Receipts hold counts and timings only (no prompt, reply or token).

    python local/bmo_engine_probe.py --engine local\\bin\\lae-engine.exe --model C:\\bmo-transfer\\Qwen3.5-9B-Q4_K_M.gguf --check snapshots
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bmo_local  # noqa: E402

MODEL_NAME = "qwen35-9b-q4-k-m"


def process_memory_mb(pid: int | None) -> int | None:
    """Resident memory of `pid` in MB, or None when it cannot be read."""
    if not pid:
        return None
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20).stdout
            fields = [f.strip('"') for f in out.strip().split('","')]
            return round(int("".join(ch for ch in fields[-1].split(" K")[0] if ch.isdigit())) / 1024) if len(fields) >= 5 else None
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=20).stdout.strip()
        return round(int(out) / 1024) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def post(eng, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{eng.base}{path}", data=json.dumps(body or {}).encode(), method="POST")
    request.add_header("Authorization", f"Bearer {eng.token}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=3600) as response:
        return json.loads(response.read())


def chat(eng, session: str, messages: list[dict]) -> dict:
    started = time.monotonic()
    payload = post(eng, "/v1/chat/completions", {"model": MODEL_NAME, "stream": False, "max_tokens": 5, "mode": "normal",
                                                 "session_id": session, "messages": messages})
    metrics = eng.get("/metrics")[1]
    return {"reply": payload["choices"][0]["message"]["content"], "prompt_tokens": payload["usage"]["prompt_tokens"],
            "seconds": round(time.monotonic() - started, 1), "runtime": metrics.get("runtime", {}), "lifecycle": metrics.get("lifecycle")}


def say(word: str) -> dict:
    return {"role": "user", "content": f"Reply with the single word: {word}"}


def verdict_snapshots(steps: dict, cold: dict) -> dict:
    restored = lambda name: (steps[name]["runtime"].get("last_restored_snapshot_tokens") or 0) > 0  # noqa: E731
    return {
        "A2_restored_after_B_used_the_engine": restored("A2"),
        "B2_restored_after_A_used_the_engine": restored("B2"),
        "A3_restored": restored("A3"),
        "two_snapshots_held": (steps["B1"]["runtime"].get("snapshot_count") or 0) >= 2,
        "A3_reply_equals_a_cold_run": steps["A3"]["reply"] == cold["reply"] and steps["A3"]["prompt_tokens"] == cold["prompt_tokens"],
    }


def verdict_idle(before: dict, idle: dict, after: dict, loaded_mb: int | None, idle_mb: int | None, unloaded_after: int | None) -> dict:
    out = {
        "unloaded_while_idle": unloaded_after is not None and idle["runtime"].get("model_loaded") is False,
        "engine_stayed_ready": idle["lifecycle"] == "READY",
        "next_message_reloaded_and_answered": bool(after["reply"].strip()) and after["runtime"].get("model_loaded") is True
                                              and after["runtime"].get("reloads") == 1,
        "saved_state_survived_the_unload": (after["runtime"].get("last_restored_snapshot_tokens") or 0) > 0,
    }
    # Memory is judged only when the loaded figure is believable (a mapped model that was never read shows little).
    if loaded_mb and idle_mb is not None and loaded_mb > 2000:
        out["memory_dropped_over_2_GB"] = loaded_mb - idle_mb > 2000
    return out


def check_snapshots(eng) -> dict:
    a, b = post(eng, "/v1/sessions")["id"], post(eng, "/v1/sessions")["id"]
    steps: dict[str, dict] = {}

    def step(name, session, history):
        steps[name] = chat(eng, session, history)
        r = steps[name]
        print(f"[{name}] {r['seconds']}s prompt={r['prompt_tokens']} reused={r['runtime'].get('last_reused_prefix_tokens')} "
              f"restored={r['runtime'].get('last_restored_snapshot_tokens')} snapshots={r['runtime'].get('snapshot_count')}/{r['runtime'].get('snapshot_slots')}", flush=True)
        return r["reply"]

    ha, hb = [say("alpha")], [say("bravo")]
    ra1, rb1 = step("A1", a, ha), step("B1", b, hb)
    ha2 = ha + [{"role": "assistant", "content": ra1}, say("charlie")]
    hb2 = hb + [{"role": "assistant", "content": rb1}, say("delta")]
    ra2 = step("A2", a, ha2)
    step("B2", b, hb2)
    ha3 = ha2 + [{"role": "assistant", "content": ra2}, say("echo")]
    step("A3", a, ha3)
    cold = chat(eng, post(eng, "/v1/sessions")["id"], ha3)
    print(f"[A3 cold run] {cold['seconds']}s restored={cold['runtime'].get('last_restored_snapshot_tokens')}", flush=True)
    return {"steps": {k: {x: v[x] for x in ("seconds", "prompt_tokens")} | {"restored": v["runtime"].get("last_restored_snapshot_tokens"),
            "snapshot_count": v["runtime"].get("snapshot_count")} for k, v in steps.items()},
            "verdict": verdict_snapshots(steps, cold)}


def check_idle(eng, wait_seconds: int) -> dict:
    pid = eng.proc.pid if getattr(eng, "proc", None) else None
    session = post(eng, "/v1/sessions")["id"]
    history = [say("alpha")]
    before = chat(eng, session, history)
    loaded_mb = process_memory_mb(pid)
    print(f"[first message] {before['seconds']}s loaded={before['runtime'].get('model_loaded')} memory_mb={loaded_mb}", flush=True)
    started, unloaded_after = time.monotonic(), None
    while time.monotonic() - started < wait_seconds:
        time.sleep(5)
        metrics = eng.get("/metrics")[1]
        if metrics.get("runtime", {}).get("model_loaded") is False:
            unloaded_after = round(time.monotonic() - started)
            break
    idle_metrics = eng.get("/metrics")[1]
    idle = {"runtime": idle_metrics.get("runtime", {}), "lifecycle": idle_metrics.get("lifecycle")}
    idle_mb = process_memory_mb(pid)
    print(f"[quiet] unloaded after {unloaded_after}s lifecycle={idle['lifecycle']} memory_mb={idle_mb}", flush=True)
    history2 = history + [{"role": "assistant", "content": before["reply"]}, say("beta")]
    after = chat(eng, session, history2)
    print(f"[after reload] {after['seconds']}s reloads={after['runtime'].get('reloads')} restored={after['runtime'].get('last_restored_snapshot_tokens')} "
          f"memory_mb={process_memory_mb(pid)}", flush=True)
    return {"first_message_seconds": before["seconds"], "reload_message_seconds": after["seconds"], "unloaded_after_seconds": unloaded_after,
            "memory_mb": {"loaded": loaded_mb, "idle": idle_mb}, "verdict": verdict_idle(before, idle, after, loaded_mb, idle_mb, unloaded_after)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", choices=("snapshots", "idle"), required=True)
    p.add_argument("--engine", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--backend", default="cpu", choices=("cpu", "intel-vulkan"))
    p.add_argument("--vulkan-device-name")
    p.add_argument("--threads", type=int)
    p.add_argument("--threads-batch", type=int, dest="threads_batch")
    p.add_argument("--wait-seconds", type=int, default=240, help="idle check: how long to wait for the unload (the engine's limit is 60 s + a few)")
    p.add_argument("--out", help="receipt path (default local/out/engine-probe-<check>-<time>.json)")
    p.add_argument("--log", default=str(HERE / "out" / "engine-probe.log"))
    args = p.parse_args(argv)
    extra = ["--idle-unload-minutes", "1"] if args.check == "idle" else []
    parsed = bmo_local.build_parser().parse_args(["serve", "--engine", args.engine, "--model", args.model, "--backend", args.backend,
                                                  "--log", args.log, *extra] + (["--vulkan-device-name", args.vulkan_device_name] if args.vulkan_device_name else [])
                                                 + (["--threads", str(args.threads)] if args.threads else [])
                                                 + (["--threads-batch", str(args.threads_batch)] if args.threads_batch else []))
    with bmo_local.Engine(parsed) as eng:
        result = check_snapshots(eng) if args.check == "snapshots" else check_idle(eng, args.wait_seconds)
    ok = all(result["verdict"].values())
    print(json.dumps(result["verdict"], indent=2))
    print(f"ENGINE PROBE ({args.check}): " + ("OK" if ok else "FAILED"), flush=True)
    out = Path(args.out) if args.out else HERE / "out" / f"engine-probe-{args.check}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"schema": "local_bmo.engine-probe.v1", "prompt_response_logging": False, "check": args.check,
                               "passed": ok, **result}, indent=2) + "\n", encoding="utf-8")
    print(f"receipt: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
