"""Turning coding-assistant jobs on from the launchers.

Delegation is off unless the operator asks: `bmo_app.py --delegate` (or
`Start-BMO.ps1 -Mode app -EnableDelegation`) is the only way the launcher
sets LAE_DELEGATE_ENABLED, an inherited value is dropped, and the key is
shown by `-ShowDelegateKey`, which needs no model and puts no secret on a
command line.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_app  # noqa: E402

TOKEN = "T" * 43
START = ROOT / "local" / "windows" / "Start-BMO.ps1"


class AppLauncherTests(unittest.TestCase):
    def env(self, inherited=None, **kwargs):
        return bmo_app.host_environment(inherited or {"PATH": "/bin"}, port=1, token=TOKEN, model="m", backend="b", **kwargs)

    def test_off_unless_asked_and_an_inherited_flag_is_dropped(self):
        self.assertNotIn("LAE_DELEGATE_ENABLED", self.env())
        self.assertNotIn("LAE_DELEGATE_ENABLED", self.env({"PATH": "/bin", "LAE_DELEGATE_ENABLED": "1"}))
        self.assertEqual(self.env(delegate=True)["LAE_DELEGATE_ENABLED"], "1")
        # The state folder override is the operator's own and passes through.
        self.assertEqual(self.env({"PATH": "/bin", "BMO_STATE_DIR": "/s"})["BMO_STATE_DIR"], "/s")

    def test_the_flag_exists_and_defaults_off(self):
        parser = bmo_app.build_parser()
        base = ["--engine", "e", "--model", "m"]
        self.assertFalse(parser.parse_args(base).delegate)
        self.assertTrue(parser.parse_args([*base, "--delegate"]).delegate)


class StartScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = START.read_text(encoding="utf-8")

    def test_show_and_rotate_need_no_model_and_run_the_python_command(self):
        self.assertIn("[CmdletBinding(DefaultParameterSetName = 'Run')]", self.text)
        self.assertIn("[Parameter(Mandatory = $true, ParameterSetName = 'Run')] [string] $ModelPath", self.text)
        self.assertIn("[Parameter(ParameterSetName = 'Key')] [switch] $ShowDelegateKey", self.text)
        self.assertIn("[Parameter(ParameterSetName = 'Key')] [switch] $RotateDelegateKey", self.text)
        block = self.text[self.text.index("if ($ShowDelegateKey -or $RotateDelegateKey)"):]
        block = block[:block.index("exit $LASTEXITCODE")]
        self.assertIn("'delegate-key'", block)
        self.assertIn("'--rotate'", block)
        # Handled before anything that needs an engine or a model.
        self.assertLess(self.text.index("if ($ShowDelegateKey -or $RotateDelegateKey)"), self.text.index("$Built = "))

    def test_enable_delegation_is_app_mode_only_and_passes_the_flag(self):
        self.assertIn("if ($EnableDelegation -and $Mode -ne 'app') { throw", self.text)
        self.assertIn("if ($EnableDelegation) { $argv += '--delegate' }", self.text)
        self.assertNotRegex(self.text, r"(?i)delegate[-_]?key\s*=|BMO_DELEGATE_KEY", "the key is never put on a command line or in the environment here")

    def test_script_stays_ascii(self):
        START.read_bytes().decode("ascii")

    def test_readme_documents_the_switches(self):
        readme = (ROOT / "local" / "windows" / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("## 4e."):readme.index("## 5. Tool-call evaluation")]
        for needle in ("-ShowDelegateKey", "-EnableDelegation", "-RotateDelegateKey", "host.json", '"delegate": true', "Approve"):
            self.assertIn(needle, section)
        for line in section.splitlines():
            if "Start-BMO.ps1" not in line:
                continue
            for token in line.split("Start-BMO.ps1", 1)[1].split():
                if re.match(r"^-[A-Za-z]\w*$", token):
                    self.assertRegex(self.text, rf"\]\s*\${token[1:]}\b", f"{token} is a real parameter")


if __name__ == "__main__":
    unittest.main()
