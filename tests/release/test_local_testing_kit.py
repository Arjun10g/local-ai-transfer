"""The ADR-0007 personal-testing kit: `local/bmo_local.py` and `local/windows/`.

Pins the two properties that matter most for running the engine on a real
laptop: raw model output never reaches a receipt, and the engine's token is
handed over on stdin -- because on Windows the engine refuses `--token-file`
by design (`native/main.cpp`, `read_token_file`), so a launcher that used a
token file would fail on the very machine this kit exists for.
"""

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_local  # noqa: E402

SECRET_OUTPUT = "<tool_call>MODEL-RAW-OUTPUT-MUST-NEVER-BE-PERSISTED-7f3a</tool_call>"


class FakeEngine:
    def __init__(self, args):
        self.token = "t" * 43
        self.port = 1
        self.ready_seconds = 1.0
        self.proc = mock.Mock(pid=4242)

    base = "http://127.0.0.1:1"

    def get(self, path: str, auth: bool = True):
        if path == "/metrics":
            return 200, {"runtime": {"last_reused_prefix_tokens": 2048, "n_threads": 4}}
        return 200, {"backend": "llama.cpp/3581ba0c/cpu", "engine_version": "0.1.0",
                     "llama_cpp_revision": "3581ba0c"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class ReceiptPrivacyTests(unittest.TestCase):
    def test_raw_output_never_reaches_the_receipt_even_when_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "receipt.json"
            argv = ["eval", "--engine", "x", "--model", "y", "--cases", "prod-mail-list-001",
                    "--show-output", "--out", str(out)]
            console, errors = io.StringIO(), io.StringIO()
            with mock.patch.object(bmo_local, "Engine", FakeEngine), \
                    mock.patch.object(bmo_local.ev, "_post", return_value=SECRET_OUTPUT), \
                    redirect_stdout(console), redirect_stderr(errors):
                self.assertEqual(bmo_local.main(argv), 0)
            receipt = out.read_text(encoding="utf-8")
            self.assertNotIn("MODEL-RAW-OUTPUT", receipt)
            self.assertNotIn("MODEL-RAW-OUTPUT", console.getvalue(),
                             "stdout carries the receipt, so it must not carry raw output either")
            # --show-output is allowed to print it, to the console only.
            self.assertIn("MODEL-RAW-OUTPUT", errors.getvalue())
            data = json.loads(receipt)
            self.assertIs(data["prompt_response_logging"], False)
            self.assertEqual(set(data["cases"][0]), {"id", "category", "passed", "reason", "seconds"})

    def test_full_eval_scores_every_case_with_the_long_timeout(self):
        # The evaluator refuses timeouts above its remote ceiling of 600 s and
        # then returns an empty result, so a launcher that did not raise the
        # ceiling would "score" 0 of 0 on the Dell without an obvious error.
        seen = []

        def fake_post(endpoint, token, payload, timeout, include_usage=False):
            seen.append(timeout)
            return ("ready", 5812) if include_usage else SECRET_OUTPUT

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "receipt.json"
            with mock.patch.object(bmo_local, "Engine", FakeEngine), \
                    mock.patch.object(bmo_local.ev, "_post", side_effect=fake_post), \
                    mock.patch.object(bmo_local.ev, "_rss_kib", return_value=None), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bmo_local.main(["eval", "--engine", "x", "--model", "y", "--out", str(out)]), 0)
            receipt = out.read_text(encoding="utf-8")
        data = json.loads(receipt)
        self.assertEqual(data["case_count"], 37)
        self.assertEqual(data["errors"], 0)
        self.assertEqual(set(seen), {1800})
        self.assertNotIn("MODEL-RAW-OUTPUT", receipt)

    def test_bench_receipt_carries_measurements_not_prompts(self):
        # The reading-speed measurement sends a large filler prompt; neither it
        # nor the model's replies may reach the receipt.
        import urllib.request

        def fake_urlopen(req, timeout=None):
            body = req.data.decode() if req.data else ""
            if req.full_url.endswith("/v1/sessions"):
                payload = {"id": "sess-00000001"}
            else:
                self.assertIn("session_id", body)
                payload = {"id": "r", "choices": [{"message": {"role": "assistant",
                           "content": SECRET_OUTPUT}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 2048, "completion_tokens": 64}}
            return mock.MagicMock(__enter__=mock.Mock(return_value=mock.Mock(
                read=mock.Mock(return_value=json.dumps(payload).encode()), status=200)),
                __exit__=mock.Mock(return_value=False))

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bench.json"
            with mock.patch.object(bmo_local, "Engine", FakeEngine), \
                    mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bmo_local.main(["bench", "--engine", "x", "--model", "y",
                                                 "--out", str(out)]), 0)
            receipt = out.read_text(encoding="utf-8")
        self.assertNotIn("MODEL-RAW-OUTPUT", receipt)
        self.assertNotIn("quick brown fox", receipt)
        data = json.loads(receipt)
        self.assertIs(data["prompt_response_logging"], False)
        self.assertEqual(data["decode"]["generated_tokens"], 64)
        self.assertEqual(data["prefill"]["prompt_tokens"], 2048)

    def test_unknown_case_ids_are_refused(self):
        with mock.patch.object(bmo_local, "Engine", FakeEngine), redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            bmo_local.main(["eval", "--engine", "x", "--model", "y", "--cases", "no-such-case"])


class TokenHandoverTests(unittest.TestCase):
    def test_launcher_uses_stdin_and_never_a_token_file(self):
        source = (ROOT / "local" / "bmo_local.py").read_text(encoding="utf-8")
        self.assertIn('"--token-stdin"', source)
        self.assertNotIn('"--token-file"', source)

    def test_generated_token_satisfies_the_engine_rule(self):
        # native/main.cpp valid_bearer_token: 16..512 printable characters.
        engine = bmo_local.Engine(mock.Mock(log="x"))
        self.assertTrue(16 <= len(engine.token) <= 512)
        self.assertTrue(all(0x20 <= ord(c) != 0x7F for c in engine.token))

    def test_windows_scripts_never_pass_a_token_file(self):
        for script in (ROOT / "local" / "windows").glob("*.ps1"):
            with self.subTest(script=script.name):
                self.assertNotIn("--token-file", script.read_text(encoding="utf-8"))

    def _console(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bmo_local, "Engine", FakeEngine), \
                mock.patch.object(bmo_local.ev, "_post", side_effect=lambda *a, include_usage=False, **k:
                                  ("ready", 12) if include_usage else SECRET_OUTPUT), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(bmo_local.main(argv), 0)
        return out.getvalue(), err.getvalue()

    def test_serve_prints_the_endpoint_but_not_the_token(self):
        # A console is screen-shared, recorded and pasted into bug reports.
        out, err = self._console(["serve", "--engine", "x", "--model", "y"])
        self.assertNotIn(FakeEngine(None).token, out + err)
        info = json.loads(out)
        self.assertEqual(info["endpoint"], "http://127.0.0.1:1/v1/chat/completions")
        self.assertNotIn("token", info)
        self.assertIn("--print-token", info["note"])

    def test_serve_prints_the_token_only_when_asked(self):
        out, _ = self._console(["serve", "--engine", "x", "--model", "y", "--print-token"])
        self.assertEqual(json.loads(out)["token"], FakeEngine(None).token)

    def test_no_other_subcommand_prints_the_token(self):
        token = FakeEngine(None).token
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["smoke"], ["eval", "--cases", "prod-mail-list-001", "--show-output"]):
                with self.subTest(command=argv[0]):
                    out, err = self._console([*argv, "--engine", "x", "--model", "y",
                                              "--out", str(Path(tmp) / f"{argv[0]}.json")])
                    self.assertNotIn(token, out + err)
                    self.assertNotIn(token, (Path(tmp) / f"{argv[0]}.json").read_text(encoding="utf-8"))
        source = (ROOT / "local" / "bmo_local.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'"token": eng\.token|info\["token"\] = eng\.token', source),
                         ['info["token"] = eng.token'], "the one place the token is printed is --print-token")

    def test_missing_engine_is_a_clear_refusal(self):
        args = mock.Mock(engine="/nonexistent/lae-engine", model="/nonexistent/m.gguf", log="x")
        with self.assertRaises(SystemExit) as caught:
            bmo_local.Engine(args).start()
        self.assertIn("engine not found", str(caught.exception))


class EngineCommandTests(unittest.TestCase):
    """The exact `lae-engine serve` command line, captured before it runs."""

    def _command(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            engine, model = Path(tmp) / "lae-engine", Path(tmp) / "m.gguf"
            engine.touch()
            model.touch()
            args = bmo_local.build_parser().parse_args(
                ["smoke", "--engine", str(engine), "--model", str(model), "--log", str(Path(tmp) / "e.log"), *extra])
            with mock.patch.object(bmo_local.subprocess, "Popen", side_effect=RuntimeError("captured")) as popen:
                with self.assertRaises(RuntimeError):
                    bmo_local.Engine(args).start()
            return popen.call_args.args[0]

    def test_cpu_passes_no_offload_and_threads_only_when_asked(self):
        # The engine refuses any offload setting on the cpu backend.
        command = self._command()
        self.assertNotIn("--gpu-layers", command)
        self.assertNotIn("--threads", command)
        command = self._command("--threads", "6")
        self.assertEqual(command[command.index("--threads") + 1], "6")

    def test_prefill_threads_are_separate_and_only_passed_when_asked(self):
        self.assertNotIn("--threads-batch", self._command("--threads", "6"))
        command = self._command("--threads", "6", "--threads-batch", "12")
        self.assertEqual(command[command.index("--threads") + 1], "6")
        self.assertEqual(command[command.index("--threads-batch") + 1], "12")

    def test_speculation_is_off_unless_asked_for(self):
        # Experimental and opt-in: a default run must never enable it.
        self.assertNotIn("--speculate", self._command())
        command = self._command("--speculate", "4")
        self.assertEqual(command[command.index("--speculate") + 1], "4")

    def test_intel_vulkan_always_passes_an_offload_the_engine_accepts(self):
        # native/main.cpp refuses intel-vulkan without 1..99 gpu layers.
        command = self._command("--backend", "intel-vulkan", "--vulkan-device-name", "Intel(R) Arc(TM) Graphics")
        self.assertEqual(command[command.index("--gpu-layers") + 1], "99")
        command = self._command("--backend", "intel-vulkan", "--vulkan-device-name", "X", "--gpu-layers", "20")
        self.assertEqual(command[command.index("--gpu-layers") + 1], "20")


class KitShapeTests(unittest.TestCase):
    def test_kit_is_separate_from_the_parked_enterprise_scripts(self):
        # release/windows/ refuses by design until attestation-grade controls
        # exist; the personal kit must not silently replace or call it.
        for script in (ROOT / "local" / "windows").glob("*.ps1"):
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"release[\\/]windows[\\/][A-Za-z-]+\.ps1'?\s*$")
        for stub in ("Build-WindowsBackend.ps1", "Run-WindowsBackend.ps1", "Start-LocalAssistant.ps1"):
            self.assertIn("NOT_READY", (ROOT / "release" / "windows" / stub).read_text(encoding="utf-8"))

    def test_expected_model_identity_matches_the_manifest(self):
        manifest = json.loads((ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json").read_text(encoding="utf-8"))
        blob = json.dumps(manifest)
        self.assertIn(bmo_local.MODEL_SHA256, blob)
        self.assertIn(str(bmo_local.MODEL_SIZE), blob)
        readme = (ROOT / "local" / "windows" / "README.md").read_text(encoding="utf-8")
        self.assertIn(bmo_local.MODEL_SHA256, readme)
        self.assertIn(f"{bmo_local.MODEL_SIZE:,}", readme)

    def test_prebuilt_engine_stands_in_only_for_cpu_and_is_never_committed(self):
        # The prebuilt engine is a MinGW CPU cross-build: it must not be picked
        # for intel-vulkan, and a binary copied into local/bin/ stays out of git.
        text = (ROOT / "local" / "windows" / "Test-BMO.ps1").read_text(encoding="utf-8")
        self.assertIn("$Backend -eq 'cpu' -and (Test-Path -LiteralPath $Prebuilt)", text)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("local/bin/", ignored)

    def test_scripts_are_ascii_so_windows_powershell_reads_them_right(self):
        # Windows PowerShell 5.1 reads a script without a BOM in the ANSI code
        # page: one UTF-8 dash in a string and the script fails to parse.
        for script in (ROOT / "local" / "windows").glob("*.ps1"):
            with self.subTest(script=script.name):
                script.read_bytes().decode("ascii")

    def test_launch_scripts_check_python_and_take_paths_literally(self):
        for name in ("Start-BMO.ps1", "Test-BMO.ps1"):
            text = (ROOT / "local" / "windows" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                # The Store's python.exe placeholder runs nothing; py -3 may be old.
                self.assertIn("print(sys.version_info >= (3, 10))", text)
                self.assertIn("$argv = @() + $PyArgs", text)
                # [ and ] in a folder name are wildcards to a plain -Path.
                self.assertIn("Resolve-Path -LiteralPath $ModelPath", text)
                self.assertNotRegex(text, r"(Resolve-Path|Test-Path|Get-Item) \$")

    def test_start_script_never_takes_node_from_the_current_folder(self):
        text = (ROOT / "local" / "windows" / "Start-BMO.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-Command -Name 'node' -CommandType Application -All", text)
        self.assertIn("-ne $Here", text)
        self.assertIn("$argv += @('--node', $Node)", text)

    def test_clone_keeps_the_fixture_bytes_the_a100_run_hashed(self):
        # Git for Windows converts line endings by default, which would change
        # fixture_sha256 on the Dell although the cases are identical.
        readme = (ROOT / "local" / "windows" / "README.md").read_text(encoding="utf-8")
        self.assertIn("git clone -c core.autocrlf=false bmo.bundle", readme)

    def test_eval_waits_longer_than_a_cpu_prefill(self):
        # A ~5,800-token prompt can take minutes on a laptop CPU, and a client
        # that gives up leaves the engine answering 409 `busy` until it finishes.
        args = bmo_local.build_parser().parse_args(["eval", "--engine", "e", "--model", "m"])
        self.assertGreaterEqual(args.timeout, 1800)



class LongContextCommandTests(unittest.TestCase):
    """`bmo_local.py longctx`: the long-conversation measurement on the laptop."""

    def test_longctx_receipt_carries_measurements_not_model_output(self):
        # The fake engine answers in process, so the command's whole HTTP path
        # (sessions, completions, metrics) runs without opening a socket.
        sys.path.insert(0, str(ROOT))
        from tests.model.long_context_fake_engine import SECRET_OUTPUT_MARKER, FakeLongContextEngine
        fake = FakeLongContextEngine(token="t" * 43)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "longctx.json"
            console, errors = io.StringIO(), io.StringIO()
            with mock.patch.object(bmo_local, "Engine", FakeEngine), \
                    mock.patch.object(bmo_local.lce.ev, "_open_url", fake.in_process_opener()), \
                    redirect_stdout(console), redirect_stderr(errors):
                self.assertEqual(bmo_local.main(["longctx", "--engine", "x", "--model", "y", "--sizes", "1000",
                                                 "--depths", "0.5", "--probes", "needle", "--styles", "turns",
                                                 "--show-output", "--out", str(out)]), 0)
            receipt = out.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_OUTPUT_MARKER, receipt)
        self.assertNotIn(SECRET_OUTPUT_MARKER, console.getvalue())
        self.assertIn(SECRET_OUTPUT_MARKER, errors.getvalue(), "--show-output prints to the console only")
        data = json.loads(receipt)
        self.assertEqual(data["schema"], "local_bmo.long-context-eval.v1")
        self.assertIs(data["prompt_response_logging"], False)
        self.assertEqual(data["model_load_seconds"], 1.0)
        self.assertEqual([(c["id"], c["outcome"]) for c in data["cells"]], [("needle/turns/s1000/d0.5/t0", "pass")])
        self.assertGreater(data["cells"][0]["prompt_tokens"], 800)

    def test_longctx_waits_longer_than_a_cpu_prefill(self):
        args = bmo_local.build_parser().parse_args(["longctx", "--engine", "e", "--model", "m"])
        self.assertGreaterEqual(args.timeout, 1800)
        self.assertLessEqual(args.max_tokens, 256, "answers are short; output budget is context lost")
        self.assertEqual(args.context, 8192)
        self.assertEqual(list(args.sizes), sorted(args.sizes), "cheapest first")

    def test_windows_wrapper_routes_longctx_with_a_receipt(self):
        text = (ROOT / "local" / "windows" / "Test-BMO.ps1").read_text(encoding="utf-8")
        self.assertRegex(text, r"ValidateSet\([^)]*'longctx'")
        self.assertIn("'longctx' { $argv += @($Launcher, 'longctx', '--out'", text)
        readme = (ROOT / "local" / "windows" / "README.md").read_text(encoding="utf-8")
        self.assertIn("-Mode longctx", readme)
        self.assertIn("context_overflow", readme)


if __name__ == "__main__":
    unittest.main()
