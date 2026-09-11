"""Static/model checks for the dormant Windows process authority contract.

This test never compiles or loads the Windows sources and never launches a
process.  The model is deliberately a refusal oracle: source and target
evidence are required before any authority can be admitted.

Several checks are mutation-style: they are anchored to a named C++ function
body rather than to a loose substring, so deleting a guard, a conjunct, or a
refusal branch from `native/windows_supervisor/launch_authority.hpp` fails
this suite instead of passing silently.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/windows-process-authority/v1.0.0.json"
CONTRACT_DOC = ROOT / "contracts/windows-process-authority/v1.0.0.md"
HEADER = ROOT / "native/windows_supervisor/launch_authority.hpp"
TRANSACTION = ROOT / "native/windows_supervisor/process_transaction.inc"
AUTHORITY = ROOT / "native/windows_supervisor/authority.hpp"
README = ROOT / "native/windows_supervisor/README.md"
BROKER = ROOT / "native/windows_broker/win32_process.cpp"
TRUST_ANCHOR = ROOT / "native/windows_broker/trust_anchor.hpp"


def strict_json(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def braced_body(text: str, anchor: str) -> str:
    """Return the brace-balanced body that follows ``anchor``.

    ``anchor`` must occur exactly once so the extraction cannot silently bind
    to an overload with the same leading signature line.
    """
    if text.count(anchor) != 1:
        raise ValueError("anchor is not unique: " + anchor)
    start = text.index(anchor)
    opening = text.index("{", start)
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
    raise ValueError("unbalanced braces after " + anchor)


# ---------------------------------------------------------------------------
# Models.  Each mirrors a specific C++ construct in launch_authority.hpp and is
# paired with a source-anchored assertion so the two cannot drift apart.
# ---------------------------------------------------------------------------


class RefusalModel:
    """Small adversarial model for the pre-child admission boundary."""

    REQUIRED = (
        "executable_handle", "working_directory_handle", "containment",
        "environment", "arguments", "cancellation", "issuer",
    )

    def __init__(self):
        self.create_calls = 0
        self.mutation_calls = 0

    def admit(self, proof: dict, gates: bool = False) -> str:
        if not gates or any(not proof.get(key, False) for key in self.REQUIRED):
            return "unavailable"
        self.create_calls += 1
        self.mutation_calls += 1
        return "ok"


class CancellationModel:
    """Mirror of `CancellationState` in launch_authority.hpp.

    Generations are never caller supplied: ``start`` derives the run generation
    from the authority's issued generation anchor and returns it, or ``0`` for a
    typed refusal.  ``unknown_manual`` is terminal, ``start`` is refused from
    ``running``/``cancel_requested``/``unknown_manual``, and every other
    transition requires the exact active generation.  Refusals never mutate a
    state the transition did not legally own.
    """

    MAX_RUNS_PER_AUTHORITY = 8
    RESTART_REFUSING_STATES = ("running", "cancel_requested", "unknown_manual")

    def __init__(self, authority_generation: int = 1):
        self.authority_generation = authority_generation
        self.active = 0
        self.run_index = 0
        self.state = "idle"
        self.retry_calls = 0

    def start(self) -> int:
        if self.authority_generation == 0:
            return 0
        if self.state in self.RESTART_REFUSING_STATES:
            return 0
        if self.run_index >= self.MAX_RUNS_PER_AUTHORITY:
            return 0
        following = self.authority_generation + self.run_index
        if following <= self.active:
            return 0
        self.run_index += 1
        self.active = following
        self.state = "running"
        return self.active

    def cancel(self, generation: int) -> bool:
        if generation == 0 or generation != self.active:
            return False
        if self.state != "running":
            return False
        self.state = "cancel_requested"
        return True

    def complete(self, generation: int) -> bool:
        if generation == 0 or generation != self.active:
            return False
        if self.state == "cancel_requested":
            # A completion racing an in-flight cancel is ambiguous, never a
            # success.  It is the contract's unknown_manual outcome.
            self.state = "unknown_manual"
            return False
        if self.state != "running":
            return False
        self.state = "complete"
        return True

    def finish_cancel_join(self, generation: int, active_process_zero: bool) -> bool:
        if generation == 0 or generation != self.active:
            return False
        if self.state != "cancel_requested":
            return False
        # Both outcomes are terminal.  A timed-out bounded join does not fall
        # back to cancel_requested.
        self.state = "unknown_manual"
        return bool(active_process_zero)

    def orphan(self, generation: int) -> bool:
        if generation == 0 or generation != self.active:
            return False
        if self.state not in ("running", "cancel_requested"):
            return False
        self.state = "unknown_manual"
        return True


class UniqueHandleModel:
    """Mirror of `UniqueHandle`: one close path, self-move-assignment safe."""

    def __init__(self, value=None, ledger=None):
        self.ledger = {} if ledger is None else ledger
        self.value = value

    def release(self):
        value, self.value = self.value, None
        return value

    def reset(self, value=None):
        if self.value is not None:
            self.ledger[self.value] = self.ledger.get(self.value, 0) + 1
        self.value = value

    def move_construct(self):
        return UniqueHandleModel(self.release(), self.ledger)

    def move_assign(self, other):
        if self is not other:
            self.reset(other.release())
        return self

    def destroy(self):
        self.reset()


def reserved_device_name_model(component: str) -> bool:
    """Mirror of `LaunchAuthority::reserved_device_name` (ASCII only)."""
    stem = component.split(".", 1)[0]
    if len(stem) not in (3, 4):
        return False
    upper = stem.upper()
    if len(stem) == 3:
        return upper in ("CON", "PRN", "AUX", "NUL")
    return upper[:3] in ("COM", "LPT") and "1" <= upper[3] <= "9"


def canonical_absolute_model(path: str) -> bool:
    """Mirror of `LaunchAuthority::canonical_absolute` (ASCII inputs)."""
    if len(path) < 8 or len(path) > 32767:
        return False
    if path[:4] != "\\\\?\\":
        return False
    drive = path[4]
    if not (("A" <= drive <= "Z") or ("a" <= drive <= "z")):
        return False
    if path[5] != ":" or path[6] != "\\":
        return False
    for index in range(4, len(path)):
        character = path[index]
        if ord(character) < 0x20 or character in "\x7f<>|\"*?/":
            return False
        if character == ":" and index != 5:
            return False
    start = 7
    while start < len(path):
        stop = path.find("\\", start)
        if stop < 0:
            stop = len(path)
        if stop == start:
            return False
        component = path[start:stop]
        if component in (".", ".."):
            return False
        if component[-1] in (".", " ") or component[0] == " ":
            return False
        if "~" in component:
            return False
        if reserved_device_name_model(component):
            return False
        if stop == len(path):
            return True
        start = stop + 1
    return False


def quote_argument(argument: str) -> str:
    """The one fixed escaping algorithm: CommandLineToArgvW / MSVCRT rules."""
    result = ['"']
    backslashes = 0
    for character in argument:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
        else:
            result.append("\\" * backslashes)
            result.append(character)
        backslashes = 0
    result.append("\\" * (backslashes * 2))
    result.append('"')
    return "".join(result)


def build_command_line(arguments) -> str:
    return " ".join(quote_argument(argument) for argument in arguments)


def parse_command_line(command_line: str):
    """CommandLineToArgvW parsing rules for the post-image portion."""
    arguments, current = [], []
    in_quotes = False
    started = False
    index = 0
    length = len(command_line)
    while index < length:
        character = command_line[index]
        if character == "\\":
            slashes = 0
            while index < length and command_line[index] == "\\":
                slashes += 1
                index += 1
            if index < length and command_line[index] == '"':
                current.append("\\" * (slashes // 2))
                if slashes % 2 == 1:
                    current.append('"')
                else:
                    in_quotes = not in_quotes
                index += 1
            else:
                current.append("\\" * slashes)
            started = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            started = True
            index += 1
            continue
        if character in (" ", "\t") and not in_quotes:
            if started:
                arguments.append("".join(current))
                current, started = [], False
            index += 1
            continue
        current.append(character)
        started = True
        index += 1
    if started:
        arguments.append("".join(current))
    return arguments


def bounded_arguments_model(arguments, total_utf16_units: int) -> bool:
    """Mirror of `LaunchAuthority::bounded_arguments`."""
    if len(arguments) > 64:
        return False
    total = 0
    for argument in arguments:
        if len(argument) > 8192:
            return False
        for character in argument:
            if ord(character) < 0x20 or ord(character) == 0x7f:
                return False
        total += len(argument) + 3
    if total > 30000:
        return False
    return total_utf16_units == total


class WindowsProcessAuthorityStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = strict_json(CONTRACT)
        cls.contract_doc = CONTRACT_DOC.read_text(encoding="utf-8")
        cls.header = HEADER.read_text(encoding="utf-8")
        cls.transaction = TRANSACTION.read_text(encoding="utf-8")
        cls.authority = AUTHORITY.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.broker = BROKER.read_text(encoding="utf-8")
        cls.launch_class = braced_body(cls.header, "class LaunchAuthority final")
        cls.cancellation_class = braced_body(cls.header, "class CancellationState final")
        cls.unique_handle_class = braced_body(cls.header, "class UniqueHandle final")
        cls.validator = braced_body(
            cls.launch_class, "bool valid_for_admission() const noexcept")

    # -- contract shape ----------------------------------------------------

    def test_contract_is_exactly_deny_closed(self):
        contract = self.contract
        self.assertEqual(contract["schema"],
                         "lae.windows-process-authority.contract.v1.0.0")
        self.assertEqual(contract["status"], "SOURCE_ONLY_NOT_READY")
        self.assertTrue(all(value is False for value in contract["availability"].values()))
        activation = contract["activation"]
        self.assertTrue(all(value is False for key, value in activation.items()
                            if key != "activation_requires_all" and
                            key != "activation_refusal_is_sticky"))
        self.assertTrue(activation["activation_requires_all"])
        self.assertTrue(activation["activation_refusal_is_sticky"])
        self.assertFalse(activation["nested_job_policy"])
        authority = contract["authority_object"]
        self.assertEqual(authority["type"], "private noncopyable RAII LaunchAuthority")
        self.assertEqual(authority["issuer"], "private LaunchAuthorityIssuer only")
        self.assertEqual(authority["handle_owner"],
                         "UniqueHandle closes each owning HANDLE exactly once")
        self.assertTrue(authority["move_only"])
        self.assertFalse(authority["public_raw_handle_accessor"])
        self.assertEqual(authority["operation_identity"],
                         ["operation_id_128", "generation_uint64", "nonce_128"])

    def test_identity_contract_requires_handles_and_rechecks(self):
        identity = self.contract["identity_pinned_handles"]
        executable = identity["executable"]
        working = identity["working_directory"]
        for field in (
            "supervisor_owned_executable_handle",
            "supervisor_owned_containing_directory_handle",
            "absolute_manifest_path_binding", "size_bytes", "volume_serial",
            "file_id_128", "sha256", "read_only_open",
            "identity_rechecked_after_open",
        ):
            self.assertIn(field, executable["required"])
        for field in (
            "supervisor_owned_directory_handle", "absolute_manifest_path_binding",
            "volume_serial", "directory_id_128", "no_reparse_components",
            "identity_rechecked_after_open",
        ):
            self.assertIn(field, working["required"])
        self.assertFalse(executable["request_path_or_reopen_allowed"])
        self.assertFalse(working["request_path_or_reopen_allowed"])
        self.assertFalse(identity["raw_paths_or_handles_in_receipt"])

    def test_containment_environment_and_orphan_contracts_are_complete(self):
        containment = self.contract["pre_child_containment"]
        for field in (
            "job_created_before_child", "kill_on_job_close",
            "active_process_zero_on_terminal", "no_ambient_handle_inheritance",
            "least_privilege_token",
        ):
            self.assertIn(field, containment["required"])
        self.assertFalse(containment["create_then_attach_gap_allowed"])
        self.assertFalse(containment["child_creation_without_proof"])

        environment = self.contract["environment"]
        self.assertFalse(environment["caller_environment_or_map_allowed"])
        self.assertFalse(environment["credential_categories_inherited"])
        self.assertFalse(environment["ambient_environment_inherited"])
        for category in ("tokens", "keys", "SSH_AUTH_SOCK", "proxy", "user_config"):
            self.assertIn(category, environment["credential_categories"])
        for field in ("exact_allowlist", "credentials_excluded", "proxy_excluded",
                      "user_config_excluded"):
            self.assertIn(field, environment["proof_fields"])

        orphan = self.contract["cancellation_and_orphan"]
        for field in (
            "supervisor_owned_cancellation_event", "bounded_cancel_join",
            "kill_entire_contained_tree", "active_process_zero_before_release",
            "supervisor_death_closes_containment",
        ):
            self.assertIn(field, orphan["required"])
        self.assertFalse(orphan["late_completion_may_mutate_new_run"])
        self.assertFalse(orphan["automatic_retry_after_ambiguous_outcome"])
        self.assertEqual(orphan["ambiguous_outcome"], "unknown_manual")
        self.assertFalse(orphan["generation_caller_supplied"])
        self.assertTrue(orphan["generation_derived_from_authority_generation"])
        self.assertTrue(orphan["generation_strictly_monotonic"])
        self.assertTrue(orphan["unknown_manual_is_terminal"])
        self.assertTrue(orphan["restart_after_orphan_requires_new_authority"])
        self.assertEqual(orphan["bounded_cancel_join_timeout_outcome"],
                         "unknown_manual")
        self.assertEqual(orphan["complete_on_never_started_authority"],
                         "typed_refusal_without_state_change")

    def test_contract_binds_an_argument_array_and_one_escaping_algorithm(self):
        arguments = self.contract["arguments"]
        self.assertFalse(arguments["argv_zero_caller_supplied"])
        self.assertFalse(arguments["shell_or_concatenated_command_line_allowed"])
        self.assertFalse(arguments["caller_supplied_command_line_allowed"])
        self.assertTrue(arguments["round_trip_parse_equality_required"])
        self.assertTrue(arguments["argument_digest_required"])
        self.assertIn("CommandLineToArgvW", arguments["command_line_escaping"])
        self.assertEqual(arguments["max_arguments"], 64)
        self.assertEqual(arguments["max_argument_utf16_units"], 8192)
        self.assertEqual(arguments["max_command_line_utf16_units"], 30000)
        for field in ("argv_array_only", "command_line_escaping_fixed",
                      "command_line_round_trip_verified",
                      "no_shell_or_concatenated_command_line"):
            self.assertIn(field, arguments["required"])
        for field in ("utf16_safe_arguments", "argument_count_and_length_bounded"):
            self.assertIn(field, arguments["proof_fields"])
        # The prose contract must document every predicate the JSON requires.
        for field in arguments["required"] + arguments["proof_fields"]:
            self.assertIn(field, self.contract_doc, field)

    def test_contract_forbids_raw_receipts_and_ambient_or_fallback_execution(self):
        forbidden = self.contract["forbidden"]
        for field in (
            "caller_supplied_executable_or_cwd_path",
            "caller_supplied_environment", "ambient_credential_inheritance",
            "CreateProcess_before_containment_proof", "shell_or_fallback_execution",
            "caller_supplied_command_line_or_shell_string",
            "caller_supplied_cancellation_generation",
            "raw_path_handle_or_environment_receipt",
            "automatic_retry_or_completion_after_ambiguous_outcome",
        ):
            self.assertIn(field, forbidden)
        transaction = self.contract["transaction"]
        self.assertTrue(transaction["one_use_move_only"])
        self.assertTrue(transaction["complete_binding_required"])
        self.assertTrue(transaction["pre_mutation_refusal"])
        self.assertFalse(transaction["journal_write_or_external_launch_in_this_revision"])

    # -- contract-to-header binding (the check that F-1 was missing) --------

    def contract_predicate_names(self):
        identity = self.contract["identity_pinned_handles"]
        names = []
        names += identity["executable"]["required"]
        names += identity["working_directory"]["required"]
        names += self.contract["pre_child_containment"]["required"]
        names += self.contract["environment"]["proof_fields"]
        names += self.contract["cancellation_and_orphan"]["required"]
        names += self.contract["arguments"]["required"]
        names += self.contract["arguments"]["proof_fields"]
        return sorted(set(names))

    def test_every_contract_predicate_is_a_header_field_and_a_validator_conjunct(self):
        """A predicate silently deleted from the header or the validator fails.

        The working directory's `directory_id_128` is the single documented
        alias: it is carried by the same `FileIdentity::file_id_128` member as
        the executable's file id.
        """
        aliases = {"directory_id_128": "file_id_128"}
        minted = braced_body(self.launch_class, "struct MintedParts final")
        file_identity = braced_body(self.launch_class, "struct FileIdentity final")
        handle_identity = braced_body(self.launch_class, "struct HandleIdentity final")
        argument_vector = braced_body(self.launch_class, "struct ArgumentVector final")
        names = self.contract_predicate_names()
        self.assertGreaterEqual(len(names), 26)
        for contract_name in names:
            member = aliases.get(contract_name, contract_name)
            declaration = re.compile(r"\b" + re.escape(member) + r"\s*(?:=|\{\}|;)")
            in_minted = declaration.search(minted) is not None
            in_nested = any(declaration.search(text) is not None for text in
                            (file_identity, handle_identity, argument_vector))
            self.assertTrue(in_minted or in_nested,
                            "no field declaration for " + contract_name)
            # A MintedParts member is copied into a trailing-underscore data
            # member; a nested-struct member is reached through its owner.
            if in_minted:
                conjunct = re.compile(r"\b" + re.escape(member) + r"_\b")
            else:
                conjunct = re.compile(r"\." + re.escape(member) + r"\b")
            self.assertIsNotNone(conjunct.search(self.validator),
                                 "no validator conjunct for " + contract_name)

    def test_every_minted_boolean_predicate_is_a_validator_conjunct(self):
        minted = braced_body(self.launch_class, "struct MintedParts final")
        booleans = re.findall(r"^\s*bool (\w+) = false;", minted, re.M)
        self.assertGreaterEqual(len(booleans), 24)
        for name in booleans:
            self.assertRegex(self.validator, r"\b" + re.escape(name) + r"_\b", name)

    def test_validator_is_gate_anchored_and_reparse_and_manifest_bound(self):
        self.assertTrue(self.validator.lstrip().startswith(
            "return kLaunchAuthorityAvailable &&"))
        for conjunct in (
            "executable_file_.absolute_manifest_path_binding",
            "working_directory_file_.absolute_manifest_path_binding",
            "executable_file_.no_reparse_components",
            "working_directory_file_.no_reparse_components",
            "job_created_before_child_",
            "credentials_excluded_", "proxy_excluded_", "user_config_excluded_",
            "canonical_absolute(executable_file_.canonical_absolute_path)",
            "canonical_absolute(working_directory_file_.canonical_absolute_path)",
            "bounded_arguments(arguments_)",
            "cancellation_state_ != nullptr",
        ):
            self.assertIn(conjunct, self.validator, conjunct)

    # -- header shape ------------------------------------------------------

    def test_header_has_nonserializable_proof_shape_and_sticky_false_gate(self):
        for token in (
            "class UniqueHandle final", "UniqueHandle(const UniqueHandle&) = delete",
            "::CloseHandle(value_)", "class CancellationState final",
            "std::uint64_t active_generation_", "kCancelRequested",
            "kUnknownManual", "class LaunchAuthority final",
            "LaunchAuthority(const LaunchAuthority&) = delete",
            "class LaunchAuthorityIssuer final", "LaunchAuthorityIssuer() = delete",
            "struct MintedParts final", "explicit LaunchAuthority(MintedParts&& parts)",
            "std::optional<LaunchAuthority> issue", "return std::nullopt",
            "std::wstring canonical_absolute_path",
            "operation_id_", "generation_", "nonce_", "volume_serial",
            "file_id_128", "sha256", "token_", "job_", "cancellation_event_",
            "environment_digest_", "kMinimalEnvironmentAllowlist",
            "kLaunchAuthorityAvailable = false", "valid_for_admission",
            "validate_for_admission", "mark_orphaned",
            "friend class LaunchAuthorityIssuer", "CloseHandle(value_)",
        ):
            self.assertIn(token, self.header)
        self.assertIn("#error", self.header)
        self.assertIn("private:", self.header)
        self.assertNotIn("HANDLE get(", self.header)
        self.assertNotIn("operator HANDLE", self.header)
        for forbidden in ("CreateProcess", "ShellExecute", "WinExec", "system(",
                          "popen(", "TerminateProcess", "AssignProcessToJobObject"):
            self.assertNotIn(forbidden, self.header, forbidden)

    def test_no_member_of_the_authority_returns_or_holds_a_raw_handle(self):
        """Stronger than banning two accessor spellings: the class body has no

        `HANDLE` token at all, so `native_handle()`, a differently named getter,
        or a raw handle data member all fail.
        """
        self.assertIsNone(re.search(r"\bHANDLE\b", self.launch_class))
        self.assertIsNone(re.search(r"\bHANDLE\b", self.cancellation_class))
        public_section = self.unique_handle_class.split("private:", 1)[0]
        self.assertIsNone(re.search(r"\bHANDLE\b", public_section))
        # The only HANDLE-returning member anywhere in the header is the
        # private UniqueHandle::release().
        returning = re.findall(r"^\s*(?:static\s+)?HANDLE\s+(\w+)\s*\(", self.header, re.M)
        self.assertEqual(returning, ["release"])
        private_section = self.unique_handle_class.split("private:", 1)[1]
        self.assertIn("HANDLE release() noexcept", private_section)

    def test_unique_handle_closes_once_and_survives_self_move_assignment(self):
        self.assertEqual(self.header.count("::CloseHandle("), 1)
        assignment = braced_body(
            self.unique_handle_class,
            "UniqueHandle& operator=(UniqueHandle&& other) noexcept")
        self.assertIn("if (this != &other) reset(other.release());", assignment)
        self.assertIn("UniqueHandle(UniqueHandle&& other) noexcept : value_(other.release())",
                      self.header)

        ledger = {}
        first = UniqueHandleModel("h1", ledger)
        moved = first.move_construct()
        first.destroy()
        moved.destroy()
        self.assertEqual(ledger, {"h1": 1})

        ledger = {}
        left = UniqueHandleModel("h1", ledger)
        right = UniqueHandleModel("h2", ledger)
        left.move_assign(right)
        left.destroy()
        right.destroy()
        self.assertEqual(ledger, {"h1": 1, "h2": 1})

        ledger = {}
        alone = UniqueHandleModel("h1", ledger)
        alone.move_assign(alone)
        self.assertEqual(alone.value, "h1")
        self.assertEqual(ledger, {})
        alone.destroy()
        self.assertEqual(ledger, {"h1": 1})

    def test_minted_parts_and_authority_are_unconstructible_outside_the_issuer(self):
        private_index = self.launch_class.index("\n private:")
        for declaration in ("struct MintedParts final",
                            "struct FileIdentity final",
                            "struct HandleIdentity final",
                            "struct ArgumentVector final",
                            "explicit LaunchAuthority(MintedParts&& parts)"):
            self.assertGreater(self.launch_class.index(declaration), private_index,
                               declaration)
        minted = braced_body(self.launch_class, "struct MintedParts final")
        self.assertIsNone(re.search(r"\bMintedParts\s*\(", minted))
        self.assertIsNone(re.search(r"\bpublic:", minted))
        self.assertIsNone(re.search(r"\bprotected:", self.launch_class))
        # A user-declared constructor exists and is private, so the authority
        # is not an aggregate and cannot be brace-initialised by a caller.
        self.assertIn("LaunchAuthority(const LaunchAuthority&) = delete", self.launch_class)
        self.assertIn("LaunchAuthority& operator=(const LaunchAuthority&) = delete",
                      self.launch_class)
        self.assertIsNone(re.search(r"^\s*LaunchAuthority\(\)\s*(=|\{)", self.launch_class, re.M))
        member = re.compile(r"^  [\w:][\w:<>,* &]*\b\w+_\s*(?:;|=|\{\})", re.M)
        public_section, private_section = self.launch_class.split("\n private:", 1)
        self.assertEqual(member.findall(public_section), [])
        self.assertGreaterEqual(len(member.findall(private_section)), 30)
        issuer = braced_body(self.header, "class LaunchAuthorityIssuer final")
        self.assertTrue(issuer.lstrip().startswith("private:"))

    def test_every_friend_name_resolves_to_a_type_declared_in_this_header(self):
        """F-6: no friendship may name an undefined, unclaimed outside type."""
        friends = sorted(set(re.findall(
            r"friend\s+(?:class|struct)\s+(\w+)\s*;", self.header)))
        self.assertEqual(friends, ["LaunchAuthority", "LaunchAuthorityIssuer"])
        for name in friends:
            self.assertIn("class " + name + " final {", self.header)
        # The previously befriended namespace-scope supervisor-state name is
        # neither declared nor befriended here any more.
        self.assertIsNone(re.search(
            r"^\s*(?:struct|class)\s+SupervisorState\s*;", self.header, re.M))
        self.assertIsNone(re.search(
            r"friend\s+(?:struct|class)\s+SupervisorState\s*;", self.header))
        # The enforcement invariant is stated where it actually lives.
        self.assertIn("UniqueHandle(HANDLE value) noexcept", self.header)

    # -- cancellation state machine: source-anchored --------------------------

    def test_cancellation_state_machine_is_source_connected_and_fail_closed(self):
        for token in (
            "cancellation_state_", "cancellation_state_->begin()",
            "cancellation_state_->request_cancel", "cancellation_state_->complete",
            "cancellation_state_->finish_bounded_cancel_join",
            "cancellation_state_->orphan", "finish_bounded_cancel_join",
            "std::lock_guard<std::mutex>",
            "generation != active_generation_", "state_ = RunState::kUnknownManual",
            "kill_on_job_close_", "active_process_zero_on_terminal_",
        ):
            self.assertIn(token, self.header)
        self.assertNotIn("TerminateProcess", self.header)

    def test_source_begin_refuses_running_cancel_requested_and_unknown_manual(self):
        """Mutation check: deleting the restart fence fails here."""
        body = braced_body(self.cancellation_class, "std::uint64_t begin() noexcept")
        self.assertRegex(
            body,
            r"if\s*\(state_ == RunState::kRunning \|\|\s*"
            r"state_ == RunState::kCancelRequested \|\|\s*"
            r"state_ == RunState::kUnknownManual\)\s*return 0;")
        # The generation is derived, never supplied, and strictly increasing.
        self.assertIn("const std::uint64_t next = authority_generation_ + run_index_;", body)
        self.assertIn("if (next <= active_generation_) return 0;", body)
        self.assertIn("if (run_index_ >= kMaxRunsPerAuthority) return 0;", body)
        self.assertIn("if (authority_generation_ == 0) return 0;", body)
        self.assertIsNone(re.search(r"\bbegin\s*\(\s*std::uint64_t", self.header))
        self.assertIn("std::uint64_t begin_run() noexcept", self.header)

    def test_source_orphan_is_fenced_by_generation_and_state(self):
        body = braced_body(self.cancellation_class,
                           "bool orphan(std::uint64_t generation) noexcept")
        self.assertIn("if (generation == 0 || generation != active_generation_) return false;",
                      body)
        self.assertRegex(
            body,
            r"if\s*\(state_ != RunState::kRunning && state_ != RunState::kCancelRequested\)\s*"
            r"return false;")
        self.assertIn("bool mark_orphaned(std::uint64_t generation) noexcept", self.header)
        self.assertIsNone(re.search(r"void\s+mark_orphaned\s*\(\s*\)", self.header))

    def test_source_complete_never_poisons_a_never_started_authority(self):
        body = braced_body(self.cancellation_class,
                           "bool complete(std::uint64_t generation) noexcept")
        guard = body.index(
            "if (generation == 0 || generation != active_generation_) return false;")
        cancel_branch = body.index("if (state_ == RunState::kCancelRequested) {")
        poison = body.index("state_ = RunState::kUnknownManual;")
        running_guard = body.index("if (state_ != RunState::kRunning) return false;")
        self.assertLess(guard, cancel_branch)
        self.assertLess(cancel_branch, poison)
        self.assertLess(poison, running_guard)
        self.assertEqual(body.count("state_ = RunState::kUnknownManual;"), 1)

    def test_source_bounded_cancel_join_timeout_is_terminal_unknown_manual(self):
        body = braced_body(
            self.cancellation_class,
            "bool finish_bounded_cancel_join(std::uint64_t generation,")
        self.assertIn("if (state_ != RunState::kCancelRequested) return false;", body)
        self.assertRegex(
            body,
            r"state_ = RunState::kUnknownManual;\s*return active_process_zero;")

    def test_header_and_readme_enumerate_the_transition_table(self):
        for document in (self.header, self.readme):
            for row in ("kIdle", "kRunning", "kCancelRequested", "kComplete",
                        "kUnknownManual", "request_cancel", "orphan",
                        "finish_bounded_cancel_join"):
                self.assertIn(row, document, row)
            self.assertIn("terminal", document.lower())
        self.assertIn("noexcept", self.readme)
        self.assertIn("std::mutex", self.readme)
        self.assertIn("std::terminate", self.readme)

    # -- cancellation state machine: model behaviour -------------------------

    def test_model_refuses_restart_from_running_cancel_requested_or_orphan(self):
        running = CancellationModel()
        generation = running.start()
        self.assertEqual(running.start(), 0)
        self.assertEqual(running.state, "running")

        cancelling = CancellationModel()
        cancelling.cancel(cancelling.start())
        self.assertEqual(cancelling.state, "cancel_requested")
        self.assertEqual(cancelling.start(), 0)
        self.assertEqual(cancelling.state, "cancel_requested")

        orphaned = CancellationModel()
        orphaned.orphan(orphaned.start())
        self.assertEqual(orphaned.state, "unknown_manual")
        self.assertEqual(orphaned.start(), 0)
        self.assertEqual(orphaned.state, "unknown_manual")
        self.assertNotEqual(generation, 0)

    def test_unknown_manual_is_terminal_and_restart_needs_a_new_authority(self):
        model = CancellationModel(authority_generation=41)
        old_generation = model.start()
        self.assertTrue(model.cancel(old_generation))
        self.assertTrue(model.finish_cancel_join(old_generation, True))
        self.assertEqual(model.state, "unknown_manual")
        # No transition escapes the terminal state on this authority.
        self.assertEqual(model.start(), 0)
        self.assertFalse(model.orphan(old_generation))
        self.assertFalse(model.cancel(old_generation))
        self.assertFalse(model.complete(old_generation))
        self.assertEqual(model.state, "unknown_manual")
        # Restart requires a freshly issued authority with a new anchor.
        successor = CancellationModel(authority_generation=old_generation + 1)
        new_generation = successor.start()
        self.assertGreater(new_generation, old_generation)
        self.assertFalse(successor.complete(old_generation))
        self.assertEqual(successor.state, "running")
        self.assertTrue(successor.complete(new_generation))
        self.assertEqual(successor.state, "complete")
        self.assertEqual(model.retry_calls, 0)
        self.assertEqual(successor.retry_calls, 0)

    def test_orphan_is_generation_and_state_fenced(self):
        model = CancellationModel(authority_generation=7)
        self.assertFalse(model.orphan(0))
        self.assertFalse(model.orphan(7))
        self.assertEqual(model.state, "idle")
        generation = model.start()
        self.assertFalse(model.orphan(generation + 1))
        self.assertEqual(model.state, "running")
        self.assertTrue(model.orphan(generation))
        self.assertEqual(model.state, "unknown_manual")

        completed = CancellationModel()
        finished = completed.start()
        self.assertTrue(completed.complete(finished))
        self.assertFalse(completed.orphan(finished))
        self.assertEqual(completed.state, "complete")

    def test_complete_on_a_never_started_authority_is_refused_without_poison(self):
        model = CancellationModel()
        for generation in (0, 1, 2, 99):
            self.assertFalse(model.complete(generation))
            self.assertEqual(model.state, "idle")
        self.assertEqual(model.active, 0)
        # The authority is still usable afterwards.
        self.assertNotEqual(model.start(), 0)

    def test_bounded_cancel_join_timeout_becomes_unknown_manual(self):
        model = CancellationModel()
        generation = model.start()
        self.assertTrue(model.cancel(generation))
        self.assertFalse(model.finish_cancel_join(generation, False))
        self.assertEqual(model.state, "unknown_manual")
        self.assertEqual(model.start(), 0)
        self.assertEqual(model.retry_calls, 0)

    def test_generations_are_derived_and_strictly_monotonic(self):
        model = CancellationModel(authority_generation=100)
        seen = []
        for _ in range(CancellationModel.MAX_RUNS_PER_AUTHORITY):
            generation = model.start()
            seen.append(generation)
            self.assertTrue(model.complete(generation))
        self.assertEqual(seen, list(range(100, 108)))
        self.assertEqual(seen, sorted(set(seen)))
        # The restart budget is bounded.
        self.assertEqual(model.start(), 0)
        self.assertEqual(model.state, "complete")
        # An unanchored authority can never start.
        self.assertEqual(CancellationModel(authority_generation=0).start(), 0)

    def test_late_completion_cannot_mutate_a_restarted_run(self):
        model = CancellationModel(authority_generation=5)
        old_generation = model.start()
        self.assertTrue(model.complete(old_generation))
        new_generation = model.start()
        self.assertNotEqual(old_generation, new_generation)
        self.assertFalse(model.complete(old_generation))
        self.assertEqual(model.state, "running")
        self.assertFalse(model.cancel(old_generation))
        self.assertFalse(model.orphan(old_generation))
        self.assertFalse(model.finish_cancel_join(old_generation, True))
        self.assertEqual(model.state, "running")
        self.assertTrue(model.complete(new_generation))
        self.assertEqual(model.state, "complete")
        self.assertEqual(model.retry_calls, 0)

    def test_completion_racing_an_in_flight_cancel_is_ambiguous_not_success(self):
        model = CancellationModel()
        generation = model.start()
        self.assertTrue(model.cancel(generation))
        self.assertFalse(model.complete(generation))
        self.assertEqual(model.state, "unknown_manual")
        self.assertEqual(model.retry_calls, 0)

    # -- canonical path shape ------------------------------------------------

    def test_canonical_absolute_rejects_every_hostile_path_class(self):
        hostile = {
            "unc_extended": "\\\\?\\UNC\\server\\share\\evil.exe",
            "unc_bare": "\\\\server\\share\\evil.exe",
            "device_globalroot": "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\evil.exe",
            "device_volume": "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\a.exe",
            "device_dot": "\\\\.\\PhysicalDrive0",
            "device_dot_drive": "\\\\.\\C:\\approved\\app.exe",
            "alternate_data_stream": "\\\\?\\C:\\approved\\readme.txt:payload.exe",
            "stream_on_directory": "\\\\?\\C:\\approved:hidden\\app.exe",
            "reserved_con": "\\\\?\\C:\\approved\\CON",
            "reserved_con_ext": "\\\\?\\C:\\approved\\con.txt",
            "reserved_prn": "\\\\?\\C:\\approved\\PRN.exe",
            "reserved_aux": "\\\\?\\C:\\approved\\AUX",
            "reserved_nul": "\\\\?\\C:\\approved\\NUL.dll",
            "reserved_com1": "\\\\?\\C:\\approved\\COM1",
            "reserved_com9": "\\\\?\\C:\\approved\\com9.exe",
            "reserved_lpt1": "\\\\?\\C:\\approved\\LPT1.exe",
            "reserved_lpt9": "\\\\?\\C:\\approved\\lpt9",
            "reserved_directory": "\\\\?\\C:\\NUL\\app.exe",
            "relative_drive": "C:\\approved\\app.exe",
            "relative_bare": "approved\\app.exe",
            "relative_rooted": "\\approved\\app.exe",
            "short_name": "\\\\?\\C:\\PROGRA~1\\app.exe",
            "trailing_dot": "\\\\?\\C:\\approved\\app.exe.",
            "trailing_space": "\\\\?\\C:\\approved\\app.exe ",
            "component_trailing_space": "\\\\?\\C:\\approved \\app.exe",
            "component_leading_space": "\\\\?\\C:\\ approved\\app.exe",
            "empty_component": "\\\\?\\C:\\approved\\\\app.exe",
            "trailing_separator": "\\\\?\\C:\\approved\\",
            "bare_root": "\\\\?\\C:\\",
            "no_root_separator": "\\\\?\\C:",
            "forward_slash": "\\\\?\\C:/approved/app.exe",
            "dotdot_component": "\\\\?\\C:\\approved\\..\\evil.exe",
            "dot_component": "\\\\?\\C:\\approved\\.\\app.exe",
            "wildcard_star": "\\\\?\\C:\\approved\\ap*.exe",
            "wildcard_question": "\\\\?\\C:\\approved\\ap?.exe",
            "redirection": "\\\\?\\C:\\approved\\app<1>.exe",
            "quote": "\\\\?\\C:\\approved\\app\".exe",
            "pipe": "\\\\?\\C:\\approved\\app|x.exe",
            "control_character": "\\\\?\\C:\\approved\\app\x01.exe",
            "non_letter_drive": "\\\\?\\1:\\approved\\app.exe",
            "missing_colon": "\\\\?\\C;\\approved\\app.exe",
            "empty": "",
        }
        for label, path in hostile.items():
            self.assertFalse(canonical_absolute_model(path), label)

    def test_canonical_absolute_accepts_only_the_pinned_shape(self):
        accepted = (
            "\\\\?\\C:\\approved\\app.exe",
            "\\\\?\\c:\\Program Files\\Approved\\app.exe",
            "\\\\?\\D:\\a..b\\app.exe",
            "\\\\?\\C:\\approved\\NULL.exe",
            "\\\\?\\C:\\approved\\CONSOLE.exe",
            "\\\\?\\C:\\approved\\COM0.exe",
            "\\\\?\\C:\\a",
        )
        for path in accepted:
            self.assertTrue(canonical_absolute_model(path), path)

    def test_canonical_absolute_source_carries_every_refusal_branch(self):
        body = braced_body(
            self.launch_class,
            "static bool canonical_absolute(const std::wstring& path) noexcept")
        for branch in (
            "if (path.size() < 8 || path.size() > 32767) return false;",
            r'if (path.compare(0, 4, L"\\\\?\\") != 0) return false;',
            r"if (!drive_letter || path[5] != L':' || path[6] != L'\\') return false;",
            "if (character == L':' && index != 5) return false;",
            "character == L'/'",
            "if (stop == start) return false;",
            "if (length == 2 && begin[0] == L'.' && begin[1] == L'.') return false;",
            "if (begin[length - 1] == L'.' || begin[length - 1] == L' ') return false;",
            "if (begin[0] == L' ') return false;",
            "if (begin[scan] == L'~') return false;",
            "if (reserved_device_name(begin, length)) return false;",
        ):
            self.assertIn(branch, body, branch)
        reserved = braced_body(
            self.launch_class,
            "static bool reserved_device_name(const wchar_t* begin,")
        for token in (
            "upper[0] == L'C' && upper[1] == L'O' && upper[2] == L'N'",
            "upper[0] == L'P' && upper[1] == L'R' && upper[2] == L'N'",
            "upper[0] == L'A' && upper[1] == L'U' && upper[2] == L'X'",
            "upper[0] == L'N' && upper[1] == L'U' && upper[2] == L'L'",
            "upper[0] == L'C' && upper[1] == L'O' && upper[2] == L'M'",
            "upper[0] == L'L' && upper[1] == L'P' && upper[2] == L'T'",
            "upper[3] >= L'1' && upper[3] <= L'9'",
            "if (stem != 3 && stem != 4) return false;",
        ):
            self.assertIn(token, reserved, token)

    # -- argument vector / command line --------------------------------------

    def test_header_binds_the_argument_array_and_the_escaping_obligation(self):
        for token in (
            "struct ArgumentVector final", "std::vector<std::wstring> ordered_arguments;",
            "std::array<std::uint8_t, 32> argument_digest{};",
            "std::uint32_t total_utf16_units = 0;",
            "CommandLineToArgvW", "lpCommandLine", "round-trip",
            "kMaxArguments = 64", "kMaxArgumentUtf16Units = 8192",
            "kMaxCommandLineUtf16Units = 30000",
            "static bool bounded_arguments(const ArgumentVector& arguments) noexcept",
            "argv_array_only_", "command_line_escaping_fixed_",
            "command_line_round_trip_verified_",
            "no_shell_or_concatenated_command_line_", "utf16_safe_arguments_",
            "argument_count_and_length_bounded_",
        ):
            self.assertIn(token, self.header, token)
        body = braced_body(
            self.launch_class,
            "static bool bounded_arguments(const ArgumentVector& arguments) noexcept")
        self.assertIn("if (arguments.ordered_arguments.size() > kMaxArguments) return false;",
                      body)
        self.assertIn("if (argument.size() > kMaxArgumentUtf16Units) return false;", body)
        self.assertIn("if (total > kMaxCommandLineUtf16Units) return false;", body)
        self.assertIn("if (character < 0x20 || character == 0x7f) return false;", body)

    def test_fixed_escaping_algorithm_round_trips_hostile_arguments(self):
        hostile = [
            "status", "--short", "a b", 'say "hi"', "trailing\\", "c:\\path\\",
            "c:\\path with space\\", 'quote"and\\slash', "", "&&", "| more",
            "%PATH%", "^caret", "tab\tinside", "a\\\\\\\"b", "*", "?", "$(x)",
        ]
        self.assertEqual(parse_command_line(build_command_line(hostile)), hostile)
        for argument in hostile:
            self.assertEqual(parse_command_line(build_command_line([argument])),
                             [argument])

    def test_naive_concatenation_breaks_round_trip_equality(self):
        arguments = ["a b", "c"]
        self.assertNotEqual(parse_command_line(" ".join(arguments)), arguments)
        self.assertEqual(parse_command_line(" ".join(arguments)), ["a", "b", "c"])
        # The fixed algorithm does not.
        self.assertEqual(parse_command_line(build_command_line(arguments)), arguments)

    def test_bounded_arguments_model_enforces_count_length_and_totals(self):
        arguments = ["status", "--short"]
        total = sum(len(argument) + 3 for argument in arguments)
        self.assertTrue(bounded_arguments_model(arguments, total))
        self.assertFalse(bounded_arguments_model(arguments, total + 1))
        self.assertFalse(bounded_arguments_model(["x"] * 65, 65 * 4))
        self.assertFalse(bounded_arguments_model(["x" * 8193], 8196))
        self.assertFalse(bounded_arguments_model(["a\x00b"], 6))
        self.assertFalse(bounded_arguments_model(["a\x1fb"], 6))
        oversized = ["x" * 8000] * 4
        self.assertFalse(bounded_arguments_model(
            oversized, sum(len(a) + 3 for a in oversized)))

    # -- transaction, broker, inventory --------------------------------------

    def test_transaction_checks_proof_and_all_gates_before_mutation(self):
        self.assertIn("std::optional<LaunchAuthority> authority", self.transaction)
        refusal = self.transaction.index("if (!plan.authority.has_value()")
        returned = self.transaction.index("return LaunchReceipt{};", refusal)
        self.assertLess(refusal, returned)
        pre_refusal = self.transaction[:refusal]
        for forbidden in ("persist_dispatching", "begin_external_dispatch",
                          "CreateProcess", "ShellExecute", "popen(", "system("):
            self.assertNotIn(forbidden, pre_refusal)
        gate = self.transaction.index("if (!kProcessLaunchAvailable", returned)
        self.assertGreater(gate, returned)
        self.assertIn("return LaunchReceipt{};", self.transaction[gate:])
        self.assertIn("kSupervisorOwnedProcessTransactionAccepted", self.authority)
        consume = self.transaction.index("authority.consume()")
        self.assertGreater(consume, gate)
        self.assertNotIn("CreateProcess", self.transaction)

    def test_transaction_ordering_comment_matches_the_ordering(self):
        """F-13: the comment must not claim consume() runs on refusal paths."""
        self.assertNotIn("including this inert refusal path", self.transaction)
        self.assertIn("which return before consume() is reached", self.transaction)
        self.assertIn("consume() therefore runs at most once", self.transaction)

    def test_broker_launch_remains_refusal_only_and_no_public_activation(self):
        self.assertIn("launch_containment_unproven", self.broker)
        self.assertIn("launch_confinement_unproven", self.broker)
        self.assertIn("broker_not_activated", self.broker)
        self.assertNotIn("CreateProcess", self.broker)
        trust_anchor = TRUST_ANCHOR.read_text(encoding="utf-8")
        self.assertIn("kSupervisorContainmentProven = false", trust_anchor)
        self.assertIn("kLaunchConfinementProven = false", trust_anchor)

    def test_header_is_not_wired_into_any_product_or_package_target(self):
        native = (ROOT / "native/CMakeLists.txt").read_text(encoding="utf-8")
        self.assertNotIn("launch_authority", native)
        self.assertNotIn("windows_supervisor", native)
        inert = (ROOT / "native/cmake/inert_windows_compile_checks.cmake").read_text(
            encoding="utf-8")
        self.assertNotIn("launch_authority", inert)
        # "Listed in a default-OFF target" is the exact claim the docs may make.
        for document in (self.readme, self.contract_doc):
            self.assertIn("default-OFF", document)
            self.assertIn("listed", document.lower())

    def test_current_qa_inventory_contains_this_test(self):
        inventory = (ROOT / "scripts/test/run_qa.py").read_text(encoding="utf-8")
        self.assertIn(
            '"tests/native/test_windows_process_authority_static.py": "native_static"',
            inventory)

    # -- admission refusal model ---------------------------------------------

    def test_every_missing_pre_child_proof_refuses_without_calls(self):
        base = {key: True for key in RefusalModel.REQUIRED}
        for missing in RefusalModel.REQUIRED:
            proof = dict(base)
            proof[missing] = False
            model = RefusalModel()
            self.assertEqual(model.admit(proof, gates=True), "unavailable", missing)
            self.assertEqual((model.create_calls, model.mutation_calls), (0, 0))

    def test_global_gate_false_refuses_even_with_complete_proof(self):
        model = RefusalModel()
        proof = {key: True for key in RefusalModel.REQUIRED}
        self.assertEqual(model.admit(proof, gates=False), "unavailable")
        self.assertEqual((model.create_calls, model.mutation_calls), (0, 0))


if __name__ == "__main__":
    unittest.main()
