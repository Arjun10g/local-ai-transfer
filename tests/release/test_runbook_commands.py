"""The Python-equivalent commands in docs/DELL_AGENT_RUNBOOK.md section 2A must parse.

An agent on a locked-down laptop will copy these when PowerShell scripts are blocked, so a renamed flag would
strand it exactly when it needs them. Each documented command is parsed by the real argument parser (nothing runs).
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local"))
import bmo_app  # noqa: E402
import bmo_chat  # noqa: E402
import bmo_local  # noqa: E402

RUNBOOK = (ROOT / "docs" / "DELL_AGENT_RUNBOOK.md").read_text(encoding="utf-8")
E, M = "local/bin/lae-engine.exe", "model.gguf"


class RunbookCommandTests(unittest.TestCase):
    def test_local_launcher_commands_parse(self):
        parser = bmo_local.build_parser()
        for argv in (["preflight", "--engine", E, "--model", M, "--verify-hash"],
                     ["smoke", "--engine", E, "--model", M, "--out", "o.json"],
                     ["bench", "--engine", E, "--model", M, "--out", "o.json", "--threads", "8", "--threads-batch", "16", "--speculate", "4"],
                     ["eval", "--engine", E, "--model", M, "--out", "o.json"],
                     ["eval", "--engine", E, "--model", M, "--cases", "a,b,c", "--show-output", "--out", "o.json"],
                     ["longctx", "--engine", E, "--model", M, "--out", "o.json"],
                     ["bench", "--engine", E, "--model", M, "--backend", "intel-vulkan", "--vulkan-device-name", "Intel(R) Arc(TM) Graphics"],
                     ["serve", "--engine", E, "--model", M, "--snapshots", "4", "--idle-unload-minutes", "1"],
                     ["delegate-key"], ["delegate-key", "--rotate"]):
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_chat_and_app_commands_parse(self):
        bmo_chat.build_parser().parse_args(["--engine", E, "--model", M, "--max-tokens", "512", "--snapshots", "4", "--idle-unload-minutes", "1"])
        bmo_app.build_parser().parse_args(["--engine", E, "--model", M, "--host-config", "c.json", "--delegate"])

    def test_the_runbook_still_documents_each_command(self):
        for needle in ("bmo_local.py preflight", "bmo_local.py smoke", "bmo_local.py bench", "bmo_local.py eval", "--cases a,b,c --show-output",
                       "bmo_local.py longctx", "bmo_chat.py --engine", "bmo_app.py --engine", "delegate-key", "get_model.py --folder",
                       "Get-ExecutionPolicy -List", "SSL_CERT_FILE", "Arjun10g/local-ai-transfer", "engine-v1", "d12958d377b7e1bb74bf74202f2266a429d607a3f0b93c64f2081266ba407513", "Never disable certificate verification"):
            self.assertIn(needle, RUNBOOK)
        self.assertRegex(RUNBOOK, re.compile(r"never[^\n]*(disable|defeat)", re.I))


if __name__ == "__main__":
    unittest.main()
