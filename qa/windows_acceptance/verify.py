#!/usr/bin/env python3
"""Fail-closed verifier for exact Dell clean-machine acceptance receipts.

This verifier does not turn fixture declarations into evidence.  READY requires
a non-fixture receipt generated on Windows, exact target identity, successful
CPU/portable checks, a recorded Vulkan disposition, and separately consented
synthetic live checks for every full-access capability.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID

SCHEMA = "local_bmo.windows-clean-machine-acceptance.v1"
MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
MODEL_SIZE = 5629109088
MODEL_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
LLAMA_CPP_REVISION = "3581ba0cf591b3f772fbb002de0f70e294bc0396"
NODE_VERSION = "v24.20.0"
NODE_SHA256 = "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"
TARGET_BOARD = "039NNG"
TARGET_BOARD_REVISION = "A00"
TARGET_DRIVER = "32.0.101.8247"
TARGET_MEMORY_BYTES = 32 * 1024**3
TARGET_MEMORY_SPEED = 5600
MIN_AVAILABLE_MEMORY = 12 * 1024**3
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PNP_INTEL = re.compile(r"(?i)^PCI\\.*VEN_8086&DEV_[0-9A-F]{4}")
CPU_NAME = re.compile(r"(?i)Intel.*Core.*Ultra\s*7")
GPU_NAME = re.compile(r"(?i)Intel.*Graphics")
WINDOWS_X64 = {"x64", "amd64", "64-bit", "64 bit"}
REQUIRED_LIVE_CHECKS = (
    "mail.read_message",
    "teams.list_messages",
    "mail.create_draft",
    "mail.send_draft",
    "teams.send_message",
    "app.open.outlook",
    "browser.open_url",
    "browser.fill_field",
    "coding.copilot_ask",
)


class ReceiptError(ValueError):
    """Receipt is malformed or fails an acceptance invariant."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_receipt_bytes(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptError("receipt exceeds the 2 MiB bound")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptError("receipt must be one JSON object")
    return value


def load_receipt(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReceiptError("receipt must be a regular non-link file")
    try:
        return _parse_receipt_bytes(path.read_bytes())
    except OSError as exc:
        raise ReceiptError("receipt could not be read") from exc


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _true(value: Any) -> bool:
    return value is True


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        return True
    except ValueError:
        return False


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _exact_keys(value: dict[str, Any], keys: set[str], label: str, reasons: list[str]) -> None:
    observed = set(value)
    if observed != keys:
        missing = sorted(keys - observed)
        unknown = sorted(observed - keys)
        reasons.append(f"{label} keys mismatch (missing={missing}, unknown={unknown})")


def _hardware_reasons(hardware: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if hardware.get("schema_version") != "1.1.0" or hardware.get("receipt_kind") != "windows-hardware-receipt":
        reasons.append("hardware receipt schema/kind is not exact")
    collection = _mapping(hardware.get("collection"))
    if not (_true(collection.get("read_only")) and collection.get("admin_required") is False and collection.get("network_changed") is False and collection.get("secrets_collected") is False):
        reasons.append("hardware collection is not read-only/no-admin/no-network/no-secret")
    safety = _mapping(hardware.get("safety"))
    if not all(_true(safety.get(key)) for key in ("no_environment_dump", "no_credentials", "no_network_changes", "no_driver_install", "no_model_access")):
        reasons.append("hardware receipt safety declarations are incomplete")

    operating_system = _mapping(hardware.get("os"))
    if "windows" not in str(operating_system.get("caption", "")).lower():
        reasons.append("OS is not Windows")
    if str(operating_system.get("architecture", "")).strip().lower() not in WINDOWS_X64:
        reasons.append("OS is not x64/AMD64")
    if not all(str(operating_system.get(key, "")).strip() for key in ("version", "build")):
        reasons.append("exact OS version/build is absent")

    computer = _mapping(hardware.get("computer"))
    if re.match(r"(?i)^Dell(?: Inc\.)?$", str(computer.get("manufacturer", "")).strip()) is None:
        reasons.append("computer manufacturer is not Dell")
    if not str(computer.get("model", "")).strip() or not str(computer.get("system_type", "")).strip():
        reasons.append("exact Dell model/system type is absent")
    total = _integer(computer.get("total_memory_bytes"))
    available = _integer(computer.get("available_memory_bytes"))
    if total is None or not 31 * 1024**3 <= total <= TARGET_MEMORY_BYTES:
        reasons.append("system-visible memory is outside the reported 32 GiB envelope")
    if available is None or available < MIN_AVAILABLE_MEMORY or total is None or available > total:
        reasons.append("available memory does not prove the 12 GiB launch floor")

    boards = [item for item in _list(hardware.get("baseboards")) if isinstance(item, dict)]
    if not any(str(item.get("product", "")).upper() == TARGET_BOARD and str(item.get("version", "")).upper() == TARGET_BOARD_REVISION for item in boards):
        reasons.append("baseboard is not 039NNG revision A00")

    modules = [item for item in _list(hardware.get("memory_modules")) if isinstance(item, dict)]
    capacities = [_integer(item.get("capacity_bytes")) for item in modules]
    if not modules or any(value is None or value <= 0 for value in capacities) or sum(value or 0 for value in capacities) != TARGET_MEMORY_BYTES:
        reasons.append("DIMM capacity receipt does not total exactly 32 GiB")
    if any(_integer(item.get("configured_speed_mts")) != TARGET_MEMORY_SPEED for item in modules):
        reasons.append("every populated DIMM is not configured at 5600 MT/s")
    if any(_integer(item.get("smbios_memory_type")) != 34 for item in modules):
        reasons.append("every populated DIMM is not reported as DDR5 (SMBIOS type 34)")

    cpus = [item for item in _list(hardware.get("cpu")) if isinstance(item, dict)]
    if len(cpus) != 1 or CPU_NAME.search(str(cpus[0].get("name", ""))) is None:
        reasons.append("CPU does not prove the reported Intel Core Ultra 7 family")
    elif (not all(str(cpus[0].get(key, "")).strip() for key in ("device_id", "processor_id")) or _integer(cpus[0].get("address_width")) != 64 or
          _integer(cpus[0].get("physical_cores")) is None or _integer(cpus[0].get("logical_processors")) is None or cpus[0]["physical_cores"] < 1 or cpus[0]["logical_processors"] < cpus[0]["physical_cores"]):
        reasons.append("exact CPU identifier/x64 receipt is incomplete")

    adapters = [item for item in _list(hardware.get("gpu_adapters")) if isinstance(item, dict)]
    matching = [item for item in adapters if item.get("is_intel") is True and GPU_NAME.search(str(item.get("name", ""))) and PNP_INTEL.search(str(item.get("pnp_device_id", ""))) and str(item.get("driver_version", "")) == TARGET_DRIVER]
    if len(matching) != 1:
        reasons.append("exactly one Intel Graphics PNP adapter on driver 32.0.101.8247 was not proven")
    other_pci_graphics = [item for item in adapters if str(item.get("pnp_device_id", "")).upper().startswith("PCI\\") and "VEN_8086" not in str(item.get("pnp_device_id", "")).upper()]
    if other_pci_graphics:
        reasons.append("a separate non-Intel PCI graphics adapter was detected")
    pnp = [item for item in _list(hardware.get("display_pnp_entities")) if isinstance(item, dict)]
    if matching and not any(str(item.get("pnp_device_id", "")).casefold() == str(matching[0].get("pnp_device_id", "")).casefold() for item in pnp):
        reasons.append("display PNP entity does not correlate to the Intel graphics adapter")

    vulkan = _mapping(hardware.get("vulkan"))
    enumeration = _mapping(vulkan.get("enumeration"))
    primary = _mapping(enumeration.get("primary_device"))
    if not (_true(vulkan.get("loader_present")) and enumeration.get("exit_code") == 0 and enumeration.get("timed_out") is False and enumeration.get("output_truncated") is False and _true(enumeration.get("explicitly_pinned")) and SHA256.fullmatch(str(enumeration.get("command_sha256", ""))) and SHA256.fullmatch(str(enumeration.get("summary_sha256", ""))) and str(enumeration.get("summary", "")).strip()):
        reasons.append("pinned successful bounded Vulkan enumeration is absent")
    if str(primary.get("vendor_id", "")).lower() not in {"0x8086", "8086"} or GPU_NAME.search(str(primary.get("name", ""))) is None or not str(primary.get("device_id", "")).strip() or not str(primary.get("api_version", "")).strip() or "INTEGRATED_GPU" not in str(primary.get("device_type", "")).upper():
        reasons.append("Vulkan primary-device identity/capability fields do not prove the Intel iGPU")
    if matching:
        pnp_device = re.search(r"(?i)&DEV_([0-9A-F]{4})", str(matching[0].get("pnp_device_id", "")))
        vulkan_device = str(primary.get("device_id", "")).lower().removeprefix("0x").zfill(4)
        if pnp_device is None or pnp_device.group(1).lower() != vulkan_device:
            reasons.append("Vulkan device ID is not correlated to the Windows PNP device ID")
    return reasons


def evaluate_receipt(receipt: dict[str, Any], *, hardware_source_bytes: bytes | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    hardware_binding_valid = False
    if hardware_source_bytes is not None:
        try:
            source_hardware = _parse_receipt_bytes(hardware_source_bytes)
            hardware_binding_valid = hashlib.sha256(hardware_source_bytes).hexdigest() == receipt.get("hardware_receipt_sha256") and source_hardware == receipt.get("hardware")
        except ReceiptError:
            hardware_binding_valid = False
    _exact_keys(receipt, {"schema", "run_id", "captured_at_utc", "executed_on_target", "fixture", "hardware_receipt_sha256", "hardware", "artifacts", "checks", "live_actions", "safety"}, "receipt", reasons)
    if receipt.get("schema") != SCHEMA:
        reasons.append("acceptance schema is not exact")
    if not _uuid(receipt.get("run_id")):
        reasons.append("run_id is not a UUID")
    if not _timestamp(receipt.get("captured_at_utc")):
        reasons.append("captured_at_utc is invalid")
    if receipt.get("executed_on_target") is not True:
        reasons.append("receipt was not executed on the target")
    if receipt.get("fixture") is not False:
        reasons.append("fixture/simulated receipts can never be READY")
    if SHA256.fullmatch(str(receipt.get("hardware_receipt_sha256", ""))) is None:
        reasons.append("hardware receipt hash is absent")
    if not hardware_binding_valid:
        reasons.append("embedded hardware receipt is not bound to the separately hashed source receipt")

    hardware = _mapping(receipt.get("hardware"))
    reasons.extend(_hardware_reasons(hardware))

    artifacts = _mapping(receipt.get("artifacts"))
    _exact_keys(artifacts, {"release_manifest_sha256", "release_verified", "host_config_sha256", "cpu_engine_sha256", "vulkan_engine_sha256", "model_name", "model_size_bytes", "model_sha256", "llama_cpp_revision", "node_version", "node_sha256"}, "artifacts", reasons)
    for key in ("release_manifest_sha256", "cpu_engine_sha256", "vulkan_engine_sha256"):
        if SHA256.fullmatch(str(artifacts.get(key, ""))) is None:
            reasons.append(f"{key} is not a SHA-256")
    if artifacts.get("release_verified") is not True:
        reasons.append("release manifest/checksums were not verified")
    if (artifacts.get("model_name"), artifacts.get("model_size_bytes"), artifacts.get("model_sha256")) != (MODEL_NAME, MODEL_SIZE, MODEL_SHA256):
        reasons.append("model artifact identity is not the immutable product identity")
    if artifacts.get("llama_cpp_revision") != LLAMA_CPP_REVISION:
        reasons.append("llama.cpp revision is not the pinned product revision")
    if (artifacts.get("node_version"), artifacts.get("node_sha256")) != (NODE_VERSION, NODE_SHA256):
        reasons.append("packaged Node runtime identity is not exact")

    checks = _mapping(receipt.get("checks"))
    _exact_keys(checks, {"cpu", "vulkan_candidate", "portable"}, "checks", reasons)
    cpu = _mapping(checks.get("cpu"))
    _exact_keys(cpu, {"status", "backend", "model_loaded", "bounded_chat", "cancellation", "clean_exit", "no_orphan", "loopback_only", "offline"}, "CPU check", reasons)
    if cpu.get("status") != "PASS" or cpu.get("backend") != f"llama.cpp/{LLAMA_CPP_REVISION[:8]}/cpu":
        reasons.append("mandatory CPU product path did not pass with the exact backend")
    for key in ("model_loaded", "bounded_chat", "cancellation", "clean_exit", "no_orphan", "loopback_only", "offline"):
        if cpu.get(key) is not True:
            reasons.append(f"CPU check did not prove {key}")

    vulkan_check = _mapping(checks.get("vulkan_candidate"))
    _exact_keys(vulkan_check, {"attempted", "status", "promoted", "reason", "cpu_fallback_used", "backend", "pnp_device_id", "driver_version", "device_name", "gpu_layers", "no_silent_fallback", "model_loaded", "bounded_chat", "terminated_no_orphan"}, "Vulkan candidate check", reasons)
    if vulkan_check.get("attempted") is not True or vulkan_check.get("status") not in {"PASS", "REJECTED_WITH_EVIDENCE"}:
        reasons.append("Vulkan candidate was skipped or lacks an evidence-backed disposition")
    if vulkan_check.get("promoted") is not False:
        reasons.append("this target harness cannot promote the Vulkan candidate")
    matched_gpu = next((item for item in _list(hardware.get("gpu_adapters")) if isinstance(item, dict) and item.get("is_intel") is True and str(item.get("driver_version", "")) == TARGET_DRIVER), {})
    if str(vulkan_check.get("pnp_device_id", "")).casefold() != str(matched_gpu.get("pnp_device_id", "")).casefold() or vulkan_check.get("driver_version") != TARGET_DRIVER:
        reasons.append("Vulkan candidate receipt is not correlated to the exact PNP adapter/driver")
    if vulkan_check.get("status") == "PASS":
        if vulkan_check.get("backend") != f"llama.cpp/{LLAMA_CPP_REVISION[:8]}/vulkan" or vulkan_check.get("no_silent_fallback") is not True or vulkan_check.get("model_loaded") is not True or vulkan_check.get("bounded_chat") is not True or vulkan_check.get("terminated_no_orphan") is not True:
            reasons.append("Vulkan PASS lacks exact backend/load/chat/fallback/termination evidence")
        if not isinstance(vulkan_check.get("gpu_layers"), int) or isinstance(vulkan_check.get("gpu_layers"), bool) or not 1 <= vulkan_check["gpu_layers"] <= 99:
            reasons.append("Vulkan PASS lacks a bounded offload layer count")
    else:
        if not isinstance(vulkan_check.get("reason"), str) or not vulkan_check["reason"].strip() or vulkan_check.get("cpu_fallback_used") is not False:
            reasons.append("Vulkan rejection is not explicit or was hidden by CPU fallback")

    portable = _mapping(checks.get("portable"))
    portable_required = (
        "powershell_foreground", "current_user_pipes", "job_kill_on_close", "launch_gate_after_job_assignment",
        "browser_shell_execute", "fragment_cleared", "bootstrap_query_absent", "bootstrap_referer_rejected",
        "bootstrap_hostile_origin_rejected", "bootstrap_one_shot", "api_bearer_required", "console_secret_scan",
        "graceful_no_orphan", "abrupt_no_orphan",
    )
    _exact_keys(portable, {"status", *portable_required}, "portable check", reasons)
    if portable.get("status") != "PASS" or any(portable.get(key) is not True for key in portable_required):
        reasons.append("portable PowerShell/Job/pipe/browser bootstrap evidence is incomplete")

    safety = _mapping(receipt.get("safety"))
    _exact_keys(safety, {"no_admin", "no_install", "no_download", "no_environment_dump", "no_secret_logging", "no_sensitive_test_data", "loopback_only"}, "acceptance safety", reasons)
    for key in ("no_admin", "no_install", "no_download", "no_environment_dump", "no_secret_logging", "no_sensitive_test_data", "loopback_only"):
        if safety.get(key) is not True:
            reasons.append(f"acceptance safety did not prove {key}")

    core_ready = not reasons
    live_reasons: list[str] = []
    live = _mapping(receipt.get("live_actions"))
    live_keys = {"opt_in", "explicit_consent", "synthetic_accounts_only", "disposable_workspace_only", "secrets_logged", "content_logged", "checks"}
    if set(live) != live_keys:
        live_reasons.append("live-action receipt keys are incomplete or unknown")
    if live.get("opt_in") is not True or live.get("explicit_consent") is not True:
        live_reasons.append("full-access live checks were not explicitly opted into and consented")
    if live.get("synthetic_accounts_only") is not True or live.get("disposable_workspace_only") is not True:
        live_reasons.append("live checks were not restricted to synthetic accounts and a disposable workspace")
    if live.get("secrets_logged") is not False or live.get("content_logged") is not False:
        live_reasons.append("live checks do not attest no-secret/no-content logging")
    if SHA256.fullmatch(str(artifacts.get("host_config_sha256", ""))) is None:
        live_reasons.append("full-access checks lack a hash-bound reviewed host config")
    live_items = _list(live.get("checks"))
    records = {item.get("id"): item for item in live_items if isinstance(item, dict) and isinstance(item.get("id"), str)}
    if len(records) != len(live_items) or set(records) != set(REQUIRED_LIVE_CHECKS):
        live_reasons.append("live checks contain missing, duplicate, or unknown IDs")
    for check_id in REQUIRED_LIVE_CHECKS:
        item = records.get(check_id)
        if not item or set(item) != {"id", "status", "operator_confirmed", "synthetic_target", "observed_at_utc"} or item.get("status") != "PASS" or item.get("operator_confirmed") is not True or item.get("synthetic_target") is not True or not _timestamp(item.get("observed_at_utc")):
            live_reasons.append(f"live check is not proven: {check_id}")
    full_access_ready = core_ready and not live_reasons
    return {
        "schema": "local_bmo.windows-acceptance-verdict.v1",
        "status": "READY" if full_access_ready else "NOT_READY",
        "core_ready": core_ready,
        "full_access_ready": full_access_ready,
        "cpu_disposition": "ACCEPTED" if not any(reason.startswith("mandatory CPU") or reason.startswith("CPU check") for reason in reasons) else "UNPROVEN_OR_REJECTED",
        "vulkan_disposition": vulkan_check.get("status", "UNPROVEN"),
        "reasons": reasons,
        "live_reasons": live_reasons,
        "limitations": ["Vulkan PASS is target candidate evidence only; promotion still requires the full independent correctness/performance/soak gate."],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = load_receipt(args.receipt)
        hardware_path = args.receipt.with_name("hardware-receipt.json")
        if hardware_path.is_symlink() or not hardware_path.is_file():
            raise ReceiptError("hardware receipt must be a regular non-link file")
        verdict = evaluate_receipt(receipt, hardware_source_bytes=hardware_path.read_bytes())
    except (ReceiptError, OSError) as exc:
        verdict = {"schema": "local_bmo.windows-acceptance-verdict.v1", "status": "NOT_READY", "core_ready": False, "full_access_ready": False, "reasons": [str(exc)], "live_reasons": []}
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
