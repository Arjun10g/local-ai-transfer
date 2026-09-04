#!/usr/bin/env python3
"""Proves shallow GGUFs cannot replace the compiled product artifact identity."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    # A superficially GGUF-shaped 70-byte file used to pass when callers
    # supplied matching --size/--sha256 values. Product identity is compiled,
    # so it must fail before llama.cpp initialization.
    fixture = b"GGUF" + (3).to_bytes(4, "little") + bytes(62)
    with tempfile.TemporaryDirectory() as directory:
        model = Path(directory) / "Qwen3.5-9B-Q4_K_M.gguf"
        model.write_bytes(fixture)
        token = Path(directory) / "token"
        token.write_text("init-guard-token")
        token.chmod(0o600)
        token_args = ["--token-stdin"] if sys.platform.startswith("win") else ["--token-file", str(token)]
        process = subprocess.run([sys.argv[1], "serve", "--backend", "cpu", "--model", str(model), *token_args], input="init-guard-token\n" if sys.platform.startswith("win") else None, capture_output=True, text=True, timeout=30)
        if process.returncode != 2 or "model_size_mismatch" not in process.stderr:
            raise AssertionError(f"shallow GGUF was not rejected by compiled identity: rc={process.returncode} stderr={process.stderr!r}")

        config = Path(directory) / "config.local.json"
        config.write_text(json.dumps({
            "model_path": str(model),
            "model_size_bytes": len(fixture),
            "model_sha256": "0" * 64,
        }))
        overridden = subprocess.run([sys.argv[1], "serve", "--config", str(config), *token_args], input="init-guard-token\n" if sys.platform.startswith("win") else None, capture_output=True, text=True, timeout=30)
        if overridden.returncode != 2 or "unknown key" not in overridden.stderr:
            raise AssertionError(f"runtime config could override compiled identity: rc={overridden.returncode} stderr={overridden.stderr!r}")
    print("compiled product model identity guard: PASS")


if __name__ == "__main__":
    main()
