import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/test/run_qa.py"
spec = importlib.util.spec_from_file_location("safe_run_qa", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SafeRunnerTests(unittest.TestCase):
    def test_runner_source_has_no_subprocess_execution_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any(alias.name == "subprocess" for node in imports for alias in node.names))
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("cmake --build", source)

    def test_safe_plan_does_not_call_process_runner_and_blocks_execution(self):
        with patch.object(runner, "scan_tree", return_value={"status": "PASS", "findings": []}) as scan:
            plan = runner.safe_plan(ROOT, skip_native=True)
        scan.assert_called_once()
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertIn("safe_mode_execution_disabled", plan["release_blockers"])
        native = next(item for item in plan["results"] if item.get("test") == "QA-003-native-cmake")
        self.assertEqual(native["status"], "SKIP")
        self.assertIn("skip-native", native["reason"])
        self.assertFalse(any("command" in item for item in plan["results"]))
        with patch.object(runner, "scan_tree", return_value={"status": "PASS", "findings": []}):
            release_plan = runner.safe_plan(ROOT, release=True)
        self.assertEqual(release_plan["status"], "BLOCKED")
        self.assertFalse(release_plan["release_passed"])

    def test_unknown_inventory_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests/host").mkdir(parents=True)
            (root / "tests/host/unknown.test.mjs").write_text("// unknown", encoding="utf-8")
            with patch.object(runner, "scan_tree", return_value={"status": "PASS", "findings": []}):
                plan = runner.safe_plan(root)
        self.assertEqual(plan["inventory"]["unknown"], ["tests/host/unknown.test.mjs"])
        self.assertTrue(any(item.get("reason") == "unknown_test_inventory_entry" for item in plan["results"]))

    def test_inventory_exactly_matches_current_tests_without_content_reads(self):
        with (
            patch.object(Path, "open", side_effect=AssertionError("test content must not be opened")),
            patch.object(Path, "read_text", side_effect=AssertionError("test content must not be read")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("test content must not be read")),
        ):
            inventory = runner.inventory_check(ROOT)
        self.assertIsNone(inventory["discovery_error"])
        self.assertEqual(inventory["unknown"], [])
        self.assertEqual(inventory["missing"], [])
        self.assertIn(
            "tests/host/windows-fs-refusal-slice.test.mjs",
            inventory["discovered"],
        )

    def test_model_and_lifecycle_are_explicitly_skipped(self):
        with patch.object(runner, "scan_tree", return_value={"status": "PASS", "findings": []}):
            plan = runner.safe_plan(ROOT)
        by_name = {item["test"]: item for item in plan["results"] if "test" in item}
        self.assertEqual(by_name["MODEL-REAL-QWEN-ORACLE"]["status"], "SKIP")
        self.assertEqual(by_name["SHADEFORM-ACCEPTANCE"]["status"], "SKIP")
        self.assertEqual(by_name["tests/performance/test_j1m_lifecycle.py"]["classification"], "lifecycle")
        self.assertEqual(by_name["tests/performance/test_j1m_lifecycle.py"]["status"], "SKIP")

    def test_protected_operator_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "protected operator evidence"):
            runner.validate_output_path(Path("experiments/runtime/incidents.jsonl"))
        with self.assertRaisesRegex(ValueError, "protected operator evidence"):
            runner.validate_output_path(Path("cost-ledger.jsonl"))

    def test_tap_parser_keeps_strict_inventory_helper(self):
        valid = """TAP version 13
ok 1 - safe
1..1
# tests 1
# suites 0
# pass 1
# fail 0
# cancelled 0
# skipped 0
# todo 0
"""
        self.assertEqual(runner.parse_tap_report(valid)["tests"], 1)
        with self.assertRaises(runner.ReporterError):
            runner.parse_tap_report(valid.replace("1..1", "1..2"))
        with self.assertRaises(runner.ReporterError):
            runner.parse_tap_report(valid.replace("1..1", "# FOO\n1..1"))
        with self.assertRaises(runner.ReporterError):
            runner.parse_tap_report(valid.replace("safe", "safe # FOO"))

    def test_node_inventory_covers_model_tests_and_rejects_linked_discovery(self):
        self.assertIn("tests/model/production_tool_fixture_parity.test.mjs", runner.discover_node_tests(ROOT))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests/host").mkdir(parents=True)
            try:
                (root / "tests/host/linked.test.mjs").symlink_to(ROOT / "tests/host/action-journal.test.mjs")
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(runner.DiscoveryError):
                runner.discover_node_tests(root)

    def test_tap_skip_and_unknown_status_cannot_pass(self):
        contradictory = """TAP version 13
ok 1 - skipped # SKIP not run
1..1
# tests 1
# suites 0
# pass 1
# fail 0
# cancelled 0
# skipped 0
# todo 0
"""
        with self.assertRaises(runner.ReporterError):
            runner.parse_tap_report(contradictory)
        self.assertEqual(runner.summarize_records([])["status"], "BLOCKED")
        self.assertFalse(runner.summarize_records([{"test": "x", "status": "BROKEN"}])["release_passed"])
        self.assertEqual(runner.summarize_records([{"test": "x", "status": "UNKNOWN"}])["status"], "BLOCKED")

    def test_output_writer_is_atomic_and_rejects_aliases_and_casefolded_operator_paths(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            target = root / "summary.json"
            runner.write_output_atomically(target, "first\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "first\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            alias = root / "alias.json"
            os.link(target, alias)
            with self.assertRaisesRegex(ValueError, "already exists"):
                runner.write_output_atomically(alias, "must-not-truncate\n")
            with self.assertRaisesRegex(ValueError, "link count"):
                runner.validate_output_path(alias)
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                pass
            else:
                with self.assertRaisesRegex(ValueError, "link or reparse"):
                    runner.validate_output_path(link)
            with self.assertRaisesRegex(ValueError, "protected operator evidence"):
                runner.validate_output_path(root / "Experiments" / "RUNTIME" / "INCIDENTS.JSONL")

    def test_linked_output_parent_is_rejected_without_a_write(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            linked = root / "linked"
            try:
                linked.symlink_to(real, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            target = linked / "summary.json"
            with self.assertRaisesRegex(ValueError, "output directory"):
                runner.write_output_atomically(target, "must-not-publish\n")
            self.assertFalse((real / "summary.json").exists())

    def test_replaced_temporary_identity_is_never_published(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            target = root / "summary.json"
            original_reopen = runner._reopen_verified_temp
            replaced = False

            def replace_before_reopen(parent_fd, name, expected, data, *, links=1):
                nonlocal replaced
                if not replaced and name.startswith(".summary.json.tmp-"):
                    replaced = True
                    os.rename(name, f"{name}.original", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    replacement = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    try:
                        os.fchmod(replacement, 0o600)
                        os.write(replacement, data)
                        os.fsync(replacement)
                    finally:
                        os.close(replacement)
                return original_reopen(parent_fd, name, expected, data, links=links)

            with patch.object(runner, "_reopen_verified_temp", side_effect=replace_before_reopen):
                with self.assertRaisesRegex(ValueError, "reopened temporary output identity changed"):
                    runner.write_output_atomically(target, "trusted\n")
            self.assertTrue(replaced)
            self.assertFalse(target.exists())

    def test_parent_path_swap_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            parent = root / "evidence"
            parent.mkdir(mode=0o700)
            target = parent / "summary.json"
            parked = root / "original-parent"
            original_verify = runner._verify_parent_path_identity
            swapped = False

            def swap_before_verify(path, expected):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    parent.rename(parked)
                    parent.mkdir(mode=0o700)
                return original_verify(path, expected)

            with patch.object(runner, "_verify_parent_path_identity", side_effect=swap_before_verify):
                with self.assertRaisesRegex(ValueError, "identity or metadata changed"):
                    runner.write_output_atomically(target, "trusted\n")
            self.assertTrue(swapped)
            self.assertFalse(target.exists())

    def test_output_metadata_drift_checks_fail_closed(self):
        valid = {
            "st_mode": stat.S_IFREG | 0o600,
            "st_uid": os.geteuid(),
            "st_gid": os.getegid(),
            "st_nlink": 1,
            "st_size": 7,
        }
        mutations = (
            {"st_mode": stat.S_IFREG | 0o644},
            {"st_uid": os.geteuid() + 1},
            {"st_nlink": 2},
            {"st_size": 8},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                info = SimpleNamespace(**(valid | mutation))
                with self.assertRaisesRegex(ValueError, "ownership, mode, link count, size"):
                    runner._validate_private_file_info(info, size=7)

    def test_windows_file_output_refuses_before_path_or_plan_access(self):
        class HostilePath:
            def __fspath__(self):
                raise AssertionError("Windows refusal inspected the path")

        with patch.object(runner.os, "name", "nt"):
            with self.assertRaisesRegex(ValueError, "secure file output is unavailable"):
                runner.write_output_atomically(HostilePath(), "ignored")
            with (
                patch.object(runner.sys, "argv", ["run_qa.py", "--output", "report.json"]),
                patch.object(runner, "safe_plan", side_effect=AssertionError("plan must not run")) as plan,
                patch.object(runner.sys, "stderr", io.StringIO()),
            ):
                self.assertEqual(runner.main(), 2)
                plan.assert_not_called()

    def test_unsupported_platform_stdout_mode_remains_plan_only_and_blocked(self):
        plan = {
            "inventory": {"discovered": [], "unknown": [], "missing": [], "classes": {}, "discovery_error": None},
            "results": [],
            "passed": False,
            "release_passed": False,
            "status": "BLOCKED",
            "release_blockers": ["safe_mode_execution_disabled"],
            "mode": "safe",
        }
        stdout = io.StringIO()
        with (
            patch.object(runner, "_safe_file_output_supported", return_value=False),
            patch.object(runner.sys, "argv", ["run_qa.py", "--output", "-"]),
            patch.object(runner.sys, "stdout", stdout),
            patch.object(runner, "safe_plan", return_value=plan),
            patch.object(runner, "write_output_atomically", side_effect=AssertionError("file output must remain disabled")) as writer,
        ):
            self.assertEqual(runner.main(), 1)
            writer.assert_not_called()
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["status"], "BLOCKED")
        self.assertFalse(parsed["release_passed"])


if __name__ == "__main__":
    unittest.main()
