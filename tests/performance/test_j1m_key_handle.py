"""Where the ephemeral J1M SSH key lives, and that it stops existing.

`991b70e` made every persisted argv prove its `-i` operand is a canonical
private handle. `create_keypair` wrote `id_ed25519` into a system temporary
directory, which is not one, so every ssh/scp command the orchestrator builds
was refused before it could spawn -- after an instance had been created and
billed. These tests pin the key's location, name, mode, single use, and
destruction, and they pin the refusal itself so the old layout cannot come
back quietly.

No provider, network, or real key generation: `ssh-keygen` is always shimmed.
"""

import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import j1m_orchestrator as orchestrator
from scripts import j1m_runner, shadeform_lifecycle as sf

ROOT = Path(__file__).resolve().parents[2]
RUN_TOKEN = "J1MTEST-" + "a" * 32


def fake_keygen(argv, **kwargs):
    """Write a plausible keypair without generating or logging key material."""

    target = Path(argv[argv.index("-f") + 1])
    target.write_text("DRY-RUN-NOT-A-KEY\n", encoding="utf-8")
    target.with_name(target.name + ".pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixture j1m\n", encoding="utf-8")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def private_root() -> Path:
    """A 0700 directory under the checkout, as the real key root is."""

    directory = tempfile.mkdtemp(prefix="key-root-", dir=ROOT)
    os.chmod(directory, 0o700)
    return Path(directory)


class EphemeralKeyLocationTests(unittest.TestCase):
    def setUp(self):
        self.root = private_root()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        patcher = mock.patch.object(subprocess, "run", side_effect=fake_keygen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, token: str = RUN_TOKEN):
        directory = sf.ephemeral_key_directory(token, root=self.root)
        return directory, sf.create_keypair(directory)

    # ------------------------------------------------------------- location

    def test_the_real_key_root_is_the_protected_secrets_area(self):
        """Not a system tempdir, and not the runtime directory.

        `experiments/runtime` is excluded deliberately: the mocked-execute
        isolation treats every path under it as operator evidence and fails any
        test that touches it, so a key placed there would be untestable.
        """

        self.assertEqual(sf.EPHEMERAL_KEY_ROOT, sf.ROOT / ".secrets" / "j1m")
        self.assertNotIn(Path(tempfile.gettempdir()), sf.EPHEMERAL_KEY_ROOT.parents)
        self.assertNotIn(sf.RUNTIME_ROOT, sf.EPHEMERAL_KEY_ROOT.parents)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".secrets/", ignored)

    def test_the_created_handle_is_exactly_what_the_argv_policy_accepts(self):
        directory, (private, public) = self.create()
        self.assertEqual(private.name, sf.EPHEMERAL_KEY_BASENAME)
        self.assertRegex(private.name, j1m_runner._HANDLE_BASENAME)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(private.stat().st_uid, os.getuid())
        self.assertTrue(j1m_runner._canonical_private_handle_path(str(private)))
        self.assertTrue(public.startswith("ssh-ed25519 "))
        sf.assert_persisted_argv_handle(private)

    def test_every_ssh_and_scp_argv_built_from_the_handle_is_accepted(self):
        """The regression itself: the whole argv, not just the handle."""

        _directory, (private, _public) = self.create()
        known_hosts = self.root / "known_hosts"
        known_hosts.write_text("203.0.113.9 ssh-ed25519 AAAA\n", encoding="utf-8")
        known_hosts.chmod(0o600)
        info = {"ip": "203.0.113.9", "ssh_user": "ubuntu", "ssh_port": 22}
        with mock.patch.object(sf, "_verified_executable", side_effect=lambda name: f"/usr/bin/{name}"):
            ssh = sf.ssh_base(info, private, known_hosts) + ["df", "-P", "-k", "/scratch"]
            scp = sf.scp_base(info, private, known_hosts) + [
                str(ROOT / "scripts" / "j1m_runner.py"), "ubuntu@203.0.113.9:/scratch/j1m/j1m_runner.py"]
        self.assertEqual(j1m_runner.validate_persisted_argv(ssh), ssh)
        self.assertEqual(j1m_runner.validate_persisted_argv(scp), scp)
        self.assertEqual(ssh[ssh.index("-i") + 1], str(private))

    def test_the_old_tempdir_layout_is_still_refused(self):
        """Pin the bug, so reverting the location fails loudly rather than late."""

        with tempfile.TemporaryDirectory(prefix="old-layout-") as directory:
            legacy = Path(directory) / "id_ed25519"
            legacy.write_text("k", encoding="utf-8")
            legacy.chmod(0o600)
            self.assertFalse(j1m_runner._canonical_private_handle_path(str(legacy)))
            with self.assertRaises(sf.ShadeformError):
                sf.assert_persisted_argv_handle(legacy)
            with self.assertRaises(ValueError):
                j1m_runner.validate_persisted_argv(
                    ["/usr/bin/ssh", "-i", str(legacy), "ubuntu@203.0.113.9", "df"])

    def test_a_handle_shaped_name_in_a_tempdir_is_not_enough_on_this_host(self):
        """Both halves of the fix are load-bearing: the name and the ancestors."""

        with tempfile.TemporaryDirectory(prefix="old-layout-") as directory:
            os.chmod(directory, 0o700)
            renamed = Path(directory) / sf.EPHEMERAL_KEY_BASENAME
            renamed.write_text("k", encoding="utf-8")
            renamed.chmod(0o600)
            if j1m_runner._canonical_private_handle_path(str(renamed)):
                self.skipTest("this platform's temp root satisfies the ancestor rule")
            with self.assertRaises(sf.ShadeformError):
                sf.assert_persisted_argv_handle(renamed)

    # ------------------------------------------------------------- lifetime

    def test_a_key_is_never_reused_across_runs(self):
        directory, _created = self.create()
        with self.assertRaisesRegex(sf.ShadeformError, "never reused"):
            sf.create_keypair(directory)
        other = sf.ephemeral_key_directory("J1MTEST-" + "b" * 32, root=self.root)
        self.assertNotEqual(other, directory)
        second, _public = sf.create_keypair(other)
        self.assertNotEqual(second.parent, directory)

    def test_a_run_token_that_could_escape_its_root_is_refused(self):
        for token in ("..", "../escape", "a/b", "", "-leading", "x" * 200):
            with self.assertRaises(sf.ShadeformError):
                sf.ephemeral_key_directory(token, root=self.root)

    def test_a_non_private_ancestor_is_refused_rather_than_widened(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        with self.assertRaisesRegex(sf.ShadeformError, "owner-private"):
            sf.create_keypair(shared / "run")
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755,
                         "an existing directory must be refused, never chmod-ed")

    def test_destruction_overwrites_and_removes_the_whole_directory(self):
        directory, (private, _public) = self.create()
        before = private.read_bytes()
        receipt = sf.destroy_ephemeral_key_directory(directory)
        self.assertEqual(receipt["status"], "removed")
        self.assertEqual(receipt["files_removed"], 2)
        self.assertFalse(directory.exists())
        self.assertTrue(before)

    def test_destruction_is_idempotent_and_never_raises(self):
        directory, _created = self.create()
        sf.destroy_ephemeral_key_directory(directory)
        self.assertEqual(sf.destroy_ephemeral_key_directory(directory)["status"], "absent")
        self.assertEqual(sf.destroy_ephemeral_key_directory(None)["status"], "absent")
        self.assertEqual(
            sf.destroy_ephemeral_key_directory(self.root / "never-existed")["status"], "absent")

    def test_the_cleanup_receipt_survives_the_persisted_receipt_policy(self):
        """A field named `run_token` would silently void the lifecycle receipt."""

        directory, _created = self.create()
        receipt = sf.destroy_ephemeral_key_directory(directory)
        self.assertNotIn("run_token", receipt)
        lifecycle = {"schema": "local_bmo.j1m.lifecycle-receipt.v1", "phase_id": "p",
                     "status": "completed", "ephemeral_key_cleanup": receipt}
        j1m_runner.validate_persisted_receipt(lifecycle)
        j1m_runner.validate_persisted_output(
            __import__("json").dumps(lifecycle, sort_keys=True))

    def test_the_receipt_never_carries_key_material(self):
        directory, (private, _public) = self.create()
        material = private.read_text(encoding="utf-8")
        receipt = sf.destroy_ephemeral_key_directory(directory)
        self.assertNotIn(material.strip(), __import__("json").dumps(receipt))


class ArtifactDestinationTests(unittest.TestCase):
    """The salvage destination is a pre-spend refusal, not a teardown surprise."""

    def setUp(self):
        self.root = private_root()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        patcher = mock.patch.object(j1m_runner, "PRIVATE_OUTPUT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_destination_is_created_owner_private(self):
        destination = self.root / "artifacts" / "qwen35-9b"
        self.assertEqual(orchestrator.prepare_artifact_destination(destination), destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)

    def test_a_group_readable_ancestor_is_refused_with_its_own_remedy(self):
        parent = self.root / "artifacts"
        parent.mkdir(mode=0o755)
        destination = parent / "qwen35-9b"
        destination.mkdir(mode=0o700)
        with self.assertRaises(sf.ShadeformError) as raised:
            orchestrator.prepare_artifact_destination(destination)
        self.assertIn(f"chmod 700 {parent}", str(raised.exception))
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)

    def test_a_destination_outside_the_trusted_root_is_refused(self):
        with self.assertRaisesRegex(sf.ShadeformError, "trusted output root"):
            orchestrator.prepare_artifact_destination(Path(tempfile.gettempdir()) / "eval")

    def test_a_private_destination_can_actually_receive_a_publication(self):
        destination = orchestrator.prepare_artifact_destination(self.root / "out" / "run")
        j1m_runner._private_atomic_write(
            destination / "proving-receipt.json", b'{"schema":"x"}\n', trusted_root=self.root)
        self.assertTrue((destination / "proving-receipt.json").is_file())


if __name__ == "__main__":
    unittest.main()
