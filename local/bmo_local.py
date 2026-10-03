#!/usr/bin/env python3
"""Run the local assistant engine on your own machine, and test it.

This is the ADR-0007 personal-testing path: one operator, their own laptop.
It does not replace the parked enterprise launchers in `release/windows/`,
which deliberately refuse until attestation-grade controls exist.

It works the same on Windows and macOS/Linux. The engine's bearer token is
generated here and handed over on stdin, never on the command line and never
in a file: on Windows the engine refuses `--token-file` outright, because a
pathname cannot be bound to an ACL check without a race
(`native/main.cpp`, `read_token_file`).

Commands
  smoke   start the engine, check its endpoints, run one chat completion,
          report model-load time and first-response latency, stop.
  eval    start the engine and score the shipping tool-call fixture.
          `--cases id,id` scores only those cases; `--show-output` also prints
          the model's raw output to this console. Raw output is never written
          to the receipt, so `prompt_response_logging` stays false.
  bench   measure this machine: how fast the engine reads a prompt, how fast
          it writes tokens, and how much a second turn saves by reusing the
          prompt prefix the context already holds.
  longctx measure whether long conversations hold up: at growing prompt
          sizes, does the model still recall planted facts, use an old tool
          result, emit a correct tool call, and how does latency grow.
          Exact-match graded; see scripts/test/long_context_eval.py.
  serve   start the engine and keep it running until Ctrl+C, printing the
          endpoint. The bearer token is printed only with `--print-token`:
          a console is often recorded or shared, and nothing else here
          ever shows the token.
  preflight  before a demo: check Python, Node, the engine, the model, disk,
          memory and power, then start the engine and the web UI host once
          and check every endpoint they rely on. One PASS/WARN/FAIL line
          per check, a fix for each problem; exits 1 on any FAIL. See
          `local/bmo_preflight.py`.
  delegate-key  print the key a coding assistant (the BMO MCP bridge) uses to
          send BMO jobs, creating it on first use; `--rotate` replaces it.
          It is printed to this console only, for pasting once into the
          coding assistant's secret prompt. It is stored in the per-user BMO
          state folder, readable only by you; it never goes on a command
          line. Jobs stay off unless BMO is started with delegation on.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import platform
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test import evaluate_tool_calls as ev  # noqa: E402
from scripts.test import long_context_eval as lce  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "model" / "production_tool_call_eval.json"
MODEL_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
MODEL_SIZE = 5629109088
# The evaluator's own ceiling is 600 s, which remote runs rely on; see
# `run_local`'s `timeout_ceiling`.
LOCAL_TIMEOUT_CEILING = 3600


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Engine:
    """A running `lae-engine serve`, token on stdin, logs to a file.

    `popen_kwargs` are extra `subprocess.Popen` options for the engine process,
    e.g. `bmo_chat` starts it outside the terminal's Ctrl+C group. The pipes
    and the log are this class's to set, so those keys cannot be overridden.
    """

    def __init__(self, args: argparse.Namespace, popen_kwargs: dict | None = None):
        self.args = args
        self.popen_kwargs = dict(popen_kwargs or {})
        self.token = secrets.token_urlsafe(32)
        self.log_path = Path(args.log).resolve()
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.ready_seconds: float | None = None

    def start(self) -> "Engine":
        engine = Path(self.args.engine).resolve()
        model = Path(self.args.model).resolve()
        if not engine.is_file():
            sys.exit(f"engine not found: {engine}  (build it first)")
        if not model.is_file():
            sys.exit(f"model not found: {model}")
        cmd = [str(engine), "serve", "--port", "0", "--backend", self.args.backend,
               "--model", str(model), "--context", str(self.args.context), "--token-stdin"]
        if self.args.backend == "intel-vulkan":
            if not self.args.vulkan_device_name:
                sys.exit("--backend intel-vulkan needs --vulkan-device-name (see `lae-engine probe` / vulkaninfo)")
            cmd += ["--vulkan-device-name", self.args.vulkan_device_name]
            # The engine refuses intel-vulkan without an explicit offload. The
            # model has 32 blocks, so 99 offloads all of it; 32 GiB of shared
            # memory holds the 5.6 GiB model with room to spare.
            cmd += ["--gpu-layers", str(self.args.gpu_layers if self.args.gpu_layers is not None else 99)]
        elif self.args.gpu_layers is not None:
            cmd += ["--gpu-layers", str(self.args.gpu_layers)]
        if self.args.threads is not None:
            cmd += ["--threads", str(self.args.threads)]
        if self.args.threads_batch is not None:
            cmd += ["--threads-batch", str(self.args.threads_batch)]
        if self.args.speculate:
            cmd += ["--speculate", str(self.args.speculate)]
        if getattr(self.args, "snapshots", None):
            cmd += ["--snapshots", str(self.args.snapshots)]
        if getattr(self.args, "idle_unload_minutes", 0):
            cmd += ["--idle-unload", str(self.args.idle_unload_minutes * 60)]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        # stderr goes to a file so a chatty backend can never fill a pipe and
        # deadlock the engine. The engine keeps its own inherited handle.
        with open(self.log_path, "wb") as log:
            try:
                self.proc = subprocess.Popen(cmd, **{**self.popen_kwargs, "stdin": subprocess.PIPE,
                                                      "stdout": subprocess.PIPE, "stderr": log})
            except OSError as exc:
                # On Windows this is typically antivirus: Defender quarantined
                # the freshly copied exe (it vanished between the check above
                # and here) or holds it open while it scans it.
                sys.exit(f"could not start the engine {engine}: {exc}\n"
                         "If Windows Security quarantined or is scanning lae-engine.exe, restore or allow it "
                         "(Windows Security > Protection history), wait a minute and retry.")
        try:
            self._await_ready(t0)
        except BaseException:
            # Loading the model takes a minute or more, and this runs before
            # `with Engine(...)` has entered its block, so __exit__ would never
            # stop the engine: a Ctrl+C here (or any failure) must, or the
            # engine stays behind holding ~6 GB with nobody to talk to it.
            self.stop()
            raise
        return self

    def _await_ready(self, t0: float) -> None:
        try:
            self.proc.stdin.write(self.token.encode())
            self.proc.stdin.close()  # the engine reads the token to EOF
        except OSError:
            pass  # it exited already; the missing ready line below reports how
        line: list[bytes] = []
        reader = threading.Thread(target=lambda: line.append(self.proc.stdout.readline()), daemon=True)
        reader.start()
        # Short joins, not one long one: on Windows a long blocking wait is not
        # interrupted by Ctrl+C, so the operator could not abort a slow load.
        deadline = time.monotonic() + self.args.ready_timeout
        while reader.is_alive() and time.monotonic() < deadline:
            reader.join(min(0.25, max(0.0, deadline - time.monotonic())))
        if not line or not line[0]:
            code = self.proc.poll()
            self.stop()
            sys.exit(f"engine did not report ready within {self.args.ready_timeout}s "
                     f"(exit code {code}); see {self.log_path}")
        try:
            ready = json.loads(line[0])
            self.port = int(ready["port"])
        except (ValueError, KeyError, TypeError):
            self.stop()
            sys.exit(f"unexpected engine ready line: {line[0][:200]!r}")
        self.ready_seconds = round(time.monotonic() - t0, 1)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def get(self, path: str, auth: bool = True) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, method="GET")
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read(65536) or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            except KeyboardInterrupt:
                # An impatient second Ctrl+C must not leave the engine behind.
                self.proc.kill()
                raise

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def _host() -> dict:
    return {"platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor() or None, "logical_cpus": os.cpu_count(),
            "python": platform.python_version()}


def _write(receipt: dict, out: str | None) -> None:
    text = json.dumps(receipt, indent=2, sort_keys=True)
    print(text)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"\nreceipt written: {Path(out).resolve()}", file=sys.stderr)


def cmd_smoke(args: argparse.Namespace) -> int:
    receipt = {"schema": "local_bmo.local-smoke.v1", "started_at_utc": _utc(), "host": _host(),
               "backend": args.backend, "context": args.context}
    with Engine(args) as eng:
        receipt["model_load_seconds"] = eng.ready_seconds
        checks = {}
        for path, auth in (("/healthz", False), ("/readyz", True), ("/version", True), ("/build-info", True)):
            status, body = eng.get(path, auth)
            checks[path] = {"status": status, "ok": status == 200}
            if path == "/build-info" and status == 200:
                receipt["build_info"] = {k: body.get(k) for k in ("backend", "engine_version", "llama_cpp_revision", "model")}
        receipt["endpoint_checks"] = checks
        status, body = eng.get("/metrics")
        if status == 200:
            receipt["runtime"] = body.get("runtime")  # context, batch sizes, threads
        payload = {"model": "qwen35-9b-q4-k-m", "stream": False, "max_tokens": 32, "mode": "normal",
                   "messages": [{"role": "user", "content": "Reply with the single word: ready"}]}
        t1 = time.monotonic()
        try:
            content, prompt_tokens = ev._post(f"{eng.base}/v1/chat/completions", eng.token, payload, 600, include_usage=True)
            receipt["chat"] = {"ok": True, "seconds": round(time.monotonic() - t1, 1), "prompt_tokens": prompt_tokens,
                               "reply_chars": len(content)}
        except Exception as exc:  # report, don't hide
            receipt["chat"] = {"ok": False, "seconds": round(time.monotonic() - t1, 1), "error": f"{type(exc).__name__}: {exc}"}
    receipt["passed"] = all(c["ok"] for c in checks.values()) and receipt["chat"]["ok"]
    _write(receipt, args.out)
    return 0 if receipt["passed"] else 1


def cmd_eval(args: argparse.Namespace) -> int:
    if not 0 < args.timeout <= LOCAL_TIMEOUT_CEILING:
        sys.exit(f"--timeout must be between 0 and {LOCAL_TIMEOUT_CEILING} seconds")
    fixture = ev.load_fixture(Path(args.fixture))
    receipt = {"schema": "local_bmo.local-eval.v1", "started_at_utc": _utc(), "host": _host(),
               "backend": args.backend, "context": args.context, "fixture_sha256": None,
               "prompt_response_logging": False}
    import hashlib
    receipt["fixture_sha256"] = hashlib.sha256(Path(args.fixture).read_bytes()).hexdigest()
    with Engine(args) as eng:
        receipt["model_load_seconds"] = eng.ready_seconds
        endpoint = f"{eng.base}/v1/chat/completions"
        t0 = time.monotonic()
        if args.cases:
            wanted = args.cases.split(",")
            cases = [c for c in fixture["cases"] if c["id"] in wanted]
            missing = sorted(set(wanted) - {c["id"] for c in cases})
            if missing:
                sys.exit(f"unknown case ids: {missing}")
            results = []
            for case in cases:
                payload = ev._case_payload(fixture, case, ev.TRANSPORT_PRODUCT_ENGINE)
                t1 = time.monotonic()
                try:
                    out = ev._post(endpoint, eng.token, payload, args.timeout)
                    passed, reason = ev.evaluate_case(case, out, fixture["tools"])
                except Exception as exc:
                    out, passed, reason = None, False, f"error:{type(exc).__name__}"
                results.append({"id": case["id"], "category": case["category"], "passed": passed,
                                "reason": reason, "seconds": round(time.monotonic() - t1, 1)})
                print(f"{case['id']}: {'PASS' if passed else 'FAIL'} ({reason}, {results[-1]['seconds']}s)",
                      file=sys.stderr)
                if args.show_output:
                    print(f"  raw output: {out!r}", file=sys.stderr)
            receipt.update({"mode": "cases", "cases": results,
                            "passed": sum(r["passed"] for r in results), "case_count": len(results)})
        else:
            result = ev.run_local(fixture, endpoint, eng.token, timeout=args.timeout,
                                  max_cases=len(fixture["cases"]), engine_pid=eng.proc.pid,
                                  timeout_ceiling=LOCAL_TIMEOUT_CEILING)
            receipt.update({"mode": "full", **ev.aggregate_result(result)})
        receipt["eval_seconds"] = round(time.monotonic() - t0, 1)
    _write(receipt, args.out)
    return 0


FILLER = "The quick brown fox jumps over the lazy dog. "


def _chat(eng: "Engine", session: str, messages: list[dict], max_tokens: int,
          timeout: float) -> tuple[str, int, int, float]:
    """One non-streaming completion. Returns reply, prompt/completion tokens, seconds."""
    body = json.dumps({"model": "qwen35-9b-q4-k-m", "stream": False, "max_tokens": max_tokens,
                       "mode": "normal", "session_id": session, "messages": messages}).encode()
    req = urllib.request.Request(f"{eng.base}/v1/chat/completions", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {eng.token}")
    req.add_header("Content-Type", "application/json")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    seconds = time.monotonic() - t0
    usage = payload.get("usage", {})
    return (payload["choices"][0]["message"]["content"], int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)), seconds)


def _new_session(eng: "Engine") -> str:
    req = urllib.request.Request(f"{eng.base}/v1/sessions", data=b"{}", method="POST")
    req.add_header("Authorization", f"Bearer {eng.token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["id"]


def cmd_bench(args: argparse.Namespace) -> int:
    receipt = {"schema": "local_bmo.local-bench.v1", "started_at_utc": _utc(), "host": _host(),
               "backend": args.backend, "context": args.context, "threads": args.threads,
               "prompt_response_logging": False}
    with Engine(args) as eng:
        receipt["model_load_seconds"] = eng.ready_seconds
        _, build = eng.get("/build-info")
        receipt["build_info"] = {k: build.get(k) for k in ("backend", "engine_version", "llama_cpp_revision")}

        # Writing speed: a short prompt, so the time is almost all generation.
        short = [{"role": "user", "content": "Count from one to twenty in words."}]
        session = _new_session(eng)
        _, p_tok, c_tok, secs = _chat(eng, session, short, args.generate_tokens, args.timeout)
        receipt["decode"] = {"prompt_tokens": p_tok, "generated_tokens": c_tok,
                             "seconds": round(secs, 1),
                             "tokens_per_second": round(c_tok / secs, 2) if secs > 0 else None}

        # Reading speed: a long prompt, one token out, so the time is almost
        # all prompt processing.
        filler = FILLER * max(1, args.prefill_tokens // 10)
        long_turn = [{"role": "user", "content": filler + "Reply with the single word: ok"}]
        cold_session = _new_session(eng)
        # Keep the model's REAL reply: the follow-ups below must contain exactly
        # what it generated, or the retained prompt is not a prefix of them and
        # reuse silently reads as "not working".
        reply_long, p_long, c_long, secs_long = _chat(eng, cold_session, long_turn, 1, args.timeout)
        receipt["prefill"] = {"prompt_tokens": p_long, "seconds": round(secs_long, 1),
                              "tokens_per_second": round(p_long / secs_long, 2) if secs_long > 0 else None}

        # Reuse, measured in the two situations a conversation produces.
        #
        # 1. A tool-call continuation extends the prompt the context already holds,
        #    so the live context serves it.
        # 2. A NEW user turn does not: the chat template re-renders the earlier
        #    assistant message without its <think> scaffold, so the live context
        #    no longer agrees with the prompt. The engine instead restores a
        #    snapshot it took at the end of the previous user message.
        # The new-turn request must come straight after the continuation on the
        # SAME session; any request on another session in between would take the
        # engine's one live context and the snapshot's key would no longer match.
        tool_continuation = long_turn + [
            {"role": "assistant", "content": reply_long},
            {"role": "tool", "name": "time.now", "tool_call_id": "call-1",
             "content": '{"utc":"2026-10-02T00:00:00Z"}'}]
        _, _, _, secs_warm = _chat(eng, cold_session, tool_continuation, 1, args.timeout)
        warm = eng.get("/metrics")[1].get("runtime", {})

        new_turn = long_turn + [{"role": "assistant", "content": reply_long},
                                {"role": "user", "content": "Reply with the single word: again"}]
        _, p_turn, _, secs_turn = _chat(eng, cold_session, new_turn, 1, args.timeout)
        turn_metrics = eng.get("/metrics")[1].get("runtime", {})

        # The same two prompts with nothing to reuse, for the comparison.
        _, _, _, secs_fresh = _chat(eng, _new_session(eng), tool_continuation, 1, args.timeout)
        _, _, _, secs_turn_cold = _chat(eng, _new_session(eng), new_turn, 1, args.timeout)

        receipt["prefix_reuse"] = {
            "tool_continuation_seconds": round(secs_warm, 1),
            "same_prompt_without_reuse_seconds": round(secs_fresh, 1),
            "speedup": round(secs_fresh / secs_warm, 2) if secs_warm > 0 else None,
            "reused_prefix_tokens": warm.get("last_reused_prefix_tokens"),
        }
        receipt["new_user_turn"] = {
            "prompt_tokens": p_turn,
            "seconds": round(secs_turn, 1),
            "same_prompt_without_snapshot_seconds": round(secs_turn_cold, 1),
            "speedup": round(secs_turn_cold / secs_turn, 2) if secs_turn > 0 else None,
            "reused_prefix_tokens": turn_metrics.get("last_reused_prefix_tokens"),
            "restored_snapshot_tokens": turn_metrics.get("last_restored_snapshot_tokens"),
            "note": "restored_snapshot_tokens of 0 means the engine re-read the whole history for a new user turn",
        }
        receipt["runtime"] = eng.get("/metrics")[1].get("runtime", {})
    _write(receipt, args.out)
    return 0


def cmd_longctx(args: argparse.Namespace) -> int:
    receipt = {"started_at_utc": _utc(), "host": _host(), "backend": args.backend, "context": args.context,
               "threads": args.threads, "engine_launched_by_tool": True}
    with Engine(args) as eng:
        receipt["model_load_seconds"] = eng.ready_seconds
        # The evaluator saves the receipt after every cell, so a run cut short
        # on a slow laptop keeps what it measured and `--resume` continues it.
        lce.run_eval(lce.EngineClient(eng.base, eng.token, args.timeout), args, receipt)
    _write(receipt, args.out)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    with Engine(args) as eng:
        info = {"endpoint": f"{eng.base}/v1/chat/completions", "model_load_seconds": eng.ready_seconds}
        if args.print_token:
            info["token"] = eng.token
        else:
            info["note"] = ("the bearer token is not printed; rerun with --print-token if another "
                            "program on this machine needs it")
        print(json.dumps(info, indent=2))
        print("engine running; Ctrl+C to stop", file=sys.stderr)
        try:
            while eng.proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    # Imported here: bmo_preflight builds on this module.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bmo_preflight
    return bmo_preflight.run(args)


# --- delegate key (host/delegate/state.mjs is the other half) -----------------

DELEGATE_KEY_FILE = "delegate-key"
DELEGATE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def delegate_state_dir(env=None, system: str | None = None, home: str | None = None) -> str:
    """The per-user BMO state folder, exactly as the host resolves it.

    BMO_STATE_DIR overrides and must be absolute; Windows uses
    %LOCALAPPDATA%\\BMO, macOS ~/Library/Application Support/BMO, other
    systems ${XDG_STATE_HOME:-~/.local/state}/bmo (a relative XDG value is
    ignored, as the XDG spec says).
    """
    env = os.environ if env is None else env
    system = sys.platform if system is None else system
    override = env.get("BMO_STATE_DIR")
    if override:
        if not (override.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", override)):
            raise ValueError("BMO_STATE_DIR must be an absolute path")
        return override
    if system == "win32":
        local = env.get("LOCALAPPDATA", "")
        if not re.match(r"^[A-Za-z]:[\\/]", local):
            raise ValueError("LOCALAPPDATA is not set to a local drive path")
        return local.rstrip("\\/") + "\\BMO"
    home = home or str(Path.home())
    if system == "darwin":
        return os.path.join(home, "Library", "Application Support", "BMO")
    xdg = env.get("XDG_STATE_HOME", "")
    return os.path.join(xdg if xdg.startswith("/") else os.path.join(home, ".local", "state"), "bmo")


def _private(path: str, *, directory: bool) -> None:
    """Refuse a symlink or another user's file; close one that is too open.

    Windows ACLs are not visible here; there the per-user %LOCALAPPDATA%
    location is what keeps the key private.
    """
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise PermissionError(f"{path} is not a plain {'folder' if directory else 'file'}")
    if os.name == "nt":
        return
    if info.st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700 if directory else 0o600)


def _write_private_temp(state_dir: str, text: str) -> str:
    temp = os.path.join(state_dir, f"{DELEGATE_KEY_FILE}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, text.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return temp


def read_delegate_key(state_dir: str) -> str | None:
    path = os.path.join(state_dir, DELEGATE_KEY_FILE)
    try:
        _private(path, directory=False)
        with open(path, encoding="ascii", errors="replace") as handle:
            key = handle.read().strip()
    except FileNotFoundError:
        return None
    if not DELEGATE_KEY_RE.match(key):
        raise ValueError(f"{path} is damaged; replace it with: python local/bmo_local.py delegate-key --rotate")
    return key


def delegate_key(state_dir: str, rotate: bool = False) -> tuple[str, bool]:
    """The stored key (created on first use), or a new one with rotate. Returns (key, new)."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    _private(state_dir, directory=True)
    path = os.path.join(state_dir, DELEGATE_KEY_FILE)
    if not rotate:
        existing = read_delegate_key(state_dir)
        if existing:
            return existing, False
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    temp = _write_private_temp(state_dir, key + "\n")
    try:
        if rotate:
            os.replace(temp, path)  # atomic: a reader sees the old key or the new one
        else:
            try:
                os.link(temp, path)  # never replaces a key another process just made
            except FileExistsError:
                pass
            except OSError:
                if not os.path.exists(path):
                    os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    stored = read_delegate_key(state_dir)
    return stored, stored == key


def cmd_delegate_key(args: argparse.Namespace) -> int:
    try:
        state_dir = delegate_state_dir()
        key, new = delegate_key(state_dir, rotate=args.rotate)
    except (OSError, ValueError) as error:
        print(f"delegate-key: {error}", file=sys.stderr)
        return 1
    # Only the key goes to stdout, so `| Set-Clipboard` or `| pbcopy` copies
    # exactly it; the explanation goes to stderr.
    print(key)
    print(("A new key was made. " if new else "") + "Paste it once into the coding assistant's BMO key prompt "
          "(the bmo MCP server asks for it). It is stored in "
          f"{os.path.join(state_dir, DELEGATE_KEY_FILE)}, readable only by you.\n"
          + ("Anything still using the old key is now refused; paste this one instead.\n" if args.rotate else "")
          + "Jobs run only while BMO is started with delegation on (Start-BMO.ps1 -Mode app -EnableDelegation), "
          "and each one waits for your Approve on the BMO page.", file=sys.stderr)
    return 0


def console_safe_output() -> None:
    """A replacement character instead of a crash on an unencodable character.

    A Windows console redirected to a file or a pipe (`| Tee-Object`) encodes
    with the ANSI code page, which cannot hold most of what a model writes
    (`--show-output` prints it) or a non-ASCII path in an error message.
    """
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    key = sub.add_parser("delegate-key", help="print (or --rotate) the coding-assistant key")
    key.set_defaults(handler=cmd_delegate_key)
    key.add_argument("--rotate", action="store_true",
                     help="replace the key; whatever holds the old one is refused from then on")
    for name, handler in (("smoke", cmd_smoke), ("eval", cmd_eval), ("bench", cmd_bench),
                          ("longctx", cmd_longctx), ("serve", cmd_serve), ("preflight", cmd_preflight)):
        s = sub.add_parser(name)
        s.set_defaults(handler=handler)
        s.add_argument("--engine", required=True, help="path to lae-engine (lae-engine.exe on Windows)")
        s.add_argument("--model", required=True, help="path to Qwen3.5-9B-Q4_K_M.gguf")
        s.add_argument("--backend", default="cpu", choices=("cpu", "intel-vulkan"))
        s.add_argument("--vulkan-device-name")
        s.add_argument("--gpu-layers", type=int, help="intel-vulkan defaults to 99 (the whole model)")
        s.add_argument("--threads", type=int, help="CPU threads (default: every logical thread)")
        s.add_argument("--threads-batch", type=int, dest="threads_batch",
                       help="CPU threads for prompt processing only (default: same as --threads)")
        s.add_argument("--speculate", type=int, default=0,
                       help="draft-free n-gram speculation: tokens verified per pass, 0-8 (default 0 = off)")
        s.add_argument("--snapshots", type=int, default=None,
                       help="conversation snapshots the engine keeps, 1-8 (default 4): lets two conversations share the engine without re-reading history")
        s.add_argument("--idle-unload-minutes", type=int, default=0, dest="idle_unload_minutes",
                       help="free the 6 GB model after this many idle minutes and reload it on the next message (default 0 = never)")
        s.add_argument("--context", type=int, default=8192)
        s.add_argument("--ready-timeout", type=float, default=600)
        s.add_argument("--log", default=str(ROOT / "local" / "out" / f"engine-{name}.log"))
        if name in ("smoke", "eval", "bench", "longctx"):
            s.add_argument("--out", help="write the JSON receipt here")
        if name == "serve":
            s.add_argument("--print-token", action="store_true",
                           help="also print the engine's bearer token (off by default: consoles get shared)")
        if name == "preflight":
            s.add_argument("--verify-hash", action="store_true",
                           help="also check the model's SHA-256 (reads all 5.6 GB, about a minute)")
            s.add_argument("--node", help="node to check and start the web UI host with (default: node on PATH)")
            s.add_argument("--chat-budget", type=float, default=300,
                           help="seconds the one tiny chat reply may take (default 300)")
        if name == "longctx":
            # --context above already sets the engine's context; the evaluator
            # reads the real value back from /metrics.
            lce.add_eval_arguments(s, include_context=False)
        if name == "bench":
            s.add_argument("--prefill-tokens", type=int, default=2000,
                           help="approximate prompt size for the reading-speed measurement")
            s.add_argument("--generate-tokens", type=int, default=64)
            s.add_argument("--timeout", type=float, default=1800)
        if name == "eval":
            s.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
            s.add_argument("--cases", help="comma-separated case ids to score only those")
            s.add_argument("--show-output", action="store_true", help="print raw model output (console only)")
            # Every case sends a ~5,800-token prompt and the engine re-reads all of
            # it each time. A laptop CPU can need several minutes, and a client
            # that gives up leaves the engine busy (HTTP 409 `busy` until it
            # finishes), so the wait is generous.
            s.add_argument("--timeout", type=float, default=1800)
    return p


def main(argv: list[str] | None = None) -> int:
    console_safe_output()
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
