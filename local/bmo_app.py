#!/usr/bin/env python3
"""Start the engine and the assistant host together, and open the web UI on the real model.

  1. `lae-engine serve --port 0 --token-stdin`, its token generated here and
     written to its stdin (`bmo_local.Engine`).
  2. `node lae-host.mjs` in native-engine mode. The host reads the engine's
     address and token from its environment (`LAE_ENGINE_*`, see
     `createHostComposition` in `lae-host.mjs`): never a command-line argument,
     never a file. Only the host process receives that environment.
  3. The host prints a one-time UI link (`LAE_REVEAL_BOOTSTRAP_URL=1`). It is
     good for one page load within 3 minutes; it is opened in the default
     browser and printed in case that fails.

Ctrl+C stops both. If either one exits, the other is stopped too.

Needs Node.js 24 or 25 (`package.json` engines). Without Node, use
`bmo_chat.py`, which needs only Python.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import bmo_chat  # noqa: E402

NODE_MAJOR_RANGE = (24, 25)  # package.json: "node": ">=24 <26"
HOST_SCRIPT = ROOT / "lae-host.mjs"
BOOTSTRAP_URL = re.compile(r"^http://127\.0\.0\.1:\d{1,5}/#bootstrap=[A-Za-z0-9_-]{43}$")
# The host refuses a per-turn engine timeout above 120 s (`host/agent/config.mjs`).
HOST_ENGINE_TIMEOUT_MS = 120000

NO_NODE = ("Node.js {want} is needed for the web UI and was not found{detail}.\n"
           "Without it, talk to the model in this terminal instead (Python only):\n"
           "  python local/bmo_chat.py --engine <lae-engine> --model <gguf>\n"
           "  or on Windows: .\\local\\windows\\Start-BMO.ps1 -ModelPath <gguf> -Mode chat\n"
           "A portable Node (the zip from nodejs.org, no installer) works too: pass --node <path to node>.")


def node_on_path(path: str | None = None, cwd: str | None = None, windows: bool = os.name == "nt") -> str | None:
    """The first node in an absolute PATH directory that is not the current directory.

    Not `shutil.which`: on Windows it looks in the current directory before
    PATH, so a node.exe planted in whatever folder the launcher was started
    from would run with the engine's token in its environment. Empty, `.` and
    other relative PATH entries name the current directory too, so they are
    skipped as well.
    """
    here = os.path.normcase(os.path.realpath(cwd or os.getcwd()))
    name = "node.exe" if windows else "node"
    entries = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    for entry in entries:
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        if os.path.normcase(os.path.realpath(entry)) == here:
            continue
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
            return candidate
    return None


def find_node(explicit: str | None = None) -> str:
    want = f"{NODE_MAJOR_RANGE[0]}-{NODE_MAJOR_RANGE[1]}"
    # An explicit --node is the operator's own choice; it is made absolute so
    # what runs does not depend on the working directory.
    node = str(Path(explicit).resolve()) if explicit else node_on_path()
    if not node or not Path(node).is_file():
        sys.exit(NO_NODE.format(want=want, detail=f" at {explicit}" if explicit else " on PATH"))
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        major = int(version.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit(NO_NODE.format(want=want, detail=f" (could not run {node})"))
    if not NODE_MAJOR_RANGE[0] <= major <= NODE_MAJOR_RANGE[1]:
        sys.exit(NO_NODE.format(want=want, detail=f" ({node} is {version})"))
    return node


def inheritable_environment(base: dict) -> dict:
    """The caller's environment without LAE_* (ours to set) and NODE_* settings.

    NODE_OPTIONS can load arbitrary code into the host (`--require x.js`),
    which would then hold the engine token; NODE_PATH redirects module
    lookups; NODE_TLS_REJECT_UNAUTHORIZED / NODE_EXTRA_CA_CERTS change whom the
    host trusts. The host needs none of them, so none are inherited.
    """
    return {k: v for k, v in base.items() if not k.upper().startswith(("LAE_", "NODE_"))}


def host_command(node: str) -> list[str]:
    # Nothing secret goes here: a command line is visible to every process on
    # the machine (Task Manager, `ps`).
    return [node, str(HOST_SCRIPT)]


def host_environment(base: dict, *, port: int, token: str, model: str, backend: str,
                     config: str | None = None, delegate: bool = False) -> dict:
    """The host's environment: the caller's, minus any stray LAE_* or NODE_* setting, plus ours.

    An inherited LAE_ENGINE_MODE=fixture or LAE_CONFIG_PATH would make the host
    refuse to start or quietly talk to a canned engine, so none are inherited.
    """
    env = inheritable_environment(base)
    env.update({
        "LAE_ENGINE_MODE": "native",
        "LAE_ENGINE_ENDPOINT": f"http://127.0.0.1:{port}",
        "LAE_ENGINE_TOKEN": token,
        # The host checks both against the engine's /build-info and refuses a
        # mismatch, so they are taken from the engine itself.
        "LAE_ENGINE_MODEL": model,
        "LAE_ENGINE_BACKEND": backend,
        "LAE_ENGINE_TIMEOUT_MS": str(HOST_ENGINE_TIMEOUT_MS),
        "LAE_REVEAL_BOOTSTRAP_URL": "1",
    })
    if config:
        env["LAE_CONFIG_PATH"] = str(Path(config).resolve())
    # Jobs from a coding assistant only on the operator's explicit --delegate:
    # an inherited LAE_DELEGATE_ENABLED was already dropped above. With it on,
    # the host writes host.json (its port, no secret) to the per-user state
    # folder so the MCP bridge can find it.
    if delegate:
        env["LAE_DELEGATE_ENABLED"] = "1"
    return env


def engine_identity(eng) -> tuple[str, str]:
    status, build = eng.get("/build-info")
    model, backend = build.get("model"), build.get("backend")
    if status != 200 or not isinstance(model, str) or not isinstance(backend, str):
        raise RuntimeError(f"engine /build-info answered HTTP {status} without model/backend")
    return model, backend


def start_host(node: str, env: dict) -> subprocess.Popen:
    # Same console and process group as this script, on purpose: Ctrl+C then
    # reaches the host directly and it shuts down its own way (cancel the
    # active turn, delete its engine sessions) while the engine, which is
    # outside the group, is still there to answer.
    return subprocess.Popen(host_command(node), cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=None)


def read_bootstrap_url(host: subprocess.Popen, timeout: float) -> str | None:
    line: list[bytes] = []
    reader = threading.Thread(target=lambda: line.append(host.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    text = line[0].decode("utf-8", "replace").strip() if line and line[0] else ""
    return text if BOOTSTRAP_URL.match(text) else None


def forward_host_output(host: subprocess.Popen) -> None:
    # Keep draining the pipe so a chatty host can never block on a full one.
    def pump():
        for raw in iter(host.stdout.readline, b""):
            print(f"[host] {raw.decode('utf-8', 'replace').rstrip()}", file=sys.stderr)
    threading.Thread(target=pump, daemon=True).start()


def supervise(host: subprocess.Popen, engine: subprocess.Popen, poll: float = 0.5) -> str:
    """Block until one process exits or Ctrl+C. Returns which happened."""
    try:
        while True:
            if host.poll() is not None:
                return "host exited"
            if engine.poll() is not None:
                return "engine exited"
            time.sleep(poll)
    except KeyboardInterrupt:
        return "interrupted"


def stop_host(host: subprocess.Popen, interrupted: bool, grace: float = 10.0) -> None:
    if host.poll() is not None:
        return
    if interrupted:
        # The host saw the same Ctrl+C and is already closing.
        try:
            host.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
    if os.name == "nt":
        host.terminate()
    else:
        host.send_signal(signal.SIGTERM)  # the host's graceful-shutdown signal
    try:
        host.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        host.kill()
        host.wait()


def run(eng, args: argparse.Namespace, node: str) -> int:
    model, backend = engine_identity(eng)
    env = host_environment(os.environ, port=eng.port, token=eng.token, model=model,
                           backend=backend, config=args.host_config, delegate=args.delegate)
    host = start_host(node, env)
    interrupted = False
    try:
        url = read_bootstrap_url(host, args.host_timeout)
        if not url:
            print("the host did not start; its error is above. The engine is being stopped.", file=sys.stderr)
            return 1
        forward_host_output(host)
        print(f"\nBMO is running on {backend}.\n  open: {url}\n"
              "  (that link works once, within 3 minutes; to open the UI again later, restart with Ctrl+C)\n"
              "  Ctrl+C here stops everything.", file=sys.stderr)
        if args.delegate:
            print("  coding-assistant jobs: ON. Each one waits for your Approve on the BMO page.\n"
                  "  The key to paste into the coding assistant: python local/bmo_local.py delegate-key",
                  file=sys.stderr)
        if not args.no_browser:
            webbrowser.open(url)
        outcome = supervise(host, eng.proc)
        interrupted = outcome == "interrupted"
        if not interrupted:
            print(f"{outcome}; stopping.", file=sys.stderr)
        return 0 if interrupted else 1
    except KeyboardInterrupt:
        interrupted = True
        return 0
    finally:
        stop_host(host, interrupted)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    bmo_chat.add_engine_args(p, "engine-app.log")
    p.add_argument("--node", help="path to node (default: the one on PATH)")
    p.add_argument("--host-config", help="host config JSON (workspace roots, providers); default: none")
    p.add_argument("--host-timeout", type=float, default=60,
                   help="seconds to wait for the host to come up")
    p.add_argument("--no-browser", action="store_true", help="print the UI link but do not open it")
    p.add_argument("--delegate", action="store_true",
                   help="accept read-only jobs from a coding assistant (the BMO MCP bridge); each "
                        "job still needs your Approve on the BMO page")
    return p


def main(argv: list[str] | None = None) -> int:
    bmo_chat._console_safe_output()
    args = build_parser().parse_args(argv)
    bmo_chat.exit_on_termination()
    node = find_node(args.node)  # before the minute-long model load, not after
    eng = bmo_chat.ChatEngine(args)
    print("starting the engine and loading the model (this can take a minute)...", file=sys.stderr)
    try:
        eng.start()
    except KeyboardInterrupt:
        return 130  # Ctrl+C during the model load; start() has stopped the engine
    try:
        return run(eng, args, node)
    finally:
        eng.stop()


if __name__ == "__main__":
    raise SystemExit(main())
