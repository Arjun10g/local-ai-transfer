from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest

from tests.reference.windows_release_verifier_model import (
    HostileRequest,
    ReferenceContractError,
    TrackedTree,
    canonical_manifest_bytes,
    canonical_receipt,
    manifest_sha256,
    validate_manifest,
    verify_public,
    verify_reference,
)


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native/windows_release_verifier/windows_release_verifier.cpp"
HPP = ROOT / "native/windows_release_verifier/windows_release_verifier.hpp"
CONTRACT_ROOT = ROOT / "contracts/windows-release-verifier"
SIGNER = "a" * 64


def fixture_manifest():
    contents = {
        "app/engine.exe": b"MZ synthetic inert engine fixture",
        "scripts/start.ps1": b"throw 'NOT_READY'\n",
        "ui/app.js": b"export const ready = false;\n",
        "ui/index.html": b"<!doctype html><title>fixture</title>\n",
    }
    entries = []
    for path in sorted(contents, key=str.casefold):
        extension = "." + path.rsplit(".", 1)[-1].casefold()
        signed = extension in {".exe", ".dll", ".ps1", ".py", ".js", ".mjs"}
        entries.append({
            "path": path,
            "size_bytes": len(contents[path]),
            "sha256": hashlib.sha256(contents[path]).hexdigest(),
            "signer_policy": "offline_authenticode_required" if signed else "not_applicable",
            "signer_subject_sha256": SIGNER if signed else None,
        })
    manifest = {
        "schema": "local_bmo.windows-release-manifest-identity.v0.1.0",
        "release_manifest_schema": "release-manifest.v1",
        "package_kind": "portable-release",
        "activation": False,
        "model": {
            "included": False,
            "disposition": "EXTERNAL_AND_VERIFIED_SEPARATELY",
        },
        "entries": entries,
    }
    return manifest, contents


def object_record(path, kind, file_id, *, content=None):
    signed = path.casefold().endswith((".exe", ".dll", ".ps1", ".py", ".js", ".mjs"))
    return {
        "path": path,
        "kind": kind,
        "file_id_before": file_id,
        "file_id_after": file_id,
        "volume_token": "volume-fixture",
        "private_current_user": True,
        "reparse": False,
        "delete_pending": False,
        "write_share_denied": True,
        "link_count": 1,
        "short_name": None,
        "streams": [] if kind == "directory" else ["::$DATA"],
        "content_base64": None if kind == "directory" else base64.b64encode(content).decode("ascii"),
        "signer_status": "valid_offline_exact_handle" if signed else "not_applicable",
        "signer_subject_sha256": SIGNER if signed else None,
    }


def fixture_tree():
    manifest, contents = fixture_manifest()
    directories = set()
    for path in contents:
        parts = path.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    objects = []
    for index, directory in enumerate(sorted(directories), 1):
        objects.append(object_record(directory, "directory", f"dir-{index}"))
    for index, path in enumerate(sorted(contents), 1):
        objects.append(object_record(path, "file", f"file-{index}", content=contents[path]))
    tree = {
        "volume": {
            "kind": "fixed",
            "filesystem": "NTFS",
            "hotplug": False,
            "network": False,
            "volume_token": "volume-fixture",
        },
        "root": {
            "file_id_before": "root",
            "file_id_after": "root",
            "volume_token": "volume-fixture",
            "private_current_user": True,
            "reparse": False,
            "delete_pending": False,
            "write_share_denied": True,
        },
        "objects": objects,
        "second_objects": copy.deepcopy(objects),
    }
    return manifest, TrackedTree(tree)


def mutate_case(manifest, tracked, mutation):
    tree = tracked._value  # explicit test-only fixture mutation
    files = [item for item in tree["objects"] if item["kind"] == "file"]
    target = files[0]
    second_target = next(item for item in tree["second_objects"] if item["path"] == target["path"])
    if mutation == "file_reparse":
        target["reparse"] = True
    elif mutation == "file_link_count_two":
        target["link_count"] = 2
    elif mutation == "file_delete_pending":
        target["delete_pending"] = True
    elif mutation == "file_write_share_allowed":
        target["write_share_denied"] = False
    elif mutation == "file_named_stream":
        target["streams"].append(":secret:$DATA")
    elif mutation == "file_short_name":
        target["short_name"] = "ENGINE~1.EXE"
    elif mutation == "extra_file":
        extra = object_record("extra.txt", "file", "extra", content=b"extra")
        tree["objects"].append(extra)
        tree["second_objects"].append(copy.deepcopy(extra))
    elif mutation == "missing_file":
        tree["objects"].remove(target)
        tree["second_objects"].remove(second_target)
    elif mutation == "case_collision":
        duplicate = copy.deepcopy(target)
        duplicate["path"] = target["path"].upper()
        duplicate["file_id_before"] = duplicate["file_id_after"] = "case-copy"
        tree["objects"].append(duplicate)
        tree["second_objects"].append(copy.deepcopy(duplicate))
    elif mutation == "hash_change":
        original = base64.b64decode(target["content_base64"])
        changed = bytes([original[0] ^ 1]) + original[1:]
        target["content_base64"] = base64.b64encode(changed).decode("ascii")
    elif mutation == "identity_change":
        target["file_id_after"] = "swapped"
    elif mutation == "second_identity_change":
        second_target["file_id_before"] = second_target["file_id_after"] = "swapped"
    elif mutation == "signer_change":
        target["signer_subject_sha256"] = "b" * 64
    elif mutation == "manifest_digest_change":
        return "0" * 64
    else:  # pragma: no cover - fixture/test mismatch
        raise AssertionError(mutation)
    return manifest_sha256(manifest)


class WindowsReleaseVerifierReferenceTests(unittest.TestCase):
    def test_public_refusal_precedes_request_and_tree_access(self):
        manifest, tracked = fixture_tree()
        digest = manifest_sha256(manifest)
        receipt = verify_public(HostileRequest(), tracked, digest)
        self.assertEqual("REFUSED_NOT_ACTIVATED", receipt["status"])
        self.assertEqual("trust_anchor_unavailable", receipt["reason"])
        self.assertEqual(0, tracked.access_count)

    def test_exact_synthetic_tree_verifies_only_inside_reference_seam(self):
        manifest, tracked = fixture_tree()
        receipt = verify_reference(
            manifest, tracked, manifest_sha256(manifest), trusted_test_boundary=True
        )
        self.assertEqual("VERIFIED", receipt["status"])
        self.assertEqual(len(manifest["entries"]), receipt["verified_files"])
        self.assertTrue(receipt["model_external"])
        self.assertFalse(receipt["activated"])
        self.assertEqual(1, tracked.access_count)

    def test_all_adversarial_cases_fail_with_expected_reason(self):
        cases = json.loads(
            (ROOT / "tests/native/fixtures/windows_release_verifier/cases.json").read_text(encoding="utf-8")
        )
        self.assertEqual("local_bmo.windows-release-verifier-fault-cases.v0.1.0", cases["schema"])
        self.assertEqual(len(cases["cases"]), len({item["name"] for item in cases["cases"]}))
        for case in cases["cases"]:
            with self.subTest(case=case["name"]):
                manifest, tracked = fixture_tree()
                digest = mutate_case(manifest, tracked, case["mutation"])
                receipt = verify_reference(
                    manifest, tracked, digest, trusted_test_boundary=True
                )
                self.assertEqual("FAILED", receipt["status"])
                self.assertEqual(case["expected_reason"], receipt["reason"])

    def test_manifest_rejects_duplicate_case_collision_paths_and_unsafe_names(self):
        manifest, _ = fixture_tree()
        duplicate = copy.deepcopy(manifest)
        duplicate["entries"].insert(1, copy.deepcopy(duplicate["entries"][0]))
        with self.assertRaises(ReferenceContractError):
            validate_manifest(duplicate)
        case_collision = copy.deepcopy(manifest)
        case_collision["entries"][1]["path"] = case_collision["entries"][0]["path"].upper()
        with self.assertRaises(ReferenceContractError):
            validate_manifest(case_collision)
        for bad in (
            "../escape.txt", "C:/absolute.txt", "//server/share", "safe:stream",
            "dir/CON.txt", "dir/FILE~1.TXT", "dir/trailing. ",
        ):
            value = copy.deepcopy(manifest)
            value["entries"][0]["path"] = bad
            value["entries"].sort(key=lambda item: item["path"].casefold())
            with self.subTest(path=bad), self.assertRaises(ReferenceContractError):
                validate_manifest(value)

    def test_manifest_rejects_model_entry_and_missing_signer_identity(self):
        manifest, _ = fixture_tree()
        model = copy.deepcopy(manifest)
        model["entries"][0]["path"] = "Qwen3.5-9B-Q4_K_M.gguf"
        model["entries"].sort(key=lambda item: item["path"].casefold())
        with self.assertRaisesRegex(ReferenceContractError, "model"):
            validate_manifest(model)
        unsigned = copy.deepcopy(manifest)
        unsigned["entries"][0]["signer_subject_sha256"] = None
        with self.assertRaisesRegex(ReferenceContractError, "signature"):
            validate_manifest(unsigned)

    def test_manifest_is_canonical_bounded_and_duplicate_key_free(self):
        manifest, _ = fixture_tree()
        data = canonical_manifest_bytes(manifest)
        self.assertEqual(data, canonical_manifest_bytes(json.loads(data)))
        self.assertLess(len(data), 16_384)
        self.assertEqual(64, len(manifest_sha256(manifest)))

    def test_receipt_is_canonical_metadata_only(self):
        manifest, tracked = fixture_tree()
        receipt = verify_reference(
            manifest, tracked, manifest_sha256(manifest), trusted_test_boundary=True
        )
        encoded = canonical_receipt(receipt)
        self.assertEqual(receipt, json.loads(encoded))
        self.assertLess(len(encoded), 4096)
        lowered = encoded.lower()
        for forbidden in (
            b"path", b"file_name", b"user", b"sid", b"volume_serial",
            b"file_id", b"certificate_subject", b"content", b"environment",
            b"qwen", b"gguf",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_root_volume_and_security_fail_closed(self):
        for field, value, reason in (
            ("private_current_user", False, "root_authority_invalid"),
            ("reparse", True, "root_authority_invalid"),
            ("delete_pending", True, "root_authority_invalid"),
            ("write_share_denied", False, "root_authority_invalid"),
            ("file_id_after", "swapped", "root_authority_invalid"),
        ):
            manifest, tracked = fixture_tree()
            tracked._value["root"][field] = value
            receipt = verify_reference(
                manifest, tracked, manifest_sha256(manifest), trusted_test_boundary=True
            )
            self.assertEqual(reason, receipt["reason"])
        for field, value in (
            ("kind", "remote"),
            ("filesystem", "ReFS"),
            ("hotplug", True),
            ("network", True),
        ):
            manifest, tracked = fixture_tree()
            tracked._value["volume"][field] = value
            receipt = verify_reference(
                manifest, tracked, manifest_sha256(manifest), trusted_test_boundary=True
            )
            self.assertEqual("unsafe_volume", receipt["reason"])


class WindowsReleaseVerifierStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = CPP.read_text(encoding="utf-8")
        cls.hpp = HPP.read_text(encoding="utf-8")

    def test_compile_surface_is_windows_only_and_safe_header_order(self):
        self.assertIn("#ifndef _WIN32", self.hpp)
        self.assertLess(self.hpp.index("#define NOMINMAX"), self.hpp.index("#include <windows.h>"))
        self.assertLess(self.hpp.index("#define WIN32_LEAN_AND_MEAN"), self.hpp.index("#include <windows.h>"))

    def test_public_gate_precedes_request_dereference_and_every_os_call(self):
        body = self.cpp[self.cpp.index("Status verify_release_tree("):]
        gate = body.index("if (!kCompiledManifestIdentityTrusted")
        request = body.index("request == nullptr")
        core = body.index("VerifierCore::run")
        self.assertLess(gate, request)
        self.assertLess(gate, core)
        self.assertIn("set_receipt(response, Status::kNotActivated)", body[gate:request])
        for gate_name in (
            "kCompiledManifestIdentityTrusted = false",
            "kAuthenticodePolicyTrusted = false",
            "kCancellableNativeIoTrusted = false",
        ):
            self.assertIn(gate_name, self.cpp)
        self.assertNotIn("getenv(", self.cpp)
        self.assertNotIn("GetEnvironmentVariable", self.cpp)
        self.assertNotIn("RegOpenKey", self.cpp)

    def test_handle_relative_identity_and_exact_inventory_primitives_present(self):
        for required in (
            "DuplicateHandle", "RootDirectory = parent", "NtOpenFile",
            "FILE_OPEN_REPARSE_POINT", "FILE_SHARE_READ",
            "FILE_ID_INFO", "FileIdInfo", "FILE_INTERNAL_INFO",
            "NtQueryDirectoryFile", "FileIdBothDirectoryInformation",
            "record->next_entry_offset", "record->file_name_length",
            "record->short_name_length != 0", "expected_child_count",
            "second full enumeration", "same_identity(root_before.identity",
        ):
            self.assertIn(required, self.cpp)

    def test_volume_object_and_security_guards_present(self):
        for required in (
            "GetVolumeInformationByHandleW", 'L\"NTFS\"', "DRIVE_FIXED",
            "IOCTL_STORAGE_GET_HOTPLUG_INFO", "MediaRemovable", "DeviceHotplug",
            "FILE_ATTRIBUTE_REPARSE_POINT", "standard.NumberOfLinks != 1",
            "standard.DeletePending", "SE_DACL_PROTECTED", "AceCount == 1",
            "FILE_ALL_ACCESS", "NtQueryInformationFile", "FileStreamInformation",
            'name == L\"::$DATA\"', 'name == L\":$I30:$INDEX_ALLOCATION\"',
            'name == L\":$I30:$BITMAP\"', "inspect_directory_chain",
        ):
            self.assertIn(required, self.cpp)

    def test_bounded_hash_and_signer_hook_present(self):
        for required in (
            "kMaximumFileBytes", "kMaximumTreeBytes", "kHashChunkBytes",
            "BCryptOpenAlgorithmProvider", "BCRYPT_SHA256_ALGORITHM",
            "BCryptHashData", "BCryptFinishHash", "ReadFile",
            "checkpoint(execution)", "now >= execution.deadline_monotonic_ms",
            "HandleBoundAuthenticodeVerifier", "verify_retained_handle",
            "signer_subject_sha256",
        ):
            self.assertIn(required, self.cpp + self.hpp)

    def test_external_model_has_no_input_or_open_surface(self):
        self.assertNotIn("model_path", self.cpp + self.hpp)
        self.assertNotIn("model_handle", self.cpp + self.hpp)
        self.assertNotIn("Qwen3.5", self.cpp + self.hpp)
        self.assertIn('equal_ordinal(path.substr(path.size() - 5), L\".gguf\"', self.cpp)

    def test_no_write_extract_network_or_process_api(self):
        source = self.cpp + self.hpp
        for forbidden in (
            "WriteFile(", "CreateProcess", "ShellExecute", "WinHttp",
            "InternetOpen", "WSAStartup", "URLDownload", "Extract", "Zip",
            "MoveFile", "DeleteFile", "SetFileInformationByHandle",
        ):
            self.assertNotIn(forbidden, source)

    def test_not_wired_into_build_host_package_or_registry(self):
        cmake = (ROOT / "native/CMakeLists.txt").read_text(encoding="utf-8")
        release_manifest = (ROOT / "release/windows/RELEASE_MANIFEST.json").read_text(encoding="utf-8")
        host_files = [
            path.read_text(encoding="utf-8")
            for path in (ROOT / "host").rglob("*.mjs")
            if path.is_file()
        ]
        combined = cmake + release_manifest + "".join(host_files)
        self.assertNotIn("windows_release_verifier", combined)
        self.assertNotIn("windows-release-verifier", combined)

    def test_contract_schemas_are_closed_and_gates_false(self):
        contract = json.loads((CONTRACT_ROOT / "v0.1.0.json").read_text(encoding="utf-8"))
        manifest_schema = json.loads((CONTRACT_ROOT / "manifest.schema.json").read_text(encoding="utf-8"))
        receipt_schema = json.loads((CONTRACT_ROOT / "receipt.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("source_only_not_activated", contract["status"])
        self.assertTrue(contract["closed_contract"])
        self.assertFalse(contract["activation_gates"]["compiled_manifest_identity_trusted"])
        self.assertFalse(contract["activation_gates"]["authenticode_policy_trusted"])
        self.assertFalse(contract["activation_gates"]["cancellable_native_io_trusted"])
        stack = [manifest_schema, receipt_schema]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("type") == "object":
                    self.assertIs(item.get("additionalProperties"), False)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        self.assertFalse(receipt_schema["properties"]["activated"]["const"])
        self.assertTrue(receipt_schema["properties"]["model_external"]["const"])

    def test_source_snapshot_matches_current_advisory_release_manifest(self):
        source = json.loads((CONTRACT_ROOT / "source-manifest.fixture.json").read_text(encoding="utf-8"))
        advisory = json.loads((ROOT / "release/windows/RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual("fixture-skeleton", source["package_kind"])
        self.assertFalse(source["activation"])
        self.assertFalse(source["model"]["included"])
        self.assertEqual(
            {entry["path"] for entry in source["entries"]},
            set(advisory["files"]),
        )
        for entry in source["entries"]:
            path = ROOT / "release/windows" / entry["path"]
            self.assertEqual(entry["size_bytes"], path.stat().st_size)
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        expected_digest = hashlib.sha256(
            (CONTRACT_ROOT / "source-manifest.fixture.json").read_bytes()
        ).hexdigest()
        self.assertIn(expected_digest, self.cpp)


if __name__ == "__main__":
    unittest.main()
