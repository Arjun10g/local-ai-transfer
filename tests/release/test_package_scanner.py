import tempfile
import unittest
from unittest import mock
from pathlib import Path

from qa.clean_machine.package import HOST_RUNTIME_FILES, scan_binary_dependencies, scan_tree
from qa.clean_machine.package_runner import NODE_EXE_SHA256, NODE_LICENSE_SHA256, build_package


class PackageScannerTests(unittest.TestCase):
    def test_repository_source_template_is_allowlisted(self):
        result = scan_tree(Path("release/windows"), require_runtime=False)
        self.assertEqual("PASS", result["status"], result)
        self.assertEqual("SKIP", result["native_windows_launch"])
        start = Path("release/windows/Start-LocalAssistant.ps1").read_text(encoding="utf-8")
        self.assertIn("portable-supervisor.mjs", start)
        self.assertNotIn("PythonCommand", start)
        self.assertNotIn("LAE_ENGINE_TOKEN", start)
        self.assertIn("AssignProcessToJobObject", start)
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000", start)
        self.assertIn("ShellExecute($bootstrapUrl)", start)
        self.assertNotIn("Start-Process $bootstrapUrl", start)
        self.assertIn("NamedPipeServerStream", start)
        self.assertIn("RevealBootstrapUrl", start)
        self.assertIn("if ($RevealBootstrapUrl) { Write-Output $bootstrapUrl }", start)

    def test_builder_closes_host_dependencies_without_target_python(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            engine = base / "lae-engine-cpu.exe"; engine.write_bytes(b"MZ\0kernel32.dll\0")
            node = base / "node.exe"; node.write_bytes(b"MZ\0kernel32.dll\0")
            node_license = base / "LICENSE"; node_license.write_text("fixture license", encoding="utf-8")
            output = base / "package"
            with mock.patch("qa.clean_machine.package_runner.file_sha256", side_effect=lambda path: NODE_EXE_SHA256 if path.name == "node.exe" else NODE_LICENSE_SHA256):
                result = build_package(Path(".").resolve(), engine, node, node_license, output)
            self.assertEqual("PASS", result["status"], result)
            packaged = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
            self.assertTrue(HOST_RUNTIME_FILES.issubset(packaged))
            manifest = __import__("json").loads((output / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["python_required_on_target"])
            self.assertNotIn("windows_backend_plan.py", manifest["files"])
            self.assertNotIn("Run-WindowsBackend.ps1", manifest["files"])
            self.assertNotIn("lae-host.mjs", manifest["files"])
            self.assertIn("runtime/node.exe", manifest["files"])
            self.assertIn("licenses/Node.js-LICENSE.txt", manifest["files"])
            self.assertIn("licenses/llama.cpp-LICENSE.txt", manifest["files"])
            self.assertEqual(NODE_EXE_SHA256, manifest["bundled_node"]["sha256"])

    def test_weight_and_secret_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("Start-LocalAssistant.ps1", "Run-WindowsBackend.ps1", "windows_backend_plan.py", "config.example.json", "ui/index.html", "THIRD_PARTY_NOTICES.md", "SBOM.spdx.json", "RELEASE_MANIFEST.json", "CHECKSUMS.sha256", "README-OPERATOR.md"):
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
