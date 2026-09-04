import importlib.util
import json
import os
import subprocess
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class J1MConfigTests(unittest.TestCase):
    def setUp(self):
        self.j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_runner")

    def test_pins_source_converter_and_target_cost(self):
        config = self.j1m.load_config()
        self.assertEqual(config["source"]["revision"], "c202236235762e1c871ad0ccb60c8ee5ba337b9a")
        self.assertEqual(config["llama_cpp"]["revision"], "3581ba0cf591b3f772fbb002de0f70e294bc0396")
        plan = self.j1m.build_plan(config, "build")
        prove = self.j1m.build_plan(config, "prove")
        self.assertEqual(plan["candidate"]["cloud"], "hyperstack")
        self.assertEqual(plan["candidate"]["region"], "montreal-canada-2")
        self.assertEqual(prove["active_run_cost_usd"], 0.3375)
        self.assertEqual(plan["active_run_cost_usd"], 1.6875)
        self.assertEqual(prove["commands"], [["python3", "scripts/j1m_runner.py", "--prove"]])
        self.assertEqual(config["artifacts"]["prove_fetch_allowlist"], ["proving-receipt.json"])
        self.assertNotIn("Qwen3.5-9B-Q4_K_M.gguf", config["artifacts"]["prove_fetch_allowlist"])
        self.assertIn("torch==2.7.0", " ".join(plan["commands"][3]))
        converters = [command for command in plan["commands"] if any("convert_hf_to_gguf.py" in part for part in command)]
        self.assertEqual(len(converters), 2)
        self.assertTrue(all("--no-mtp" in command for command in converters))
        self.assertTrue(any("torch==2.7.0" in command for command in plan["commands"][3]))
        self.assertTrue(any("--verify-llama" in command for command in plan["commands"]))
        self.assertTrue(any("--inspect-tensors" in command for command in plan["commands"]))

    def test_hf_token_file_is_private_and_removed(self):
        with self.j1m.hf_token_file("test-token-never-logged") as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("HF_TOKEN=", path.read_text())
        self.assertFalse(path.exists())

    def test_artifact_allowlist_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.gguf").write_bytes(b"fixture")
            with self.assertRaises(ValueError):
                self.j1m.artifact_manifest(root, ["../ok.gguf"])

    def test_receipts_are_created_without_manifest_self_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"]
            for name in names:
                (root / name).write_bytes(b"fixture")
            manifest = self.j1m.write_artifacts(root, names)
            self.assertEqual(len(manifest["artifacts"]), 7)
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "checksums.sha256").is_file())

    def test_local_fetch_allows_only_q4_and_receipts(self):
        fetch = load(ROOT / "scripts/j1m_fetch.py", "j1m_fetch")
        selected = fetch.select_local_artifacts(["Qwen3.5-9B-Q4_K_M.gguf", "manifest.json"])
        self.assertEqual(selected[0], "Qwen3.5-9B-Q4_K_M.gguf")
        with self.assertRaises(ValueError):
            fetch.select_local_artifacts(["Qwen3.5-9B-bf16.gguf"])


class StaticSafetyTests(unittest.TestCase):
    def test_no_donor_capture_executable_remains(self):
        for path in (ROOT / "scripts").rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("archive_prefix", text, path)
            self.assertNotIn("capture.run", text, path)
            self.assertNotIn("olmo", text, path)
            self.assertNotIn("moe", text, path)

    def test_hardware_receipt_has_no_serial_or_full_output_path(self):
        text = (ROOT / "hardware/windows-probe/Get-HardwareReceipt.ps1").read_text()
        self.assertNotIn("IdentifyingNumber", text)
        self.assertNotIn("product_identifier", text)
        self.assertNotIn('Write-Output "Receipt written: $([IO.Path]::GetFullPath', text)

    def test_lifecycle_has_no_account_wide_delete_path(self):
        text = (ROOT / "scripts/shadeform_lifecycle.py").read_text(encoding="utf-8")
        self.assertNotIn("/instances?", text)
        self.assertNotIn("/instances/list", text)
        self.assertIn("f\"/instances/{exact}/delete\"", text)

    def test_append_only_cost_ledger_pending_then_settled(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory() as directory:
            original = sf.COST_LEDGER
            sf.COST_LEDGER = Path(directory) / "cost-ledger.jsonl"
            try:
                sf.append_cost_event({"instance_id": "instance-ledger-1", "phase_id": "phase-a", "status": "pending", "estimated_cost_usd": 1.0})
                self.assertEqual(sf.ledger_spend(), (0.0, ["instance-ledger-1"]))
                sf.append_cost_event({"instance_id": "instance-ledger-1", "phase_id": "phase-a", "status": "settled", "actual_cost_usd": 0.42})
                self.assertEqual(sf.ledger_spend(), (0.42, []))
                self.assertEqual(len(sf.COST_LEDGER.read_text().splitlines()), 2)
            finally:
                sf.COST_LEDGER = original


class LoopbackLifecycleTests(unittest.TestCase):
    """Exercise exact deletion and killed-launcher recovery without a provider."""

    def test_killed_launcher_has_no_orphan(self):
        from scripts import shadeform_lifecycle as sf

        calls: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                calls.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":true}')

            def do_GET(self):
                calls.append(self.path)
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        phase = "j1m-loopback-no-orphan"
        runtime = sf.runtime_ledger_path(phase)
        ledger = sf.MARKDOWN_LEDGER
        original_ledger = ledger.read_text(encoding="utf-8")
        cost_ledger = sf.COST_LEDGER
        original_cost_ledger = cost_ledger.read_bytes() if cost_ledger.exists() else None
        env_fd, env_name = tempfile.mkstemp(prefix="j1m-loopback-env-")
        os.close(env_fd)
        env_file = Path(env_name)
        env_file.write_text("SHADEFORM_API_KEY=stub-key\n", encoding="utf-8")
        launcher = subprocess.Popen([os.sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            sf.write_owned_resource(sf.OwnedResource(
                phase_id=phase, run_id="j1m-test", instance_id="instance-loopback-1",
                ownership_nonce="0123456789abcdef0123456789abcdef", ssh_key_id="key-loopback-1",
                ssh_key_name="j1m-test-key", gpu="A100_80G", cloud="hyperstack", region="Montreal",
                hourly_usd=1.35, created_at_utc=sf.utc_now().isoformat(), launcher_pid=launcher.pid,
            ))
            env = {**os.environ, "EP_SHADEFORM_API_BASE_FOR_TESTS": f"http://127.0.0.1:{server.server_port}"}
            result = subprocess.run([
                os.sys.executable, "scripts/shadeform_watchdog.py", "--phase-id", phase,
                "--instance-id", "instance-loopback-1", "--launcher-pid", str(launcher.pid),
                "--max-seconds", "0.15", "--poll-seconds", "0.03", "--env-file", str(env_file),
            ], env=env, timeout=10)
            launcher.wait(timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(runtime.exists())
            self.assertIn("/instances/instance-loopback-1/delete", calls)
            self.assertIn("/sshkeys/key-loopback-1/delete", calls)
            self.assertNotIn("/instances", [path for path in calls if path == "/instances"])
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()
            runtime.unlink(missing_ok=True)
            ledger.write_text(original_ledger, encoding="utf-8")
            if original_cost_ledger is None:
                cost_ledger.unlink(missing_ok=True)
            else:
                cost_ledger.write_bytes(original_cost_ledger)
            env_file.unlink(missing_ok=True)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_transport_timeout_is_a_result(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator")
        result = orchestrator._remote([os.sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)
        self.assertEqual(result["status"], "transport_timeout")


if __name__ == "__main__":
    unittest.main()
