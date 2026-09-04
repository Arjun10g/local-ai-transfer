#!/usr/bin/env python3
"""Optional real-model smoke, enabled only with an explicit local artifact receipt."""

import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile


def stop_process(process):
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main():
    path = os.environ.get("LAE_QWEN35_MODEL")
    size = os.environ.get("LAE_QWEN35_MODEL_SIZE")
    sha256 = os.environ.get("LAE_QWEN35_MODEL_SHA256")
    if not (path and size and sha256):
        print("real model smoke: SKIP (set LAE_QWEN35_MODEL, LAE_QWEN35_MODEL_SIZE, and LAE_QWEN35_MODEL_SHA256)")
        return 0
    request_timeout = float(os.environ.get("LAE_REAL_MODEL_TIMEOUT_SECONDS", "180"))
    if request_timeout < 1 or request_timeout > 600:
        raise ValueError("LAE_REAL_MODEL_TIMEOUT_SECONDS must be between 1 and 600")
    with tempfile.TemporaryFile(mode="w+t") as stderr_log:
        token_file = Path(path).with_name(".lae-token")
        token_file.write_text("real-model-smoke")
        token_file.chmod(0o600)
        token_args = ["--token-stdin"] if sys.platform.startswith("win") else ["--token-file", str(token_file)]
        process = subprocess.Popen([sys.argv[1], "serve", "--backend", "cpu", "--model", path, "--size", size, "--sha256", sha256, "--context", "512", *token_args], stdin=subprocess.PIPE if sys.platform.startswith("win") else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr_log, text=True)
        if sys.platform.startswith("win"):
            process.stdin.write("real-model-smoke\n")
            process.stdin.close()
        try:
            ready_line = process.stdout.readline()
            if not ready_line:
                process.wait(timeout=30)
                stderr_log.seek(0)
                raise AssertionError(f"real model server did not become ready: {stderr_log.read()[-1000:]!r}")
            ready = json.loads(ready_line)
            connection = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=request_timeout)
            payload = json.dumps({"model": "qwen35-9b-q4-k-m", "messages": [{"role": "user", "content": "Reply with exactly OK."}], "max_tokens": 2})
            connection.request("POST", "/v1/chat/completions", payload, {"Authorization": "Bearer real-model-smoke", "Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read().decode()
            connection.close()
            if response.status != 200:
                stop_process(process)
                stderr_log.seek(0)
                raise AssertionError(f"real model request failed: {response.status} {body[:200]} engine_stderr={stderr_log.read()[-1000:]!r}")
            result = json.loads(body)
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not isinstance(content, str) or not content.strip():
                raise AssertionError(f"real model returned an empty response: {body[:200]}")
        finally:
            stop_process(process)
    print("real model smoke: PASS")


if __name__ == "__main__":
    main()
