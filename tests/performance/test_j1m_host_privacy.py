"""Host-side privacy invariants for the remote J1M lanes (J1M-HOST-PRIVACY-001).

Run ``j1m-eval-20260911-remote-d`` (instance ``d75747f8-4801-4f72-8a5f-5713ef333905``
on hyperstack, 20:24-20:29Z, USD 3.273486 booked) created an A100, ran four
stages successfully, and then salvaged 0 of 8 receipts.  Nothing in the plan was
malformed: every command passed ``validate_persisted_argv``, and the offline
gate passed.  The defect was entirely in the *mode* of what the plan created on
the host -- the remote login shell carried the image's ``umask 022``, so the
plan's ``mkdir -p`` produced ``0755`` directories and the probes wrote ``0644``
receipts.  ``j1m_runner``'s own private writer refused its first write and the
runner exited 2; the salvage policy correctly refused the ``0644`` receipt as
``salvage_not_private_regular_file``.

These tests pin the three source-level consequences of that incident:

1. the ``umask 077`` prefix exists exactly once, first, on every remote command
   and is built in exactly one place (``sf.ssh_base``);
2. every directory the remote plans create is chmodded ``0700`` explicitly,
   because a umask cannot fix a directory that already exists;
3. every host-side receipt writer publishes ``0600`` atomically into a ``0700``
   directory, so our own files can never trip the salvage privacy refusal.

Plus the stage-record change that made the failure legible at all, and the
offline host-tree simulation that reproduces the incident from source.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import shadeform_lifecycle as sf

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTANCE = {"ip": "203.0.113.9", "ssh_user": "shadeform", "ssh_port": 22}


class RemoteUmaskPrefixTests(unittest.TestCase):
    """The prefix is built in one place and reaches every remote command."""

    def setUp(self):
        self.sf = sf
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "host_privacy_orchestrator")
        self.runner = load(ROOT / "scripts/j1m_runner.py", "host_privacy_runner")
        self.config = self.runner.load_config()

    def _ssh_base(self):
        with mock.patch.object(self.sf, "_verified_executable", side_effect=lambda name: f"/usr/bin/{name}"):
            return self.sf.ssh_base(INSTANCE, Path("/nonexistent/id"), Path("/nonexistent/known_hosts"))

    def test_the_prefix_is_owner_private_and_fails_closed(self):
        self.assertEqual(self.sf.REMOTE_SHELL_UMASK, "077")
        self.assertEqual(self.sf.remote_shell_prefix(), ["umask", "077", "&&"])
        # `&&`, never `;`: a shell that cannot set the umask must not go on to
        # create world-readable state.
        self.assertNotIn(";", self.sf.remote_shell_prefix())

    def test_ssh_base_ends_with_the_prefix_so_the_command_follows_it(self):
        base = self._ssh_base()
        self.assertEqual(base[-4:], ["shadeform@203.0.113.9", "umask", "077", "&&"])
        self.assertEqual(base[0], "/usr/bin/ssh")

    def test_scp_never_carries_the_prefix(self):
        """scp runs no login shell; a `umask` operand there would be a filename."""

        with mock.patch.object(self.sf, "_verified_executable", side_effect=lambda name: f"/usr/bin/{name}"):
            scp = self.sf.scp_base(INSTANCE, Path("/nonexistent/id"), Path("/nonexistent/known_hosts"))
        self.assertNotIn("umask", scp)

    def test_every_remote_command_carries_the_prefix_exactly_once_and_first(self):
        base = self._ssh_base()
        selection = self.orchestrator._comparator_selection("q8,bf16")
        plans = [
            [argv for _name, argv in self.orchestrator._remote_workspace_stages(
                "shadeform", "/scratch/j1m", str(self.config["resources"]["progress_path"]))],
            self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m"),
            self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m", selection),
            self.orchestrator._comparator_remote_commands(self.config, "/scratch/j1m", selection),
            self.orchestrator._comparator_cleanup_commands(self.config, "/scratch/j1m"),
            self.orchestrator._canary_remote_commands(self.config, "/scratch/j1m-canary"),
            [self.orchestrator._remote_job_command("prove", "/scratch/j1m", 70)],
            [self.orchestrator._remote_job_command("build", "/scratch/j1m", 70)],
            [["sudo", "shutdown", "-h", "+120"]],
        ]
        commands = [argv for plan in plans for argv in plan]
        self.assertGreaterEqual(len(commands), 30)
        for argv in commands:
            full = base + argv
            # Exactly once: the prefix must not be duplicated by a plan that
            # also tries to set it, and must precede the command it guards.
            self.assertEqual(full.count("umask"), 1, argv)
            self.assertEqual(full.count("077"), 1, argv)
            index = full.index("umask")
            self.assertEqual(full[index:index + 3], ["umask", "077", "&&"], argv)
            self.assertEqual(index, len(base) - 3, argv)
            self.assertEqual(full[index + 3:], argv, argv)
            # The prefix is the only shell text; the command itself stays argv.
            self.assertTrue(all(";" not in part and "&&" not in part for part in argv), argv)
            # The prefix itself must survive the persisted-argv policy; the
            # `-i` handle in `base` is only provable against a real ephemeral
            # key (see test_j1m_key_handle), so it is excluded here.
            prefixed = self.sf.remote_shell_prefix() + argv
            self.assertEqual(self.runner.validate_persisted_argv(prefixed), prefixed)


class RemoteDirectoryModeTests(unittest.TestCase):
    """`mkdir -p` cannot fix a directory that already exists; chmod can."""

    def setUp(self):
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "host_privacy_dirs_orchestrator")
        self.runner = load(ROOT / "scripts/j1m_runner.py", "host_privacy_dirs_runner")
        self.config = self.runner.load_config()
        self.progress_relative = str(self.config["resources"]["progress_path"])

    def _assert_every_mkdir_is_chmodded(self, commands):
        created: list[str] = []
        privatised: list[str] = []
        for argv in commands:
            if argv[:2] == ["mkdir", "-p"]:
                created.extend(argv[2:])
            elif argv[:2] == ["chmod", "700"]:
                privatised.extend(argv[2:])
            elif argv and argv[0] in {"mkdir", "chmod", "install"}:
                self.fail(f"unreviewed directory command shape: {argv}")
        self.assertTrue(created)
        self.assertEqual(sorted(set(created)), sorted(set(privatised)))
        return created

    def test_the_workspace_preflight_creates_and_privatises_the_same_paths(self):
        stages = self.orchestrator._remote_workspace_stages(
            "shadeform", "/scratch/j1m", self.progress_relative)
        names = [name for name, _argv in stages]
        self.assertEqual(names, [
            "scratch_root", "scratch_not_symlink", "scratch_owner",
            "scratch_root_private", "remote_workspace",
            "remote_workspace_private", "scratch_df", "scratch_writable"])
        # The root's own shape is established BEFORE anything is written
        # beneath it: an unusable root must fail while the instance is still
        # cheap, not after the model download.
        self.assertLess(names.index("scratch_not_symlink"), names.index("remote_workspace"))
        self.assertLess(names.index("scratch_root_private"), names.index("remote_workspace"))
        created = self._assert_every_mkdir_is_chmodded(
            [argv for _name, argv in stages if argv[0] != "sudo"])
        self.assertEqual(created, [
            "/scratch/j1m", "/scratch/j1m/artifacts",
            "/scratch/experiments", "/scratch/experiments/runtime"])
        # `chmod 700` immediately follows the `mkdir -p` it privatises.
        self.assertEqual(names.index("remote_workspace_private"),
                         names.index("remote_workspace") + 1)

    def test_the_trusted_root_is_owned_but_deliberately_not_forced_to_0700(self):
        """`/scratch` is the policy boundary, not a private directory.

        `_private_ancestor_snapshot` checks the root with `& 0o022` only, so an
        image's own root-owned `0755` `/scratch` is acceptable once it is
        chowned to the ssh user -- and forcing `0700` on a shared mountpoint
        would be a change to the image, not to this run.
        """

        stages = dict(self.orchestrator._remote_workspace_stages(
            "shadeform", "/scratch/j1m", self.progress_relative))
        self.assertEqual(stages["scratch_owner"], ["sudo", "chown", "shadeform", "/scratch"])
        self.assertNotIn("/scratch", stages["remote_workspace_private"])
        self.assertNotIn("/scratch", stages["remote_workspace"])
        # `go-w` strips exactly the bits the root predicate rejects and leaves
        # the read/execute bits the non-root toolchain needs; it is NOT 0700.
        self.assertEqual(stages["scratch_root_private"],
                         ["sudo", "chmod", "go-w", "/scratch"])
        self.assertNotIn("700", stages["scratch_root_private"])

    def test_the_root_predicate_refuses_the_shapes_the_preflight_now_repairs(self):
        """The images this plan may land on, judged by the production predicate.

        `mkdir -p` is a no-op on an existing `/scratch`, so before this stage
        the IMAGE decided the root's mode. `_private_ancestor_snapshot` checks
        the root with `& 0o022` and refuses a symlink outright, so a `1777`
        scratch mount, a `0775` group share, or a symlink into `/mnt` would
        have failed every private write after the instance became billable --
        and this provider books the whole reservation either way.
        """

        root = Path(tempfile.mkdtemp(prefix="j1m-root-shape-"))
        self.addCleanup(shutil.rmtree, root, True)
        target = root / "artifacts"
        target.mkdir()
        os.chmod(target, 0o700)
        probe = target / "receipt.json"
        for mode in (0o777, 0o775, 0o1777, 0o757):
            os.chmod(root, mode)
            with self.assertRaises(ValueError, msg=f"{mode:04o} accepted"):
                self.runner._private_ancestor_snapshot(probe, root)
        # What the preflight leaves behind is accepted.
        os.chmod(root, 0o1777 & ~0o022)
        self.runner._private_ancestor_snapshot(probe, root)
        os.chmod(root, 0o755)
        self.runner._private_ancestor_snapshot(probe, root)

    def test_the_private_directories_are_derived_from_the_configured_progress_path(self):
        """The runner's ROOT on the host is /scratch, not /scratch/j1m.

        The uploaded runner lives at `<remote_root>/j1m_runner.py`, so
        `Path(__file__).resolve().parents[1]` is `/scratch`. Its progress file
        therefore lands at `/scratch/<resources.progress_path>` -- a directory
        the failed run's plan never created at all, which is a second refusal
        hiding behind the permission one.
        """

        self.assertEqual(
            self.orchestrator._host_private_directories("/scratch/j1m", "a/b/c.json"),
            ["/scratch/j1m", "/scratch/j1m/artifacts", "/scratch/a", "/scratch/a/b"])
        derived = self.orchestrator._host_private_directories("/scratch/j1m", self.progress_relative)
        self.assertIn("/scratch/experiments/runtime", derived)
        self.assertEqual(
            str(Path("/scratch") / self.progress_relative),
            "/scratch/experiments/runtime/J1M.progress.json")

    def test_the_eval_plan_chmods_every_directory_it_creates_including_parents(self):
        commands = self.orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        self.assertEqual(commands[0][:2], ["mkdir", "-p"])
        self.assertEqual(commands[1][:2], ["chmod", "700"])
        created = self._assert_every_mkdir_is_chmodded(commands)
        # Intermediate components are listed explicitly: `chmod` is not
        # recursive and `mkdir -p` would leave a pre-existing parent alone.
        for parent in ("/scratch/j1m/engine", "/scratch/j1m/engine/tests"):
            self.assertIn(parent, created)
        self.assertIn("/scratch/j1m/artifacts", created)

    def test_the_bootstrap_slice_covers_the_new_privatising_stage(self):
        source = (ROOT / "scripts/j1m_orchestrator.py").read_text(encoding="utf-8")
        # Both bounds are derived from the timeout tuple, so adding a stage
        # cannot leave one slice re-timed and the other short.
        self.assertIn("eval_commands[:_EVAL_BOOTSTRAP_STAGE_COUNT]", source)
        self.assertIn("eval_commands[_EVAL_BOOTSTRAP_STAGE_COUNT:]", source)
        self.assertNotIn("eval_commands[:4]", source)
        self.assertEqual(len(self.orchestrator._EVAL_BOOTSTRAP_TIMEOUTS), 5)
        self.assertEqual(self.orchestrator._EVAL_BOOTSTRAP_STAGE_COUNT,
                         len(self.orchestrator._EVAL_BOOTSTRAP_TIMEOUTS))
        self.assertEqual(self.orchestrator._WORKSPACE_STAGE_COUNT, 8)
        # The envelope must still fit its run/host/watchdog/provider clocks.
        envelope = self.orchestrator._eval_deadline_ceiling(self.config)
        self.assertLess(envelope["ceiling_seconds"], envelope["run_seconds"])
        self.assertLess(envelope["run_seconds"], envelope["host_shutdown_from_create_seconds"])


class HostReceiptWriterTests(unittest.TestCase):
    """Every host-side receipt writer publishes 0600 into a 0700 directory."""

    WRITERS = (
        "scripts/test/remote_toolchain_probe.py",
        "scripts/test/cuda_device_probe.py",
        "scripts/test/remote_eval_prepare.py",
        "scripts/test/remote_model_eval.py",
        "scripts/test/remote_comparator_eval.py",
    )

    def _writer(self, relative: str):
        return load(ROOT / relative, f"host_privacy_{Path(relative).stem}")

    def test_every_uploaded_receipt_writer_exposes_the_private_publisher(self):
        for relative in self.WRITERS:
            with self.subTest(relative):
                module = self._writer(relative)
                self.assertTrue(callable(getattr(module, "_publish_private_receipt", None)))

    def test_the_published_receipt_is_0600_in_a_0700_directory(self):
        previous = os.umask(0o022)  # the image default that caused the incident
        try:
            for relative in self.WRITERS:
                with self.subTest(relative), tempfile.TemporaryDirectory() as directory:
                    module = self._writer(relative)
                    target = Path(directory) / "artifacts" / "toolchain-receipt.json"
                    module._publish_private_receipt(target, b'{"status": "verified"}\n')
                    info = os.lstat(target)
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    self.assertEqual(info.st_nlink, 1)
                    self.assertTrue(stat.S_ISREG(info.st_mode))
                    self.assertEqual(stat.S_IMODE(os.lstat(target.parent).st_mode), 0o700)
                    self.assertEqual(target.read_bytes(), b'{"status": "verified"}\n')
        finally:
            os.umask(previous)

    def test_publication_is_atomic_and_leaves_no_temporary_behind(self):
        module = self._writer("scripts/test/remote_toolchain_probe.py")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cuda-device-receipt.json"
            module._publish_private_receipt(target, b"first\n")
            module._publish_private_receipt(target, b"second-and-longer\n")
            self.assertEqual(target.read_bytes(), b"second-and-longer\n")
            self.assertEqual([item.name for item in Path(directory).iterdir()],
                             ["cuda-device-receipt.json"])
            self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o600)

    def test_a_pre_existing_world_readable_directory_is_repaired(self):
        """The run-d shape exactly: a 0755 artifact directory already there."""

        module = self._writer("scripts/test/cuda_device_probe.py")
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory) / "artifacts"
            artifacts.mkdir()
            os.chmod(artifacts, 0o755)
            module._publish_private_receipt(artifacts / "eval-receipt.json", b"{}\n")
            self.assertEqual(stat.S_IMODE(os.lstat(artifacts).st_mode), 0o700)

    def test_a_receipt_written_this_way_passes_the_salvage_privacy_predicate(self):
        """The exact refusal run d hit: salvage_not_private_regular_file."""

        orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "host_privacy_salvage")
        module = self._writer("scripts/test/remote_eval_prepare.py")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "toolchain-receipt.json"
            module._publish_private_receipt(target, b'{"ok": true}\n')
            self.assertEqual(
                orchestrator._salvage_staged_bytes(target, 4096), b'{"ok": true}\n')
            os.chmod(target, 0o644)
            with self.assertRaises(orchestrator._SalvageRefusal) as caught:
                orchestrator._salvage_staged_bytes(target, 4096)
            self.assertEqual(caught.exception.args[0], "salvage_not_private_regular_file")


class FailedStageStdoutTests(unittest.TestCase):
    """A paid failure has to be readable; a credential still must not be."""

    def setUp(self):
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "host_privacy_stdout")

    def _remote(self, **fields):
        result = types.SimpleNamespace(**fields)
        with mock.patch.object(self.orchestrator.subprocess, "run", return_value=result):
            return self.orchestrator._remote(["ssh", "host", "probe"], timeout=1)

    def test_the_runner_typed_refusal_reaches_the_stage_record(self):
        refusal = json.dumps({"error_code": "input_rejected", "status": "refused"}, sort_keys=True)
        receipt = self._remote(returncode=2, stdout=refusal, stderr="")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["stdout_tail"], refusal)
        self.assertEqual(receipt["stderr_tail"], "")

    def test_a_completed_stage_retains_no_stdout_at_all(self):
        receipt = self._remote(returncode=0, stdout="anything at all", stderr="")
        self.assertEqual(receipt["status"], "completed")
        self.assertNotIn("stdout_tail", receipt)

    def test_the_tail_is_bounded_to_the_same_limit_as_stderr(self):
        receipt = self._remote(returncode=1, stdout="x" * 5_000, stderr="y" * 5_000)
        self.assertEqual(len(receipt["stdout_tail"]), self.orchestrator._STDERR_TAIL_LIMIT)
        self.assertEqual(len(receipt["stderr_tail"]), self.orchestrator._STDERR_TAIL_LIMIT)
        self.assertLessEqual(self.orchestrator._STDERR_TAIL_LIMIT, 2048)

    def test_credential_shaped_stdout_is_redacted_not_retained(self):
        for noisy in ("Authorization: Bearer hf_abcdefghijklmnop",
                      "HF_TOKEN=hf_abcdefghijklmnop",
                      "api_key: sk-do-not-retain"):
            with self.subTest(noisy):
                receipt = self._remote(returncode=2, stdout=noisy, stderr="")
                self.assertEqual(receipt["stdout_tail"], "<redacted>")
                self.assertNotIn("do-not-retain", json.dumps(receipt))
                self.assertNotIn("hf_abcdefghijklmnop", json.dumps(receipt))

    def test_a_truncated_json_fragment_cannot_break_receipt_persistence(self):
        """The tail is truncated after screening, so re-validate what is kept.

        A stream whose last 1200 bytes happen to *begin* with `{` is a JSON
        fragment the persisted-value validator would reject on its own, and a
        receipt carrying it could not be written at all -- which is how a
        diagnostic aid turns into a second lost run.
        """

        limit = self.orchestrator._STDERR_TAIL_LIMIT
        fragment = "p" * 100 + "{" + "q" * (limit - 1)
        self.assertTrue(fragment[-limit:].startswith("{"))
        receipt = self._remote(returncode=2, stdout=fragment, stderr="")
        self.assertEqual(receipt["stdout_tail"], "<redacted>")
        self.orchestrator.j1m_runner.validate_persisted_receipt(receipt)

        whole = '{"metrics": {"' + "a" * 4000 + '": 1}}'
        kept = self._remote(returncode=2, stdout=whole, stderr="")["stdout_tail"]
        self.orchestrator.j1m_runner.validate_persisted_output(kept)
        self.assertLessEqual(len(kept), limit)

    def test_a_transport_timeout_also_records_its_stdout(self):
        expired = self.orchestrator.subprocess.TimeoutExpired(
            ["ssh"], 1, output="partial output", stderr="")
        with mock.patch.object(self.orchestrator.subprocess, "run", side_effect=expired):
            receipt = self.orchestrator._remote(["ssh", "host", "probe"], timeout=1)
        self.assertEqual(receipt["status"], "transport_timeout")
        self.assertEqual(receipt["stdout_tail"], "partial output")


class OfflineHostSimulationTests(unittest.TestCase):
    """The gate that would have caught run d before it was billed."""

    def setUp(self):
        self.dry_run = load(ROOT / "scripts/j1m_dry_run.py", "host_privacy_dry_run")
        self.sf = sf

    def test_the_current_plan_accepts_every_private_write(self):
        outcome = self.dry_run.host_tree_simulation(
            umask_prefix=self.sf.remote_shell_prefix())
        self.assertEqual(outcome["plan"], "current")
        self.assertEqual(outcome["refused"], [])
        self.assertEqual(outcome["created_file_mode"], "0600")
        self.assertGreaterEqual(outcome["probed_paths"], 7)
        self.assertEqual(len(outcome["written_paths"]), 2)
        # The progress file and the command receipt are the two writes the
        # runner performs before any stage runs, and both must be provable.
        self.assertEqual(outcome["written_paths"], [
            "/scratch/experiments/runtime/J1M.progress.json",
            "/scratch/j1m/artifacts/command-receipt.json"])
        # The trusted root stays 0755 throughout: the policy allows that, and
        # a simulation that quietly made it 0700 would prove nothing.
        self.assertEqual(outcome["root_mode"], "0755")

    def test_the_failed_runs_own_plan_is_refused_by_the_same_simulation(self):
        outcome = self.dry_run.host_tree_simulation(
            umask_prefix=self.dry_run._IMAGE_DEFAULT_UMASK_PREFIX, legacy=True)
        self.assertEqual(outcome["plan"], "run-d")
        self.assertEqual(outcome["created_file_mode"], "0644")
        self.assertEqual(outcome["written_paths"], [])
        refused = {item["path"]: item["error"] for item in outcome["refused"]}
        self.assertEqual(len(refused), outcome["probed_paths"])
        # Both halves of the incident, each with its own distinct refusal.
        self.assertEqual(refused["/scratch/experiments/runtime/J1M.progress.json"],
                         "private ancestor is unavailable")
        self.assertEqual(refused["/scratch/j1m/artifacts/command-receipt.json"],
                         "private ancestor is unsafe")
        self.assertEqual(refused["/scratch/j1m/artifacts/toolchain-receipt.json"],
                         "private ancestor is unsafe")

    def test_the_simulation_only_ever_executes_mkdir_chmod_and_touch(self):
        self.assertEqual(self.dry_run._HOST_REPLAYABLE_PROGRAMS,
                         frozenset({"mkdir", "chmod", "touch"}))
        config = load(ROOT / "scripts/j1m_runner.py", "host_privacy_sim_runner").load_config()
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                commands = self.dry_run._host_replay_commands(config, legacy=legacy)
                self.assertTrue(commands)
                for argv in commands:
                    self.assertIn(argv[0], self.dry_run._HOST_REPLAYABLE_PROGRAMS)
                    for part in argv[1:]:
                        self.assertTrue(part.startswith("/scratch") or part.startswith("-")
                                        or part == "700", part)

    def test_the_simulation_refuses_a_path_outside_its_stand_in_root(self):
        config = load(ROOT / "scripts/j1m_runner.py", "host_privacy_escape_runner").load_config()
        with mock.patch.object(self.dry_run, "_host_replay_commands",
                               return_value=[["mkdir", "-p", "/etc/j1m-should-never-exist"]]):
            with self.assertRaises(self.dry_run.DryRunError):
                self.dry_run.host_tree_simulation(umask_prefix=self.sf.remote_shell_prefix())
        self.assertFalse(Path("/etc/j1m-should-never-exist").exists())
        self.assertTrue(config)

    def test_the_gate_reports_both_simulation_checks(self):
        receipt_names = [
            "host_tree_simulation_accepts_every_private_write",
            "host_tree_simulation_reproduces_the_run_d_refusal",
        ]
        source = (ROOT / "scripts/j1m_dry_run.py").read_text(encoding="utf-8")
        for name in receipt_names:
            self.assertIn(name, source)

    def test_the_replay_never_touches_the_repository_tree(self):
        before = sorted(item.name for item in ROOT.iterdir())
        self.dry_run.host_tree_simulation(umask_prefix=self.sf.remote_shell_prefix())
        self.assertEqual(sorted(item.name for item in ROOT.iterdir()), before)


class NoWeakenedPolicyTests(unittest.TestCase):
    """The fix may not have relaxed any privacy predicate to get its PASS."""

    def test_the_private_writer_and_salvage_predicates_are_unchanged(self):
        runner = (ROOT / "scripts/j1m_runner.py").read_text(encoding="utf-8")
        orchestrator = (ROOT / "scripts/j1m_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("permissions & (0o077 if strict_permissions else 0o022)", runner)
        self.assertIn("stat.S_IMODE(info.st_mode) != 0o600", runner)
        self.assertIn("before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077", orchestrator)
        self.assertIn('raise _SalvageRefusal("salvage_not_private_regular_file")', orchestrator)

    def test_the_replayed_shell_text_is_only_the_umask_prefix(self):
        """No plan command may smuggle further shell syntax past the argv rule."""

        self.assertEqual(sf.remote_shell_prefix(), ["umask", "077", "&&"])
        proc = subprocess.run(
            ["/bin/sh", "-c", "umask 077 && umask"], check=True,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.stdout.strip().lstrip("0"), "77")


if __name__ == "__main__":
    unittest.main()


class FailedPlanSummaryTests(unittest.TestCase):
    """A failed `--run` must describe itself over the SSH transport.

    Run `j1m-eval-20260911-remote-e` booked its whole USD 3.273486 reservation
    and reported `exit 1` with both streams empty, because `--run` returned 1
    without printing anything and the real evidence stayed in
    `command-receipt.json` on a host about to be deleted. The reservation is
    charged in full whether or not the failure can be read, so the summary has
    to survive the orchestrator's own credential screen -- if it does not, the
    tail is replaced wholesale and the diagnosis is lost again.
    """

    def setUp(self):
        self.runner = load(ROOT / "scripts/j1m_runner.py", "plan_summary_runner")
        self.orchestrator = load(ROOT / "scripts/j1m_orchestrator.py", "plan_summary_orchestrator")

    def _receipt(self, stage, status, **extra):
        receipt = {
            "stage": stage, "argv": ["/scratch/j1m/venv/bin/pip", "wheel", "--wheel-dir", "/scratch/j1m/wheelhouse"],
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "ended_at_utc": "2026-01-01T00:00:01+00:00",
            "exit_code": 0 if status == "completed" else 1, "status": status,
        }
        receipt.update(extra)
        return receipt

    def test_the_summary_names_the_failed_stage_without_leaking_the_argv(self):
        receipts = [
            self._receipt(1, "completed"),
            self._receipt(2, "failed", stderr_tail="ERROR: could not build wheels"),
        ]
        summary = self.runner._failed_plan_summary(receipts)
        self.assertEqual(summary["status"], "plan_failed")
        self.assertEqual(summary["failed_stage"], 2)
        self.assertEqual(summary["stages_recorded"], 2)
        self.assertEqual(summary["stages_completed"], 1)
        self.assertEqual(summary["exit_code"], 1)
        self.assertEqual(summary["stderr_tail"], "ERROR: could not build wheels")
        # Program and operand are basenames: enough to name the stage, never
        # the full argv the receipt already holds.
        self.assertEqual(summary["failed_program"], "pip")
        self.assertEqual(summary["failed_operand"], "wheel")
        for value in summary.values():
            self.assertNotIn("/scratch", str(value))

    def test_the_summary_survives_the_orchestrators_credential_screen(self):
        """The property that actually matters: it reaches the lifecycle receipt."""

        receipts = [self._receipt(1, "failed", stderr_tail="fatal: repository not found")]
        text = json.dumps(self.runner._failed_plan_summary(receipts), sort_keys=True)
        # Exactly what `_remote` does to a failed stage's stdout.
        self.assertEqual(self.orchestrator._failed_stage_output_tail(text), text)
        self.assertNotEqual(self.orchestrator._failed_stage_output_tail(text), "<redacted>")

    def test_a_plan_that_recorded_no_stage_is_typed_rather_than_invented(self):
        summary = self.runner._failed_plan_summary([])
        self.assertEqual(summary["error_type"], "no_stage_receipts")
        self.assertEqual(summary["stages_recorded"], 0)
        self.assertNotIn("failed_stage", summary)
        text = json.dumps(summary, sort_keys=True)
        self.assertEqual(self.orchestrator._failed_stage_output_tail(text), text)

    def test_an_unscreenable_stderr_tail_is_redacted_not_dropped(self):
        receipts = [self._receipt(1, "failed", stderr_tail="tok " + "a1b2c3d4" * 12)]
        summary = self.runner._failed_plan_summary(receipts)
        # Whatever the screen decides, the structural fields always survive and
        # the whole summary still reaches the receipt.
        self.assertEqual(summary["failed_stage"], 1)
        self.assertIn("stderr_tail", summary)
        text = json.dumps(summary, sort_keys=True)
        self.assertEqual(self.orchestrator._failed_stage_output_tail(text), text)
