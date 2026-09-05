import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qa.clean_machine.package import (
    BoundedFileError,
    FORBIDDEN_PACKAGE_PATHS,
    HOST_RUNTIME_FILES,
    PACKAGE_ALLOWLIST,
    checksums,
    scan_binary_dependencies,
    scan_tree,
)
from qa.clean_machine.package_runner import PACKAGE_BUILD_BLOCKER, PACKAGE_READINESS, build_package


ROOT = Path(__file__).resolve().parents[2]


class ExplodingPath:
    def __fspath__(self):
        raise AssertionError("caller path was accessed")


class PackageScannerTests(unittest.TestCase):
    def test_repository_source_lint_is_bounded_advisory_only(self):
        result = scan_tree(ROOT / "release/windows", require_runtime=False)
        self.assertEqual("PASS", result["status"], result)
        self.assertEqual("ADVISORY-SOURCE-LINT-ONLY", result["authorization"])
        self.assertEqual("SKIP", result["native_windows_launch"])

        manifest = json.loads((ROOT / "release/windows/RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual("fixture-skeleton", manifest["kind"])
        self.assertEqual("REFUSED-NOT_READY", manifest["package_build"])
        self.assertEqual("REFUSED-NOT_READY", manifest["native_windows_launch"])
        self.assertTrue(FORBIDDEN_PACKAGE_PATHS.isdisjoint(PACKAGE_ALLOWLIST))
        self.assertTrue(FORBIDDEN_PACKAGE_PATHS.isdisjoint(HOST_RUNTIME_FILES))
        self.assertIn("host/tools/local/platform-safety.mjs", HOST_RUNTIME_FILES)
        self.assertIn("host/tools/local/platform-safety.mjs", PACKAGE_ALLOWLIST)
        self.assertNotIn("lae-host.mjs", manifest["files"])

        notices = (ROOT / "release/windows/THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("does not bundle `runtime/node.exe`", notices)
        self.assertNotIn("license texts are included", notices.lower())

    def test_runtime_scan_refuses_before_path_access(self):
        with mock.patch.dict(scan_tree.__globals__, {"_tree_entries": mock.Mock(side_effect=AssertionError("enumerated"))}):
            result = scan_tree(ExplodingPath(), require_runtime=True)
        self.assertEqual("FAIL", result["status"])
        self.assertEqual(PACKAGE_READINESS, result["readiness"])
        self.assertEqual("NONE", result["authorization"])
        self.assertEqual(["secure-handle-relative-package-scan-unavailable"], result["findings"])

    def test_builder_refuses_before_path_access_or_output_write(self):
        path = ExplodingPath()
        result = build_package(path, path, path, path, path)
        self.assertEqual(
            {
                "status": "FAIL",
                "readiness": PACKAGE_READINESS,
                "stage": "safety-unavailable",
                "findings": [PACKAGE_BUILD_BLOCKER],
                "output_created": False,
                "native_windows_launch": "REFUSED",
            },
            result,
        )
        with self.assertRaisesRegex(BoundedFileError, "handle-relative package checksums"):
            checksums(path, ["file"])

    def test_windows_entrypoints_refuse_without_access_write_or_spawn_primitives(self):
        scripts = (
            "Start-LocalAssistant.ps1",
            "Build-WindowsBackend.ps1",
            "Run-WindowsBackend.ps1",
            "Verify-Release.ps1",
        )
        forbidden = (
            "Add-Type",
            "Get-Item",
            "Get-Content",
            "Get-FileHash",
            "New-Item",
            "Set-Content",
            "System.Diagnostics.Process",
            "Start-Process",
            "Invoke-Expression",
            "& $",
        )
        for name in scripts:
            source = (ROOT / "release/windows" / name).read_text(encoding="utf-8")
            self.assertIn("throw 'NOT_READY:", source, name)
            self.assertIn("no path was accessed", source, name)
            for token in forbidden:
                self.assertNotIn(token, source, name)

    def test_advisory_scanner_rejects_weights_secrets_and_unknown_dlls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Qwen3.5-9B.gguf").write_bytes(b"weights")
            (root / "credentials.txt").write_text("api_key = TEST_ONLY_SECRET_VALUE", encoding="utf-8")
            result = scan_tree(root)
            self.assertEqual("FAIL", result["status"])
            self.assertTrue(any(item.startswith("forbidden-artifact:") for item in result["findings"]))
            self.assertTrue(any(item.startswith("secret-pattern:") for item in result["findings"]))

            binary = root / "fixture.exe"
            binary.write_bytes(b"MZ\0evil.dll\0kernel32.dll\0")
            self.assertEqual(["evil.dll"], scan_binary_dependencies(binary)["unresolved"])

    def test_advisory_tree_and_file_reads_have_explicit_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RELEASE_MANIFEST.json").write_text("[]", encoding="utf-8")
            result = scan_tree(root)
            self.assertIn("manifest-invalid", result["findings"])

            with mock.patch.dict(scan_tree.__globals__, {"MAX_TREE_ENTRIES": 2}):
                for name in ("one", "two", "three"):
                    (root / name).write_text("x", encoding="utf-8")
                result = scan_tree(root)
            self.assertIn("tree-exceeds-2-entries", result["findings"])

    def test_metadata_only_scan_never_opens_forbidden_weight_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "weights.gguf").write_bytes(b"secret model bytes")
            with mock.patch.dict(scan_tree.__globals__, {"read_bounded_file": mock.Mock(side_effect=AssertionError("forbidden read"))}):
                result = scan_tree(root, metadata_only=True)
            self.assertEqual("FAIL", result["status"])
            self.assertEqual("ADVISORY-METADATA-ONLY", result["authorization"])
            self.assertIn("forbidden-artifact:weights.gguf", result["findings"])


if __name__ == "__main__":
    unittest.main()
