"""Binds the frozen descriptor-WAL contract to the dormant Win32 v2 boundary.

`contracts/action-journal-descriptor-wal/v0.1.0.json` is the contract ADR-0004
requires for the v2 `DescriptorActionJournal` WAL boundary before any
transport, import, package, or activation path exists. Nothing here compiles,
loads, or executes Windows code, and nothing here creates a process.

The v2 status set is derived, not transcribed: every `StorageStatus`-returning
definition in `windows_storage.cpp` is extracted by its signature, a call graph
is built over those definitions, and the statuses reachable from the v2 entry
points are compared against the contract. A status added to, removed from, or
moved between the two boundaries therefore fails this suite instead of drifting
past it.

Three R4 properties are pinned rather than restated. Two already exist in
`tests/native/test_windows_descriptor_journal_bootstrap_static.py` and are
referenced by name below instead of being duplicated:
`test_duplicate_is_not_born_inheritable_and_carries_no_delete` (exact duplicate
mask, `DuplicateHandle(..., FALSE, 0)`, no `DUPLICATE_SAME_ACCESS`) and
`test_failed_create_discards_the_unpublished_leaf_and_reopen_never_deletes`
(the discard guard is disarmed before the lease takes ownership). The third —
that no accessor exposes the lease's raw handle — had no pin and is added here.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CPP = ROOT / "native" / "action_journal_storage" / "windows_storage.cpp"
HPP = ROOT / "native" / "action_journal_storage" / "windows_storage.hpp"
DESIGN = ROOT / "native" / "action_journal_storage" / "DESCRIPTOR_WAL_BOOTSTRAP.md"
CONTRACT = ROOT / "contracts" / "action-journal-descriptor-wal" / "v0.1.0.json"
CONTRACT_MD = ROOT / "contracts" / "action-journal-descriptor-wal" / "v0.1.0.md"
STORAGE_CONTRACT = ROOT / "contracts" / "action-journal-storage" / "v0.1.0.json"
STORAGE_CONTRACT_MD = ROOT / "contracts" / "action-journal-storage" / "v0.1.0.md"
HOST = ROOT / "host" / "agent" / "action-journal.mjs"
BOOTSTRAP_SUITE = ROOT / "tests" / "native" / "test_windows_descriptor_journal_bootstrap_static.py"

MAX_SOURCE_BYTES = 256 * 1024
MAX_JSON_BYTES = 64 * 1024

WAL_HEADER = b'{"format":"lae-action-journal-wal","version":2}\n'
WAL_HEADER_BYTES = 48
WAL_MAX_BYTES = 33554432
WAL_LEAF_NAME = "action-journal-v2.wal"

AVAILABILITY_GATES = (
    "production_available",
    "native_target_registered",
    "helper_added",
    "transport_added",
    "node_integration_added",
    "package_added",
    "activation_permitted",
)

# Every `StorageStatus`-returning definition in the translation unit. Declared
# so a new one cannot be added without either appearing here or failing the
# derivation guard below.
STATUS_DEFINITIONS = frozenset({
    "acquire_storage",
    "acquire_descriptor_wal",
    "decode_header",
    "descriptor_wal_shape",
    "encode_header",
    "file_shape",
    "filesystem_policy",
    "initialize_header",
    "load_header",
    "open_error",
    "validate_descriptor_wal_prefix",
    "validate_volume",
    "zero_initialize",
    "DescriptorWalHandoff::arm_inheritance",
    "DescriptorWalHandoff::revoke_inheritance",
    "DescriptorWalLease::prepare_inheritable_handoff",
    "DescriptorWalLease::recheck",
    "DescriptorWalLease::revalidate",
})

V2_ENTRY_POINTS = (
    "acquire_descriptor_wal",
    "DescriptorWalLease::recheck",
    "DescriptorWalLease::revalidate",
    "DescriptorWalLease::prepare_inheritable_handoff",
    "DescriptorWalHandoff::arm_inheritance",
    "DescriptorWalHandoff::revoke_inheritance",
)

V1_ENTRY_POINTS = ("acquire_storage",)

# Declared by the storage contract and emitted by no function body in the
# translation unit. Pinned so the gap between the declared superset and the
# emitted union cannot silently grow.
DECLARED_BUT_UNEMITTED = frozenset({"platform_unavailable"})

# The four codes ADR-0004 added to the storage contract for this boundary.
ADR_0004_CODES = frozenset({
    "handoff_already_transferred",
    "source_handle_inheritable",
    "inheritance_control_failed",
    "final_path_mismatch",
})

REFERENCED_BOOTSTRAP_TESTS = (
    "test_duplicate_is_not_born_inheritable_and_carries_no_delete",
    "test_failed_create_discards_the_unpublished_leaf_and_reopen_never_deletes",
)


def bounded_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"oversized source: {path}")
    return raw.decode("utf-8", errors="strict")


def strict_json(path: Path):
    """Parse with a duplicate-key-rejecting hook and a byte bound."""

    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"oversized contract: {path}")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)


def code_only(text: str) -> str:
    """C++ text with line comments removed, for absence assertions."""

    return re.sub(r"//[^\n]*", "", text)


def function_body(text: str, signature: str) -> str:
    """Text of one C++ definition, from its signature to its column-0 brace."""

    match = re.search(signature + r".*?\n\}", text, re.S)
    if match is None:
        raise AssertionError(f"no definition matched: {signature}")
    return match.group(0)


def status_definitions(cpp: str) -> dict[str, str]:
    """Every `StorageStatus`-returning definition, keyed by name."""

    bodies: dict[str, str] = {}
    for match in re.finditer(
        r"^StorageStatus ([A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)\(", cpp, re.M
    ):
        name = match.group(1)
        if name in bodies:
            raise AssertionError(f"duplicate definition: {name}")
        bodies[name] = function_body(cpp[match.start():], r"StorageStatus " + re.escape(name) + r"\(")
    return bodies


def emitted_statuses(body: str) -> set[str]:
    """Enumerator names a body can return, ignoring comparisons and comments."""

    text = re.sub(r"(?:==|!=)\s*StorageStatus::k[A-Za-z0-9]+", "", code_only(body))
    return set(re.findall(r"StorageStatus::(k[A-Za-z0-9]+)", text))


def reachable(bodies: dict[str, str], roots) -> set[str]:
    """Definitions reachable from `roots` through direct calls."""

    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        body = bodies[name]
        for candidate in bodies:
            short = candidate.split("::")[-1]
            if candidate not in seen and re.search(
                r"(?<![\w:])" + re.escape(short) + r"\(", body
            ):
                pending.append(candidate)
    return seen


class DescriptorWalContractStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpp = bounded_text(CPP)
        cls.hpp = bounded_text(HPP)
        cls.design = bounded_text(DESIGN)
        cls.host = bounded_text(HOST)
        cls.contract = strict_json(CONTRACT)
        cls.contract_md = bounded_text(CONTRACT_MD)
        cls.storage = strict_json(STORAGE_CONTRACT)
        cls.storage_md = bounded_text(STORAGE_CONTRACT_MD)
        cls.bootstrap_suite = bounded_text(BOOTSTRAP_SUITE)
        cls.bodies = status_definitions(cls.cpp)
        cls.mapping = dict(
            re.findall(r"case StorageStatus::(k\w+): return \"([a-z0-9_]+)\";", cls.cpp)
        )
        cls.handoff = function_body(
            cls.cpp, r"StorageStatus DescriptorWalLease::prepare_inheritable_handoff\(")
        cls.acquire = function_body(
            cls.cpp, r"StorageStatus acquire_descriptor_wal\(const DescriptorWalRequest")

    def status_set(self, roots) -> set[str]:
        names: set[str] = set()
        for definition in reachable(self.bodies, roots):
            names |= emitted_statuses(self.bodies[definition])
        return {self.mapping[name] for name in names}

    # --- parsing and identity -------------------------------------------------

    def test_both_contracts_parse_strictly_without_duplicate_keys(self):
        # strict_json raises on a duplicate key or an oversized file; re-parsing
        # here makes that an assertion of this suite rather than a setup detail.
        for path in (CONTRACT, STORAGE_CONTRACT):
            with self.subTest(contract=path.name):
                self.assertIsInstance(strict_json(path), dict)
        self.assertEqual(
            CONTRACT.read_bytes().decode("utf-8"),
            CONTRACT.read_bytes().decode("utf-8", errors="strict"))

    def test_contract_identity_is_exact(self):
        self.assertEqual(self.contract["name"], "lae.action-journal-descriptor-wal")
        self.assertEqual(self.contract["version"], "0.1.0")
        self.assertEqual(
            self.contract["$id"], "lae://contracts/action-journal-descriptor-wal/0.1.0")
        self.assertEqual(
            self.contract["schema_version"],
            "lae.action-journal-descriptor-wal-boundary.v1")
        self.assertEqual(self.contract["status"], "source_only_not_enabled")
        self.assertEqual(self.contract["platform"], "windows_x64_source_only")
        self.assertEqual(
            self.contract["required_by"], "ADR-0004-inert-contract-additive-extension")
        self.assertEqual(self.contract["interface_change_request"], "ICR-RUN-WDJB-001")
        # A distinct identity from the v1 storage boundary it must not absorb.
        self.assertNotEqual(self.contract["schema_version"], self.storage["schema_version"])
        self.assertNotEqual(self.contract["$id"], self.storage["$id"])
        self.assertEqual(
            self.contract["sibling_storage_contract"], self.storage["schema_version"])

    def test_every_availability_gate_is_literal_false(self):
        for gate in AVAILABILITY_GATES:
            with self.subTest(gate=gate):
                self.assertIs(self.contract[gate], False)
        self.assertIs(self.contract["receipt"]["descriptor_bridge_available"], False)
        self.assertIs(self.contract["trust_anchor"]["is_an_authenticity_anchor"], False)
        # The sibling contract's gates are untouched by this slice.
        for gate in AVAILABILITY_GATES:
            with self.subTest(storage_gate=gate):
                self.assertIs(self.storage[gate], False)

    # --- derived status sets --------------------------------------------------

    def test_status_derivation_covers_every_status_returning_definition(self):
        self.assertEqual(set(self.bodies), set(STATUS_DEFINITIONS))
        enum = re.search(
            r"enum class StorageStatus : std::uint8_t \{(.+?)\n\};", self.hpp, re.S)
        self.assertIsNotNone(enum)
        members = set(re.findall(r"\b(k[A-Za-z0-9]+),", enum.group(1)))
        self.assertEqual(members, set(self.mapping))
        self.assertEqual(len(self.mapping), 30)
        for entry in V2_ENTRY_POINTS + V1_ENTRY_POINTS:
            with self.subTest(entry=entry):
                self.assertIn(entry, self.bodies)

    def test_v2_status_set_exactly_matches_the_contract(self):
        declared = self.contract["status_codes"]
        self.assertEqual(len(declared), len(set(declared)))
        self.assertEqual(set(declared), self.status_set(V2_ENTRY_POINTS))
        self.assertEqual(len(declared), 26)
        # The ADR-0004 codes exist because of this boundary and only this one.
        self.assertTrue(ADR_0004_CODES <= set(declared))
        self.assertFalse(ADR_0004_CODES & self.status_set(V1_ENTRY_POINTS))

    def test_v1_and_v2_sets_stay_within_the_storage_contract_superset(self):
        storage = set(self.storage["status_codes"])
        v1 = self.status_set(V1_ENTRY_POINTS)
        v2 = self.status_set(V2_ENTRY_POINTS)
        self.assertTrue(v1 | v2 <= storage)
        # Exactly one declared code is emitted by no function body. The union
        # plus that code is the storage contract, so nothing can go missing.
        self.assertEqual(storage - (v1 | v2), set(DECLARED_BUT_UNEMITTED))
        self.assertEqual(v1 | v2 | DECLARED_BUT_UNEMITTED, storage)
        for name in DECLARED_BUT_UNEMITTED:
            with self.subTest(status=name):
                self.assertIn(name, storage)
                self.assertNotIn(name, self.contract["status_codes"])
        self.assertEqual(
            set(self.contract["status_codes_retained_by_the_storage_contract"]["storage_only"]),
            storage - v2)
        self.assertEqual(
            sorted(self.contract["status_codes_retained_by_the_storage_contract"]["storage_only"]),
            sorted(DECLARED_BUT_UNEMITTED | (v1 - v2)))

    def test_storage_contract_json_is_untouched_and_its_superset_is_documented(self):
        self.assertTrue(ADR_0004_CODES <= set(self.storage["status_codes"]))
        self.assertEqual(self.storage["version"], "0.1.0")
        self.assertEqual(self.storage["fixed_leaf_name"], "action-journal-v1.container")
        self.assertEqual(self.storage["limits"]["container_bytes"], 33558528)
        for token in (
            "documented superset",
            "contracts/action-journal-descriptor-wal/v0.1.0.json",
            "ADR-0004",
            "ICR-RUN-WDJB-001",
            "requires a new interface change request",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.storage_md)

    # --- header and limit parity ---------------------------------------------

    def test_header_constant_is_byte_identical_in_contract_cpp_and_node(self):
        native = re.search(
            r'constexpr char kDescriptorWalHeader\[\] =\s*"([^"]*(?:\\"[^"]*)*)";', self.cpp)
        self.assertIsNotNone(native)
        native_bytes = bytes(native.group(1), "utf-8").decode("unicode_escape").encode()
        node = re.search(r"const DESCRIPTOR_WAL_HEADER = '([^']+)';", self.host)
        self.assertIsNotNone(node)
        node_bytes = bytes(node.group(1), "utf-8").decode("unicode_escape").encode()
        declared = self.contract["header"]
        contract_bytes = bytes.fromhex(declared["hex"])
        self.assertEqual(contract_bytes, WAL_HEADER)
        self.assertEqual(native_bytes, contract_bytes)
        self.assertEqual(node_bytes, contract_bytes)
        self.assertEqual(declared["utf8"].encode("utf-8"), contract_bytes)
        self.assertEqual(declared["bytes"], WAL_HEADER_BYTES)
        self.assertEqual(len(contract_bytes), declared["bytes"])
        self.assertEqual(declared["native_constant"], "kDescriptorWalHeader")
        self.assertEqual(declared["node_constant"], "DESCRIPTOR_WAL_HEADER")
        self.assertIn(declared["native_constant"], self.cpp)
        self.assertIn(declared["node_constant"], self.host)
        self.assertIn(
            "kDescriptorWalHeaderBytes =\n    sizeof(kDescriptorWalHeader) - 1", self.cpp)
        self.assertIs(declared["trailing_newline"], True)
        self.assertTrue(contract_bytes.endswith(b"\n"))
        self.assertIn(declared["hex"], self.contract_md)

    def test_wal_byte_limit_is_identical_in_contract_cpp_and_node(self):
        limits = self.contract["limits"]
        self.assertEqual(limits["max_wal_bytes"], WAL_MAX_BYTES)
        native = re.search(
            r"kDescriptorWalMaxBytes = (\d+)ull \* (\d+)ull \* (\d+)ull", self.hpp)
        self.assertIsNotNone(native)
        product = 1
        for part in native.groups():
            product *= int(part)
        self.assertEqual(product, limits["max_wal_bytes"])
        node = re.search(
            r"const MAX_DESCRIPTOR_WAL_BYTES = (\d+) \* (\d+) \* (\d+);", self.host)
        self.assertIsNotNone(node)
        node_product = 1
        for part in node.groups():
            node_product *= int(part)
        self.assertEqual(node_product, limits["max_wal_bytes"])
        # Inclusive on the native side, and the contract says so.
        bound = re.search(
            r"return size (<=|<) kDescriptorWalMaxBytes \? StorageStatus::kOkOpened",
            self.bodies["descriptor_wal_shape"])
        self.assertIsNotNone(bound)
        self.assertEqual(bound.group(1) == "<=", limits["max_wal_bytes_inclusive"])
        self.assertIs(limits["max_wal_bytes_inclusive"], True)
        self.assertEqual(limits["native_constant"], "kDescriptorWalMaxBytes")
        self.assertEqual(limits["node_constant"], "MAX_DESCRIPTOR_WAL_BYTES")
        self.assertEqual(
            limits["max_path_utf16_code_units"],
            self.storage["limits"]["max_path_utf16_code_units"])

    def test_fixed_leaf_name_matches_the_native_constant(self):
        self.assertEqual(self.contract["fixed_leaf_name"], WAL_LEAF_NAME)
        self.assertIn(
            'kDescriptorWalLeafName[] = L"' + WAL_LEAF_NAME + '"', self.hpp)
        self.assertNotEqual(
            self.contract["fixed_leaf_name"], self.storage["fixed_leaf_name"])
        self.assertIn("kDescriptorWalLeafName", self.acquire)
        self.assertNotIn("request.leaf", self.acquire)

    # --- contract-to-source cross-checks -------------------------------------

    def test_required_predicate_names_all_appear_in_the_cpp_header(self):
        predicates = self.contract["required_predicates"]
        self.assertEqual(len(predicates), len(set(predicates)))
        self.assertGreaterEqual(len(predicates), 10)
        for predicate in predicates:
            with self.subTest(predicate=predicate):
                self.assertIn(predicate, self.hpp)

    def test_duplicate_access_mask_is_declared_and_matches_the_source(self):
        access = self.contract["handoff"]["duplicate_access"]
        self.assertEqual(access["requested_mask"], "GENERIC_READ | GENERIC_WRITE")
        self.assertIs(access["b_inherit_handle"], False)
        self.assertEqual(access["duplicate_options"], 0)
        self.assertIs(access["duplicate_same_access"], False)
        self.assertIs(access["delete"], False)
        self.assertIs(access["write_dac"], False)
        self.assertIs(access["write_owner"], False)
        self.assertIs(access["read_control_crosses"], True)
        self.assertIs(self.contract["handoff"]["born_inheritable"], False)
        # The declared mask is the mask the source requests, verbatim.
        self.assertRegex(
            self.handoff,
            r"DuplicateHandle\(GetCurrentProcess\(\), impl_->file\.get\(\),\s*\n"
            r"\s*GetCurrentProcess\(\), candidate->handle\.put\(\),\s*\n"
            r"\s*" + re.escape(access["requested_mask"]) + r", FALSE, 0\)")
        self.assertNotIn("DUPLICATE_SAME_ACCESS", code_only(self.handoff))
        self.assertNotIn("DELETE", code_only(self.handoff))
        self.assertIn(access["requested_mask"], self.design)
        self.assertIn(access["requested_mask"], self.contract_md)
        self.assertIn("DUPLICATE_SAME_ACCESS", self.contract_md)
        self.assertEqual(self.contract["handoff"]["max_prepared_duplicates"], 1)
        self.assertEqual(self.contract["limits"]["max_prepared_duplicates"], 1)
        self.assertEqual(
            self.contract["handoff"]["second_prepare_status"],
            "handoff_already_transferred")
        self.assertIn(
            self.contract["handoff"]["second_prepare_status"],
            self.contract["status_codes"])

    def test_launcher_precondition_strings_appear_in_contract_header_and_doc(self):
        preconditions = self.contract["launcher_preconditions"]
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", preconditions["required"])
        self.assertIn("no_ambient_handle_inheritance", preconditions["required"])
        self.assertEqual(
            preconditions["supervisor_contract_property"],
            "no_ambient_handle_inheritance")
        self.assertEqual(
            preconditions["bInheritHandles_true_without_an_explicit_handle_list"],
            "forbidden")
        self.assertEqual(
            preconditions["serialize_duplicate_as_LAE_ACTION_JOURNAL_FD"], "forbidden")
        self.assertIs(preconditions["handle_is_a_crt_file_descriptor"], False)
        self.assertIs(preconditions["child_must_convert_handle_to_a_crt_descriptor"], True)
        for token in ("PROC_THREAD_ATTRIBUTE_HANDLE_LIST",
                      "no_ambient_handle_inheritance",
                      "LAE_ACTION_JOURNAL_FD"):
            with self.subTest(token=token):
                self.assertIn(token, self.hpp)
                self.assertIn(token, self.design)
                self.assertIn(token, self.contract_md)
        # Arm immediately before the single CreateProcess*, revoke or close
        # after and on every failure, and refuse take_handle while armed.
        self.assertIn(
            "arm_inheritance_immediately_before_the_single_create_process_call",
            preconditions["required"])
        self.assertIn(
            "revoke_or_close_after_child_creation_and_on_every_failure_path",
            preconditions["required"])
        self.assertIn(
            "take_handle_refused_while_inheritance_armed", preconditions["required"])
        self.assertIs(
            self.contract["handoff"]["take_handle_refused_while_inheritance_armed"], True)
        self.assertIn(
            "if (!valid() || inheritance_armed()) return INVALID_HANDLE_VALUE;",
            function_body(
                self.cpp, r"HANDLE DescriptorWalHandoff::take_handle\(\) noexcept \{"))
        control = self.contract["handoff"]["inheritance_control"]
        self.assertEqual(control["api"], "SetHandleInformation(HANDLE_FLAG_INHERIT, ...)")
        self.assertIs(control["idempotent"], True)
        self.assertIs(control["decides_on_observed_kernel_flag_not_api_return"], True)
        self.assertEqual(control["failure_status"], "inheritance_control_failed")
        for method in (control["arm"], control["revoke"], control["observe"]):
            with self.subTest(method=method):
                self.assertIn(method, self.hpp)
        self.assertEqual(
            self.contract["handoff"]["inheritance_states"],
            ["absent", "prepared_not_inheritable", "inheritance_armed",
             "inheritance_revoked", "closed"])

    def test_delete_on_failure_rule_matches_the_source(self):
        rule = self.contract["delete_on_failure"]
        self.assertEqual(rule["applies_to"], "create_new")
        self.assertEqual(rule["never_applies_to"], "open_existing")
        self.assertEqual(rule["raii_guard"], "CreatedFileDiscard")
        self.assertIs(rule["armed_only_under_create"], True)
        self.assertIs(rule["disarmed_before_lease_ownership"], True)
        self.assertIs(rule["is_a_wal_repair"], False)
        self.assertIs(rule["reopen_path_requests_delete"], False)
        self.assertIs(rule["reopen_path_may_delete_truncate_or_write"], False)
        self.assertIs(rule["path_metadata_trusted_a_second_time"], False)
        self.assertIn(rule["raii_guard"], self.cpp)
        self.assertIn(rule["mechanism"].split(" with ")[0], self.cpp)
        self.assertIn("FileDispositionInfo", self.cpp)
        # DELETE exists on the create path only, and nowhere else in the call.
        code = code_only(self.acquire)
        self.assertRegex(
            code,
            r"const DWORD leaf_access = GENERIC_READ \| GENERIC_WRITE \| READ_CONTROL \|\s*\n"
            r"\s*\(create \? DELETE : 0\);")
        self.assertEqual(len(re.findall(r"(?<![_A-Za-z])DELETE\b", code)), 1)
        self.assertEqual(
            self.contract["operations"]["create_new"]["on_failure"],
            "discard_the_never_published_create_new_leaf_through_its_own_open_handle")
        self.assertEqual(
            self.contract["operations"]["open_existing"]["on_failure"],
            "preserve_the_file_exactly_as_found_never_delete_truncate_or_write")
        self.assertEqual(
            self.contract["operations"]["create_new"]["disposition"], "CREATE_NEW")
        self.assertEqual(
            self.contract["operations"]["open_existing"]["disposition"], "OPEN_EXISTING")
        self.assertEqual(self.contract["operations"]["create_new"]["initial_size"], 0)
        self.assertIs(
            self.contract["header"]["published_size_asserted_not_assumed"], True)
        self.assertRegex(
            self.acquire,
            r"if \(create && candidate->size != kDescriptorWalHeaderBytes\)\s*\n"
            r"\s*return fail\(StorageStatus::kSizeMismatch\);")

    def test_shared_position_and_transferred_state_rule_matches_the_source(self):
        shared = self.contract["shared_file_object"]
        self.assertIs(shared["duplicate_is_a_new_open"], False)
        self.assertIs(shared["shares_one_file_object_and_one_file_position"], True)
        self.assertEqual(shared["position_owner_after_handoff"], "child")
        self.assertEqual(
            shared["parent_side_io_after_handoff"],
            "refused_with_handoff_already_transferred")
        self.assertEqual(shared["child_io_mode"], "positional reads and writes only")
        self.assertEqual(shared["transferred_predicate"], "handoff_transferred")
        self.assertIs(shared["revalidate_refused_after_transfer"], True)
        self.assertIn("bool " + shared["transferred_predicate"] + "() const noexcept;",
                      self.hpp)
        self.assertRegex(
            self.bodies["DescriptorWalLease::revalidate"],
            r"if \(impl_->handoff_transferred\)\s*\n"
            r"\s*return StorageStatus::kHandoffAlreadyTransferred;\s*\n"
            r"\s*return recheck\(\);")
        for token in ("share one file object", "positional reads",
                      "child owns that position"):
            with self.subTest(token=token):
                self.assertIn(token, self.design)
        self.assertEqual(self.contract["lease"]["states"],
                         ["empty", "held", "handoff_transferred"])
        self.assertIs(self.contract["lease"]["recheck_set_equals_acquisition_check_set"],
                      True)

    def test_receipt_fields_match_the_native_struct_exactly(self):
        struct = re.search(
            r"struct DescriptorWalReceipt \{(.+?)\n\};", self.hpp, re.S).group(1)
        fields = re.findall(r"^\s+[\w:<>,\s\*]+?\b(\w+)\s*(?:=|;|\{)", struct, re.M)
        self.assertEqual(fields, self.contract["receipt"]["fields"])
        for forbidden in self.contract["receipt"]["forbidden"]:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, struct.lower())
        self.assertEqual(
            self.contract["receipt"]["schema"],
            "lae.action-journal-descriptor-wal-receipt.v1")
        self.assertNotEqual(
            self.contract["receipt"]["schema"], self.storage["receipt"]["schema"])

    # --- R4 closure -----------------------------------------------------------

    def test_no_accessor_exposes_the_leases_raw_handle(self):
        declaration = re.search(
            r"class DescriptorWalLease final \{(.+?)\n\};", self.hpp, re.S).group(1)
        self.assertNotIn("HANDLE", declaration)
        self.assertNotIn("retained_file_handle", declaration)
        # The v1 lease deliberately does expose one, so this is a real
        # difference between the two boundaries, not an artefact of the regex.
        v1_declaration = re.search(
            r"class JournalStorageLease final \{(.+?)\n\};", self.hpp, re.S).group(1)
        self.assertIn("HANDLE retained_file_handle() const noexcept;", v1_declaration)
        # No out-of-line definition returns a HANDLE from the lease either; the
        # only HANDLE-returning descriptor-WAL members belong to the handoff,
        # which owns the narrowed duplicate rather than the retained handle.
        handle_members = set(re.findall(
            r"^HANDLE (DescriptorWal\w+)::(\w+)\(", self.cpp, re.M))
        self.assertEqual(
            handle_members, {("DescriptorWalHandoff", "handle"),
                             ("DescriptorWalHandoff", "take_handle")})
        self.assertIs(self.contract["lease"]["public_raw_handle_accessor"], False)
        self.assertIs(
            self.contract["handoff"]["handle_value_in_receipt_log_command_line_or_environment"],
            False)

    def test_r4_is_closed_in_the_design_note_with_its_reasoning(self):
        self.assertIn("## Accepted design limitations", self.design)
        limitation = self.design[self.design.index("## Accepted design limitations"):]
        for token in (
            "review item R4",
            "Win32 has no operation that narrows the access already granted to an open\nhandle",
            "same file object",
            "ReOpenFile",
            "collides with the exclusive reservation",
            "the WAL has been created, written, flushed",
            "Share bookkeeping lives\non the file object, not the handle",
            "denied to everyone else for the complete lease lifetime",
        ):
            with self.subTest(token=token):
                self.assertIn(token, limitation)
        # The two existing pins are referenced, not duplicated, and they exist.
        for name in REFERENCED_BOOTSTRAP_TESTS:
            with self.subTest(test=name):
                self.assertIn(name, limitation)
                self.assertIn("def " + name + "(self):", self.bootstrap_suite)
        self.assertIn("test_descriptor_wal_contract_static.py", limitation)
        self.assertIn("Accepted limitation", self.contract_md)
        self.assertIs(
            self.contract["delete_on_failure"]["disarmed_before_lease_ownership"], True)

    # --- dormancy -------------------------------------------------------------

    def test_contract_md_states_the_adr_0004_requirement_and_that_no_path_exists(self):
        self.assertIn("ADR-0004", self.contract_md)
        self.assertIn(
            "**This contract is\nrequired by ADR-0004 before any transport, import, package, or activation path\n"
            "for the v2 WAL boundary, and no such path exists today.**",
            self.contract_md)
        for token in ("ICR-RUN-WDJB-001", "acquire_descriptor_wal", "literal `false`"):
            with self.subTest(token=token):
                self.assertIn(token, self.contract_md)
        self.assertIn(
            "coordination/adrs/ADR-0004-inert-contract-additive-extension.md",
            self.contract_md)

    def test_the_new_contract_has_no_consumer_transport_or_package_entry(self):
        for relative in (
            "lae-host.mjs",
            "package.json",
            "native/CMakeLists.txt",
            "CMakeLists.txt",
            "host/agent/action-journal.mjs",
            "host/agent/native-action-journal-client.mjs",
            "host/agent/action-journal-protocol.mjs",
        ):
            with self.subTest(path=relative):
                text = bounded_text(ROOT / relative)
                self.assertNotIn("action-journal-descriptor-wal", text)
                self.assertNotIn("acquire_descriptor_wal", text)
        self.assertNotIn("action_journal_storage", bounded_text(ROOT / "native" / "CMakeLists.txt"))
        self.assertNotRegex(self.cpp, r"\b(CreateProcess\w*|ShellExecute\w*)\s*\(")
        self.assertNotIn("LAE_ACTION_JOURNAL_FD", code_only(self.cpp))
        self.assertIn("Sol review and merge of this contract", self.contract["residual_gates"])


if __name__ == "__main__":
    unittest.main()
