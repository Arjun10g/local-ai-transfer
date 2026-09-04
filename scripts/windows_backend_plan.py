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
import re
from pathlib import Path
from typing import Any

MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
MODEL_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
MODEL_SIZE_BYTES = 5629109088
LLAMA_CPP_REVISION = "3581ba0cf591b3f772fbb002de0f70e294bc0396"
# The product engine currently has no reviewed GGML_VULKAN CMake profile.
# Keep this false until that profile and runtime offload path land together;
# an upstream SYCL binary must not become a backdoor product runtime.
PRODUCT_VULKAN_PROFILE_READY = False
SYCL_BUILD_FLAGS = [
    "-DGGML_SYCL=ON",
    "-DGGML_SYCL_TARGET=INTEL",
    "-DGGML_SYCL_F16=ON",
]
OFFICIAL_LLAMA_SYCL_DOC = "https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md"
OFFICIAL_INTEL_ONEAPI_DOC = "https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/get-started-guide/2025-2/get-started-on-windows.html"
_MAX_RECEIPT_BYTES = 512 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BackendPlanError(ValueError):
    """A capability, provenance, or policy condition is not proven."""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise BackendPlanError("hardware receipt must be a regular file")
    if path.stat().st_size > _MAX_RECEIPT_BYTES:
        raise BackendPlanError("hardware receipt is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
    if str(receipt.get("os", {}).get("caption", "")).lower().find("windows") < 0:
        raise BackendPlanError("receipt does not prove Windows")
    computer = receipt.get("computer")
    if not isinstance(computer, dict) or not isinstance(computer.get("total_memory_bytes"), int):
        raise BackendPlanError("receipt has no total physical memory")
    if computer["total_memory_bytes"] < 24 * 1024**3:
        raise BackendPlanError("at least 24 GiB physical memory is required")


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


def _validate_model_identity(model_path: str | None, size: int, digest: str) -> dict[str, Any]:
    if size != MODEL_SIZE_BYTES or digest != MODEL_SHA256 or not _SHA256.fullmatch(digest):
        raise BackendPlanError("model identity does not match the pinned Q4_K_M artifact")
    verified = False
    if model_path:
        path = Path(model_path)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise BackendPlanError("model path is not a regular file")
            if path.stat().st_size != size:
                raise BackendPlanError("model size does not match the pinned artifact")
            if _receipt_hash(path) != digest:
                raise BackendPlanError("model SHA-256 does not match the pinned artifact")
            verified = True
    return {"name": MODEL_NAME, "size_bytes": size, "sha256": digest, "file_verified": verified}


def build_plan(
    backend: str,
    receipt_path: Path,
    *,
    model_path: str | None = None,
    model_size: int = MODEL_SIZE_BYTES,
    model_sha256: str = MODEL_SHA256,
) -> dict[str, Any]:
    """Return a bounded plan or raise; no backend fallback is ever performed."""

    if backend not in {"cpu-safe", "intel-sycl-experimental"}:
        raise BackendPlanError("backend must be explicitly cpu-safe or intel-sycl-experimental")
    receipt = _load_json(receipt_path)
    _validate_common_receipt(receipt)
    model = _validate_model_identity(model_path, model_size, model_sha256)
    memory = receipt["computer"]["total_memory_bytes"] / 1024**3
    plan: dict[str, Any] = {
        "schema": "local_bmo.windows-backend-plan.v1",
        "status": "planned" if not model["file_verified"] else "ready",
        "execution_ready": model["file_verified"],
        "backend": backend,
        "selection": "operator-explicit-no-fallback",
        "model": model,
        "hardware_receipt_sha256": _receipt_hash(receipt_path),
        "memory": {
            "installed_gib": round(memory, 3),
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
        plan["runtime"] = {"engine": "local-assistant-native", "compiled_backend": "cpu", "gpu_offload": False, "promotion_rank": 0}
        return plan

    if not PRODUCT_VULKAN_PROFILE_READY:
        raise BackendPlanError("SYCL diagnostic is blocked until the product-engine GGML_VULKAN profile is reviewed")

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
        "gpu_offload": True,
        "promotion_rank": 2,
        "build_flags": SYCL_BUILD_FLAGS,
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
    parser.add_argument("--backend", required=True, choices=("cpu-safe", "intel-sycl-experimental"))
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--model-path")
    parser.add_argument("--model-size", type=int, default=MODEL_SIZE_BYTES)
    parser.add_argument("--model-sha256", default=MODEL_SHA256)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.backend, args.receipt, model_path=args.model_path, model_size=args.model_size, model_sha256=args.model_sha256)
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
