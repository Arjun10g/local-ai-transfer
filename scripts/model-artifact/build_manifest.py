#!/usr/bin/env python3
"""Build the strict production model manifest from accepted J1M receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from qa.conformance.runner import schema_errors
from scripts.j1m_fetch import verify_local_bundle


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_ref(path: Path, fragment: str = "") -> str:
    suffix = f"#{fragment}" if fragment else ""
    return f"{path.name}@sha256:{sha256(path)}{suffix}"


def build(artifacts: Path, output: Path, schema_path: Path) -> dict[str, Any]:
    verify_local_bundle(artifacts)
    source_path = artifacts / "source-model-receipt.json"
    tensor_path = artifacts / "tensor-metadata.json"
    conversion_path = artifacts / "conversion-receipt.json"
    model_path = artifacts / "model-receipt.json"
    artifact_manifest_path = artifacts / "manifest.json"
    scan_path = artifacts / "scan-receipt.json"
    toolchain_path = artifacts / "toolchain.json"
    checksums_path = artifacts / "checksums.sha256"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    tensor = json.loads(tensor_path.read_text(encoding="utf-8"))
    conversion = json.loads(conversion_path.read_text(encoding="utf-8"))
    model = json.loads(model_path.read_text(encoding="utf-8"))
    artifact_manifest = json.loads(artifact_manifest_path.read_text(encoding="utf-8"))
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    if source.get("status") != "verified" or tensor.get("status") != "verified" or conversion.get("status") != "conversion-complete" or model.get("status") != "checksums-and-tensor-inventory-verified" or scan.get("status") != "verified":
        raise ValueError("J1M receipts are not all accepted")
    if tensor.get("vision_projection_present") is not False or conversion.get("no_mmproj") is not True or conversion.get("no_mtp") is not True:
        raise ValueError("J1M receipts do not prove the approved text-only artifact")
    q4 = next((item for item in artifact_manifest.get("artifacts", []) if item.get("name") == "Qwen3.5-9B-Q4_K_M.gguf"), None)
    q4_identity = {key: q4.get(key) for key in ("size_bytes", "sha256")} if isinstance(q4, dict) else None
    if q4_identity != model.get("q4_artifact"):
        raise ValueError("Q4 identity disagrees across J1M receipts")
    file_hashes = source.get("file_hashes", {})
    payload = {
        "schema_version": "1.1.0",
        "model_id": "qwen35-9b-q4-k-m",
        "display_name": "Qwen3.5-9B Q4_K_M",
        "source": {
            "organization": "Qwen",
            "repository": "Qwen3.5-9B",
            "revision": source["revision"],
            "license_id": "Apache-2.0",
            "model_card_sha256": file_hashes["README.md"],
            "license_file_sha256": source["license_sha256"],
        },
        "artifact": {
            "format": "gguf",
            "architecture": tensor["gguf_metadata"]["general.architecture"],
            "modality_profile": "text_only_no_mmproj",
            "quantization_profile": "Q4_K_M",
            "expected_file_name": "Qwen3.5-9B-Q4_K_M.gguf",
            "expected_size_bytes": q4["size_bytes"],
            "sha256": q4["sha256"],
            "tensor_inventory_sha256": canonical_sha256(tensor["tensors"]),
            "gguf_metadata_sha256": canonical_sha256(tensor["gguf_metadata"]),
        },
        "conversion": {
            "llama_cpp_revision": conversion["llama_cpp_revision"],
            "conversion_command_receipt": receipt_ref(artifacts / "command-receipt.json", "convert_hf_to_gguf"),
            "quantization_command_receipt": receipt_ref(artifacts / "command-receipt.json", "Q4_K_M"),
            "build_environment_receipt": receipt_ref(toolchain_path),
            "reference_artifact_id": receipt_ref(scan_path, "Qwen3.5-9B-bf16.gguf"),
            "created_at_utc": artifact_manifest["created_at_utc"],
        },
        "expected_profile": {
            "parameter_class": "9B",
            "layers": 32,
            "hidden_size": 4096,
            "hybrid_pattern": "8x(3_gated_deltanet_then_1_gated_attention)",
            "full_attention_layers": 8,
            "query_heads_on_attention_layers": 16,
            "kv_heads_on_attention_layers": 4,
            "attention_head_dimension": 256,
            "native_context_tokens": 262144,
            "mtp_present": True,
            "vision_projection_required_for_text_only": False,
            "accepted_gguf_versions": [3],
            "metadata_allowlist_version": "qwen35-9b-v1",
        },
        "tokenizer_and_template": {
            "tokenizer_metadata_sha256": source["tokenizer_sha256"],
            "chat_template_sha256": source["chat_template_sha256"],
            "special_token_fixture_version": "model/quality-eval/quality-fixture-spec.json#thinking-off-001",
            "tool_call_fixture_version": "model/quality-eval/quality-fixture-spec.json#tool-xml-001",
            "thinking_control": "chat_template_enable_thinking_boolean",
        },
        "runtime_policy": {
            "default_context_tokens": 8192,
            "maximum_mvp_context_tokens": 16384,
            "default_thinking": False,
            "default_active_generations": 1,
            "cpu_backend_required": True,
            "accelerated_backend_policy": "approved_hardware_receipt_only",
            "minimum_engine_build": "lae-engine/0.1.0+llama.cpp.3581ba0c",
        },
        "approvals": {
            "source_approval": receipt_ref(source_path),
            "artifact_scan_receipt": receipt_ref(scan_path),
            "transfer_receipt": receipt_ref(checksums_path),
        },
    }
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = schema_errors(payload, schema)
    if errors:
        raise ValueError(f"generated model manifest violates schema: {errors}")
    if payload["artifact"]["expected_size_bytes"] <= 0:
        raise ValueError("generated model manifest has an invalid artifact size")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts" / "qwen35-9b")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema", type=Path, default=ROOT / "model" / "manifests" / "qwen35-9b-q4-k-m.schema.json")
    args = parser.parse_args(argv)
    output = args.output or args.artifacts / "model-manifest.json"
    payload = build(args.artifacts.resolve(), output.resolve(), args.schema.resolve())
    print(json.dumps({"output": str(output), "model_id": payload["model_id"], "sha256": sha256(output.resolve()), "status": "verified"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
