"""A deterministic stand-in for `lae-engine serve`, for long-context tests only.

It speaks, over loopback or in process (`in_process_opener`), the subset
of the engine's HTTP API that
`scripts/test/long_context_eval.py` uses (`POST /v1/sessions`,
`POST /v1/chat/completions` non-streaming, `GET /metrics`, `GET /readyz`,
`GET /build-info`)
and mirrors the engine's limits closely enough to exercise every outcome:

* a "token" is 3 characters of the rendered prompt; prompt + max_tokens over
  the context answers HTTP 400 `invalid_request`, like the engine's
  "context limit exceeded" (native/server/http_server.cpp);
* the request bounds of native/server/chat_request.cpp and http_server.cpp
  (64 messages, 32 KiB per string, 48 KiB of history, 64 KiB body) answer
  `request_too_large`;
* the retained prompt is reused only when a request in the same session
  strictly extends it (native/backend/llama_backend.cpp
  `reusable_prefix_tokens`), and `/metrics` reports it.

The "model" reads the planted facts back out of the prompt with regexes and
answers perfectly when the fact lies within `effective_window` tokens of the
end of the prompt, and wrongly otherwise. That gives a known accuracy-vs-size
curve the evaluator must reproduce. It never sees the evaluator's expected
values: everything it answers comes from the prompt text it was sent.
"""

from __future__ import annotations

import io
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET_OUTPUT_MARKER = "LCX-SECRET-MODEL-OUTPUT-5e0b"
FACT = re.compile(r"the (code word|badge number|on-call engineer|meeting room|workspace id) for project (\w+) is (?:now )?([^.]+?)\.")
TIME_CALL = "<tool_call>\n<function=time.now>\n<parameter=format>\nutc\n</parameter>\n</function>\n</tool_call>"


class FakeLongContextEngine:
    def __init__(self, *, context_tokens: int = 8192, effective_window: int | None = None,
                 delay_seconds: float = 0.0, token: str = "t" * 43, enforce_request_bounds: bool = True):
        self.context_tokens = context_tokens
        self.effective_window = effective_window  # None: perfect recall at any distance
        self.delay_seconds = delay_seconds
        self.token = token
        self.enforce_request_bounds = enforce_request_bounds
        self.requests: list[dict] = []  # every chat body received, for test introspection
        self.sessions: set[str] = set()
        self.state = ""  # rendered prompt + output the "context" retains
        self.state_session = ""
        self.runtime = {"context_tokens": context_tokens, "n_batch": context_tokens, "n_ubatch": 512,
                        "n_threads": 4, "n_threads_batch": 4, "speculate_tokens": 0,
                        "last_reused_prefix_tokens": 0, "last_common_prefix_tokens": 0,
                        "last_state_tokens": 0, "retained_prompt_tokens": 0}
        self.busy = False
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "FakeLongContextEngine":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up (timeout test)

            def _dispatch(self, method: str):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = fake.handle(method, self.path, self.headers.get("Authorization"), length, body)
                self._send(status, payload)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def base(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def handle(self, method: str, path: str, authorization: str | None, length: int,
               body: bytes) -> tuple[int, dict]:
        """One request, the same whether it arrived over loopback or in process."""
        if authorization != f"Bearer {self.token}":
            return 401, {"error": {"code": "unauthorized"}}
        if method == "GET" and path == "/metrics":
            return 200, {"lifecycle": "BUSY" if self.busy else "READY", "runtime": dict(self.runtime)}
        if method == "GET" and path == "/readyz":
            return 200, {"ready": True, "lifecycle": "BUSY" if self.busy else "READY"}
        if method == "GET" and path == "/build-info":
            return 200, {"backend": "fake/long-context", "engine_version": "0.0.0-fake", "llama_cpp_revision": "none"}
        if method == "POST" and length > 64 * 1024:
            return 413, {"error": {"code": "request_too_large"}}
        if method == "POST" and path == "/v1/sessions":
            with self._lock:
                sid = f"sess-{len(self.sessions) + 1:08d}"
                self.sessions.add(sid)
            return 201, {"id": sid, "object": "session", "state_version": 1}
        if method == "POST" and path == "/v1/chat/completions":
            return self.complete(json.loads(body or b"{}"))
        return 404, {"error": {"code": "not_found"}}

    def in_process_opener(self):
        """A stand-in for `evaluate_tool_calls._open_url` that never opens a socket."""
        fake = self

        def open_url(request: urllib.request.Request, timeout: float):
            parts = urllib.parse.urlsplit(request.full_url)
            data = request.data or b""
            status, payload = fake.handle(request.get_method(), parts.path, request.get_header("Authorization"),
                                          len(data), data)
            raw = json.dumps(payload).encode()
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(raw))
            response = io.BytesIO(raw)
            response.status = status
            return response
        return open_url

    # -- the "engine" ----------------------------------------------------------
    @staticmethod
    def render(messages: list[dict], tools: list[dict] | None) -> str:
        text = f"<|im_start|>system\n<tools>{json.dumps(tools)}</tools><|im_end|>\n" if tools else ""
        for m in messages:
            text += f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n"
        return text + "<|im_start|>assistant\n"

    @staticmethod
    def tokens(text: str) -> int:
        return math.ceil(len(text) / 3)

    def complete(self, body: dict) -> tuple[int, dict]:
        self.requests.append(body)
        messages, tools = body["messages"], body.get("tools")
        if body.get("session_id") not in self.sessions:
            return 404, {"error": {"code": "not_found"}}
        if self.enforce_request_bounds:
            contents = [m["content"].encode() for m in messages]
            if len(messages) > 64 or max(map(len, contents)) > 32768 or sum(map(len, contents)) > 49152:
                return 400, {"error": {"code": "request_too_large", "request_id": "req-1"}}
        if self.busy:
            return 409, {"error": {"code": "busy"}}
        rendered = self.render(messages, tools)
        prompt_tokens = self.tokens(rendered)
        if prompt_tokens + body["max_tokens"] > self.context_tokens:
            return 400, {"error": {"code": "invalid_request", "request_id": "req-1"}}
        self.busy = True
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            same = self.state_session == body["session_id"] and self.state
            common = len(_common_prefix(self.state, rendered)) if same else 0
            reused = self.tokens(self.state) if same and rendered.startswith(self.state) and len(rendered) > len(self.state) else 0
            output = self.answer(messages, tools, rendered)
            self.runtime.update({"last_reused_prefix_tokens": reused, "last_common_prefix_tokens": common // 3,
                                 "last_state_tokens": self.tokens(self.state) if same else 0})
            # The engine retains the prompt plus what it generated (not EOS).
            self.state, self.state_session = rendered + output, body["session_id"]
            self.runtime["retained_prompt_tokens"] = self.tokens(self.state)
        finally:
            self.busy = False
        completion = max(1, self.tokens(output))
        return 200, {"id": "req-1", "choices": [{"message": {"role": "assistant", "content": output},
                                                 "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion}}

    def _in_window(self, rendered: str, position: int) -> bool:
        if self.effective_window is None:
            return True
        return self.tokens(rendered[position:]) <= self.effective_window

    def _lookup(self, rendered: str, attribute: str, project: str) -> str | None:
        found = None
        for match in FACT.finditer(rendered):
            if match.group(1) == attribute and match.group(2) == project and self._in_window(rendered, match.start()):
                found = match.group(3)  # the last visible statement wins
        return found

    def answer(self, messages: list[dict], tools: list[dict] | None, rendered: str) -> str:
        user = [m for m in messages if m["role"] == "user"][-1]["content"]
        question = user.split("\n\n")[-1]
        tool_names = {t["function"]["name"] for t in tools or []}
        if messages[-1]["role"] == "user" and "First check the current UTC time" in question and "time.now" in tool_names:
            return TIME_CALL
        wrong = f"I am not sure. UNKNOWN-0000 ({SECRET_OUTPUT_MARKER})"
        m = re.search(r"Read the file (\S+) from my workspace for project (\w+)\.", question)
        if m:
            workspace = self._lookup(rendered, "workspace id", m.group(2)) or "ws-unknown-0000"
            return (f"<tool_call>\n<function=fs.read_text>\n<parameter=workspace_id>\n{workspace}\n</parameter>\n"
                    f"<parameter=path>\n{m.group(1)}\n</parameter>\n</function>\n</tool_call>")
        m = re.search(r"the code word, the badge number and the on-call engineer for project (\w+)\?", question)
        if m:
            values = [self._lookup(rendered, a, m.group(1)) for a in ("code word", "badge number", "on-call engineer")]
            if None in values:
                return wrong
            return f"code word: {values[0]}; badge number: {values[1]}; on-call engineer: {values[2]} ({SECRET_OUTPUT_MARKER})"
        m = re.search(r"What is the (code word|badge number|on-call engineer|meeting room) for project (\w+)", question)
        if m:
            value = self._lookup(rendered, m.group(1), m.group(2))
            return f"{value} ({SECRET_OUTPUT_MARKER})" if value else wrong
        m = re.search(r"fs\.read_text result for (\S+), what was the invoice_total", question)
        if m:
            hit = re.search(re.escape(f'"path": "{m.group(1)}"') + r'.{0,200}?"invoice_total": "([0-9.]+)"', rendered)
            if hit and self._in_window(rendered, hit.start()):
                return f"{hit.group(1)} ({SECRET_OUTPUT_MARKER})"
            return wrong
        if "Count from one to forty" in question:
            return "one, two, three, four, five, six, seven, eight, nine, ten"
        return wrong


def _common_prefix(a: str, b: str) -> str:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return a[:n]
