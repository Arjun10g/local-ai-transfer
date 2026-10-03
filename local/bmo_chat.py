#!/usr/bin/env python3
"""Talk to the local assistant engine from a terminal.

Starts `lae-engine serve` (token on stdin, exactly as `bmo_local.py` does),
opens one engine session and streams each reply as it is written. Python
standard library only, so it runs on a laptop that has no Node.js.

The engine keeps no conversation of its own: each request carries the whole
history, and the engine reuses whatever prompt prefix its context still holds.
So the history lives here, in memory, and is resent in full every turn.

Nothing the model writes is saved anywhere. Timings are printed, not stored.

In the chat
  /reset           forget the conversation and start over
  /system <text>   set the system prompt (`/system` alone clears it)
  /stats           timings and token counts for the last reply
  /trim            drop the oldest exchange (and resend a message that did not fit)
  /quit            stop the engine and exit (also Ctrl+D, or Ctrl+Z then Enter on Windows)
  Ctrl+C           while a reply is being written: stop it and return to the prompt
"""

from __future__ import annotations

import argparse
import codecs
import http.client
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bmo_local  # noqa: E402

MODEL_ID = "qwen35-9b-q4-k-m"
# Request bounds the engine enforces (`native/server/chat_request.cpp`).
# Checking them here lets an over-long conversation take the same trim path
# as a context overflow instead of surfacing as an opaque 400.
MAX_MESSAGES = 64
MAX_HISTORY_BYTES = 49152
MAX_MESSAGE_BYTES = 32768
# The engine's per-reply ceiling is 2,048 tokens; engines built before that
# change refuse anything above 256 (see Chat.send).
MAX_REPLY_TOKENS = 2048
DEFAULT_REPLY_TOKENS = 1024
LEGACY_REPLY_TOKENS = 256
MAX_SSE_LINE = 256 * 1024


# --- engine process -------------------------------------------------------

def detached_popen_kwargs() -> dict:
    """Popen options that start the engine outside this terminal's Ctrl+C reach.

    Ctrl+C is delivered to every process attached to the terminal: SIGINT to
    the foreground process group on macOS/Linux, CTRL_C_EVENT to the whole
    console on Windows. The engine treats either as "stop". In a chat, Ctrl+C
    means "stop this reply", so the engine must not see it; this process
    cancels the request and stops the engine itself at exit.
    """
    if os.name == "nt":
        # A new process group starts with Ctrl+C ignored, and the engine's own
        # SIGINT handler does not undo that. Closing the window still ends it.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class ChatEngine(bmo_local.Engine):
    """`bmo_local.Engine`, started so that Ctrl+C in this terminal does not stop it."""

    def __init__(self, args: argparse.Namespace):
        super().__init__(args, popen_kwargs=detached_popen_kwargs())


# --- engine HTTP API -----------------------------------------------------

class EngineAPI:
    """The few engine endpoints a chat needs, over a fresh connection each time
    (the engine answers `Connection: close`)."""

    def __init__(self, port: int, token: str, host: str = "127.0.0.1", timeout: float = 1800.0):
        self.host, self.port, self.token, self.timeout = host, port, token, timeout

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, payload: dict | None = None,
             timeout: float = 30.0) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            headers = self.headers() if body is not None else {"Authorization": f"Bearer {self.token}"}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(1 << 20)
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {}
            return resp.status, data if isinstance(data, dict) else {}
        finally:
            conn.close()

    def new_session(self) -> str:
        status, data = self.call("POST", "/v1/sessions", {})
        if status != 201 or not isinstance(data.get("id"), str):
            raise EngineRefused(status, _code(data))
        return data["id"]

    def metrics(self) -> dict:
        try:
            status, data = self.call("GET", "/metrics", timeout=10)
        except OSError:
            return {}
        return data if status == 200 else {}

    def cancel(self, request_id: str) -> bool:
        try:
            status, data = self.call("POST", f"/v1/cancel/{request_id}", {}, timeout=5)
        except OSError:
            return False
        return status == 200 and data.get("cancelled") is True

    def wait_idle(self, timeout: float = 30.0, poll: float = 0.1) -> bool:
        """Wait until no generation is running, so the next turn is not refused as busy.

        A cancelled request stops at the engine's next cancellation check, not
        instantly; sending the next turn before then earns a 409.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.metrics()
            if data.get("lifecycle") == "READY" and not data.get("active_generations"):
                return True
            time.sleep(poll)
        return False


class EngineRefused(Exception):
    def __init__(self, status: int, code: str | None):
        super().__init__(f"engine refused: HTTP {status} {code or ''}".strip())
        self.status, self.code = status, code


def _code(data) -> str | None:
    error = data.get("error") if isinstance(data, dict) else None
    return error.get("code") if isinstance(error, dict) else None


# --- one streamed completion ---------------------------------------------

@dataclass
class TurnStats:
    seconds: float = 0.0
    first_token_seconds: float | None = None
    generated_tokens: int = 0
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    reused_prefix_tokens: int | None = None

    @property
    def decode_tokens_per_second(self) -> float | None:
        # Measured from the first token, so prompt reading is not counted as writing.
        if self.first_token_seconds is None or self.generated_tokens < 2:
            return None
        span = self.seconds - self.first_token_seconds
        return (self.generated_tokens - 1) / span if span > 0 else None

    @property
    def prefill_tokens_per_second(self) -> float | None:
        if self.prompt_tokens is None or not self.first_token_seconds:
            return None
        fresh = self.prompt_tokens - (self.reused_prefix_tokens or 0)
        return fresh / self.first_token_seconds if fresh > 0 else None


@dataclass
class TurnResult:
    # ok | cancelled | overflow | busy | not_ready | session_lost | unauthorized | engine_gone | error
    status: str
    text: str = ""
    request_id: str | None = None
    detail: str = ""
    stats: TurnStats = field(default_factory=TurnStats)
    dropped_exchanges: int = 0
    http_status: int | None = None  # set when refused before streaming began


def _classify(status: int | None, code: str | None) -> str:
    # The engine reports "context limit exceeded" as code invalid_request: an
    # HTTP 400 on the non-streaming path, an SSE error frame on the streaming
    # one. This client builds every other part of the request itself, so an
    # invalid_request here means the conversation no longer fits.
    if code in ("invalid_request", "request_too_large") or status == 413:
        return "overflow"
    if code == "busy" or status == 409:
        return "busy"
    if code == "not_ready" or status == 503:
        return "not_ready"
    if code == "not_found" or status == 404:
        return "session_lost"
    if status == 401:
        return "unauthorized"
    return "error"


def _reader(api: EngineAPI, body: bytes, out: queue.Queue, holder: dict) -> None:
    """Runs on a worker thread so the main thread stays free to see Ctrl+C.

    On Windows a thread blocked in a socket read does not see Ctrl+C until the
    read returns, which during a long prompt can be minutes.
    """
    try:
        conn = http.client.HTTPConnection(api.host, api.port, timeout=api.timeout)
        holder["conn"] = conn
        conn.request("POST", "/v1/chat/completions", body=body, headers=api.headers())
        resp = conn.getresponse()
        request_id = resp.getheader("X-Request-Id")
        if resp.status != 200:
            raw = resp.read(65536)
            try:
                code = _code(json.loads(raw or b"{}"))
            except ValueError:
                code = None
            out.put(("http_error", resp.status, code, request_id))
            return
        out.put(("open", request_id))
        while True:
            line = resp.readline(MAX_SSE_LINE)
            if not line:
                out.put(("eof",))
                return
            line = line.rstrip(b"\r\n")
            if not line.startswith(b"data: "):
                continue
            data = line[6:]
            if data == b"[DONE]":
                out.put(("done",))
                return
            out.put(("event", data))
    except BaseException as exc:  # reported to the main thread, never raised here
        out.put(("exception", exc))


def _abort(api: EngineAPI, request_id: str | None, out: queue.Queue, holder: dict) -> None:
    # The response headers (with the request id) may still be queued.
    while request_id is None:
        try:
            item = out.get_nowait()
        except queue.Empty:
            break
        if item[0] in ("open", "http_error"):
            request_id = item[-1]
    # Cancel by id first: the engine checks the flag between tokens and inside
    # the prompt's forward pass, so this stops even a long prompt read. Closing
    # the connection alone is noticed only when the next token fails to send.
    if request_id:
        api.cancel(request_id)
    conn = holder.get("conn")
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)  # wakes the reader on every platform
        except OSError:
            pass
    if conn is not None:
        conn.close()
    try:
        api.wait_idle()
    except KeyboardInterrupt:
        pass  # a second Ctrl+C: stop waiting; the next turn reports busy if it must


def stream_completion(api: EngineAPI, payload: dict, on_text: Callable[[str], None],
                      poll: float = 0.1) -> TurnResult:
    """POST one streaming completion and hand each piece of text to `on_text`.

    A KeyboardInterrupt raised while waiting (Ctrl+C) or by `on_text` cancels
    the request on the engine and returns status "cancelled".
    """
    out: queue.Queue = queue.Queue()
    holder: dict = {}
    stats = TurnStats()
    parts: list[str] = []
    request_id: str | None = None
    # Tokens are pieces of UTF-8 and the engine escapes them byte for byte, so
    # one character can arrive split across two frames. Decoding the stream as
    # bytes, incrementally, keeps such characters whole.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    t0 = time.monotonic()
    body = json.dumps(payload).encode()
    threading.Thread(target=_reader, args=(api, body, out, holder), daemon=True).start()

    def finish(status: str, detail: str = "") -> TurnResult:
        tail = decoder.decode(b"", final=True)
        if tail:
            parts.append(tail)
            on_text(tail)
        stats.seconds = time.monotonic() - t0
        return TurnResult(status, "".join(parts), request_id, detail, stats)

    try:
        while True:
            try:
                # A short wait keeps Ctrl+C responsive on Windows, where a
                # long blocking wait is not interrupted by it.
                item = out.get(timeout=poll)
            except queue.Empty:
                continue
            kind = item[0]
            if kind == "open":
                request_id = item[1]
            elif kind == "http_error":
                _, status, code, request_id = item
                result = finish(_classify(status, code), f"HTTP {status} {code or ''}".strip())
                result.http_status = status
                return result
            elif kind == "event":
                try:
                    frame = json.loads(item[1].decode("utf-8", "surrogateescape"))
                except ValueError:
                    return finish("error", "the engine sent a malformed stream frame")
                if isinstance(frame.get("error"), dict):
                    code = frame["error"].get("code")
                    return finish(_classify(None, code), f"engine error {code}")
                choice = (frame.get("choices") or [{}])[0]
                delta = (choice.get("delta") or {}).get("content")
                if isinstance(delta, str):
                    stats.generated_tokens += 1  # the engine sends one frame per token
                    if stats.first_token_seconds is None:
                        stats.first_token_seconds = time.monotonic() - t0
                    text = decoder.decode(delta.encode("utf-8", "surrogateescape"))
                    if text:
                        parts.append(text)
                        on_text(text)
                if choice.get("finish_reason"):
                    stats.finish_reason = choice["finish_reason"]
                usage = frame.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                    stats.prompt_tokens = usage["prompt_tokens"]
            elif kind == "done":
                return finish("cancelled" if stats.finish_reason == "cancelled" else "ok")
            elif kind == "eof":
                return finish("error", "the engine closed the stream before finishing")
            elif kind == "exception":
                exc = item[1]
                if isinstance(exc, ConnectionRefusedError):
                    return finish("engine_gone", "the engine is not running")
                return finish("error", f"{type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        _abort(api, request_id, out, holder)
        stats.seconds = time.monotonic() - t0
        return TurnResult("cancelled", "".join(parts), request_id, "stopped by Ctrl+C", stats)


# --- conversation --------------------------------------------------------

class Conversation:
    """Client-side history: an optional system prompt, then whole exchanges."""

    def __init__(self, system: str | None = None):
        self.system = system or None
        self.history: list[dict] = []  # always user, assistant, user, assistant, ...

    def messages(self, user_text: str) -> list[dict]:
        head = [{"role": "system", "content": self.system}] if self.system else []
        return head + self.history + [{"role": "user", "content": user_text}]

    def commit(self, user_text: str, reply: str) -> None:
        self.history += [{"role": "user", "content": user_text},
                         {"role": "assistant", "content": reply}]

    def trim_oldest(self) -> bool:
        """Drop the oldest user/assistant exchange. False when there is none.

        Whole exchanges only: an assistant reply without the question it
        answers, or a question without its reply, reads to the model as a
        different conversation.
        """
        if len(self.history) < 2:
            return False
        del self.history[:2]
        return True

    def reset(self) -> None:
        self.history.clear()

    @property
    def exchanges(self) -> int:
        return len(self.history) // 2


def fits_request_bounds(messages: list[dict]) -> bool:
    sizes = [len(m["content"].encode("utf-8", "surrogatepass")) for m in messages]
    return (len(messages) <= MAX_MESSAGES and sum(sizes) <= MAX_HISTORY_BYTES
            and max(sizes, default=0) <= MAX_MESSAGE_BYTES)


class Chat:
    def __init__(self, api: EngineAPI, session_id: str, conversation: Conversation,
                 max_tokens: int = DEFAULT_REPLY_TOKENS, auto_trim: bool = True):
        self.api, self.session_id, self.conversation = api, session_id, conversation
        self.max_tokens, self.auto_trim = max_tokens, auto_trim
        self.last: TurnResult | None = None
        self.pending: str | None = None  # a message that did not fit, kept for /trim

    def payload(self, messages: list[dict]) -> dict:
        return {"model": MODEL_ID, "session_id": self.session_id, "messages": messages,
                "stream": True, "max_tokens": self.max_tokens, "mode": "normal"}

    def send(self, user_text: str, on_text: Callable[[str], None]) -> TurnResult:
        """One user turn: stream the reply, trimming or recovering as needed.

        Only a completed reply joins the history. A cancelled or refused turn
        leaves the conversation exactly as it was.
        """
        dropped, renewed = 0, False
        while True:
            messages = self.conversation.messages(user_text)
            if fits_request_bounds(messages):
                result = stream_completion(self.api, self.payload(messages), on_text)
            else:
                result = TurnResult("overflow", detail="over the engine's request size limits")
            if (result.status == "overflow" and result.http_status == 400
                    and self.max_tokens > LEGACY_REPLY_TOKENS):
                # On a stream the engine reports a context overflow as an SSE
                # error frame, after the 200. A 400 before streaming is its
                # request parser, and the likeliest cause is an engine built
                # before replies could exceed 256 tokens.
                print(f"(this engine allows at most {LEGACY_REPLY_TOKENS} tokens per reply; using that)",
                      file=sys.stderr)
                self.max_tokens = LEGACY_REPLY_TOKENS
                continue
            if result.status == "session_lost" and not renewed:
                # The engine keeps four sessions and evicts the oldest.
                self.session_id, renewed = self.api.new_session(), True
                continue
            if result.status == "overflow" and self.auto_trim and self.conversation.trim_oldest():
                dropped += 1
                continue
            break
        result.dropped_exchanges = dropped
        if result.status == "ok":
            self.conversation.commit(user_text, result.text)
            self.pending = None
            runtime = self.api.metrics().get("runtime") or {}
            if result.stats.prompt_tokens is None and isinstance(runtime.get("retained_prompt_tokens"), int):
                # Older engines end the stream without a usage block. The
                # context holds the prompt plus every token written, so the
                # prompt is the difference (exact without speculation, within
                # one with it).
                result.stats.prompt_tokens = max(0, runtime["retained_prompt_tokens"] - result.stats.generated_tokens)
            result.stats.reused_prefix_tokens = runtime.get("last_reused_prefix_tokens")
        elif result.status == "overflow" and self.conversation.exchanges:
            self.pending = user_text
        self.last = result
        return result

    def command(self, line: str, out) -> str | None:
        """Handle a /command. Returns "quit" to end the chat, or a message to resend."""
        name, _, rest = line.partition(" ")
        if name in ("/quit", "/exit"):
            return "quit"
        if name == "/reset":
            self.conversation.reset()
            self.pending = None
            print("(conversation cleared)", file=out)
        elif name == "/system":
            self.conversation.system = rest.strip() or None
            print("(system prompt set)" if self.conversation.system else "(system prompt cleared)", file=out)
        elif name == "/stats":
            print(describe_stats(self.last, self.api.metrics().get("runtime") or {}), file=out)
        elif name == "/trim":
            if self.conversation.trim_oldest():
                print(f"(dropped the oldest exchange; {self.conversation.exchanges} left)", file=out)
            else:
                print("(nothing to drop)", file=out)
            if self.pending:
                return "resend"
        elif name == "/help":
            print(__doc__.split("In the chat", 1)[1].rstrip(), file=out)
        else:
            print(f"(unknown command {name}; /help lists them)", file=out)
        return None


# --- what the operator sees ----------------------------------------------

def describe_stats(result: TurnResult | None, runtime: dict) -> str:
    if result is None:
        return "(no reply yet)"
    s = result.stats
    lines = [f"last reply: {result.status}, {s.seconds:.1f} s total, finish: {s.finish_reason or '-'}"]
    if s.first_token_seconds is not None:
        lines.append(f"  first token after {s.first_token_seconds:.1f} s (reading the prompt)")
    tps = s.decode_tokens_per_second
    lines.append(f"  {s.generated_tokens} tokens written" + (f", {tps:.1f} tokens/s" if tps else ""))
    if s.prompt_tokens is not None:
        reused = f", {s.reused_prefix_tokens} reused from the previous turn" if s.reused_prefix_tokens else ""
        pps = s.prefill_tokens_per_second
        lines.append(f"  prompt_tokens: {s.prompt_tokens}{reused}" + (f", read at {pps:.0f} tokens/s" if pps else ""))
    if runtime:
        spec = runtime.get("speculate_tokens") or 0
        threads = (f", threads {runtime['n_threads']}/{runtime.get('n_threads_batch')} (write/read)"
                   if runtime.get("n_threads") else "")
        lines.append(f"  engine: context {runtime.get('context_tokens')}{threads}, "
                     f"speculation {'off' if not spec else spec}")
    return "\n".join(lines)


def describe_failure(result: TurnResult, chat: "Chat") -> str | None:
    if result.status == "ok":
        if result.stats.finish_reason == "length":
            return (f"(reply stopped at the {chat.max_tokens}-token limit per reply; "
                    "type 'continue' for more)")
        return None
    messages = {
        "cancelled": "(stopped)",
        "overflow": ("(the conversation is too long for the model's context. Type /trim to drop the "
                     "oldest exchange and resend, or /reset to start over.)"),
        "busy": ("(the engine is still finishing an earlier request. Wait a few seconds and send "
                 "again; if this keeps happening, /quit and start again.)"),
        "not_ready": "(the engine is not ready: it is still loading or is shutting down. Try again shortly.)",
        "session_lost": "(the engine lost this chat's session; send again.)",
        "unauthorized": "(the engine refused this client's token. /quit and start again.)",
        "engine_gone": "(the engine has stopped. Its log is in local/out/. /quit and start again.)",
    }
    if result.status == "overflow" and not chat.conversation.exchanges:
        # Nothing left to drop, so /trim cannot help; only a shorter message can.
        earlier = "even with every earlier exchange dropped, " if result.dropped_exchanges else ""
        return f"({earlier}this message is too long for the model's context; shorten it.)"
    return messages.get(result.status, f"(error: {result.detail or result.status})")


def repl(chat: Chat, read_line: Callable[[str], str] = input, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr

    def show(text: str) -> None:
        out.write(text)
        out.flush()

    while True:
        try:
            line = read_line("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            return 0
        if not line:
            continue
        # A console can hand over unpaired surrogates, which are not valid
        # UTF-8 and would make the engine refuse the whole request.
        line = line.encode("utf-8", "replace").decode("utf-8")
        if line.startswith("/"):
            action = chat.command(line, err)
            if action == "quit":
                return 0
            if action != "resend":
                continue
            line = chat.pending
        show("bmo> ")
        result = chat.send(line, show)
        print(file=out)
        if result.dropped_exchanges and result.status == "ok":
            print(f"(dropped {result.dropped_exchanges} oldest exchange(s) to fit the context)", file=err)
        note = describe_failure(result, chat)
        if note:
            print(note, file=err)


def exit_on_termination() -> None:
    """Turn SIGTERM / SIGHUP into an orderly exit.

    By default they end Python without running `finally`, which would leave
    the engine (deliberately outside this terminal's process group) running
    with the model loaded. SystemExit, unlike KeyboardInterrupt, is not taken
    for "stop this reply", so the chat ends instead of returning to the prompt.
    """
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)


# A Windows console or a redirected stream may not be able to encode
# everything the model writes; a replacement character beats a crash.
_console_safe_output = bmo_local.console_safe_output


def add_engine_args(p: argparse.ArgumentParser, log_name: str) -> None:
    """The engine options of `bmo_local.py`, so every launcher takes the same flags."""
    p.add_argument("--engine", required=True, help="path to lae-engine (lae-engine.exe on Windows)")
    p.add_argument("--model", required=True, help="path to Qwen3.5-9B-Q4_K_M.gguf")
    p.add_argument("--backend", default="cpu", choices=("cpu", "intel-vulkan"))
    p.add_argument("--vulkan-device-name")
    p.add_argument("--gpu-layers", type=int, help="intel-vulkan defaults to 99 (the whole model)")
    p.add_argument("--threads", type=int, help="CPU threads (default: every logical thread)")
    p.add_argument("--threads-batch", type=int, dest="threads_batch",
                   help="CPU threads for prompt processing only (default: same as --threads)")
    p.add_argument("--speculate", type=int, default=0,
                   help="draft-free n-gram speculation: tokens verified per pass, 0-8 (default 0 = off)")
    p.add_argument("--snapshots", type=int, default=None,
                   help="conversation snapshots the engine keeps, 1-8 (default 4): lets two conversations share the engine without re-reading history")
    p.add_argument("--idle-unload-minutes", type=int, default=0, dest="idle_unload_minutes",
                   help="free the 6 GB model after this many idle minutes and reload it on the next message (default 0 = never)")
    p.add_argument("--context", type=int, default=8192)
    p.add_argument("--ready-timeout", type=float, default=600)
    p.add_argument("--log", default=str(HERE / "out" / log_name))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_engine_args(p, "engine-chat.log")
    p.add_argument("--system", help="system prompt for the conversation")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_REPLY_TOKENS,
                   choices=range(1, MAX_REPLY_TOKENS + 1), metavar="1-2048",
                   help=f"longest reply, in tokens (default {DEFAULT_REPLY_TOKENS}; a longer limit "
                        "leaves less of the context for the conversation)")
    p.add_argument("--no-auto-trim", dest="auto_trim", action="store_false",
                   help="when the conversation outgrows the context, ask instead of "
                        "dropping the oldest exchanges")
    # A long prompt on a laptop CPU can take minutes before the first token.
    p.add_argument("--timeout", type=float, default=1800, help="seconds to wait for one reply")
    return p


def main(argv: list[str] | None = None) -> int:
    _console_safe_output()
    args = build_parser().parse_args(argv)
    exit_on_termination()
    eng = ChatEngine(args)
    print("starting the engine and loading the model (this can take a minute)...", file=sys.stderr)
    try:
        eng.start()
    except KeyboardInterrupt:
        return 130  # Ctrl+C during the model load; start() has stopped the engine
    try:
        api = EngineAPI(eng.port, eng.token, timeout=args.timeout)
        chat = Chat(api, api.new_session(), Conversation(args.system), args.max_tokens, args.auto_trim)
        print(f"ready (model loaded in {eng.ready_seconds} s). Type a message; /help for commands, "
              "Ctrl+C stops a reply, /quit exits.", file=sys.stderr)
        return repl(chat)
    finally:
        eng.stop()


if __name__ == "__main__":
    raise SystemExit(main())
