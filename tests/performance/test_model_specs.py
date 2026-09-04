#!/usr/bin/env python3
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts/model-artifact/validate_specs.py"
spec = importlib.util.spec_from_file_location("validate_specs", VALIDATOR)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ModelSpecTests(unittest.TestCase):
    def test_source_lock_is_immutable_and_complete(self):
        lock = json.loads((ROOT / "model/source-lock/qwen35-9b.source-lock.json").read_text())
        self.assertEqual(lock["revision"], "c202236235762e1c871ad0ccb60c8ee5ba337b9a")
        self.assertEqual(lock["revision"], lock["resolved_from"]["repository_sha_from_api"])
        self.assertEqual(module.check_lock(ROOT / "model/source-lock/qwen35-9b.source-lock.json"), [])

    def test_phase_zero_specs_validate(self):
        errors = module.check_json_specs(ROOT)
        self.assertEqual(errors, [])

    def test_schema_is_strict(self):
        schema = json.loads((ROOT / "model/manifests/qwen35-9b-q4-k-m.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("artifact", schema["required"])
        self.assertEqual(schema["$defs"]["runtime"]["properties"]["default_context_tokens"]["const"], 8192)


if __name__ == "__main__":
    unittest.main()
