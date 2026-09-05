import ast
import importlib.util
from pathlib import Path
import tempfile
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

    def test_unknown_inventory_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests/host").mkdir(parents=True)
            (root / "tests/host/unknown.test.mjs").write_text("// unknown", encoding="utf-8")
            with patch.object(runner, "scan_tree", return_value={"status": "PASS", "findings": []}):
                plan = runner.safe_plan(root)
        self.assertEqual(plan["inventory"]["unknown"], ["tests/host/unknown.test.mjs"])
        self.assertTrue(any(item.get("reason") == "unknown_test_inventory_entry" for item in plan["results"]))

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


if __name__ == "__main__":
    unittest.main()
