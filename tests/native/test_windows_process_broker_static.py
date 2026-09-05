import copy
import json
import struct
import unittest
from pathlib import PureWindowsPath, Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "windows-process-broker" / "v1.0.0"
FIXTURES = CONTRACT / "fixtures"
NEGATIVE = ROOT / "tests" / "native" / "fixtures" / "windows_broker"
SOURCE = ROOT / "native" / "windows_broker"
LAUNCH_KINDS = {"process", "application", "browser", "copilot"}
FORBIDDEN_HOSTS = {
    "bash.exe", "bitsadmin.exe", "certutil.exe", "cmd.exe", "control.exe",
    "cscript.exe", "csi.exe", "forfiles.exe", "hh.exe", "installutil.exe",
    "msbuild.exe", "mshta.exe", "node.exe", "odbcconf.exe", "pcalua.exe",
    "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "reg.exe",
    "regsvr32.exe", "rundll32.exe", "sc.exe", "schtasks.exe", "sh.exe",
    "wmic.exe", "wscript.exe", "conhost.exe", "curl.exe", "dllhost.exe",
    "dotnet.exe", "explorer.exe", "git.exe", "java.exe", "msiexec.exe",
    "regasm.exe", "regsvcs.exe", "ssh.exe", "tar.exe",
}


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


def control_free(value):
    return isinstance(value, str) and bool(value) and all(
        ord(character) >= 0x20 and ord(character) != 0x7F for character in value
    )


def identifier(value, minimum, maximum, dotted=False):
    allowed = "_-" + ("." if dotted else "")
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and all(character.isascii() and (character.isalnum() or character in allowed)
                for character in value)
    )


def validate_request(value):
    common = {"schema", "request_id", "kind"}
    if not isinstance(value, dict) or value.get("schema") != "lae.windows-broker.request.v1":
        return False
    if not identifier(value.get("request_id"), 16, 64):
        return False
    kind = value.get("kind")
    if kind == "hello":
        return set(value) == common
    if kind == "cancel":
        return set(value) == common | {"target_request_id"} and identifier(
            value.get("target_request_id"), 16, 64
        )
    if kind != "invoke" or set(value) != common | {
        "action_id", "deadline_ms", "arguments"
    }:
        return False
    deadline = value.get("deadline_ms")
    arguments = value.get("arguments")
    if not (
        identifier(value.get("action_id"), 2, 96, dotted=True)
        and isinstance(deadline, int)
        and not isinstance(deadline, bool)
        and 100 <= deadline <= 120000
        and isinstance(arguments, dict)
        and len(arguments) <= 16
    ):
        return False
    for name, argument in arguments.items():
        if not identifier(name, 1, 64, dotted=True):
            return False
        if isinstance(argument, bool):
            return False
        if isinstance(argument, int):
            if argument < 0 or argument > 0x7FFFFFFF:
                return False
        elif not control_free(argument) or len(argument.encode("utf-8")) > 65536:
            return False
    return True


def valid_https_url(value):
    forbidden = {'\\', '"', '<', '>', '{', '}', '|', '^', '`'}
    if (
        not control_free(value)
        or len(value) > 2048
        or "#" in value
        or any(character.isspace() or character in forbidden for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.hostname.lower() != "localhost"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        )
    except ValueError:
        return False


def canonical_unsigned(value):
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and value == str(int(value))
        and int(value) <= 0x7FFFFFFF
    )


def manifest_semantics(manifest, allow_disabled_fixture=True):
    try:
        executables = {item["id"]: item for item in manifest["executables"]}
        if len(executables) != len(manifest["executables"]):
            return False
        for executable in executables.values():
            if executable["class"] not in {
                "process_tool", "application", "browser", "copilot"
            }:
                return False
            if PureWindowsPath(executable["absolute_path"]).name.lower() in FORBIDDEN_HOSTS:
                return False
            if not executable["absolute_path"].lower().endswith(".exe"):
                return False

        class_for_kind = {
            "process": "process_tool",
            "application": "application",
            "browser": "browser",
            "copilot": "copilot",
        }
        seen_actions = set()
        for action in manifest["actions"]:
            if action["action_id"] in seen_actions:
                return False
            seen_actions.add(action["action_id"])
            kind = action["kind"]
            parameters = action["parameters"]
            if len({item["name"] for item in parameters}) != len(parameters):
                return False
            if any(item.get("required") is not True for item in parameters):
                return False
            for parameter in parameters:
                if not 0 < parameter["max_bytes"] <= 65536:
                    return False
                allowed = parameter.get("allowed_values", [])
                if len(set(allowed)) != len(allowed):
                    return False
                if any(
                    not control_free(item)
                    or len(item.encode("utf-8")) > parameter["max_bytes"]
                    for item in allowed
                ):
                    return False
                if parameter["kind"] == "unsigned_decimal" and any(
                    not canonical_unsigned(item) for item in allowed
                ):
                    return False
                if parameter["kind"] == "https_url" and any(
                    not valid_https_url(item) for item in allowed
                ):
                    return False

            if kind in LAUNCH_KINDS:
                required = {
                    "executable_id", "cwd_id", "argv_template_id",
                    "action_policy_id", "confinement_profile", "argv_template",
                }
                if not required <= set(action) or not action["argv_template"]:
                    return False
                executable = executables[action["executable_id"]]
                if executable["class"] != class_for_kind[kind]:
                    return False
                confinement = action["confinement_profile"]
                if confinement != "appcontainer-low-integrity-v1":
                    if not (allow_disabled_fixture and manifest["mode"] == "fixture"
                            and confinement == "unproven-disabled"):
                        return False
                used = {}
                declarations = {item["name"]: item for item in parameters}
                for part in action["argv_template"]:
                    if set(part) == {"literal"}:
                        if not control_free(part["literal"]) or len(part["literal"]) > 4096:
                            return False
                    elif set(part) == {"parameter"}:
                        name = part["parameter"]
                        if name not in declarations or declarations[name]["placement"] != "argv":
                            return False
                        used[name] = used.get(name, 0) + 1
                    else:
                        return False
                if any(
                    item["placement"] == "argv" and used.get(item["name"]) != 1
                    for item in parameters
                ):
                    return False
                if kind == "application" and parameters:
                    return False
                if kind == "browser":
                    if len(parameters) != 1:
                        return False
                    url = parameters[0]
                    if not (
                        url["name"] == "url"
                        and url["kind"] == "https_url"
                        and url["placement"] == "argv"
                        and url["max_bytes"] <= 2048
                        and action["visible"] is True
                    ):
                        return False
                if kind == "copilot":
                    if len(parameters) != 1:
                        return False
                    prompt = parameters[0]
                    if not (
                        prompt["name"] == "prompt"
                        and prompt["kind"] == "utf8"
                        and prompt["placement"] == "stdin"
                        and prompt["max_bytes"] <= 65536
                        and action["visible"] is False
                    ):
                        return False
                if kind == "process" and any(
                    item["placement"] != "argv"
                    or item["kind"] == "workspace_relative"
                    or not item.get("allowed_values")
                    for item in parameters
                ):
                    return False
            else:
                if any(field in action for field in (
                    "executable_id", "cwd_id", "argv_template_id",
                    "action_policy_id", "confinement_profile", "argv_template",
                )):
                    return False
                if kind == "clipboard_read" and not (
                    not parameters
                    and 0 < action["stdout_limit_bytes"] <= 65536
                    and action["stderr_limit_bytes"] == 0
                    and action["timeout_ms"] <= 5000
                    and action["max_processes"] == 1
                    and action["visible"] is False
                ):
                    return False
                if kind == "clipboard_write" and not (
                    len(parameters) == 1
                    and parameters[0]["kind"] == "utf8"
                    and parameters[0]["placement"] == "clipboard"
                    and parameters[0]["max_bytes"] <= 65536
                    and action["stdout_limit_bytes"] == 0
                    and action["stderr_limit_bytes"] == 0
                    and action["timeout_ms"] <= 5000
                    and action["max_processes"] == 1
                    and action["visible"] is False
                ):
                    return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def action_accepts(manifest, request):
    if not validate_request(request):
        return False
    actions = {item["action_id"]: item for item in manifest["actions"]}
    action = actions.get(request["action_id"])
    if not action:
        return False
    declared = {item["name"]: item for item in action["parameters"]}
    if set(request["arguments"]) != set(declared):
        return False
    for name, declaration in declared.items():
        value = request["arguments"][name]
        if declaration["kind"] == "unsigned_decimal":
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            rendered = str(value)
        else:
            if not isinstance(value, str):
                return False
            rendered = value
        if len(rendered.encode("utf-8")) > declaration["max_bytes"]:
            return False
        if declaration.get("allowed_values") and rendered not in declaration["allowed_values"]:
            return False
        if declaration["kind"] == "https_url" and not valid_https_url(rendered):
            return False
    return True


def apply_negative(manifest, fragment):
    candidate = copy.deepcopy(manifest)
    if "action_id" in fragment:
        action = next(item for item in candidate["actions"]
                      if item["action_id"] == fragment["action_id"])
        action.update({key: value for key, value in fragment.items() if key != "action_id"})
    elif "executable_id" in fragment:
        executable = next(item for item in candidate["executables"]
                          if item["id"] == fragment["executable_id"])
        executable.update({key: value for key, value in fragment.items()
                           if key != "executable_id"})
    return candidate


class WindowsProcessBrokerStaticTests(unittest.TestCase):
    def setUp(self):
        self.manifest = strict_load(FIXTURES / "fixture-manifest.json")

    def test_source_is_inactive_and_launch_code_is_absent(self):
        cmake = (ROOT / "native" / "CMakeLists.txt").read_text(encoding="utf-8")
        package = (ROOT / "release" / "windows" / "RELEASE_MANIFEST.json").read_text(encoding="utf-8")
        local_index = (ROOT / "host" / "tools" / "local" / "index.mjs").read_text(encoding="utf-8")
        trust = (SOURCE / "trust_anchor.hpp").read_text(encoding="utf-8")
        identity = (SOURCE / "win32_identity.cpp").read_text(encoding="utf-8")
        process = (SOURCE / "win32_process.cpp").read_text(encoding="utf-8")
        self.assertNotIn("windows_broker", cmake)
        self.assertNotIn("windows-process-broker", package)
        self.assertIn('#define LAE_WINDOWS_BROKER_MANIFEST_SHA256_HEX ""', trust)
        self.assertIn("kSupervisorContainmentProven = false", trust)
        self.assertIn("kLaunchConfinementProven = false", trust)
        self.assertIn("release_activation_prerequisites_configured()", identity)
        broker = (SOURCE / "broker.cpp").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            broker.count("release_activation_prerequisites_configured()"), 2
        )
        self.assertIn('"activation", "inactive_source"', broker)
        self.assertIn("effectivePlatform !== 'win32'", local_index)
        self.assertGreaterEqual(local_index.count("unsafe_subprocess_boundary"), 5)
        for forbidden in (
            "CreateProcess", "ResumeThread", "AssignProcessToJobObject",
            "CreateRestrictedToken", "DISABLE_MAX_PRIVILEGE", "TerminateJobObject",
        ):
            self.assertNotIn(forbidden, process)
        self.assertIn("launch_containment_unproven", process)

    def test_windows_header_macros_precede_direct_windows_includes(self):
        for path in SOURCE.glob("*.*"):
            text = path.read_text(encoding="utf-8")
            if "#include <windows.h>" not in text:
                continue
            before = text[:text.index("#include <windows.h>")]
            self.assertIn("#define NOMINMAX", before, path.name)
            self.assertIn("#define WIN32_LEAN_AND_MEAN", before, path.name)

    def test_contract_is_bounded_and_receipt_never_claims_process(self):
        request = strict_load(CONTRACT / "request.schema.json")
        response = strict_load(CONTRACT / "response.schema.json")
        manifest = strict_load(CONTRACT / "manifest.schema.json")
        invoke = request["$defs"]["invoke"]["allOf"][1]
        self.assertEqual(invoke["additionalProperties"], False)
        self.assertEqual(invoke["properties"]["deadline_ms"]["maximum"], 120000)
        self.assertEqual(response["additionalProperties"], False)
        receipt = response["$defs"]["receipt"]
        self.assertEqual(receipt["properties"]["process_created"]["const"], False)
        self.assertTrue(set(receipt["properties"]).isdisjoint({
            "path", "argv", "arguments", "environment", "stdout", "stderr",
            "clipboard_text", "prompt", "url", "token", "secret",
            "restricted_token", "confinement",
        }))
        self.assertEqual(manifest["additionalProperties"], False)
        self.assertTrue(manifest_semantics(self.manifest))
        product = copy.deepcopy(self.manifest)
        product["mode"] = "product"
        self.assertFalse(manifest_semantics(product, allow_disabled_fixture=False))

    def test_frame_read_write_and_thread_join_paths_are_bounded(self):
        payload = (FIXTURES / "valid-invoke.json").read_bytes()
        framed = struct.pack(">I", len(payload)) + payload
        self.assertEqual(struct.unpack(">I", framed[:4])[0], len(payload))
        protocol = (SOURCE / "protocol.cpp").read_text(encoding="utf-8")
        check = protocol.index("length == 0 || length > kMaxRequestFrameBytes")
        allocation = protocol.index("json.resize(length)")
        self.assertLess(check, allocation)
        for required in (
            "read_some_bounded", "kFrameAssemblyDeadlineMs", "CreateThread",
            "kResponseWriteDeadlineMs", "CancelSynchronousIo",
            "kIoCancellationGraceMs", "fail_stop_on_stuck_io",
        ):
            self.assertIn(required, protocol)
        self.assertEqual(protocol.count("ReadFile("), 1)
        self.assertEqual(protocol.count("WriteFile("), 1)
        broker = (SOURCE / "broker.cpp").read_text(encoding="utf-8")
        self.assertNotIn("INFINITE", protocol)
        self.assertNotIn("INFINITE", broker)
        self.assertIn("bounded_join", broker)
        self.assertIn("if (!bounded_join(active->worker", broker)
        self.assertLess(broker.index("WaitForSingleObject(thread, timeout_ms)"),
                        broker.index("worker.join()"))
        self.assertIn("fail_stop_on_stuck_worker", broker)

    def test_request_and_manifest_negative_fixtures_fail(self):
        self.assertTrue(validate_request(strict_load(FIXTURES / "valid-invoke.json")))
        self.assertTrue(action_accepts(self.manifest, strict_load(FIXTURES / "valid-invoke.json")))
        self.assertTrue(validate_request(strict_load(FIXTURES / "valid-cancel.json")))
        with self.assertRaises(DuplicateKey):
            strict_load(NEGATIVE / "duplicate-key.json")
        for name in (
            "raw-command-surface.json", "timeout-too-large.json", "argument-control.json"
        ):
            self.assertFalse(validate_request(strict_load(NEGATIVE / name)), name)
        self.assertFalse(action_accepts(self.manifest, strict_load(NEGATIVE / "unknown-argument.json")))
        self.assertFalse(action_accepts(self.manifest, strict_load(NEGATIVE / "browser-non-https.json")))
        self.assertFalse(action_accepts(self.manifest, strict_load(NEGATIVE / "browser-localhost.json")))
        for name in (
            "application-user-argv.json", "class-mismatch.json", "clipboard-over-limit.json",
            "control-allowed-value.json", "control-literal.json", "copilot-prompt-argv.json",
            "generic-host.json", "ignored-parameter.json", "numeric-allowed-invalid.json",
            "non-exe-host.json", "process-unconstrained-argv.json",
        ):
            fragment = strict_load(NEGATIVE / name)
            self.assertFalse(manifest_semantics(apply_negative(self.manifest, fragment)), name)

    def test_cpp_manifest_parser_carries_the_same_refusal_gates(self):
        parser = (SOURCE / "manifest.cpp").read_text(encoding="utf-8")
        for required in (
            "explicit_exe_path(path)", "forbidden_executable_host(",
            "!parameter.required", "used_parameters[parameter.name] != 1",
            'output.confinement_profile != "appcontainer-low-integrity-v1"',
            "executable_class == ExecutableClass::kProcessTool",
            "parameter.placement != ParameterPlacement::kArgv",
            "parameter.allowed_values.empty()", "valid_unsigned_decimal",
            "valid_https_url", "output.timeout_ms > 5000",
        ):
            self.assertIn(required, parser)
        for host in FORBIDDEN_HOSTS:
            self.assertIn(f'L"{host}"', parser)
        protocol = (SOURCE / "protocol.cpp").read_text(encoding="utf-8")
        self.assertIn("value.is_number_unsigned()", protocol)
        self.assertIn("c < 0x20 || c == 0x7f", protocol)

    def test_replay_cache_is_bounded_and_checked_before_dispatch(self):
        replay = strict_load(NEGATIVE / "replayed-request-id.json")
        self.assertEqual(replay[0]["request_id"], replay[1]["request_id"])
        broker = (SOURCE / "broker.cpp").read_text(encoding="utf-8")
        self.assertIn("kReplayCacheEntries = 1024", broker)
        self.assertIn("recent_request_id_set_", broker)
        remember = broker.index("remember_request_id(request.request_id)")
        dispatch = broker.index("request.kind == RequestKind::kHello", remember)
        self.assertLess(remember, dispatch)
        self.assertIn('"request_id_replayed"', broker[remember:dispatch])

    def test_manifest_identity_precedes_product_parse(self):
        source = (SOURCE / "win32_identity.cpp").read_text(encoding="utf-8")
        digest = source.index("digest != kCompiledManifestSha256")
        strict_parse = source.index("parse_strict_document(content")
        product_parse = source.index("parse_product_manifest(json")
        self.assertLess(digest, strict_parse)
        self.assertLess(strict_parse, product_parse)
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", source)
        self.assertIn("kManifestReadDeadlineMs", source)

    def test_clipboard_is_manifest_bounded_and_rechecks_stop_state(self):
        source = (SOURCE / "win32_process.cpp").read_text(encoding="utf-8")
        self.assertIn("action.stdout_limit_bytes > 64 * 1024", source)
        self.assertIn("input.size() > parameter->max_bytes", source)
        self.assertIn("MB_ERR_INVALID_CHARS", source)
        self.assertIn("WC_ERR_INVALID_CHARS", source)
        self.assertIn("if (units == 0) return true", source)
        self.assertGreaterEqual(source.count("stop_code(cancelled, deadline)"), 7)
        last_check = source.index("This is the last cancellation/deadline check")
        mutation = source.index("EmptyClipboard()", last_check)
        self.assertLess(last_check, mutation)

    def test_copilot_content_is_stdin_only_and_receipt_is_metadata_only(self):
        action = next(item for item in self.manifest["actions"] if item["kind"] == "copilot")
        prompt = next(item for item in action["parameters"] if item["name"] == "prompt")
        self.assertEqual(prompt["placement"], "stdin")
        self.assertNotIn({"parameter": "prompt"}, action["argv_template"])
        protocol = (SOURCE / "protocol.cpp").read_text(encoding="utf-8")
        start = protocol.index("nlohmann::json receipt_json")
        receipt_block = protocol[start:protocol.index("}  // namespace", start)]
        self.assertNotIn('"prompt"', receipt_block)
        self.assertNotIn('"arguments"', receipt_block)
        self.assertNotIn('"clipboard_text"', receipt_block)

    def test_docs_do_not_overclaim_activation_or_runtime_evidence(self):
        docs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SOURCE / "README.md", CONTRACT / "README.md")
        )
        for phrase in (
            "NOT_READY", "no process-creation implementation",
            "durable host action journal", "static", "Windows runtime evidence",
        ):
            self.assertIn(phrase, docs)


if __name__ == "__main__":
    unittest.main()
