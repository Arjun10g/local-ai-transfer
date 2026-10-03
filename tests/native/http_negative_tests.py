#!/usr/bin/env python3
"""Negative loopback/auth tests for the fixture HTTP boundary."""

import http.client
import json
import socket
import signal
import subprocess
import sys
import time
import tempfile
from pathlib import Path


def request(port, method, path, headers=None, body=None, omit_host=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    supplied_host = any(key.lower() == "host" for key in (headers or {}))
    conn.putrequest(method, path, skip_host=omit_host or supplied_host)
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


def raw_request(port, payload, timeout=3):
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(payload)
        chunks = []
        while True:
            part = sock.recv(4096)
            if not part:
                break
            chunks.append(part)
        return b"".join(chunks)
    finally:
        sock.close()


def main():
    executable = sys.argv[1]
    use_stdin_token = sys.platform.startswith("win")
    launcher = Path(__file__).parents[2] / "release" / "windows" / "Run-WindowsBackend.ps1"
    launcher_text = launcher.read_text(encoding="utf-8")
    if "throw 'NOT_READY:" not in launcher_text or "no path was accessed" not in launcher_text:
        raise AssertionError("Windows launcher must refuse before input access or process creation")
    for args in ([executable, "serve", "--port", "0"], [executable, "serve", "--port", "0", "--token-file", ""]):
        result = subprocess.run(args, capture_output=True, text=True, timeout=2)
        if result.returncode != 2 or "bearer token source" not in result.stderr:
            raise AssertionError(f"missing/empty token was accepted: {args!r} rc={result.returncode} stderr={result.stderr!r}")
    for value in ("0", "257", "-1", "4x", ""):
        result = subprocess.run([executable, "serve", "--port", "0", "--threads", value], capture_output=True, text=True, timeout=2)
        if result.returncode != 2 or "invalid numeric argument" not in result.stderr:
            raise AssertionError(f"out-of-range --threads {value!r} was accepted: rc={result.returncode} stderr={result.stderr!r}")
    for value in ("0", "257", "-1", "4x", ""):
        result = subprocess.run([executable, "serve", "--port", "0", "--threads-batch", value], capture_output=True, text=True, timeout=2)
        if result.returncode != 2 or "invalid numeric argument" not in result.stderr:
            raise AssertionError(f"out-of-range --threads-batch {value!r} was accepted: rc={result.returncode} stderr={result.stderr!r}")
    for value in ("9", "-1", "2x", ""):
        result = subprocess.run([executable, "serve", "--port", "0", "--speculate", value], capture_output=True, text=True, timeout=2)
        if result.returncode != 2 or "invalid numeric argument" not in result.stderr:
            raise AssertionError(f"out-of-range --speculate {value!r} was accepted: rc={result.returncode} stderr={result.stderr!r}")
    for value in ("0", "9", "-1", "two", "2x"):
        result = subprocess.run([executable, "serve", "--port", "0", "--snapshots", value], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 or "invalid numeric argument" not in result.stderr:
            raise AssertionError(f"out-of-range --snapshots {value!r} was accepted: rc={result.returncode} stderr={result.stderr!r}")
    for value in ("1", "29", "86401", "-1", "soon", "30x"):
        result = subprocess.run([executable, "serve", "--port", "0", "--idle-unload", value], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 or "invalid numeric argument" not in result.stderr:
            raise AssertionError(f"out-of-range --idle-unload {value!r} was accepted: rc={result.returncode} stderr={result.stderr!r}")
    result = subprocess.run([executable, "serve", "--port", "0", "--speculate", "0"], capture_output=True, text=True, timeout=2)
    if result.returncode != 2 or "bearer token source" not in result.stderr:
        raise AssertionError(f"--speculate 0 (off) did not parse through to the token check: stderr={result.stderr!r}")
    result = subprocess.run([executable, "serve", "--port", "0", "--threads", "256"], capture_output=True, text=True, timeout=2)
    if result.returncode != 2 or "bearer token source" not in result.stderr:
        raise AssertionError(f"--threads 256 did not parse through to the token check: stderr={result.stderr!r}")
    if not use_stdin_token:
        with tempfile.TemporaryDirectory() as directory:
            insecure = Path(directory) / "insecure-token"
            insecure.write_text("insecure-token")
            insecure.chmod(0o644)
            result = subprocess.run([executable, "serve", "--port", "0", "--token-file", str(insecure)], capture_output=True, text=True, timeout=2)
            if result.returncode != 2:
                raise AssertionError("group/world-readable token file was accepted")
            alias = Path(directory) / "token-alias"
            alias.symlink_to(insecure)
            result = subprocess.run([executable, "serve", "--port", "0", "--token-file", str(alias)], capture_output=True, text=True, timeout=2)
            if result.returncode != 2:
                raise AssertionError("token symlink was accepted")
            short = Path(directory) / "short-token"
            short.write_text("too-short")
            short.chmod(0o600)
            result = subprocess.run([executable, "serve", "--port", "0", "--token-file", str(short)], capture_output=True, text=True, timeout=2)
            if result.returncode != 2:
                raise AssertionError("short bearer token was accepted")
            oversized = Path(directory) / "oversized-token"
            oversized.write_text("x" * 513)
            oversized.chmod(0o600)
            result = subprocess.run([executable, "serve", "--port", "0", "--token-file", str(oversized)], capture_output=True, text=True, timeout=2)
            if result.returncode != 2:
                raise AssertionError("oversized bearer token was accepted")
        result = subprocess.run([executable, "serve", "--port", "0", "--token", "legacy-token"], capture_output=True, text=True, timeout=2)
        if result.returncode != 2 or "unknown argument" not in result.stderr:
            raise AssertionError("legacy token argv was accepted")
    with tempfile.TemporaryDirectory() as directory:
        token_file = Path(directory) / "token"
        token_file.write_text("config-test-token")
        token_file.chmod(0o600)
        config = Path(directory) / "config.local.json"
        config.write_text(json.dumps({"model_path": str(Path(directory).resolve() / "Qwen3.5-9B-Q4_K_M.gguf")}))
        token_args = ["--token-stdin"] if use_stdin_token else ["--token-file", str(token_file)]
        result = subprocess.run([executable, "serve", "--config", str(config), *token_args], input="config-test-token\n" if use_stdin_token else None, capture_output=True, text=True, timeout=2)
        if result.returncode == 2 and "unknown argument" in result.stderr:
            raise AssertionError(f"native CLI rejected launcher's --config: {result.stderr!r}")
        if "model validation failed:" not in result.stderr:
            raise AssertionError(f"--config did not reach explicit model validation: rc={result.returncode} stderr={result.stderr!r}")

    with tempfile.TemporaryDirectory() as directory:
      token_file = Path(directory) / "token"
      token_file.write_text("negative-test-token")
      token_file.chmod(0o600)
      token_args = ["--token-stdin"] if use_stdin_token else ["--token-file", str(token_file)]
      process = subprocess.Popen([executable, "serve", "--port", "0", *token_args], stdin=subprocess.PIPE if use_stdin_token else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
      if use_stdin_token:
          process.stdin.write("negative-test-token\n")
          process.stdin.close()
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
        duplicate = raw_request(port, f"GET /readyz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer negative-test-token\r\n\r\n".encode())
        if b" 400 " not in duplicate or b"invalid_headers" not in duplicate:
            raise AssertionError(f"duplicate security header was accepted: {duplicate!r}")
        malformed = raw_request(port, f"GET /readyz extra HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer negative-test-token\r\n\r\n".encode())
        if b" 400 " not in malformed or b"invalid_request_line" not in malformed:
            raise AssertionError(f"malformed request line was accepted: {malformed!r}")
        slow = socket.create_connection(("127.0.0.1", port), timeout=3)
        slow.settimeout(8)
        try:
            slow.sendall(f"GET /readyz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer negative-test-token\r\n".encode())
            timed = slow.recv(4096)
            if b" 408 " not in timed or b"request_timeout" not in timed:
                raise AssertionError(f"slowloris did not receive typed timeout: {timed!r}")
        finally:
            slow.close()
        for _ in range(100):
            assert_status(request(port, "GET", "/healthz", {"Host": f"127.0.0.1:{port}"}), 200, "worker churn health")
        invalid_type = request(port, "POST", "/v1/sessions", {**auth, "Host": f"127.0.0.1:{port}", "Content-Type": "text/plain"}, "{}")
        assert_status(invalid_type, 415, "non-JSON content type")
        invalid_path = request(port, "POST", "/v1/cancel/../etc", {**auth, "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}, "{}")
        assert_status(invalid_path, 400, "invalid cancellation path id")
        surplus = raw_request(port, f"POST /v1/sessions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer negative-test-token\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}x".encode())
        if b" 400 " not in surplus or b"surplus_body" not in surplus:
            raise AssertionError(f"surplus body was accepted: {surplus!r}")
        valid = request(port, "GET", "/readyz", {**auth, "Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"})
        assert_status(valid, 200, "valid loopback host/origin")
        build_info = request(port, "GET", "/build-info", {**auth, "Host": f"127.0.0.1:{port}"})
        assert_status(build_info, 200, "build info")
        build_payload = json.loads(build_info[2])
        if build_payload.get("backend") != "fixture-cpu/0.1.0" or build_payload.get("model") != "fixture":
            raise AssertionError(f"build info identity is not explicit: {build_payload!r}")
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
