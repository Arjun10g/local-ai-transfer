from __future__ import annotations

import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
