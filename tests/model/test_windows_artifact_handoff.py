from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qa.conformance.runner import schema_errors


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("windows_handoff", ROOT / "scripts/model-artifact/verify_windows_handoff.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def handoff() -> dict:
    value = {
        "schema": module.SCHEMA,
        "artifact": {
            "model_id": module.MODEL_ID,
            "file_name": module.MODEL_NAME,
            "size_bytes": 5629109088,
            "sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "source_revision": "c" * 40,
            "llama_cpp_revision": "d" * 40,
            "quantization": "Q4_K_M",
            "text_only": True,
            "vision_projection_present": False,
        },
        "evidence": {key: str(index + 1) * 64 for index, key in enumerate((
            "source_model_receipt_sha256", "conversion_receipt_sha256", "model_receipt_sha256",
            "scan_receipt_sha256", "artifact_manifest_sha256", "toolchain_receipt_sha256", "checksums_sha256",
        ))},
        "release": {"manifest_sha256": "8" * 64, "package_kind": "portable-release", "model_included": False},
        "security": {"no_model_bytes_in_handoff": True, "no_credentials": True, "no_urls": True, "offline_verifier": True},
        "signature": {"algorithm": "ed25519", "key_id": "release-key-v1", "signature_base64": base64.b64encode(b"s" * 64).decode("ascii"), "payload_sha256": "0" * 64},
    }
    value["evidence"]["artifact_manifest_sha256"] = value["artifact"]["manifest_sha256"]
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    value["signature"]["payload_sha256"] = hashlib.sha256(json.dumps(unsigned, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")).hexdigest()
    return value


class WindowsArtifactHandoffTests(unittest.TestCase):
    def test_contract_schema_accepts_only_the_fixture_shape(self):
        schema = json.loads((ROOT / "contracts/windows-release-artifact-handoff/v1.0.0.json").read_text(encoding="utf-8"))
        self.assertEqual([], schema_errors(handoff(), schema))

    def test_public_verifier_refuses_without_trust_anchor(self):
        result = module.verify_handoff(handoff())
        self.assertEqual("REFUSED_NOT_ACTIVATED", result["status"])
        self.assertFalse(result["signature_verified"])
        self.assertFalse(result["activated"])

    def test_injected_verifier_is_the_only_trusted_seam(self):
        calls = []
        result = module._verify_handoff_with_trust_anchor(handoff(), lambda payload, signature, key_id: calls.append((payload, signature, key_id)) or False)
        self.assertEqual("REFUSED_NOT_ACTIVATED", result["status"])
        result = module._verify_handoff_with_trust_anchor(handoff(), lambda payload, signature, key_id: True)
        self.assertEqual("VERIFIED", result["status"])
        self.assertTrue(result["signature_verified"])

    def test_public_api_has_no_verifier_parameter(self):
        with self.assertRaises(TypeError):
            module.verify_handoff(handoff(), lambda *_args: True)

    def test_callback_cannot_bypass_invalid_identity(self):
        value = handoff()
        value["artifact"]["file_name"] = "other.gguf"
        with self.assertRaisesRegex(module.HandoffError, "artifact identity"):
            module._verify_handoff_with_trust_anchor(value, lambda *_args: True)

    def test_rejects_unknown_or_missing_keys(self):
        value = handoff(); value["unexpected"] = True
        with self.assertRaises(module.HandoffError): module.validate_handoff(value)
        value = handoff(); del value["evidence"]["checksums_sha256"]
        with self.assertRaises(module.HandoffError): module.validate_handoff(value)

    def test_rejects_payload_digest_and_signature_encoding_changes(self):
        value = handoff(); value["artifact"]["size_bytes"] += 1
        with self.assertRaisesRegex(module.HandoffError, "payload digest"): module.validate_handoff(value)
        value = handoff(); value["signature"]["signature_base64"] = "not*base64"
        with self.assertRaisesRegex(module.HandoffError, "signature encoding"): module.validate_handoff(value)
        value = handoff(); value["signature"]["signature_base64"] = base64.b64encode(b"short").decode("ascii")
        with self.assertRaisesRegex(module.HandoffError, "signature exceeds"): module.validate_handoff(value)

    def test_rejects_model_metadata_variants(self):
        for key, bad in (("sha256", "A" * 64), ("source_revision", "e" * 39), ("quantization", "Q8_0"), ("text_only", False), ("vision_projection_present", True)):
            value = handoff(); value["artifact"][key] = bad
            with self.assertRaises(module.HandoffError): module.validate_handoff(value)

    def test_receipt_manifest_digest_must_bind_artifact_identity(self):
        value = handoff(); value["evidence"]["artifact_manifest_sha256"] = "9" * 64
        with self.assertRaisesRegex(module.HandoffError, "cross-bound"):
            module.validate_handoff(value)

    def test_rejects_release_inclusion_or_security_downgrade(self):
        value = handoff(); value["release"]["model_included"] = True
        with self.assertRaises(module.HandoffError): module.validate_handoff(value)
        value = handoff(); value["security"]["no_urls"] = False
        with self.assertRaises(module.HandoffError): module.validate_handoff(value)

    def test_rejects_duplicate_json_keys(self):
        raw = json.dumps(handoff(), sort_keys=True).replace('"schema":', '"schema":"duplicate", "schema":', 1)
        with self.assertRaisesRegex(module.HandoffError, "duplicate JSON key"):
            module.parse_handoff_bytes(raw.encode("utf-8"))

    def test_rejects_invalid_utf8_nonfinite_and_deep_json(self):
        with self.assertRaises(module.HandoffError): module.parse_handoff_bytes(b"\xff")
        with self.assertRaises(module.HandoffError): module.parse_handoff_bytes(b'{"x":NaN}')
        with self.assertRaises(module.HandoffError): module.parse_handoff_bytes((b'{"x":' + b'[' * 20 + b'0' + b']' * 20 + b'}'))

    def test_rejects_oversize_and_nonobject_inputs(self):
        with self.assertRaises(module.HandoffError): module.parse_handoff_bytes(b"x" * (module.MAX_HANDOFF_BYTES + 1))
        with self.assertRaises(module.HandoffError): module.parse_handoff_bytes(b"[]")

    def test_load_requires_regular_nonlink_bounded_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "handoff.json"; path.write_text(json.dumps(handoff()), encoding="utf-8")
            self.assertEqual(module.SCHEMA, module.load_handoff(path)["schema"])
            link = root / "link.json"
            try: link.symlink_to(path)
            except OSError: self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(module.HandoffError, "regular non-link"): module.load_handoff(link)

    def test_load_requires_single_link_and_nofollow_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"
            path.write_text(json.dumps(handoff()), encoding="utf-8")
            hardlink = Path(directory) / "handoff-copy.json"
            try:
                os.link(path, hardlink)
            except OSError:
                self.skipTest("hard links unavailable")
            with self.assertRaisesRegex(module.HandoffError, "identity or size"):
                module.load_handoff(path)
            hardlink.unlink()
            with mock.patch.object(module.os, "O_NOFOLLOW", None):
                with self.assertRaisesRegex(module.HandoffError, "nofollow"):
                    module.load_handoff(path)

    def test_cli_is_sanitized_and_refuses_without_external_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"
            path.write_text(json.dumps(handoff()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = module.main([str(path)])
            self.assertEqual(2, code)
            rendered = output.getvalue()
            self.assertIn("REFUSED_NOT_ACTIVATED", rendered)
            self.assertNotIn(str(path), rendered)
            self.assertNotIn("a" * 64, rendered)

    def test_verifier_exception_is_fail_closed(self):
        with self.assertRaisesRegex(module.HandoffError, "failed closed"):
            module._verify_handoff_with_trust_anchor(handoff(), lambda *_args: (_ for _ in ()).throw(RuntimeError("secret")))

    def test_receipt_and_release_digests_are_strict(self):
        value = handoff(); value["evidence"]["model_receipt_sha256"] = "0" * 63
        with self.assertRaises(module.HandoffError): module.validate_handoff(value)
        value = handoff(); value["evidence"]["model_receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(module.HandoffError, "placeholder"): module.validate_handoff(value)
        value = handoff(); value["release"]["manifest_sha256"] = "G" * 64
        with self.assertRaises(module.HandoffError): module.validate_handoff(value)

    def test_canonical_payload_is_ascii_and_contains_no_model_bytes(self):
        value = handoff(); payload = module._canonical_payload(value)
        self.assertTrue(payload.isascii())
        self.assertNotIn(b"s" * 64, payload)
        self.assertNotIn(b"model-bytes", payload)


if __name__ == "__main__":
    unittest.main()
