import importlib.util
import contextlib
import hashlib
import io
import json
import os
import subprocess
import stat
import shutil
import tempfile
import threading
import time
import types
import urllib.error
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
        self.assertEqual(config["modes"]["eval"]["backend"], "cuda")
        self.assertEqual(config["modes"]["eval"]["cuda_device_name"], "CUDA0")
        self.assertEqual(config["modes"]["eval"]["cuda_architecture"], 80)
        self.assertEqual(config["source"]["authentication"], "public-unauthenticated")
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
        dependency_commands = [command for command in plan["commands"] if "requirements-convert_hf_to_gguf.txt" in " ".join(command)]
        self.assertGreaterEqual(len(dependency_commands), 2)
        self.assertTrue(any("--no-index" in command and "--find-links" in command for command in dependency_commands))
        self.assertTrue(any(any("wheelhouse-lock" in part for part in command) for command in plan["commands"]))
        converters = [command for command in plan["commands"] if any("convert_hf_to_gguf.py" in part for part in command)]
        self.assertEqual(len(converters), 2)
        self.assertTrue(all("--no-mtp" in command for command in converters))
        self.assertTrue(any(any("gguf-py" in part for part in command) for command in plan["commands"]))
        self.assertTrue(any("--no-index" in command and "gguf" in command and "-e" not in command for command in plan["commands"]))
        self.assertNotIn("--token-file", [part for command in plan["commands"] for part in command])
        self.assertIn(["sudo", "apt-get", "update"], plan["commands"])
        self.assertIn(["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "python3-venv", "cmake", "build-essential"], plan["commands"])
        self.assertTrue(any("--verify-llama" in command for command in plan["commands"]))
        self.assertTrue(any("--inspect-tensors" in command for command in plan["commands"]))
        hf_commands = [command for command in plan["commands"] if any(part == "download" for part in command)]
        self.assertEqual(len(hf_commands), 1)
        self.assertNotIn("--local-dir-use-symlinks", hf_commands[0])
        cmake_configure = next(command for command in plan["commands"] if command[:2] == ["cmake", "-S"])
        self.assertIn("-DGGML_CUDA=OFF", cmake_configure)
        self.assertIn("-DLLAMA_BUILD_TOOLS=ON", cmake_configure)
        for disabled in ("TESTS", "EXAMPLES", "SERVER", "APP", "UI"):
            self.assertIn(f"-DLLAMA_BUILD_{disabled}=OFF", cmake_configure)
        remote_plan = self.j1m.command_plan(config, runner="/scratch/j1m/j1m_runner.py", config_path="/scratch/j1m/j1m-config.json")
        nested_runner_commands = [command for command in remote_plan if "/scratch/j1m/j1m_runner.py" in command]
        self.assertTrue(nested_runner_commands)
        self.assertTrue(all("--config" in command for command in nested_runner_commands))
        self.assertTrue(all(command[command.index("--config") + 1] == "/scratch/j1m/j1m-config.json" for command in nested_runner_commands))

    def test_utility_mode_does_not_require_default_config(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(self.j1m, "DEFAULT_CONFIG", Path(directory) / "missing.json"), mock.patch.object(self.j1m, "check_scratch", return_value={"status": "ok"}) as check:
            self.assertEqual(self.j1m.main(["--scratch", directory, "--min-scratch-gib", "1"]), 0)
            check.assert_called_once()

    def test_hf_token_file_is_private_and_removed(self):
        with self.j1m.hf_token_file("test-token-never-logged") as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("HF_TOKEN=", path.read_text())
        self.assertFalse(path.exists())

    def test_dependency_wheelhouse_lock_is_hash_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "gguf-1.0-py3-none-any.whl").write_bytes(b"wheel")
            lock = root / "wheelhouse-lock.json"
            self.j1m.write_wheelhouse_lock(wheelhouse, lock, llama_revision="a" * 40)
            self.assertEqual(self.j1m.verify_wheelhouse(wheelhouse, lock)["status"], "verified")
            self.j1m.main(["--wheelhouse-lock", str(lock), "--verify-wheelhouse", "--wheelhouse", str(wheelhouse)])
            (wheelhouse / "gguf-1.0-py3-none-any.whl").write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                self.j1m.verify_wheelhouse(wheelhouse, lock)

    def test_create_outcome_classification_does_not_turn_http_rejection_pending(self):
        from scripts import shadeform_lifecycle as sf
        rejection = sf.ShadeformHTTPError(400, "validation rejected")
        http_cause = urllib.error.HTTPError("https://127.0.0.1", 400, "bad", {}, io.BytesIO())
        rejection.__cause__ = http_cause
        try:
            self.assertFalse(sf.is_ambiguous_transport(rejection))
        finally:
            http_cause.close()
        self.assertTrue(sf.is_ambiguous_transport(sf.AmbiguousProviderOutcome("timeout")))

    def test_create_unusable_success_is_ambiguous_but_http_failure_is_definitive(self):
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100", "cloud", "region", "a100-80", 1.0, 80, "ubuntu", False)
        with mock.patch.object(sf, "request", return_value={"status": "accepted"}):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.create_instance("api", {}, phase_id="j1m-create-test", run_id="run", candidate=candidate, ssh_key_id="key-123456", nonce="a" * 32, max_runtime_hours=0.25)
        with mock.patch.object(sf, "request", side_effect=sf.ShadeformHTTPError(400, "rejected")):
            with self.assertRaises(sf.ShadeformHTTPError):
                sf.create_instance("api", {}, phase_id="j1m-create-test", run_id="run", candidate=candidate, ssh_key_id="key-123456", nonce="b" * 32, max_runtime_hours=0.25)

    def test_create_attempt_reservation_is_durable_gate(self):
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100", "cloud", "region", "a100-80", 1.35, 80, "ubuntu", False)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(sf, "COST_LEDGER", Path(directory) / "cost-ledger.jsonl"):
            fingerprint = sf.ssh_public_key_fingerprint("ssh-ed25519 AAAA")
            attempt_id = sf.reserve_create_attempt("j1m-reservation-test", "c" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64, public_key_fingerprint=fingerprint)
            event = json.loads(Path(directory, "cost-ledger.jsonl").read_text().strip())
            self.assertEqual(attempt_id, "attempt-" + "c" * 32)
            self.assertEqual(event["status"], "pending")
            self.assertEqual(event["estimated_cost_usd"], 0.421875)
            self.assertEqual(event["ssh_public_key_fingerprint"], fingerprint)
            sf.reserve_create_attempt("j1m-reservation-test", "c" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64, ssh_key_id="key-123456")
            enriched = json.loads(Path(directory, "cost-ledger.jsonl").read_text().splitlines()[-1])
            self.assertEqual(enriched["ssh_key_id"], "key-123456")
            with mock.patch.object(sf, "append_cost_event", side_effect=OSError("ledger unavailable")):
                with self.assertRaises(OSError):
                    sf.reserve_create_attempt("j1m-reservation-test", "e" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64)

    def test_ssh_key_ownership_normalizes_comment_but_rejects_malformed_key(self):
        from scripts import shadeform_lifecycle as sf
        with mock.patch.object(sf, "request", return_value={"id": "key-123456", "name": "j1m-key", "public_key": "  ssh-ed25519   AAAA   provider-comment\n"}):
            sf.verify_ssh_key_ownership("api", "j1m-key-test", "key-123456", expected_name="j1m-key", expected_public_key="ssh-ed25519 AAAA local-comment")
        with mock.patch.object(sf, "request", return_value={"id": "key-123456", "name": "j1m-key", "public_key": "ssh-ed25519 not-base64!"}):
            with self.assertRaises(sf.ShadeformError):
                sf.verify_ssh_key_ownership("api", "j1m-key-test", "key-123456", expected_name="j1m-key", expected_public_key="ssh-ed25519 AAAA")

    def test_ambiguous_ssh_key_create_reconciles_only_unique_nonce_fingerprint(self):
        from scripts import shadeform_lifecycle as sf
        public_key = "ssh-ed25519 AAAA"
        name = "j1m-0123456789abcdef0123456789abcdef"
        responses = iter([
            {"ssh_keys": [{"id": "key-123456", "name": name, "public_key": public_key}]},
            {"id": "key-123456", "name": name, "public_key": public_key},
        ])
        with mock.patch.object(sf, "request", side_effect=lambda *args, **kwargs: next(responses)):
            self.assertEqual(sf.reconcile_ssh_key("api", "j1m-key-test", expected_name=name, expected_public_key=public_key), "key-123456")
        fingerprint = sf.ssh_public_key_fingerprint(public_key)
        with mock.patch.object(sf, "request", return_value={"ssh_keys": [{"id": "key-123456", "name": name, "public_key": public_key}]}):
            reconciled = sf.reconcile_ssh_key("api", "j1m-key-test", expected_name=name, expected_fingerprint=fingerprint)
        self.assertEqual(reconciled, "key-123456")
        with mock.patch.object(sf, "request", return_value={"deleted": True}) as delete_request:
            self.assertEqual(sf.delete_ssh_key("api", "j1m-key-test", reconciled), {"deleted": True})
        self.assertEqual(delete_request.call_args.args[2], "/sshkeys/key-123456/delete")
        with mock.patch.object(sf, "request", return_value={"ssh_keys": []}):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.reconcile_ssh_key("api", "j1m-key-test", expected_name=name, expected_public_key=public_key)
        with mock.patch.object(sf, "request", return_value={"ssh_keys": [
            {"id": "key-123456", "name": name, "public_key": public_key},
            {"id": "key-654321", "name": name, "public_key": public_key},
        ]}):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.reconcile_ssh_key("api", "j1m-key-test", expected_name=name, expected_public_key=public_key)

    def test_ssh_key_create_transport_or_schema_failure_is_ambiguous(self):
        from scripts import shadeform_lifecycle as sf
        with mock.patch.object(sf, "request", side_effect=TimeoutError("provider timeout")):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.add_ssh_key("api", "j1m-key-test", "j1m-key", "ssh-ed25519 AAAA")
        with mock.patch.object(sf, "request", side_effect=sf.ShadeformHTTPError(503, "temporary")):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.add_ssh_key("api", "j1m-key-test", "j1m-key", "ssh-ed25519 AAAA")
        with mock.patch.object(sf, "request", return_value=[{"id": "key-123456"}]):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.add_ssh_key("api", "j1m-key-test", "j1m-key", "ssh-ed25519 AAAA")

    def test_post_cleanup_requires_exact_gguf_set_and_no_vision_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Qwen3.5-9B-Q4_K_M.gguf").write_bytes(b"q4")
            (root / "Qwen3.5-9B-mmproj.gguf").write_bytes(b"vision")
            with self.assertRaises(ValueError):
                self.j1m.post_cleanup_verify(root)
            (root / "Qwen3.5-9B-mmproj.gguf").unlink()
            self.assertEqual(self.j1m.post_cleanup_verify(root)["remaining_gguf"], ["Qwen3.5-9B-Q4_K_M.gguf"])

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
            scan_records = [{"name": name, "size_bytes": (root / name).stat().st_size, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in names]
            with self.assertRaises(ValueError):
                self.j1m.post_cleanup_verify(root)
            (root / "Qwen3.5-9B-bf16.gguf").unlink()
            (root / "Qwen3.5-9B-Q8_0.gguf").unlink()
            post_cleanup = self.j1m.post_cleanup_verify(root)
            self.assertEqual(post_cleanup["inventory_scope"], "post_cleanup_filesystem")
            lock = json.loads((ROOT / "model" / "source-lock" / "qwen35-9b.source-lock.json").read_text(encoding="utf-8"))
            source_hashes = {item["path"]: item.get("sha256") or item.get("lfs_sha256") for item in lock["source_files"] if not item.get("excluded_from_text_only") and (item.get("sha256") or item.get("lfs_sha256"))}
            (root / "source-model-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified", "model_id": lock["model_id"], "revision": lock["revision"], "checked_files": list(source_hashes), "file_hashes": source_hashes, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64, "verified_at_utc": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
            (root / "tensor-metadata.json").write_text(json.dumps({"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": 0, "tensors": [], "gguf_metadata": {"general.architecture": "qwen35"}, "vision_projection_present": False, "chat_template_sha256": "c" * 64}), encoding="utf-8")
            (root / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": "e" * 40, "python": "Python 3.11", "cmake": "cmake 3.28", "compiler": "cc 12", "os_packages": [], "pip_freeze": "", "dependency_wheelhouse_lock": {}}), encoding="utf-8")
            (root / "command-receipt.json").write_text(json.dumps([{"stage": 1, "argv": ["source-check"], "started_at_utc": "2026-01-01T00:00:00+00:00", "ended_at_utc": "2026-01-01T00:00:01+00:00", "exit_code": 0, "status": "completed"}]) + "\n", encoding="utf-8")
            (root / "scan-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True, "artifacts": scan_records, "vision_projection_present": False}), encoding="utf-8")
            (root / "post-cleanup-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": ["Qwen3.5-9B-Q4_K_M.gguf"], "forbidden_artifacts": [], "q4": {"size_bytes": (root / "Qwen3.5-9B-Q4_K_M.gguf").stat().st_size, "sha256": hashlib.sha256((root / "Qwen3.5-9B-Q4_K_M.gguf").read_bytes()).hexdigest()}}), encoding="utf-8")
            manifest = self.j1m.write_artifacts(root, names)
            self.assertEqual(len(manifest["artifacts"]), 9)
            self.assertEqual(manifest["inventory_scope"], "post_cleanup_deployable_allowlist")
            self.assertEqual(manifest["deployable_model_artifacts"], ["Qwen3.5-9B-Q4_K_M.gguf"])
            self.assertEqual(manifest["tensor_metadata"]["status"], "verified")
            self.assertNotEqual(manifest["tensor_metadata"]["status"], "pending_converter_receipt")
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "checksums.sha256").is_file())
            forged = json.loads((root / "source-model-receipt.json").read_text(encoding="utf-8"))
            forged["status"] = "verified"
            forged["file_hashes"][next(iter(forged["file_hashes"]))] = "f" * 64
            (root / "source-model-receipt.json").write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source receipt hash"):
                self.j1m.write_artifacts(root, names)

    def test_deployable_bundle_remains_verifiable_without_intermediates(self):
        fetch = load(ROOT / "scripts/j1m_fetch.py", "j1m_fetch_bundle")
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory) / "remote"
            local = Path(directory) / "local"
            remote.mkdir()
            for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"):
                (remote / name).write_bytes(name.encode())
            lock = json.loads((ROOT / "model" / "source-lock" / "qwen35-9b.source-lock.json").read_text(encoding="utf-8"))
            source_hashes = {item["path"]: item.get("sha256") or item.get("lfs_sha256") for item in lock["source_files"] if not item.get("excluded_from_text_only") and (item.get("sha256") or item.get("lfs_sha256"))}
            (remote / "source-model-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified", "model_id": lock["model_id"], "revision": lock["revision"], "checked_files": list(source_hashes), "file_hashes": source_hashes, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64, "verified_at_utc": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
            (remote / "tensor-metadata.json").write_text(json.dumps({"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": 0, "tensors": [], "gguf_metadata": {"general.architecture": "qwen35"}, "vision_projection_present": False, "chat_template_sha256": "c" * 64}), encoding="utf-8")
            (remote / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": "e" * 40, "python": "Python 3.11", "cmake": "cmake 3.28", "compiler": "cc 12", "os_packages": [], "pip_freeze": "", "dependency_wheelhouse_lock": {}}), encoding="utf-8")
            (remote / "command-receipt.json").write_text(json.dumps([{"stage": 1, "argv": ["source-check"], "started_at_utc": "2026-01-01T00:00:00+00:00", "ended_at_utc": "2026-01-01T00:00:01+00:00", "exit_code": 0, "status": "completed"}]) + "\n", encoding="utf-8")
            scan_records = [{"name": name, "size_bytes": (remote / name).stat().st_size, "sha256": hashlib.sha256((remote / name).read_bytes()).hexdigest()} for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf")]
            (remote / "scan-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True, "artifacts": scan_records, "vision_projection_present": False}), encoding="utf-8")
            q4 = remote / "Qwen3.5-9B-Q4_K_M.gguf"
            (remote / "post-cleanup-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": [q4.name], "forbidden_artifacts": [], "q4": {"size_bytes": q4.stat().st_size, "sha256": hashlib.sha256(q4.read_bytes()).hexdigest()}}), encoding="utf-8")
            self.j1m.write_artifacts(remote, ["Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"])
            (remote / "Qwen3.5-9B-bf16.gguf").unlink()
            (remote / "Qwen3.5-9B-Q8_0.gguf").unlink()
            fetch.copy_selected(remote, local, ["Qwen3.5-9B-Q4_K_M.gguf", "manifest.json", "checksums.sha256", "tensor-metadata.json", "source-model-receipt.json", "conversion-receipt.json", "model-receipt.json", "toolchain.json", "command-receipt.json", "scan-receipt.json", "post-cleanup-receipt.json"])
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

    def test_readerfield_contents_and_source_chat_template_hash_are_verified(self):
        chat_template = "{{ messages[0]['content'] }}"

        class NumpyLikeUInt64:
            def __init__(self, value):
                self.value = value
            def item(self):
                return self.value

        class ReaderField:
            def __init__(self, value):
                self.value = value
            def contents(self):
                return self.value

        class Tensor:
            name = "blk.0.attn.weight"
            shape = [NumpyLikeUInt64(2), NumpyLikeUInt64(2)]
            tensor_type = "Q4_K_M"

        class Reader:
            version = 3
            fields = {
                "general.architecture": ReaderField(["qwen35"]),
                "general.file_type": ReaderField([15]),
                "general.version": ReaderField([3]),
                "tokenizer.chat_template": ReaderField([chat_template.encode("utf-8")]),
            }
            tensors = [Tensor()]

            def __init__(self, _path):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gguf = root / "model.gguf"
            metadata = root / "tensor-metadata.json"
            source = root / "source-model-receipt.json"
            gguf.write_bytes(b"fixture")
            source.write_text(json.dumps({"chat_template_sha256": hashlib.sha256(chat_template.encode()).hexdigest()}), encoding="utf-8")
            fake_gguf = types.SimpleNamespace(GGUFReader=Reader)
            with mock.patch.dict("sys.modules", {"gguf": fake_gguf}):
                self.j1m.main(["--inspect-tensors", str(gguf), str(metadata), "--source-receipt", str(source)])
            receipt = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(receipt["gguf_metadata"]["general.architecture"], "qwen35")
            self.assertEqual(receipt["gguf_metadata"]["general.file_type"], 15)
            self.assertEqual(receipt["tensors"][0]["shape"], [2, 2])
            self.assertEqual(receipt["vision_projection_present"], False)
            self.assertEqual(receipt["chat_template_sha256"], hashlib.sha256(chat_template.encode()).hexdigest())

    def test_administrative_failure_is_returned_without_mutating_conversion_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt_path = root / "command-receipt.json"
            progress = root / "progress.json"
            outcomes = iter([0, 1])

            def fake_run(*_args, **_kwargs):
                return types.SimpleNamespace(returncode=next(outcomes))

            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                result = self.j1m.run_commands([["source-check"], ["python", "j1m_runner.py", "--manifest", str(root)], ["rm", "-f", "intermediate"]], progress, receipt_path=receipt_path)
            self.assertEqual(result[-1]["status"], "failed")
            persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0]["argv"], ["source-check"])

    def test_failed_stage_receipt_has_bounded_redacted_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt_path = root / "command-receipt.json"
            command = [os.sys.executable, "-c", "import sys; print('token=do-not-retain ' * 300, file=sys.stderr); raise SystemExit(7)"]
            result = self.j1m.run_commands([command], root / "progress.json", receipt_path=receipt_path)
            self.assertEqual(result[0]["exit_code"], 7)
            self.assertLessEqual(len(result[0]["stderr_tail"]), 1200)
            self.assertNotIn("do-not-retain", result[0]["stderr_tail"])
            self.assertIn("<redacted>", result[0]["stderr_tail"])

    def test_missing_executable_is_persisted_as_stage_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt_path = root / "command-receipt.json"
            result = self.j1m.run_commands([["j1m-executable-that-does-not-exist"]], root / "progress.json", receipt_path=receipt_path)
            self.assertEqual(result[0]["status"], "launch_failed")
            self.assertEqual(result[0]["error_type"], "FileNotFoundError")
            persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[0]["status"], "launch_failed")

    def test_hf_token_is_injected_only_into_download_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = root / "token.env"
            token_file.write_text("HF_TOKEN=token-not-logged\n", encoding="utf-8")
            token_file.chmod(0o600)
            captured = []

            def fake_run(*_args, **kwargs):
                captured.append(kwargs.get("env", {}))
                return types.SimpleNamespace(returncode=0)

            with mock.patch.dict(os.environ, {"HF_TOKEN": "ambient-never-used", "SHADEFORM_API_KEY": "api-never-used", "SHADEFORM_SSH": "ssh-never-used"}, clear=False), mock.patch.object(subprocess, "run", side_effect=fake_run):
                self.j1m.run_commands([["cmake", "--version"], ["hf", "download", "Qwen/model"]], root / "progress.json", token_file=token_file)
            self.assertNotIn("HF_TOKEN", captured[0])
            self.assertNotIn("SHADEFORM_API_KEY", captured[0])
            self.assertNotIn("SHADEFORM_SSH", captured[0])
            self.assertEqual(captured[1]["HF_TOKEN"], "token-not-logged")
            self.assertNotIn("SHADEFORM_API_KEY", captured[1])

    def test_ephemeral_key_generation_does_not_interpret_provider_identifier(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory() as directory:
            private, public = sf.create_ephemeral_ssh_key({"SHADEFORM_SSH": "provider-uuid-123456789012345678901234"}, Path(directory) / "nested" / "ssh")
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)
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

    def test_host_key_requires_stable_bounded_scans_and_strict_checking(self):
        from scripts import shadeform_lifecycle as sf
        self.assertNotIn("accept-new", " ".join(sf._transport_options(Path("/tmp/known_hosts"))))
        keyscan = "[127.0.0.1]:2222 ssh-ed25519 AAAATESTKEY comment\n"
        responses = iter([
            types.SimpleNamespace(returncode=0, stdout=keyscan),
            types.SimpleNamespace(returncode=0, stdout=keyscan),
            types.SimpleNamespace(returncode=0, stdout="256 SHA256:stable-fingerprint host (ED25519)\n"),
        ])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(sf.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)):
            receipt = sf.acquire_pinned_host_key({"ip": "127.0.0.1", "ssh_port": 2222, "ssh_user": "u"}, Path(directory) / "known_hosts")
            self.assertEqual(receipt["proof"], "two-stable-bounded-scans-residual-tofu")
            self.assertEqual(receipt["fingerprint"], "SHA256:stable-fingerprint")

    def test_host_key_multi_algorithm_set_is_stable_and_provider_fingerprint_selects_one(self):
        from scripts import shadeform_lifecycle as sf
        keys = "[127.0.0.1]:2222 ssh-ed25519 AAAAED\n[127.0.0.1]:2222 ecdsa-sha2-nistp256 AAAAEC\n"
        with tempfile.TemporaryDirectory() as directory:
            responses = iter([
                types.SimpleNamespace(returncode=0, stdout=keys),
                types.SimpleNamespace(returncode=0, stdout=keys),
                types.SimpleNamespace(returncode=0, stdout="256 SHA256:ec host (ECDSA)\n"),
                types.SimpleNamespace(returncode=0, stdout="256 SHA256:ed host (ED25519)\n"),
            ])
            with mock.patch.object(sf.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)):
                receipt = sf.acquire_pinned_host_key({"ip": "127.0.0.1", "ssh_port": 2222, "ssh_user": "u"}, Path(directory) / "known_hosts")
            self.assertEqual(receipt["key_count"], 2)
            self.assertEqual(len(Path(directory, "known_hosts").read_text().splitlines()), 2)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter([
                types.SimpleNamespace(returncode=0, stdout=keys),
                types.SimpleNamespace(returncode=0, stdout="256 SHA256:ec host (ECDSA)\n"),
                types.SimpleNamespace(returncode=0, stdout="256 SHA256:ed host (ED25519)\n"),
            ])
            with mock.patch.object(sf.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)):
                receipt = sf.acquire_pinned_host_key({"ip": "127.0.0.1", "ssh_port": 2222, "ssh_user": "u"}, Path(directory) / "known_hosts", provider_fingerprint="SHA256:ec")
            self.assertEqual(receipt["key_count"], 1)
            self.assertIn("ecdsa-sha2-nistp256", Path(directory, "known_hosts").read_text())

    def test_instance_info_must_match_nonce_tags_and_key(self):
        from scripts import shadeform_lifecycle as sf
        nonce = "0123456789abcdef0123456789abcdef"
        info = {"id": "instance-owned-1", "name": f"ep-j1m-{nonce}", "tags": ["local-bmo-j1m", "ep-phase-phase-a", f"ep-run-{nonce}"], "ssh_key_id": "key-owned-1"}
        sf.verify_instance_ownership(info, instance_id="instance-owned-1", phase_id="phase-a", nonce=nonce, ssh_key_id="key-owned-1")
        exact_profile = {**info, "cloud": "hyperstack", "region": "montreal-canada-2", "shade_instance_type": "A100_80G", "hourly_price": 135, "configuration": {"gpu_type": "A100_80G", "num_gpus": 1, "vram_per_gpu_in_gb": 80, "os": "ubuntu22.04_cuda12.2_shade_os"}}
        sf.verify_instance_ownership(exact_profile, instance_id="instance-owned-1", phase_id="phase-a", nonce=nonce, ssh_key_id="key-owned-1", expected_cloud="hyperstack", expected_region="montreal-canada-2", expected_instance_type="A100_80G", expected_hourly_usd=1.35, expected_gpu="A100_80G", expected_gpu_count=1, expected_vram_gb=80, expected_os_image="ubuntu22.04_cuda12.2_shade_os")
        exact_profile["hourly_price"] = 136
        with self.assertRaises(sf.ShadeformError):
            sf.verify_instance_ownership(exact_profile, instance_id="instance-owned-1", phase_id="phase-a", nonce=nonce, ssh_key_id="key-owned-1", expected_hourly_usd=1.35)
        info["tags"] = ["local-bmo-j1m"]
        with self.assertRaises(sf.ShadeformError):
            sf.verify_instance_ownership(info, instance_id="instance-owned-1", phase_id="phase-a", nonce=nonce, ssh_key_id="key-owned-1")

    def test_teardown_deletes_after_salvage_failure_and_revokes_key_on_delete_failure(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
        phase = "phase-teardown-failure"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sf.RUNTIME_ROOT = root / "runtime"
            sf.MARKDOWN_LEDGER = root / "ledger.md"
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            sf.COST_LEDGER = root / "cost.jsonl"
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            bad_destination = root / "destination-file"
            bad_destination.write_text("not a directory", encoding="utf-8")
            salvage_source = root / "receipt.json"
            salvage_source.write_text("receipt", encoding="utf-8")
            sf.write_owned_resource(sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-fail-1", ownership_nonce="0123456789abcdef0123456789abcdef", ssh_key_id="key-fail-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0, created_at_utc=sf.utc_now().isoformat(), launcher_pid=None,
            ))
            with mock.patch.object(teardown.shadeform, "_delete_instance", side_effect=RuntimeError("delete transport")) as delete, mock.patch.object(teardown.shadeform, "delete_ssh_key", return_value={"success": True}) as key_delete:
                with self.assertRaises(RuntimeError):
                    teardown.teardown_exact(phase, "instance-fail-1", env_file=env, salvage=salvage_source, salvage_destination=bad_destination)
            delete.assert_called_once()
            key_delete.assert_called_once_with("stub-api", phase, "key-fail-1")
            self.assertTrue(sf.runtime_ledger_path(phase).exists())
        sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_delete_instance_never_starts_final_poll_past_cleanup_deadline(self):
        from scripts import shadeform_lifecycle as sf
        clock = iter(i * 0.6 for i in range(30))
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return {"id": "instance-deadline", "status": "pending"} if args[1] == "GET" else {"accepted": True}

        with mock.patch.object(sf.time, "monotonic", side_effect=lambda: next(clock)), mock.patch.object(sf.time, "sleep"), mock.patch.object(sf, "request", side_effect=fake_request):
            result = sf._delete_instance("api", "j1m-key-test", "instance-deadline", deadline=5.0)
        self.assertFalse(result["success"])
        self.assertTrue(calls)
        self.assertLessEqual(max(calls), 5.0)

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
                with self.assertRaises(sf.BudgetError):
                    sf.remaining_budget_usd({"SHADEFORM_MAX_TOTAL_COST_USD": "50"})
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
        for mode in ("prove", "build", "eval"):
            selected = config["modes"][mode]
            self.assertGreater(selected["provider_backstop_hours"], selected["runtime_hours"])
            self.assertLess(selected["external_watchdog_seconds"], selected["provider_backstop_hours"] * 3600)
            self.assertGreater(selected["transfer_reserve_seconds"], 0)
        self.assertEqual(config["resources"]["expected_q4_gib"], 6)
        selected = config["modes"]["eval"]
        budgets = selected["stage_budgets_seconds"]
        cumulative = sum(budgets.values())
        self.assertLess(cumulative, selected["runtime_hours"] * 3600)
        # All clocks are measured from creation, while shutdown is armed only
        # after activation. Include the worst-case activation offset rather
        # than comparing independent scalar durations.
        run_seconds = selected["runtime_hours"] * 3600
        watchdog_seconds = selected["external_watchdog_seconds"]
        host_shutdown_seconds = selected["activation_timeout_seconds"] + 120 + 3 * 15 + 5 * 30 + 30 + selected["host_shutdown_delay_minutes"] * 60
        provider_seconds = selected["provider_backstop_hours"] * 3600
        self.assertLess(cumulative, run_seconds)
        self.assertLess(run_seconds, host_shutdown_seconds)
        self.assertLess(host_shutdown_seconds, watchdog_seconds)
        self.assertLess(run_seconds, provider_seconds)
        # Remote-only eval never transfers the model from this laptop; this
        # bucket is retained only for non-eval compatibility.
        self.assertEqual(budgets["model_upload"], 600)
        self.assertNotIn("timedelta(minutes=30)", (ROOT / "scripts/j1m_orchestrator.py").read_text(encoding="utf-8"))

        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_deadline_ceiling")
        envelope = orchestrator._eval_deadline_ceiling(config)
        self.assertEqual(envelope["upload_count"], 15)
        self.assertLess(envelope["ceiling_seconds"], envelope["run_seconds"])
        self.assertLess(envelope["run_seconds"], envelope["host_shutdown_from_create_seconds"])
        self.assertLess(envelope["host_shutdown_from_create_seconds"], envelope["watchdog_seconds"])

    def test_teardown_failure_keeps_watchdog_for_exact_retry(self):
        source = (ROOT / "scripts" / "j1m_orchestrator.py").read_text(encoding="utf-8")
        cleanup = source[source.index("if instance_id is not None:") : source.index("if attempt_reserved and settle_attempt_after_cleanup:")]
        self.assertIn("lifecycle[\"deletion\"] = teardown_exact", cleanup)
        self.assertNotIn("finally:\n                        #", cleanup)
        self.assertIn("if deletion_confirmed():\n                stop_watchdog()", cleanup)
        self.assertIn('deletion.get("retry_required") is not True', source)

    def test_execute_captures_teardown_failure_before_final_persistence(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_teardown_behavior")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        progress = []
        persisted = []

        class Watchdog:
            pid = 42
            def poll(self): return None
            def terminate(self): raise AssertionError("watchdog must remain alive after unconfirmed teardown")
            def wait(self, timeout): raise AssertionError("watchdog must remain alive after unconfirmed teardown")

        def remote(*args, **kwargs):
            return {"status": "completed", "exit_code": 0}

        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "id_ed25519"
            identity.write_text("private", encoding="utf-8")
            with contextlib.ExitStack() as stack:
                teardown_failure = mock.patch.object(orchestrator, "teardown_exact", side_effect=RuntimeError("delete unavailable"))
                patches = [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"),
                    mock.patch.object(orchestrator.sf, "list_candidates", return_value=[candidate]),
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key", return_value=(identity, "ssh-ed25519 AAAA")),
                    mock.patch.object(orchestrator.sf, "reserve_create_attempt", return_value="attempt-x"),
                    mock.patch.object(orchestrator.sf, "add_ssh_key", return_value="key-123456"),
                    mock.patch.object(orchestrator.sf, "verify_ssh_key_ownership", return_value={}),
                    mock.patch.object(orchestrator.sf, "create_instance", return_value="instance-123456"),
                    mock.patch.object(orchestrator.sf, "process_start_marker", return_value=None),
                    mock.patch.object(orchestrator.sf, "write_owned_resource"),
                    mock.patch.object(orchestrator.sf, "append_cost_event"),
                    mock.patch.object(orchestrator.sf, "wait_active", return_value={"id": "instance-123456", "status": "active", "ip": "127.0.0.1", "ssh_user": "runner", "ssh_port": 22}),
                    mock.patch.object(orchestrator.sf, "verify_instance_ownership"),
                    mock.patch.object(orchestrator.sf, "validate_ssh_user", return_value="runner"),
                    mock.patch.object(orchestrator.sf, "acquire_pinned_host_key", return_value={"status": "verified"}),
                    mock.patch.object(orchestrator.sf, "ssh_base", return_value=["ssh"]),
                    mock.patch.object(orchestrator.sf, "scp_base", return_value=["scp"]),
                    mock.patch.object(orchestrator.subprocess, "Popen", return_value=Watchdog()),
                    mock.patch.object(orchestrator, "_remote", side_effect=remote),
                    mock.patch.object(orchestrator, "_salvage", return_value=[]),
                    mock.patch.object(orchestrator, "_persist_lifecycle", side_effect=lambda phase, value: persisted.append(value)),
                    mock.patch.object(orchestrator, "_progress", side_effect=lambda path, event, **details: progress.append(event)),
                    mock.patch.object(orchestrator.j1m_runner, "write_progress"),
                ]
                for patcher in patches:
                    stack.enter_context(patcher)
                teardown_mock = stack.enter_context(teardown_failure)
                with self.assertRaisesRegex(RuntimeError, "teardown was not confirmed"):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=ROOT / "model/conversion/j1m-config.json",
                        phase_id="teardown-behavior", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
        self.assertTrue(persisted)
        self.assertEqual(progress[-1], "failed")
        self.assertIsNotNone(teardown_mock.call_args.kwargs.get("deadline"))

    def test_execute_reservation_failure_restores_signal_and_terminalizes(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_reservation_behavior")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        progress = []
        persisted = []
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "id_ed25519"
            identity.write_text("private", encoding="utf-8")
            with contextlib.ExitStack() as stack:
                for patcher in [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"),
                    mock.patch.object(orchestrator.sf, "list_candidates", return_value=[candidate]),
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key", return_value=(identity, "ssh-ed25519 AAAA")),
                    mock.patch.object(orchestrator.sf, "reserve_create_attempt", side_effect=OSError("ledger unavailable")),
                    mock.patch.object(orchestrator, "_persist_lifecycle", side_effect=lambda phase, value: persisted.append(value)),
                    mock.patch.object(orchestrator, "_progress", side_effect=lambda path, event, **details: progress.append(event)),
                    mock.patch.object(orchestrator.j1m_runner, "write_progress"),
                ]:
                    stack.enter_context(patcher)
                with self.assertRaises(OSError):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=ROOT / "model/conversion/j1m-config.json",
                        phase_id="reservation-behavior", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
        self.assertTrue(persisted)
        self.assertEqual(progress[-1], "failed")

    def test_unresolved_ssh_key_ambiguity_keeps_reservation_pending(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_key_ambiguity")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        progress = []
        persisted = []
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "id_ed25519"
            identity.write_text("private", encoding="utf-8")
            with contextlib.ExitStack() as stack:
                for patcher in [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"),
                    mock.patch.object(orchestrator.sf, "list_candidates", return_value=[candidate]),
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key", return_value=(identity, "ssh-ed25519 AAAA")),
                    mock.patch.object(orchestrator.sf, "reserve_create_attempt", return_value="attempt-x"),
                    mock.patch.object(orchestrator.sf, "add_ssh_key", side_effect=sf.AmbiguousProviderOutcome("unknown")),
                    mock.patch.object(orchestrator.sf, "reconcile_ssh_key", side_effect=sf.AmbiguousProviderOutcome("zero matches")),
                    mock.patch.object(orchestrator.sf, "append_incident"),
                    mock.patch.object(orchestrator.sf, "append_cost_event"),
                    mock.patch.object(orchestrator, "_persist_lifecycle", side_effect=lambda phase, value: persisted.append(value)),
                    mock.patch.object(orchestrator, "_progress", side_effect=lambda path, event, **details: progress.append(event)),
                    mock.patch.object(orchestrator.j1m_runner, "write_progress"),
                ]:
                    stack.enter_context(patcher)
                with self.assertRaises(sf.AmbiguousProviderOutcome):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=ROOT / "model/conversion/j1m-config.json",
                        phase_id="key-ambiguity", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
                self.assertFalse(orchestrator.sf.append_cost_event.called)
        self.assertTrue(persisted)
        self.assertEqual(persisted[-1]["ssh_key_reconciliation"]["status"], "unresolved")
        self.assertEqual(progress[-1], "failed")

    def test_eval_rejects_local_artifact_execution_path(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_reject_local_eval")
        with self.assertRaises(ValueError):
            orchestrator.execute(
                ROOT / ".env", config_path=ROOT / "model/conversion/j1m-config.json",
                phase_id="local-eval-refused", run_id="J1M", artifact_destination=Path("/tmp/eval"),
                mode="eval", model_artifact=Path("/tmp/Qwen3.5-9B-Q4_K_M.gguf"),
            )

    def test_progress_wrapper_accepts_stage_detail_without_collision(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_progress_wrapper")
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.json"
            orchestrator._progress(progress, "eval-stage-starting", phase_id="p", operation_stage="eval-bootstrap:mkdir")
            payload = json.loads(progress.read_text(encoding="utf-8"))
        self.assertEqual(payload["stage"], "eval-stage-starting")
        self.assertEqual(payload["operation_stage"], "eval-bootstrap:mkdir")

    def test_remote_failure_does_not_retain_stdout_evidence(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_remote_output")
        completed = subprocess.CompletedProcess(
            ["probe"],
            2,
            stdout="remote toolchain refused: token=do-not-retain\n",
            stderr="",
        )
        with mock.patch.object(orchestrator.subprocess, "run", return_value=completed):
            receipt = orchestrator._remote(["probe"], timeout=1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertNotIn("stdout_tail", receipt)
        self.assertNotIn("do-not-retain", json.dumps(receipt))

    def test_eval_stage_labels_distinguish_python_and_cmake_operations(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_stage_labels")
        self.assertEqual(
            orchestrator._eval_stage_label(["python3", "/scratch/j1m/remote_toolchain_probe.py"]),
            "eval-stage:remote_toolchain_probe",
        )
        self.assertEqual(orchestrator._eval_stage_label(["cmake", "-S", "engine"]), "eval-stage:cmake-configure")
        self.assertEqual(orchestrator._eval_stage_label(["cmake", "--build", "build"]), "eval-stage:cmake-build")

    def test_salvage_timeout_is_size_aware_and_deadline_bounded(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_timeout")
        info = {"phase_id": "j1m-test", "instance_info": {"ssh_user": "u", "ip": "127.0.0.1"}}
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "id"
            known_hosts = Path(directory) / "known_hosts"
            destination = Path(directory) / "artifacts"
            with mock.patch.object(orchestrator.sf, "_preflight"), mock.patch.object(orchestrator.sf, "scp_base", return_value=["scp"]), mock.patch.object(orchestrator, "_remote", side_effect=lambda command, timeout: {"status": "completed", "timeout": timeout}) as remote:
                result = orchestrator._salvage(info, identity, known_hosts, destination, ["Qwen3.5-9B-Q4_K_M.gguf"], q4_expected_gib=6, deadline=time.monotonic() + 1000)
            self.assertEqual(result[0]["status"], "completed")
            self.assertGreaterEqual(remote.call_args.kwargs["timeout"], 360)
            with mock.patch.object(orchestrator.sf, "_preflight"), mock.patch.object(orchestrator.sf, "scp_base", return_value=["scp"]), mock.patch.object(orchestrator, "_remote", side_effect=lambda command, timeout: {"status": "completed", "timeout": timeout}) as remote:
                orchestrator._salvage(info, identity, known_hosts, destination, ["Qwen3.5-9B-Q4_K_M.gguf"], q4_expected_gib=6, deadline=time.monotonic() + 500)
            self.assertLessEqual(remote.call_args.kwargs["timeout"], 80)

    def test_salvage_stops_without_scp_when_only_deletion_reserve_remains(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_salvage_reserve")
        info = {"phase_id": "j1m-test", "instance_info": {"ssh_user": "u", "ip": "127.0.0.1"}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(orchestrator.sf, "_preflight"), mock.patch.object(orchestrator.sf, "scp_base", return_value=["scp"]), mock.patch.object(orchestrator, "_remote") as remote:
            result = orchestrator._salvage(info, Path(directory) / "id", Path(directory) / "known", Path(directory) / "out", ["one.json", "two.json"], deadline=time.monotonic() + 0.01)
        remote.assert_not_called()
        self.assertEqual([item["status"] for item in result], ["salvage_failed", "salvage_failed"])

    def test_eval_deadline_envelope_keeps_host_shutdown_jitter(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_jitter")
        config = load(ROOT / "scripts/j1m_runner.py", "j1m_jitter_config").load_config()
        envelope = orchestrator._eval_deadline_ceiling(config)
        self.assertGreaterEqual(envelope["watchdog_seconds"] - envelope["host_shutdown_from_create_seconds"], 120)

    def test_orchestrator_failed_remote_receipt_does_not_retain_stdout(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_no_stdout")
        result = types.SimpleNamespace(returncode=2, stdout="model response SECRET_PROMPT", stderr="safe failure")
        with mock.patch.object(orchestrator.subprocess, "run", return_value=result):
            receipt = orchestrator._remote(["ssh", "host", "eval"], timeout=1)
        self.assertNotIn("stdout_tail", receipt)
        self.assertNotIn("SECRET_PROMPT", json.dumps(receipt))

    def test_remote_prove_uses_the_uploaded_config(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_prove_argv")
        command = orchestrator._remote_job_command("prove", "/scratch/j1m", 70)
        self.assertEqual(command[command.index("--config") + 1], "/scratch/j1m/j1m-config.json")
        self.assertEqual(command[command.index("--output") + 1], "/scratch/j1m/artifacts/proving-receipt.json")
        self.assertEqual(command[command.index("--min-scratch-gib") + 1], "70")

    def test_remote_build_uses_the_uploaded_config(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_build_argv")
        command = orchestrator._remote_job_command("build", "/scratch/j1m", 70)
        self.assertEqual(command, ["python3", "/scratch/j1m/j1m_runner.py", "--run", "--config", "/scratch/j1m/j1m-config.json"])

    def test_eval_plan_is_bounded_and_builds_pinned_cuda_backend_remotely(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_eval_plan")
        j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_eval_plan_config")
        config = j1m.load_config()
        plan = j1m.build_plan(config, "eval")
        self.assertEqual(orchestrator.main(["--mode", "eval"]), 0)
        self.assertEqual(plan["active_run_cost_usd"], 2.619)
        self.assertEqual(plan["provider_backstop_cost_usd"], 3.2738)
        self.assertGreater(config["modes"]["eval"]["provider_backstop_hours"], config["modes"]["eval"]["runtime_hours"])
        self.assertEqual(config["artifacts"]["eval_fetch_allowlist"], ["eval-receipt.json", "startup-preflight-receipt.json", "eval-artifact-receipt.json", "toolchain-receipt.json", "cuda-device-receipt.json"])
        commands = orchestrator._eval_remote_commands(config, "/scratch/j1m")
        flattened = [part for command in commands for part in command]
        self.assertIn(config["llama_cpp"]["revision"], flattened)
        self.assertIn("-DLAE_ENABLE_LLAMA_CPP=ON", flattened)
        self.assertIn("-DLAE_ENABLE_LLAMA_CUDA=ON", flattened)
        self.assertIn("-DCMAKE_CUDA_ARCHITECTURES=80", flattened)
        self.assertIn("-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc", flattened)
        self.assertIn(["cmake", "--build", "/scratch/j1m/engine-build", "--target", "lae-engine", "--parallel", "8"], commands)
        self.assertIn("cuda_device_probe.py", " ".join(flattened))
        self.assertIn("--backend", flattened)
        self.assertIn("cuda", flattened)
        self.assertIn("--token-file", flattened)
        self.assertIn("--toolchain-receipt", flattened)
        self.assertIn("--preflight-receipt", flattened)
        self.assertTrue(any(part.endswith("startup-preflight-receipt.json") for part in flattened))
        self.assertIn("remote_model_eval.py", " ".join(flattened))
        self.assertNotIn("--token", flattened)
        self.assertTrue(all(";" not in part and "&&" not in part for part in flattened))

    def test_eval_uploads_exact_model_and_repo_evaluator_sources(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_eval_uploads")
        j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_eval_upload_config")
        uploads = orchestrator._eval_uploads(j1m.load_config(), "/scratch/j1m", Path("/tmp/Qwen3.5-9B-Q4_K_M.gguf"), Path("/tmp/model-manifest.json"))
        names = {local.name for local, _remote, _recursive in uploads}
        self.assertEqual(names, {"Qwen3.5-9B-Q4_K_M.gguf", "model-manifest.json", "model-manifest.sha256", "remote_model_eval.py", "remote_eval_prepare.py", "evaluate_tool_calls.py", "cuda_device_probe.py", "remote_toolchain_probe.py", "cuda_source_closure.py", "ggml-cuda-source-lock.json", "tool_call_eval.json", "CMakeLists.txt", "native", "runtime_tests.cpp", "model_validator_tests.cpp"})
        self.assertTrue(any(recursive and local.name == "native" for local, _remote, recursive in uploads))
        closure_upload = next((remote for local, remote, _recursive in uploads if local.name == "cuda_source_closure.py"), None)
        self.assertEqual(closure_upload, "/scratch/j1m/engine/scripts/cuda_source_closure.py")
        lock_upload = next((remote for local, remote, _recursive in uploads if local.name == "ggml-cuda-source-lock.json"), None)
        self.assertEqual(lock_upload, "/scratch/j1m/ggml-cuda-source-lock.json")
        self.assertIn((ROOT / "vendor/llama.cpp/ggml/CMakeLists.txt", "/scratch/j1m/ggml-CMakeLists.txt", False), uploads)
        self.assertIn((ROOT / "tests/native/runtime_tests.cpp", "/scratch/j1m/engine/tests/native/runtime_tests.cpp", False), uploads)
        self.assertIn((ROOT / "tests/native/model_validator_tests.cpp", "/scratch/j1m/engine/tests/native/model_validator_tests.cpp", False), uploads)
        source = (ROOT / "scripts/j1m_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("eval_commands[:3]", source)
        self.assertIn("eval_commands[3:]", source)
        commands = orchestrator._eval_remote_commands(j1m.load_config(), "/scratch/j1m")
        self.assertEqual(commands[1][:2], ["sudo", "apt-get"])
        self.assertEqual(commands[2][:5], ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"])
        j1m_index = next(index for index, command in enumerate(commands) if "j1m_runner.py" in command[1])
        toolchain_indices = [index for index, command in enumerate(commands) if "remote_toolchain_probe.py" in command[1]]
        self.assertEqual(len(toolchain_indices), 2)
        self.assertLess(toolchain_indices[0], j1m_index)
        self.assertGreater(toolchain_indices[1], j1m_index)
        self.assertLess(toolchain_indices[1], next(index for index, command in enumerate(commands) if command[0:2] == ["cmake", "-S"]))
        self.assertIn(["cp", "/scratch/j1m/ggml-cuda-source-lock.json", "/scratch/j1m/engine/vendor/llama.cpp/ggml-cuda-source-lock.json"], commands)
        self.assertIn(["cp", "/scratch/j1m/ggml-CMakeLists.txt", "/scratch/j1m/engine/vendor/llama.cpp/ggml/CMakeLists.txt"], commands)
        self.assertIn(["cp", "-a", "/scratch/llama.cpp", "/scratch/j1m/engine/vendor/llama.cpp"], commands)
        self.assertIn(["python3", "/scratch/j1m/remote_eval_prepare.py", "--artifact", "/scratch/j1m/artifacts/Qwen3.5-9B-Q4_K_M.gguf", "--manifest", "/scratch/j1m/model-manifest.json", "--output", "/scratch/j1m/artifacts/eval-artifact-receipt.json"], commands)
        self.assertIn(["python3", "/scratch/j1m/j1m_runner.py", "--run", "--config", "/scratch/j1m/j1m-config.json"], commands)
        self.assertEqual(orchestrator._eval_stage_timeout(j1m.load_config(), next(command for command in commands if "j1m_runner.py" in command[1])), 1800.0)
        eval_command = next(command for command in commands if command and command[0] == "python3" and any("remote_model_eval.py" in part for part in command))
        self.assertEqual(eval_command[eval_command.index("--model") + 1], "/scratch/j1m/artifacts/Qwen3.5-9B-Q4_K_M.gguf")
        self.assertEqual(eval_command[eval_command.index("--llama-checkout") + 1], "/scratch/llama.cpp")
        evaluator_timeout = float(eval_command[eval_command.index("--timeout") + 1])
        self.assertEqual(evaluator_timeout, 420.0)
        self.assertLessEqual(evaluator_timeout + 60.0, j1m.load_config()["modes"]["eval"]["stage_budgets_seconds"]["evaluation"])
        native_upload = next(remote for local, remote, recursive in uploads if local.name == "native" and recursive)
        self.assertEqual(native_upload, "/scratch/j1m/engine")
        self.assertNotIn("/engine/native/native", native_upload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "native"
            (source / "engine").mkdir(parents=True)
            (source / "engine" / "marker.txt").write_text("native", encoding="utf-8")
            target = root / "engine"
            target.mkdir()
            shutil.copytree(source, target / source.name)
            self.assertTrue((target / "native" / "engine" / "marker.txt").is_file())
            self.assertFalse((target / "native" / "native").exists())

    def test_eval_remote_only_upload_omits_large_local_model(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_remote_only_uploads")
        j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_remote_only_upload_config")
        uploads = orchestrator._eval_uploads(
            j1m.load_config(), "/scratch/j1m", None,
            ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json",
        )
        names = {local.name for local, _remote, _recursive in uploads}
        self.assertNotIn("Qwen3.5-9B-Q4_K_M.gguf", names)
        self.assertIn("model-manifest.json", names)
        self.assertIn("remote_eval_prepare.py", names)
        self.assertNotIn("/model/Qwen3.5-9B-Q4_K_M.gguf", " ".join(remote for _local, remote, _recursive in uploads))

    def test_remote_eval_prepare_verifies_small_fixture_without_local_model(self):
        remote = load(ROOT / "scripts/test/remote_eval_prepare.py", "remote_eval_prepare_fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "Qwen3.5-9B-Q4_K_M.gguf"
            artifact.write_bytes(b"q4 fixture")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            manifest = root / "model-manifest.json"
            manifest.write_text(json.dumps({"artifact": {
                "expected_file_name": artifact.name,
                "expected_size_bytes": artifact.stat().st_size,
                "sha256": digest,
                "quantization_profile": "Q4_K_M",
                "modality_profile": "text_only_no_mmproj",
            }}), encoding="utf-8")
            (root / "model-manifest.sha256").write_text(
                f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  model-manifest.json\n",
                encoding="utf-8",
            )
            receipt = remote.verify(artifact, manifest, root / "receipt.json")
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(json.loads((root / "receipt.json").read_text())["sha256"], digest)
            (root / "model-manifest.sha256").write_text("0" * 64 + "  model-manifest.json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "lock_mismatch"):
                remote.verify(artifact, manifest, root / "receipt-2.json")

    def test_eval_artifact_identity_is_stream_hash_locked(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_eval_identity")
        j1m = load(ROOT / "scripts/j1m_runner.py", "j1m_eval_identity_config")
        config = j1m.load_config()
        approved = orchestrator._verify_eval_artifact(
            None, ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json", config,
        )
        self.assertEqual(approved["name"], "Qwen3.5-9B-Q4_K_M.gguf")
        tampered_config = json.loads(json.dumps(config))
        tampered_config["artifacts"]["eval_manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "trust anchor"):
            orchestrator._verify_eval_artifact(
                None, ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json", tampered_config,
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "Qwen3.5-9B-Q4_K_M.gguf"
            artifact.write_bytes(b"approved q4 fixture")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            manifest = root / "model-manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "1.1.0",
                "source": {"organization": "Qwen", "repository": "Qwen3.5-9B", "revision": config["source"]["revision"]},
                "conversion": {"llama_cpp_revision": config["llama_cpp"]["revision"]},
                "artifact": {"expected_file_name": artifact.name, "expected_size_bytes": artifact.stat().st_size, "sha256": digest, "modality_profile": "text_only_no_mmproj", "quantization_profile": "Q4_K_M"},
            }), encoding="utf-8")
            (root / "model-manifest.sha256").write_text(f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  model-manifest.json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "trust anchor"):
                orchestrator._verify_eval_artifact(None, manifest, config)
            identity = orchestrator._verify_eval_artifact(artifact, manifest, config, expected_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
            self.assertEqual(identity["sha256"], digest)
            artifact.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_artifact(artifact, manifest, config, expected_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())

    def test_eval_artifact_receipt_is_exact_and_trust_bound(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_eval_artifact_receipt")
        artifact = {"name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 4, "sha256": "a" * 64}
        receipt = {"schema": "local_bmo.j1m.remote-eval-artifact-receipt.v1", "status": "verified", **artifact, "manifest_sha256": orchestrator._APPROVED_EVAL_MANIFEST_SHA256, "manifest_lock_sha256": orchestrator._APPROVED_EVAL_MANIFEST_SHA256}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eval-artifact-receipt.json"
            with self.assertRaises(OSError):
                orchestrator._verify_eval_artifact_receipt(path, artifact)
            path.write_text(json.dumps(receipt), encoding="utf-8")
            selected = orchestrator._verify_eval_artifact_receipt(path, artifact)
            self.assertEqual(selected["sha256"], artifact["sha256"])
            for hostile in (
                {**receipt, "sha256": "b" * 64},
                {**receipt, "manifest_sha256": "c" * 64},
                {**receipt, "extra": "forged"},
            ):
                path.write_text(json.dumps(hostile), encoding="utf-8")
                with self.assertRaises(ValueError):
                    orchestrator._verify_eval_artifact_receipt(path, artifact)
            path.write_text('{"schema":"local_bmo.j1m.remote-eval-artifact-receipt.v1","schema":"local_bmo.j1m.remote-eval-artifact-receipt.v1"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_artifact_receipt(path, artifact)
            path.write_bytes(b"{" + b"x" * (orchestrator._EVAL_ARTIFACT_RECEIPT_MAX_BYTES + 1))
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_artifact_receipt(path, artifact)

    def test_remote_eval_redacts_diagnostics_and_never_accepts_bearer_argv(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_security")
        source = (ROOT / "scripts/test/remote_model_eval.py").read_text(encoding="utf-8")
        self.assertNotIn('"--token",', source)
        self.assertNotIn('"--size"', source)
        self.assertNotIn('"--sha256"', source)
        self.assertNotIn("secret-value", remote._tail("token=secret-value"))
        self.assertIn("<redacted>", remote._tail("token=secret-value"))

    def test_remote_eval_engine_launch_matches_hardened_cuda_cli(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_argv")
        args = types.SimpleNamespace(
            engine="/approved/lae-engine",
            model="/approved/Qwen3.5-9B-Q4_K_M.gguf",
            cuda_device_name="CUDA0",
        )
        launch = remote._engine_launch_argv(args, Path("/approved/engine-token"), "cuda")
        self.assertEqual(launch, [
            "/approved/lae-engine", "serve", "--port", "0", "--backend", "cuda",
            "--model", "/approved/Qwen3.5-9B-Q4_K_M.gguf", "--context", "2048",
            "--token-file", "/approved/engine-token", "--gpu-layers", "99",
            "--cuda-device-name", "CUDA0",
        ])
        self.assertNotIn("--size", launch)
        self.assertNotIn("--sha256", launch)

    def test_remote_eval_native_model_preflight_is_strict_and_bounded(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_preflight")
        args = types.SimpleNamespace(engine="/approved/lae-engine", model="/approved/Qwen3.5-9B-Q4_K_M.gguf")
        artifact = {"size_bytes": 42, "sha256": "a" * 64}
        payload = {"valid": True, "code": "ok", "size_bytes": 42, "sha256": "a" * 64, "gguf_version": 3}
        with mock.patch.object(remote, "_run_bounded", return_value={"status": "completed", "exit_code": 0, "stdout": json.dumps(payload)}) as bounded:
            self.assertEqual(remote._engine_model_preflight(args, artifact), payload)
        bounded.assert_called_once_with(["/approved/lae-engine", "verify-model", "--model", args.model], timeout=30, output_limit=remote.MAX_ENGINE_LINE)
        for hostile in ({**payload, "path": "/secret/model"}, {**payload, "sha256": "b" * 64}, {**payload, "size_bytes": 43}):
            with mock.patch.object(remote, "_run_bounded", return_value={"status": "completed", "exit_code": 0, "stdout": json.dumps(hostile)}):
                with self.assertRaisesRegex(ValueError, "engine_model_preflight_invalid"):
                    remote._engine_model_preflight(args, artifact)

        invalid_model = {"valid": False, "code": "model_hash_mismatch", "size_bytes": 42, "sha256": "", "gguf_version": 0}
        with mock.patch.object(remote, "_run_bounded", return_value={"status": "failed", "exit_code": 2, "stdout": json.dumps(invalid_model)}):
            with self.assertRaisesRegex(ValueError, "engine_model_preflight_invalid"):
                remote._engine_model_preflight(args, artifact)
        for result, expected in (
            ({"status": "timeout", "exit_code": None}, "engine_model_preflight_timeout"),
            ({"status": "failed", "exit_code": -9}, "engine_model_preflight_terminated_by_signal"),
            ({"status": "failed", "exit_code": 7}, "engine_model_preflight_exit"),
            ({"status": "output_too_large", "exit_code": 0}, "engine_model_preflight_output_too_large"),
            ({"status": "failed", "exit_code": 0, "stdout": json.dumps(payload)}, "engine_model_preflight_failed"),
        ):
            with mock.patch.object(remote, "_run_bounded", return_value=result):
                with self.assertRaisesRegex(ValueError, expected):
                    remote._engine_model_preflight(args, artifact)
        wrong_version = {**payload, "gguf_version": 2}
        with mock.patch.object(remote, "_run_bounded", return_value={"status": "completed", "exit_code": 0, "stdout": json.dumps(wrong_version)}):
            with self.assertRaisesRegex(ValueError, "engine_model_preflight_invalid"):
                remote._engine_model_preflight(args, artifact)

    def test_remote_eval_duplicate_json_maps_to_finite_stage_codes(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_duplicate_codes")
        args = types.SimpleNamespace(engine="/approved/lae-engine", model="/approved/model")
        artifact = {"size_bytes": 42, "sha256": "a" * 64}
        duplicate = '{"valid":true,"valid":true,"code":"ok","size_bytes":42,"sha256":"' + "a" * 64 + '","gguf_version":3}'
        with mock.patch.object(remote, "_run_bounded", return_value={"status": "completed", "exit_code": 0, "stdout": duplicate}):
            with self.assertRaisesRegex(ValueError, "engine_model_preflight_invalid"):
                remote._engine_model_preflight(args, artifact)
        with mock.patch.object(remote, "_run_bounded", return_value={"status": "completed", "exit_code": 0, "stdout": '{"llama_cpp_revision":"x","llama_cpp_revision":"x","compiled_backend":"y"}'}):
            with self.assertRaisesRegex(ValueError, "engine_build_info_invalid"):
                remote._engine_build_info(Path("/approved/engine"), "x" * 40, "cpu")
        with self.assertRaisesRegex(ValueError, "evaluator_receipt_invalid"):
            remote._parse_evaluator_result({"status": "completed", "exit_code": 0, "stdout": '{"case_count":1,"case_count":1,"passed":1,"failed":0,"errors":0}'}, expected_case_count=1, expected_categories={"x"})

    def test_remote_eval_preflight_receipt_is_atomic_small_and_secret_free(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_preflight_receipt")
        preflight = {"valid": True, "code": "ok", "size_bytes": 42, "sha256": "a" * 64, "gguf_version": 3}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup-preflight-receipt.json"
            summary = remote._write_preflight_receipt(path, preflight)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "verified")
            self.assertEqual(saved["schema"], remote.MODEL_PREFLIGHT_RECEIPT_SCHEMA)
            self.assertEqual(saved["sha256"], "a" * 64)
            self.assertLess(path.stat().st_size, 1024)
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "engine_model_preflight_invalid"):
                remote._write_preflight_receipt(path, {**preflight, "path": "/secret/token"})

    def test_remote_eval_preflight_always_salvages_bounded_outcome(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_outcomes")
        args = types.SimpleNamespace(engine="/approved/lae-engine", model="/approved/model")
        artifact = {"size_bytes": 42, "sha256": "a" * 64}
        outcomes = (
            ({"status": "timeout", "exit_code": None}, "timeout"),
            ({"status": "output_too_large", "exit_code": 0}, "oversize"),
            ({"status": "failed", "exit_code": -9}, "terminated"),
            ({"status": "completed", "exit_code": 0, "stdout": "not-json"}, "rejected"),
            ({"status": "failed", "exit_code": 2, "stdout": json.dumps({"valid": False, "code": "model_hash_mismatch", "size_bytes": 0, "sha256": "", "gguf_version": 0})}, "rejected"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (result, expected_status) in enumerate(outcomes):
                path = Path(directory) / f"preflight-{index}.json"
                with mock.patch.object(remote, "_run_bounded", return_value=result):
                    with self.assertRaises(ValueError):
                        remote._engine_model_preflight(args, artifact, receipt_path=path)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], expected_status)
                self.assertNotIn("stderr", json.dumps(saved))
                self.assertNotIn("/approved", json.dumps(saved))

    def test_remote_eval_ready_reader_bounds_partial_lines(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_ready_reader")
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            os.write(write_fd, b'{"port":1}')
            with self.assertRaisesRegex(ValueError, "engine_ready_timeout"):
                remote._read_ready_line(reader, time.monotonic() + 0.02)
        finally:
            reader.close()
            os.close(write_fd)

    def test_remote_eval_ready_reader_rejects_bytes_after_identity_line(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_ready_trailing")
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            os.write(write_fd, b'{"event":"ready"}\nextra\n')
            with self.assertRaisesRegex(ValueError, "engine_ready_receipt_invalid"):
                remote._read_ready_line(reader, time.monotonic() + 1.0)
        finally:
            reader.close()
            os.close(write_fd)

    def test_remote_eval_stages_share_outer_deadline_and_cleanup_reserve(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_deadline")
        with mock.patch.object(remote.time, "monotonic", return_value=100.0):
            self.assertEqual(remote._stage_timeout(600.0, 120.0, "expired"), 120.0)
            self.assertEqual(remote._stage_timeout(200.0, 300.0, "expired"), 70.0)
            with self.assertRaisesRegex(ValueError, "expired"):
                remote._stage_timeout(130.5, 30.0, "expired")
        self.assertEqual(remote.EVAL_TOTAL_TIMEOUT, 480.0)
        self.assertEqual(remote.CLEANUP_RESERVE_SECONDS, 30.0)

    def test_remote_eval_hash_and_fixture_reads_honor_deadline_and_exact_case_shape(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_bounded_reads")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.bin"
            path.write_bytes(b"fixture")
            with mock.patch.object(remote.time, "monotonic", return_value=100.0):
                with self.assertRaisesRegex(ValueError, "q4_artifact_hash_mismatch"):
                    remote.sha256(path, deadline=120.0)
            fixture = json.loads((ROOT / "tests/model/tool_call_eval.json").read_text(encoding="utf-8"))
            fixture["cases"][0]["unexpected"] = True
            bad = Path(directory) / "bad-fixture.json"
            bad.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evaluator_fixture_invalid"):
                remote._fixture_contract(bad)

    def test_remote_eval_preflight_receipt_rejects_zero_signal_and_requires_not_started_reason(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_receipt_coherence")
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_receipt_coherence")
        artifact = {"size_bytes": 4, "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup-preflight-receipt.json"
            summary = remote._write_preflight_receipt(path, status="not_started", error_code="engine_model_preflight_not_started")
            self.assertEqual(summary["status"], "not_started")
            with self.assertRaisesRegex(ValueError, "engine_model_preflight_invalid"):
                remote._write_preflight_receipt(path, status="not_started", error_code="engine_model_preflight_failed")
            path.write_text(json.dumps({"schema": remote.MODEL_PREFLIGHT_RECEIPT_SCHEMA, "status": "terminated", "error_code": "engine_model_preflight_terminated_by_signal", "child": {"signal": 0}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "child"):
                orchestrator._verify_startup_preflight_receipt(path, artifact)

    def test_remote_eval_startup_verifier_accepts_typed_not_started_outcome(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_not_started")
        artifact = {"size_bytes": 4, "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup-preflight-receipt.json"
            path.write_text(json.dumps({"schema": "local_bmo.j1m.startup-preflight-receipt.v1", "status": "not_started", "error_code": "engine_model_preflight_not_started"}), encoding="utf-8")
            self.assertEqual(orchestrator._verify_startup_preflight_receipt(path, artifact)["status"], "not_started")
            path.write_text(json.dumps({"schema": "local_bmo.j1m.startup-preflight-receipt.v1", "status": "not_started", "error_code": "engine_model_preflight_not_started", "child": {"exit_code": 0}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_startup_preflight_receipt(path, artifact)

    def test_remote_eval_startup_literals_map_to_finite_category_codes(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_stderr_categories")
        self.assertEqual(remote._exact_serve_stderr_code("exact configured CUDA device is unavailable\n"), "engine_cuda_unavailable")
        self.assertEqual(remote._exact_serve_stderr_code("llama model load failed\n"), "engine_model_load_failed")
        self.assertEqual(remote._exact_serve_stderr_code("loopback bind/listen failed\n"), "engine_bind_failed")
        native_main = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        for literal in ("engine initialization failed", "server start failed"):
            self.assertIn(f'"{literal}\\n"', native_main)
            self.assertEqual(remote._exact_serve_stderr_code(literal + "\n"), "engine_startup_failed")
        for literal, code in (("llama model load failed", "engine_model_load_failed"), ("llama context creation failed", "engine_context_failed"), ("loopback bind/listen failed", "engine_bind_failed")):
            self.assertIn(literal, native_main + (ROOT / "native/backend/llama_backend.cpp").read_text(encoding="utf-8") + (ROOT / "native/server/http_server.cpp").read_text(encoding="utf-8"))
            self.assertEqual(remote._exact_serve_stderr_code(literal + "\n"), code)
        self.assertIsNone(remote._exact_serve_stderr_code("prefix\nllama model load failed\ntrailing diagnostics"))

    def test_remote_eval_serve_eof_is_finite_and_secret_free(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_eof")

        class Child:
            def __init__(self, returncode):
                self.returncode = returncode
            def wait(self, timeout):
                return self.returncode
            def poll(self):
                return self.returncode

        exited = remote._classify_serve_eof(Child(2))
        self.assertEqual(str(exited), "engine_not_ready")
        self.assertEqual(exited.child_status, {"exit_code": 2})
        signalled = remote._classify_serve_eof(Child(-9))
        self.assertEqual(str(signalled), "engine_terminated_by_signal")
        self.assertEqual(signalled.child_status, {"signal": 9})
        self.assertEqual(str(remote._classify_serve_eof(Child(None))), "engine_ready_eof")
        self.assertEqual(str(remote._classify_serve_eof(Child(1), "context must be between 1 and 16384 tokens\n")), "engine_not_ready")
        self.assertEqual(str(remote._classify_serve_eof(Child(1), "token=super-secret /private/model\n")), "engine_not_ready")
        self.assertIsNone(remote._exact_serve_stderr_code("unknown native path /private/model"))

    def test_remote_eval_jinja_parse_errors_are_redacted(self):
        source = (ROOT / "native/backend/llama_chat_template.cpp").read_text(encoding="utf-8")
        self.assertIn('throw std::runtime_error("llama chat template parse failed")', source)
        self.assertNotIn("throw std::runtime_error(error.what())", source)

    def test_remote_eval_failure_code_is_bounded_and_secret_free(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_error_code")
        self.assertEqual(remote._safe_error_code(ValueError("engine_not_ready:token=secret-value")), "engine_not_ready")
        self.assertEqual(remote._safe_error_code(OSError("/secret/path was unavailable")), "evaluation_failed")
        for secret_like in ("secret_value", "hf_token", "password123"):
            self.assertEqual(remote._safe_error_code(ValueError(secret_like)), "evaluation_failed")

    def test_remote_eval_preserves_bounded_quality_failure_metrics(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_quality_result")
        categories = {"tool_selection"}
        failed_metrics = {
            "case_count": 2, "passed": 1, "failed": 1, "errors": 0, "peak_rss_kib": 10,
            "category_summary": {"tool_selection": {"case_count": 2, "passed": 1, "failed": 1, "errors": 0}},
        }
        parsed, all_passed, has_failure = remote._parse_evaluator_result(
            {"status": "failed", "exit_code": 1, "stdout": json.dumps(failed_metrics)},
            expected_case_count=2,
            expected_categories=categories,
        )
        self.assertEqual(parsed["failed"], 1)
        self.assertFalse(all_passed)
        self.assertTrue(has_failure)
        for mismatched in (
            {"status": "completed", "exit_code": 0, "stdout": json.dumps(failed_metrics)},
            {"status": "failed", "exit_code": 1, "stdout": json.dumps({**failed_metrics, "passed": 2, "failed": 0, "category_summary": {"tool_selection": {"case_count": 2, "passed": 2, "failed": 0, "errors": 0}}})},
        ):
            with self.assertRaisesRegex(ValueError, "exit_status_mismatch"):
                remote._parse_evaluator_result(mismatched, expected_case_count=2, expected_categories=categories)

    def test_remote_eval_verifies_same_model_manifest_identity(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_identity")
        config = load(ROOT / "scripts/j1m_runner.py", "remote_model_eval_identity_config").load_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "Qwen3.5-9B-Q4_K_M.gguf"
            model.write_bytes(b"q4 fixture")
            manifest = root / "model-manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "1.1.0",
                "source": {"organization": "Qwen", "repository": "Qwen3.5-9B", "revision": config["source"]["revision"]},
                "conversion": {"llama_cpp_revision": config["llama_cpp"]["revision"]},
                "artifact": {"expected_file_name": model.name, "expected_size_bytes": model.stat().st_size, "sha256": hashlib.sha256(model.read_bytes()).hexdigest(), "modality_profile": "text_only_no_mmproj", "quantization_profile": "Q4_K_M"},
            }), encoding="utf-8")
            (root / "model-manifest.sha256").write_text(f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  model-manifest.json\n", encoding="utf-8")
            receipt = remote.verify_artifact(model, manifest, source_revision=config["source"]["revision"], llama_revision=config["llama_cpp"]["revision"])
            self.assertEqual(receipt["llama_cpp_revision"], config["llama_cpp"]["revision"])
            manifest.write_text(manifest.read_text().replace(config["llama_cpp"]["revision"], "0" * 40), encoding="utf-8")
            with self.assertRaises(ValueError):
                remote.verify_artifact(model, manifest, source_revision=config["source"]["revision"], llama_revision=config["llama_cpp"]["revision"])

    def test_manifest_duplicate_and_oversize_snapshots_are_rejected(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_manifest_snapshot")
        prepare = load(ROOT / "scripts/test/remote_eval_prepare.py", "remote_eval_prepare_manifest_snapshot")
        config = load(ROOT / "scripts/j1m_runner.py", "remote_model_eval_manifest_snapshot_config").load_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "Qwen3.5-9B-Q4_K_M.gguf"
            model.write_bytes(b"q4 fixture")
            manifest = root / "model-manifest.json"
            duplicate = b'{"schema_version":"1.1.0","schema_version":"1.1.0"}'
            manifest.write_bytes(duplicate)
            (root / "model-manifest.sha256").write_text(f"{hashlib.sha256(duplicate).hexdigest()}  model-manifest.json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "model_manifest_invalid"):
                remote.verify_artifact(model, manifest, source_revision=config["source"]["revision"], llama_revision=config["llama_cpp"]["revision"])
            with self.assertRaisesRegex(ValueError, "remote_q4_manifest_invalid"):
                prepare.verify(model, manifest, root / "receipt.json")
            oversized = b"{" + b"\"x\":\"" + b"a" * (256 * 1024) + b"\"}"
            manifest.write_bytes(oversized)
            with self.assertRaises(ValueError):
                prepare.verify(model, manifest, root / "receipt-oversize.json")

    def test_eval_receipt_acceptance_is_hash_and_total_bound(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_eval_receipt")
        config = load(ROOT / "scripts/j1m_runner.py", "j1m_eval_receipt_config").load_config()
        artifact = {"name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 4, "sha256": "a" * 64, "source_revision": config["source"]["revision"], "llama_cpp_revision": config["llama_cpp"]["revision"], "modality": "text_only_no_mmproj", "quantization": "Q4_K_M"}
        fixture = json.loads((ROOT / "tests/model/tool_call_eval.json").read_text(encoding="utf-8"))
        category_counts = {category: sum(case["category"] == category for case in fixture["cases"]) for category in {case["category"] for case in fixture["cases"]}}
        category_summary = {category: {"case_count": count, "passed": count, "failed": 0, "errors": 0} for category, count in category_counts.items()}
        case_count = len(fixture["cases"])
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "eval-receipt.json"
            receipt.write_text(json.dumps({
                "schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "verified", "artifact": artifact,
                "engine": {"engine_version": "0.1.0", "api_version": "0.1.0", "compiled_backend": f"llama.cpp/{config['llama_cpp']['revision'][:8]}/cuda", "llama_cpp_revision": config["llama_cpp"]["revision"], "model": "qwen35-9b-q4-k-m"},
                "model_preflight": {"valid": True, "code": "ok", "status": "verified", "size_bytes": 4, "sha256": "a" * 64, "gguf_version": 3},
                "cuda_device": {"schema": "local_bmo.j1m.cuda-device-receipt.v1", "status": "verified", "selector": "CUDA0", "device_count": 1, "device": {"index": 0, "name": "NVIDIA A100 80GB", "memory_total_mib": 81920, "driver_version": "550.1"}, "source": "nvidia-smi bounded query"},
                "toolchain": {"schema": "local_bmo.j1m.remote-toolchain-receipt.v1", "status": "verified", "required": {"python3": ">=3.8", "git": ">=2.30", "cmake": ">=3.18", "g++": ">=9.0", "nvcc": ">=12.0"}, "versions": {"python3": {"major": 3, "minor": 10, "reported": "Python 3.10", "executable": "/usr/bin/python3"}, "git": {"major": 2, "minor": 39, "reported": "git version 2.39", "executable": "/usr/bin/git"}, "cmake": {"major": 3, "minor": 22, "reported": "cmake version 3.22", "executable": "/usr/bin/cmake"}, "g++": {"major": 11, "minor": 4, "reported": "g++ (Ubuntu 11.4)", "executable": "/usr/bin/g++"}, "nvcc": {"major": 12, "minor": 2, "reported": "Cuda compilation tools, release 12.2", "executable": "/usr/local/cuda/bin/nvcc"}}, "packages": {"ca-certificates": "20240101", "cmake": "3.22.1", "build-essential": "12.9", "git": "1:2.39.2", "python3": "3.10.12", "python3-venv": "3.10.12"}, "package_install": "ubuntu apt repositories; exact resolved package versions captured by dpkg-query"},
                "metrics": {"case_count": case_count, "passed": case_count, "failed": 0, "errors": 0, "peak_rss_kib": 123, "category_summary": category_summary},
                "prompt_response_logging": False, "token_logging": False,
            }), encoding="utf-8")
            selected = orchestrator._verify_eval_receipt(receipt, artifact)
            self.assertEqual(selected["metrics"]["case_count"], case_count)
            self.assertEqual(set(selected), {"status", "artifact", "engine", "model_preflight", "cuda_device", "metrics", "toolchain"})
            self.assertEqual(selected["artifact"]["modality"], "text_only_no_mmproj")
            self.assertEqual(selected["artifact"]["quantization"], "Q4_K_M")
            self.assertEqual(set(selected["toolchain"]["versions"]), {"python3", "git", "cmake", "g++", "nvcc"})
            missing_rss = json.loads(receipt.read_text(encoding="utf-8"))
            missing_rss["metrics"].pop("peak_rss_kib")
            receipt.write_text(json.dumps(missing_rss), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metrics"):
                orchestrator._verify_eval_receipt(receipt, artifact)
            missing_rss["metrics"]["peak_rss_kib"] = 123
            receipt.write_text(json.dumps(missing_rss), encoding="utf-8")
            failed = json.loads(receipt.read_text())
            failed["status"] = "completed_with_failures"
            failed["metrics"]["passed"] -= 1
            failed["metrics"]["failed"] += 1
            failed_category = next(iter(failed["metrics"]["category_summary"].values()))
            failed_category["passed"] -= 1
            failed_category["failed"] += 1
            receipt.write_text(json.dumps(failed), encoding="utf-8")
            self.assertEqual(orchestrator._verify_eval_receipt(receipt, artifact)["status"], "completed_with_failures")
            failed["status"] = "verified"
            receipt.write_text(json.dumps(failed), encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_receipt(receipt, artifact)
            failed["status"] = "completed_with_failures"
            failed["metrics"] = {"case_count": case_count, "passed": case_count, "failed": 0, "errors": 0, "peak_rss_kib": 123, "category_summary": category_summary}
            receipt.write_text(json.dumps(failed), encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_receipt(receipt, artifact)
            failed["status"] = "verified"
            failed["metrics"] = {"case_count": case_count, "passed": case_count, "failed": 0, "errors": 0, "peak_rss_kib": 123, "category_summary": category_summary}
            failed.pop("toolchain", None)
            receipt.write_text(json.dumps(failed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "toolchain"):
                orchestrator._verify_eval_receipt(receipt, artifact)
            failed["toolchain"] = json.loads(receipt.read_text(encoding="utf-8")).get("toolchain")
            failed["metrics"] = {"case_count": 8, "passed": 8, "failed": 0, "errors": 0, "peak_rss_kib": 123}
            receipt.write_text(json.dumps(failed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metrics"):
                orchestrator._verify_eval_receipt(receipt, artifact)

    def test_eval_receipts_require_separate_verified_startup_preflight(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_preflight_receipts")
        artifact = {"name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 4, "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup-preflight-receipt.json"
            path.write_text(json.dumps({"schema": "local_bmo.j1m.startup-preflight-receipt.v1", "status": "verified", "size_bytes": 4, "sha256": "a" * 64, "gguf_version": 3}), encoding="utf-8")
            self.assertEqual(orchestrator._verify_startup_preflight_receipt(path, artifact)["status"], "verified")
            for hostile in (
                {"schema": "local_bmo.j1m.startup-preflight-receipt.v1", "status": "verified", "size_bytes": 4, "sha256": "b" * 64, "gguf_version": 3},
                {"schema": "local_bmo.j1m.startup-preflight-receipt.v1", "status": "rejected", "validator_code": "/secret/path"},
            ):
                path.write_text(json.dumps(hostile), encoding="utf-8")
                with self.assertRaises(ValueError):
                    orchestrator._verify_startup_preflight_receipt(path, artifact)
            path.write_text('{"schema":"local_bmo.j1m.startup-preflight-receipt.v1","status":"verified","status":"rejected"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_startup_preflight_receipt(path, artifact)

    def test_remote_eval_metrics_reject_bool_missing_and_bad_totals(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_metrics")
        self.assertEqual(remote._validate_metrics({"case_count": 8, "passed": 8, "failed": 0, "errors": 0, "peak_rss_kib": None})["passed"], 8)
        for invalid in (
            {"case_count": 8, "passed": True, "failed": 0, "errors": 0},
            {"case_count": 8, "passed": 7, "failed": 0, "errors": 0},
            {"case_count": 8, "passed": 8, "failed": 0, "errors": 0, "peak_rss_kib": -1},
        ):
            with self.assertRaises(ValueError):
                remote._validate_metrics(invalid)

    def test_cuda_probe_writes_single_a100_placement_receipt(self):
        probe = load(ROOT / "scripts/test/cuda_device_probe.py", "cuda_device_probe_test")
        result = types.SimpleNamespace(returncode=0, stdout="0, NVIDIA A100-SXM4-80GB, 81920, 550.54.15\n")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(probe.subprocess, "run", return_value=result) as run:
            receipt = probe.probe(Path(directory) / "cuda-device-receipt.json")
            written = json.loads((Path(directory) / "cuda-device-receipt.json").read_text())
        self.assertEqual(receipt["selector"], "CUDA0")
        self.assertEqual(receipt["device_count"], 1)
        self.assertEqual(run.call_args.args[0][:2], ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version"])
        self.assertEqual(written["status"], "verified")

    def test_remote_toolchain_probe_requires_cmake_and_nvcc_versions(self):
        probe = load(ROOT / "scripts/test/remote_toolchain_probe.py", "remote_toolchain_probe_test")
        versions = {
            "python3": "Python 3.10.12\n",
            "git": "git version 2.39.2\n",
            "cmake": "cmake version 3.22.1\n",
            "g++": "g++ (Ubuntu 11.4.0) 11.4.0\n",
            "/usr/local/cuda/bin/nvcc": "Cuda compilation tools, release 12.2, V12.2.140\n",
        }
        def run(command, **_kwargs):
            if command[0] == "dpkg-query":
                return types.SimpleNamespace(returncode=0, stdout="ca-certificates=20240101\ncmake=3.22.1\nbuild-essential=12.9\ngit=1:2.39.2\npython3=3.10.12\npython3-venv=3.10.12\n", stderr="")
            return types.SimpleNamespace(returncode=0, stdout=versions[command[0]], stderr="")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(probe.subprocess, "run", side_effect=run):
            receipt = probe.probe(Path(directory) / "toolchain-receipt.json", nvcc="/usr/local/cuda/bin/nvcc")
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(json.loads((Path(directory) / "toolchain-receipt.json").read_text())["versions"]["cmake"]["major"], 3)
            self.assertEqual(receipt["packages"]["cmake"], "3.22.1")
            self.assertEqual(receipt["versions"]["nvcc"]["executable"], "/usr/local/cuda/bin/nvcc")
        with mock.patch.object(probe.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "cmake_unavailable"):
                probe._probe("cmake")
        with tempfile.TemporaryDirectory() as directory:
            refused = Path(directory) / "refused.json"
            argv = ["remote_toolchain_probe.py", "--nvcc", "/usr/local/cuda/bin/nvcc", "--output", str(refused)]
            with mock.patch("sys.argv", argv), mock.patch.object(probe.subprocess, "run", side_effect=FileNotFoundError):
                self.assertEqual(probe.main(), 2)
            refusal = json.loads(refused.read_text(encoding="utf-8"))
            self.assertEqual(refusal["status"], "refused")
            self.assertEqual(refusal["error_type"], "python3_unavailable")

    def test_eval_receipt_rejects_cpu_identity_for_cuda_lane(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_cuda_identity")
        artifact = {"name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 4, "sha256": "a" * 64, "llama_cpp_revision": "b" * 40}
        fixture = json.loads((ROOT / "tests/model/tool_call_eval.json").read_text(encoding="utf-8"))
        category_counts = {category: sum(case["category"] == category for case in fixture["cases"]) for category in {case["category"] for case in fixture["cases"]}}
        category_summary = {category: {"case_count": count, "passed": count, "failed": 0, "errors": 0} for category, count in category_counts.items()}
        case_count = len(fixture["cases"])
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "eval-receipt.json"
            receipt.write_text(json.dumps({"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "verified", "artifact": artifact, "model_preflight": {"valid": True, "code": "ok", "status": "verified", "size_bytes": 4, "sha256": "a" * 64, "gguf_version": 3}, "engine": {"llama_cpp_revision": "b" * 40, "compiled_backend": "llama.cpp/bbbbbbbb/cpu"}, "metrics": {"case_count": case_count, "passed": case_count, "failed": 0, "errors": 0, "peak_rss_kib": 1, "category_summary": category_summary}, "prompt_response_logging": False, "token_logging": False}), encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_receipt(receipt, artifact)

    def test_native_cuda_profile_is_explicit_and_fail_closed(self):
        cmake = (ROOT / "native/CMakeLists.txt").read_text(encoding="utf-8")
        backend = (ROOT / "native/backend/llama_backend.cpp").read_text(encoding="utf-8")
        main = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        self.assertIn("option(LAE_ENABLE_LLAMA_CUDA", cmake)
        self.assertIn("set(GGML_CUDA ON", cmake)
        self.assertIn("cuda_source_closure.py", cmake)
        self.assertIn('--root "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/llama.cpp"', cmake)
        self.assertIn('--manifest "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/llama.cpp/ggml-cuda-source-lock.json"', cmake)
        self.assertIn("GGML_BACKEND_DEVICE_TYPE_GPU", backend)
        self.assertIn("exact configured CUDA device is unavailable", backend)
        self.assertIn('backend == "cuda"', main)
        self.assertIn("--cuda-device-name", main)

    def test_cuda_source_closure_lock_rejects_missing_modified_and_extra_files(self):
        closure = load(ROOT / "scripts/cuda_source_closure.py", "cuda_source_closure_integrity")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "llama.cpp"
            shutil.copytree(ROOT / "vendor/llama.cpp/ggml", root / "ggml")
            shutil.copy2(ROOT / "vendor/llama.cpp/LICENSE", root / "LICENSE")
            lock = root / "ggml-cuda-source-lock.json"
            shutil.copy2(ROOT / "vendor/llama.cpp/ggml-cuda-source-lock.json", lock)
            self.assertTrue(closure.verify_closure(root, lock)["verified"])
            sample = next((root / "ggml/src/ggml-cuda").rglob("*"))
            if not sample.is_file():
                sample = next(path for path in (root / "ggml/src/ggml-cuda").rglob("*") if path.is_file())
            sample.write_bytes(sample.read_bytes() + b"tamper")
            with self.assertRaises(closure.CudaClosureError):
                closure.verify_closure(root, lock)
            sample.write_bytes((ROOT / "vendor/llama.cpp" / sample.relative_to(root)).read_bytes())
            extra = root / "ggml/src/ggml-cuda/unexpected.txt"
            extra.write_text("unexpected\n", encoding="utf-8")
            with self.assertRaises(closure.CudaClosureError):
                closure.verify_closure(root, lock)

    def test_remote_eval_main_returns_success_for_case_failures_and_emits_failed_receipt_on_shape_error(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_main_status")
        artifact = {"name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 4, "sha256": "a" * 64, "llama_cpp_revision": "b" * 40}
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "eval-receipt.json"
            args = ["--model", "m", "--model-manifest", "mm", "--model-manifest-lock", "ml", "--source-revision", "c" * 40, "--llama-revision", "b" * 40, "--llama-checkout", "checkout", "--engine", "engine", "--evaluator", "eval", "--fixture", "fixture", "--token-file", "token", "--toolchain-receipt", "toolchain", "--receipt", str(receipt)]
            failed_metrics = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "completed_with_failures", "artifact": artifact, "engine": {"llama_cpp_revision": "b" * 40, "compiled_backend": "llama.cpp/bbbbbbbb/cpu"}, "metrics": {"case_count": 8, "passed": 7, "failed": 1, "errors": 0, "peak_rss_kib": 1}, "prompt_response_logging": False, "token_logging": False}
            with mock.patch.object(remote, "verify_artifact", return_value=artifact), mock.patch.object(remote, "_launch_and_evaluate", return_value=failed_metrics):
                self.assertEqual(remote.main(args), 0)
            self.assertEqual(json.loads(receipt.read_text())["status"], "completed_with_failures")
            with mock.patch.object(remote, "verify_artifact", return_value=artifact), mock.patch.object(remote, "_launch_and_evaluate", side_effect=KeyError("metrics")):
                self.assertEqual(remote.main(args), 1)
            malformed = json.loads(receipt.read_text())
            self.assertEqual(malformed["status"], "failed")
            self.assertNotIn("metrics", malformed)
            startup = remote.EngineStartupFailure("engine_not_ready", {"exit_code": 2})
            with mock.patch.object(remote, "verify_artifact", return_value=artifact), mock.patch.object(remote, "_launch_and_evaluate", side_effect=startup):
                self.assertEqual(remote.main(args), 1)
            startup_receipt = json.loads(receipt.read_text())
            self.assertEqual(startup_receipt["error_code"], "engine_not_ready")
            self.assertEqual(startup_receipt["child"], {"exit_code": 2})

            def fail_after_preflight(call_args, _artifact, **_kwargs):
                call_args._preflight_summary = {"status": "verified", "size_bytes": 42, "sha256": "a" * 64, "gguf_version": 3}
                raise ValueError("engine_ready_timeout:/private/token")

            with mock.patch.object(remote, "verify_artifact", return_value=artifact), mock.patch.object(remote, "_launch_and_evaluate", side_effect=fail_after_preflight):
                self.assertEqual(remote.main(args), 1)
            safe_receipt = json.loads(receipt.read_text())
            self.assertEqual(safe_receipt["preflight"]["status"], "verified")
            self.assertNotIn("private", json.dumps(safe_receipt))


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
        self.assertEqual(result["error_type"], "TimeoutExpired")

    def test_remote_stderr_is_bounded_and_redacted(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_stderr")
        result = orchestrator._remote([os.sys.executable, "-c", "import sys; sys.stderr.write('api_key=secret-value ' * 300); sys.exit(3)"], timeout=5)
        self.assertEqual(result["status"], "failed")
        self.assertLessEqual(len(result["stderr_tail"]), 1200)
        self.assertNotIn("secret-value", result["stderr_tail"])
        self.assertIn("<redacted>", result["stderr_tail"])

    def test_provider_endpoint_rejects_unsafe_identity_inputs(self):
        from scripts import shadeform_lifecycle as sf
        with self.assertRaises(sf.ShadeformError):
            sf.ssh_base({"ip": "not-an-ip", "ssh_user": "ubuntu", "ssh_port": 22}, Path("/tmp/id"), Path("/tmp/known"))
        with self.assertRaises(sf.ShadeformError):
            sf.ssh_base({"ip": "127.0.0.1", "ssh_user": "ubuntu;rm", "ssh_port": 22}, Path("/tmp/id"), Path("/tmp/known"))
        with self.assertRaises(sf.ShadeformError):
            sf.ssh_base({"ip": "127.0.0.1", "ssh_user": "ubuntu", "ssh_port": 70000}, Path("/tmp/id"), Path("/tmp/known"))


if __name__ == "__main__":
    unittest.main()
