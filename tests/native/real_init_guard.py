#!/usr/bin/env python3
"""Proves a validated model reaches LlamaBackend initialization (no weights)."""

import hashlib
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    # Header has the required architecture key but no tensor payload; validator
    # passes it, while llama.cpp must reject it during model initialization.
    key = b"general.architecture"
    value = b"qwen35"
    fixture = b"GGUF" + (3).to_bytes(4, "little") + (1).to_bytes(8, "little") + (1).to_bytes(8, "little")
    fixture += len(key).to_bytes(8, "little") + key + (8).to_bytes(4, "little") + len(value).to_bytes(8, "little") + value
    with tempfile.TemporaryDirectory() as directory:
        model = Path(directory) / "Qwen3.5-9B-Q4_K_M.gguf"
        model.write_bytes(fixture)
        token = Path(directory) / "token"
        token.write_text("init-guard-token")
        token.chmod(0o600)
        token_args = ["--token-stdin"] if sys.platform.startswith("win") else ["--token-file", str(token)]
        process = subprocess.run([sys.argv[1], "serve", "--backend", "cpu", "--model", str(model), "--size", str(len(fixture)), "--sha256", hashlib.sha256(fixture).hexdigest(), *token_args], input="init-guard-token\n" if sys.platform.startswith("win") else None, capture_output=True, text=True, timeout=30)
    if process.returncode != 1 or "llama model load failed" not in process.stderr:
        raise AssertionError(f"backend initialization was not reached: rc={process.returncode} stderr={process.stderr!r}")
    print("real backend initialization guard: PASS")


if __name__ == "__main__":
    main()
