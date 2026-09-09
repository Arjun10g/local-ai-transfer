from __future__ import annotations

import ast
import os
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import shadeform_lifecycle as sf


class ShadeformMutationEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=sf.ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.env = self.root / ".env"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_env(self, text: str, *, mode: int = 0o600) -> None:
        self.env.write_text(text, encoding="utf-8")
        self.env.chmod(mode)

    def test_accepts_exact_mutation_keys_without_mutating_process_environment(self) -> None:
        self.write_env(
            "SHADEFORM_API_KEY=fixture-key\n"
            "SHADEFORM_SSH=fixture-ownership-input\n"
            "SHADEFORM_MAX_TOTAL_COST_USD=50\n"
            "SHADEFORM_AUTO_TERMINATE_HOURS=2.5\n"
        )
        before = os.environ.get("SHADEFORM_API_KEY")
        loaded = sf.load_env(self.env)
        self.assertEqual(loaded["SHADEFORM_MAX_TOTAL_COST_USD"], "50")
        self.assertEqual(os.environ.get("SHADEFORM_API_KEY"), before)
        self.assertEqual(set(loaded), {
            "SHADEFORM_API_KEY", "SHADEFORM_SSH",
            "SHADEFORM_MAX_TOTAL_COST_USD", "SHADEFORM_AUTO_TERMINATE_HOURS",
        })

    def test_rejects_unknown_duplicate_and_malformed_assignments(self) -> None:
        cases = (
            "UNAPPROVED_KEY=fixture\n",
            "SHADEFORM_SSH_KEY_ID=legacy-key\n",
            "SHADEFORM_API_KEY=one\nSHADEFORM_API_KEY=two\n",
            "SHADEFORM_API_KEY\n",
            "export SHADEFORM_API_KEY=fixture\n",
            " SHADEFORM_API_KEY=fixture\n",
        )
        for content in cases:
            with self.subTest(case=content.splitlines()[0].split("=", 1)[0]):
                self.write_env(content)
                with self.assertRaises(sf.ShadeformError):
                    sf.load_env(self.env)

    def test_rejects_oversize_invalid_encoding_and_noncanonical_values(self) -> None:
        cases = (
            (b"SHADEFORM_API_KEY=" + b"x" * (sf.MAX_MUTATION_ENV_VALUE_CHARS + 1) + b"\n"),
            b"SHADEFORM_API_KEY=fixture\r\n",
            b"SHADEFORM_API_KEY= fixture\n",
            b"SHADEFORM_API_KEY='fixture\n",
            b"SHADEFORM_API_KEY=\xff\n",
        )
        for raw in cases:
            with self.subTest(size=len(raw)):
                self.env.write_bytes(raw)
                self.env.chmod(0o600)
                with self.assertRaises(sf.ShadeformError):
                    sf.load_env(self.env)

        self.env.write_bytes(b"SHADEFORM_API_KEY=" + b"x" * sf.MAX_MUTATION_ENV_BYTES + b"\n")
        self.env.chmod(0o600)
        with self.assertRaises(sf.ShadeformError):
            sf.load_env(self.env)

    def test_rejects_symlink_hardlink_wrong_mode_and_wrong_owner(self) -> None:
        target = self.root / "target.env"
        target.write_text("SHADEFORM_API_KEY=fixture\n", encoding="utf-8")
        target.chmod(0o600)
        self.env.symlink_to(target)
        with self.assertRaises(sf.ShadeformError):
            sf.load_env(self.env)
        self.env.unlink()

        self.write_env("SHADEFORM_API_KEY=fixture\n", mode=0o640)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.load_env(self.env)
        self.env.chmod(0o600)
        hardlink = self.root / "hardlink.env"
        hardlink.hardlink_to(self.env)
        with self.assertRaisesRegex(sf.ShadeformError, "single-link"):
            sf.load_env(self.env)
        hardlink.unlink()

        with mock.patch.object(sf.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
                sf.load_env(self.env)

    def test_rejects_parent_mode_and_missing_capability(self) -> None:
        self.write_env("SHADEFORM_API_KEY=fixture\n")
        self.root.chmod(0o750)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.load_env(self.env)
        self.root.chmod(0o700)
        with mock.patch.object(sf, "OPEN_SUPPORTS_DIR_FD", False):
            with self.assertRaisesRegex(sf.ShadeformError, "handle-relative"):
                sf.load_env(self.env)

    def test_rejects_path_replacement_after_descriptor_open(self) -> None:
        self.write_env("SHADEFORM_API_KEY=fixture\n")
        original = sf._read_private_file_at

        def replace_then_read(descriptor: int, **kwargs: object) -> bytes:
            path = kwargs["path"]
            assert isinstance(path, Path)
            path.unlink()
            path.write_text("SHADEFORM_API_KEY=replacement\n", encoding="utf-8")
            path.chmod(0o600)
            return original(descriptor, **kwargs)

        with mock.patch.object(sf, "_read_private_file_at", side_effect=replace_then_read):
            with self.assertRaises(sf.ShadeformError):
                sf.load_env(self.env)

    def test_failure_does_not_echo_assignment_value(self) -> None:
        sentinel = "fixture-secret-must-not-be-echoed"
        self.write_env(f"UNAPPROVED_KEY={sentinel}\n")
        with self.assertRaises(sf.ShadeformError) as caught:
            sf.load_env(self.env)
        self.assertNotIn(sentinel, str(caught.exception))

    def _write_donor(self, content: str) -> None:
        self.write_env(content)

    def test_projects_only_allowlisted_keys_from_mixed_donor(self) -> None:
        self._write_donor(
            "git_access= value \"with\" = equals\n"
            "PATH=/usr/bin:/bin\n"
            "UNRELATED=  'opaque donor syntax' = still opaque  \n"
            "SHADEFORM_SSH=fixture-ownership-input\n"
            "SHADEFORM_API_KEY=fixture-api-input\n"
            "SHADEFORM_GPU_TYPES=A100\n"
        )
        destination = self.root / "projected.env"
        report = sf.project_mutation_env(self.env, destination)
        self.assertEqual(report["status"], "published")
        self.assertEqual(report["selected_key_count"], 3)
        self.assertNotIn("opaque donor syntax", str(report))
        self.assertNotIn("still opaque", str(report))
        self.assertEqual(
            report["selected_keys"],
            ["SHADEFORM_API_KEY", "SHADEFORM_GPU_TYPES", "SHADEFORM_SSH"],
        )
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        projected = destination.read_text(encoding="utf-8")
        self.assertNotIn("git_access", projected)
        self.assertNotIn("PATH=", projected)
        self.assertNotIn("UNRELATED", projected)
        self.assertEqual(
            projected,
            "SHADEFORM_API_KEY=fixture-api-input\n"
            "SHADEFORM_GPU_TYPES=A100\n"
            "SHADEFORM_SSH=fixture-ownership-input\n",
        )

    def test_unrelated_values_are_opaque_within_file_and_line_bounds(self) -> None:
        opaque = "x" * 2049
        self._write_donor(
            "UNRELATED_LONG=" + opaque + "\n"
            "UNRELATED_TAB=before\tafter\n"
            "UNRELATED_QUOTES='quoted = donor'\n"
            "SHADEFORM_API_KEY=api\n"
            "SHADEFORM_SSH=ssh\n"
        )
        destination = self.root / "projected.env"
        report = sf.project_mutation_env(self.env, destination)
        self.assertEqual(report["selected_keys"], ["SHADEFORM_API_KEY", "SHADEFORM_SSH"])
        serialized = str(report) + destination.read_text(encoding="utf-8")
        self.assertNotIn(opaque, serialized)
        self.assertNotIn("before\tafter", serialized)
        self.assertNotIn("quoted = donor", serialized)

    def test_projection_accepts_surrounding_ascii_whitespace_on_unrelated_keys(self) -> None:
        self._write_donor(
            "git_access =opaque-donor-value\n"
            "\tUNRELATED_TAB\t=another-opaque-value\n"
            "SHADEFORM_API_KEY=api\n"
            "SHADEFORM_SSH=ssh\n"
        )
        destination = self.root / "projected.env"
        report = sf.project_mutation_env(self.env, destination)
        self.assertEqual(report["selected_keys"], ["SHADEFORM_API_KEY", "SHADEFORM_SSH"])
        projected = destination.read_text(encoding="utf-8")
        self.assertNotIn("git_access", projected)
        self.assertNotIn("UNRELATED_TAB", projected)
        self.assertNotIn("opaque-donor-value", str(report))
        self.assertNotIn("another-opaque-value", str(report))

    def test_projection_rejects_whitespace_aliases_of_selected_keys(self) -> None:
        destination = self.root / "projected.env"
        cases = (
            " SHADEFORM_API_KEY=api\nSHADEFORM_SSH=ssh\n",
            "SHADEFORM_API_KEY =api\nSHADEFORM_SSH=ssh\n",
            "SHADEFORM_API_KEY=api\nSHADEFORM_SSH\t=ssh\n",
        )
        for content in cases:
            with self.subTest():
                self._write_donor(content)
                with self.assertRaisesRegex(
                    sf.ShadeformError, "selected key has noncanonical whitespace",
                ):
                    sf.project_mutation_env(self.env, destination)
                self.assertFalse(destination.exists())

    def test_projection_rejects_duplicate_or_malformed_donor_assignments(self) -> None:
        destination = self.root / "projected.env"
        cases = (
            "SHADEFORM_API_KEY=one\nSHADEFORM_API_KEY=two\nSHADEFORM_SSH=ssh\n",
            "SHADEFORM_API_KEY=one\nnot-an-assignment\nSHADEFORM_SSH=ssh\n",
            "SHADEFORM_API_KEY=one\nBAD KEY=value\nSHADEFORM_SSH=ssh\n",
            "SHADEFORM_API_KEY=one\nSHADEFORM_SSH= ssh\n",
        )
        for content in cases:
            with self.subTest(case=content.splitlines()[1]):
                self._write_donor(content)
                with self.assertRaises(sf.ShadeformError):
                    sf.project_mutation_env(self.env, destination)
                self.assertFalse(destination.exists())

    def test_projection_refuses_existing_or_unsafe_destination(self) -> None:
        self._write_donor("SHADEFORM_API_KEY=api\nSHADEFORM_SSH=ssh\n")
        destination = self.root / "projected.env"
        destination.write_text("existing\n", encoding="utf-8")
        destination.chmod(0o600)
        with self.assertRaises(sf.ShadeformError):
            sf.project_mutation_env(self.env, destination)
        destination.unlink()
        target = self.root / "target.env"
        target.write_text("target\n", encoding="utf-8")
        target.chmod(0o600)
        destination.symlink_to(target)
        with self.assertRaises(sf.ShadeformError):
            sf.project_mutation_env(self.env, destination)

    def test_project_root_env_is_refused_but_private_secrets_layout_is_accepted(self) -> None:
        self._write_donor("SHADEFORM_API_KEY=api\nSHADEFORM_SSH=ssh\n")
        repository = self.root / "repository"
        repository.mkdir(mode=0o755)
        root_env = repository / ".env"
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.project_mutation_env(self.env, root_env)
        self.assertFalse(root_env.exists())

        secrets_directory = repository / ".secrets"
        secrets_directory.mkdir(mode=0o700)
        protected_env = secrets_directory / "shadeform.env"
        report = sf.project_mutation_env(self.env, protected_env)
        self.assertEqual(report["selected_key_count"], 2)
        self.assertEqual(protected_env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(sf.load_env(protected_env)), {"SHADEFORM_API_KEY", "SHADEFORM_SSH"})

    def test_mutation_entrypoints_default_to_ignored_private_layout(self) -> None:
        self.assertEqual(sf.MUTATION_ENV_FILE, sf.ROOT / ".secrets" / "shadeform.env")
        ignored = (sf.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".secrets/", ignored)
        instructions = (sf.ROOT / "scripts/shadeform/README.md").read_text(encoding="utf-8")
        self.assertIn("mkdir -m 700 .secrets", instructions)
        self.assertNotIn("chmod 700 .secrets", instructions)
        for relative in (
            "scripts/j1m_orchestrator.py",
            "scripts/shadeform_teardown.py",
            "scripts/shadeform_watchdog.py",
            "scripts/shadeform/remote_external_tools.py",
        ):
            source = (sf.ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("MUTATION_ENV_FILE", source, relative)
            self.assertNotIn('ROOT / ".env"', source, relative)

    def test_projection_refuses_ancestor_change_and_missing_capability(self) -> None:
        self._write_donor("SHADEFORM_API_KEY=api\nSHADEFORM_SSH=ssh\n")
        destination = self.root / "projected.env"
        with mock.patch.object(sf, "_env_ancestors_stable", return_value=False):
            with self.assertRaises(sf.ShadeformError):
                sf.project_mutation_env(self.env, destination)
        self.assertFalse(destination.exists())
        with mock.patch.object(sf, "LINK_SUPPORTS_DIR_FD", False):
            with self.assertRaisesRegex(sf.ShadeformError, "descriptor-relative"):
                sf.project_mutation_env(self.env, destination)

    def test_prepublication_ancestor_failure_leaves_no_destination(self) -> None:
        self._write_donor("SHADEFORM_API_KEY=api\nSHADEFORM_SSH=ssh\n")
        destination = self.root / "projected.env"
        with mock.patch.object(
            sf, "_env_ancestors_stable", side_effect=(True, True, True, False),
        ):
            with self.assertRaisesRegex(sf.ShadeformError, "before publication"):
                sf.project_mutation_env(self.env, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".projected.env.*.tmp")), [])

    def test_postpublication_replacement_is_preserved_without_cleanup_unlink(self) -> None:
        self._write_donor("SHADEFORM_API_KEY=api\nSHADEFORM_SSH=ssh\n")
        destination = self.root / "projected.env"
        original = sf._read_private_file_at

        def replace_destination(descriptor: int, **kwargs: object) -> bytes:
            path = kwargs["path"]
            assert isinstance(path, Path)
            if path == destination:
                replacement = self.root / "replacement.env"
                replacement.write_text("replacement\n", encoding="utf-8")
                replacement.chmod(0o600)
                destination.unlink()
                replacement.rename(destination)
            return original(descriptor, **kwargs)

        with mock.patch.object(sf, "_read_private_file_at", side_effect=replace_destination):
            with self.assertRaises(sf.ShadeformError):
                sf.project_mutation_env(self.env, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "replacement\n")
        self.assertEqual(list(self.root.glob(".projected.env.*.tmp")), [])

    def test_direct_projection_cli_is_sanitized_and_matches_api(self) -> None:
        self._write_donor(
            "git_access=unrelated-fixture\n"
            "SHADEFORM_API_KEY=fixture-api-input\n"
            "SHADEFORM_SSH=fixture-ownership-input\n"
        )
        environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(sf.ROOT)}
        reports = []
        for destination, command in (
            (
                self.root / "projected-direct.env",
                [sys.executable, str(sf.ROOT / "scripts" / "shadeform" / "migrate_env.py")],
            ),
            (
                self.root / "projected-module.env",
                [sys.executable, "-m", "scripts.shadeform.migrate_env"],
            ),
        ):
            result = subprocess.run(
                command + ["--source", str(self.env), "--destination", str(destination)],
                cwd=sf.ROOT, env=environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            reports.append(json.loads(result.stdout))
            self.assertNotIn("fixture-api-input", result.stdout)
            self.assertNotIn("fixture-ownership-input", result.stdout)
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0]["selected_key_count"], 2)
        self.assertEqual(reports[0]["selected_keys"], ["SHADEFORM_API_KEY", "SHADEFORM_SSH"])

    def test_subprocess_environment_source_is_private_immutable_and_unaliased(self) -> None:
        with self.assertRaises(TypeError):
            sf._SECURE_SUBPROCESS_ENV["PATH"] = "ambient"
        first = sf._secure_subprocess_env()
        first["PATH"] = "ambient"
        self.assertEqual(sf._secure_subprocess_env()["PATH"], "/usr/bin:/bin")

    def test_lifecycle_helpers_use_absolute_executables_and_minimal_env(self) -> None:
        with mock.patch.object(
            sf, "_verified_executable", side_effect=lambda name: f"/verified/{name}"
        ), mock.patch.object(sf.subprocess, "run") as run:
            def keypair_side_effect(*args: object, **kwargs: object) -> types.SimpleNamespace:
                argv = args[0]
                assert isinstance(argv, list)
                Path(argv[-1]).write_text("private-fixture", encoding="utf-8")
                Path(f"{argv[-1]}.pub").write_text("ssh-ed25519 fixture", encoding="utf-8")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            run.side_effect = keypair_side_effect
            sf.create_keypair(self.root / "keys")
            call = run.call_args
            self.assertEqual(call.args[0][0], "/verified/ssh-keygen")
            self.assertEqual(call.kwargs["env"], sf._secure_subprocess_env())

        host_line = "127.0.0.1 ssh-ed25519 AAAA\n"
        responses = iter((
            types.SimpleNamespace(returncode=0, stdout=host_line, stderr=""),
            types.SimpleNamespace(returncode=0, stdout=host_line, stderr=""),
            types.SimpleNamespace(returncode=0, stdout="256 SHA256:fixture host (ED25519)\n", stderr=""),
        ))
        with mock.patch.object(
            sf, "_verified_executable", side_effect=lambda name: f"/verified/{name}"
        ), mock.patch.object(sf.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)) as run:
            sf.acquire_pinned_host_key(
                {"ip": "127.0.0.1", "ssh_port": 2222, "ssh_user": "u"},
                self.root / "known_hosts",
            )
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["env"], sf._secure_subprocess_env())
                self.assertTrue(call.args[0][0].startswith("/verified/"))

    def test_mutation_transport_call_sites_are_explicit_and_absolute(self) -> None:
        from scripts import j1m_orchestrator as orchestrator

        completed = types.SimpleNamespace(returncode=2, stdout="secret-output", stderr="safe")
        with mock.patch.object(orchestrator.subprocess, "run", return_value=completed) as run:
            orchestrator._remote(["ssh", "host"], timeout=1)
        self.assertEqual(run.call_args.kwargs["env"], sf._secure_subprocess_env())

        with mock.patch.object(
            sf, "_verified_executable", side_effect=lambda name: f"/verified/{name}"
        ):
            ssh = sf.ssh_base(
                {"ip": "127.0.0.1", "ssh_user": "runner", "ssh_port": 22},
                self.root / "id", self.root / "known_hosts",
            )
            scp = sf.scp_base(
                {"ip": "127.0.0.1", "ssh_user": "runner", "ssh_port": 22},
                self.root / "id", self.root / "known_hosts",
            )
        self.assertEqual(ssh[0], "/verified/ssh")
        self.assertEqual(scp[0], "/verified/scp")
        self.assertEqual(ssh[ssh.index("-F") + 1], "/dev/null")
        self.assertEqual(scp[scp.index("-F") + 1], "/dev/null")
        for argv in (ssh, scp):
            options = set(argv)
            self.assertIn("ControlMaster=no", options)
            self.assertIn("ControlPersist=no", options)
            self.assertIn("ControlPath=none", options)
            self.assertNotIn("ControlMaster=auto", options)
            self.assertNotIn("ControlPersist=600", options)
            self.assertFalse(any("/tmp/ep-cm-" in value for value in argv))

        for relative in (
            "scripts/shadeform_lifecycle.py", "scripts/j1m_orchestrator.py",
            "scripts/shadeform/remote_external_tools.py", "scripts/shadeform_watchdog.py",
        ):
            tree = ast.parse((sf.ROOT / relative).read_text(encoding="utf-8"))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ]
            self.assertTrue(calls, relative)
            self.assertTrue(all(any(keyword.arg == "env" for keyword in call.keywords) for call in calls), relative)

    def test_python_child_refuses_unsafe_or_changing_interpreter_path(self) -> None:
        fake_dir = self.root / "python-parent"
        fake_dir.mkdir(mode=0o700)
        fake = fake_dir / "python"
        fake.write_bytes(b"python-fixture")
        fake.chmod(0o700)
        popen = mock.Mock()
        with mock.patch.object(sf.sys, "executable", str(fake)), mock.patch.object(
            sf.subprocess, "Popen", popen
        ):
            fake_dir.chmod(0o777)
            with self.assertRaises(sf.ShadeformError):
                sf._verified_python_executable()
            fake_dir.chmod(0o700)
            with mock.patch.object(sf, "_env_ancestors_stable", return_value=False):
                with self.assertRaises(sf.ShadeformError):
                    sf._verified_python_executable()
            first = os.stat(fake, follow_symlinks=False)
            changed = types.SimpleNamespace(
                st_dev=first.st_dev,
                st_ino=first.st_ino + 1,
                st_mode=first.st_mode,
                st_uid=first.st_uid,
                st_nlink=first.st_nlink,
            )
            with mock.patch.object(
                sf, "_env_ancestor_snapshot", return_value=()
            ), mock.patch.object(
                sf, "_env_ancestors_stable", return_value=True
            ), mock.patch.object(sf.os, "stat", side_effect=(first, changed)):
                with self.assertRaises(sf.ShadeformError):
                    sf._verified_python_executable()
            link = self.root / "python-link"
            link.symlink_to(fake)
            with mock.patch.object(sf.sys, "executable", str(link)):
                with self.assertRaises(sf.ShadeformError):
                    sf._verified_python_executable()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
