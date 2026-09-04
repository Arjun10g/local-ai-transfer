#!/usr/bin/env python3
"""Negative loopback/auth tests for the fixture HTTP boundary."""

import http.client
import json
import signal
import subprocess
import sys
import time


def request(port, method, path, headers=None, body=None, omit_host=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.putrequest(method, path, skip_host=omit_host)
    for key, value in (headers or {}).items():
        conn.putheader(key, value)
    if body is not None:
        conn.putheader("Content-Length", str(len(body.encode())))
    conn.endheaders(body.encode() if body is not None else None)
    response = conn.getresponse()
    payload = response.read().decode()
    result = response.status, dict(response.getheaders()), payload
    conn.close()
    return result


def assert_status(actual, expected, label):
    if actual[0] != expected:
        raise AssertionError(f"{label}: expected {expected}, got {actual[0]} body={actual[2]!r}")


def main():
    executable = sys.argv[1]
    for args in ([executable, "serve", "--port", "0"], [executable, "serve", "--port", "0", "--token", ""]):
        result = subprocess.run(args, capture_output=True, text=True, timeout=2)
        if result.returncode != 2 or "non-empty --token" not in result.stderr:
            raise AssertionError(f"missing/empty token was accepted: {args!r} rc={result.returncode} stderr={result.stderr!r}")

    process = subprocess.Popen([executable, "serve", "--port", "0", "--token", "negative-test-token"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready_line = process.stdout.readline()
        ready = json.loads(ready_line)
        port = ready["port"]
        auth = {"Authorization": "Bearer negative-test-token"}

        assert_status(request(port, "GET", "/readyz"), 401, "missing bearer token")
        assert_status(request(port, "GET", "/readyz", {**auth, "Host": "127.0.0.1.evil"}), 400, "crafted host")
        assert_status(request(port, "GET", "/readyz", {**auth, "Host": "localhost.evil"}), 400, "crafted localhost")
        assert_status(request(port, "GET", "/readyz", auth, omit_host=True), 400, "missing host")
        assert_status(request(port, "GET", "/readyz", {**auth, "Origin": "http://127.0.0.1.evil"}), 400, "crafted origin")
        assert_status(request(port, "GET", "/readyz", {**auth, "Origin": "http://localhost.evil:1234"}), 400, "crafted origin port")
        valid = request(port, "GET", "/readyz", {**auth, "Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"})
        assert_status(valid, 200, "valid loopback host/origin")
        lowered = {key.lower(): value for key, value in valid[1].items()}
        if "access-control-allow-origin" in lowered or any(value == "*" for value in lowered.values()):
            raise AssertionError("fixture server emitted permissive CORS headers")
        chat_headers = {**auth, "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        valid_chat = json.dumps({"model": "fixture", "messages": [{"role": "system", "content": "policy"}, {"role": "user", "content": "hello"}], "stream": False, "max_tokens": 2})
        assert_status(request(port, "POST", "/v1/chat/completions", chat_headers, valid_chat), 200, "ordered message history")
        negative_bodies = [
            (json.dumps({"model": "fixture", "messages": [{"role": "developer", "content": "no"}]}), "unsupported role"),
            (json.dumps({"model": "fixture", "messages": [{"role": "user", "content": "ok", "extra": True}]}), "unknown message field"),
            (json.dumps({"model": "fixture", "messages": [{"role": "user", "content": "ok"}], "unknown": 1}), "unknown request field"),
            (json.dumps({"model": "fixture", "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1.5}), "fractional max_tokens"),
            (json.dumps({"model": "fixture", "messages": [{"role": "user", "content": "ok"}, {"role": "system", "content": "late"}]}), "system ordering"),
        ]
        for body, label in negative_bodies:
            assert_status(request(port, "POST", "/v1/chat/completions", chat_headers, body), 400, label)
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    print("HTTP negative loopback/auth tests: PASS")


if __name__ == "__main__":
    main()
