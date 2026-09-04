import importlib.util
import json
import os
import subprocess
import stat
import tempfile
import threading
import unittest
from unittest import mock
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
        self.assertIn("requirements-convert_hf_to_gguf.txt", " ".join(plan["commands"][3]))
        converters = [command for command in plan["commands"] if any("convert_hf_to_gguf.py" in part for part in command)]
        self.assertEqual(len(converters), 2)
        self.assertTrue(all("--no-mtp" in command for command in converters))
        self.assertTrue(any("gguf-py" in command for command in plan["commands"][3]))
        self.assertTrue(any("--verify-llama" in command for command in plan["commands"]))
        self.assertTrue(any("--inspect-tensors" in command for command in plan["commands"]))
        hf_commands = [command for command in plan["commands"] if any(part == "download" for part in command)]
        self.assertEqual(len(hf_commands), 1)
        self.assertNotIn("--local-dir-use-symlinks", hf_commands[0])

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
            (root / "source-model-receipt.json").write_text(json.dumps({"status": "verified", "revision": "a" * 40, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64}), encoding="utf-8")
            (root / "tensor-metadata.json").write_text(json.dumps({"status": "verified", "tensor_count": 3}), encoding="utf-8")
            (root / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": "e" * 40}), encoding="utf-8")
            (root / "command-receipt.json").write_text("[{\"stage\": 1, \"status\": \"completed\"}]\n", encoding="utf-8")
            (root / "scan-receipt.json").write_text(json.dumps({"status": "verified"}), encoding="utf-8")
            manifest = self.j1m.write_artifacts(root, names)
            self.assertEqual(len(manifest["artifacts"]), 8)
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "checksums.sha256").is_file())

    def test_deployable_bundle_remains_verifiable_without_intermediates(self):
        fetch = load(ROOT / "scripts/j1m_fetch.py", "j1m_fetch_bundle")
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory) / "remote"
            local = Path(directory) / "local"
            remote.mkdir()
            for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"):
                (remote / name).write_bytes(name.encode())
            (remote / "source-model-receipt.json").write_text(json.dumps({"status": "verified", "revision": "a" * 40, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64}), encoding="utf-8")
            (remote / "tensor-metadata.json").write_text(json.dumps({"status": "verified"}), encoding="utf-8")
            (remote / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1"}), encoding="utf-8")
            (remote / "command-receipt.json").write_text("[{\"stage\": 1, \"status\": \"completed\"}]\n", encoding="utf-8")
            (remote / "scan-receipt.json").write_text(json.dumps({"status": "verified"}), encoding="utf-8")
            self.j1m.write_artifacts(remote, ["Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"])
            (remote / "Qwen3.5-9B-bf16.gguf").unlink()
            (remote / "Qwen3.5-9B-Q8_0.gguf").unlink()
            fetch.copy_selected(remote, local, ["Qwen3.5-9B-Q4_K_M.gguf", "manifest.json", "checksums.sha256", "tensor-metadata.json", "source-model-receipt.json", "conversion-receipt.json", "model-receipt.json", "toolchain.json", "command-receipt.json", "scan-receipt.json"])
            fetch.verify_local_bundle(local)

    def test_local_fetch_allows_only_q4_and_receipts(self):
        fetch = load(ROOT / "scripts/j1m_fetch.py", "j1m_fetch")
        selected = fetch.select_local_artifacts(["Qwen3.5-9B-Q4_K_M.gguf", "manifest.json"])
        self.assertEqual(selected[0], "Qwen3.5-9B-Q4_K_M.gguf")
        with self.assertRaises(ValueError):
            fetch.select_local_artifacts(["Qwen3.5-9B-bf16.gguf"])
        self.assertIn("toolchain.json", fetch.LOCAL_ALLOWLIST)

    def test_toolchain_receipt_contains_freeze_from_same_artifact_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "artifacts"
            output.mkdir()
            (output / "pip-freeze.txt").write_text("example-package==1.2.3\n", encoding="utf-8")
            self.j1m.main(["--toolchain", str(output / "toolchain.json"), "--llama-checkout", str(ROOT)])
            receipt = json.loads((output / "toolchain.json").read_text())
            self.assertIn("example-package==1.2.3", receipt["pip_freeze"])

    def test_ephemeral_key_generation_does_not_interpret_provider_identifier(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory() as directory:
            private, public = sf.create_ephemeral_ssh_key({"SHADEFORM_SSH": "provider-uuid-123456789012345678901234"}, Path(directory))
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            self.assertTrue(public.startswith("ssh-ed25519 "))


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

    def test_candidate_budget_includes_backstop_margin(self):
        from scripts import shadeform_lifecycle as sf
        raw = [{"gpu_type": "A100", "num_gpus": 1, "cloud": "cloud-a", "hourly_price": 90, "configuration": {"vram_per_gpu_in_gb": 80}, "availability": [{"region": "r1", "available": True}], "shade_instance_type": "a100-80"}]
        env = {"SHADEFORM_GPU_TYPES": "A100", "SHADEFORM_MAX_HOURLY_COST_USD": "2", "SHADEFORM_EXCLUDED_CLOUDS": ""}
        self.assertEqual(sf._rank_candidates(raw, env, min_vram_gb=80, max_runtime_hours=1.0, budget_usd=1.0), [])

    def test_scratch_receipt_reports_observed_and_required(self):
        j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_scratch")
        receipt = j1m.check_scratch(Path("/tmp"), 1)
        self.assertGreaterEqual(receipt["available_gib"], receipt["required_gib"])

    def test_deadline_backstops_exceed_watchdog_and_run(self):
        j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_deadline")
        config = j1m.load_config()
        for mode in ("prove", "build"):
            selected = config["modes"][mode]
            self.assertGreater(selected["provider_backstop_hours"], selected["runtime_hours"])
            self.assertLess(selected["external_watchdog_seconds"], selected["provider_backstop_hours"] * 3600)


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
            launcher.kill()
            launcher.wait(timeout=5)
            env = {**os.environ, "EP_SHADEFORM_API_BASE_FOR_TESTS": f"http://127.0.0.1:{server.server_port}"}
            result = subprocess.run([
                os.sys.executable, "scripts/shadeform_watchdog.py", "--phase-id", phase,
                "--instance-id", "instance-loopback-1", "--launcher-pid", str(launcher.pid),
                "--max-seconds", "0.15", "--poll-seconds", "0.03", "--env-file", str(env_file),
            ], env=env, timeout=10)
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
