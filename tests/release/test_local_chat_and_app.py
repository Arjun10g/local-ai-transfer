"""The talk-to-it launchers: `local/bmo_chat.py`, `local/bmo_app.py`, `Start-BMO.ps1`.

Everything here runs offline against a stdlib stand-in for `lae-engine` that
speaks the same HTTP contract (`native/server/http_server.cpp`): sessions,
streamed and plain completions, cancel by request id, /metrics, and the
engine's refusals -- the context overflow (`invalid_request`, as an SSE error
frame on a stream and as HTTP 400 otherwise), 409 busy and 503 not_ready.

What is pinned: the full history goes out every turn; text is shown as it
arrives; an overflow trims whole exchanges or asks, never crashes; Ctrl+C
mid-reply cancels on the engine and the next turn works; the token never
reaches a command line or a file; nothing the model writes reaches disk.
"""

import _thread
import builtins
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_app  # noqa: E402
import bmo_chat  # noqa: E402
import bmo_local  # noqa: E402

TOKEN = "T" * 43
SECRET = "MODEL-OUTPUT-NEVER-ON-DISK-91c2"
LAUNCHERS = [ROOT / "local" / "bmo_chat.py", ROOT / "local" / "bmo_app.py",
             ROOT / "local" / "windows" / "Start-BMO.ps1"]


class FakeEngine:
    """A stand-in `lae-engine serve` on a free loopback port.

    `plan` holds one behaviour per chat request, consumed in order; when it is
    empty the request is answered with `default_reply`. `context_chars` plays
    the context limit: a request whose message contents add up to more is
    refused the way the engine refuses a prompt that does not fit.
    """

    def __init__(self):
        self.requests: list[dict] = []  # chat request bodies, in order
        self.cancels: list[str] = []
        self.plan: list[tuple] = []
        self.default_reply = ["Hel", "lo", "!"]
        self.context_chars: int | None = None
        self.token_delay = 0.0
        self.wind_down = 0.0  # how long a cancelled request keeps the engine busy
        self.active: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.counter = 0
        self.sessions = 0
        self.last_token_sent_at: float | None = None
        self.retained = 0
        self.usage_prompt: int | None = None  # set: the stream tail carries usage
        self.build_info = {"backend": "llama.cpp/3581ba0c/cpu", "model": "qwen35-9b-q4-k-m",
                           "engine_version": "0.1.0"}
        self.runtime_extra: dict = {"snapshot_tokens": 0}  # a None value drops the field
        self.reply_delay = 0.0  # seconds before a non-streamed reply
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # the engine closes every connection

            def log_message(self, *a):
                pass

            def send(self, status, body, rid):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Request-Id", rid)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                rid = engine.next_id()
                if self.path == "/healthz":
                    return self.send(200, {"status": "ok", "lifecycle": engine.lifecycle()}, rid)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.send(401, {"error": {"code": "unauthorized"}}, rid)
                if self.path == "/readyz":
                    return self.send(200, {"ready": True, "lifecycle": engine.lifecycle()}, rid)
                if self.path == "/version":
                    return self.send(200, {"api_version": "v1", "engine_version": "0.1.0"}, rid)
                if self.path == "/build-info":
                    return self.send(200, engine.build_info, rid)
                if self.path == "/metrics":
                    runtime = {"context_tokens": 8192, "n_threads": 12, "n_threads_batch": 12,
                               "speculate_tokens": 0, "retained_prompt_tokens": engine.retained,
                               "last_reused_prefix_tokens": 40, **engine.runtime_extra}
                    return self.send(200, {"lifecycle": engine.lifecycle(), "active_generations": len(engine.active),
                                           "runtime": {k: v for k, v in runtime.items() if v is not None}}, rid)
                self.send(404, {"error": {"code": "not_found"}}, rid)

            def do_DELETE(self):
                self.send(200, {"deleted": True}, engine.next_id())

            def do_POST(self):
                rid = engine.next_id()
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.send(401, {"error": {"code": "unauthorized"}}, rid)
                if self.path == "/v1/sessions":
                    engine.sessions += 1
                    return self.send(201, {"id": f"sess-{engine.sessions:08d}", "object": "session"}, rid)
                if self.path.startswith("/v1/cancel/"):
                    target = self.path.rsplit("/", 1)[1]
                    engine.cancels.append(target)
                    event = engine.active.get(target)
                    if event:
                        event.set()
                    return self.send(200, {"cancelled": bool(event)}, rid)
                if self.path == "/v1/chat/completions":
                    return self.chat(body, rid)
                self.send(404, {"error": {"code": "not_found"}}, rid)

            def chat(self, body, rid):
                if engine.active:
                    return self.send(409, {"error": {"code": "busy", "request_id": rid}}, rid)
                engine.requests.append(body)
                step = engine.plan.pop(0) if engine.plan else ("reply", engine.default_reply)
                if step[0] == "http":
                    return self.send(step[1], {"error": {"code": step[2], "request_id": rid}}, rid)
                overflow = (engine.context_chars is not None and
                            sum(len(m["content"]) for m in body["messages"]) > engine.context_chars)
                if not body.get("stream"):
                    if overflow:
                        return self.send(400, {"error": {"code": "invalid_request", "request_id": rid}}, rid)
                    text = "".join(step[1])
                    time.sleep(engine.reply_delay)
                    return self.send(200, {"id": rid, "choices": [{"message": {"role": "assistant", "content": text},
                                                                   "finish_reason": "stop"}]}, rid)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Request-Id", rid)
                self.end_headers()
                if overflow or step[0] == "overflow_sse":
                    self.wfile.write(b'data: {"error":{"code":"invalid_request"}}\n\ndata: [DONE]\n\n')
                    return
                if step[0] == "sse_error":
                    self.wfile.write(b'data: {"error":{"code":"%s"}}\n\ndata: [DONE]\n\n' % step[1].encode())
                    return
                cancelled = threading.Event()
                engine.active[rid] = cancelled
                finish = "stop" if step[0] == "reply" else "length"
                try:
                    for token in step[1]:
                        if cancelled.is_set():
                            finish = "cancelled"
                            break
                        raw = token if isinstance(token, bytes) else json.dumps(token)[1:-1].encode()
                        # Bytes go into the JSON string raw, as the engine's
                        # json_escape does with a split UTF-8 character.
                        self.wfile.write(b'data: {"id":"' + rid.encode() + b'","choices":[{"delta":{"content":"'
                                         + raw + b'"}}]}\n\n')
                        self.wfile.flush()
                        engine.last_token_sent_at = time.monotonic()
                        time.sleep(engine.token_delay)
                    else:
                        engine.retained = 100 + len(step[1])
                    usage = (b',"usage":{"prompt_tokens":%d,"completion_tokens":%d}' % (engine.usage_prompt, len(step[1]))
                             if engine.usage_prompt is not None else b"")
                    self.wfile.write(b'data: {"id":"%s","choices":[{"delta":{},"finish_reason":"%s"}]%s}\n\n'
                                     b"data: [DONE]\n\n" % (rid.encode(), finish.encode(), usage))
                except OSError:
                    pass  # the client went away; the engine notices the same way
                finally:
                    if finish == "cancelled" or cancelled.is_set():
                        time.sleep(engine.wind_down)
                    del engine.active[rid]

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def next_id(self) -> str:
        with self.lock:
            self.counter += 1
            return f"req-{self.counter}"

    def lifecycle(self) -> str:
        return "BUSY" if self.active else "READY"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


FAKE_ENGINE_PY = """
import json, os, sys, time
if sys.argv[1:2] == ["version"]:
    print("0.1.0")
    raise SystemExit(0)
sys.stdin.read()  # the token, read to EOF as the engine does
port = os.environ.get("FAKE_ENGINE_PORT")
if port:  # report ready on an in-process FakeEngine; without it, "load" forever
    print(json.dumps({"event": "ready", "port": int(port)}), flush=True)
time.sleep(600)
"""


def write_fake_engine(directory: Path, name: str = "lae-engine", source: str = FAKE_ENGINE_PY) -> Path:
    """An executable stand-in for `lae-engine`: a real child process to start and stop.

    A shell wrapper because a shebang cannot name an interpreter whose path has
    spaces; `exec` keeps the PID, so stopping it stops the Python process.
    """
    (directory / f"{name}.py").write_text(source, encoding="utf-8")
    exe = directory / name
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$0.py" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    return exe


def reap_engine(eng) -> None:
    """Test cleanup for an Engine started on the stand-in executable."""
    # Kill first: if Engine left the stand-in running (the bug the lifecycle
    # tests catch), closing its stdout would block on the reader thread forever.
    if eng.proc is None:
        return
    if eng.proc.poll() is None:
        eng.proc.kill()
        eng.proc.wait()
    for pipe in (eng.proc.stdin, eng.proc.stdout):
        pipe.close()


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.addCleanup(self.engine.close)
        self.api = bmo_chat.EngineAPI(self.engine.port, TOKEN, timeout=30)
        self.chat = bmo_chat.Chat(self.api, self.api.new_session(), bmo_chat.Conversation())
        self.shown: list[str] = []

    def say(self, text: str) -> bmo_chat.TurnResult:
        return self.chat.send(text, self.shown.append)


class HistoryAndStreamingTests(EngineCase):
    def test_full_history_is_sent_every_turn_on_one_session(self):
        self.chat.conversation.system = "You are BMO."
        replies = [["A", "1"], ["B", "2"], ["C", "3"]]
        self.engine.plan = [("reply", r) for r in replies]
        for question in ("q1", "q2", "q3"):
            self.assertEqual(self.say(question).status, "ok")
        sent = self.engine.requests
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[2]["messages"], [
            {"role": "system", "content": "You are BMO."},
            {"role": "user", "content": "q1"}, {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "q2"}, {"role": "assistant", "content": "B2"},
            {"role": "user", "content": "q3"}])
        self.assertEqual([len(r["messages"]) for r in sent], [2, 4, 6])
        self.assertEqual({r["session_id"] for r in sent}, {"sess-00000001"})
        self.assertTrue(all(r["stream"] is True and r["max_tokens"] == 1024 and r["mode"] == "normal" for r in sent))

    def test_text_is_shown_while_the_reply_is_still_being_written(self):
        self.engine.token_delay = 0.05
        tokens = [f"w{i} " for i in range(10)]
        self.engine.plan = [("reply", tokens)]
        seen_at: list[float] = []
        result = self.chat.send("hi", lambda text: seen_at.append(time.monotonic()))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "".join(tokens))
        self.assertEqual(len(seen_at), 10, "one update per token, not one at the end")
        self.assertLess(seen_at[0], self.engine.last_token_sent_at,
                        "the first words must appear before the engine has sent the last")
        self.assertEqual(result.stats.generated_tokens, 10)

    def test_a_character_split_across_tokens_arrives_whole(self):
        smile = "\N{SMILING FACE WITH OPEN MOUTH}".encode()
        self.engine.plan = [("reply", ["ok ", smile[:2], smile[2:], " ü"])]
        result = self.say("hi")
        self.assertEqual(result.text, "ok \N{SMILING FACE WITH OPEN MOUTH} ü")
        self.assertNotIn("�", "".join(self.shown))

    def test_stats_report_prompt_tokens_time_and_speed(self):
        self.engine.token_delay = 0.01
        self.engine.plan = [("reply", ["a", "b", "c", "d"])]
        self.say("hi")
        err = io.StringIO()
        self.chat.command("/stats", err)
        report = err.getvalue()
        self.assertIn("prompt_tokens: 100", report)  # no usage block: retained 104 - 4 written
        self.assertIn("tokens/s", report)
        self.assertIn("4 tokens written", report)
        self.assertIn("context 8192", report)

    def test_stats_prefer_the_usage_the_stream_reports(self):
        self.engine.usage_prompt = 777
        self.engine.plan = [("reply", ["a", "b"])]
        self.assertEqual(self.say("hi").stats.prompt_tokens, 777)

    def test_length_cap_is_explained(self):
        self.engine.plan = [("length", ["x"])]
        result = self.say("long please")
        self.assertEqual(result.stats.finish_reason, "length")
        self.assertIn("1024-token limit", bmo_chat.describe_failure(result, self.chat))


class OverflowTests(EngineCase):
    def fill(self, n):
        for i in range(n):
            self.engine.plan.append(("reply", [f"answer{i}"]))
            self.assertEqual(self.say(f"question{i}").status, "ok")

    def test_overflow_drops_whole_oldest_exchanges_and_the_turn_succeeds(self):
        self.chat.conversation.system = "sys"
        self.fill(3)
        # Room for the system prompt, one exchange and the new question only.
        self.engine.context_chars = 3 + (9 + 7) + 9
        result = self.say("question3")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.dropped_exchanges, 2)
        sent = self.engine.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "user"])
        self.assertEqual(sent[1]["content"], "question2", "the newest exchanges survive")
        self.assertEqual(self.chat.conversation.exchanges, 2, "the survivor plus the new exchange")

    def test_without_auto_trim_the_user_is_asked_and_trim_resends(self):
        self.chat.auto_trim = False
        self.fill(2)
        self.engine.context_chars = 20
        result = self.say("question2")
        self.assertEqual(result.status, "overflow")
        self.assertEqual(self.chat.conversation.exchanges, 2, "a refused turn changes nothing")
        note = bmo_chat.describe_failure(result, self.chat)
        self.assertIn("/trim", note)
        self.assertIn("/reset", note)
        # /trim drops one exchange and hands the unsent message back.
        out, err = io.StringIO(), io.StringIO()
        lines = iter(["/trim", "/quit"])
        self.engine.context_chars = 9 + 7 + 9
        self.assertEqual(bmo_chat.repl(self.chat, lambda _: next(lines), out, err), 0)
        self.assertEqual(self.engine.requests[-1]["messages"][-1]["content"], "question2")
        self.assertEqual(self.chat.last.status, "ok")

    def test_a_message_too_long_on_its_own_is_explained_not_crashed(self):
        self.engine.context_chars = 5
        result = self.say("far too long for this context")
        self.assertEqual(result.status, "overflow")
        self.assertIn("shorten it", bmo_chat.describe_failure(result, self.chat))

    def test_plain_http_400_overflow_takes_the_same_path(self):
        self.chat.max_tokens = bmo_chat.LEGACY_REPLY_TOKENS
        self.fill(1)
        self.engine.plan = [("http", 400, "invalid_request")]
        result = self.say("next")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.dropped_exchanges, 1)

    def test_an_engine_that_allows_only_256_reply_tokens_costs_no_history(self):
        # An engine built before the 2,048 ceiling refuses max_tokens 1024 in
        # its parser (HTTP 400), which must not be mistaken for an overflow.
        self.fill(2)
        self.engine.plan = [("http", 400, "invalid_request")]
        with redirect_stderr(io.StringIO()) as err:
            result = self.say("next")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.dropped_exchanges, 0)
        self.assertEqual(self.engine.requests[-1]["max_tokens"], 256)
        self.assertEqual(self.chat.conversation.exchanges, 3)
        self.assertIn("256 tokens per reply", err.getvalue())

    def test_engine_request_bounds_are_trimmed_to_before_sending(self):
        # 64 messages is the engine's ceiling; 31 exchanges + 1 is 63, 32 is 65.
        for i in range(32):
            self.chat.conversation.commit(f"u{i}", f"a{i}")
        self.assertEqual(self.say("new").status, "ok")
        self.assertLessEqual(len(self.engine.requests[-1]["messages"]), bmo_chat.MAX_MESSAGES)

    def test_trim_never_splits_an_exchange(self):
        conv = bmo_chat.Conversation("s")
        conv.commit("u0", "a0")
        conv.commit("u1", "a1")
        self.assertTrue(conv.trim_oldest())
        self.assertEqual(conv.messages("u2"), [{"role": "system", "content": "s"}, {"role": "user", "content": "u1"},
                                               {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}])
        self.assertTrue(conv.trim_oldest())
        self.assertFalse(conv.trim_oldest())


class RefusalTests(EngineCase):
    def test_busy_and_not_ready_get_their_own_explanations(self):
        self.engine.plan = [("http", 409, "busy"), ("http", 503, "not_ready")]
        busy, not_ready = self.say("a"), self.say("b")
        self.assertEqual((busy.status, not_ready.status), ("busy", "not_ready"))
        self.assertIn("still finishing", bmo_chat.describe_failure(busy, self.chat))
        self.assertIn("not ready", bmo_chat.describe_failure(not_ready, self.chat))
        self.assertEqual(self.chat.conversation.exchanges, 0)

    def test_busy_as_a_stream_error_frame(self):
        self.engine.plan = [("sse_error", "busy")]
        self.assertEqual(self.say("a").status, "busy")

    def test_a_lost_session_is_replaced_once(self):
        self.engine.plan = [("http", 404, "not_found")]
        self.assertEqual(self.say("a").status, "ok")
        self.assertEqual(self.engine.requests[-1]["session_id"], "sess-00000002")

    def test_a_stopped_engine_is_reported(self):
        self.engine.close()
        result = self.say("a")
        self.assertEqual(result.status, "engine_gone")


class CancellationTests(EngineCase):
    def test_ctrl_c_mid_reply_cancels_on_the_engine_and_the_next_turn_works(self):
        self.engine.token_delay = 0.05
        self.engine.wind_down = 0.4  # the engine stops at its next check, not instantly
        self.engine.plan = [("reply", [f"t{i}" for i in range(200)]), ("reply", ["fine"])]
        chunks = []

        def ctrl_c_after_three(text):
            chunks.append(text)
            if len(chunks) == 3:
                raise KeyboardInterrupt  # what Python raises on Ctrl+C in this thread

        started = time.monotonic()
        result = self.chat.send("count forever", ctrl_c_after_three)
        self.assertEqual(result.status, "cancelled")
        self.assertLess(time.monotonic() - started, 5, "must not wait for the 200 tokens")
        self.assertEqual(self.engine.cancels, [result.request_id], "cancelled by the id the engine issued")
        self.assertEqual(self.engine.lifecycle(), "READY", "returns only once the engine is idle")
        self.assertEqual(self.chat.conversation.exchanges, 0, "a stopped reply is not history")
        nxt = self.say("hello again")
        self.assertEqual(nxt.status, "ok")
        self.assertEqual(nxt.text, "fine")
        self.assertEqual([m["role"] for m in self.engine.requests[-1]["messages"]], ["user"])

    @unittest.skipIf(os.name == "nt", "sends a POSIX SIGINT")
    def test_a_real_ctrl_c_signal_stops_the_reply_and_the_chat_carries_on(self):
        """The same, with an actual SIGINT to a separate chat process mid-reply."""
        self.engine.token_delay = 0.05
        self.engine.plan = [("reply", [f"t{i} " for i in range(200)]), ("reply", ["second ", "reply"])]
        child = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import bmo_chat as c; "
             "api = c.EngineAPI(int(sys.argv[2]), sys.argv[3], timeout=30); "
             "raise SystemExit(c.repl(c.Chat(api, api.new_session(), c.Conversation()), read_line=lambda p: input()))",
             str(ROOT / "local"), str(self.engine.port), TOKEN],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)
        child.stdin.write("count forever\n")
        child.stdin.flush()
        deadline = time.monotonic() + 10
        while not self.engine.active and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)  # a few tokens in
        child.send_signal(signal.SIGINT)
        out, err = child.communicate("hello\n/quit\n", timeout=30)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(len(self.engine.cancels), 1)
        self.assertIn("(stopped)", err)
        self.assertIn("second reply", out)
        self.assertEqual([m["content"] for m in self.engine.requests[-1]["messages"]], ["hello"])

    def test_ctrl_c_at_the_prompt_ends_the_chat_cleanly(self):
        def interrupt(_):
            raise KeyboardInterrupt
        self.assertEqual(bmo_chat.repl(self.chat, interrupt, io.StringIO(), io.StringIO()), 0)


class NothingOnDiskTests(EngineCase):
    def test_a_whole_chat_writes_no_file(self):
        self.engine.plan = [("reply", [SECRET]), ("reply", [SECRET])]
        writes = []
        real_open = builtins.open

        def watching_open(file, mode="r", *a, **k):
            if any(flag in mode for flag in "wax+"):
                writes.append(str(file))
            return real_open(file, mode, *a, **k)

        lines = iter(["hello", "/stats", "/system be brief", "again", "/reset", "/quit"])
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.object(builtins, "open", watching_open), \
                        mock.patch.object(io, "open", watching_open), \
                        mock.patch.object(Path, "write_text", side_effect=AssertionError("write_text")), \
                        mock.patch.object(Path, "write_bytes", side_effect=AssertionError("write_bytes")):
                    self.assertEqual(bmo_chat.repl(self.chat, lambda _: next(lines), out, err), 0)
            finally:
                os.chdir(cwd)
            self.assertEqual(os.listdir(tmp), [])
        self.assertEqual(writes, [])
        self.assertIn(SECRET, out.getvalue(), "the reply is shown on the console")

    def test_launchers_have_no_file_writing_code(self):
        for path in LAUNCHERS[:2]:
            source = path.read_text(encoding="utf-8")
            for pattern in (r"(?<![.\w])open\(", r"write_text", r"write_bytes", r"\.dump\("):
                self.assertIsNone(re.search(pattern, source), f"{path.name}: {pattern}")


class TokenHandlingTests(unittest.TestCase):
    def test_no_launcher_mentions_a_token_file(self):
        for path in LAUNCHERS:
            self.assertNotIn("--token-file", path.read_text(encoding="utf-8"), path.name)
            self.assertNotIn("token-file", path.read_text(encoding="utf-8").lower(), path.name)

    def test_engine_gets_the_token_on_stdin_and_starts_outside_the_ctrl_c_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine_exe, model = Path(tmp) / "lae-engine", Path(tmp) / "m.gguf"
            engine_exe.write_bytes(b"")
            model.write_bytes(b"")
            args = bmo_chat.build_parser().parse_args(
                ["--engine", str(engine_exe), "--model", str(model), "--log", str(Path(tmp) / "e.log")])
            proc = mock.Mock()
            proc.stdout.readline.return_value = b'{"event":"ready","port":4321}\n'
            proc.poll.return_value = 0
            with mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
                eng = bmo_chat.ChatEngine(args).start()
            argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
            self.assertIn("--token-stdin", argv)
            self.assertFalse(any(eng.token in part for part in argv), "token on the command line")
            proc.stdin.write.assert_called_once_with(eng.token.encode())
            for key, value in bmo_chat.detached_popen_kwargs().items():
                self.assertEqual(kwargs.get(key), value)
            self.assertIs(bmo_local.subprocess, subprocess, "the Popen shim must not leak")

    def test_host_gets_the_token_in_its_environment_only(self):
        inherited = {"PATH": "/bin", "LAE_ENGINE_MODE": "fixture", "LAE_CONFIG_PATH": "/x.json", "lae_port": "1"}
        env = bmo_app.host_environment(inherited, port=4321, token=TOKEN, model="qwen35-9b-q4-k-m",
                                       backend="llama.cpp/3581ba0c/cpu")
        argv = bmo_app.host_command("/usr/bin/node")
        self.assertFalse(any(TOKEN in part for part in argv))
        self.assertEqual(env["LAE_ENGINE_TOKEN"], TOKEN)
        self.assertEqual([k for k, v in env.items() if TOKEN in v], ["LAE_ENGINE_TOKEN"])
        self.assertEqual(env["LAE_ENGINE_MODE"], "native")
        self.assertEqual(env["LAE_ENGINE_ENDPOINT"], "http://127.0.0.1:4321")
        self.assertEqual(env["LAE_REVEAL_BOOTSTRAP_URL"], "1")
        self.assertNotIn("LAE_CONFIG_PATH", env, "an inherited host config is not picked up silently")
        self.assertNotIn("lae_port", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_start_script_mirrors_the_test_script_engine_resolution(self):
        start = (ROOT / "local" / "windows" / "Start-BMO.ps1").read_text(encoding="utf-8")
        test = (ROOT / "local" / "windows" / "Test-BMO.ps1").read_text(encoding="utf-8")
        block = re.compile(r"\$Built = .*?\nWrite-Host \"== engine: \$Engine\"", re.S)
        self.assertEqual(block.search(start).group(0), block.search(test).group(0))
        self.assertIn("$ExpectedSize = 5629109088", start)
        self.assertIn(str(bmo_local.MODEL_SIZE), start)
        for flag in ("--threads", "--threads-batch", "--speculate", "--gpu-layers", "--vulkan-device-name"):
            self.assertIn(f"'{flag}'", start)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell not installed")
    def test_start_script_parses(self):
        script = ROOT / "local" / "windows" / "Start-BMO.ps1"
        check = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                 f"'{script}',[ref]$null,[ref]$e); exit $e.Count")
        self.assertEqual(subprocess.run(["pwsh", "-NoProfile", "-Command", check], timeout=120).returncode, 0)


@unittest.skipIf(os.name == "nt", "the stand-in engine is a shell script")
class EngineLifecycleTests(unittest.TestCase):
    """bmo_local.Engine owns its process from Popen on, before `with` is entered."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.exe = write_fake_engine(self.dir)
        self.model = self.dir / "m.gguf"
        self.model.write_bytes(b"")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FAKE_ENGINE_PORT", None)  # the stand-in never reports ready

    def args(self, parser=bmo_local.build_parser, prefix=("smoke",)):
        return parser().parse_args([*prefix, "--engine", str(self.exe), "--model", str(self.model),
                                    "--log", str(self.dir / "e.log"), "--ready-timeout", "30"])

    def interrupt_soon(self, after=0.5):
        timer = threading.Timer(after, _thread.interrupt_main)  # what Ctrl+C does to the main thread
        timer.start()
        self.addCleanup(timer.cancel)

    def engine(self, cls=bmo_local.Engine):
        eng = cls(self.args())

        self.addCleanup(reap_engine, eng)
        return eng

    def test_ctrl_c_during_the_model_load_stops_the_engine(self):
        eng = self.engine()
        self.interrupt_soon()
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            eng.start()
        self.assertLess(time.monotonic() - started, 10, "Ctrl+C must not wait out --ready-timeout")
        self.assertIsNotNone(eng.proc.poll(), "the engine outlived a Ctrl+C during its load")

    def test_any_failure_after_the_engine_started_stops_it(self):
        eng = self.engine()
        with mock.patch.object(bmo_local.Engine, "_await_ready", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                eng.start()
        self.assertIsNotNone(eng.proc.poll())

    def test_chat_returns_130_and_leaves_no_engine_on_ctrl_c_during_load(self):
        # The chat engine is outside the terminal's Ctrl+C group, so nothing
        # but the launcher can stop it.
        for name in ("SIGTERM", "SIGHUP"):  # bmo_chat.main installs handlers
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))
        started = []

        class Watched(bmo_chat.ChatEngine):
            def start(self):
                started.append(self)
                return super().start()

        argv = ["--engine", str(self.exe), "--model", str(self.model), "--log", str(self.dir / "e.log")]
        self.interrupt_soon()
        with mock.patch.object(bmo_chat, "ChatEngine", Watched), redirect_stderr(io.StringIO()):
            self.assertEqual(bmo_chat.main(argv), 130)
        self.addCleanup(reap_engine, started[0])
        self.assertIsNotNone(started[0].proc.poll())

    def test_popen_options_reach_the_engine_but_never_replace_its_pipes(self):
        proc = mock.Mock()
        proc.stdout.readline.return_value = b'{"event":"ready","port":4321}\n'
        with mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            bmo_local.Engine(self.args(), popen_kwargs={"start_new_session": True, "stdin": None,
                                                        "stdout": None, "stderr": None}).start()
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual((kwargs["stdin"], kwargs["stdout"]), (subprocess.PIPE, subprocess.PIPE))
        self.assertIsNotNone(kwargs["stderr"], "the engine log must still be the engine's stderr")

    def test_an_exe_the_os_will_not_start_gets_an_antivirus_hint(self):
        # What Windows raises when Defender has quarantined or is holding the exe.
        with mock.patch.object(subprocess, "Popen", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(SystemExit) as stop:
                bmo_local.Engine(self.args()).start()
        self.assertIn("Windows Security", str(stop.exception.code))

    def test_console_output_survives_characters_the_console_cannot_encode(self):
        # A redirected Windows console writes in the ANSI code page.
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stdout", narrow):
            bmo_local.console_safe_output()
            print("\u4e2d\u6587 \N{SMILING FACE WITH OPEN MOUTH}")
            narrow.flush()
        self.assertIs(bmo_chat._console_safe_output, bmo_local.console_safe_output)
        with mock.patch.object(bmo_local, "console_safe_output") as safe, \
                mock.patch.object(bmo_local, "cmd_smoke", return_value=0):
            bmo_local.main(["smoke", "--engine", "e", "--model", "m"])
        safe.assert_called_once()


def _node_ok() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    return out.startswith(("v24.", "v25."))


class AppLaunchTests(unittest.TestCase):
    def test_a_missing_node_points_to_the_python_chat(self):
        with mock.patch.object(bmo_app, "node_on_path", return_value=None), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stop:
                bmo_app.find_node()
        self.assertIn("bmo_chat.py", str(stop.exception.code))

    @unittest.skipIf(os.name == "nt", "the stand-in node is a shell script")
    def test_a_node_planted_in_the_current_directory_never_runs(self):
        # Windows' own lookup (and shutil.which there) tries the current
        # directory before PATH; empty, "." and relative PATH entries do too.
        with tempfile.TemporaryDirectory() as tmp:
            work, real_dir = Path(tmp) / "work", Path(tmp) / "nodejs"
            work.mkdir()
            real_dir.mkdir()
            marker = Path(tmp) / "planted-node-ran"
            planted = f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\nprint('v25.0.0')\n"
            write_fake_engine(work, "node", planted)
            (work / "node.exe").write_text("", encoding="utf-8")  # the Windows name, planted too
            real =write_fake_engine(real_dir, "node", "print('v25.0.0')\n")
            (real_dir / "node.exe").write_text("", encoding="utf-8")
            sneaky = os.pathsep.join(["", ".", "work", str(work), str(real_dir)])
            only_cwd = os.pathsep.join(["", ".", str(work)])
            cwd = os.getcwd()
            os.chdir(work)
            try:
                for windows in (False, True):
                    found = bmo_app.node_on_path(sneaky, windows=windows)
                    self.assertEqual(Path(found).parent, real_dir)
                    self.assertTrue(Path(found).is_absolute())
                    self.assertIsNone(bmo_app.node_on_path(only_cwd, windows=windows))
                with mock.patch.dict(os.environ, {"PATH": sneaky}):
                    self.assertEqual(bmo_app.find_node(), str(real))
            finally:
                os.chdir(cwd)
            self.assertFalse(marker.exists(), "the planted node ran")

    def test_the_host_inherits_no_node_settings(self):
        inherited = {"PATH": "/bin", "NODE_OPTIONS": "--require /tmp/evil.js", "NODE_PATH": "/tmp/mods",
                     "NODE_EXTRA_CA_CERTS": "/tmp/ca.pem", "node_tls_reject_unauthorized": "0"}
        env = bmo_app.host_environment(inherited, port=1, token=TOKEN, model="m", backend="b")
        self.assertEqual([k for k in env if k.upper().startswith("NODE")], [])
        self.assertEqual(env["PATH"], "/bin")

    def test_supervise_notices_either_process_exiting(self):
        quick = subprocess.Popen([sys.executable, "-c", "pass"])
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertEqual(bmo_app.supervise(quick, slow, poll=0.05), "host exited")
            self.assertEqual(bmo_app.supervise(slow, quick, poll=0.05), "engine exited")
        finally:
            bmo_app.stop_host(slow, interrupted=False, grace=5)
        self.assertIsNotNone(slow.poll())

    @unittest.skipUnless(_node_ok(), "Node.js 24/25 not available")
    def test_the_real_host_talks_to_the_engine_with_the_handed_over_token(self):
        """Run `node lae-host.mjs` exactly as bmo_app does, against the stand-in engine."""
        engine = FakeEngine()
        self.addCleanup(engine.close)
        engine.default_reply = ["Hi ", "from ", "the ", "engine"]
        fake = mock.Mock(port=engine.port, token=TOKEN)
        fake.get.side_effect = lambda path: bmo_chat.EngineAPI(engine.port, TOKEN).call("GET", path)
        model, backend = bmo_app.engine_identity(fake)
        env = bmo_app.host_environment(os.environ, port=engine.port, token=TOKEN, model=model, backend=backend)
        host = bmo_app.start_host(shutil.which("node"), env)
        try:
            url = bmo_app.read_bootstrap_url(host, 30)
            self.assertIsNotNone(url, "host printed no bootstrap URL")
            origin, nonce = url.split("/#bootstrap=")
            token = self._post(origin, "/bootstrap", {"nonce": nonce}, as_page=True)["token"]
            session = self._post(origin, "/api/sessions", {}, token=token)["session_id"]
            stream = self._post(origin, "/api/chat", {"session_id": session, "message": "hello",
                                                      "request_id": "req_test_0001"}, token=token, raw=True)
            self.assertIn("Hi from the engine", "".join(re.findall(r'"text":"([^"]*)"', stream)))
            self.assertEqual(engine.requests[-1]["messages"][-1], {"role": "user", "content": "hello"})
        finally:
            bmo_app.stop_host(host, interrupted=False, grace=10)
            host.stdout.close()
        self.assertIsNotNone(host.poll())

    @staticmethod
    def _post(origin, path, body, token=None, raw=False, as_page=False):
        req = urllib.request.Request(origin + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if as_page:  # the one-time bootstrap answers only the UI page's own fetch
            req.add_header("Origin", origin)
            req.add_header("Sec-Fetch-Site", "same-origin")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode()
        return data if raw else json.loads(data)


if __name__ == "__main__":
    unittest.main()
