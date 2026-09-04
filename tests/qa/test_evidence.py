import json
import os
from pathlib import Path
import tempfile
import unittest

from qa.harness.evidence import audit_environment, build_evidence_manifest, validate_evidence_manifest


class EvidenceTests(unittest.TestCase):
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
