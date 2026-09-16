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
  serve   start the engine and keep it running until Ctrl+C, printing the
          endpoint and token so another program on this machine can use it.
"""

from __future__ import annotations

import argparse
import json
import os
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

DEFAULT_FIXTURE = ROOT / "tests" / "model" / "production_tool_call_eval.json"
MODEL_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
MODEL_SIZE = 5629109088
# The evaluator's own ceiling is 600 s, which remote runs rely on; see
# `run_local`'s `timeout_ceiling`.
LOCAL_TIMEOUT_CEILING = 3600


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Engine:
    """A running `lae-engine serve`, token on stdin, logs to a file."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
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
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        # stderr goes to a file so a chatty backend can never fill a pipe and
        # deadlock the engine. The engine keeps its own inherited handle.
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        self.proc.stdin.write(self.token.encode())
        self.proc.stdin.close()  # the engine reads the token to EOF
        line: list[bytes] = []
        reader = threading.Thread(target=lambda: line.append(self.proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(self.args.ready_timeout)
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
        return self

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


def cmd_serve(args: argparse.Namespace) -> int:
    with Engine(args) as eng:
        print(json.dumps({"endpoint": f"{eng.base}/v1/chat/completions", "token": eng.token,
                          "model_load_seconds": eng.ready_seconds}, indent=2))
        print("engine running; Ctrl+C to stop", file=sys.stderr)
        try:
            while eng.proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name, handler in (("smoke", cmd_smoke), ("eval", cmd_eval), ("serve", cmd_serve)):
        s = sub.add_parser(name)
        s.set_defaults(handler=handler)
        s.add_argument("--engine", required=True, help="path to lae-engine (lae-engine.exe on Windows)")
        s.add_argument("--model", required=True, help="path to Qwen3.5-9B-Q4_K_M.gguf")
        s.add_argument("--backend", default="cpu", choices=("cpu", "intel-vulkan"))
        s.add_argument("--vulkan-device-name")
        s.add_argument("--gpu-layers", type=int, help="intel-vulkan defaults to 99 (the whole model)")
        s.add_argument("--threads", type=int, help="CPU threads (default: every logical thread)")
        s.add_argument("--context", type=int, default=8192)
        s.add_argument("--ready-timeout", type=float, default=600)
        s.add_argument("--log", default=str(ROOT / "local" / "out" / f"engine-{name}.log"))
        if name in ("smoke", "eval"):
            s.add_argument("--out", help="write the JSON receipt here")
        if name == "eval":
            s.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
            s.add_argument("--cases", help="comma-separated case ids to score only those")
            s.add_argument("--show-output", action="store_true", help="print raw model output (console only)")
            # Every case sends a ~5,800-token prompt and the engine re-reads all of
            # it each time. A laptop CPU can need several minutes, and a client
            # that gives up leaves the engine busy (503s until it finishes), so
            # the wait is generous.
            s.add_argument("--timeout", type=float, default=1800)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
