import json
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "windows-process-broker" / "v1.0.0"
FIXTURES = CONTRACT / "fixtures"
NEGATIVE = ROOT / "tests" / "native" / "fixtures" / "windows_broker"
SOURCE = ROOT / "native" / "windows_broker"


class DuplicateKey(ValueError):
    pass


def strict_load(path):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKey(key)
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs)


def validate_request(value):
    common = {"schema", "request_id", "kind"}
    if not isinstance(value, dict) or value.get("schema") != "lae.windows-broker.request.v1":
        return False
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 64:
        return False
    if not all(character.isascii() and (character.isalnum() or character in "_-") for character in request_id):
        return False
    kind = value.get("kind")
    if kind == "hello":
        return set(value) == common
    if kind == "cancel":
        target = value.get("target_request_id")
        return set(value) == common | {"target_request_id"} and isinstance(target, str) and 16 <= len(target) <= 64
    if kind != "invoke" or set(value) != common | {"action_id", "deadline_ms", "arguments"}:
        return False
    action_id = value.get("action_id")
    deadline = value.get("deadline_ms")
    arguments = value.get("arguments")
    return (
        isinstance(action_id, str)
        and 2 <= len(action_id) <= 96
        and isinstance(deadline, int)
        and not isinstance(deadline, bool)
        and 100 <= deadline <= 120000
        and isinstance(arguments, dict)
        and len(arguments) <= 16
    )


def action_accepts(manifest, request):
    actions = {item["action_id"]: item for item in manifest["actions"]}
    action = actions.get(request["action_id"])
    if not action:
        return False
    declared = {item["name"]: item for item in action["parameters"]}
    if set(request["arguments"]) - set(declared):
        return False
    for name, declaration in declared.items():
        if declaration["required"] and name not in request["arguments"]:
            return False
        if name in request["arguments"]:
            value = request["arguments"][name]
            if not isinstance(value, str) or not value or len(value.encode()) > declaration["max_bytes"]:
                return False
    return True


class WindowsProcessBrokerStaticTests(unittest.TestCase):
    def setUp(self):
        self.manifest = strict_load(FIXTURES / "fixture-manifest.json")

    def test_source_is_inactive_and_windows_tools_stay_hidden(self):
        cmake = (ROOT / "native" / "CMakeLists.txt").read_text(encoding="utf-8")
        package_manifest = (ROOT / "release" / "windows" / "RELEASE_MANIFEST.json").read_text(encoding="utf-8")
        local_index = (ROOT / "host" / "tools" / "local" / "index.mjs").read_text(encoding="utf-8")
        trust = (SOURCE / "trust_anchor.hpp").read_text(encoding="utf-8")
        self.assertNotIn("windows_broker", cmake)
        self.assertNotIn("windows-process-broker", package_manifest)
        self.assertIn('#define LAE_WINDOWS_BROKER_MANIFEST_SHA256_HEX ""', trust)
        self.assertIn("effectivePlatform !== 'win32'", local_index)
        self.assertGreaterEqual(local_index.count("unsafe_subprocess_boundary"), 5)

    def test_contract_documents_are_strict_bounded_metadata(self):
        request = strict_load(CONTRACT / "request.schema.json")
        response = strict_load(CONTRACT / "response.schema.json")
        manifest = strict_load(CONTRACT / "manifest.schema.json")
        invoke = request["$defs"]["invoke"]["allOf"][1]
        self.assertEqual(invoke["additionalProperties"], False)
        self.assertEqual(invoke["properties"]["deadline_ms"]["maximum"], 120000)
        self.assertEqual(response["additionalProperties"], False)
        receipt_fields = set(response["$defs"]["receipt"]["properties"])
        self.assertTrue(receipt_fields.isdisjoint({
            "path", "argv", "arguments", "environment", "stdout", "stderr",
            "clipboard_text", "prompt", "url", "token", "secret",
        }))
        self.assertEqual(manifest["additionalProperties"], False)
        environment = manifest["$defs"]["action"]["properties"]["environment_profile"]
        self.assertEqual(environment["const"], "systemroot-broker-temp-v1")

    def test_length_prefix_is_big_endian_and_bounded_before_allocation(self):
        payload = (FIXTURES / "valid-invoke.json").read_bytes()
        framed = struct.pack(">I", len(payload)) + payload
        self.assertEqual(struct.unpack(">I", framed[:4])[0], len(payload))
        source = (SOURCE / "protocol.cpp").read_text(encoding="utf-8")
        length_check = source.index("length == 0 || length > kMaxRequestFrameBytes")
        allocation = source.index("json.resize(length)")
        self.assertLess(length_check, allocation)

    def test_schema_rejection_and_no_generic_command_surface(self):
        self.assertTrue(validate_request(strict_load(FIXTURES / "valid-invoke.json")))
        self.assertTrue(validate_request(strict_load(FIXTURES / "valid-cancel.json")))
        with self.assertRaises(DuplicateKey):
            strict_load(NEGATIVE / "duplicate-key.json")
        for name in ("raw-command-surface.json", "timeout-too-large.json"):
            self.assertFalse(validate_request(strict_load(NEGATIVE / name)), name)
        raw = strict_load(NEGATIVE / "raw-command-surface.json")
        for forbidden in ("executable", "argv", "cwd", "environment"):
            self.assertIn(forbidden, raw)
        request_schema = (CONTRACT / "request.schema.json").read_text(encoding="utf-8")
        for forbidden in ('"executable"', '"argv"', '"cwd"', '"environment"', '"shell"'):
            self.assertNotIn(forbidden, request_schema)

    def test_manifest_digest_is_checked_before_product_policy_parse(self):
        source = (SOURCE / "win32_identity.cpp").read_text(encoding="utf-8")
        digest_check = source.index("digest != kCompiledManifestSha256")
        strict_parse = source.index("parse_strict_document(content")
        product_parse = source.index("parse_product_manifest(json")
        self.assertLess(digest_check, strict_parse)
        self.assertLess(strict_parse, product_parse)

    def test_manifest_binding_rejects_unknown_parameters_and_fixture_activation(self):
        self.assertEqual(self.manifest["mode"], "fixture")
        parser = (SOURCE / "manifest.cpp").read_text(encoding="utf-8")
        self.assertIn('value["mode"] != "product"', parser)
        valid = strict_load(FIXTURES / "valid-invoke.json")
        invalid = strict_load(NEGATIVE / "unknown-argument.json")
        self.assertTrue(action_accepts(self.manifest, valid))
        self.assertFalse(action_accepts(self.manifest, invalid))
        launch_kinds = {"process", "application", "browser", "copilot"}
        launch_actions = [item for item in self.manifest["actions"] if item["kind"] in launch_kinds]
        self.assertTrue(launch_actions)
        self.assertTrue(all(item["restricted_token_required"] is True for item in launch_actions))
        self.assertTrue(all(item["environment_profile"] == "systemroot-broker-temp-v1" for item in self.manifest["actions"]))

    def test_argv_is_manifest_rendered_and_shell_hosts_are_forbidden(self):
        action = next(item for item in self.manifest["actions"] if item["action_id"] == "process.git.status")
        self.assertEqual(action["argv_template"], [{"literal": "status"}, {"literal": "--short"}])
        manifest_source = (SOURCE / "manifest.cpp").read_text(encoding="utf-8")
        process_source = (SOURCE / "win32_process.cpp").read_text(encoding="utf-8")
        for executable in ("cmd.exe", "powershell.exe", "pwsh.exe", "rundll32.exe", "mshta.exe"):
            self.assertIn(executable, manifest_source)
        self.assertIn("render_command_line(*executable, action, request.arguments", process_source)
        self.assertIn("executable_lease.normalized_path.c_str(), command_line.data()", process_source)
        for forbidden_api in ("ShellExecute", "system(", "_popen("):
            self.assertNotIn(forbidden_api, process_source)

    def test_environment_is_rebuilt_from_three_nonsecret_entries(self):
        source = (SOURCE / "win32_process.cpp").read_text(encoding="utf-8")
        for expected in ('L"SystemRoot="', 'L"TEMP="', 'L"TMP="'):
            self.assertEqual(source.count(expected), 1)
        for ambient in ("GetEnvironmentStrings", "CreateEnvironmentBlock", "GetEnvironmentVariable"):
            self.assertNotIn(ambient, source)
        for secret_name in ("HF_TOKEN", "HUGGING_FACE", "GITHUB_TOKEN", "AZURE_TOKEN", "API_KEY"):
            self.assertNotIn(secret_name, source)
        self.assertIn("CREATE_UNICODE_ENVIRONMENT", source)
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", source)

    def test_executable_and_cwd_identity_are_held_through_suspended_assignment(self):
        mismatch = strict_load(NEGATIVE / "identity-mismatch.json")
        self.assertNotEqual(mismatch["expected"]["file_id_128"], mismatch["observed"]["file_id_128"])
        identity = (SOURCE / "win32_identity.cpp").read_text(encoding="utf-8")
        process = (SOURCE / "win32_process.cpp").read_text(encoding="utf-8")
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", identity)
        self.assertIn("constexpr DWORD kFileLeaseShare = FILE_SHARE_READ", identity)
        self.assertIn("candidate.file_id_128 != expected.file_id_128", identity)
        hashed = process.index("acquire_executable_lease", process.index("execute_bound_action"))
        created = process.index("CreateProcessAsUserW", hashed)
        checked = process.index("process_image_matches(process.get()", created)
        assigned = process.index("AssignProcessToJobObject", checked)
        resumed = process.index("ResumeThread", assigned)
        self.assertLess(hashed, created)
        self.assertLess(created, checked)
        self.assertLess(checked, assigned)
        self.assertLess(assigned, resumed)

    def test_timeout_cancel_and_success_require_full_job_reap(self):
        source = (SOURCE / "win32_process.cpp").read_text(encoding="utf-8")
        for required in (
            "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
            "JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO",
            "JobObjectBasicAccountingInformation",
            "accounting.ActiveProcesses == 0",
            "TerminateJobObject",
            "wait_for_job_zero",
            "deadline_expired",
            "cancelled",
            "job_reap_failed",
        ):
            self.assertIn(required, source)
        execute = source.index("Response execute_bound_action")
        zero_proof = source.rindex("entire_job_reaped")
        final_receipt = source.index("Receipt receipt;", zero_proof)
        self.assertGreater(zero_proof, execute)
        self.assertLess(zero_proof, final_receipt)
        broker = (SOURCE / "broker.cpp").read_text(encoding="utf-8")
        self.assertIn("kBrokerWorkerShutdownMs", broker)
        self.assertIn("TerminateProcess(GetCurrentProcess(), 70)", broker)

    def test_copilot_content_uses_stdin_not_argv_or_receipt(self):
        action = next(item for item in self.manifest["actions"] if item["kind"] == "copilot")
        prompt = next(item for item in action["parameters"] if item["name"] == "prompt")
        self.assertEqual(prompt["placement"], "stdin")
        self.assertNotIn({"parameter": "prompt"}, action["argv_template"])
        protocol = (SOURCE / "protocol.cpp").read_text(encoding="utf-8")
        start = protocol.index("nlohmann::json receipt_json")
        receipt_block = protocol[start:protocol.index("}  // namespace", start)]
        self.assertNotIn('"prompt"', receipt_block)
        self.assertNotIn('"arguments"', receipt_block)


if __name__ == "__main__":
    unittest.main()
