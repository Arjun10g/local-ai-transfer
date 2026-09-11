"""Bounded receipt-salvage transport for the J1M orchestrator.

`2d7db4f` removed the SCP fetch loop because a pathname handed to SCP could not
stay bound to the validated destination across an untrusted transfer. These
tests pin the replacement: a source-fixed receipt allowlist under one fixed
remote directory, an exact non-shell argv, hard size/count/clock caps, strict
validation of untrusted bytes, and publication only through the descriptor-safe
writer. No network, provider, or real transfer occurs: `_remote` is always
mocked, and a real transfer would be a test failure.
"""

import hashlib
import importlib.util
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]

ARTIFACT = {
    "name": "Qwen3.5-9B-Q4_K_M.gguf",
    "size_bytes": 5_629_109_088,
    "sha256": "c6" * 32,
}
RUN_IDENTITY = {"run_id": "J1M", "instance_id": "instance-123456", "artifact": ARTIFACT}
HOST_KEY = {"status": "verified", "fingerprint": "SHA256:" + "A" * 43, "key_count": 1,
            "proof": "two-stable-bounded-scans-residual-tofu"}


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def receipt_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def eval_artifact_receipt() -> dict:
    return {
        "schema": "local_bmo.j1m.remote-eval-artifact-receipt.v1",
        "status": "verified",
        "name": ARTIFACT["name"],
        "size_bytes": ARTIFACT["size_bytes"],
        "sha256": ARTIFACT["sha256"],
        "manifest_sha256": "3b" * 32,
        "manifest_lock_sha256": "3b" * 32,
    }


def cuda_device_receipt() -> dict:
    return {
        "schema": "local_bmo.j1m.cuda-device-receipt.v1",
        "status": "verified",
        "selector": "CUDA0",
        "device_count": 1,
        "device": {"name": "NVIDIA A100 80GB PCIe", "memory_total_mib": 81920},
        "source": "nvidia-smi bounded query",
    }


def toolchain_receipt() -> dict:
    return {
        "schema": "local_bmo.j1m.remote-toolchain-receipt.v1",
        "status": "verified",
        "required": {"nvcc": ">=12.0"},
        "versions": {"nvcc": "12.4.131"},
        "packages": {"build-essential": "12.9ubuntu3"},
        "package_install": "ubuntu apt repositories",
    }


def startup_preflight_receipt() -> dict:
    return {
        "schema": "local_bmo.j1m.startup-preflight-receipt.v1",
        "status": "verified",
        "size_bytes": ARTIFACT["size_bytes"],
        "sha256": ARTIFACT["sha256"],
        "gguf_version": 3,
    }


def eval_receipt() -> dict:
    return {
        "schema": "local_bmo.j1m.real-tool-eval-receipt.v1",
        "status": "verified",
        "artifact": {
            "name": ARTIFACT["name"],
            "size_bytes": ARTIFACT["size_bytes"],
            "sha256": ARTIFACT["sha256"],
        },
        "fixture": {"sha256": "c7" * 32, "case_count": 37},
        "engine": {"backend": "cuda", "gpu_layers": 99},
        "model_preflight": {"valid": True, "code": "ok", "status": "verified"},
        "toolchain": {"schema": "local_bmo.j1m.remote-toolchain-receipt.v1", "status": "verified"},
        "metrics": {"case_count": 37, "passed": 30, "failed": 6, "errors": 1},
    }


class FakeTransfer:
    """Stand in for one `scp` process; writes bytes to the staged local path.

    Recording the exact argv is the point: nothing here builds a shell string,
    and the remote operand must always be the fixed directory joined with one
    allowlisted basename.
    """

    def __init__(self, payloads: dict, *, outcome: dict | None = None):
        self.payloads = payloads
        self.outcome = outcome
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command, *, timeout):
        self.calls.append(list(command))
        self.timeouts.append(timeout)
        if self.outcome is not None:
            return dict(self.outcome)
        staged = Path(command[-1])
        name = command[-2].rsplit("/", 1)[-1]
        payload = self.payloads.get(name)
        if payload is None:
            return {"status": "failed", "exit_code": 1, "error_type": "remote_exit",
                    "stderr_tail": "scp: No such file or directory"}
        staged.write_bytes(payload)
        staged.chmod(0o600)
        return {"status": "completed", "exit_code": 0, "stderr_tail": ""}


class SalvageTransportTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "salvage_orchestrator")
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.directory.name)
        os.chmod(self.root, 0o700)
        self.destination = self.root / "artifacts"
        self.destination.mkdir(mode=0o700)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "203.0.113.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample\n", encoding="utf-8")
        self.known_hosts.chmod(0o600)
        # A canonical private handle, as `validate_persisted_argv` requires for
        # the `-i` operand.
        self.key_dir = self.root / "ssh"
        self.key_dir.mkdir(mode=0o700)
        self.identity = self.key_dir / "ssh-key"
        self.identity.write_text("PRIVATE KEY MATERIAL DO NOT LOG\n", encoding="utf-8")
        self.identity.chmod(0o600)
        self.info = {
            "phase_id": "j1m-salvage-test",
            "instance_info": {"ip": "203.0.113.9", "ssh_user": "ubuntu", "ssh_port": 2222},
        }
        self.addCleanup(self.directory.cleanup)

    def salvage(self, names, payloads=None, *, outcome=None, deadline=None,
                run_identity=RUN_IDENTITY, host_key=HOST_KEY, identity=None,
                known_hosts=None, destination=None):
        transfer = FakeTransfer(payloads or {}, outcome=outcome)
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator.sf, "_preflight"), \
                mock.patch.object(self.orchestrator, "_remote", side_effect=transfer):
            results = self.orchestrator._salvage(
                self.info,
                self.identity if identity is None else identity,
                self.known_hosts if known_hosts is None else known_hosts,
                self.destination if destination is None else destination,
                names,
                deadline=deadline,
                run_identity=run_identity,
                host_key=host_key,
            )
        return results, transfer

    def salvage_receipt(self) -> dict:
        return json.loads((self.destination / "salvage-receipt.json").read_text(encoding="utf-8"))

    def codes(self, results) -> list:
        return [item.get("error_code", item.get("status")) for item in results]

    # ----------------------------------------------------------------- argv

    def test_transport_argv_is_an_exact_option_bound_argument_vector(self):
        results, transfer = self.salvage(
            ["cuda-device-receipt.json"],
            {"cuda-device-receipt.json": receipt_bytes(cuda_device_receipt())},
        )
        self.assertEqual([item["status"] for item in results], ["completed"])
        self.assertEqual(len(transfer.calls), 1)
        argv = transfer.calls[0]
        # An argument vector, never a shell string.
        self.assertIsInstance(argv, list)
        for item in argv:
            self.assertIsInstance(item, str)
        for metacharacter in (";", "|", "&&", "$(", "`", ">", "<", "\n"):
            self.assertNotIn(metacharacter, " ".join(argv))
        self.assertTrue(argv[0].endswith("scp"))
        options = {argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-o"}
        for required in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes",
                         "ForwardAgent=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
                         "ConnectTimeout=15"):
            self.assertIn(required, options)
        self.assertTrue(any(item.startswith("UserKnownHostsFile=") and str(self.known_hosts) in item
                            for item in options))
        self.assertIn("-4", argv)                                  # IPv4 only
        self.assertEqual(argv[argv.index("-i") + 1], str(self.identity))
        self.assertEqual(argv[argv.index("-P") + 1], "2222")       # exact creation-record port
        self.assertEqual(argv[-2], "ubuntu@203.0.113.9:/scratch/j1m/artifacts/cuda-device-receipt.json")
        self.assertNotIn("-r", argv)                               # no recursion
        # The local operand is the private staging path, never the destination.
        self.assertFalse(str(self.destination) in argv[-1])

    def test_transport_argv_satisfies_the_persisted_argv_credential_policy(self):
        """The argv reaching `_remote` must survive `validate_persisted_argv`."""

        _results, transfer = self.salvage(
            ["toolchain-receipt.json"],
            {"toolchain-receipt.json": receipt_bytes(toolchain_receipt())},
        )
        self.orchestrator.j1m_runner.validate_persisted_argv(transfer.calls[0])

    def test_transport_never_receives_the_validated_destination_pathname(self):
        """The `2d7db4f` TOCTOU concern: SCP is not given the real destination."""

        _results, transfer = self.salvage(
            ["eval-receipt.json"], {"eval-receipt.json": receipt_bytes(eval_receipt())},
        )
        flattened = " ".join(transfer.calls[0])
        self.assertNotIn(str(self.destination), flattened)
        self.assertTrue((self.destination / "eval-receipt.json").exists())

    # ------------------------------------------------------------ allowlist

    def test_only_source_allowlisted_names_are_ever_fetched(self):
        hostile = [
            "../../etc/passwd",
            "/etc/shadow",
            "eval-receipt.json/../../root/.ssh/id_rsa",
            "*.json",
            "Qwen3.5-9B-Q4_K_M.gguf",
            "salvage-receipt.json",
            "",
        ]
        results, transfer = self.salvage(hostile)
        self.assertEqual(transfer.calls, [])
        self.assertEqual(
            self.codes(results), ["salvage_name_not_allowlisted"] * len(hostile))
        self.assertEqual(sorted(item.name for item in self.destination.iterdir()),
                         ["salvage-receipt.json"])

    def test_non_string_names_are_refused_without_constructing_a_remote_path(self):
        results, transfer = self.salvage([None, 17, {"name": "eval-receipt.json"}])
        self.assertEqual(transfer.calls, [])
        self.assertEqual(self.codes(results), ["salvage_name_not_allowlisted"] * 3)

    def test_a_repeated_allowlisted_name_is_fetched_at_most_once(self):
        payloads = {"cuda-device-receipt.json": receipt_bytes(cuda_device_receipt())}
        results, transfer = self.salvage(
            ["cuda-device-receipt.json", "cuda-device-receipt.json"], payloads)
        self.assertEqual(len(transfer.calls), 1)
        self.assertEqual(self.codes(results), ["completed", "salvage_duplicate_name"])

    def test_a_request_larger_than_the_bounded_candidate_count_is_refused(self):
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator, "_remote") as remote:
            with self.assertRaisesRegex(ValueError, "bounded candidate count"):
                self.orchestrator._salvage(
                    self.info, self.identity, self.known_hosts, self.destination,
                    ["eval-receipt.json"] * 65, host_key=HOST_KEY,
                )
        remote.assert_not_called()

    def test_fetch_count_never_exceeds_the_allowlist_size(self):
        self.assertEqual(self.orchestrator._SALVAGE_MAX_FILES,
                         len(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST))
        names = sorted(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST)
        payloads = {
            "cuda-device-receipt.json": receipt_bytes(cuda_device_receipt()),
            "eval-artifact-receipt.json": receipt_bytes(eval_artifact_receipt()),
            "eval-receipt.json": receipt_bytes(eval_receipt()),
            "startup-preflight-receipt.json": receipt_bytes(startup_preflight_receipt()),
            "toolchain-receipt.json": receipt_bytes(toolchain_receipt()),
        }
        results, transfer = self.salvage(names, payloads)
        self.assertLessEqual(len(transfer.calls), self.orchestrator._SALVAGE_MAX_FILES)
        outcome = {item["name"]: item.get("error_code", item["status"]) for item in results}
        # `proving-receipt.json` is allowlisted but absent on an eval host; it
        # is recorded as missing, and the other five still land.
        self.assertEqual(outcome.pop("proving-receipt.json"), "salvage_transport_failed")
        self.assertEqual(sorted(set(outcome.values())), ["completed"])

    # ---------------------------------------------------------------- caps

    def test_a_file_beyond_the_per_file_cap_is_refused_and_never_published(self):
        oversize = b"[" + b"0," * (3 * 1024 * 1024) + b"0]"
        self.assertGreater(len(oversize), self.orchestrator._SALVAGE_MAX_FILE_BYTES // 2)
        results, _transfer = self.salvage(
            ["eval-receipt.json"], {"eval-receipt.json": oversize})
        self.assertEqual(self.codes(results), ["salvage_oversize"])
        self.assertFalse((self.destination / "eval-receipt.json").exists())

    def test_per_file_cap_refuses_rather_than_truncating(self):
        limit = self.orchestrator._SALVAGE_MAX_FILE_BYTES
        with mock.patch.object(self.orchestrator, "_SALVAGE_MAX_FILE_BYTES", 64):
            results, _transfer = self.salvage(
                ["toolchain-receipt.json"],
                {"toolchain-receipt.json": receipt_bytes(toolchain_receipt())},
            )
        self.assertEqual(self.codes(results), ["salvage_oversize"])
        self.assertFalse((self.destination / "toolchain-receipt.json").exists())
        self.assertEqual(self.orchestrator._SALVAGE_MAX_FILE_BYTES, limit)

    def test_total_cap_stops_further_fetches_after_the_budget_is_consumed(self):
        payloads = {
            "cuda-device-receipt.json": receipt_bytes(cuda_device_receipt()),
            "toolchain-receipt.json": receipt_bytes(toolchain_receipt()),
        }
        budget = len(payloads["cuda-device-receipt.json"])
        with mock.patch.object(self.orchestrator, "_SALVAGE_MAX_TOTAL_BYTES", budget):
            results, transfer = self.salvage(
                ["cuda-device-receipt.json", "toolchain-receipt.json"], payloads)
        self.assertEqual(self.codes(results), ["completed", "salvage_total_size_cap"])
        self.assertEqual(len(transfer.calls), 1)
        self.assertFalse((self.destination / "toolchain-receipt.json").exists())

    def test_a_single_file_exceeding_the_remaining_total_budget_is_refused(self):
        payload = receipt_bytes(cuda_device_receipt())
        with mock.patch.object(self.orchestrator, "_SALVAGE_MAX_TOTAL_BYTES", len(payload) - 1):
            results, transfer = self.salvage(
                ["cuda-device-receipt.json"], {"cuda-device-receipt.json": payload})
        self.assertEqual(self.codes(results), ["salvage_total_size_cap"])
        self.assertEqual(len(transfer.calls), 1)
        self.assertFalse((self.destination / "cuda-device-receipt.json").exists())

    def test_each_transfer_timeout_is_bounded_by_the_per_file_and_wall_clocks(self):
        _results, transfer = self.salvage(
            ["cuda-device-receipt.json"],
            {"cuda-device-receipt.json": receipt_bytes(cuda_device_receipt())},
        )
        self.assertLessEqual(transfer.timeouts[0], self.orchestrator._SALVAGE_FILE_TIMEOUT_SECONDS)
        self.assertGreater(transfer.timeouts[0], 0)

    def test_a_deadline_with_only_the_deletion_reserve_left_spawns_no_transfer(self):
        results, transfer = self.salvage(
            ["eval-receipt.json", "cuda-device-receipt.json"],
            deadline=time.monotonic() + 1.0,
        )
        self.assertEqual(transfer.calls, [])
        self.assertEqual(self.codes(results),
                         ["salvage_deadline_reserve", "salvage_deadline_reserve"])

    def test_an_exhausted_wall_clock_stops_the_remaining_receipts(self):
        payloads = {
            "cuda-device-receipt.json": receipt_bytes(cuda_device_receipt()),
            "toolchain-receipt.json": receipt_bytes(toolchain_receipt()),
        }
        with mock.patch.object(self.orchestrator, "_SALVAGE_WALL_CLOCK_SECONDS", 0.0):
            results, transfer = self.salvage(
                ["cuda-device-receipt.json", "toolchain-receipt.json"], payloads)
        self.assertEqual(transfer.calls, [])
        self.assertEqual(self.codes(results),
                         ["salvage_deadline_reserve", "salvage_deadline_reserve"])

    # --------------------------------------------------------- validation

    def test_invalid_json_is_refused_with_a_typed_reason(self):
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": b"{not json"})
        self.assertEqual(self.codes(results), ["salvage_invalid_json"])
        self.assertFalse((self.destination / "cuda-device-receipt.json").exists())

    def test_duplicate_keys_are_refused_by_the_strict_decoder(self):
        duplicated = (b'{"schema": "local_bmo.j1m.cuda-device-receipt.v1", '
                      b'"status": "verified", "status": "tampered"}')
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": duplicated})
        self.assertEqual(self.codes(results), ["salvage_invalid_json"])
        self.assertFalse((self.destination / "cuda-device-receipt.json").exists())

    def test_a_non_object_payload_is_refused(self):
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": b'["verified"]'})
        self.assertEqual(self.codes(results), ["salvage_not_an_object"])

    def test_a_receipt_declaring_another_schema_is_refused(self):
        payload = cuda_device_receipt()
        payload["schema"] = "local_bmo.j1m.remote-toolchain-receipt.v1"
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": receipt_bytes(payload)})
        self.assertEqual(self.codes(results), ["salvage_schema_mismatch"])

    def test_a_receipt_missing_a_required_top_level_key_is_refused(self):
        payload = eval_receipt()
        del payload["metrics"]
        results, _transfer = self.salvage(
            ["eval-receipt.json"], {"eval-receipt.json": receipt_bytes(payload)})
        self.assertEqual(self.codes(results), ["salvage_required_key_missing"])
        self.assertFalse((self.destination / "eval-receipt.json").exists())

    def test_every_allowlisted_name_declares_a_required_key_set(self):
        self.assertEqual(set(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST),
                         set(self.orchestrator._SALVAGE_REQUIRED_KEYS))
        for required in self.orchestrator._SALVAGE_REQUIRED_KEYS.values():
            self.assertIn("schema", required)

    def test_an_artifact_identity_mismatch_is_refused(self):
        payload = eval_artifact_receipt()
        payload["sha256"] = "de" * 32
        results, _transfer = self.salvage(
            ["eval-artifact-receipt.json"],
            {"eval-artifact-receipt.json": receipt_bytes(payload)},
        )
        self.assertEqual(self.codes(results), ["salvage_identity_mismatch"])
        self.assertFalse((self.destination / "eval-artifact-receipt.json").exists())

    def test_a_nested_artifact_identity_mismatch_in_the_eval_receipt_is_refused(self):
        payload = eval_receipt()
        payload["artifact"]["size_bytes"] = 1
        results, _transfer = self.salvage(
            ["eval-receipt.json"], {"eval-receipt.json": receipt_bytes(payload)})
        self.assertEqual(self.codes(results), ["salvage_identity_mismatch"])

    def test_a_verified_preflight_receipt_for_another_artifact_is_refused(self):
        payload = startup_preflight_receipt()
        payload["sha256"] = "ab" * 32
        results, _transfer = self.salvage(
            ["startup-preflight-receipt.json"],
            {"startup-preflight-receipt.json": receipt_bytes(payload)},
        )
        self.assertEqual(self.codes(results), ["salvage_identity_mismatch"])

    def test_a_receipt_claiming_a_foreign_run_id_is_refused(self):
        payload = cuda_device_receipt()
        payload["run_id"] = "someone-elses-run"
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": receipt_bytes(payload)})
        self.assertEqual(self.codes(results), ["salvage_identity_mismatch"])

    def test_a_receipt_claiming_a_foreign_instance_is_refused(self):
        payload = cuda_device_receipt()
        payload["instance_id"] = "instance-999999"
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": receipt_bytes(payload)})
        self.assertEqual(self.codes(results), ["salvage_identity_mismatch"])

    def test_a_credential_bearing_receipt_is_refused_before_publication(self):
        payload = cuda_device_receipt()
        payload["source"] = "HF_TOKEN=hf_livevalue"
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json"], {"cuda-device-receipt.json": receipt_bytes(payload)})
        self.assertEqual(self.codes(results), ["salvage_receipt_content_refused"])
        self.assertFalse((self.destination / "cuda-device-receipt.json").exists())

    # --------------------------------------------------------- host key

    def test_a_host_key_mismatch_during_transfer_is_a_typed_refusal(self):
        outcome = {
            "status": "failed", "exit_code": 255, "error_type": "remote_exit",
            "stderr_tail": ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                            "Host key verification failed."),
        }
        results, transfer = self.salvage(["eval-receipt.json"], outcome=outcome)
        self.assertEqual(self.codes(results), ["salvage_host_key_mismatch"])
        self.assertEqual(results[0]["exit_code"], 255)
        self.assertEqual(len(transfer.calls), 1)
        self.assertFalse((self.destination / "eval-receipt.json").exists())

    def test_a_missing_pinned_known_hosts_file_refuses_before_any_transfer(self):
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator, "_remote") as remote:
            with self.assertRaisesRegex(ValueError, "host key pin is unavailable"):
                self.orchestrator._salvage(
                    self.info, self.identity, self.root / "absent", self.destination,
                    ["eval-receipt.json"], host_key=HOST_KEY,
                )
        remote.assert_not_called()

    def test_a_world_readable_known_hosts_file_refuses_before_any_transfer(self):
        self.known_hosts.chmod(0o644)
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator, "_remote") as remote:
            with self.assertRaisesRegex(ValueError, "not a private regular file"):
                self.orchestrator._salvage(
                    self.info, self.identity, self.known_hosts, self.destination,
                    ["eval-receipt.json"], host_key=HOST_KEY,
                )
        remote.assert_not_called()

    def test_a_known_hosts_file_changed_since_acquisition_refuses(self):
        """A host swapped between acquisition and teardown cannot be re-pinned."""

        self.known_hosts.write_text(
            "203.0.113.9 ssh-ed25519 AAAAfirst\n203.0.113.9 ssh-rsa AAAAsecond\n",
            encoding="utf-8")
        self.known_hosts.chmod(0o600)
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator, "_remote") as remote:
            with self.assertRaisesRegex(ValueError, "changed since acquisition"):
                self.orchestrator._salvage(
                    self.info, self.identity, self.known_hosts, self.destination,
                    ["eval-receipt.json"], host_key=HOST_KEY,
                )
        remote.assert_not_called()

    def test_an_unverified_host_key_acquisition_refuses(self):
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator, "_remote") as remote:
            with self.assertRaisesRegex(ValueError, "never verified"):
                self.orchestrator._salvage(
                    self.info, self.identity, self.known_hosts, self.destination,
                    ["eval-receipt.json"], host_key={"status": "unverified"},
                )
        remote.assert_not_called()

    # --------------------------------------------------------- transport

    def test_a_transfer_timeout_is_recorded_as_a_typed_failure(self):
        outcome = {"status": "transport_timeout", "exit_code": None,
                   "error_type": "transport_timeout", "stderr_tail": ""}
        results, _transfer = self.salvage(["eval-receipt.json"], outcome=outcome)
        self.assertEqual(self.codes(results), ["salvage_timeout"])

    def test_a_transport_os_failure_is_recorded_as_a_typed_failure(self):
        outcome = {"status": "transport_os", "exit_code": None,
                   "error_type": "transport_os", "stderr_tail": "<redacted>"}
        results, _transfer = self.salvage(["eval-receipt.json"], outcome=outcome)
        self.assertEqual(self.codes(results), ["salvage_transport_os"])

    def test_a_missing_allowlisted_receipt_is_recorded_and_not_fatal(self):
        results, transfer = self.salvage(
            ["cuda-device-receipt.json", "toolchain-receipt.json"],
            {"toolchain-receipt.json": receipt_bytes(toolchain_receipt())},
        )
        self.assertEqual(self.codes(results), ["salvage_transport_failed", "completed"])
        self.assertEqual(len(transfer.calls), 2)
        self.assertTrue((self.destination / "toolchain-receipt.json").exists())

    def test_an_unusable_private_key_handle_is_a_typed_refusal_without_a_process(self):
        self.identity.chmod(0o644)
        results, transfer = self.salvage(["eval-receipt.json"])
        self.assertEqual(self.codes(results), ["salvage_identity_handle_unusable"])
        self.assertEqual(transfer.calls, [])

    def test_salvage_never_raises_for_a_per_file_failure(self):
        """Teardown must remain reachable no matter what the host returned."""

        outcomes = [
            {"status": "failed", "exit_code": 1, "stderr_tail": "denied"},
            {"status": "transport_timeout", "exit_code": None, "stderr_tail": ""},
            {"status": "transport_os", "exit_code": None, "stderr_tail": "<redacted>"},
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome["status"]):
                results, _transfer = self.salvage(
                    sorted(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST), outcome=outcome)
                self.assertEqual(len(results), self.orchestrator._SALVAGE_MAX_FILES)
                self.assertTrue(all(item["status"] == "salvage_failed" for item in results))

    # --------------------------------------------------------- publication

    def test_a_validated_receipt_is_published_byte_exact_and_owner_private(self):
        raw = receipt_bytes(eval_artifact_receipt())
        results, _transfer = self.salvage(
            ["eval-artifact-receipt.json"], {"eval-artifact-receipt.json": raw})
        published = self.destination / "eval-artifact-receipt.json"
        self.assertEqual(published.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(published.stat().st_mode), 0o600)
        self.assertEqual(results[0]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(results[0]["size_bytes"], len(raw))
        self.assertEqual(results[0]["status"], "completed")

    def test_the_orchestrator_verifier_accepts_a_salvaged_artifact_receipt(self):
        """Publication must land in the shape the existing verifier reads."""

        raw = receipt_bytes(eval_artifact_receipt())
        self.salvage(["eval-artifact-receipt.json"], {"eval-artifact-receipt.json": raw})
        with mock.patch.object(self.orchestrator.j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root), \
                mock.patch.object(self.orchestrator, "_APPROVED_EVAL_MANIFEST_SHA256", "3b" * 32):
            verified = self.orchestrator._verify_eval_artifact_receipt(
                self.destination / "eval-artifact-receipt.json", ARTIFACT)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["sha256"], ARTIFACT["sha256"])

    def test_nothing_is_left_behind_in_the_private_staging_directory(self):
        _results, transfer = self.salvage(
            ["cuda-device-receipt.json"],
            {"cuda-device-receipt.json": receipt_bytes(cuda_device_receipt())},
        )
        staged = Path(transfer.calls[0][-1])
        self.assertFalse(staged.exists())
        self.assertFalse(staged.parent.exists())

    # ------------------------------------------------------ salvage receipt

    def test_salvage_receipt_records_bounded_metadata_only(self):
        payloads = {
            "cuda-device-receipt.json": receipt_bytes(cuda_device_receipt()),
            "eval-receipt.json": b"{not json",
        }
        results, _transfer = self.salvage(
            ["cuda-device-receipt.json", "eval-receipt.json", "Qwen3.5-9B-Q4_K_M.gguf"],
            payloads,
        )
        receipt = self.salvage_receipt()
        self.assertEqual(receipt["schema"], "local_bmo.j1m.salvage-receipt.v1")
        self.assertEqual(receipt["status"], "salvage_failed")
        self.assertEqual(receipt["transport"], "scp-argv-bounded-source-allowlist-v1")
        self.assertEqual(receipt["remote_directory"], "/scratch/j1m/artifacts")
        self.assertEqual(receipt["requested"], 3)
        self.assertEqual(receipt["completed"], 1)
        self.assertEqual(receipt["failed"], 2)
        self.assertEqual(receipt["caps"], {
            "per_file_bytes": self.orchestrator._SALVAGE_MAX_FILE_BYTES,
            "total_bytes": self.orchestrator._SALVAGE_MAX_TOTAL_BYTES,
            "max_files": self.orchestrator._SALVAGE_MAX_FILES,
            "wall_clock_seconds": self.orchestrator._SALVAGE_WALL_CLOCK_SECONDS,
            "per_file_timeout_seconds": self.orchestrator._SALVAGE_FILE_TIMEOUT_SECONDS,
        })
        self.assertEqual(receipt["allowlist"],
                         sorted(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST))
        self.assertEqual(receipt["known_hosts_sha256"],
                         hashlib.sha256(self.known_hosts.read_bytes()).hexdigest())
        self.assertEqual(receipt["files"], results)
        self.assertIsInstance(receipt["duration_ms"], int)
        self.assertEqual(receipt["total_bytes"], len(payloads["cuda-device-receipt.json"]))
        self.assertEqual(stat.S_IMODE((self.destination / "salvage-receipt.json").stat().st_mode),
                         0o600)

    def test_salvage_receipt_status_is_salvaged_only_when_everything_landed(self):
        payloads = {
            "cuda-device-receipt.json": receipt_bytes(cuda_device_receipt()),
            "toolchain-receipt.json": receipt_bytes(toolchain_receipt()),
        }
        self.salvage(["cuda-device-receipt.json", "toolchain-receipt.json"], payloads)
        self.assertEqual(self.salvage_receipt()["status"], "salvaged")

    def test_salvage_receipt_holds_no_key_material_token_or_receipt_content(self):
        payloads = {"cuda-device-receipt.json": receipt_bytes(cuda_device_receipt())}
        self.salvage(["cuda-device-receipt.json"], payloads)
        serialized = (self.destination / "salvage-receipt.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE KEY MATERIAL", serialized)
        self.assertNotIn("nvidia-smi bounded query", serialized)   # receipt content
        self.assertNotIn("NVIDIA A100", serialized)
        self.assertNotIn(str(self.identity), serialized)
        self.orchestrator.j1m_runner.validate_persisted_receipt(json.loads(serialized))

    def test_a_failed_salvage_receipt_write_does_not_raise(self):
        with mock.patch.object(self.orchestrator, "_write_salvage_receipt",
                               side_effect=OSError("read-only")):
            results, _transfer = self.salvage(
                ["cuda-device-receipt.json"],
                {"cuda-device-receipt.json": receipt_bytes(cuda_device_receipt())},
            )
        self.assertEqual([item["status"] for item in results], ["completed"])

    # ------------------------------------------------------------ teardown

    def test_salvage_failure_still_reaches_exact_teardown(self):
        """A refusing salvage must not strand a paid instance."""

        orchestrator = self.orchestrator
        order: list[str] = []

        def failing_salvage(*_args, **_kwargs):
            order.append("salvage")
            raise ValueError("salvage destination is not a private directory")

        with mock.patch.object(orchestrator, "_salvage", side_effect=failing_salvage), \
                mock.patch.object(orchestrator, "teardown_exact",
                                  side_effect=lambda *a, **k: order.append("teardown") or {
                                      "deletion": {"success": True}}) as teardown:
            lifecycle = {"instance_info": {"ip": "203.0.113.9"}}
            try:
                orchestrator._salvage(
                    self.info, self.identity, self.known_hosts, self.destination,
                    ["eval-receipt.json"],
                )
            except ValueError:
                lifecycle["salvage"] = [
                    {"status": "salvage_failed", "error_type": "salvage_failed"}]
            orchestrator.teardown_exact("phase", "instance-123456")
        self.assertEqual(order, ["salvage", "teardown"])
        self.assertEqual(lifecycle["salvage"][0]["status"], "salvage_failed")
        teardown.assert_called_once()

    def test_availability_flag_is_consumed_by_the_transport(self):
        self.assertTrue(self.orchestrator._EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE)
        with mock.patch.object(self.orchestrator, "_EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE", False), \
                mock.patch.object(self.orchestrator, "_remote") as remote:
            with self.assertRaisesRegex(ValueError, "transport is unavailable"):
                self.orchestrator._salvage(
                    self.info, self.identity, self.known_hosts, self.destination,
                    ["eval-receipt.json"], host_key=HOST_KEY,
                )
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
