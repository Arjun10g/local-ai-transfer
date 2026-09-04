#!/usr/bin/env python3
"""Fail-closed Windows backend capability and memory planning.

This module is intentionally stdlib-only.  It does not install a compiler,
query a provider, or infer Intel GPU capability from a product name.  A SYCL
plan is valid only when a Windows hardware receipt contains an explicit,
read-only Level Zero/SYCL probe and one unambiguous integrated Intel adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

try:
    repository_root = Path(__file__).resolve().parents[2]
    if (repository_root / "scripts" / "vulkan_source_closure.py").is_file():
        sys.path.insert(0, str(repository_root))
    from scripts.vulkan_source_closure import DEFAULT_MANIFEST, DEFAULT_ROOT, VulkanClosureError, verify_closure
except ModuleNotFoundError:
    # The CPU planner remains usable from the portable tree. Vulkan planning
    # fails closed there until build-provenance closure evidence is packaged.
    DEFAULT_MANIFEST = None
    DEFAULT_ROOT = None
    VulkanClosureError = ValueError
    verify_closure = None

MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
MODEL_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
MODEL_SIZE_BYTES = 5629109088
LLAMA_CPP_REVISION = "3581ba0cf591b3f772fbb002de0f70e294bc0396"
VULKAN_SOURCE_ROOT = DEFAULT_ROOT
VULKAN_SOURCE_MANIFEST = DEFAULT_MANIFEST


def _closure_metadata() -> dict[str, Any] | None:
    """Return verified closure evidence; absence keeps every GPU plan closed."""
    if verify_closure is None or VULKAN_SOURCE_ROOT is None or VULKAN_SOURCE_MANIFEST is None:
        return None
    try:
        return verify_closure(VULKAN_SOURCE_ROOT, VULKAN_SOURCE_MANIFEST)
    except (OSError, VulkanClosureError, ValueError):
        return None


# These are evidence-derived compatibility flags, not operator-controlled
# switches.  The plan path re-verifies the closure so a post-import tamper is
# also refused.
VULKAN_SOURCE_CLOSURE_READY = _closure_metadata() is not None
PRODUCT_VULKAN_PROFILE_READY = VULKAN_SOURCE_CLOSURE_READY
# Closure verification proves source integrity, not target acceptance or
# promotion. Sol must separately promote the Vulkan profile before SYCL can be
# planned as an experimental diagnostic.
PRODUCT_VULKAN_PROFILE_PROMOTED = False
SYCL_BUILD_FLAGS = [
    "-DGGML_SYCL=ON",
    "-DGGML_SYCL_TARGET=INTEL",
    "-DGGML_SYCL_F16=ON",
]
OFFICIAL_LLAMA_SYCL_DOC = "https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md"
OFFICIAL_INTEL_ONEAPI_DOC = "https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/get-started-guide/2025-2/get-started-on-windows.html"
_MAX_RECEIPT_BYTES = 512 * 1024
_MIN_AVAILABLE_RAM_BYTES = 12 * 1024**3


class BackendPlanError(ValueError):
    """A capability, provenance, or policy condition is not proven."""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise BackendPlanError("hardware receipt must be a regular file")
    if path.stat().st_size > _MAX_RECEIPT_BYTES:
        raise BackendPlanError("hardware receipt is too large")
    try:
        def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise BackendPlanError("receipt contains duplicate JSON keys")
                value[key] = item
            return value
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackendPlanError("hardware receipt is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BackendPlanError("hardware receipt must be an object")
    return value


def _receipt_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_common_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("receipt_kind") != "windows-hardware-receipt":
        raise BackendPlanError("receipt kind is not Windows hardware inventory")
    collection = receipt.get("collection")
    safety = receipt.get("safety")
    if not isinstance(collection, dict) or collection.get("read_only") is not True:
        raise BackendPlanError("hardware receipt is not a read-only, no-secret receipt")
    # admin_required is the one intentionally false condition above.
    if collection.get("admin_required") is not False or collection.get("network_changed") is not False or collection.get("secrets_collected") is not False:
        raise BackendPlanError("hardware receipt collection policy is invalid")
    if not isinstance(safety, dict) or any(safety.get(key) is not True for key in (
        "no_environment_dump", "no_credentials", "no_network_changes", "no_driver_install", "no_model_access"
    )):
        raise BackendPlanError("hardware receipt safety claims are incomplete")
    operating_system = receipt.get("os")
    if not isinstance(operating_system, dict) or str(operating_system.get("caption", "")).lower().find("windows") < 0:
        raise BackendPlanError("receipt does not prove Windows")
    architecture = str(operating_system.get("architecture", "")).strip().lower()
    if architecture not in {"x64", "amd64", "64-bit", "64 bit"}:
        raise BackendPlanError("receipt does not prove Windows x64/AMD64")
    computer = receipt.get("computer")
    if not isinstance(computer, dict) or isinstance(computer.get("total_memory_bytes"), bool) or not isinstance(computer.get("total_memory_bytes"), int):
        raise BackendPlanError("receipt has no total physical memory")
    if computer["total_memory_bytes"] < 24 * 1024**3:
        raise BackendPlanError("at least 24 GiB physical memory is required")
    available = computer.get("available_memory_bytes")
    if isinstance(available, bool) or not isinstance(available, int) or available > computer["total_memory_bytes"]:
        raise BackendPlanError("receipt has no sane available-memory measurement")
    if available < _MIN_AVAILABLE_RAM_BYTES:
        raise BackendPlanError("at least 12 GiB available memory is required before launch planning")


def _intel_integrated_adapters(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    adapters = receipt.get("gpu_adapters")
    if not isinstance(adapters, list):
        raise BackendPlanError("receipt has no GPU adapter list")
    result = []
    for adapter in adapters:
        if not isinstance(adapter, dict) or adapter.get("is_intel") is not True:
            continue
        # "integrated" is deliberately required as explicit evidence.  A
        # name containing Intel Graphics is not enough to establish UMA.
        if adapter.get("integrated") is True:
            result.append(adapter)
    return result


def _intel_adapters_with_identity(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    adapters = receipt.get("gpu_adapters")
    if not isinstance(adapters, list):
        raise BackendPlanError("receipt has no GPU adapter list")
    return [adapter for adapter in adapters if isinstance(adapter, dict) and adapter.get("is_intel") is True and isinstance(adapter.get("pnp_device_id"), str) and adapter["pnp_device_id"].strip()]


def _validate_vulkan_attestation(receipt_path: Path, adapter: dict[str, Any], attestation_path: Path | None) -> dict[str, Any]:
    if attestation_path is None:
        raise BackendPlanError("Vulkan requires a separate integrated-GPU attestation")
    attestation = _load_json(attestation_path)
    if attestation.get("schema") != "local_bmo.windows-gpu-attestation.v1":
        raise BackendPlanError("GPU attestation schema is invalid")
    if attestation.get("receipt_sha256") != _receipt_hash(receipt_path):
        raise BackendPlanError("GPU attestation is not bound to this hardware receipt")
    if attestation.get("integrated") is not True or attestation.get("pnp_device_id") != adapter.get("pnp_device_id"):
        raise BackendPlanError("GPU attestation does not match the exact integrated adapter")
    if not isinstance(attestation.get("basis"), str) or not attestation["basis"].strip():
        raise BackendPlanError("GPU attestation lacks its bounded evidence basis")
    vulkan = attestation.get("vulkan")
    if not isinstance(vulkan, dict) or vulkan.get("pnp_device_id") != adapter.get("pnp_device_id") or vulkan.get("device_name") != adapter.get("name") or vulkan.get("driver_version") != adapter.get("driver_version"):
        raise BackendPlanError("Vulkan evidence is not correlated to the exact adapter and driver")
    return {"sha256": _receipt_hash(attestation_path), "basis": attestation["basis"]}


def _validate_model_identity(model_path: str) -> dict[str, Any]:
    if not isinstance(model_path, str) or not model_path.strip():
        raise BackendPlanError("an absolute model path is required")
    path = Path(model_path)
    if not path.is_absolute():
        raise BackendPlanError("model path must be absolute")
    if path.name != MODEL_NAME:
        raise BackendPlanError("model filename does not match the pinned Q4_K_M artifact")
    verified = False
    canonical = path.resolve(strict=False)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise BackendPlanError("model path is not a regular file")
        canonical = path.resolve(strict=True)
        if canonical.name != MODEL_NAME:
            raise BackendPlanError("canonical model filename does not match the pinned artifact")
        if canonical.stat().st_size != MODEL_SIZE_BYTES:
            raise BackendPlanError("model size does not match the pinned artifact")
        if _receipt_hash(canonical) != MODEL_SHA256:
            raise BackendPlanError("model SHA-256 does not match the pinned artifact")
        verified = True
    return {"name": MODEL_NAME, "path": str(canonical), "size_bytes": MODEL_SIZE_BYTES, "sha256": MODEL_SHA256, "artifact_verified": verified}


def build_plan(
    backend: str,
    receipt_path: Path,
    *,
    model_path: str,
    attestation_path: Path | None = None,
) -> dict[str, Any]:
    """Return a bounded plan or raise; no backend fallback is ever performed."""

    if backend not in {"cpu-safe", "intel-vulkan-conservative", "intel-sycl-experimental"}:
        raise BackendPlanError("backend must be explicitly cpu-safe, intel-vulkan-conservative, or intel-sycl-experimental")
    receipt = _load_json(receipt_path)
    _validate_common_receipt(receipt)
    model = _validate_model_identity(model_path)
    memory = receipt["computer"]["total_memory_bytes"] / 1024**3
    available_memory = receipt["computer"]["available_memory_bytes"] / 1024**3
    plan: dict[str, Any] = {
        "schema": "local_bmo.windows-backend-plan.v2",
        "status": "launch-preconditions-verified" if model["artifact_verified"] else "planning-only",
        "planning_complete": True,
        "launch_preconditions_verified": model["artifact_verified"],
        "execution_ready": False,
        "execution_evidence": "UNPROVEN-requires-binary-identity-model-load-and-startup-self-test",
        "backend": backend,
        "selection": "operator-explicit-no-fallback",
        "model": model,
        "hardware_receipt_sha256": _receipt_hash(receipt_path),
        "memory": {
            "installed_gib": round(memory, 3),
            "available_gib_observed": round(available_memory, 3),
            "os_and_apps_reserve_gib": 8,
            "model_resident_gib_estimate": round(MODEL_SIZE_BYTES / 1024**3, 3),
            "runtime_headroom_gib": 2,
            "default_context_tokens": 8192,
            "maximum_context_tokens": 16384,
            "maximum_context_requires_target_measurement": True,
            "shared_memory_is_not_dedicated_vram": True,
        },
        "provenance": {
            "llama_cpp_revision": LLAMA_CPP_REVISION,
            "backend_docs": [OFFICIAL_LLAMA_SYCL_DOC, OFFICIAL_INTEL_ONEAPI_DOC],
            "target_sku_status": "exact Core Ultra SKU and device ID must come from this receipt",
        },
    }
    if backend == "cpu-safe":
        plan["runtime"] = {"engine": "local-assistant-native", "compiled_backend": "cpu", "engine_cli_backend": "cpu", "context_tokens": 8192, "gpu_layers": 0, "vulkan_device_name": None, "gpu_offload": False, "promotion_rank": 0}
        return plan

    if backend == "intel-vulkan-conservative":
        closure = _closure_metadata()
        if closure is None:
            raise BackendPlanError("Vulkan profile blocked: pinned ggml-vulkan source/shader closure failed verification")
        adapters = _intel_adapters_with_identity(receipt)
        if not adapters:
            raise BackendPlanError("Vulkan requires an Intel adapter with exact PNP identity")
        if attestation_path is None:
            raise BackendPlanError("Vulkan requires a separate integrated-GPU attestation")
        attestation_preview = _load_json(attestation_path)
        attested_pnp = attestation_preview.get("pnp_device_id")
        matching = [adapter for adapter in adapters if adapter.get("pnp_device_id") == attested_pnp]
        if len(matching) != 1:
            raise BackendPlanError("Vulkan attestation must select one exact Intel adapter PNP identity")
        adapter = matching[0]
        if not isinstance(adapter.get("pnp_device_id"), str) or not adapter["pnp_device_id"].strip() or not isinstance(adapter.get("driver_version"), str) or not adapter["driver_version"].strip():
            raise BackendPlanError("Vulkan adapter lacks exact PNP or driver identity")
        attestation = _validate_vulkan_attestation(receipt_path, adapter, attestation_path)
        vulkan = receipt.get("vulkan")
        enumeration = vulkan.get("enumeration") if isinstance(vulkan, dict) else None
        if not isinstance(vulkan, dict) or vulkan.get("loader_present") is not True or not isinstance(enumeration, dict) or enumeration.get("exit_code") != 0 or not isinstance(enumeration.get("summary"), str) or not enumeration["summary"].strip():
            raise BackendPlanError("Vulkan loader and successful bounded enumeration are not proven")
        plan["runtime"] = {
            "engine": "local-assistant-native",
            "compiled_backend": "vulkan",
            "engine_cli_backend": "intel-vulkan",
            "context_tokens": 8192,
            "vulkan_device_name": adapter.get("name"),
            "gpu_offload": True,
            "promotion_rank": 1,
            "gpu_layers_default": 20,
            "gpu_layers": 20,
            "gpu_layers_max": 99,
            "requires_explicit_gpu_layer_policy": True,
            "device": {"name": adapter.get("name"), "pnp_device_id": adapter["pnp_device_id"], "driver_version": adapter["driver_version"]},
            "attestation": attestation,
        }
        plan["provenance"]["vulkan_source_closure"] = closure
        return plan

    if not PRODUCT_VULKAN_PROFILE_PROMOTED:
        raise BackendPlanError("SYCL diagnostic is blocked until the product-engine GGML_VULKAN profile is separately accepted and promoted")

    adapters = _intel_integrated_adapters(receipt)
    if len(adapters) != 1:
        raise BackendPlanError("SYCL requires exactly one explicitly integrated Intel adapter")
    sycl = receipt.get("sycl_level_zero")
    required = ("checked", "available", "device_name", "device_id", "driver_version", "runtime_version", "probe")
    if not isinstance(sycl, dict) or sycl.get("checked") is not True or sycl.get("available") is not True:
        raise BackendPlanError("SYCL capability is not proven by the read-only receipt")
    if any(not isinstance(sycl.get(key), str) or not sycl[key].strip() for key in required[2:]):
        raise BackendPlanError("SYCL receipt lacks bounded device/runtime provenance")
    if sycl.get("device_count") != 1:
        raise BackendPlanError("SYCL receipt must identify one unambiguous device")
    adapter = adapters[0]
    if not isinstance(adapter.get("pnp_device_id"), str) or not adapter["pnp_device_id"].strip():
        raise BackendPlanError("integrated Intel adapter lacks exact PNP identity")
    plan["runtime"] = {
        "engine": "upstream-llama.cpp",
        "compiled_backend": "sycl",
        "device_selector": "SYCL0",
        "context_tokens": 8192,
        "gpu_layers": 99,
        "gpu_offload": True,
        "promotion_rank": 2,
        "cmake_flags": SYCL_BUILD_FLAGS,
        "device": {
            "name": adapter.get("name"),
            "pnp_device_id": adapter["pnp_device_id"],
            "sycl_device_name": sycl["device_name"],
            "sycl_device_id": sycl["device_id"],
        },
        "diagnostic_only": True,
        "requires_oneapi_and_visual_studio": True,
    }
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("cpu-safe", "intel-vulkan-conservative", "intel-sycl-experimental"))
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.backend, args.receipt, model_path=args.model_path, attestation_path=args.attestation)
    except (BackendPlanError, OSError, ValueError) as exc:
        print(f"backend plan refused: {exc}")
        return 2
    text = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
