#!/usr/bin/env python3
"""Pre-flight for a demo: is this machine ready to run BMO, and does it?

Run it as `bmo_local.py preflight` (or `Test-BMO.ps1 -Mode preflight`). In
order, it checks:

  1. the machine: Python, Node.js (only the web UI needs it), the engine
     binary, the model file, free disk, memory and, on Windows, power;
  2. the engine, for real: start it with the model (token on stdin, exactly
     as every launcher does), then /healthz, /readyz, /version, /build-info,
     one tiny chat reply within a time budget, and the /metrics fields the
     tools read (n_threads, context_tokens, snapshot_tokens);
  3. the web UI host: if Node is usable, start `lae-host.mjs` in fixture mode
     (no engine, no model) and check its /healthz.

Each check prints one PASS / WARN / FAIL line, with a fix for anything that
is not a PASS. A WARN never stops the demo; a FAIL does, and the exit code is
then 1. The JSON summary goes to stdout and to a receipt in local/out/. Like
every receipt here it holds names, statuses and timings: never the prompt,
never the reply, never the engine token.

Everything the checks ask the machine goes through `Probes`, so each platform
branch is testable on any platform.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import bmo_app  # noqa: E402
import bmo_chat  # noqa: E402
import bmo_local  # noqa: E402

MIN_PYTHON = (3, 10)
GIB = 1024 ** 3
# The engine writes its log and receipts to local/out/ (kilobytes), but
# Windows also needs room on the system drive to page when memory is tight.
DISK_FAIL_BYTES = 1 * GIB
DISK_WARN_BYTES = 5 * GIB
# The model is 5.6 GB and is kept resident, plus the context and the OS.
RAM_TOTAL_FAIL_BYTES = 8 * GIB
RAM_TOTAL_WARN_BYTES = 16 * GIB
RAM_AVAILABLE_WARN_BYTES = 7 * GIB
TINY_PROMPT = [{"role": "user", "content": "Reply with the single word: ready"}]
TINY_REPLY_TOKENS = 16
RUNTIME_FIELDS = ("n_threads", "context_tokens", "snapshot_tokens")

# `powercfg /getactivescheme` prints the plan's GUID and a localized name; the
# GUIDs of the built-in plans are the same on every Windows install.
POWER_SCHEMES = {
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": ("High performance", True),
    "e9a42b02-d5df-448d-aa00-03f14749eb61": ("Ultimate Performance", True),
    "381b4222-f694-41f0-9685-ff5bb260df2e": ("Balanced", False),
    "a1841308-3541-4fab-bc81-f71556f20b4a": ("Power saver", False),
}
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


# --- what the machine is asked ---------------------------------------------

class _MemoryStatusEx(ctypes.Structure):
    # MEMORYSTATUSEX. Fixed-width types, so the layout is Windows' on any OS.
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_uint8), ("BatteryFlag", ctypes.c_uint8),
                ("BatteryLifePercent", ctypes.c_uint8), ("SystemStatusFlag", ctypes.c_uint8),
                ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]


def _kernel32():
    return ctypes.WinDLL("kernel32")  # only reached on Windows


def windows_memory(kernel32=None) -> tuple[int, int | None] | None:
    kernel32 = kernel32 or _kernel32()
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def posix_memory(sysconf: Callable[[str], int] = getattr(os, "sysconf", None),
                 meminfo: Path = Path("/proc/meminfo")) -> tuple[int, int | None] | None:
    try:
        page = sysconf("SC_PAGE_SIZE")
        total = page * sysconf("SC_PHYS_PAGES")
    except (TypeError, ValueError, OSError):
        return None
    available = None
    try:
        # Linux: MemAvailable counts reclaimable cache, which free pages do not.
        match = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo.read_text(encoding="ascii"), re.M)
        available = int(match.group(1)) * 1024 if match else None
    except (OSError, ValueError):
        pass
    if available is None:
        try:
            available = page * sysconf("SC_AVPHYS_PAGES")
        except (ValueError, OSError):
            available = None  # macOS has no such counter
    return total, available


def windows_on_battery(kernel32=None) -> bool | None:
    kernel32 = kernel32 or _kernel32()
    status = _SystemPowerStatus()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    # 0 offline, 1 online, 255 unknown; BatteryFlag 128 means no battery at all.
    if status.BatteryFlag == 128 or status.ACLineStatus not in (0, 1):
        return None
    return status.ACLineStatus == 0


class Probes:
    """Every question the checks ask the machine. Tests replace methods."""

    def __init__(self):
        self.system = platform.system()  # "Windows", "Darwin", "Linux"

    def python_version(self) -> tuple[int, ...]:
        return tuple(sys.version_info[:3])

    def node_on_path(self) -> str | None:
        return bmo_app.node_on_path()  # never the current directory; see there

    def run(self, cmd: list[str], timeout: float) -> tuple[int, str]:
        done = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout

    def file_size(self, path: Path) -> int | None:
        return path.stat().st_size if path.is_file() else None

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()

    def disk_free(self, path: Path) -> int:
        while not path.exists() and path != path.parent:
            path = path.parent  # local/out/ may not exist yet
        return shutil.disk_usage(path).free

    def memory(self) -> tuple[int, int | None] | None:
        return windows_memory() if self.system == "Windows" else posix_memory()

    def on_battery(self) -> bool | None:
        return windows_on_battery() if self.system == "Windows" else None

    def power_scheme(self) -> str | None:
        if self.system != "Windows":
            return None
        try:
            code, out = self.run(["powercfg", "/getactivescheme"], timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return out if code == 0 else None


# --- the checks -------------------------------------------------------------

def _gib(n: int) -> str:
    return f"{n / GIB:.1f} GiB"


def check_python(probes: Probes) -> Check:
    version = probes.python_version()
    shown = ".".join(map(str, version))
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check("python", PASS, shown)
    return Check("python", FAIL, f"{shown} is too old",
                 "install Python 3.10 or newer from python.org (tick 'Add python.exe to PATH'), reopen PowerShell")


def check_node(probes: Probes, explicit: str | None) -> tuple[Check, str | None]:
    """Returns the check and the node to start the host with (None: do not)."""
    lo, hi = bmo_app.NODE_MAJOR_RANGE
    fix = (f"install Node.js {lo} or {hi} (or unzip the portable zip from nodejs.org and pass its node.exe "
           "with --node / -NodePath). Chat mode does not need Node.")
    node = str(Path(explicit).resolve()) if explicit else probes.node_on_path()
    if not node:
        return Check("node", WARN, "not found on PATH; the web UI (-Mode app) needs it, chat mode does not",
                     fix), None
    if not Path(node).is_file():
        return Check("node", FAIL, f"no node at {node}", fix), None
    try:
        code, out = probes.run([node, "--version"], timeout=30)
        version = out.strip()
        major = int(version.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Check("node", FAIL, f"could not run {node}: {type(exc).__name__}", fix), None
    if code != 0 or not lo <= major <= hi:
        return Check("node", FAIL, f"{node} is {version or 'unknown'}; the web UI needs {lo} or {hi}", fix), None
    return Check("node", PASS, f"{version} ({node})"), node


def check_engine_binary(probes: Probes, engine: Path) -> Check:
    if not engine.is_file():
        return Check("engine binary", FAIL, f"not found: {engine}",
                     "run .\\local\\windows\\Build-Engine.ps1, or copy the prebuilt lae-engine.exe to local\\bin\\")
    try:
        # A minute, not seconds: antivirus scans a new exe on its first run.
        code, out = probes.run([str(engine), "version"], timeout=60)
    except subprocess.TimeoutExpired:
        return Check("engine binary", FAIL, "`lae-engine version` did not finish within 60 s",
                     "antivirus may be holding the new exe: check Windows Security > Protection history, retry")
    except OSError as exc:
        return Check("engine binary", FAIL, f"could not run it: {exc}",
                     "if Windows Security quarantined or blocked lae-engine.exe, restore or allow it; "
                     "a copy from a download may need: Unblock-File <path to lae-engine.exe>")
    if code != 0:
        # STATUS_DLL_NOT_FOUND, as Windows reports it unsigned or signed.
        dll = code in (0xC0000135, -0x3FFFFECB)
        return Check("engine binary", FAIL, f"`lae-engine version` exited with {code}"
                     + (" (a DLL it needs is missing)" if dll else ""),
                     "use the statically linked prebuilt engine, or rebuild with Build-Engine.ps1")
    return Check("engine binary", PASS, f"lae-engine {out.strip()[:40]} ({engine})")


def check_model(probes: Probes, model: Path, verify_hash: bool) -> list[Check]:
    size = probes.file_size(model)
    fix = (f"copy Qwen3.5-9B-Q4_K_M.gguf again ({bmo_local.MODEL_SIZE:,} bytes, SHA-256 "
           f"{bmo_local.MODEL_SHA256}) to a local disk, not a USB stick or network share")
    if size is None:
        return [Check("model file", FAIL, f"not found: {model}", fix)]
    if size != bmo_local.MODEL_SIZE:
        return [Check("model file", FAIL, f"{size:,} bytes, expected {bmo_local.MODEL_SIZE:,}: "
                      "an incomplete copy or the wrong file", fix)]
    checks = [Check("model file", PASS, f"{size:,} bytes ({model})")]
    if verify_hash:
        digest = probes.sha256(model)
        checks.append(Check("model sha256", PASS, digest) if digest == bmo_local.MODEL_SHA256
                      else Check("model sha256", FAIL, f"{digest} does not match", fix))
    return checks


def check_disk(probes: Probes, path: Path) -> Check:
    try:
        free = probes.disk_free(path)
    except OSError as exc:
        return Check("disk", WARN, f"could not measure free space: {exc}")
    fix = "free some disk space (Settings > System > Storage)"
    if free < DISK_FAIL_BYTES:
        return Check("disk", FAIL, f"{_gib(free)} free", fix)
    if free < DISK_WARN_BYTES:
        return Check("disk", WARN, f"{_gib(free)} free; Windows needs room to page when memory is tight", fix)
    return Check("disk", PASS, f"{_gib(free)} free")


def check_memory(probes: Probes) -> Check:
    try:
        measured = probes.memory()
    except (OSError, AttributeError, ValueError) as exc:
        measured, error = None, f": {type(exc).__name__}"
    else:
        error = ""
    if not measured:
        return Check("memory", WARN, f"could not measure memory{error}")
    total, available = measured
    shown = f"{_gib(total)} total" + (f", {_gib(available)} available" if available is not None else "")
    if total < RAM_TOTAL_FAIL_BYTES:
        return Check("memory", FAIL, f"{shown}; the model alone needs about 6 GiB",
                     "this machine cannot hold the model; use one with 16 GiB or more")
    if total < RAM_TOTAL_WARN_BYTES:
        return Check("memory", WARN, f"{shown}; expect slow replies", "close other programs before the demo")
    if available is not None and available < RAM_AVAILABLE_WARN_BYTES:
        return Check("memory", WARN, f"{shown}; the model needs about 6 GiB free or it will page and crawl",
                     "close browsers, IDEs and other heavy programs before the demo")
    return Check("memory", PASS, shown)


def check_power(probes: Probes) -> list[Check]:
    """Windows only, WARN at worst: a laptop on battery or a power-saving plan
    runs its cores slowly, which is the difference between seconds and minutes."""
    if probes.system != "Windows":
        return []
    checks = []
    try:
        battery = probes.on_battery()
    except (OSError, AttributeError, ValueError):
        battery = None
    if battery is True:
        checks.append(Check("power source", WARN, "running on battery",
                            "plug in the charger: on battery Windows slows the CPU"))
    elif battery is False:
        checks.append(Check("power source", PASS, "plugged in"))
    try:
        scheme = probes.power_scheme()
    except (OSError, ValueError):
        scheme = None
    match = GUID.search(scheme or "")
    if match:
        guid = match.group(0).lower()
        named = re.search(r"\(([^)]*)\)", scheme)
        name, fast = POWER_SCHEMES.get(guid, (named.group(1) if named else guid, False))
        if fast:
            checks.append(Check("power plan", PASS, name))
        else:
            checks.append(Check("power plan", WARN, f"'{name}', not High performance",
                                "Settings > System > Power & battery > Power mode: Best performance "
                                "(or: powercfg /setactive SCHEME_MIN, where that plan exists)"))
    return checks


def _engine_checks(eng, args: argparse.Namespace) -> list[Check]:
    """Everything that needs the running engine. The reply itself is never kept."""
    checks = []
    status, body = eng.get("/healthz", auth=False)
    checks.append(Check("engine /healthz", PASS if status == 200 else FAIL, f"HTTP {status}"))
    status, body = eng.get("/readyz")
    checks.append(Check("engine /readyz", PASS if status == 200 and body.get("ready") is True else FAIL,
                        f"HTTP {status}", "" if status == 200 else "see the engine log in local\\out\\"))
    status, body = eng.get("/version")
    version = body.get("engine_version")
    checks.append(Check("engine /version", PASS if status == 200 and isinstance(version, str) else FAIL,
                        f"HTTP {status}, engine {version}"))
    status, body = eng.get("/build-info")
    model, backend = body.get("model"), body.get("backend")
    wanted_backend = isinstance(backend, str) and backend.rsplit("/", 1)[-1] == args.backend
    ok = status == 200 and model == bmo_chat.MODEL_ID and wanted_backend
    checks.append(Check("engine /build-info", PASS if ok else FAIL,
                        f"model {model}, backend {backend} (asked for {bmo_chat.MODEL_ID} on {args.backend})",
                        "" if ok else "the engine is not the build or backend you meant: check -EnginePath / -Backend"))

    started = time.monotonic()
    try:
        session = bmo_local._new_session(eng)
        reply, prompt_tokens, written, seconds = bmo_local._chat(eng, session, TINY_PROMPT, TINY_REPLY_TOKENS,
                                                                 args.chat_budget)
    except urllib.error.HTTPError as exc:
        checks.append(Check("tiny chat reply", FAIL, f"HTTP {exc.code}", "see the engine log in local\\out\\"))
    except (OSError, ValueError, KeyError, IndexError) as exc:
        waited = time.monotonic() - started
        timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc)
        checks.append(Check("tiny chat reply", FAIL,
                            f"no reply within {args.chat_budget:g} s" if timed_out
                            else f"{type(exc).__name__} after {waited:.1f} s",
                            "too slow for a demo: plug in, close other programs, try -Threads (README 'If it is "
                            "slow') or -Backend intel-vulkan" if timed_out else "see the engine log in local\\out\\"))
    else:
        # Lengths and counts only: the text the model wrote goes nowhere.
        detail = f"{seconds:.1f} s, {prompt_tokens} prompt tokens, {written} written"
        if seconds > args.chat_budget:
            checks.append(Check("tiny chat reply", FAIL, f"{detail}; over the {args.chat_budget:g} s budget",
                                "too slow for a demo: see README 'If it is slow'"))
        elif not reply.strip():
            checks.append(Check("tiny chat reply", WARN, f"{detail}; the reply was empty"))
        else:
            checks.append(Check("tiny chat reply", PASS, detail))

    status, body = eng.get("/metrics")
    runtime = body.get("runtime") if isinstance(body.get("runtime"), dict) else {}
    missing = [f for f in RUNTIME_FIELDS if not isinstance(runtime.get(f), int)]
    if status != 200 or missing:
        checks.append(Check("engine /metrics", FAIL, f"HTTP {status}; missing {', '.join(missing) or '-'}",
                            "this engine is older than the tools: rebuild it (Build-Engine.ps1) or copy the "
                            "current prebuilt lae-engine.exe"))
    else:
        detail = (f"context {runtime['context_tokens']}, threads {runtime['n_threads']}/"
                  f"{runtime.get('n_threads_batch')}, snapshot {runtime['snapshot_tokens']} tokens")
        if runtime["context_tokens"] != args.context:
            checks.append(Check("engine /metrics", WARN, f"{detail}; asked for context {args.context}"))
        else:
            checks.append(Check("engine /metrics", PASS, detail))
    return checks


def _read_line(stream, timeout: float) -> bytes:
    line: list[bytes] = []
    reader = threading.Thread(target=lambda: line.append(stream.readline()), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while reader.is_alive() and time.monotonic() < deadline:
        reader.join(0.25)  # short joins keep Ctrl+C responsive on Windows
    return line[0] if line else b""


def check_host(node: str, timeout: float = 60) -> Check:
    """Start the web UI host in fixture mode (no engine) and ask its /healthz."""
    # As bmo_app starts it, minus anything that would point it at an engine
    # (an inherited LAE_* setting could make the fixture host refuse to start)
    # or load code into it (NODE_OPTIONS).
    env = bmo_app.inheritable_environment(os.environ)
    env.update({"LAE_ENGINE_MODE": "fixture", "LAE_PORT": "0"})  # 0: the OS picks a free port
    try:
        host = subprocess.Popen(bmo_app.host_command(node), cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return Check("web UI host", FAIL, f"could not start node: {exc}")
    result = None
    try:
        raw = _read_line(host.stdout, timeout)
        try:
            ready = json.loads(raw)
            port = int(ready["port"])
            if ready.get("ready") is not True:
                raise ValueError("not ready")
        except (ValueError, KeyError, TypeError):
            port = None
        if port is not None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=10) as resp:
                    ok = resp.status == 200 and json.loads(resp.read(4096)).get("ok") is True
                result = Check("web UI host", PASS if ok else FAIL, f"fixture mode, /healthz on port {port}")
            except (OSError, ValueError) as exc:
                result = Check("web UI host", FAIL, f"/healthz on port {port}: {type(exc).__name__}")
    finally:
        bmo_app.stop_host(host, interrupted=False, grace=10)
        # Fixture mode handles no prompts, so its error output is safe to show.
        error = host.stderr.read(8192).decode("utf-8", "replace").strip() if result is None else ""
        host.stdout.close()
        host.stderr.close()
    if result is None:
        last = error.splitlines()[-1][:300] if error else f"exit code {host.returncode}, no output"
        result = Check("web UI host", FAIL, f"it did not start: {last}",
                       "check the Node version above; run `node lae-host.mjs` in the repo to see the whole error")
    return result


def run(args: argparse.Namespace, probes: Probes | None = None,
        engine_factory: Callable = bmo_local.Engine, out_dir: Path | None = None) -> int:
    probes = probes or Probes()
    out_dir = out_dir or ROOT / "local" / "out"
    started_at = bmo_local._utc()
    checks: list[Check] = []

    def add(*new: Check) -> None:
        for check in new:
            checks.append(check)
            print(f"{check.status}  {check.name:<20} {check.detail}", file=sys.stderr, flush=True)
            if check.fix and check.status in (WARN, FAIL):
                print(f"      fix: {check.fix}", file=sys.stderr, flush=True)

    engine_path, model_path = Path(args.engine).resolve(), Path(args.model).resolve()
    add(check_python(probes))
    node_check, node = check_node(probes, args.node)
    add(node_check)
    add(check_engine_binary(probes, engine_path))
    add(*check_model(probes, model_path, args.verify_hash))
    add(check_disk(probes, out_dir))
    add(check_memory(probes))
    add(*check_power(probes))

    if any(c.status == FAIL for c in checks if c.name in ("engine binary", "model file", "model sha256")):
        add(Check("engine start", SKIP, "fix the engine binary / model first"))
    else:
        print("starting the engine and loading the model (this can take a minute)...", file=sys.stderr, flush=True)
        eng = engine_factory(args)
        try:
            try:
                eng.start()
            except SystemExit as exc:  # Engine.start reports its refusals this way
                add(Check("engine start", FAIL, str(exc.code), f"see the engine log: {eng.log_path}"))
            else:
                add(Check("engine start", PASS, f"model loaded in {eng.ready_seconds} s"))
                add(*_engine_checks(eng, args))
        finally:
            eng.stop()

    if node:
        add(check_host(node))
    else:
        add(Check("web UI host", SKIP, "no usable Node.js; chat mode does not need it"))

    counts = {s: sum(c.status == s for c in checks) for s in (PASS, WARN, FAIL, SKIP)}
    passed = counts[FAIL] == 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    receipt_path = out_dir / f"preflight-{args.backend}-{stamp}.json"
    summary = {"schema": "local_bmo.preflight.v1", "started_at_utc": started_at, "host": bmo_local._host(),
               "backend": args.backend, "context": args.context, "prompt_response_logging": False,
               "checks": [asdict(c) for c in checks], "counts": counts, "passed": passed,
               "receipt": str(receipt_path)}
    text = json.dumps(summary, indent=2, sort_keys=True)
    print(text)
    out_dir.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(text + "\n", encoding="utf-8")
    verdict = "READY for the demo" if passed else f"NOT READY: {counts[FAIL]} check(s) failed"
    print(f"\n{verdict} ({counts[WARN]} warning(s)); receipt: {receipt_path}", file=sys.stderr)
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    return bmo_local.main(["preflight", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
