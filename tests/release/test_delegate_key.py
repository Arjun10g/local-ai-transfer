"""The coding-assistant key on the Python side: `bmo_local.py delegate-key`.

The host (host/delegate/state.mjs) and this command must agree on where the
per-user state folder is and on the key's format, and the key file must be
private: 0600 in a 0700 folder, never a symlink, printed only to stdout.
"""

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_local  # noqa: E402

KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")
POSIX = os.name != "nt"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class StateDirTests(unittest.TestCase):
    def test_each_platform_resolves_the_documented_folder(self):
        sd = bmo_local.delegate_state_dir
        self.assertEqual(sd({"LOCALAPPDATA": "C:\\Users\\op\\AppData\\Local"}, "win32"), "C:\\Users\\op\\AppData\\Local\\BMO")
        self.assertEqual(sd({}, "darwin", "/Users/op"), "/Users/op/Library/Application Support/BMO")
        self.assertEqual(sd({}, "linux", "/home/op"), "/home/op/.local/state/bmo")
        self.assertEqual(sd({"XDG_STATE_HOME": "/xdg"}, "linux", "/home/op"), "/xdg/bmo")
        self.assertEqual(sd({"XDG_STATE_HOME": "rel"}, "linux", "/home/op"), "/home/op/.local/state/bmo")
        self.assertEqual(sd({"BMO_STATE_DIR": "/s"}, "linux", "/home/op"), "/s")
        for env, system in (({}, "win32"), ({"LOCALAPPDATA": "\\\\server\\share"}, "win32"), ({"BMO_STATE_DIR": "rel"}, "linux")):
            with self.subTest(env=env), self.assertRaises(ValueError):
                sd(env, system, "/home/op")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_host_resolves_the_same_folder(self):
        script = ("import('./host/delegate/state.mjs').then(m => { const cases = JSON.parse(process.argv[1]);"
                  " console.log(JSON.stringify(cases.map(([env, platform, home]) => m.resolveStateDir({ env, platform, home })))); })")
        cases = [[{"LOCALAPPDATA": "C:\\Users\\op\\AppData\\Local"}, "win32", None], [{}, "darwin", "/Users/op"],
                 [{}, "linux", "/home/op"], [{"XDG_STATE_HOME": "/xdg"}, "linux", "/home/op"], [{"BMO_STATE_DIR": "/s"}, "linux", "/h"]]
        out = subprocess.run(["node", "-e", script, json.dumps(cases)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [bmo_local.delegate_state_dir(env, system, home) for env, system, home in cases])


class DelegateKeyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "a", "BMO")

    def test_first_use_creates_a_private_key_that_then_stays(self):
        key, new = bmo_local.delegate_key(self.dir)
        self.assertTrue(new)
        self.assertRegex(key, KEY)
        if POSIX:
            self.assertEqual(mode(self.dir), 0o700)
            self.assertEqual(mode(os.path.join(self.dir, "delegate-key")), 0o600)
        self.assertEqual(bmo_local.delegate_key(self.dir), (key, False))
        self.assertEqual(sorted(os.listdir(self.dir)), ["delegate-key"], "no temporary file is left")

    def test_the_key_is_created_private_not_fixed_up_afterwards(self):
        # The file is opened 0600 from the start: a later chmod would leave a
        # window in which another user could read it.
        modes = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *args, **kwargs):
            if str(path).startswith(self.dir):
                modes.append(mode)
            return real_open(path, flags, mode, *args, **kwargs)

        with mock.patch.object(bmo_local.os, "open", spy):
            bmo_local.delegate_key(self.dir)
            bmo_local.delegate_key(self.dir, rotate=True)
        self.assertEqual(modes, [0o600, 0o600])

    def test_rotate_replaces_the_key(self):
        key, _ = bmo_local.delegate_key(self.dir)
        fresh, new = bmo_local.delegate_key(self.dir, rotate=True)
        self.assertTrue(new)
        self.assertNotEqual(fresh, key)
        self.assertEqual(bmo_local.read_delegate_key(self.dir), fresh)
        if POSIX:
            self.assertEqual(mode(os.path.join(self.dir, "delegate-key")), 0o600)

    def test_a_damaged_key_is_refused_not_silently_replaced(self):
        os.makedirs(self.dir, mode=0o700)
        Path(self.dir, "delegate-key").write_text("not a key\n")
        with self.assertRaises(ValueError):
            bmo_local.delegate_key(self.dir)
        self.assertEqual(Path(self.dir, "delegate-key").read_text(), "not a key\n")

    @unittest.skipUnless(POSIX, "POSIX modes")
    def test_open_permissions_are_closed_and_a_symlink_is_refused(self):
        key, _ = bmo_local.delegate_key(self.dir)
        os.chmod(self.dir, 0o755)
        os.chmod(os.path.join(self.dir, "delegate-key"), 0o644)
        self.assertEqual(bmo_local.delegate_key(self.dir), (key, False))
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(os.path.join(self.dir, "delegate-key")), 0o600)
        other = os.path.join(os.path.dirname(self.dir), "elsewhere")
        os.makedirs(other)
        link = os.path.join(os.path.dirname(self.dir), "link")
        os.symlink(other, link)
        with self.assertRaises(PermissionError):
            bmo_local.delegate_key(link)

    def test_the_command_prints_only_the_key_on_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"BMO_STATE_DIR": self.dir}), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(bmo_local.main(["delegate-key"]), 0)
        key = out.getvalue().strip()
        self.assertRegex(key, KEY)
        self.assertEqual(out.getvalue(), key + "\n")
        self.assertNotIn(key, err.getvalue(), "the explanation never repeats the key")
        self.assertIn("Approve", err.getvalue())
        out2 = io.StringIO()
        with mock.patch.dict(os.environ, {"BMO_STATE_DIR": self.dir}), redirect_stdout(out2), redirect_stderr(io.StringIO()):
            self.assertEqual(bmo_local.main(["delegate-key", "--rotate"]), 0)
        self.assertNotEqual(out2.getvalue().strip(), key)

    def test_a_bad_state_folder_fails_with_a_message(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"BMO_STATE_DIR": "relative/dir"}), redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(bmo_local.main(["delegate-key"]), 1)
        self.assertIn("BMO_STATE_DIR", err.getvalue())

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_host_reads_the_key_this_command_made_and_vice_versa(self):
        key, _ = bmo_local.delegate_key(self.dir)
        read = ("import('./host/delegate/state.mjs').then(async m => console.log(await m.readDelegateKey(process.argv[1])))")
        out = subprocess.run(["node", "-e", read, self.dir], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), key, out.stderr)
        rotate = ("import('./host/delegate/state.mjs').then(async m => { await m.rotateDelegateKey(process.argv[1]); })")
        self.assertEqual(subprocess.run(["node", "-e", rotate, self.dir], cwd=ROOT, timeout=60).returncode, 0)
        fresh = bmo_local.read_delegate_key(self.dir)
        self.assertRegex(fresh, KEY)
        self.assertNotEqual(fresh, key)


if __name__ == "__main__":
    unittest.main()
