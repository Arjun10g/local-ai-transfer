import unittest

from qa.adversarial.checks import bounded_json, no_shell_string, reject_path_escape, require_loopback, safe_https_url, validate_tool_envelope
from qa.harness.shadeform import validate_job_manifest, warning_level


class AdversarialTests(unittest.TestCase):
    def test_loopback_only(self):
        self.assertTrue(require_loopback("127.0.0.1"))
        self.assertFalse(require_loopback("0.0.0.0"))

    def test_path_escape_and_ads(self):
        self.assertTrue(reject_path_escape("notes/readme.md"))
        for value in ("../secret", "..\\secret", "C:/secret", "notes/x:stream", "\\\\server\\share"):
            self.assertFalse(reject_path_escape(value))

    def test_json_depth_limit(self):
        self.assertEqual({"ok": True}, bounded_json('{"ok":true}'))
        with self.assertRaises(ValueError):
            bounded_json("[" * 40 + "0" + "]" * 40, max_depth=16)

    def test_tool_and_process_boundaries(self):
        self.assertTrue(validate_tool_envelope({"id": "call_1", "name": "time.now", "arguments": {}}))
        self.assertFalse(validate_tool_envelope({"id": "call_1", "name": "time.now", "arguments": {}, "execute": True}))
        self.assertTrue(no_shell_string(["git", "status"]))
        self.assertFalse(no_shell_string("git status"))

    def test_url_policy(self):
        self.assertTrue(safe_https_url("https://example.invalid"))
        for value in ("file:///tmp/a", "http://example.invalid", "https://127.0.0.1", "https://u:p@example.invalid"):
            self.assertFalse(safe_https_url(value))

    def test_shadeform_budget_and_cleanup(self):
        import json
        from pathlib import Path
        job = json.loads(Path("qa/fixtures/shadeform/job.json").read_text(encoding="utf-8"))
        invalid = json.loads(Path("qa/fixtures/shadeform/job-invalid.json").read_text(encoding="utf-8"))
        self.assertEqual([], validate_job_manifest(job))
        self.assertTrue(validate_job_manifest(invalid))
        self.assertEqual("OK", warning_level(0))
        self.assertEqual("WARN", warning_level(50))
        self.assertEqual("URGENT", warning_level(80))
        self.assertEqual("STOP", warning_level(100))


if __name__ == "__main__":
    unittest.main()
