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
import time
import types
import urllib.error
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

from tests.performance.lifecycle_test_isolation import (
    direct_execute_methods,
    isolated_lifecycle_execute,
)

ROOT = Path(__file__).resolve().parents[2]


# Every receipt the bounded salvage transport can carry must declare the run
# that produced it; ``j1m_runner`` stamps these from the uploaded
# ``run-identity.json``, and fixtures must therefore carry them too.
RUN_IDENTITY = {"run_id": "unbound", "instance_id": "unbound"}


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stored_cost_event(sf, event):
    canonical = sf._canonical_cost_event(event, stored=False)
    canonical["recorded_at_utc"] = "2026-01-01T00:00:00+00:00"
    return sf._canonical_cost_event(canonical, stored=True)


def write_test_cost_genesis(sf, path: Path, *, cap: float = 50.0) -> None:
    genesis = sf._canonical_cost_genesis({
        "schema": sf.COST_EVENT_SCHEMA,
        "event_kind": "genesis",
        "program": sf.COST_LEDGER_PROGRAM,
        "currency": sf.COST_LEDGER_CURRENCY,
        "budget_cap_usd": cap,
        "prior_settled_spend_usd": 0.0,
        "current_pending_owner_count": 0,
        "display_ledger_sha256": "1" * 64,
        "incidents_sha256": "2" * 64,
        "recorded_at_utc": "2026-01-01T00:00:00+00:00",
    })
    path.write_text(
        json.dumps(genesis, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def private_config(root: Path) -> Path:
    """Copy read-only config into the fixture's owner-private trust root."""

    target = root / "j1m-config.json"
    shutil.copy2(ROOT / "model/conversion/j1m-config.json", target)
    target.chmod(0o600)
    return target


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
        manifest_command = next(command for command in remote_plan if "--manifest" in command)
        self.assertEqual(manifest_command[manifest_command.index("--lock") + 1], str(self.j1m.SOURCE_LOCK))

    def test_utility_mode_does_not_require_default_config(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, mock.patch.object(self.j1m, "DEFAULT_CONFIG", Path(directory) / "missing.json"), mock.patch.object(self.j1m, "check_scratch", return_value={"status": "ok"}) as check:
            self.assertEqual(self.j1m.main(["--scratch", directory, "--min-scratch-gib", "1"]), 0)
            check.assert_called_once()

    def test_manifest_handler_passes_explicit_source_lock_to_writer_and_plan(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            custom_lock = Path(directory) / "uploaded.source-lock.json"
            output = Path(directory) / "artifacts"
            with mock.patch.object(self.j1m, "write_artifacts") as writer:
                self.assertEqual(self.j1m.main(["--manifest", str(output), "--lock", str(custom_lock)]), 0)
            writer.assert_called_once()
            call = writer.call_args.kwargs
            self.assertEqual(call["source_lock"], custom_lock)
            manifest_commands = [command for command in call["commands"] if "--manifest" in command]
            self.assertEqual(len(manifest_commands), 1)
            self.assertEqual(manifest_commands[0][manifest_commands[0].index("--lock") + 1], str(custom_lock))

    def test_hf_token_file_is_private_and_removed(self):
        with self.j1m.hf_token_file("test-token-never-logged") as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("HF_TOKEN=", path.read_text())
        self.assertFalse(path.exists())

    def test_dependency_wheelhouse_lock_is_hash_verified(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir(mode=0o700)
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
        with mock.patch.object(sf, "read_owned_resource", return_value=None), \
                mock.patch.object(sf, "request", return_value={"status": "accepted"}):
            with self.assertRaises(sf.AmbiguousProviderOutcome):
                sf.create_instance("api", {}, phase_id="j1m-create-test", run_id="run", candidate=candidate, ssh_key_id="key-123456", nonce="a" * 32, max_runtime_hours=0.25)
        with mock.patch.object(sf, "read_owned_resource", return_value=None), \
                mock.patch.object(sf, "request", side_effect=sf.ShadeformHTTPError(400, "rejected")):
            with self.assertRaises(sf.ShadeformHTTPError):
                sf.create_instance("api", {}, phase_id="j1m-create-test", run_id="run", candidate=candidate, ssh_key_id="key-123456", nonce="b" * 32, max_runtime_hours=0.25)

    def test_create_attempt_reservation_is_durable_gate(self):
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100", "cloud", "region", "a100-80", 1.35, 80, "ubuntu", False)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, mock.patch.object(sf, "COST_LEDGER", Path(directory).resolve() / "cost-ledger.jsonl"):
            write_test_cost_genesis(sf, sf.COST_LEDGER)
            fingerprint = sf.ssh_public_key_fingerprint("ssh-ed25519 AAAA")
            attempt_id = sf.reserve_create_attempt("j1m-reservation-test", "c" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64, expected_budget_cap_usd=50.0, public_key_fingerprint=fingerprint)
            event = json.loads(Path(directory, "cost-ledger.jsonl").read_text().splitlines()[-1])
            self.assertEqual(attempt_id, "attempt-" + "c" * 32)
            self.assertEqual(event["status"], "pending")
            self.assertEqual(event["estimated_cost_usd"], 0.421875)
            self.assertEqual(event["ssh_public_key_fingerprint"], fingerprint)
            sf.reserve_create_attempt("j1m-reservation-test", "c" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64, expected_budget_cap_usd=50.0, public_key_fingerprint=fingerprint, ssh_key_id="key-123456")
            enriched = json.loads(Path(directory, "cost-ledger.jsonl").read_text().splitlines()[-1])
            self.assertEqual(enriched["ssh_key_id"], "key-123456")
            with mock.patch.object(sf, "_append_reserved_cost_event", side_effect=OSError("ledger unavailable")):
                with self.assertRaises(OSError):
                    sf.reserve_create_attempt("j1m-reservation-test", "e" * 32, candidate, backstop_hours=0.3125, public_key_sha256="d" * 64, expected_budget_cap_usd=50.0)

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
            self.assertEqual(sf._delete_ssh_key_once("api", "j1m-key-test", reconciled), {"deleted": True})
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

    def test_reconciliation_requests_honor_hard_deadline(self):
        from scripts import shadeform_lifecycle as sf
        nonce = "0123456789abcdef0123456789abcdef"
        public_key = "ssh-ed25519 AAAA"
        request_timeouts = []

        def request(*args, **kwargs):
            request_timeouts.append(kwargs["timeout"])
            if args[2] == "/sshkeys":
                return {"ssh_keys": [{"id": "key-recon-1", "name": f"j1m-{nonce}", "public_key": public_key}]}
            if args[2] == "/sshkeys/key-recon-1/info":
                return {"id": "key-recon-1", "name": f"j1m-{nonce}", "public_key": public_key}
            if args[2] == "/instances":
                return {"instances": [{"id": "instance-recon-1", "name": f"ep-run-{nonce}", "tags": ["local-bmo-j1m", "ep-phase-phase-a", f"ep-run-{nonce}"]}]}
            if args[2] == "/instances/instance-recon-1/info":
                return {"id": "instance-recon-1", "name": f"ep-run-{nonce}", "tags": ["local-bmo-j1m", "ep-phase-phase-a", f"ep-run-{nonce}"], "ssh_key_id": "key-recon-1", "cloud": "hyperstack", "region": "r", "shade_instance_type": "a100", "hourly_price": 100, "configuration": {"gpu_type": "A100", "num_gpus": 1, "vram_per_gpu_in_gb": 80, "os": "ubuntu"}}
            raise AssertionError(args[2])

        with mock.patch.object(sf, "request", side_effect=request), mock.patch.object(sf.time, "monotonic", return_value=100.0):
            self.assertEqual(sf.reconcile_ssh_key("api", "phase-a", expected_name=f"j1m-{nonce}", expected_public_key=public_key, deadline=105.0), "key-recon-1")
            self.assertEqual(sf.reconcile_instance_by_nonce(
                "api", "phase-a", expected_name=f"ep-run-{nonce}", nonce=nonce,
                ssh_key_id="key-recon-1", expected_cloud="hyperstack", expected_region="r",
                expected_instance_type="a100", expected_hourly_usd=1.0, expected_gpu="A100",
                expected_gpu_count=1, expected_vram_gb=80, expected_os_image="ubuntu", deadline=105.0,
            ), "instance-recon-1")
        self.assertTrue(request_timeouts)
        self.assertLessEqual(max(request_timeouts), 5.0)

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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "Qwen3.5-9B-Q4_K_M.gguf").write_bytes(b"q4")
            (root / "Qwen3.5-9B-mmproj.gguf").write_bytes(b"vision")
            with self.assertRaises(ValueError):
                self.j1m.post_cleanup_verify(root)
            (root / "Qwen3.5-9B-mmproj.gguf").unlink()
            self.assertEqual(self.j1m.post_cleanup_verify(root)["remaining_gguf"], ["Qwen3.5-9B-Q4_K_M.gguf"])

    def test_artifact_allowlist_rejects_traversal(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ok.gguf").write_bytes(b"fixture")
            with self.assertRaises(ValueError):
                self.j1m.artifact_manifest(root, ["../ok.gguf"])

    def test_receipts_are_created_without_manifest_self_reference(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
            (root / "source-model-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified", "model_id": lock["model_id"], "revision": lock["revision"], "checked_files": list(source_hashes), "file_hashes": source_hashes, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64, "verified_at_utc": "2026-01-01T00:00:00+00:00", **RUN_IDENTITY}), encoding="utf-8")
            (root / "tensor-metadata.json").write_text(json.dumps({"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": 0, "tensors": [], "gguf_metadata": {"general.architecture": "qwen35"}, "vision_projection_present": False, "chat_template_sha256": "c" * 64, **RUN_IDENTITY}), encoding="utf-8")
            (root / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": "e" * 40, "python": "Python 3.11", "cmake": "cmake 3.28", "compiler": "cc 12", "os_packages": [], "pip_freeze": "", "dependency_wheelhouse_lock": {}, **RUN_IDENTITY}), encoding="utf-8")
            (root / "command-receipt.json").write_text(json.dumps([{"stage": 1, "argv": ["source-check"], "started_at_utc": "2026-01-01T00:00:00+00:00", "ended_at_utc": "2026-01-01T00:00:01+00:00", "exit_code": 0, "status": "completed"}]) + "\n", encoding="utf-8")
            (root / "scan-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True, "artifacts": scan_records, "vision_projection_present": False, **RUN_IDENTITY}), encoding="utf-8")
            (root / "post-cleanup-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": ["Qwen3.5-9B-Q4_K_M.gguf"], "forbidden_artifacts": [], "q4": {"size_bytes": (root / "Qwen3.5-9B-Q4_K_M.gguf").stat().st_size, "sha256": hashlib.sha256((root / "Qwen3.5-9B-Q4_K_M.gguf").read_bytes()).hexdigest()}, **RUN_IDENTITY}), encoding="utf-8")
            manifest = self.j1m.write_artifacts(root, names)
            self.assertEqual(len(manifest["artifacts"]), 9)
            self.assertEqual(manifest["inventory_scope"], "post_cleanup_deployable_allowlist")
            self.assertEqual(manifest["deployable_model_artifacts"], ["Qwen3.5-9B-Q4_K_M.gguf"])
            self.assertEqual(manifest["tensor_metadata"]["status"], "verified")
            self.assertNotEqual(manifest["tensor_metadata"]["status"], "pending_converter_receipt")
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "checksums.sha256").is_file())
            command_payload = json.loads((root / "command-receipt.json").read_text(encoding="utf-8"))
            command_payload[0]["exit_code"] = False
            (root / "command-receipt.json").write_text(json.dumps(command_payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "command receipt status"):
                self.j1m.write_artifacts(root, names)
            command_payload[0]["exit_code"] = 0
            (root / "command-receipt.json").write_text(json.dumps(command_payload), encoding="utf-8")
            forged = json.loads((root / "source-model-receipt.json").read_text(encoding="utf-8"))
            forged["status"] = "verified"
            forged["file_hashes"][next(iter(forged["file_hashes"]))] = "f" * 64
            (root / "source-model-receipt.json").write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source receipt hash"):
                self.j1m.write_artifacts(root, names)

    def test_deployable_bundle_remains_verifiable_without_intermediates(self):
        fetch = load(ROOT / "scripts/j1m_fetch.py", "j1m_fetch_bundle")
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            remote = Path(directory) / "remote"
            local = Path(directory) / "local"
            remote.mkdir(mode=0o700)
            for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"):
                (remote / name).write_bytes(name.encode())
            lock = json.loads((ROOT / "model" / "source-lock" / "qwen35-9b.source-lock.json").read_text(encoding="utf-8"))
            source_hashes = {item["path"]: item.get("sha256") or item.get("lfs_sha256") for item in lock["source_files"] if not item.get("excluded_from_text_only") and (item.get("sha256") or item.get("lfs_sha256"))}
            (remote / "source-model-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified", "model_id": lock["model_id"], "revision": lock["revision"], "checked_files": list(source_hashes), "file_hashes": source_hashes, "tokenizer_sha256": "b" * 64, "chat_template_sha256": "c" * 64, "license_sha256": "d" * 64, "verified_at_utc": "2026-01-01T00:00:00+00:00", **RUN_IDENTITY}), encoding="utf-8")
            (remote / "tensor-metadata.json").write_text(json.dumps({"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": 0, "tensors": [], "gguf_metadata": {"general.architecture": "qwen35"}, "vision_projection_present": False, "chat_template_sha256": "c" * 64, **RUN_IDENTITY}), encoding="utf-8")
            (remote / "toolchain.json").write_text(json.dumps({"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": "e" * 40, "python": "Python 3.11", "cmake": "cmake 3.28", "compiler": "cc 12", "os_packages": [], "pip_freeze": "", "dependency_wheelhouse_lock": {}, **RUN_IDENTITY}), encoding="utf-8")
            (remote / "command-receipt.json").write_text(json.dumps([{"stage": 1, "argv": ["source-check"], "started_at_utc": "2026-01-01T00:00:00+00:00", "ended_at_utc": "2026-01-01T00:00:01+00:00", "exit_code": 0, "status": "completed"}]) + "\n", encoding="utf-8")
            scan_records = [{"name": name, "size_bytes": (remote / name).stat().st_size, "sha256": hashlib.sha256((remote / name).read_bytes()).hexdigest()} for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf")]
            (remote / "scan-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True, "artifacts": scan_records, "vision_projection_present": False, **RUN_IDENTITY}), encoding="utf-8")
            q4 = remote / "Qwen3.5-9B-Q4_K_M.gguf"
            (remote / "post-cleanup-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": [q4.name], "forbidden_artifacts": [], "q4": {"size_bytes": q4.stat().st_size, "sha256": hashlib.sha256(q4.read_bytes()).hexdigest()}, **RUN_IDENTITY}), encoding="utf-8")
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "artifacts"
            output.mkdir(mode=0o700)
            (output / "pip-freeze.txt").write_text("example-package==1.2.3\n", encoding="utf-8")
            self.j1m.main(["--toolchain", str(output / "toolchain.json"), "--llama-checkout", str(ROOT)])
            receipt = json.loads((output / "toolchain.json").read_text())
            self.assertIn("example-package==1.2.3", receipt["pip_freeze"])

    def test_readerfield_contents_and_source_chat_template_hash_are_verified(self):
        chat_template = "<bos>{{ messages[0]['content'] }}"

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

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            receipt_path = root / "command-receipt.json"
            command = [os.sys.executable, "-c", "import sys; print('token' + chr(61) + 'do-not-retain ' * 300, file=sys.stderr); raise SystemExit(7)"]
            result = self.j1m.run_commands([command], root / "progress.json", receipt_path=receipt_path)
            self.assertEqual(result[0]["exit_code"], 7)
            self.assertLessEqual(len(result[0]["stderr_tail"]), 1200)
            self.assertNotIn("do-not-retain", result[0]["stderr_tail"])
            self.assertIn("<redacted>", result[0]["stderr_tail"])

    def test_missing_executable_is_persisted_as_stage_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            receipt_path = root / "command-receipt.json"
            result = self.j1m.run_commands([["j1m-executable-that-does-not-exist"]], root / "progress.json", receipt_path=receipt_path)
            self.assertEqual(result[0]["status"], "launch_failed")
            self.assertEqual(result[0]["error_type"], "launch_failed")
            persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[0]["status"], "launch_failed")

    def test_hf_token_is_injected_only_into_download_stage(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            private, public = sf.create_ephemeral_ssh_key({"SHADEFORM_SSH": "provider-uuid-123456789012345678901234"}, Path(directory) / "nested" / "ssh")
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)
            self.assertTrue(public.startswith("ssh-ed25519 "))


class StaticSafetyTests(unittest.TestCase):
    def test_all_j1m_execute_regressions_isolate_operator_runtime(self):
        found = direct_execute_methods(ROOT / "tests" / "performance")
        # 12 lifecycle regressions plus the MODEL-COMPARATOR-EVAL-001
        # pre-spend refusal in tests/performance/test_comparator_eval.py.
        self.assertEqual(len(found), 13)
        self.assertTrue(
            all(item["isolated"] for item in found),
            [item for item in found if not item["isolated"]],
        )

    def test_native_context_batch_policy_keeps_logical_window_and_bounded_microbatch(self):
        header = (ROOT / "native/backend/llama_backend.hpp").read_text(encoding="utf-8")
        source = (ROOT / "native/backend/llama_backend.cpp").read_text(encoding="utf-8")
        self.assertIn("ContextBatchConfig context_batch_config(unsigned context_tokens);", header)
        self.assertIn("ContextBatchConfig{n_ctx, n_ctx, std::min(n_ctx, 512u)}", source)
        self.assertIn("context_params.n_batch = batch_config.n_batch", source)
        self.assertIn("context_params.n_ubatch = batch_config.n_ubatch", source)
        self.assertIn("batch_config.n_ctx", source)
        self.assertIn("llama_n_batch(impl_->context)", source)
        self.assertIn("llama_n_ubatch(impl_->context)", source)

    def test_remote_eval_rejects_engine_exit_after_valid_evaluator_output(self):
        source = (ROOT / "scripts/test/remote_model_eval.py").read_text(encoding="utf-8")
        self.assertIn('status = "failed" if child_status is not None', source)
        self.assertIn('"child": child_status', source)
        self.assertIn('require_diagnostics=True', source)

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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, mock.patch.object(sf.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)):
            receipt = sf.acquire_pinned_host_key({"ip": "127.0.0.1", "ssh_port": 2222, "ssh_user": "u"}, Path(directory) / "known_hosts")
            self.assertEqual(receipt["proof"], "two-stable-bounded-scans-residual-tofu")
            self.assertEqual(receipt["fingerprint"], "SHA256:stable-fingerprint")

    def test_host_key_multi_algorithm_set_is_stable_and_provider_fingerprint_selects_one(self):
        from scripts import shadeform_lifecycle as sf
        keys = "[127.0.0.1]:2222 ssh-ed25519 AAAAED\n[127.0.0.1]:2222 ecdsa-sha2-nistp256 AAAAEC\n"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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

    def test_malformed_owned_ledger_refuses_before_provider_delete(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "malformed-owned-ledger"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            original_root = sf.RUNTIME_ROOT
            sf.RUNTIME_ROOT = root / "runtime"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            try:
                payload = {
                    "phase_id": phase, "run_id": "test", "instance_id": "instance-owned-1",
                    "ownership_nonce": "0123456789abcdef0123456789abcdef", "ssh_key_id": "key-owned-1",
                    "ssh_key_name": "key", "gpu": "A100", "cloud": "hyperstack", "region": "r",
                    "hourly_usd": 1.0, "created_at_utc": sf.utc_now().isoformat(), "status": "created",
                }
                ledger = sf.runtime_ledger_path(phase)
                for field, value in (("created_at_utc", "2026-01-01T00:00:00"), ("hourly_usd", float("nan"))):
                    invalid = {**payload, field: value}
                    ledger.write_text(json.dumps(invalid), encoding="utf-8")
                    with mock.patch.object(teardown.shadeform, "_delete_instance") as delete:
                        with self.assertRaises(sf.ShadeformError):
                            teardown.teardown_exact(phase, "instance-owned-1", env_file=root / "missing.env")
                    delete.assert_not_called()
            finally:
                sf.RUNTIME_ROOT = original_root

    def test_cost_bookkeeping_failure_retains_key_for_exact_retry(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "cost-bookkeeping-key-cleanup"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            sf.write_owned_resource(sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-cost-1", ownership_nonce="0123456789abcdef0123456789abcdef",
                ssh_key_id="key-cost-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0,
                created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1,
                vram_gb=80, os_image="ubuntu", ssh_public_key="ssh-ed25519 AAAA",
            ))
            try:
                with mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "verify_owned_ssh_key_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "_delete_instance", return_value={"success": True}), \
                        mock.patch.object(teardown.shadeform, "append_cost_event", side_effect=ValueError("ledger shape")), \
                        mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}) as key_delete:
                    receipt = teardown.teardown_exact(phase, "instance-cost-1", env_file=env)
                key_delete.assert_not_called()
                self.assertEqual(receipt["cost_bookkeeping_error_type"], "ValueError")
                self.assertTrue(receipt["key_cleanup_deferred"])
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_post_key_receipt_failure_retains_owned_record_for_manual_retry(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "post-key-receipt-failure"
        nonce = "0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            record = sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-post-receipt-1", ownership_nonce=nonce,
                ssh_key_id="key-post-receipt-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r",
                hourly_usd=1.0, created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1,
                vram_gb=80, os_image="ubuntu", ssh_public_key="ssh-ed25519 AAAA",
            )
            try:
                sf.write_owned_resource(record)
                real_receipt = teardown._write_deletion_receipt
                receipt_calls = 0

                def receipt_with_second_write_failure(*args, **kwargs):
                    nonlocal receipt_calls
                    receipt_calls += 1
                    if receipt_calls == 2:
                        raise OSError("post-key receipt barrier failed")
                    return real_receipt(*args, **kwargs)

                with mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "_delete_instance", return_value={"success": True}), \
                        mock.patch.object(teardown.shadeform, "append_cost_event"), \
                        mock.patch.object(teardown.shadeform, "verify_owned_ssh_key_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}) as key_delete, \
                        mock.patch.object(teardown, "_write_deletion_receipt", side_effect=receipt_with_second_write_failure), \
                        mock.patch.object(teardown.shadeform, "clear_owned_resource") as clear:
                    with self.assertRaises(RuntimeError):
                        teardown.teardown_exact(phase, record.instance_id, env_file=env)
                key_delete.assert_called_once()
                clear.assert_not_called()
                self.assertIsNotNone(sf.read_owned_resource(phase))
                self.assertGreaterEqual(receipt_calls, 2)
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_delete_dispatch_requires_durable_intent_before_provider_call(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "dispatch-intent-required"
        record = sf.OwnedResource(
            phase_id=phase, run_id="test", instance_id="instance-intent-1", ownership_nonce="0123456789abcdef0123456789abcdef",
            ssh_key_id="key-intent-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0,
            created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1, vram_gb=80, os_image="ubuntu",
            ssh_public_key="ssh-ed25519 AAAA",
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            try:
                sf.write_owned_resource(record)
                with mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                        mock.patch.object(teardown, "_write_deletion_intent", side_effect=OSError("intent fsync failed")), \
                        mock.patch.object(teardown.shadeform, "_delete_instance") as delete:
                    with self.assertRaises(RuntimeError):
                        teardown.teardown_exact(phase, record.instance_id, env_file=env)
                delete.assert_not_called()
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_lifecycle_persistence_uses_directory_barriers(self):
        from scripts import shadeform_lifecycle as sf
        phase = "persistence-directory-barrier"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS = root / "runtime", root / "ledger.md", root / "cost.jsonl", root / "incidents.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            sf.MARKDOWN_LEDGER.chmod(0o600)
            write_test_cost_genesis(sf, sf.COST_LEDGER)
            record = sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-barrier-1", ownership_nonce="fedcba9876543210fedcba9876543210",
                ssh_key_id="key-barrier-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0,
                created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1, vram_gb=80, os_image="ubuntu",
                ssh_public_key="ssh-ed25519 AAAA",
            )
            barriers = []
            try:
                with mock.patch.object(sf, "_fsync_directory", side_effect=lambda path: barriers.append(Path(path))):
                    sf.write_owned_resource(record)
                    sf.append_cost_event({"instance_id": record.instance_id, "phase_id": phase, "ownership_nonce": record.ownership_nonce, "status": "pending", "estimated_cost_usd": 0.0})
                    sf.append_cost_event({"instance_id": record.instance_id, "phase_id": phase, "ownership_nonce": record.ownership_nonce, "status": "settled", "actual_cost_usd": 0.0})
                    sf.append_incident({"incident": "barrier-test", "phase_id": phase})
                self.assertGreaterEqual(len(barriers), 2)
                self.assertTrue(all(path == root / "runtime" or path == root for path in barriers))
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER, sf.INCIDENTS = originals

    def test_confirmed_delete_retry_skips_provider_delete(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "confirmed-delete-retry"
        nonce = "fedcba9876543210fedcba9876543210"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            record = sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-retry-1", ownership_nonce=nonce,
                ssh_key_id="key-retry-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r",
                hourly_usd=1.0, created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1,
                vram_gb=80, os_image="ubuntu", ssh_public_key="ssh-ed25519 AAAA",
            )
            try:
                sf.write_owned_resource(record)
                delete = mock.Mock(return_value={"success": True})
                with mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "_delete_instance", delete), \
                        mock.patch.object(teardown.shadeform, "append_cost_event", side_effect=OSError("ledger unavailable")), \
                        mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact") as key_delete:
                    first = teardown.teardown_exact(phase, record.instance_id, env_file=env)
                self.assertTrue(first["retry_required"])
                key_delete.assert_not_called()
                with mock.patch.object(teardown.shadeform, "append_cost_event"), \
                        mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete") as verify, \
                        mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
                    second = teardown.teardown_exact(phase, record.instance_id, env_file=env)
                self.assertTrue(second["actual_cost_usd"] >= 0)
                delete.assert_called_once()
                verify.assert_not_called()
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_dispatched_delete_intent_reconciles_exact_404_only(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "dispatch-intent-reconcile"
        nonce = "00112233445566778899aabbccddeeff"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            record = sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-dispatch-1", ownership_nonce=nonce,
                ssh_key_id="key-dispatch-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r",
                hourly_usd=1.0, created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1,
                vram_gb=80, os_image="ubuntu", ssh_public_key="ssh-ed25519 AAAA",
            )
            try:
                sf.write_owned_resource(record)
                delete = mock.Mock(side_effect=RuntimeError("connection lost after dispatch"))
                with mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "_delete_instance", delete):
                    with self.assertRaises(RuntimeError):
                        teardown.teardown_exact(phase, record.instance_id, env_file=env)
                intent = json.loads(teardown._deletion_intent_path(phase, record).read_text(encoding="utf-8"))
                self.assertEqual(intent["status"], "dispatched")
                with mock.patch.object(teardown.shadeform, "instance_info", side_effect=sf.ShadeformHTTPError(404, "gone")), \
                        mock.patch.object(teardown.shadeform, "_delete_instance") as second_delete, \
                        mock.patch.object(teardown.shadeform, "append_cost_event"), \
                        mock.patch.object(teardown.shadeform, "verify_owned_ssh_key_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}):
                    result = teardown.teardown_exact(phase, record.instance_id, env_file=env)
                self.assertTrue(result["deletion"]["reconciled_from_deletion_intent"])
                second_delete.assert_not_called()
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_teardown_settles_attempt_reservation_before_key_cleanup(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "attempt-settlement-order"
        nonce = "0123456789abcdef0123456789abcdef"
        record = sf.OwnedResource(
            phase_id=phase, run_id="test", instance_id="instance-order-1",
            ownership_nonce=nonce, ssh_key_id="key-order-1", ssh_key_name="key",
            gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0,
            created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100",
            gpu_count=1, vram_gb=80, os_image="ubuntu",
            ssh_public_key="ssh-ed25519 AAAA",
        )
        calls = []
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
            sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            try:
                with mock.patch.object(teardown.shadeform, "read_owned_resource", return_value=record), \
                        mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                        mock.patch.object(teardown.shadeform, "_delete_instance", return_value={"success": True}), \
                        mock.patch.object(teardown.shadeform, "append_cost_event", side_effect=lambda event: calls.append(("cost", event["instance_id"]))), \
                        mock.patch.object(teardown.shadeform, "write_owned_resource", side_effect=lambda value: calls.append(("record", value.status))), \
                        mock.patch.object(teardown.shadeform, "verify_owned_ssh_key_before_delete", side_effect=lambda *args, **kwargs: calls.append(("key-verify", args[2]))), \
                        mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact", side_effect=lambda *args, **kwargs: (calls.append(("key-delete", args[2])) or {"status": "confirmed"})), \
                        mock.patch.object(teardown, "_write_deletion_receipt", side_effect=lambda *args, **kwargs: calls.append(("receipt", args[1]["instance_id"]))), \
                        mock.patch.object(teardown.shadeform, "clear_owned_resource"):
                    receipt = teardown.teardown_exact(phase, record.instance_id, env_file=env)
                self.assertTrue(receipt["attempt_reservation_settled"])
                self.assertLess(calls.index(("cost", record.instance_id)), calls.index(("cost", f"attempt-{nonce}")))
                self.assertLess(
                    calls.index(("cost", f"attempt-{nonce}")),
                    next(index for index, item in enumerate(calls) if item[0] == "key-delete"),
                )
            finally:
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_teardown_retains_key_when_attempt_or_record_persistence_fails(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        phase = "teardown-bookkeeping-faults"
        nonce = "fedcba9876543210fedcba9876543210"
        record = sf.OwnedResource(
            phase_id=phase, run_id="test", instance_id="instance-bookkeeping-1",
            ownership_nonce=nonce, ssh_key_id="key-bookkeeping-1", ssh_key_name="key",
            gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0,
            created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100",
            gpu_count=1, vram_gb=80, os_image="ubuntu",
            ssh_public_key="ssh-ed25519 AAAA",
        )
        for fault in ("attempt", "record", "receipt"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                root = Path(directory)
                originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
                sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = root / "runtime", root / "ledger.md", root / "cost.jsonl"
                sf.RUNTIME_ROOT.mkdir(mode=0o700)
                sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
                env = root / "env"
                env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
                env.chmod(0o600)
                append_calls = []
                try:
                    def append_cost(event):
                        append_calls.append(event)
                        if fault == "attempt" and len(append_calls) == 2:
                            raise OSError("attempt ledger unavailable")

                    with mock.patch.object(teardown.shadeform, "read_owned_resource", return_value=record), \
                            mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                            mock.patch.object(teardown.shadeform, "_delete_instance", return_value={"success": True}), \
                            mock.patch.object(teardown.shadeform, "append_cost_event", side_effect=append_cost), \
                            mock.patch.object(teardown.shadeform, "write_owned_resource", side_effect=OSError("record persistence unavailable") if fault == "record" else None), \
                            mock.patch.object(teardown.shadeform, "verify_owned_ssh_key_before_delete", return_value={}), \
                            mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact") as key_delete, \
                            mock.patch.object(teardown, "_write_deletion_receipt", side_effect=OSError("receipt persistence unavailable") if fault == "receipt" else None):
                        if fault == "receipt":
                            with self.assertRaises(RuntimeError):
                                teardown.teardown_exact(phase, record.instance_id, env_file=env)
                            receipt = None
                        else:
                            receipt = teardown.teardown_exact(phase, record.instance_id, env_file=env)
                    key_delete.assert_not_called()
                    if receipt is not None:
                        self.assertTrue(receipt["retry_required"])
                        self.assertIn(receipt["ssh_key_cleanup_error_type"], {
                            "DeferredUntilAttemptSettlement", "DeferredUntilOwnedRecordPersistence",
                        })
                finally:
                    sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER = originals

    def test_teardown_retains_key_on_unconfirmed_delete_failure(self):
        from scripts import shadeform_lifecycle as sf
        from scripts import shadeform_teardown as teardown
        originals = (sf.RUNTIME_ROOT, sf.MARKDOWN_LEDGER, sf.COST_LEDGER)
        phase = "phase-teardown-failure"
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            sf.RUNTIME_ROOT = root / "runtime"
            sf.RUNTIME_ROOT.mkdir(mode=0o700)
            sf.MARKDOWN_LEDGER = root / "ledger.md"
            sf.MARKDOWN_LEDGER.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            sf.COST_LEDGER = root / "cost.jsonl"
            env = root / "env"
            env.write_text("SHADEFORM_API_KEY=stub-api\n", encoding="utf-8")
            env.chmod(0o600)
            bad_destination = root / "destination-file"
            bad_destination.write_text("not a directory", encoding="utf-8")
            salvage_source = root / "receipt.json"
            salvage_source.write_text("receipt", encoding="utf-8")
            sf.write_owned_resource(sf.OwnedResource(
                phase_id=phase, run_id="test", instance_id="instance-fail-1", ownership_nonce="0123456789abcdef0123456789abcdef", ssh_key_id="key-fail-1", ssh_key_name="key", gpu="A100", cloud="hyperstack", region="r", hourly_usd=1.0, created_at_utc=sf.utc_now().isoformat(), provider_delete_deadline_utc=(sf.utc_now() + sf.timedelta(hours=2)).isoformat(), instance_type="a100", gpu_count=1, vram_gb=80, os_image="ubuntu", ssh_public_key="ssh-ed25519 AAAA", launcher_pid=None,
            ))
            with mock.patch.object(teardown.shadeform, "verify_owned_instance_before_delete", return_value={}), \
                    mock.patch.object(teardown.shadeform, "verify_owned_ssh_key_before_delete", return_value={}), \
                    mock.patch.object(teardown.shadeform, "_delete_instance", side_effect=RuntimeError("delete transport")) as delete, mock.patch.object(teardown.shadeform, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}) as key_delete:
                with self.assertRaises(RuntimeError):
                    teardown.teardown_exact(phase, "instance-fail-1", env_file=env, salvage=salvage_source, salvage_destination=bad_destination)
            delete.assert_called_once()
            key_delete.assert_not_called()
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            original = sf.COST_LEDGER
            sf.COST_LEDGER = Path(directory).resolve() / "cost-ledger.jsonl"
            try:
                write_test_cost_genesis(sf, sf.COST_LEDGER)
                nonce = "a" * 32
                sf.append_cost_event({"instance_id": "instance-ledger-1", "phase_id": "phase-a", "ownership_nonce": nonce, "status": "pending", "estimated_cost_usd": 1.0})
                self.assertEqual(sf.ledger_spend(), (0.0, ["instance-ledger-1"]))
                with self.assertRaises(sf.BudgetError):
                    sf.remaining_budget_usd({"SHADEFORM_MAX_TOTAL_COST_USD": "50"})
                sf.append_cost_event({"instance_id": "instance-ledger-1", "phase_id": "phase-a", "ownership_nonce": nonce, "status": "settled", "actual_cost_usd": 0.42})
                self.assertEqual(sf.ledger_spend(), (0.42, []))
                self.assertEqual(len(sf.COST_LEDGER.read_text().splitlines()), 3)
            finally:
                sf.COST_LEDGER = original

    def test_cost_ledger_rejects_malformed_settled_amounts(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            original = sf.COST_LEDGER
            sf.COST_LEDGER = Path(directory) / "cost-ledger.jsonl"
            try:
                for value in (True, -1, float("nan"), float("inf")):
                    payload = {
                        "schema": sf.COST_EVENT_SCHEMA,
                        "instance_id": "instance-invalid-cost",
                        "phase_id": "invalid-cost",
                        "ownership_nonce": "b" * 32,
                        "owner_binding_sha256": sf.cost_owner_binding_sha256(
                            "invalid-cost", "b" * 32, "instance-invalid-cost",
                        ),
                        "status": "settled",
                        "actual_cost_usd": value,
                        "recorded_at_utc": "2026-01-01T00:00:00+00:00",
                    }
                    sf.COST_LEDGER.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                    with self.subTest(value=value), self.assertRaises(sf.ShadeformError):
                        sf.ledger_spend()
            finally:
                sf.COST_LEDGER = original

    def test_cost_ledger_rejects_unknown_latest_status_instead_of_erasing_pending(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            original = sf.COST_LEDGER
            sf.COST_LEDGER = Path(directory) / "cost-ledger.jsonl"
            try:
                pending = stored_cost_event(sf, {
                    "instance_id": "pending-attempt", "phase_id": "pending-phase",
                    "ownership_nonce": "c" * 32, "status": "pending",
                    "estimated_cost_usd": 1.0,
                })
                bogus = dict(pending)
                bogus["status"] = "bogus"
                bogus["actual_cost_usd"] = 0.0
                bogus.pop("estimated_cost_usd")
                sf.COST_LEDGER.write_text("\n".join([
                    json.dumps(pending),
                    json.dumps(bogus),
                ]) + "\n", encoding="utf-8")
                with self.assertRaises(sf.ShadeformError):
                    sf.ledger_spend()
            finally:
                sf.COST_LEDGER = original

    def test_append_cost_event_requires_coherent_pending_or_settled_shape(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, mock.patch.object(sf, "COST_LEDGER", Path(directory).resolve() / "cost-ledger.jsonl"):
            owner = {"phase_id": "cost-shape", "ownership_nonce": "d" * 32}
            invalid = [
                {"instance_id": "cost-shape", **owner, "status": "unknown", "actual_cost_usd": 0.0},
                {"instance_id": "cost-shape", **owner, "status": "pending", "estimated_cost_usd": 1.0, "actual_cost_usd": 0.0},
                {"instance_id": "cost-shape", **owner, "status": "pending", "estimated_cost_usd": float("nan")},
                {"instance_id": "cost-shape", **owner, "status": "settled", "estimated_cost_usd": 1.0, "actual_cost_usd": 0.0},
                {"instance_id": "cost-shape", **owner, "status": "settled", "actual_cost_usd": False},
            ]
            for event in invalid:
                with self.subTest(event=event), self.assertRaises(ValueError):
                    sf.append_cost_event(event)

    def test_markdown_never_authorizes_absent_json_cost_ledger(self):
        from scripts import shadeform_lifecycle as sf
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            original_cost, original_markdown = sf.COST_LEDGER, sf.MARKDOWN_LEDGER
            sf.COST_LEDGER = Path(directory) / "missing-cost.jsonl"
            sf.MARKDOWN_LEDGER = Path(directory) / "ledger.md"
            try:
                sf.MARKDOWN_LEDGER.write_text("\n".join([
                    sf.LEDGER_HEADER,
                    "| 2026-01-01 | phase-a | instance-md-1 | A100 | $1.0000 | run | pending | $0.0000 | 0.0 |",
                ]) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(sf.ShadeformError, "authoritative JSON"):
                    sf.ledger_spend()
                with mock.patch.object(sf, "_fetch_instance_types") as provider:
                    with self.assertRaisesRegex(sf.ShadeformError, "authoritative JSON"):
                        sf.list_candidates(
                            "secret", {}, phase_id="budget-proof",
                            min_vram_gb=80, max_runtime_hours=0.25,
                        )
                provider.assert_not_called()
                sf.MARKDOWN_LEDGER.write_text("\n".join([
                    sf.LEDGER_HEADER,
                    "| 2026-01-01 | phase-a | instance-md-1 | A100 | $1.0000 | run | deleted | $9999.0000 | 0.0 |",
                ]) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(sf.ShadeformError, "authoritative JSON"):
                    sf.ledger_spend()
            finally:
                sf.COST_LEDGER, sf.MARKDOWN_LEDGER = original_cost, original_markdown

    def test_unsafe_json_cost_ledger_blocks_before_provider_catalogue_access(self):
        from scripts import shadeform_lifecycle as sf

        pending = stored_cost_event(sf, {
            "instance_id": "instance-budget-proof",
            "phase_id": "budget-proof",
            "ownership_nonce": "e" * 32,
            "status": "pending",
            "estimated_cost_usd": 1.0,
        })
        valid_payload = (json.dumps(pending, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)

            def blocked(cost_path, *, stat_effect=None):
                patches = [mock.patch.object(sf, "COST_LEDGER", cost_path)]
                if stat_effect is not None:
                    patches.append(mock.patch.object(sf.os, "stat", side_effect=stat_effect))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    provider = stack.enter_context(mock.patch.object(sf, "_fetch_instance_types"))
                    with self.assertRaises(sf.ShadeformError):
                        sf.list_candidates(
                            "secret", {}, phase_id="budget-proof",
                            min_vram_gb=80, max_runtime_hours=0.25,
                        )
                provider.assert_not_called()

            target = root / "symlink-target"
            target.write_bytes(valid_payload)
            symlink = root / "symlink-cost"
            symlink.symlink_to(target)
            blocked(symlink)

            hard_target = root / "hardlink-target"
            hard_target.write_bytes(valid_payload)
            hardlink = root / "hardlink-cost"
            os.link(hard_target, hardlink)
            blocked(hardlink)

            oversized = root / "oversized-cost"
            oversized.write_bytes(b"x" * (sf.MAX_COST_LEDGER_BYTES + 1))
            blocked(oversized)

            blocked(root / "missing-cost")

            stable = root / "stable-cost"
            stable.write_bytes(valid_payload)
            replacement = root / "replacement-cost"
            replacement.write_bytes(valid_payload + valid_payload)
            real_stat = os.stat

            def swapped_stat(path, *args, **kwargs):
                if Path(path) == stable and kwargs.get("follow_symlinks") is False:
                    return real_stat(replacement, follow_symlinks=False)
                return real_stat(path, *args, **kwargs)

            blocked(stable, stat_effect=swapped_stat)

    def test_provider_preflight_rejects_unsafe_bounded_policy_files_before_transport(self):
        from scripts import shadeform_lifecycle as sf

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            incident = root / "incidents.md"
            incident.write_text("# bounded incident catalogue\n", encoding="utf-8")

            def refused(markdown):
                with mock.patch.object(sf, "MARKDOWN_LEDGER", markdown), \
                        mock.patch.object(sf, "INCIDENT_LOG", incident), \
                        mock.patch.object(sf.urllib.request, "urlopen") as transport:
                    with self.assertRaises(sf.ShadeformError):
                        sf.request("secret", "GET", "/instances", phase_id="preflight-test")
                transport.assert_not_called()

            valid_markdown = root / "valid-ledger"
            valid_markdown.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")

            def refused_incident(catalog):
                with mock.patch.object(sf, "MARKDOWN_LEDGER", valid_markdown), \
                        mock.patch.object(sf, "INCIDENT_LOG", catalog), \
                        mock.patch.object(sf.urllib.request, "urlopen") as transport:
                    with self.assertRaises(sf.ShadeformError):
                        sf.request("secret", "GET", "/instances", phase_id="preflight-test")
                transport.assert_not_called()

            target = root / "symlink-target"
            target.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            symlink = root / "symlink-ledger"
            symlink.symlink_to(target)
            refused(symlink)

            hard_target = root / "hardlink-target"
            hard_target.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            hardlink = root / "hardlink-ledger"
            os.link(hard_target, hardlink)
            refused(hardlink)

            oversized = root / "oversized-ledger"
            oversized.write_bytes(b"x" * (sf.MAX_MARKDOWN_LEDGER_BYTES + 1))
            refused(oversized)

            refused(root / "missing-ledger")

            incident_target = root / "incident-symlink-target"
            incident_target.write_text("# incident\n", encoding="utf-8")
            incident_symlink = root / "incident-symlink"
            incident_symlink.symlink_to(incident_target)
            refused_incident(incident_symlink)

            incident_hard_target = root / "incident-hardlink-target"
            incident_hard_target.write_text("# incident\n", encoding="utf-8")
            incident_hardlink = root / "incident-hardlink"
            os.link(incident_hard_target, incident_hardlink)
            refused_incident(incident_hardlink)

            incident_oversized = root / "incident-oversized"
            incident_oversized.write_bytes(
                b"x" * (sf.MAX_INCIDENT_CATALOG_BYTES + 1)
            )
            refused_incident(incident_oversized)
            refused_incident(root / "incident-missing")

            stable = root / "stable-ledger"
            stable.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            replacement = root / "replacement-ledger"
            replacement.write_text(sf.LEDGER_HEADER + "\nextra\n", encoding="utf-8")
            real_stat = os.stat

            def swapped_stat(path, *args, **kwargs):
                if Path(path) == stable and kwargs.get("follow_symlinks") is False:
                    return real_stat(replacement, follow_symlinks=False)
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(sf, "MARKDOWN_LEDGER", stable), \
                    mock.patch.object(sf, "INCIDENT_LOG", incident), \
                    mock.patch.object(sf.os, "stat", side_effect=swapped_stat), \
                    mock.patch.object(sf.urllib.request, "urlopen") as transport:
                with self.assertRaisesRegex(sf.ShadeformError, "changed during bounded read"):
                    sf.request("secret", "GET", "/instances", phase_id="preflight-test")
            transport.assert_not_called()

            stable_incident = root / "stable-incident"
            stable_incident.write_text("# incident\n", encoding="utf-8")
            replacement_incident = root / "replacement-incident"
            replacement_incident.write_text("# changed incident\n", encoding="utf-8")
            real_stat = os.stat

            def swapped_incident_stat(path, *args, **kwargs):
                if Path(path) == stable_incident and kwargs.get("follow_symlinks") is False:
                    return real_stat(replacement_incident, follow_symlinks=False)
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(sf, "MARKDOWN_LEDGER", valid_markdown), \
                    mock.patch.object(sf, "INCIDENT_LOG", stable_incident), \
                    mock.patch.object(sf.os, "stat", side_effect=swapped_incident_stat), \
                    mock.patch.object(sf.urllib.request, "urlopen") as transport:
                with self.assertRaisesRegex(sf.ShadeformError, "changed during bounded read"):
                    sf.request("secret", "GET", "/instances", phase_id="preflight-test")
            transport.assert_not_called()

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
        cleanup = source[source.index("if instance_id is not None:") : source.index("if attempt_reserved and settle_attempt_after_cleanup and not attempt_settled_during_cleanup:")]
        self.assertIn("lifecycle[\"deletion\"] = teardown_exact", cleanup)
        self.assertNotIn("finally:\n                        #", cleanup)
        self.assertIn("if deletion_confirmed():\n                stop_watchdog()", cleanup)
        self.assertIn('deletion.get("retry_required") is not True', source)

    def test_watchdog_is_prearmed_before_ssh_key_mutation(self):
        source = (ROOT / "scripts" / "j1m_orchestrator.py").read_text(encoding="utf-8")
        self.assertLess(source.index("watchdog = subprocess.Popen("), source.index("key_id = sf.add_ssh_key"))

    def test_watchdog_settles_instance_and_attempt_before_key_failure(self):
        watchdog = load(ROOT / "scripts/shadeform_watchdog.py", "j1m_watchdog_key_failure_order")
        from scripts import shadeform_lifecycle as sf
        phase = "watchdog-key-failure-order"
        nonce = "0123456789abcdef0123456789abcdef"
        now = time.time()
        intent = {
            "schema": sf.COST_EVENT_SCHEMA,
            "phase_id": phase, "ownership_nonce": nonce,
            "instance_id": "attempt-" + nonce, "instance_create_intent": True,
            "intent_schema": sf.INSTANCE_CREATE_INTENT_SCHEMA,
            "owner_binding_sha256": sf.cost_owner_binding_sha256(
                phase, nonce, "attempt-" + nonce,
            ),
            "status": "pending", "estimated_cost_usd": 1.35,
            "reservation": "instance-create-intent", "instance_name": "ep-name",
            "ssh_key_id": "key-123456",
            "create_started_at_utc": "2026-09-04T00:00:00+00:00",
            "recorded_at_utc": "2026-09-04T00:00:00+00:00",
            "hourly_usd": 1.35, "backstop_hours": 1.0,
            "provider_delete_deadline_utc": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
        }
        info = {"id": "instance-reconciled", "name": "ep-name"}
        events = []
        order = []
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(watchdog, "_pending_intent", return_value=intent), \
                mock.patch.object(sf, "read_phase_ownership", return_value=(None, False)), \
                mock.patch.object(sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}), \
                mock.patch.object(sf, "require_env", return_value="api"), \
                mock.patch.object(sf, "reconcile_instance_by_nonce", return_value="instance-reconciled"), \
                mock.patch.object(sf, "instance_info", return_value=info), \
                mock.patch.object(sf, "verify_instance_ownership"), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "append_cost_event", side_effect=lambda event: events.append(event)), \
                mock.patch.object(sf, "verify_ssh_key_fingerprint", side_effect=lambda *args, **kwargs: order.append("key-verify")), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", side_effect=RuntimeError("key cleanup unavailable")), \
                mock.patch.object(sf, "append_incident", side_effect=lambda event: order.append(event["incident"])), \
                mock.patch("scripts.shadeform_teardown.teardown_recovered_exact", return_value={"status": "complete", "retry_required": False}) as shared, \
                mock.patch.object(watchdog.time, "sleep", return_value=None):
            result = watchdog.main([
                "--phase-id", phase, "--launcher-pid", str(os.getpid()),
                "--max-seconds", "0.01", "--deadline-epoch", str(now + 700),
                "--provider-delete-deadline-epoch", str(now + 3600),
                "--allow-unrecorded-exact", "--precreate-recovery",
                "--ownership-nonce", nonce, "--instance-name", "ep-name",
                "--ssh-key-id", "key-123456", "--ssh-key-name", "ep-key",
                "--ssh-key-fingerprint", "SHA256:abc", "--cloud", "hyperstack",
                "--region", "montreal-canada-2", "--instance-type", "A100_80G",
                "--hourly-usd", "1.35", "--gpu", "A100_80G", "--gpu-count", "1",
                "--vram-gb", "80", "--os-image", "ubuntu22.04_cuda12.2_shade_os",
            ])
        self.assertEqual(result, 0)
        recovered = shared.call_args.args[0]
        self.assertEqual(recovered.instance_id, "instance-reconciled")
        self.assertEqual(recovered.provider_delete_deadline_utc, intent["provider_delete_deadline_utc"])
        self.assertEqual(events, [])

    def test_watchdog_cost_failure_keeps_key_recovery_pending(self):
        watchdog = load(ROOT / "scripts/shadeform_watchdog.py", "j1m_watchdog_cost_failure_order")
        from scripts import shadeform_lifecycle as sf
        phase = "watchdog-cost-failure-order"
        nonce = "fedcba9876543210fedcba9876543210"
        now = time.time()
        intent = {
            "schema": sf.COST_EVENT_SCHEMA,
            "phase_id": phase, "ownership_nonce": nonce,
            "instance_id": "attempt-" + nonce, "instance_create_intent": True,
            "intent_schema": sf.INSTANCE_CREATE_INTENT_SCHEMA,
            "owner_binding_sha256": sf.cost_owner_binding_sha256(
                phase, nonce, "attempt-" + nonce,
            ),
            "status": "pending", "estimated_cost_usd": 1.35,
            "reservation": "instance-create-intent", "instance_name": "ep-name",
            "ssh_key_id": "key-123456",
            "create_started_at_utc": "2026-09-04T00:00:00+00:00",
            "recorded_at_utc": "2026-09-04T00:00:00+00:00",
            "hourly_usd": 1.35, "backstop_hours": 1.0,
            "provider_delete_deadline_utc": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
        }
        info = {"id": "instance-cost-failure", "name": "ep-name"}
        incidents = []
        cost_calls = []
        def append_cost(event):
            cost_calls.append(event)
            if event["status"] == "settled":
                raise RuntimeError("ledger unavailable")
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(watchdog, "_pending_intent", return_value=intent), \
                mock.patch.object(sf, "read_phase_ownership", return_value=(None, False)), \
                mock.patch.object(sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}), \
                mock.patch.object(sf, "require_env", return_value="api"), \
                mock.patch.object(sf, "reconcile_instance_by_nonce", return_value="instance-cost-failure"), \
                mock.patch.object(sf, "instance_info", return_value=info), \
                mock.patch.object(sf, "verify_instance_ownership"), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "append_cost_event", side_effect=append_cost), \
                mock.patch.object(sf, "append_incident", side_effect=lambda event: incidents.append(event)), \
                mock.patch.object(sf, "verify_ssh_key_fingerprint") as key_verify, \
                mock.patch.object(sf, "delete_owned_ssh_key_exact") as key_delete, \
                mock.patch("scripts.shadeform_teardown.teardown_recovered_exact", side_effect=RuntimeError("cost recovery pending")), \
                mock.patch.object(watchdog.time, "sleep", return_value=None):
            result = watchdog.main([
                "--phase-id", phase, "--launcher-pid", str(os.getpid()),
                "--max-seconds", "0.01", "--deadline-epoch", str(now + 700),
                "--provider-delete-deadline-epoch", str(now + 3600),
                "--allow-unrecorded-exact", "--precreate-recovery",
                "--ownership-nonce", nonce, "--instance-name", "ep-name",
                "--ssh-key-id", "key-123456", "--ssh-key-name", "ep-key",
                "--ssh-key-fingerprint", "SHA256:abc", "--cloud", "hyperstack",
                "--region", "montreal-canada-2", "--instance-type", "A100_80G",
                "--hourly-usd", "1.35", "--gpu", "A100_80G", "--gpu-count", "1",
                "--vram-gb", "80", "--os-image", "ubuntu22.04_cuda12.2_shade_os",
            ])
        self.assertEqual(result, 1)
        self.assertEqual(incidents, [])
        key_verify.assert_not_called()
        key_delete.assert_not_called()

    def test_watchdog_attempt_settlement_failure_keeps_key(self):
        watchdog = load(ROOT / "scripts/shadeform_watchdog.py", "j1m_watchdog_attempt_failure_order")
        from scripts import shadeform_lifecycle as sf
        phase = "watchdog-attempt-failure-order"
        nonce = "00112233445566778899aabbccddeeff"
        now = time.time()
        intent = {
            "schema": sf.COST_EVENT_SCHEMA,
            "phase_id": phase, "ownership_nonce": nonce,
            "instance_id": "attempt-" + nonce, "instance_create_intent": True,
            "intent_schema": sf.INSTANCE_CREATE_INTENT_SCHEMA,
            "owner_binding_sha256": sf.cost_owner_binding_sha256(
                phase, nonce, "attempt-" + nonce,
            ),
            "status": "pending", "estimated_cost_usd": 1.35,
            "reservation": "instance-create-intent", "instance_name": "ep-name",
            "ssh_key_id": "key-123456",
            "create_started_at_utc": "2026-09-04T00:00:00+00:00",
            "recorded_at_utc": "2026-09-04T00:00:00+00:00",
            "hourly_usd": 1.35, "backstop_hours": 1.0,
            "provider_delete_deadline_utc": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
        }
        events = []
        def append_cost(event):
            events.append(event)
            if event["status"] == "settled" and event["instance_id"].startswith("attempt-"):
                raise OSError("attempt ledger unavailable")
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(watchdog, "_pending_intent", return_value=intent), \
                mock.patch.object(sf, "read_phase_ownership", return_value=(None, False)), \
                mock.patch.object(sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}), \
                mock.patch.object(sf, "require_env", return_value="api"), \
                mock.patch.object(sf, "reconcile_instance_by_nonce", return_value="instance-attempt-failure"), \
                mock.patch.object(sf, "instance_info", return_value={"id": "instance-attempt-failure", "name": "ep-name"}), \
                mock.patch.object(sf, "verify_instance_ownership"), \
                mock.patch.object(sf, "_delete_instance", return_value={"success": True}), \
                mock.patch.object(sf, "append_cost_event", side_effect=append_cost), \
                mock.patch.object(sf, "append_incident"), \
                mock.patch.object(sf, "verify_ssh_key_fingerprint") as key_verify, \
                mock.patch.object(sf, "delete_owned_ssh_key_exact") as key_delete, \
                mock.patch("scripts.shadeform_teardown.teardown_recovered_exact", side_effect=OSError("attempt recovery pending")), \
                mock.patch.object(watchdog.time, "sleep", return_value=None):
            result = watchdog.main([
                "--phase-id", phase, "--launcher-pid", str(os.getpid()),
                "--max-seconds", "0.01", "--deadline-epoch", str(now + 700),
                "--provider-delete-deadline-epoch", str(now + 3600),
                "--allow-unrecorded-exact", "--precreate-recovery",
                "--ownership-nonce", nonce, "--instance-name", "ep-name",
                "--ssh-key-id", "key-123456", "--ssh-key-name", "ep-key",
                "--ssh-key-fingerprint", "SHA256:abc", "--cloud", "hyperstack",
                "--region", "montreal-canada-2", "--instance-type", "A100_80G",
                "--hourly-usd", "1.35", "--gpu", "A100_80G", "--gpu-count", "1",
                "--vram-gb", "80", "--os-image", "ubuntu22.04_cuda12.2_shade_os",
            ])
        self.assertEqual(result, 1)
        self.assertEqual(events, [])
        key_verify.assert_not_called()
        key_delete.assert_not_called()

    def test_watchdog_key_only_settles_attempt_before_key(self):
        watchdog = load(ROOT / "scripts/shadeform_watchdog.py", "j1m_watchdog_key_only_order")
        from scripts import shadeform_lifecycle as sf
        phase = "watchdog-key-only-order"
        nonce = "11223344556677889900aabbccddeeff"
        now = time.time()
        intent = {
            "phase_id": phase, "ownership_nonce": nonce,
            "instance_id": "attempt-" + nonce, "instance_create_intent": False,
            "create_started_at_utc": "2026-09-04T00:00:00+00:00",
            "hourly_usd": 1.35, "backstop_hours": 1.0,
            "provider_delete_deadline_utc": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
        }
        events = []
        order = []
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(watchdog, "_pending_intent", return_value=intent), \
                mock.patch.object(sf, "read_phase_ownership", return_value=(None, False)), \
                mock.patch.object(sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}), \
                mock.patch.object(sf, "require_env", return_value="api"), \
                mock.patch.object(sf, "reconcile_ssh_key", return_value="key-key-only"), \
                mock.patch.object(sf, "reconcile_instance_by_nonce", return_value=None), \
                mock.patch.object(sf, "append_cost_event", side_effect=lambda event: events.append(event)), \
                mock.patch.object(sf, "append_incident", side_effect=lambda event: order.append(event["incident"])), \
                mock.patch.object(sf, "delete_owned_ssh_key_exact", side_effect=lambda *args, **kwargs: (order.append("key-delete") or {"status": "confirmed"})), \
                mock.patch.object(watchdog.time, "sleep", return_value=None):
            result = watchdog.main([
                "--phase-id", phase, "--launcher-pid", str(os.getpid()),
                "--max-seconds", "0.01", "--deadline-epoch", str(now + 700),
                "--provider-delete-deadline-epoch", str(now + 3600),
                "--allow-unrecorded-exact", "--precreate-recovery", "--key-only-recovery",
                "--ownership-nonce", nonce, "--instance-name", "ep-name",
                "--ssh-key-name", "ep-key", "--ssh-key-fingerprint", "SHA256:abc",
                "--cloud", "hyperstack", "--region", "montreal-canada-2",
                "--instance-type", "A100_80G", "--hourly-usd", "1.35",
                "--gpu", "A100_80G", "--gpu-count", "1", "--vram-gb", "80",
                "--os-image", "ubuntu22.04_cuda12.2_shade_os",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(events[0]["instance_id"], "attempt-" + nonce)
        self.assertLess(order.index("watchdog-key-recovery-settled"), order.index("key-delete"))

    def test_j1m_definitive_failure_settles_attempt_before_key_cleanup(self):
        source = (ROOT / "scripts" / "j1m_orchestrator.py").read_text(encoding="utf-8")
        definitive = source[source.index("elif key_id is not None and not ambiguous_create:"):source.index("# Keep the watchdog alive", source.index("elif key_id is not None and not ambiguous_create:"))]
        self.assertLess(definitive.index("sf.append_cost_event"), definitive.index("sf.delete_owned_ssh_key_exact"))
        self.assertIn("attempt reservation settlement was not confirmed", definitive)

    @isolated_lifecycle_execute
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

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            identity = Path(directory) / "ssh-key"
            identity.write_text("private", encoding="utf-8")
            identity.chmod(0o600)
            with contextlib.ExitStack() as stack:
                teardown_failure = mock.patch.object(orchestrator, "teardown_exact", side_effect=RuntimeError("delete unavailable"))
                patches = [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)),
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
                        Path(directory) / "env", config_path=private_config(Path(directory)),
                        phase_id="teardown-behavior", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
        self.assertTrue(persisted)
        self.assertEqual(progress[-1], "failed")
        self.assertIsNotNone(teardown_mock.call_args.kwargs.get("deadline"))

    @isolated_lifecycle_execute
    def test_owned_record_fallback_reuses_real_pending_before_key_delete(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_unrecorded_settlement")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        order = []
        progress = []
        captured = []

        class Watchdog:
            pid = 42
            def poll(self): return None
            def terminate(self): order.append("watchdog-stop")
            def wait(self, timeout): return 0

        provider_info = {
            "id": "instance-123456", "name": "ep-test-placeholder",
            "tags": ["local-bmo-j1m", "ep-phase-unrecorded-fallback", "ep-run-placeholder"],
            "ssh_key_id": "key-123456", "cloud": "hyperstack", "region": "montreal-canada-2",
            "shade_instance_type": "A100_80G", "hourly_price": 135,
            "configuration": {"gpu_type": "A100_80G", "num_gpus": 1, "vram_per_gpu_in_gb": 80, "os": "ubuntu22.04_cuda12.2_shade_os"},
        }
        nonce = "a" * 32
        attempt_id = "attempt-" + nonce
        write_owned_resource = orchestrator.sf.write_owned_resource
        owned_write_calls = 0
        key_confirmed = False

        def fail_initial_owned_write(record):
            nonlocal owned_write_calls
            owned_write_calls += 1
            captured.append(record)
            if owned_write_calls == 1:
                raise OSError("owned record crash")
            return write_owned_resource(record)

        def verify_instance(*_args, **_kwargs):
            order.append("instance-verify")
            return provider_info

        def delete_key(*_args, **_kwargs):
            nonlocal key_confirmed
            instance_state = sf.exact_owner_cost_state(
                "unrecorded-fallback", nonce, "instance-123456",
            )
            attempt_state = sf.exact_owner_cost_state(
                "unrecorded-fallback", nonce, attempt_id,
            )
            self.assertEqual(instance_state["status"], "settled")
            self.assertEqual(attempt_state["status"], "settled")
            self.assertIsNotNone(sf.read_owned_resource("unrecorded-fallback"))
            if not key_confirmed:
                order.append("key-delete")
                key_confirmed = True
            return {"status": "confirmed"}

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            identity = Path(directory) / "ssh-key"
            identity.write_text("private", encoding="utf-8")
            identity.chmod(0o600)
            with contextlib.ExitStack() as stack:
                patches = [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)),
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"),
                    mock.patch.object(orchestrator.sf, "list_candidates", return_value=[candidate]),
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key", return_value=(identity, "ssh-ed25519 AAAA")),
                    mock.patch.object(orchestrator.sf, "new_ownership_nonce", return_value=nonce),
                    mock.patch.object(orchestrator.sf, "ssh_public_key_fingerprint", return_value="A" * 43),
                    mock.patch.object(orchestrator.sf, "add_ssh_key", return_value="key-123456"),
                    mock.patch.object(orchestrator.sf, "verify_ssh_key_ownership", return_value={}),
                    mock.patch.object(orchestrator.sf, "create_instance", return_value="instance-123456"),
                    mock.patch.object(orchestrator.sf, "process_start_marker", return_value=None),
                    mock.patch.object(orchestrator.sf, "write_owned_resource", side_effect=fail_initial_owned_write),
                    mock.patch.object(orchestrator.sf, "verify_owned_instance_before_delete", side_effect=verify_instance),
                    mock.patch.object(orchestrator.sf, "_delete_instance", side_effect=lambda *args, **kwargs: order.append("instance-delete") or {"success": True}),
                    mock.patch.object(orchestrator.sf, "verify_owned_ssh_key_before_delete", return_value={}),
                    mock.patch.object(orchestrator.sf, "delete_owned_ssh_key_exact", side_effect=delete_key),
                    mock.patch.object(orchestrator, "_persist_lifecycle"),
                    mock.patch.object(orchestrator, "_progress", side_effect=lambda path, event, **details: progress.append(event)),
                    mock.patch.object(orchestrator.j1m_runner, "write_progress"),
                    mock.patch.object(orchestrator.subprocess, "Popen", return_value=Watchdog()),
                    mock.patch.object(orchestrator, "_salvage", return_value=[]),
                ]
                for patcher in patches:
                    stack.enter_context(patcher)
                with self.assertRaises(OSError):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=private_config(Path(directory)),
                        phase_id="unrecorded-fallback", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
                self.assertTrue(captured)
                receipt = orchestrator.teardown_recovered_exact(
                    captured[0], env_file=Path(directory) / "env",
                )
                self.assertEqual(receipt["status"], "complete")

                data = sf.bounded_stable_bytes(
                    sf.COST_LEDGER, sf.MAX_COST_LEDGER_BYTES,
                    label="test cost ledger",
                )
                events = sf._cost_ledger_events(data)
                instance = [
                    event for event in events
                    if event.get("instance_id") == "instance-123456"
                ]
                self.assertEqual(
                    [event["status"] for event in instance],
                    ["pending", "settled"],
                )
                self.assertEqual(instance[0]["estimated_cost_usd"], 0.421875)
                attempt = [
                    event for event in events
                    if event.get("instance_id") == attempt_id
                ]
                self.assertEqual(attempt[-1]["status"], "settled")
                self.assertEqual(sf.ledger_spend()[1], [])

        self.assertEqual(order.count("instance-delete"), 1)
        self.assertEqual(order.count("key-delete"), 1)
        self.assertLess(order.index("instance-verify"), order.index("instance-delete"))
        self.assertLess(order.index("instance-delete"), order.index("key-delete"))

    @isolated_lifecycle_execute
    def test_no_ledger_fallback_attempt_failure_retains_key(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_unrecorded_attempt_failure")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        events = []
        provider_info = {
            "id": "instance-654321", "name": "ep-test-placeholder",
            "tags": ["local-bmo-j1m", "ep-phase-unrecorded-attempt", "ep-run-placeholder"],
            "ssh_key_id": "key-654321", "cloud": "hyperstack", "region": "montreal-canada-2",
            "shade_instance_type": "A100_80G", "hourly_price": 135,
            "configuration": {"gpu_type": "A100_80G", "num_gpus": 1, "vram_per_gpu_in_gb": 80, "os": "ubuntu22.04_cuda12.2_shade_os"},
        }
        class Watchdog:
            pid = 42
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout): return 0
        nonce = "b" * 32
        attempt_id = "attempt-" + nonce
        write_owned_resource = orchestrator.sf.write_owned_resource
        append_cost_event = orchestrator.sf.append_cost_event
        owned_write_calls = 0

        def fail_initial_owned_write(record):
            nonlocal owned_write_calls
            owned_write_calls += 1
            if owned_write_calls == 1:
                raise OSError("owned record crash")
            return write_owned_resource(record)

        def append_cost(event):
            events.append(event)
            if (
                event["instance_id"] == attempt_id
                and event.get("status") == "settled"
            ):
                raise OSError("attempt settlement unavailable")
            return append_cost_event(event)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            identity = Path(directory) / "ssh-key"
            identity.write_text("private", encoding="utf-8")
            identity.chmod(0o600)
            with contextlib.ExitStack() as stack:
                key_delete = stack.enter_context(mock.patch.object(orchestrator.sf, "delete_owned_ssh_key_exact"))
                for patcher in [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)),
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"),
                    mock.patch.object(orchestrator.sf, "list_candidates", return_value=[candidate]),
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key", return_value=(identity, "ssh-ed25519 AAAA")),
                    mock.patch.object(orchestrator.sf, "new_ownership_nonce", return_value=nonce),
                    mock.patch.object(orchestrator.sf, "add_ssh_key", return_value="key-654321"),
                    mock.patch.object(orchestrator.sf, "verify_ssh_key_ownership", return_value={}),
                    mock.patch.object(orchestrator.sf, "append_instance_create_intent"),
                    mock.patch.object(orchestrator.sf, "create_instance", return_value="instance-654321"),
                    mock.patch.object(orchestrator.sf, "process_start_marker", return_value=None),
                    mock.patch.object(orchestrator.sf, "write_owned_resource", side_effect=fail_initial_owned_write),
                    mock.patch.object(
                        orchestrator.sf,
                        "append_cost_event",
                        side_effect=append_cost,
                    ),
                    mock.patch.object(orchestrator.sf, "instance_info", return_value=provider_info),
                    mock.patch.object(orchestrator.sf, "verify_instance_ownership"),
                    mock.patch.object(orchestrator.sf, "_delete_instance", return_value={"success": True}),
                    mock.patch.object(orchestrator.sf, "append_incident"),
                    mock.patch.object(orchestrator, "_persist_lifecycle"),
                    mock.patch.object(orchestrator, "_progress"),
                    mock.patch.object(orchestrator.j1m_runner, "write_progress"),
                    mock.patch.object(orchestrator.subprocess, "Popen", return_value=Watchdog()),
                    mock.patch.object(orchestrator, "_salvage", return_value=[]),
                ]:
                    stack.enter_context(patcher)
                with self.assertRaises(OSError):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=private_config(Path(directory)),
                        phase_id="unrecorded-attempt", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
            key_delete.assert_not_called()
            self.assertEqual(events[-1]["instance_id"], attempt_id)

    @isolated_lifecycle_execute
    def test_execute_reservation_failure_restores_signal_and_terminalizes(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_reservation_behavior")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        progress = []
        persisted = []
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            identity = Path(directory) / "ssh-key"
            identity.write_text("private", encoding="utf-8")
            identity.chmod(0o600)
            with contextlib.ExitStack() as stack:
                for patcher in [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)),
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
                        Path(directory) / "env", config_path=private_config(Path(directory)),
                        phase_id="reservation-behavior", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
        self.assertTrue(persisted)
        self.assertEqual(progress[-1], "failed")

    @isolated_lifecycle_execute
    def test_unresolved_ssh_key_ambiguity_keeps_reservation_pending(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_key_ambiguity")
        from scripts import shadeform_lifecycle as sf
        candidate = sf.Candidate("A100_80G", "hyperstack", "montreal-canada-2", "A100_80G", 1.35, 80, "ubuntu22.04_cuda12.2_shade_os", False)
        progress = []
        persisted = []

        class Watchdog:
            pid = 42
            def poll(self): return None
            def terminate(self): raise AssertionError("ambiguous ownership must retain watchdog")
            def wait(self, timeout): raise AssertionError("ambiguous ownership must retain watchdog")

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            identity = Path(directory) / "ssh-key"
            identity.write_text("private", encoding="utf-8")
            identity.chmod(0o600)
            with contextlib.ExitStack() as stack:
                for patcher in [
                    mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api"}),
                    mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)),
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"),
                    mock.patch.object(orchestrator.sf, "list_candidates", return_value=[candidate]),
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key", return_value=(identity, "ssh-ed25519 AAAA")),
                    mock.patch.object(orchestrator.sf, "reserve_create_attempt", return_value="attempt-x"),
                    mock.patch.object(orchestrator.sf, "add_ssh_key", side_effect=sf.AmbiguousProviderOutcome("unknown")),
                    mock.patch.object(orchestrator.sf, "reconcile_ssh_key", side_effect=sf.AmbiguousProviderOutcome("zero matches")),
                    mock.patch.object(orchestrator.sf, "append_incident"),
                    mock.patch.object(orchestrator.sf, "append_cost_event"),
                    mock.patch.object(orchestrator.subprocess, "Popen", return_value=Watchdog()),
                    mock.patch.object(orchestrator, "_persist_lifecycle", side_effect=lambda phase, value: persisted.append(value)),
                    mock.patch.object(orchestrator, "_progress", side_effect=lambda path, event, **details: progress.append(event)),
                    mock.patch.object(orchestrator.j1m_runner, "write_progress"),
                ]:
                    stack.enter_context(patcher)
                with self.assertRaises(sf.AmbiguousProviderOutcome):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=private_config(Path(directory)),
                        phase_id="key-ambiguity", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
                self.assertFalse(orchestrator.sf.append_cost_event.called)
        self.assertTrue(persisted)
        self.assertEqual(persisted[-1]["ssh_key_reconciliation"]["status"], "unresolved")
        self.assertEqual(progress[-1], "failed")

    @isolated_lifecycle_execute
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            progress = Path(directory) / "progress.json"
            orchestrator._progress(progress, "eval-stage-starting", phase_id="p", operation_stage="eval-bootstrap:mkdir")
            payload = json.loads(progress.read_text(encoding="utf-8"))
        self.assertEqual(payload["stage"], "eval-stage-starting")
        self.assertEqual(payload["operation_stage"], "eval-bootstrap:mkdir")

    def test_remote_failure_retains_only_a_credential_screened_stdout_tail(self):
        """J1M-HOST-PRIVACY-001 changed this contract deliberately.

        Retaining no stdout at all is what made run j1m-eval-20260911-remote-d
        unreadable: the remote runner prints its typed JSON refusal on stdout,
        so the lifecycle receipt recorded `exit 2` with an empty stderr tail
        and no diagnosis for a run that had already been billed. A failed
        stage now keeps a bounded stdout tail -- but only one that survives
        the same credential screening every other persisted value gets, so a
        credential-shaped line still never reaches the receipt.
        """

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
        self.assertEqual(receipt["stdout_tail"], "<redacted>")
        self.assertNotIn("do-not-retain", json.dumps(receipt))

    def test_eval_stage_labels_distinguish_python_and_cmake_operations(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_stage_labels")
        self.assertEqual(
            orchestrator._eval_stage_label(["python3", "/scratch/j1m/remote_toolchain_probe.py"]),
            "eval-stage:remote_toolchain_probe",
        )
        self.assertEqual(orchestrator._eval_stage_label(["cmake", "-S", "engine"]), "eval-stage:cmake-configure")
        self.assertEqual(orchestrator._eval_stage_label(["cmake", "--build", "build"]), "eval-stage:cmake-build")

    def test_salvage_refuses_the_model_artifact_name_without_any_transfer(self):
        """The bounded transport carries receipts; the Q4 GGUF is not fetchable."""

        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_timeout")
        info = {"phase_id": "j1m-test", "instance_info": {"ssh_user": "u", "ip": "127.0.0.1"}}
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            identity = Path(directory) / "id"
            known_hosts = Path(directory) / "known_hosts"
            known_hosts.write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
            known_hosts.chmod(0o600)
            destination = Path(directory) / "artifacts"
            destination.mkdir(mode=0o700)
            with mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)), \
                    mock.patch.object(orchestrator.sf, "_preflight"), \
                    mock.patch.object(orchestrator.sf, "scp_base") as scp_base, \
                    mock.patch.object(orchestrator, "_remote") as remote:
                results = orchestrator._salvage(
                    info, identity, known_hosts, destination,
                    ["Qwen3.5-9B-Q4_K_M.gguf"], q4_expected_gib=6,
                    deadline=time.monotonic() + 1000,
                )
            self.assertEqual(results, [{
                "name": "Qwen3.5-9B-Q4_K_M.gguf",
                "status": "salvage_failed",
                "error_code": "salvage_refused_non_receipt",
                "refusal_class": "weights",
            }])
            remote.assert_not_called()
            scp_base.assert_not_called()
            self.assertFalse((destination / "Qwen3.5-9B-Q4_K_M.gguf").exists())

    def test_salvage_stops_without_scp_when_only_deletion_reserve_remains(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_salvage_reserve")
        info = {"phase_id": "j1m-test", "instance_info": {"ssh_user": "u", "ip": "127.0.0.1"}}
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)), \
                mock.patch.object(orchestrator.sf, "_preflight"), \
                mock.patch.object(orchestrator.sf, "scp_base", return_value=["scp"]), \
                mock.patch.object(orchestrator, "_remote") as remote:
            destination = Path(directory) / "out"
            destination.mkdir(mode=0o700)
            known_hosts = Path(directory) / "known"
            known_hosts.write_text("host ssh-ed25519 AAAA\n", encoding="utf-8")
            known_hosts.chmod(0o600)
            results = orchestrator._salvage(
                info, Path(directory) / "id", known_hosts, destination,
                ["eval-receipt.json", "cuda-device-receipt.json"],
                deadline=time.monotonic() + 0.01,
            )
            self.assertEqual(
                [item["error_code"] for item in results],
                ["salvage_deadline_reserve", "salvage_deadline_reserve"],
            )
        remote.assert_not_called()

    def test_eval_deadline_envelope_keeps_host_shutdown_jitter(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_jitter")
        config = load(ROOT / "scripts/j1m_runner.py", "j1m_jitter_config").load_config()
        envelope = orchestrator._eval_deadline_ceiling(config)
        self.assertGreaterEqual(envelope["watchdog_seconds"] - envelope["host_shutdown_from_create_seconds"], 120)

    @isolated_lifecycle_execute
    def test_insufficient_backstop_is_rejected_before_catalogue_or_key_mutation(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_backstop_gate")
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            with mock.patch.object(orchestrator.sf, "load_env", return_value={"SHADEFORM_API_KEY": "api", "SHADEFORM_AUTO_TERMINATE_HOURS": "0.1"}), \
                    mock.patch.object(orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", Path(directory)), \
                    mock.patch.object(orchestrator.sf, "require_env", return_value="api"), \
                    mock.patch.object(orchestrator.sf, "list_candidates") as list_candidates, \
                    mock.patch.object(orchestrator.sf, "create_ephemeral_ssh_key") as create_key:
                with self.assertRaises(orchestrator.sf.BackstopError):
                    orchestrator.execute(
                        Path(directory) / "env", config_path=private_config(Path(directory)),
                        phase_id="backstop-gate", run_id="test", artifact_destination=Path(directory) / "artifacts", mode="prove",
                    )
            list_candidates.assert_not_called()
            create_key.assert_not_called()

    def test_orchestrator_stdout_tail_is_bounded_typed_and_failure_only(self):
        """The typed refusal has to be readable; a completed stage keeps nothing."""

        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_no_stdout")
        refusal = json.dumps({"error_code": "input_rejected", "status": "refused"}, sort_keys=True)
        result = types.SimpleNamespace(returncode=2, stdout=refusal, stderr="")
        with mock.patch.object(orchestrator.subprocess, "run", return_value=result):
            receipt = orchestrator._remote(["ssh", "host", "eval"], timeout=1)
        # This is the exact stdout j1m_runner._safe_cli printed on run
        # j1m-eval-20260911-remote-d, which the receipt could not show.
        self.assertEqual(receipt["stdout_tail"], refusal)
        self.assertEqual(receipt["error_type"], "remote_exit")

        succeeded = types.SimpleNamespace(returncode=0, stdout="a" * 5000, stderr="")
        with mock.patch.object(orchestrator.subprocess, "run", return_value=succeeded):
            completed_receipt = orchestrator._remote(["ssh", "host", "eval"], timeout=1)
        self.assertNotIn("stdout_tail", completed_receipt)

        noisy = types.SimpleNamespace(returncode=2, stdout="b" * 5000, stderr="")
        with mock.patch.object(orchestrator.subprocess, "run", return_value=noisy):
            bounded = orchestrator._remote(["ssh", "host", "eval"], timeout=1)
        self.assertEqual(len(bounded["stdout_tail"]), orchestrator._STDERR_TAIL_LIMIT)
        self.assertLessEqual(len(bounded["stdout_tail"]), 2048)

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
        self.assertEqual(config["artifacts"]["eval_fetch_allowlist"], ["eval-receipt.json", "startup-preflight-receipt.json", "eval-artifact-receipt.json", "toolchain-receipt.json", "cuda-device-receipt.json", "command-receipt.json"])
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
        # ``--token-file`` is deliberately absent: it named a path on a host
        # this process has not contacted, which ``validate_persisted_argv``
        # can never accept as a private handle, so every eval argv was
        # refused before it could spawn.  The remote evaluator now creates
        # its bearer token in an owner-private directory of its own.
        self.assertNotIn("--token-file", flattened)
        self.assertFalse([part for part in flattened if part.endswith("engine-token")])
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
        self.assertEqual(names, {"Qwen3.5-9B-Q4_K_M.gguf", "model-manifest.json", "model-manifest.sha256", "remote_model_eval.py", "remote_eval_prepare.py", "evaluate_tool_calls.py", "cuda_device_probe.py", "remote_toolchain_probe.py", "cuda_source_closure.py", "ggml-cuda-source-lock.json", "production_tool_call_eval.json", "CMakeLists.txt", "native", "runtime_tests.cpp", "model_validator_tests.cpp"})
        self.assertTrue(any(recursive and local.name == "native" for local, _remote, recursive in uploads))
        closure_upload = next((remote for local, remote, _recursive in uploads if local.name == "cuda_source_closure.py"), None)
        self.assertEqual(closure_upload, "/scratch/j1m/engine/scripts/cuda_source_closure.py")
        lock_upload = next((remote for local, remote, _recursive in uploads if local.name == "ggml-cuda-source-lock.json"), None)
        self.assertEqual(lock_upload, "/scratch/j1m/ggml-cuda-source-lock.json")
        self.assertIn((ROOT / "vendor/llama.cpp/ggml/CMakeLists.txt", "/scratch/j1m/ggml-CMakeLists.txt", False), uploads)
        self.assertIn((ROOT / "tests/native/runtime_tests.cpp", "/scratch/j1m/engine/tests/native/runtime_tests.cpp", False), uploads)
        self.assertIn((ROOT / "tests/native/model_validator_tests.cpp", "/scratch/j1m/engine/tests/native/model_validator_tests.cpp", False), uploads)
        source = (ROOT / "scripts/j1m_orchestrator.py").read_text(encoding="utf-8")
        # [:3]/[3:] before J1M-HOST-PRIVACY-001, [:4]/[4:] once the bootstrap
        # carried the `chmod 700`. The bound is now DERIVED from the timeout
        # tuple, so a plan change cannot re-time a stage or drop one silently.
        self.assertIn("eval_commands[:_EVAL_BOOTSTRAP_STAGE_COUNT]", source)
        self.assertIn("eval_commands[_EVAL_BOOTSTRAP_STAGE_COUNT:]", source)
        self.assertNotIn("eval_commands[:4]", source)
        commands = orchestrator._eval_remote_commands(j1m.load_config(), "/scratch/j1m")
        self.assertEqual(commands[0][:2], ["mkdir", "-p"])
        self.assertEqual(commands[1][:2], ["chmod", "700"])
        self.assertEqual(commands[2][:2], ["sudo", "apt-get"])
        self.assertEqual(commands[3][:5], ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"])
        # The trust store is republished readable immediately after the install
        # that may have regenerated it under `umask 077`, and before any
        # non-root stage needs TLS. Scoped to certs, never to /etc/ssl itself.
        self.assertEqual(commands[4], ["sudo", "chmod", "-R", "go+rX", "/etc/ssl/certs"])
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
        self.assertIn(["python3", "/scratch/j1m/j1m_runner.py", "--run", "--config", "/scratch/j1m/j1m-config.json", "--lock", "/scratch/j1m/qwen35-9b.source-lock.json"], commands)
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            source = root / "native"
            (source / "engine").mkdir(parents=True, mode=0o700)
            (source / "engine" / "marker.txt").write_text("native", encoding="utf-8")
            target = root / "engine"
            target.mkdir(mode=0o700)
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "eval-artifact-receipt.json"
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_artifact_receipt(path, artifact)
            path.write_text(json.dumps(receipt), encoding="utf-8")
            path.chmod(0o600)
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
            path.chmod(0o600)
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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

    def test_remote_eval_diagnostics_are_finite_and_totals_bound(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_diagnostics")
        categories = {"tool_selection"}
        metrics = {
            "case_count": 2, "passed": 1, "failed": 0, "errors": 1, "peak_rss_kib": 10,
            "category_summary": {"tool_selection": {"case_count": 2, "passed": 1, "failed": 0, "errors": 1}},
            "canary": {"attempted": True, "passed": True, "error_code": None, "tool_count": 11, "message_chars": 2400, "prompt_tokens": 700, "context_tokens": 2048, "output_reserve_tokens": 64},
            "error_diagnostics": {"schema": "local_bmo.tool-call-eval-diagnostics.v1", "total_errors": 1, "overall": {"http_503": 1}, "by_category": {"tool_selection": {"http_503": 1}}},
            "quality_diagnostics": {"schema": "local_bmo.tool-call-quality-diagnostics.v1", "total_failed": 0, "overall": {}, "by_category": {"tool_selection": {}}},
        }
        parsed, all_passed, has_failure = remote._parse_evaluator_result(
            {"status": "failed", "exit_code": 1, "stdout": json.dumps(metrics)},
            expected_case_count=2, expected_categories=categories, require_diagnostics=True,
        )
        self.assertEqual(parsed["error_diagnostics"]["overall"], {"http_503": 1})
        self.assertFalse(all_passed)
        self.assertTrue(has_failure)
        for bad in (
            {**metrics, "error_diagnostics": {**metrics["error_diagnostics"], "overall": {"bogus": 1}}},
            {**metrics, "error_diagnostics": {**metrics["error_diagnostics"], "total_errors": 0}},
            {**metrics, "error_diagnostics": {**metrics["error_diagnostics"], "extra": False}},
            {**metrics, "error_diagnostics": {**metrics["error_diagnostics"], "overall": {"http_503": 1}, "by_category": {"tool_selection": {}}}},
            {**metrics, "canary": {**metrics["canary"], "passed": False, "error_code": "http_503", "prompt_tokens": None}},
            {**metrics, "category_summary": {"tool_selection": {"case_count": 2, "passed": 0, "failed": 2, "errors": 0}}},
        ):
            with self.assertRaises(ValueError):
                remote._validate_metrics(bad, expected_case_count=2, expected_categories=categories, require_diagnostics=True)

    def test_remote_eval_fixture_category_distribution_is_bound(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_category_distribution")
        categories = {"tool_selection", "abstention"}
        summary = {
            "tool_selection": {"case_count": 2, "passed": 2, "failed": 0, "errors": 0},
            "abstention": {"case_count": 0, "passed": 0, "failed": 0, "errors": 0},
        }
        metrics = {"case_count": 2, "passed": 2, "failed": 0, "errors": 0, "peak_rss_kib": None, "category_summary": summary}
        with self.assertRaises(ValueError):
            remote._validate_metrics(metrics, expected_case_count=2, expected_categories=categories, expected_category_counts={"tool_selection": 1, "abstention": 1})

    def test_remote_eval_verifies_same_model_manifest_identity(self):
        remote = load(ROOT / "scripts/test/remote_model_eval.py", "remote_model_eval_identity")
        config = load(ROOT / "scripts/j1m_runner.py", "remote_model_eval_identity_config").load_config()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        fixture = json.loads((ROOT / "tests/model/production_tool_call_eval.json").read_text(encoding="utf-8"))
        category_counts = {category: sum(case["category"] == category for case in fixture["cases"]) for category in {case["category"] for case in fixture["cases"]}}
        category_summary = {category: {"case_count": count, "passed": count, "failed": 0, "errors": 0} for category, count in category_counts.items()}
        case_count = len(fixture["cases"])
        fixture_identity = orchestrator._tool_eval_contract()["fixture_identity"]
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            receipt = Path(directory) / "eval-receipt.json"
            receipt.write_text(json.dumps({
                "schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "verified", "artifact": artifact, "fixture": fixture_identity,
                "engine": {"engine_version": "0.1.0", "api_version": "0.1.0", "compiled_backend": f"llama.cpp/{config['llama_cpp']['revision'][:8]}/cuda", "llama_cpp_revision": config["llama_cpp"]["revision"], "model": "qwen35-9b-q4-k-m"},
                "model_preflight": {"valid": True, "code": "ok", "status": "verified", "size_bytes": 4, "sha256": "a" * 64, "gguf_version": 3},
                "cuda_device": {"schema": "local_bmo.j1m.cuda-device-receipt.v1", "status": "verified", "selector": "CUDA0", "device_count": 1, "device": {"index": 0, "name": "NVIDIA A100 80GB", "memory_total_mib": 81920, "driver_version": "550.1"}, "source": "nvidia-smi bounded query"},
                "toolchain": {"schema": "local_bmo.j1m.remote-toolchain-receipt.v1", "status": "verified", "required": {"python3": ">=3.8", "git": ">=2.30", "cmake": ">=3.18", "g++": ">=9.0", "nvcc": ">=12.0"}, "versions": {"python3": {"major": 3, "minor": 10, "reported": "Python 3.10", "executable": "/usr/bin/python3"}, "git": {"major": 2, "minor": 39, "reported": "git version 2.39", "executable": "/usr/bin/git"}, "cmake": {"major": 3, "minor": 22, "reported": "cmake version 3.22", "executable": "/usr/bin/cmake"}, "g++": {"major": 11, "minor": 4, "reported": "g++ (Ubuntu 11.4)", "executable": "/usr/bin/g++"}, "nvcc": {"major": 12, "minor": 2, "reported": "Cuda compilation tools, release 12.2", "executable": "/usr/local/cuda/bin/nvcc"}}, "packages": {"ca-certificates": "20240101", "cmake": "3.22.1", "build-essential": "12.9", "git": "1:2.39.2", "python3": "3.10.12", "python3-venv": "3.10.12"}, "package_install": "ubuntu apt repositories; exact resolved package versions captured by dpkg-query"},
                "metrics": {"case_count": case_count, "passed": case_count, "failed": 0, "errors": 0, "peak_rss_kib": 123, "category_summary": category_summary, "canary": {"attempted": True, "passed": True, "error_code": None, "tool_count": 33, "message_chars": 2400, "prompt_tokens": 700, "context_tokens": 8192, "output_reserve_tokens": 256}, "error_diagnostics": {"schema": "local_bmo.tool-call-eval-diagnostics.v1", "total_errors": 0, "overall": {}, "by_category": {category: {} for category in category_summary}}, "quality_diagnostics": {"schema": "local_bmo.tool-call-quality-diagnostics.v1", "total_failed": 0, "overall": {}, "by_category": {category: {} for category in category_summary}}},
                "prompt_response_logging": False, "tokens_logged": False,
            }), encoding="utf-8")
            selected = orchestrator._verify_eval_receipt(receipt, artifact)
            self.assertEqual(selected["metrics"]["case_count"], case_count)
            self.assertEqual(set(selected), {"status", "artifact", "fixture", "engine", "model_preflight", "cuda_device", "metrics", "toolchain"})
            self.assertEqual(selected["artifact"]["modality"], "text_only_no_mmproj")
            self.assertEqual(selected["artifact"]["quantization"], "Q4_K_M")
            self.assertEqual(set(selected["toolchain"]["versions"]), {"python3", "git", "cmake", "g++", "nvcc"})
            valid_payload = json.loads(receipt.read_text(encoding="utf-8"))
            hostile_quality = json.loads(json.dumps(valid_payload))
            hostile_quality["metrics"]["quality_diagnostics"]["overall"] = {"raw_model_text": 1}
            hostile_quality["metrics"]["quality_diagnostics"]["by_category"] = {
                category: {"raw_model_text": 1} for category in category_summary
            }
            receipt.write_text(json.dumps(hostile_quality), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "quality diagnostics"):
                orchestrator._verify_eval_receipt(receipt, artifact)
            receipt.write_text(json.dumps(valid_payload), encoding="utf-8")
            hostile_fixture = json.loads(json.dumps(valid_payload))
            hostile_fixture["fixture"]["sha256"] = "0" * 64
            receipt.write_text(json.dumps(hostile_fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixture identity"):
                orchestrator._verify_eval_receipt(receipt, artifact)
            receipt.write_text(json.dumps(valid_payload), encoding="utf-8")
            redistributed = json.loads(json.dumps(valid_payload))
            categories = list(redistributed["metrics"]["category_summary"])
            redistributed["metrics"]["category_summary"][categories[0]]["case_count"] += 1
            redistributed["metrics"]["category_summary"][categories[0]]["passed"] += 1
            redistributed["metrics"]["category_summary"][categories[1]]["case_count"] -= 1
            redistributed["metrics"]["category_summary"][categories[1]]["passed"] -= 1
            receipt.write_text(json.dumps(redistributed), encoding="utf-8")
            with self.assertRaises(ValueError):
                orchestrator._verify_eval_receipt(receipt, artifact)
            receipt.write_text(json.dumps(valid_payload), encoding="utf-8")
            child_failed = json.loads(receipt.read_text(encoding="utf-8"))
            child_failed["status"] = "failed"
            child_failed["child"] = {"exit_code": 1}
            receipt.write_text(json.dumps(child_failed), encoding="utf-8")
            child_selected = orchestrator._verify_eval_receipt(receipt, artifact)
            self.assertEqual(child_selected["status"], "failed")
            self.assertEqual(child_selected["child"], {"exit_code": 1})
            self.assertIn("error_diagnostics", child_selected["metrics"])
            child_failed.pop("child")
            child_failed["status"] = "verified"
            receipt.write_text(json.dumps(child_failed), encoding="utf-8")
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
            failed["metrics"]["quality_diagnostics"] = {"schema": "local_bmo.tool-call-quality-diagnostics.v1", "total_failed": 1, "overall": {"missing_call": 1}, "by_category": {category: ({"missing_call": 1} if item["failed"] else {}) for category, item in failed["metrics"]["category_summary"].items()}}
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, mock.patch.object(probe.subprocess, "run", return_value=result) as run:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, mock.patch.object(probe.subprocess, "run", side_effect=run):
            receipt = probe.probe(Path(directory) / "toolchain-receipt.json", nvcc="/usr/local/cuda/bin/nvcc")
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(json.loads((Path(directory) / "toolchain-receipt.json").read_text())["versions"]["cmake"]["major"], 3)
            self.assertEqual(receipt["packages"]["cmake"], "3.22.1")
            self.assertEqual(receipt["versions"]["nvcc"]["executable"], "/usr/local/cuda/bin/nvcc")
        with mock.patch.object(probe.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "cmake_unavailable"):
                probe._probe("cmake")
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            receipt = Path(directory) / "eval-receipt.json"
            receipt.write_text(json.dumps({"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "verified", "artifact": artifact, "model_preflight": {"valid": True, "code": "ok", "status": "verified", "size_bytes": 4, "sha256": "a" * 64, "gguf_version": 3}, "engine": {"llama_cpp_revision": "b" * 40, "compiled_backend": "llama.cpp/bbbbbbbb/cpu"}, "metrics": {"case_count": case_count, "passed": case_count, "failed": 0, "errors": 0, "peak_rss_kib": 1, "category_summary": category_summary}, "prompt_response_logging": False, "tokens_logged": False}), encoding="utf-8")
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
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
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            receipt = Path(directory) / "eval-receipt.json"
            args = ["--model", "m", "--model-manifest", "mm", "--model-manifest-lock", "ml", "--source-revision", "c" * 40, "--llama-revision", "b" * 40, "--llama-checkout", "checkout", "--engine", "engine", "--evaluator", "eval", "--fixture", "fixture", "--token-file", "token", "--toolchain-receipt", "toolchain", "--receipt", str(receipt)]
            failed_metrics = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "completed_with_failures", "artifact": artifact, "engine": {"llama_cpp_revision": "b" * 40, "compiled_backend": "llama.cpp/bbbbbbbb/cpu"}, "metrics": {"case_count": 8, "passed": 7, "failed": 1, "errors": 0, "peak_rss_kib": 1}, "prompt_response_logging": False, "tokens_logged": False}
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
    """Exercise fail-closed lifecycle boundaries with mocked external edges."""

    def test_killed_launcher_without_intent_fails_closed(self):
        from scripts import shadeform_watchdog as watchdog
        from scripts import shadeform_lifecycle as sf

        nonce = "0" * 32
        with mock.patch.object(watchdog, "identity_alive", return_value=False), \
                mock.patch.object(sf, "read_phase_ownership", return_value=(None, False)), \
                mock.patch.object(watchdog, "_pending_intent", return_value=None), \
                mock.patch.object(sf, "load_env") as load_env, \
                mock.patch.object(sf, "request") as provider:
            result = watchdog.main([
                "--phase-id", "j1m-loopback-no-orphan",
                "--instance-name", "ep-run-" + nonce,
                "--launcher-pid", "1234", "--max-seconds", "1",
                "--deadline-epoch", str(time.time() + 1000),
                "--provider-delete-deadline-epoch", str(time.time() + 900),
                "--allow-unrecorded-exact", "--ownership-nonce", nonce,
                "--ssh-key-id", "key-loopback-1", "--ssh-key-name", "j1m-" + nonce,
                "--ssh-key-fingerprint", "A" * 43,
                "--cloud", "cloud", "--region", "region", "--instance-type", "type",
                "--hourly-usd", "1", "--gpu", "A100", "--gpu-count", "1",
                "--vram-gb", "80", "--os-image", "ubuntu",
            ])
        self.assertEqual(result, 1)
        load_env.assert_not_called()
        provider.assert_not_called()

    def test_mocked_transport_failure_is_a_result(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator")
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(orchestrator.subprocess, "run", return_value=completed) as run:
            result = orchestrator._remote(["ssh", "host"], timeout=0.01)
        run.assert_called_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "remote_exit")

    def test_remote_stderr_is_bounded_and_redacted(self):
        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "j1m_orchestrator_stderr")
        completed = subprocess.CompletedProcess([], 3, stdout="", stderr="api_key=secret-value " * 300)
        with mock.patch.object(orchestrator.subprocess, "run", return_value=completed):
            result = orchestrator._remote(["ssh", "host"], timeout=5)
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
