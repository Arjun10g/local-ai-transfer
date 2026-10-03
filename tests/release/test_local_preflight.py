"""The demo pre-flight: `local/bmo_preflight.py` (`bmo_local.py preflight`).

Runs offline. The engine is a real child process (a stand-in executable)
that reports ready on an in-process stand-in of the engine's HTTP API; Node
is either a stand-in that serves /healthz or, when installed, the real host
in fixture mode. Every platform probe (memory, battery, power plan, disk) is
injected, so the Windows branches run on any OS.

What is pinned: every check prints one PASS/WARN/FAIL line with a fix; any
FAIL exits 1; a missing Node or a slow power plan is only a WARN; the engine
is stopped afterwards; and neither the summary nor the receipt (written under
the receipt folder only) holds the prompt, the reply or the token.
"""

import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "local"))
import bmo_local  # noqa: E402
import bmo_preflight as pf  # noqa: E402
from tests.release.test_local_chat_and_app import (  # noqa: E402
    TOKEN, FakeEngine, _node_ok, reap_engine, write_fake_engine)

GIB = 1024 ** 3
SECRET_REPLY = "PREFLIGHT-REPLY-NEVER-KEPT-5be1"

FAKE_NODE_PY = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
if sys.argv[1:2] == ["--version"]:
    print("v25.1.0")
    raise SystemExit(0)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        body = json.dumps({"ok": self.path == "/healthz"}).encode()
        self.send_response(200 if self.path == "/healthz" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = HTTPServer(("127.0.0.1", 0), H)
print(json.dumps({"ready": True, "host": "127.0.0.1", "port": server.server_address[1], "engine": "fixture"}),
      flush=True)
server.serve_forever()
"""
BROKEN_NODE_PY = """
import sys
if sys.argv[1:2] == ["--version"]:
    print("v25.1.0")
    raise SystemExit(0)
sys.stderr.write("Error: config_load_failed: planted failure\\n")
raise SystemExit(1)
"""


class FakeProbes(pf.Probes):
    """A healthy Windows laptop on mains power with the High performance plan."""

    def __init__(self, node=None):
        super().__init__()
        self.system = "Windows"
        self.node = node
        self.size = bmo_local.MODEL_SIZE
        self.digest = bmo_local.MODEL_SHA256
        self.free = 100 * GIB
        self.mem = (32 * GIB, 20 * GIB)
        self.battery = False
        self.scheme = ("Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (High performance)")
        self.version = (3, 12, 4)

    def python_version(self):
        return self.version

    def node_on_path(self):
        return self.node

    def file_size(self, path):
        # The real file is a few bytes; a 5.6 GB stand-in is not worth the disk.
        return self.size if path.is_file() else None

    def sha256(self, path):
        return self.digest

    def disk_free(self, path):
        return self.free

    def memory(self):
        return self.mem

    def on_battery(self):
        return self.battery

    def power_scheme(self):
        return self.scheme


@unittest.skipIf(os.name == "nt", "the stand-in engine and node are shell scripts")
class PreflightRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "out"
        self.engine = FakeEngine()
        self.addCleanup(self.engine.close)
        self.engine.default_reply = [SECRET_REPLY]
        self.exe = write_fake_engine(self.dir)
        self.model = self.dir / "Qwen3.5-9B-Q4_K_M.gguf"
        self.model.write_bytes(b"gguf")
        self.node = str(write_fake_engine(self.dir, "node", FAKE_NODE_PY))
        env = mock.patch.dict(os.environ, {"FAKE_ENGINE_PORT": str(self.engine.port)})
        env.start()
        self.addCleanup(env.stop)
        # The stand-in HTTP engine knows one token; hand the launcher that one.
        token = mock.patch.object(bmo_local.secrets, "token_urlsafe", return_value=TOKEN)
        token.start()
        self.addCleanup(token.stop)
        self.started: list = []

    def factory(self, args):
        eng = bmo_local.Engine(args)
        self.started.append(eng)
        self.addCleanup(reap_engine, eng)
        return eng

    def run_preflight(self, probes=None, *extra):
        args = bmo_local.build_parser().parse_args(
            ["preflight", "--engine", str(self.exe), "--model", str(self.model),
             "--log", str(self.dir / "engine.log"), "--ready-timeout", "30", *extra])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = pf.run(args, probes or FakeProbes(self.node), engine_factory=self.factory, out_dir=self.out)
        self.summary = json.loads(stdout.getvalue())
        self.stderr = stderr.getvalue()
        self.status = {c["name"]: c["status"] for c in self.summary["checks"]}
        return code

    def assert_nothing_private(self, *texts):
        for text in texts:
            self.assertNotIn(SECRET_REPLY, text)
            self.assertNotIn("Reply with the single word", text)
            self.assertNotIn(TOKEN, text)

    def test_a_ready_machine_passes_every_check_in_order(self):
        self.assertEqual(self.run_preflight(None, "--verify-hash"), 0, self.stderr)
        self.assertEqual(list(self.status), [
            "python", "node", "engine binary", "model file", "model sha256", "disk", "memory",
            "power source", "power plan", "engine start", "engine /healthz", "engine /readyz",
            "engine /version", "engine /build-info", "tiny chat reply", "engine /metrics", "web UI host"])
        self.assertEqual(set(self.status.values()), {"PASS"}, self.stderr)
        self.assertIs(self.summary["passed"], True)
        self.assertEqual(self.engine.requests[0]["max_tokens"], pf.TINY_REPLY_TOKENS)
        for name in self.status:
            self.assertRegex(self.stderr, rf"(?m)^PASS  {name}")
        self.assertIn("READY for the demo", self.stderr)
        self.assertIsNotNone(self.started[0].proc.poll(), "the engine must be stopped afterwards")

    def test_the_receipt_is_the_summary_and_holds_no_prompt_reply_or_token(self):
        self.run_preflight()
        receipts = list(self.out.glob("preflight-cpu-*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0].read_text(encoding="utf-8")
        self.assertEqual(json.loads(receipt), self.summary)
        self.assertIs(self.summary["prompt_response_logging"], False)
        self.assert_nothing_private(receipt, self.stderr, json.dumps(self.summary))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.suffix == ".json"), [],
                         "nothing is written outside the receipt folder")

    def test_missing_node_is_only_a_warning(self):
        self.assertEqual(self.run_preflight(FakeProbes(node=None)), 0, self.stderr)
        self.assertEqual((self.status["node"], self.status["web UI host"]), ("WARN", "SKIP"))
        self.assertIn("fix: install Node.js 24 or 25", self.stderr)

    def test_a_node_of_the_wrong_version_fails(self):
        old = write_fake_engine(self.dir, "oldnode", "print('v20.11.0')\n")
        self.assertEqual(self.run_preflight(FakeProbes(node=str(old))), 1)
        self.assertEqual(self.status["node"], "FAIL")
        self.assertIn("NOT READY", self.stderr)

    def test_a_host_that_does_not_start_fails_with_its_own_error(self):
        broken = str(write_fake_engine(self.dir, "brokennode", BROKEN_NODE_PY))
        self.assertEqual(self.run_preflight(FakeProbes(node=broken)), 1)
        self.assertEqual(self.status["web UI host"], "FAIL")
        self.assertIn("config_load_failed: planted failure", self.stderr)

    def test_a_short_model_fails_and_the_engine_is_not_started(self):
        probes = FakeProbes(self.node)
        probes.size = bmo_local.MODEL_SIZE - 1
        self.assertEqual(self.run_preflight(probes), 1)
        self.assertEqual((self.status["model file"], self.status["engine start"]), ("FAIL", "SKIP"))
        self.assertEqual(self.started, [])
        self.assertIn("incomplete copy", self.stderr)

    def test_a_wrong_model_hash_fails(self):
        probes = FakeProbes(self.node)
        probes.digest = "0" * 64
        self.assertEqual(self.run_preflight(probes, "--verify-hash"), 1)
        self.assertEqual(self.status["model sha256"], "FAIL")

    def test_an_engine_built_for_another_backend_fails(self):
        self.engine.build_info = {**self.engine.build_info, "backend": "llama.cpp/3581ba0c/cpu-disabled"}
        self.assertEqual(self.run_preflight(), 1)
        self.assertEqual(self.status["engine /build-info"], "FAIL")

    def test_an_engine_without_the_runtime_fields_the_tools_read_fails(self):
        self.engine.runtime_extra = {"snapshot_tokens": None}
        self.assertEqual(self.run_preflight(), 1)
        self.assertEqual(self.status["engine /metrics"], "FAIL")
        self.assertIn("snapshot_tokens", self.stderr)

    def test_a_reply_slower_than_the_budget_fails(self):
        self.engine.reply_delay = 2.0
        self.assertEqual(self.run_preflight(None, "--chat-budget", "0.3"), 1)
        self.assertEqual(self.status["tiny chat reply"], "FAIL")
        self.assertIn("no reply within 0.3 s", self.stderr)
        self.assertIsNotNone(self.started[0].proc.poll())

    def test_an_engine_that_never_gets_ready_fails_cleanly(self):
        os.environ.pop("FAKE_ENGINE_PORT")
        self.assertEqual(self.run_preflight(None, "--ready-timeout", "0.5"), 1)
        self.assertEqual(self.status["engine start"], "FAIL")
        self.assertNotIn("engine /healthz", self.status)
        self.assertIsNotNone(self.started[0].proc.poll())

    def test_battery_and_a_balanced_plan_warn_but_never_fail(self):
        probes = FakeProbes(self.node)
        probes.battery = True
        probes.scheme = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Ausbalanciert)"
        self.assertEqual(self.run_preflight(probes), 0)
        self.assertEqual((self.status["power source"], self.status["power plan"]), ("WARN", "WARN"))
        self.assertIn("plug in the charger", self.stderr)

    @unittest.skipUnless(_node_ok(), "Node.js 24/25 not available")
    def test_the_real_host_starts_in_fixture_mode(self):
        check = pf.check_host(shutil.which("node"))
        self.assertEqual(check.status, "PASS", check.detail)

    def test_the_host_inherits_no_engine_or_node_settings(self):
        seen = {}
        real_popen = subprocess.Popen

        def spy(*a, **k):
            seen.update(k["env"])
            return real_popen(*a, **k)

        planted = {"NODE_OPTIONS": "--require /nonexistent/evil.js", "LAE_ENGINE_TOKEN": "x" * 43}
        with mock.patch.dict(os.environ, planted), mock.patch.object(pf.subprocess, "Popen", side_effect=spy):
            self.assertEqual(pf.check_host(self.node).status, "PASS")
        self.assertNotIn("NODE_OPTIONS", seen)
        self.assertNotIn("LAE_ENGINE_TOKEN", seen)
        self.assertEqual(seen["LAE_ENGINE_MODE"], "fixture")


class MachineCheckTests(unittest.TestCase):
    def test_python_older_than_3_10_fails(self):
        probes = FakeProbes()
        probes.version = (3, 9, 18)
        self.assertEqual(pf.check_python(probes).status, "FAIL")
        probes.version = (3, 10, 0)
        self.assertEqual(pf.check_python(probes).status, "PASS")

    def test_memory_thresholds(self):
        probes = FakeProbes()
        for mem, want in (((4 * GIB, 3 * GIB), "FAIL"), ((12 * GIB, 8 * GIB), "WARN"),
                          ((32 * GIB, 3 * GIB), "WARN"), ((32 * GIB, 20 * GIB), "PASS"),
                          ((32 * GIB, None), "PASS"), (None, "WARN")):
            probes.mem = mem
            self.assertEqual(pf.check_memory(probes).status, want, mem)

    def test_disk_thresholds(self):
        probes = FakeProbes()
        for free, want in ((GIB // 2, "FAIL"), (3 * GIB, "WARN"), (50 * GIB, "PASS")):
            probes.free = free
            self.assertEqual(pf.check_disk(probes, Path(".")).status, want)

    def test_power_is_checked_only_on_windows_and_never_fails(self):
        probes = FakeProbes()
        probes.system = "Darwin"
        self.assertEqual(pf.check_power(probes), [])
        probes.system = "Windows"
        for battery, scheme in ((None, None), (True, "garbage"), (False, "Power Scheme GUID: "
                                                                  "a1841308-3541-4fab-bc81-f71556f20b4a  (Power saver)")):
            probes.battery, probes.scheme = battery, scheme
            self.assertNotIn("FAIL", [c.status for c in pf.check_power(probes)])
        probes.battery, probes.scheme = None, "Power Scheme GUID: e9a42b02-d5df-448d-aa00-03f14749eb61  (Ultimate)"
        self.assertEqual([(c.name, c.status) for c in pf.check_power(probes)], [("power plan", "PASS")])

    def test_engine_binary_problems_name_their_likely_cause(self):
        probes = FakeProbes()
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "lae-engine.exe"
            self.assertIn("Build-Engine.ps1", pf.check_engine_binary(probes, exe).fix)
            exe.write_bytes(b"")
            with mock.patch.object(probes, "run", side_effect=PermissionError(13, "Access is denied")):
                self.assertIn("Windows Security", pf.check_engine_binary(probes, exe).fix)
            with mock.patch.object(probes, "run", return_value=(0xC0000135, "")):
                self.assertIn("DLL", pf.check_engine_binary(probes, exe).detail)
            with mock.patch.object(probes, "run", return_value=(0, "0.1.0\n")):
                self.assertEqual(pf.check_engine_binary(probes, exe).status, "PASS")


class PlatformProbeTests(unittest.TestCase):
    """The ctypes and sysconf branches, fed by stand-ins so they run anywhere."""

    def test_windows_memory_reads_global_memory_status_ex(self):
        class Kernel32:
            def GlobalMemoryStatusEx(self, ref):  # noqa: N802 - the Win32 name
                status = ref._obj
                assert status.dwLength == ctypes.sizeof(pf._MemoryStatusEx)
                status.ullTotalPhys, status.ullAvailPhys = 32 * GIB, 21 * GIB
                return 1

        self.assertEqual(pf.windows_memory(Kernel32()), (32 * GIB, 21 * GIB))
        failing = mock.Mock()
        failing.GlobalMemoryStatusEx.return_value = 0
        self.assertIsNone(pf.windows_memory(failing))

    def test_windows_battery_reads_system_power_status(self):
        def kernel32(ac, flag):
            class K:
                def GetSystemPowerStatus(self, ref):  # noqa: N802
                    ref._obj.ACLineStatus, ref._obj.BatteryFlag = ac, flag
                    return 1
            return K()

        self.assertIs(pf.windows_on_battery(kernel32(0, 1)), True)
        self.assertIs(pf.windows_on_battery(kernel32(1, 8)), False)
        self.assertIsNone(pf.windows_on_battery(kernel32(1, 128)), "a desktop has no battery")
        self.assertIsNone(pf.windows_on_battery(kernel32(255, 1)))

    def test_posix_memory_prefers_linux_mem_available(self):
        pages = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 4 * 1024 * 1024, "SC_AVPHYS_PAGES": 1}
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp) / "meminfo"
            meminfo.write_text("MemTotal: 16777216 kB\nMemAvailable:    8388608 kB\n", encoding="ascii")
            self.assertEqual(pf.posix_memory(pages.__getitem__, meminfo), (16 * GIB, 8 * GIB))
            self.assertEqual(pf.posix_memory(pages.__getitem__, Path(tmp) / "absent"), (16 * GIB, 4096))

    def test_posix_memory_on_macos_reports_total_only(self):
        def sysconf(name):
            if name == "SC_AVPHYS_PAGES":
                raise ValueError("unrecognized configuration name")
            return {"SC_PAGE_SIZE": 16384, "SC_PHYS_PAGES": 524288}[name]

        self.assertEqual(pf.posix_memory(sysconf, Path("/nonexistent/meminfo")), (8 * GIB, None))
        self.assertIsNone(pf.posix_memory(None))  # Windows has no os.sysconf


class WiringTests(unittest.TestCase):
    def test_bmo_local_routes_preflight(self):
        args = bmo_local.build_parser().parse_args(["preflight", "--engine", "e", "--model", "m", "--verify-hash"])
        self.assertIs(args.verify_hash, True)
        self.assertFalse(hasattr(args, "out"), "the receipt location is fixed under local/out")
        with mock.patch.object(pf, "run", return_value=0) as run:
            self.assertEqual(bmo_local.main(["preflight", "--engine", "e", "--model", "m"]), 0)
        run.assert_called_once()

    def test_the_windows_scripts_offer_preflight(self):
        test = (ROOT / "local" / "windows" / "Test-BMO.ps1").read_text(encoding="utf-8")
        self.assertRegex(test, r"ValidateSet\([^)]*'preflight'")
        self.assertIn("$argv += @($Launcher, 'preflight')", test)
        self.assertIn("if ($VerifyModelHash) { $argv += '--verify-hash' }", test)
        self.assertIn("-Mode preflight", (ROOT / "local" / "windows" / "Start-BMO.ps1").read_text(encoding="utf-8"))
        self.assertIn("-Mode preflight", (ROOT / "local" / "windows" / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
