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
        self.assertIn("$Backend -eq 'cpu' -and (Test-Path $Prebuilt)", text)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("local/bin/", ignored)

    def test_clone_keeps_the_fixture_bytes_the_a100_run_hashed(self):
        # Git for Windows converts line endings by default, which would change
        # fixture_sha256 on the Dell although the cases are identical.
        readme = (ROOT / "local" / "windows" / "README.md").read_text(encoding="utf-8")
        self.assertIn("git clone -c core.autocrlf=false bmo.bundle", readme)

    def test_eval_waits_longer_than_a_cpu_prefill(self):
        # A ~5,800-token prompt can take minutes on a laptop CPU, and a client
        # that gives up leaves the engine answering 503 until it finishes.
        args = bmo_local.build_parser().parse_args(["eval", "--engine", "e", "--model", "m"])
        self.assertGreaterEqual(args.timeout, 1800)


if __name__ == "__main__":
    unittest.main()
