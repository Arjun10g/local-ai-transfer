import contextlib
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RemoteCanaryAndReceiptHardeningTests(unittest.TestCase):
    def setUp(self):
        self.runner = load(ROOT / "scripts/j1m_runner.py", "canary_j1m_runner")
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "canary_j1m_orchestrator")
        self.config = self.runner.load_config()

    def test_canary_plan_is_explicit_no_model_probe_only_and_bounded(self):
        plan = self.runner.build_plan(self.config, "canary")
        commands = self.runner.canary_command_plan(self.config, "/scratch/canary")
        self.assertEqual(plan["mode_policy"], {"no_model": True, "probe_only": True, "salvage_required": True, "teardown_required": True})
        self.assertEqual(plan["artifact_allowlist"], ["toolchain-receipt.json", "cuda-device-receipt.json"])
        self.assertEqual(commands, [
            ["python3", "/scratch/canary/remote_toolchain_probe.py", "--nvcc", "/usr/local/cuda/bin/nvcc", "--output", "/scratch/canary/artifacts/toolchain-receipt.json"],
            ["python3", "/scratch/canary/cuda_device_probe.py", "--output", "/scratch/canary/artifacts/cuda-device-receipt.json"],
        ])
        flattened = " ".join(item for command in commands for item in command).lower()
        for forbidden in ("hf", "convert", "quant", "llama", "cmake", "build", "eval", "model"):
            self.assertNotIn(forbidden, flattened)

    def test_orchestrator_exposes_canary_as_dry_run_only(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.orchestrator.main(["--mode", "canary"]), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan["no_model"])
        self.assertTrue(plan["probe_only"])
        self.assertEqual(plan["execution_status"], "disabled_pending_existing_remote_gates_and_explicit_approval")
        with mock.patch.object(self.orchestrator.sf, "preflight_legacy_deletion_evidence") as preflight:
            with self.assertRaisesRegex(self.orchestrator.sf.ShadeformError, "remains gated"):
                execute = self.orchestrator.execute
                execute(Path("/nonexistent"), config_path=ROOT / "model/conversion/j1m-config.json", phase_id="canary", run_id="run", artifact_destination=Path("/tmp/canary"), mode="canary")
        preflight.assert_not_called()

    def test_hostile_argv_is_rejected_but_private_handle_paths_are_allowed(self):
        hostile = [
            ["tool", "HF_TOKEN=fixture"],
            ["tool", "--env=HF_TOKEN=fixture"],
            ["tool", "--env=SAFE=HF_TOKEN=fixture"],
            ["tool", "HF_TOKEN"],
            ["tool", "НF_TOKEN=fixture"],
            ["tool", "ＨＦ_TOKEN=fixture"],
            ["tool", "--authorization=Bearer fixture"],
            ["tool", "https://provider.test/run?access_token=fixture"],
            ["tool", "https://provider.test/run?%61ccess_token=fixture"],
            ["tool", "https://provider.test/run?%2561ccess_token%253Dfixture"],
            ["tool", "%48F_TOKEN%3Dfixture"],
            ["tool", "--header", "Authorization: Bearer fixture"],
            ["tool", "-HAuthorization: fixture"],
        ]
        for argv in hostile:
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.validate_persisted_argv(argv)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            token_path = root / "run-token"
            identity_path = root / "ssh-key"
            token_path.write_text("handle", encoding="utf-8")
            identity_path.write_text("handle", encoding="utf-8")
            token_path.chmod(0o600)
            identity_path.chmod(0o600)
            self.assertEqual(self.runner.validate_persisted_argv(["tool", "--token-file", str(token_path), "--identity-file=" + str(identity_path)])[-1], "--identity-file=" + str(identity_path))
            for argv in (["tool", "--token-file", "opaque-handle"], ["tool", "--token-file", "relative/token"], ["tool", "--token-file=/private/../token"], ["tool", "--identity-file", "opaque-handle"]):
                with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.validate_persisted_argv(argv)

    def test_unicode_and_deep_percent_security_views_fail_closed(self):
        hostile = [
            ["tool", "HF_\u200bTOKEN=fixture"],
            ["tool", "HF_TOKEN\u202e=fixture"],
            ["tool", "H\u0301F_TOKEN=fixture"],
            ["tool", "H\u093fF_TOKEN=fixture"],
            ["tool", "%25252525252525252548F_TOKEN%253Dfixture"],
            ["tool", "HF_TOKEN%"],
            ["tool", "HF_TOKEN%2"],
            ["tool", "HF_TOKEN%FF=fixture"],
            ["tool", "%" + ("25" * 40000)],
        ]
        for argv in hostile:
            with self.subTest(kind="hostile"):
                with self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.validate_persisted_argv(argv)

    def test_private_handle_validation_rejects_filesystem_ambiguity(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            safe_dir = root / "private"
            safe_dir.mkdir()
            safe_dir.chmod(0o700)
            target = safe_dir / "token-file"
            target.write_text("handle", encoding="utf-8")
            target.chmod(0o600)
            self.assertTrue(self.runner._canonical_private_handle_path(str(target)))
            safe_dir.chmod(0o755)
            self.assertFalse(self.runner._canonical_private_handle_path(str(target)))
            safe_dir.chmod(0o700)
            permissive = safe_dir / "permissive-token"
            permissive.write_text("handle", encoding="utf-8")
            permissive.chmod(0o640)
            self.assertFalse(self.runner._canonical_private_handle_path(str(permissive)))
            hardlink = safe_dir / "hardlink-token"
            hardlink.hardlink_to(target)
            self.assertFalse(self.runner._canonical_private_handle_path(str(hardlink)))
            symlink = safe_dir / "symlink-token"
            symlink.symlink_to(target)
            self.assertFalse(self.runner._canonical_private_handle_path(str(symlink)))
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(safe_dir, target_is_directory=True)
            linked_target = linked_parent / "linked-token"
            self.assertFalse(self.runner._canonical_private_handle_path(str(linked_target)))
            opaque = safe_dir / "opaque"
            opaque.write_text("handle", encoding="utf-8")
            opaque.chmod(0o600)
            self.assertFalse(self.runner._canonical_private_handle_path(str(opaque)))
            with mock.patch.object(self.runner.os, "getuid", return_value=os.getuid() + 1):
                self.assertFalse(self.runner._canonical_private_handle_path(str(target)))

    def test_read_token_file_uses_private_descriptor_boundary(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            token_path = root / "engine-token"
            token_path.write_text("HF_TOKEN=fixture\n", encoding="utf-8")
            token_path.chmod(0o600)
            self.assertEqual(self.runner.read_token_file(token_path), "fixture")
            token_path.chmod(0o640)
            with self.assertRaises(ValueError):
                self.runner.read_token_file(token_path)
            token_path.chmod(0o600)
            token_path.write_text("hf_token=fixture\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.runner.read_token_file(token_path)

    def test_private_handle_open_rechecks_identity_without_leaking_path(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            target = root / "engine-token"
            target.write_text("handle", encoding="utf-8")
            target.chmod(0o600)
            real_open = self.runner.os.open
            real_close = self.runner.os.close
            fd = real_open(str(target), self.runner.os.O_RDONLY | self.runner.os.O_NOFOLLOW)
            real_close(fd)
            with mock.patch.object(self.runner.os, "fstat", return_value=type("FakeStat", (), {"st_dev": 1, "st_ino": 2, "st_uid": 3, "st_nlink": 1, "st_mode": 0o100600})()):
                with self.assertRaisesRegex(ValueError, "changed during validation") as raised:
                    self.runner._open_private_handle(str(target))
                self.assertNotIn(str(target), str(raised.exception))

    def test_private_handle_open_rechecks_parent_identity_after_open(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            target = root / "engine-token"
            target.write_text("handle", encoding="utf-8")
            target.chmod(0o600)
            parent = target.parent
            real_lstat = self.runner.os.lstat
            seen_parent = 0

            def lstat_with_replacement(path):
                nonlocal seen_parent
                result = real_lstat(path)
                if Path(path) == parent:
                    seen_parent += 1
                    if seen_parent > 1:
                        return types.SimpleNamespace(
                            st_mode=result.st_mode,
                            st_uid=result.st_uid,
                            st_nlink=result.st_nlink,
                            st_dev=result.st_dev,
                            st_ino=result.st_ino + 1,
                        )
                return result

            with mock.patch.object(self.runner.os, "lstat", side_effect=lstat_with_replacement):
                with self.assertRaisesRegex(ValueError, "changed during validation"):
                    self.runner._open_private_handle(str(target))

    def test_progress_and_output_receipts_reject_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.json"
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.write_progress(progress, "stage", argv=["tool", "HF_TOKEN=fixture"])
            self.assertFalse(progress.exists())
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.write_progress(progress, "stage", stderr="Authorization: Bearer fixture")
            self.assertFalse(progress.exists())
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.write_progress(progress, "stage", nested={"HF_TOKEN": "fixture"})
            self.assertFalse(progress.exists())
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.validate_persisted_output("prefix\ntoken=fixture\n")
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.validate_persisted_receipt({"HF_TOKEN": "fixture"})
            receipt = Path(directory) / "command-receipt.json"
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.run_commands([["tool", "HF_TOKEN=fixture"]], progress, receipt_path=receipt)
            self.assertFalse(receipt.exists())

    def test_escaped_json_keys_and_nested_values_are_rejected_before_write(self):
        hostile = [
            '{"safe":"HF_TOKEN=fixture"}',
            '{"safe":"\\u0048\\u0046_TOKEN=fixture"}',
            '{"\\u0048\\u0046_TOKEN":"fixture"}',
            '["{\\"safe\\":\\"HF_TOKEN=fixture\\"}"]',
        ]
        for value in hostile:
            with self.subTest(kind="escaped"):
                with self.assertRaisesRegex(ValueError, "credential-like|persisted JSON"):
                    self.runner.validate_persisted_output(value)
                with self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.validate_persisted_argv(["tool", value])
                with self.assertRaisesRegex(ValueError, "credential-like|persisted JSON"):
                    self.runner.validate_persisted_receipt({"nested": value})
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.json"
            with self.assertRaisesRegex(ValueError, "credential-like|persisted JSON"):
                self.runner.write_progress(progress, "stage", nested=hostile[1])
            self.assertFalse(progress.exists())

    def test_security_names_require_ascii_but_ordinary_unicode_values_are_allowed(self):
        hostile = [
            ["tool", "ΗF_TOKEN=fixture"],
            ["tool", "ＡPI_KEY=fixture"],
            ["tool", "https://provider.test/run?ΑPI_KEY=fixture"],
            ["tool", "--ＡPI-KEY=fixture"],
        ]
        for argv in hostile:
            with self.subTest(kind="unicode-name"), self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.validate_persisted_argv(argv)
        self.runner.validate_persisted_output("ordinary café value")
        with self.assertRaisesRegex(ValueError, "credential-like"):
            self.runner.validate_persisted_receipt({"正常": "ordinary"})

    def test_receipt_and_assignment_bounds_are_finite_and_fail_closed(self):
        nested: object = "value"
        for _ in range(40):
            nested = {"safe": nested}
        with self.assertRaisesRegex(ValueError, "bound"):
            self.runner.validate_persisted_receipt(nested)
        with self.assertRaisesRegex(ValueError, "bound"):
            self.runner.validate_persisted_receipt(["safe"] * 5000)
        with self.assertRaisesRegex(ValueError, "credential-like"):
            self.runner.validate_persisted_argv(["tool", "safe=" * 40 + "value"])

    def test_strict_bounded_json_is_shared_by_runner_and_orchestrator(self):
        hostile = [
            b'{"safe": 1, "safe": 2}',
            b'{"safe": NaN}',
            b'{"safe": 1e309}',
            b'{"safe": 9223372036854775808}',
            b'{"safe": "unterminated}',
            b'[' + (b'{' * 40) + b'0' + (b'}' * 40) + b']',
        ]
        for raw in hostile:
            with self.subTest(kind=raw[:24]):
                with self.assertRaises(ValueError):
                    self.runner._bounded_json_loads(raw)
                with self.assertRaises(ValueError):
                    self.orchestrator._decode_bounded_json(raw)
                with self.assertRaises(ValueError):
                    self.runner.validate_persisted_output(raw)

    def test_quoted_escaped_embedded_assignments_fail_closed(self):
        hostile = [
            r'{"safe\\\"HF_TOKEN": "fixture"}',
            r'{"safe": "prefix,\\\"Authorization\\\":\\\"Bearer fixture"}',
            r'["wrapper={\\\"api_key\\\":\\\"fixture\\\"}"]',
            r'prefix {"nested": {"client-secret": "fixture"}} suffix',
        ]
        for value in hostile:
            with self.subTest(kind=value[:24]):
                with self.assertRaises(ValueError):
                    self.runner.validate_persisted_output(value)
                with self.assertRaises(ValueError):
                    self.runner.validate_persisted_argv(["tool", value])

    def test_private_atomic_writer_refuses_unproved_target_without_side_effects(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            parent = root / "private"
            parent.mkdir()
            parent.chmod(0o700)
            target = parent / "receipt.json"
            with self.assertRaises(ValueError):
                self.runner._private_atomic_write(target, b'{"HF_TOKEN":"fixture"}\n')
            self.assertFalse(target.exists())
            target.symlink_to(parent / "other")
            with self.assertRaises(ValueError):
                self.runner._private_atomic_write(target, b'{"safe":true}\n')
            self.assertTrue(target.is_symlink())

    def test_private_policy_walks_scoped_ancestors_and_rechecks_identity(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            private.chmod(0o700)
            target = private / "receipt.json"
            self.runner._private_atomic_write(
                target, b'{"safe":true}\n', trusted_root=root,
            )
            self.assertEqual(target.read_bytes(), b'{"safe":true}\n')

            permissive = root / "permissive"
            permissive.mkdir()
            permissive.chmod(0o755)
            with self.assertRaises(ValueError):
                self.runner._private_atomic_write(
                    permissive / "receipt.json", b'{"safe":true}\n',
                    trusted_root=root,
                )

            linked = root / "linked"
            linked.symlink_to(private, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.runner._private_atomic_write(
                    linked / "receipt.json", b'{"safe":true}\n',
                    trusted_root=root,
                )

            with mock.patch.object(self.runner.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(ValueError):
                    self.runner._private_atomic_write(
                        private / "foreign.json", b'{"safe":true}\n',
                        trusted_root=root,
                    )

            with mock.patch.object(self.runner, "_private_ancestors_stable", return_value=False):
                with self.assertRaisesRegex(ValueError, "parent|ancestor"):
                    self.runner._private_atomic_write(
                        private / "swapped.json", b'{"safe":true}\n',
                        trusted_root=root,
                    )
                self.assertFalse((private / "swapped.json").exists())

    def test_bounded_receipt_read_rejects_hardlinks_and_missing_capability(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            receipt = root / "receipt.json"
            receipt.write_text('{"safe":true}\n', encoding="utf-8")
            hardlink = root / "receipt-hardlink.json"
            hardlink.hardlink_to(receipt)
            with self.assertRaises(ValueError):
                self.runner._bounded_json_file(hardlink)
            original = self.runner.os.O_NOFOLLOW
            del self.runner.os.O_NOFOLLOW
            try:
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    self.runner._bounded_json_file(receipt)
            finally:
                self.runner.os.O_NOFOLLOW = original

    def test_salvage_rejects_symlinked_ancestor_before_process_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            private.chmod(0o700)
            linked = root / "linked"
            linked.symlink_to(private, target_is_directory=True)
            destination = linked / "artifacts"
            info = {"phase_id": "canary", "instance_info": {"ssh_user": "u", "ip": "127.0.0.1"}}
            with mock.patch.object(self.orchestrator, "_remote") as remote:
                with self.assertRaises(ValueError):
                    self.orchestrator._salvage(
                        info, root / "id", root / "known", destination,
                        ["receipt.json"],
                    )
            remote.assert_not_called()

    def test_salvage_refuses_after_destination_swap_without_transport_use(self):
        """A destination swapped after validation still spawns no transfer.

        The bounded transport replaced the blanket refusal of `2d7db4f`, but the
        TOCTOU concern that motivated it is unchanged: the validated
        destination must be proved stable before any process exists, and it is
        never the pathname handed to SCP.
        """

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            destination = root / "artifacts"
            destination.mkdir()
            destination.chmod(0o700)
            moved = root / "moved-artifacts"
            info = {"phase_id": "canary", "instance_info": {"ssh_user": "u", "ip": "127.0.0.1"}}

            def swap_after_validation(_snapshot):
                destination.rename(moved)
                destination.symlink_to(moved, target_is_directory=True)
                return True

            with mock.patch.object(
                    self.orchestrator.j1m_runner, "_private_ancestors_stable",
                    side_effect=swap_after_validation), \
                    mock.patch.object(self.orchestrator, "_remote") as remote, \
                    mock.patch.object(self.orchestrator.sf, "scp_base") as scp_base:
                with self.assertRaisesRegex(ValueError, "salvage destination changed"):
                    self.orchestrator._salvage(
                        info, root / "id", root / "known", destination,
                        ["eval-receipt.json"],
                    )
            remote.assert_not_called()
            scp_base.assert_not_called()

    def test_canary_plan_keeps_salvage_obligation_with_a_bounded_transport(self):
        """The availability flag is source truth, and it is bounded truth."""

        self.assertTrue(self.orchestrator._EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE)
        plan = self.runner.build_plan(self.config, "canary")
        self.assertTrue(plan["mode_policy"]["salvage_required"])
        # Arbitrary-path salvage remains impossible: the fetchable set is a
        # source-fixed mapping of receipt basenames, and the remote directory
        # is a single source constant rather than a caller or config value.
        self.assertEqual(sorted(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST), [
            "comparator-receipt-bf16.json",
            "comparator-receipt-q4_k_m.json",
            "comparator-receipt-q8_0.json",
            "conversion-receipt.json",
            "cuda-device-receipt.json",
            "eval-artifact-receipt.json",
            "eval-receipt.json",
            "manifest.json",
            "model-receipt.json",
            "post-cleanup-receipt.json",
            "proving-receipt.json",
            "scan-receipt.json",
            "source-model-receipt.json",
            "startup-preflight-receipt.json",
            "tensor-metadata.json",
            "toolchain-receipt.json",
            "toolchain.json",
        ])
        # Build mode now has receipts it can actually salvage.  A paid
        # conversion that returned nothing and still reported success was the
        # defect; the weights stay unsalvageable by design.
        self.assertEqual(
            sorted(self.orchestrator._SALVAGE_NON_RECEIPT_NAMES),
            ["Qwen3.5-9B-Q4_K_M.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-bf16.gguf",
             "checksums.sha256", "command-receipt.json"],
        )
        build_allowlist = self.config["artifacts"]["local_fetch_allowlist"]
        self.assertTrue(set(build_allowlist) <= (
            set(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST)
            | set(self.orchestrator._SALVAGE_NON_RECEIPT_NAMES)
        ))
        self.assertTrue(any(name in self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST
                            for name in build_allowlist))
        self.assertEqual(self.orchestrator._SALVAGE_REMOTE_DIRECTORY, "/scratch/j1m/artifacts")
        self.assertEqual(
            self.orchestrator._SALVAGE_MAX_FILES,
            len(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST),
        )
        for hostile in ("../../etc/passwd", "*.json", "/etc/shadow", "eval-receipt.json ",
                        "Qwen3.5-9B-Q4_K_M.gguf", "salvage-receipt.json"):
            self.assertNotIn(hostile, self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST)
        # Every fetchable name is a bare basename: no separator, no traversal,
        # no glob metacharacter can appear in a constructed remote operand. The
        # comparator arm names carry an underscore, which the general pin does
        # not admit; they are ENUMERATED rather than admitted by relaxing the
        # pin, so a non-enumerated name is still refused.
        comparator = frozenset(self.orchestrator._SALVAGE_COMPARATOR_RECEIPTS)
        self.assertEqual(len(comparator), 3)
        self.assertTrue(comparator <= set(self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST))
        for name in self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST:
            if name in comparator:
                self.assertRegex(name, r"\Acomparator-receipt-(?:q4_k_m|q8_0|bf16)\.json\Z")
                continue
            self.assertRegex(name, r"\A[a-z][a-z0-9-]*\.json\Z")
        for hostile in ("comparator-receipt-q4.json", "comparator-receipt-.json",
                        "comparator-receipt-../etc/passwd", "comparator-receipt-*.json"):
            self.assertNotIn(hostile, self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST)
        # Every fetchable receipt must also declare a run-identity binding and
        # a required-key set; nothing is fetchable on schema alone.
        for name in self.orchestrator._SALVAGE_RECEIPT_ALLOWLIST:
            self.assertIn(name, self.orchestrator._SALVAGE_REQUIRED_KEYS)
        self.assertEqual(self.orchestrator._SALVAGE_REQUIRED_IDENTITY,
                         frozenset(self.runner.RUN_IDENTITY_FIELDS))

    def test_malformed_tensor_receipt_is_finite_refusal_without_typeerror_or_file(self):
        class Reader:
            version = 3
            fields = {"general.architecture": object()}
            tensors = []

            def __init__(self, _path):
                pass

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            gguf = root / "model.gguf"
            metadata = root / "tensor-metadata.json"
            gguf.write_bytes(b"fixture")
            with mock.patch.dict("sys.modules", {"gguf": types.SimpleNamespace(GGUFReader=Reader)}):
                with self.assertRaisesRegex(ValueError, "refused|rejected"):
                    self.runner.main(["--inspect-tensors", str(gguf), str(metadata)])
            self.assertFalse(metadata.exists())

    def test_write_artifacts_rejects_before_output_directory_or_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created"
            with self.assertRaises(ValueError):
                self.runner.write_artifacts(output, ["Qwen3.5-9B-Q4_K_M.gguf"])
            self.assertFalse(output.exists())

    def test_pip_freeze_rejects_raw_subprocess_output_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            freeze = Path(directory) / "pip-freeze.txt"
            result = types.SimpleNamespace(stdout="HF_TOKEN=fixture\n")
            with mock.patch.object(self.runner.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.main(["--pip-freeze", str(freeze)])
            self.assertFalse(freeze.exists())

    def test_verify_subprocess_output_is_validated_before_print(self):
        result = types.SimpleNamespace(stdout="HF_TOKEN=fixture\n")
        with mock.patch.object(self.runner.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "credential-like"):
                self.runner.main(["--verify-llama", "/private/checkout", "fixture"])

    def test_command_output_failure_persists_only_finite_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "command-receipt.json"
            command = ["python3", "-c", "pass"]
            def fake_run(*_args, **kwargs):
                kwargs["stderr"].write(b"HF_TOKEN=secret-material\n")
                return type("Result", (), {"returncode": 0})()
            with mock.patch.object(self.runner.subprocess, "run", side_effect=fake_run):
                result = self.runner.run_commands(
                    [command], root / "progress.json", receipt_path=receipt,
                    trusted_root=root,
                )
            self.assertEqual(result[0]["status"], "failed")
            self.assertEqual(result[0]["error_type"], "unsafe_output")
            self.assertNotIn("secret-material", receipt.read_text(encoding="utf-8"))
            self.assertNotIn("secret-material", (root / "progress.json").read_text(encoding="utf-8"))

    def test_converter_receipt_rejects_hostile_argv_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            q4 = root / "Qwen3.5-9B-Q4_K_M.gguf"
            q4.write_bytes(b"q4")
            (root / "tensor-metadata.json").write_text("{}\n", encoding="utf-8")
            for name in ("source-model-receipt.json", "toolchain.json", "command-receipt.json", "scan-receipt.json", "post-cleanup-receipt.json"):
                (root / name).write_text("{}\n", encoding="utf-8")
            q4_identity = {"size_bytes": q4.stat().st_size, "sha256": self.runner._sha256(q4)}
            scan = {"artifacts": [{"name": q4.name, **q4_identity}]}
            post_cleanup = {"q4": q4_identity}
            with mock.patch.object(
                self.runner,
                "_validate_producer_receipts",
                return_value=(
                    {"revision": "source"},
                    {"status": "verified", "gguf_metadata": {}},
                    {},
                    [],
                    scan,
                    post_cleanup,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.write_artifacts(
                        root,
                        [q4.name],
                        commands=[["python3", "convert_hf_to_gguf.py", "--env=HF_TOKEN=fixture"]],
                    )
            self.assertFalse((root / "conversion-receipt.json").exists())
            model_receipt = root / "model-receipt.json"
            with mock.patch.object(
                self.runner,
                "_validate_producer_receipts",
                return_value=(
                    {"revision": "source"},
                    {"status": "verified", "gguf_metadata": {"HF_TOKEN": "sentinel"}},
                    {},
                    [],
                    scan,
                    post_cleanup,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.write_artifacts(root, [q4.name], commands=[])
            self.assertFalse(model_receipt.exists())

    def test_inspect_tensors_rejects_hostile_producer_metadata_before_write(self):
        class ReaderField:
            def __init__(self, value):
                self._value = value

            def contents(self):
                return self._value

        class Tensor:
            name = "HF_TOKEN=sentinel"
            shape = [2, 2]
            tensor_type = "Q4_K_M"

        class Reader:
            version = 3
            fields = {
                "general.architecture": ReaderField(["qwen35"]),
                "tokenizer.chat_template": ReaderField([b"template"]),
            }
            tensors = [Tensor()]

            def __init__(self, _path):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gguf = root / "model.gguf"
            metadata = root / "tensor-metadata.json"
            gguf.write_bytes(b"fixture")
            fake_gguf = types.SimpleNamespace(GGUFReader=Reader)
            with mock.patch.dict("sys.modules", {"gguf": fake_gguf}):
                with self.assertRaisesRegex(ValueError, "credential-like"):
                    self.runner.main(["--inspect-tensors", str(gguf), str(metadata)])
            self.assertFalse(metadata.exists())
            self.assertNotIn("sentinel", "".join(path.read_text(encoding="utf-8") for path in root.iterdir() if path.is_file() and path != gguf))


if __name__ == "__main__":
    unittest.main()
