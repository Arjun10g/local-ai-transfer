import tempfile
import unittest
from pathlib import Path

from qa.clean_machine.package import scan_binary_dependencies, scan_tree


class PackageScannerTests(unittest.TestCase):
    def test_repository_skeleton_is_allowlisted_but_not_runnable(self):
        result = scan_tree(Path("release/windows"), require_runtime=False)
        self.assertEqual("PASS", result["status"], result)
        self.assertEqual("SKIP", result["native_windows_launch"])

    def test_weight_and_secret_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("lae-host.mjs", "Start-LocalAssistant.ps1", "config.example.json", "ui/index.html", "THIRD_PARTY_NOTICES.md", "SBOM.spdx.json", "RELEASE_MANIFEST.json", "CHECKSUMS.sha256", "README-OPERATOR.md"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe", encoding="utf-8")
            (root / "Qwen3.5-9B.gguf").write_bytes(b"weights")
            (root / "credentials.txt").write_text("api_key = TEST_ONLY_SECRET_VALUE", encoding="utf-8")
            result = scan_tree(root)
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any(item.startswith("forbidden-artifact:") for item in result["findings"]))
        self.assertTrue(any(item.startswith("secret-pattern:") for item in result["findings"]))

    def test_unknown_dll_is_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fixture.exe"
            binary.write_bytes(b"MZ\x00evil.dll\x00kernel32.dll\x00")
            result = scan_binary_dependencies(binary)
        self.assertEqual(["evil.dll"], result["unresolved"])


if __name__ == "__main__":
    unittest.main()
