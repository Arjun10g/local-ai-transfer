#!/usr/bin/env python3
"""Optional real-model smoke, enabled only with an explicit local artifact receipt."""

import http.client
import json
import os
import signal
import subprocess
import sys


def main():
    path = os.environ.get("LAE_QWEN35_MODEL")
    size = os.environ.get("LAE_QWEN35_MODEL_SIZE")
    sha256 = os.environ.get("LAE_QWEN35_MODEL_SHA256")
    if not (path and size and sha256):
        print("real model smoke: SKIP (set LAE_QWEN35_MODEL, LAE_QWEN35_MODEL_SIZE, and LAE_QWEN35_MODEL_SHA256)")
        return 0
    process = subprocess.Popen([sys.argv[1], "serve", "--backend", "cpu", "--model", path, "--size", size, "--sha256", sha256, "--token", "real-model-smoke"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready = json.loads(process.stdout.readline())
        connection = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=30)
        payload = json.dumps({"model": "qwen35-9b-q4-k-m", "messages": [{"role": "user", "content": "Reply with one short greeting."}], "max_tokens": 8})
        connection.request("POST", "/v1/chat/completions", payload, {"Authorization": "Bearer real-model-smoke", "Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        if response.status != 200:
            raise AssertionError(f"real model request failed: {response.status} {body[:200]}")
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
    print("real model smoke: PASS")


if __name__ == "__main__":
    main()
