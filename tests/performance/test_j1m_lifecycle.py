import importlib.util
import hashlib
import io
import json
import os
import subprocess
import stat
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
            attempt_id = sf.reserve_create_attempt("j1m-reservation-test", "c" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64)
            event = json.loads(Path(directory, "cost-ledger.jsonl").read_text().strip())
            self.assertEqual(attempt_id, "attempt-" + "c" * 32)
            self.assertEqual(event["status"], "pending")
            self.assertEqual(event["estimated_cost_usd"], 0.421875)
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
            (root / "source-model-receipt.json").write_text(json.dumps({"status": "verified", "revision": "a" * 40, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64}), encoding="utf-8")
            (root / "tensor-metadata.json").write_text(json.dumps({"status": "verified", "tensor_count": 3}), encoding="utf-8")
            (root / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": "e" * 40}), encoding="utf-8")
            (root / "command-receipt.json").write_text(json.dumps([{"stage": 1, "argv": ["source-check"], "started_at_utc": "2026-01-01T00:00:00+00:00", "ended_at_utc": "2026-01-01T00:00:01+00:00", "exit_code": 0, "status": "completed"}]) + "\n", encoding="utf-8")
            (root / "scan-receipt.json").write_text(json.dumps({"status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "artifacts": scan_records}), encoding="utf-8")
            manifest = self.j1m.write_artifacts(root, names)
            self.assertEqual(len(manifest["artifacts"]), 9)
            self.assertEqual(manifest["inventory_scope"], "post_cleanup_deployable_allowlist")
            self.assertEqual(manifest["deployable_model_artifacts"], ["Qwen3.5-9B-Q4_K_M.gguf"])
            self.assertEqual(manifest["tensor_metadata"]["status"], "verified")
            self.assertNotEqual(manifest["tensor_metadata"]["status"], "pending_converter_receipt")
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
            (remote / "command-receipt.json").write_text(json.dumps([{"stage": 1, "argv": ["source-check"], "started_at_utc": "2026-01-01T00:00:00+00:00", "ended_at_utc": "2026-01-01T00:00:01+00:00", "exit_code": 0, "status": "completed"}]) + "\n", encoding="utf-8")
            scan_records = [{"name": name, "size_bytes": (remote / name).stat().st_size, "sha256": hashlib.sha256((remote / name).read_bytes()).hexdigest()} for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf")]
            (remote / "scan-receipt.json").write_text(json.dumps({"status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "artifacts": scan_records}), encoding="utf-8")
            q4 = remote / "Qwen3.5-9B-Q4_K_M.gguf"
            (remote / "post-cleanup-receipt.json").write_text(json.dumps({"status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": [q4.name], "q4": {"size_bytes": q4.stat().st_size, "sha256": hashlib.sha256(q4.read_bytes()).hexdigest()}}), encoding="utf-8")
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

        class ReaderField:
            def __init__(self, value):
                self.value = value
            def contents(self):
                return self.value

        class Tensor:
            name = "blk.0.attn.weight"
            shape = [2, 2]
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
            self.assertGreater(selected["transfer_reserve_seconds"], 0)
        self.assertEqual(config["resources"]["expected_q4_gib"], 6)

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
                orchestrator._salvage(info, identity, known_hosts, destination, ["Qwen3.5-9B-Q4_K_M.gguf"], q4_expected_gib=6, deadline=time.monotonic() + 100)
            self.assertLessEqual(remote.call_args.kwargs["timeout"], 70)

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
