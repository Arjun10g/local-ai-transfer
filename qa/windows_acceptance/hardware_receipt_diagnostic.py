#!/usr/bin/env python3
"""Strict, bounded validator for the source-only Windows hardware receipt.

The collector is diagnostic.  This module never turns fixture data or an
identity-unbound PowerShell run into target acceptance evidence.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any


SCHEMA_VERSION = "1.2.0-diagnostic"
RECEIPT_KIND = "windows-hardware-diagnostic-receipt"
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_STRING_CHARS = 512
MAX_JSON_INTEGER_DIGITS = 20
TARGET_MEMORY_BYTES = 32 * 1024**3
TARGET_MEMORY_SPEED = 5600
TARGET_GPU_DRIVER = "32.0.101.8247"
TARGET_BOARD = "039NNG"
TARGET_BOARD_REVISION = "A00"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PCI_TUPLE = re.compile(r"^PCI\\VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}$")
PNP_INTEL = re.compile(r"^PCI\\VEN_8086&DEV_[0-9A-F]{4}$")
CPU_NAME = re.compile(r"Intel.*Core.*Ultra\s*7", re.IGNORECASE)
GPU_NAME = re.compile(r"Intel.*Graphics", re.IGNORECASE)
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
PATHISH = re.compile(
    r"(?:[\\/]|^[A-Za-z]:|^[.~]|^(?:Users|home|Documents and Settings)(?:$|[\\/])|"
    r"^(?:NT|UNC|Device|\\\\\?\\\\)(?:$|[:\\/]))",
    re.IGNORECASE,
)
SENSITIVE_KEYS = {
    "username", "user_name", "domain", "serial_number", "processor_id",
    "path", "file_path", "device_path", "environment", "token", "secret",
    "credential", "command_line", "caption_path",
}

TOP_KEYS = {
    "schema_version", "receipt_kind", "generated_at_utc", "executed_on_target",
    "fixture", "collector", "collection", "expected", "observed", "checks",
    "verdict",
}
COLLECTOR_KEYS = {
    "manifest_id", "script_sha256", "expected_script_sha256",
    "authenticode_required", "authenticode_status", "signer_subject_sha256",
    "authenticode_revocation_policy", "execution_identity_bound",
    "cim_materialization_bound_proven",
}
COLLECTION_KEYS = {
    "read_only", "offline", "stdout_only", "admin_required", "wmi_mutation",
    "downloads", "installs", "model_loaded", "secrets_collected",
}
EXPECTED_KEYS = {
    "profile_id", "computer_manufacturer", "cpu_family", "vpro_enterprise",
    "gpu_name", "gpu_vendor_id", "gpu_driver_version", "integrated_gpu",
    "separate_discrete_gpu", "memory_bytes", "memory_type", "memory_speed_mts",
    "board_product", "board_revision", "os_architecture",
}
OBSERVED_KEYS = {
    "os", "computer", "cpu", "memory_modules", "baseboards", "bios",
    "gpu_adapters", "display_drivers", "gpu_memory", "disk_volumes",
    "pagefile", "power_plan", "runtimes",
}
OS_KEYS = {"product", "version", "build", "architecture", "sku", "secure_boot"}
COMPUTER_KEYS = {"manufacturer", "model", "system_type", "total_memory_bytes", "available_memory_bytes"}
CPU_KEYS = {
    "slot_index", "name", "manufacturer", "architecture", "address_width",
    "physical_cores", "logical_processors", "max_clock_mhz", "virtualization_firmware",
    "second_level_address_translation", "vpro_enterprise", "vpro_evidence_source",
    "features",
}
CPU_FEATURE_KEYS = {"aes", "avx", "avx2", "bmi1", "bmi2", "fma", "popcnt", "sse42"}
MEMORY_KEYS = {"slot_index", "capacity_bytes", "speed_mts", "configured_speed_mts", "smbios_memory_type", "data_width"}
BOARD_KEYS = {"manufacturer", "product", "revision"}
BIOS_KEYS = {"manufacturer", "version", "release_utc", "secure_boot"}
GPU_KEYS = {"slot_index", "name", "vendor", "pnp_device_id", "driver_version", "driver_date_utc", "adapter_ram_bytes", "integrated", "status"}
DRIVER_KEYS = {"slot_index", "name", "pnp_device_id", "driver_version", "driver_date_utc", "manufacturer", "signed", "signer_name"}
GPU_MEMORY_KEYS = {"shared_capacity_bytes", "dedicated_capacity_bytes", "source"}
DISK_KEYS = {"slot_index", "filesystem", "size_bytes", "free_bytes", "compressed"}
PAGEFILE_KEYS = {"allocated_bytes", "current_usage_bytes", "peak_usage_bytes"}
POWER_KEYS = {"active", "scheme_guid", "name"}
RUNTIME_KEYS = {"vulkan", "opencl", "directml"}
RUNTIME_ITEM_KEYS = {"present", "version", "source"}
CHECKS_KEYS = {"hardware_match", "reason_codes"}
VERDICT_KEYS = {"status", "target_evidence_accepted"}

EXPECTED = {
    "profile_id": "dell-039nng-a00-core-ultra-7-vpro-intel-graphics",
    "computer_manufacturer": "Dell Inc.",
    "cpu_family": "Intel Core Ultra 7",
    "vpro_enterprise": True,
    "gpu_name": "Intel Graphics",
    "gpu_vendor_id": "VEN_8086",
    "gpu_driver_version": TARGET_GPU_DRIVER,
    "integrated_gpu": True,
    "separate_discrete_gpu": False,
    "memory_bytes": TARGET_MEMORY_BYTES,
    "memory_type": "DDR5",
    "memory_speed_mts": TARGET_MEMORY_SPEED,
    "board_product": TARGET_BOARD,
    "board_revision": TARGET_BOARD_REVISION,
    "os_architecture": "x64",
}

# These are validator-side bounds, not a producer output constant. The
# PowerShell entrypoint is a refusal-only stub and emits no reason array. A
# reason is included only when the validator observes the corresponding
# malformed, missing, mismatched, or unavailable evidence.
TRUST_GATE_REASON_CODES = frozenset({
    "target_trust_anchor_unavailable", "fixture_not_target_evidence",
    "not_executed_on_target", "collector_hash_mismatch",
    "collector_signature_unproven", "collector_execution_identity_unbound",
    "cim_materialization_bound_unproven", "collection_safety_unproven",
})
DIAGNOSTIC_REASON_CODES = frozenset({
    "windows_x64_mismatch", "secure_boot_unproven", "dell_manufacturer_mismatch",
    "system_memory_mismatch", "available_memory_invalid", "baseboard_mismatch",
    "cpu_sku_mismatch", "cpu_topology_invalid", "vpro_enterprise_unproven",
    "cpu_features_unproven", "memory_module_capacity_mismatch",
    "memory_ddr5_5600_mismatch", "duplicate_memory_slot",
    "intel_gpu_identity_mismatch", "integrated_gpu_unproven",
    "discrete_gpu_absence_unproven", "duplicate_gpu_device",
    "separate_gpu_detected", "duplicate_display_driver",
    "display_driver_binding_unproven", "gpu_shared_memory_unproven",
    "bios_evidence_incomplete", "fixed_disk_ntfs_unproven", "pagefile_unproven",
    "power_plan_unproven", "vulkan_runtime_absent", "opencl_runtime_absent",
    "directml_runtime_absent",
})
ALLOWED_REASON_CODES = TRUST_GATE_REASON_CODES | DIAGNOSTIC_REASON_CODES


class HardwareReceiptError(ValueError):
    """The hardware receipt is malformed or internally contradictory."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HardwareReceiptError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_json_integer(lexeme: str) -> int:
    """Convert only a small JSON integer lexeme, before Python conversion."""

    if not isinstance(lexeme, str) or len(lexeme) > MAX_JSON_INTEGER_DIGITS:
        raise HardwareReceiptError("integer literal exceeds bounded JSON limit")
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", lexeme) is None:
        raise HardwareReceiptError("JSON integer literal is malformed")
    try:
        return int(lexeme, 10)
    except (ValueError, OverflowError) as exc:
        raise HardwareReceiptError("JSON integer literal is invalid") from exc


def _reject_json_number(lexeme: str) -> None:
    del lexeme
    raise HardwareReceiptError("floating-point JSON numbers are not allowed")


def parse_hardware_receipt(data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise HardwareReceiptError("hardware receipt bytes are malformed")
    data = bytes(data)
    if not data or len(data) > MAX_RECEIPT_BYTES:
        raise HardwareReceiptError("hardware receipt byte bound violated")
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_int=_bounded_json_integer,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except HardwareReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise HardwareReceiptError("hardware receipt is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise HardwareReceiptError("hardware receipt must be one object")
    return value


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HardwareReceiptError(f"{label} keys are not exact")
    return value


def _array(value: Any, label: str, maximum: int, *, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise HardwareReceiptError(f"{label} count is outside bounds")
    return value


def _string(value: Any, label: str, *, maximum: int = MAX_STRING_CHARS, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise HardwareReceiptError(f"{label} is not a bounded string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise HardwareReceiptError(f"{label} is not valid Unicode scalar text") from exc
    if CONTROL.search(value):
        raise HardwareReceiptError(f"{label} contains control characters")
    if PATHISH.search(value) and "pnp_device_id" not in label:
        raise HardwareReceiptError(f"{label} contains a path")
    return value


def _integer(value: Any, label: str, low: int, high: int, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise HardwareReceiptError(f"{label} is outside bounds")
    return value


def _boolean(value: Any, label: str, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if not isinstance(value, bool):
        raise HardwareReceiptError(f"{label} is not boolean")
    return value


def _case(value: Any) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _timestamp(value: Any, label: str, *, nullable: bool = False) -> str | None:
    value = _string(value, label, maximum=32, nullable=nullable)
    if value is None:
        return None
    if not UTC.fullmatch(value):
        raise HardwareReceiptError(f"{label} is not canonical UTC")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HardwareReceiptError(f"{label} is invalid") from exc
    return value


def _reject_sensitive_tree(value: Any, label: str = "receipt") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in SENSITIVE_KEYS:
                raise HardwareReceiptError(f"{label} contains forbidden sensitive key")
            _reject_sensitive_tree(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_tree(item, f"{label}[{index}]")
    elif isinstance(value, str):
        _string(value, label)


def _validate_shape(receipt: dict[str, Any]) -> None:
    _exact(receipt, TOP_KEYS, "receipt")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["receipt_kind"] != RECEIPT_KIND:
        raise HardwareReceiptError("hardware receipt schema/kind is not exact")
    _timestamp(receipt["generated_at_utc"], "generated_at_utc")
    _boolean(receipt["executed_on_target"], "executed_on_target")
    _boolean(receipt["fixture"], "fixture")

    collector = _exact(receipt["collector"], COLLECTOR_KEYS, "collector")
    manifest_id = _string(collector["manifest_id"], "collector.manifest_id", maximum=64)
    if MANIFEST_ID.fullmatch(manifest_id) is None:
        raise HardwareReceiptError("collector.manifest_id is not a valid identifier")
    for key in ("script_sha256", "expected_script_sha256"):
        if not isinstance(collector[key], str) or SHA256.fullmatch(collector[key]) is None:
            raise HardwareReceiptError(f"collector.{key} is not SHA-256")
    _boolean(collector["authenticode_required"], "collector.authenticode_required")
    if collector["authenticode_revocation_policy"] != "offline-no-revocation-check":
        raise HardwareReceiptError("authenticode revocation policy is not explicit")
    if not isinstance(collector["authenticode_status"], str) or collector["authenticode_status"] not in {"Valid", "NotSigned", "HashMismatch", "Unavailable", "Error"}:
        raise HardwareReceiptError("collector.authenticode_status is invalid")
    if collector["signer_subject_sha256"] is not None and (not isinstance(collector["signer_subject_sha256"], str) or SHA256.fullmatch(collector["signer_subject_sha256"]) is None):
        raise HardwareReceiptError("collector signer digest is invalid")
    _boolean(collector["execution_identity_bound"], "collector.execution_identity_bound")
    _boolean(collector["cim_materialization_bound_proven"], "collector.cim_materialization_bound_proven")

    collection = _exact(receipt["collection"], COLLECTION_KEYS, "collection")
    for key in COLLECTION_KEYS:
        _boolean(collection[key], f"collection.{key}")
    expected = _exact(receipt["expected"], EXPECTED_KEYS, "expected")
    if expected != EXPECTED:
        raise HardwareReceiptError("expected target profile is not immutable")

    observed = _exact(receipt["observed"], OBSERVED_KEYS, "observed")
    os_info = _exact(observed["os"], OS_KEYS, "observed.os")
    for key in ("product", "version", "build", "architecture"):
        _string(os_info[key], f"observed.os.{key}", maximum=128, nullable=True)
    _integer(os_info["sku"], "observed.os.sku", 0, 1000, nullable=True)
    _boolean(os_info["secure_boot"], "observed.os.secure_boot", nullable=True)
    computer = _exact(observed["computer"], COMPUTER_KEYS, "observed.computer")
    for key in ("manufacturer", "model", "system_type"):
        _string(computer[key], f"observed.computer.{key}", maximum=128, nullable=True)
    _integer(computer["total_memory_bytes"], "observed.computer.total_memory_bytes", 1, 2**50, nullable=True)
    _integer(computer["available_memory_bytes"], "observed.computer.available_memory_bytes", 0, 2**50, nullable=True)

    cpus = _array(observed["cpu"], "observed.cpu", 4)
    for index, value in enumerate(cpus):
        item = _exact(value, CPU_KEYS, f"cpu[{index}]")
        _integer(item["slot_index"], f"cpu[{index}].slot_index", 0, 3)
        for key in ("name", "manufacturer", "architecture", "vpro_evidence_source"):
            _string(item[key], f"cpu[{index}].{key}", maximum=128, nullable=True)
        for key, low, high in (("address_width", 32, 64), ("physical_cores", 1, 256), ("logical_processors", 1, 512), ("max_clock_mhz", 1, 100000)):
            _integer(item[key], f"cpu[{index}].{key}", low, high, nullable=True)
        for key in ("virtualization_firmware", "second_level_address_translation", "vpro_enterprise"):
            _boolean(item[key], f"cpu[{index}].{key}", nullable=True)
        features = _exact(item["features"], CPU_FEATURE_KEYS, f"cpu[{index}].features")
        for key in CPU_FEATURE_KEYS:
            _boolean(features[key], f"cpu[{index}].features.{key}", nullable=True)

    modules = _array(observed["memory_modules"], "observed.memory_modules", 16)
    for index, value in enumerate(modules):
        item = _exact(value, MEMORY_KEYS, f"memory_modules[{index}]")
        _integer(item["slot_index"], f"memory_modules[{index}].slot_index", 0, 15)
        for key in ("capacity_bytes",):
            _integer(item[key], f"memory_modules[{index}].{key}", 1, 2**50, nullable=True)
        for key in ("speed_mts", "configured_speed_mts"):
            _integer(item[key], f"memory_modules[{index}].{key}", 1, 100000, nullable=True)
        _integer(item["smbios_memory_type"], f"memory_modules[{index}].smbios_memory_type", 0, 255, nullable=True)
        _integer(item["data_width"], f"memory_modules[{index}].data_width", 1, 1024, nullable=True)

    boards = _array(observed["baseboards"], "observed.baseboards", 4)
    for index, value in enumerate(boards):
        item = _exact(value, BOARD_KEYS, f"baseboards[{index}]")
        for key in BOARD_KEYS:
            _string(item[key], f"baseboards[{index}].{key}", maximum=128, nullable=True)
    bios_items = _array(observed["bios"], "observed.bios", 4)
    for index, value in enumerate(bios_items):
        item = _exact(value, BIOS_KEYS, f"bios[{index}]")
        _string(item["manufacturer"], f"bios[{index}].manufacturer", maximum=128, nullable=True)
        _string(item["version"], f"bios[{index}].version", maximum=128, nullable=True)
        _timestamp(item["release_utc"], f"bios[{index}].release_utc", nullable=True)
        _boolean(item["secure_boot"], f"bios[{index}].secure_boot", nullable=True)

    adapters = _array(observed["gpu_adapters"], "observed.gpu_adapters", 8)
    for index, value in enumerate(adapters):
        item = _exact(value, GPU_KEYS, f"gpu_adapters[{index}]")
        _integer(item["slot_index"], f"gpu_adapters[{index}].slot_index", 0, 7)
        for key in ("name", "vendor", "pnp_device_id", "driver_version", "status"):
            _string(item[key], f"gpu_adapters[{index}].{key}", maximum=256, nullable=True)
        if item["pnp_device_id"] is not None and PCI_TUPLE.fullmatch(item["pnp_device_id"]) is None:
            raise HardwareReceiptError(f"gpu_adapters[{index}].pnp_device_id is not a normalized PCI tuple")
        _timestamp(item["driver_date_utc"], f"gpu_adapters[{index}].driver_date_utc", nullable=True)
        _integer(item["adapter_ram_bytes"], f"gpu_adapters[{index}].adapter_ram_bytes", 0, 2**50, nullable=True)
        _boolean(item["integrated"], f"gpu_adapters[{index}].integrated", nullable=True)
    drivers = _array(observed["display_drivers"], "observed.display_drivers", 16)
    for index, value in enumerate(drivers):
        item = _exact(value, DRIVER_KEYS, f"display_drivers[{index}]")
        _integer(item["slot_index"], f"display_drivers[{index}].slot_index", 0, 15)
        for key in ("name", "pnp_device_id", "driver_version", "manufacturer", "signer_name"):
            _string(item[key], f"display_drivers[{index}].{key}", maximum=256, nullable=True)
        if item["pnp_device_id"] is not None and PCI_TUPLE.fullmatch(item["pnp_device_id"]) is None:
            raise HardwareReceiptError(f"display_drivers[{index}].pnp_device_id is not a normalized PCI tuple")
        _timestamp(item["driver_date_utc"], f"display_drivers[{index}].driver_date_utc", nullable=True)
        _boolean(item["signed"], f"display_drivers[{index}].signed", nullable=True)

    gpu_memory = _exact(observed["gpu_memory"], GPU_MEMORY_KEYS, "observed.gpu_memory")
    _integer(gpu_memory["shared_capacity_bytes"], "gpu_memory.shared_capacity_bytes", 0, 2**50, nullable=True)
    _integer(gpu_memory["dedicated_capacity_bytes"], "gpu_memory.dedicated_capacity_bytes", 0, 2**50, nullable=True)
    _string(gpu_memory["source"], "gpu_memory.source", maximum=64)
    disks = _array(observed["disk_volumes"], "observed.disk_volumes", 16)
    for index, value in enumerate(disks):
        item = _exact(value, DISK_KEYS, f"disk_volumes[{index}]")
        _integer(item["slot_index"], f"disk_volumes[{index}].slot_index", 0, 15)
        _string(item["filesystem"], f"disk_volumes[{index}].filesystem", maximum=32, nullable=True)
        _integer(item["size_bytes"], f"disk_volumes[{index}].size_bytes", 1, 2**60, nullable=True)
        _integer(item["free_bytes"], f"disk_volumes[{index}].free_bytes", 0, 2**60, nullable=True)
        _boolean(item["compressed"], f"disk_volumes[{index}].compressed", nullable=True)
    pagefile = _exact(observed["pagefile"], PAGEFILE_KEYS, "observed.pagefile")
    for key in PAGEFILE_KEYS:
        _integer(pagefile[key], f"observed.pagefile.{key}", 0, 2**50, nullable=True)
    power = _exact(observed["power_plan"], POWER_KEYS, "observed.power_plan")
    _boolean(power["active"], "power_plan.active", nullable=True)
    _string(power["scheme_guid"], "power_plan.scheme_guid", maximum=36, nullable=True)
    _string(power["name"], "power_plan.name", maximum=128, nullable=True)
    runtimes = _exact(observed["runtimes"], RUNTIME_KEYS, "observed.runtimes")
    for runtime_name, value in runtimes.items():
        item = _exact(value, RUNTIME_ITEM_KEYS, f"runtime.{runtime_name}")
        _boolean(item["present"], f"runtime.{runtime_name}.present")
        _string(item["version"], f"runtime.{runtime_name}.version", maximum=64, nullable=True)
        if item["source"] != "system32-fixed-name-presence-only":
            raise HardwareReceiptError(f"runtime.{runtime_name}.source is invalid")

    checks = _exact(receipt["checks"], CHECKS_KEYS, "checks")
    _boolean(checks["hardware_match"], "checks.hardware_match")
    reasons = _array(checks["reason_codes"], "checks.reason_codes", 64)
    if any(not isinstance(reason, str) for reason in reasons):
        raise HardwareReceiptError("reason codes contain a non-string")
    if reasons != sorted(set(reasons)):
        raise HardwareReceiptError("reason codes must be unique and sorted")
    for index, reason in enumerate(reasons):
        if (not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None
                or reason not in ALLOWED_REASON_CODES):
            raise HardwareReceiptError(f"reason code {index} is invalid")
    verdict = _exact(receipt["verdict"], VERDICT_KEYS, "verdict")
    if verdict["status"] != "NOT_READY":
        raise HardwareReceiptError("verdict status is invalid")
    if verdict["target_evidence_accepted"] is not False:
        raise HardwareReceiptError("target evidence acceptance is disabled")
    _reject_sensitive_tree(receipt)


def hardware_reason_codes(receipt: dict[str, Any]) -> list[str]:
    try:
        return _hardware_reason_codes(receipt)
    except HardwareReceiptError:
        raise
    except (AttributeError, IndexError, KeyError, OverflowError, TypeError) as exc:
        raise HardwareReceiptError("hardware receipt evidence has malformed types") from exc


def _hardware_reason_codes(receipt: dict[str, Any]) -> list[str]:
    """Validate exact shape and return deterministic target mismatch codes."""

    _validate_shape(receipt)
    # No reviewed identity-pinned collector/validator trust anchor is active in
    # the product. A syntactically perfect JSON object is not authority, but
    # bounded diagnostic evidence still receives only observed reason codes.
    reasons: set[str] = {"target_trust_anchor_unavailable"}
    collector = receipt["collector"]
    collection = receipt["collection"]
    observed = receipt["observed"]
    if receipt["fixture"] is not False:
        reasons.add("fixture_not_target_evidence")
    if receipt["executed_on_target"] is not True:
        reasons.add("not_executed_on_target")
    if collector["script_sha256"] != collector["expected_script_sha256"]:
        reasons.add("collector_hash_mismatch")
    if collector["authenticode_required"] is not True or collector["authenticode_status"] != "Valid":
        reasons.add("collector_signature_unproven")
    if collector["execution_identity_bound"] is not True:
        reasons.add("collector_execution_identity_unbound")
    if collector["cim_materialization_bound_proven"] is not True:
        reasons.add("cim_materialization_bound_unproven")
    expected_collection = {
        "read_only": True, "offline": True, "stdout_only": True,
        "admin_required": False, "wmi_mutation": False, "downloads": False,
        "installs": False, "model_loaded": False, "secrets_collected": False,
    }
    if collection != expected_collection:
        reasons.add("collection_safety_unproven")

    os_info = observed["os"]
    if not isinstance(os_info["product"], str) or not isinstance(os_info["architecture"], str) or "windows" not in os_info["product"].casefold() or os_info["architecture"].casefold() not in {"x64", "amd64", "64-bit", "64 bit"}:
        reasons.add("windows_x64_mismatch")
    if os_info["secure_boot"] is not True:
        reasons.add("secure_boot_unproven")
    computer = observed["computer"]
    if not isinstance(computer["manufacturer"], str) or re.fullmatch(r"Dell(?: Inc\.)?", computer["manufacturer"], re.IGNORECASE) is None:
        reasons.add("dell_manufacturer_mismatch")
    if computer["total_memory_bytes"] != TARGET_MEMORY_BYTES:
        reasons.add("system_memory_mismatch")
    if not isinstance(computer["available_memory_bytes"], int) or not isinstance(computer["total_memory_bytes"], int) or computer["available_memory_bytes"] > computer["total_memory_bytes"]:
        reasons.add("available_memory_invalid")

    boards = observed["baseboards"]
    if len(boards) != 1 or _case(boards[0]["product"]) != TARGET_BOARD.casefold() or _case(boards[0]["revision"]) != TARGET_BOARD_REVISION.casefold():
        reasons.add("baseboard_mismatch")
    cpus = observed["cpu"]
    if len(cpus) != 1 or CPU_NAME.search(cpus[0]["name"] or "") is None or "intel" not in _case(cpus[0]["manufacturer"]):
        reasons.add("cpu_sku_mismatch")
    elif _case(cpus[0]["architecture"]) not in {"x64", "amd64", "9"} or cpus[0]["address_width"] != 64 or not isinstance(cpus[0]["logical_processors"], int) or not isinstance(cpus[0]["physical_cores"], int) or cpus[0]["logical_processors"] < cpus[0]["physical_cores"]:
        reasons.add("cpu_topology_invalid")
    if len(cpus) == 1:
        if cpus[0]["vpro_enterprise"] is not True:
            reasons.add("vpro_enterprise_unproven")
        if any(cpus[0]["features"][key] is not True for key in CPU_FEATURE_KEYS):
            reasons.add("cpu_features_unproven")

    modules = observed["memory_modules"]
    if any(not isinstance(item["capacity_bytes"], int) for item in modules) or sum(item["capacity_bytes"] or 0 for item in modules) != TARGET_MEMORY_BYTES:
        reasons.add("memory_module_capacity_mismatch")
    if any(item["configured_speed_mts"] != TARGET_MEMORY_SPEED or item["smbios_memory_type"] != 34 for item in modules):
        reasons.add("memory_ddr5_5600_mismatch")
    if len({item["slot_index"] for item in modules}) != len(modules):
        reasons.add("duplicate_memory_slot")

    adapters = observed["gpu_adapters"]
    if len({_case(item["pnp_device_id"]) for item in adapters}) != len(adapters):
        reasons.add("duplicate_gpu_device")
    if len(adapters) > 1 and any(
        not isinstance(item["pnp_device_id"], str)
        or PCI_TUPLE.fullmatch(item["pnp_device_id"]) is None
        for item in adapters
    ):
        # An additional adapter without a canonical identity cannot be
        # classified as integrated, discrete, or an excluded software device.
        # Do not infer absence of a discrete GPU from that incomplete record.
        reasons.add("discrete_gpu_absence_unproven")
    intel = [item for item in adapters if PNP_INTEL.fullmatch(item["pnp_device_id"] or "") and GPU_NAME.search(item["name"] or "") and item["driver_version"] == TARGET_GPU_DRIVER]
    if len(intel) != 1:
        reasons.add("intel_gpu_identity_mismatch")
    elif intel[0]["integrated"] is not True:
        reasons.add("integrated_gpu_unproven")
    if any(isinstance(item["pnp_device_id"], str) and item["pnp_device_id"].upper().startswith("PCI\\") and "VEN_8086" not in item["pnp_device_id"].upper() for item in adapters):
        reasons.add("separate_gpu_detected")
    drivers = observed["display_drivers"]
    if len({_case(item["pnp_device_id"]) for item in drivers}) != len(drivers):
        reasons.add("duplicate_display_driver")
    if len(intel) == 1 and not any(_case(item["pnp_device_id"]) == _case(intel[0]["pnp_device_id"]) and item["driver_version"] == TARGET_GPU_DRIVER and item["signed"] is True for item in drivers):
        reasons.add("display_driver_binding_unproven")
    gpu_memory = observed["gpu_memory"]
    if gpu_memory["shared_capacity_bytes"] is None or gpu_memory["source"] != "identity-pinned-dxgi":
        reasons.add("gpu_shared_memory_unproven")

    if not observed["bios"] or any(item["secure_boot"] is not True for item in observed["bios"]):
        reasons.add("bios_evidence_incomplete")
    if not observed["disk_volumes"] or any(_case(item["filesystem"]) != "ntfs" or not isinstance(item["free_bytes"], int) or not isinstance(item["size_bytes"], int) or item["free_bytes"] > item["size_bytes"] for item in observed["disk_volumes"]):
        reasons.add("fixed_disk_ntfs_unproven")
    if any(observed["pagefile"][key] is None for key in PAGEFILE_KEYS):
        reasons.add("pagefile_unproven")
    if observed["power_plan"]["active"] is not True or observed["power_plan"]["scheme_guid"] is None:
        reasons.add("power_plan_unproven")
    for runtime_name, runtime in observed["runtimes"].items():
        if runtime["present"] is not True:
            reasons.add(f"{runtime_name}_runtime_absent")
    # Validate producer output before set membership/sorting. This is kept
    # separate from shape validation so malformed generated values fail with
    # the typed diagnostic error rather than leaking through a set operation.
    if any(not isinstance(reason, str) for reason in reasons):
        raise HardwareReceiptError("validator produced a non-string reason code")
    if not reasons.issubset(ALLOWED_REASON_CODES):
        raise HardwareReceiptError("validator produced an unknown reason code")
    if any(not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None for reason in reasons):
        raise HardwareReceiptError("validator produced a malformed reason code")
    return sorted(reasons)


def validate_hardware_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    reasons = hardware_reason_codes(receipt)
    match = not reasons
    claimed_reasons = receipt["checks"]["reason_codes"]
    claimed_match = receipt["checks"]["hardware_match"]
    claimed_status = receipt["verdict"]["status"]
    claimed_accepted = receipt["verdict"]["target_evidence_accepted"]
    expected_status = "HARDWARE_MATCH" if match else "NOT_READY"
    if claimed_reasons != reasons or claimed_match is not match or claimed_status != expected_status or claimed_accepted is not match:
        raise HardwareReceiptError("receipt verdict contradicts validated evidence")
    return {"schema": "local_bmo.windows-hardware-verdict.v1", "status": expected_status, "target_evidence_accepted": match, "reason_codes": reasons}
