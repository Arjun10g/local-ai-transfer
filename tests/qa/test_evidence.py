import json
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

from qa.harness.evidence import audit_environment, build_evidence_manifest, validate_evidence_manifest


ROOT = Path(__file__).resolve().parents[2]
_qa_spec = importlib.util.spec_from_file_location("canonical_run_qa", ROOT / "scripts/test/run_qa.py")
assert _qa_spec and _qa_spec.loader
run_qa = importlib.util.module_from_spec(_qa_spec)
_qa_spec.loader.exec_module(run_qa)


class EvidenceTests(unittest.TestCase):
    def test_canonical_node_discovery_includes_security_tests(self):
        discovered = run_qa.discover_node_tests(ROOT)
        self.assertTrue(any(name.startswith("tests/host/") for name in discovered))
        self.assertIn("tests/security/permission-mode-adversarial.test.mjs", discovered)
        self.assertIn("tests/security/tool-calling-adversarial.test.mjs", discovered)

    def test_mandatory_skip_is_conditional_not_release_pass(self):
        summary = run_qa.summarize_records([
            {"status": "PASS", "test": "fixture", "mandatory": True},
            run_qa.skipped("real-target", "not-run"),
        ])
        self.assertEqual(summary["status"], "CONDITIONAL_PASS")
        self.assertTrue(summary["passed"])
        self.assertFalse(summary["release_passed"])
        self.assertEqual(summary["mandatory_unproven"], ["real-target"])

    def test_failure_is_not_masked_by_conditional_evidence(self):
        summary = run_qa.summarize_records([
            {"status": "FAIL", "test": "fixture", "mandatory": True},
            run_qa.skipped("real-target", "not-run"),
        ])
        self.assertEqual(summary["status"], "FAIL")
        self.assertFalse(summary["passed"])
        self.assertFalse(summary["release_passed"])

    def test_manifest_has_valid_schema_and_no_environment_values(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_evidence_manifest(source_root=directory, build_id="test-build")
        self.assertEqual([], validate_evidence_manifest(manifest))
        self.assertTrue(all(set(item) == {"present"} for item in manifest["environment"]["keys"].values()))
        self.assertNotIn("PATH", manifest["environment"]["keys"])

    def test_si001_spacing_and_lowercase_credential_are_presence_only(self):
        secret = "TEST_ONLY_SI001_SECRET_VALUE"
        result = audit_environment(
            [f"git_access = {secret}", f"SAFE_MODE = {secret}", f"API_TOKEN={secret}"],
            allowlist=("SAFE_MODE",),
        )
        encoded = json.dumps(result, sort_keys=True)
        self.assertEqual({"SAFE_MODE": {"present": True}}, result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("git_access", encoded)
        self.assertNotIn("API_TOKEN", encoded)

    def test_mapping_values_are_not_returned(self):
        result = audit_environment({"CI": "TOP_SECRET", "PATH": "/private"}, allowlist=("CI",))
        self.assertEqual({"CI": {"present": True}}, result)
        self.assertNotIn("TOP_SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
