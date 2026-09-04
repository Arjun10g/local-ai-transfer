#!/usr/bin/env python3
"""Plan and run the controlled Qwen3.5-9B GGUF conversion job (J1M).

The default action is a no-spend plan.  Execution is deliberately limited to
an already-authorised host; this module never creates a Shadeform resource.
Commands are recorded as argv arrays, and the HF token is materialised only in
a private temporary file for the duration of a build.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "model" / "conversion" / "j1m-config.json"
SOURCE_LOCK = ROOT / "model" / "source-lock" / "qwen35-9b.source-lock.json"
TOKEN_ENV = "HF_TOKEN"
CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONUNBUFFERED",
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
})
_COMMAND_LOG_TAIL_LIMIT = 1200
_RECEIPT_MAX_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "local_bmo.j1m.v1":
        raise ValueError("invalid J1M config schema")
    source = payload.get("source", {})
    if source.get("model_id") != "Qwen/Qwen3.5-9B":
        raise ValueError("J1M source model is not the approved Qwen3.5-9B")
    if len(str(source.get("revision", ""))) != 40:
        raise ValueError("J1M source revision must be an immutable commit SHA")
    llama = payload.get("llama_cpp", {})
    if len(str(llama.get("revision", ""))) != 40:
        raise ValueError("J1M llama.cpp revision must be an immutable commit SHA")
    if payload.get("text_only") is not True:
        raise ValueError("J1M must be text-only")
    eval_mode = payload.get("modes", {}).get("eval", {})
    if eval_mode.get("backend") != "cuda" or eval_mode.get("cuda_device_name") != "CUDA0" or eval_mode.get("cuda_architecture") != 80 or eval_mode.get("gpu_layers") != 99:
        raise ValueError("eval must use the explicit CUDA A100 evaluation profile")
    if eval_mode.get("cuda_compiler") != "/usr/local/cuda/bin/nvcc":
        raise ValueError("eval must bind the approved absolute CUDA compiler path")
    if eval_mode.get("build_parallelism") != 8:
        raise ValueError("eval must use the reviewed bounded CUDA build parallelism")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_json_file(path: Path, *, limit: int = _RECEIPT_MAX_BYTES) -> Any:
    """Read one receipt snapshot, rejecting oversize and duplicate keys."""
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"receipt exceeds {limit} bytes: {path.name}")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate receipt key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid receipt JSON: {path.name}") from exc


def _receipt_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def _validate_producer_receipts(output_dir: Path, source_lock: Path, *, llama_revision: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Validate every producer receipt before a completion manifest is emitted."""
    source_lock_payload = _bounded_json_file(source_lock)
    if not isinstance(source_lock_payload, dict) or source_lock_payload.get("schema_version") != "1.0.0":
        raise ValueError("source lock schema is invalid")
    source = _bounded_json_file(output_dir / "source-model-receipt.json")
    source_keys = {"schema", "status", "model_id", "revision", "checked_files", "file_hashes", "license_sha256", "tokenizer_sha256", "chat_template_sha256", "verified_at_utc"}
    if (not isinstance(source, dict) or set(source) != source_keys or source["schema"] != "local_bmo.j1m.source-model-receipt.v1" or source["status"] != "verified" or source["model_id"] != source_lock_payload.get("model_id") or source["revision"] != source_lock_payload.get("revision")):
        raise ValueError("source receipt identity is invalid")
    checked = source["checked_files"]
    hashes = source["file_hashes"]
    if (not isinstance(checked, list) or len(checked) > 10000 or len(set(checked)) != len(checked) or any(not isinstance(item, str) or not item or len(item) > 512 for item in checked) or not isinstance(hashes, dict) or set(hashes) != set(checked)):
        raise ValueError("source receipt file inventory is invalid")
    for digest in hashes.values():
        _receipt_sha(digest, "source file hash")
    lock_hashes = {
        str(item["path"]): str(item.get("sha256") or item.get("lfs_sha256"))
        for item in source_lock_payload.get("source_files", [])
        if isinstance(item, dict) and not item.get("excluded_from_text_only") and (item.get("sha256") or item.get("lfs_sha256"))
    }
    if set(hashes) != set(lock_hashes) or any(lock_hashes.get(name) != digest for name, digest in hashes.items()):
        raise ValueError("source receipt hash is not bound to the immutable source lock")
    for field in ("license_sha256", "tokenizer_sha256", "chat_template_sha256"):
        _receipt_sha(source[field], field)
    if not isinstance(source["verified_at_utc"], str) or not source["verified_at_utc"]:
        raise ValueError("source receipt timestamp is invalid")

    tensor = _bounded_json_file(output_dir / "tensor-metadata.json")
    tensor_keys = {"schema", "status", "text_only", "tensor_count", "tensors", "gguf_metadata", "vision_projection_present", "chat_template_sha256"}
    if (not isinstance(tensor, dict) or set(tensor) != tensor_keys or tensor["schema"] != "local_bmo.j1m.tensor-metadata.v1" or tensor["status"] != "verified" or tensor["text_only"] is not True or tensor["vision_projection_present"] is not False):
        raise ValueError("tensor receipt schema or policy is invalid")
    tensors = tensor["tensors"]
    if (not isinstance(tensors, list) or len(tensors) != tensor["tensor_count"] or len(tensors) > 200000 or isinstance(tensor["tensor_count"], bool) or not isinstance(tensor["tensor_count"], int) or any(not isinstance(item, dict) or set(item) != {"name", "shape", "type"} or not isinstance(item["name"], str) or not isinstance(item["shape"], list) or len(item["shape"]) > 16 or any(isinstance(dim, bool) or not isinstance(dim, int) or dim < 0 for dim in item["shape"]) or not isinstance(item["type"], str) for item in tensors)):
        raise ValueError("tensor inventory is invalid")
    metadata = tensor["gguf_metadata"]
    if not isinstance(metadata, dict) or set(metadata) - {"general.architecture", "general.file_type", "general.version", "tokenizer.chat_template", "gguf.version"} or str(metadata.get("general.architecture", "")).lower().replace(".", "").replace("_", "") != "qwen35":
        raise ValueError("GGUF metadata identity is invalid")
    _receipt_sha(tensor["chat_template_sha256"], "tensor chat template hash")
    if tensor["chat_template_sha256"] != source["chat_template_sha256"]:
        raise ValueError("tensor chat template hash is not bound to the source receipt")

    toolchain = _bounded_json_file(output_dir / "toolchain.json")
    toolchain_keys = {"schema", "llama_cpp_head", "python", "cmake", "compiler", "os_packages", "pip_freeze", "dependency_wheelhouse_lock"}
    if not isinstance(toolchain, dict) or set(toolchain) != toolchain_keys or toolchain.get("schema") != "local_bmo.j1m.toolchain.v1" or (llama_revision is not None and toolchain.get("llama_cpp_head") != llama_revision):
        raise ValueError("toolchain receipt identity is invalid")
    if any(not isinstance(toolchain.get(field), str) or not toolchain[field] or len(toolchain[field]) > 4096 for field in ("llama_cpp_head", "python", "cmake", "compiler")) or not isinstance(toolchain.get("pip_freeze"), str) or len(toolchain["pip_freeze"]) > _RECEIPT_MAX_BYTES or not isinstance(toolchain["os_packages"], list) or len(toolchain["os_packages"]) > 64 or any(not isinstance(item, str) or len(item) > 512 for item in toolchain["os_packages"]):
        raise ValueError("toolchain receipt shape is invalid")
    dependency_lock = toolchain["dependency_wheelhouse_lock"]
    if not isinstance(dependency_lock, dict) or (dependency_lock and dependency_lock.get("schema") != "local_bmo.j1m.wheelhouse-lock.v1"):
        raise ValueError("toolchain dependency lock is invalid")

    command_receipt = _bounded_json_file(output_dir / "command-receipt.json")
    if not isinstance(command_receipt, list) or not command_receipt or len(command_receipt) > 128:
        raise ValueError("command receipt sequence is invalid")
    base = {"stage", "argv", "started_at_utc", "ended_at_utc", "exit_code", "status"}
    optional = {"stdout_tail", "stderr_tail", "error_type"}
    for expected_stage, item in enumerate(command_receipt, 1):
        if not isinstance(item, dict) or not base <= set(item) or set(item) - base - optional:
            raise ValueError("command receipt entry is invalid")
        if isinstance(item["stage"], bool) or not isinstance(item["stage"], int) or item["stage"] <= 0 or not isinstance(item["argv"], list) or not item["argv"] or any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 4096 for arg in item["argv"]):
            raise ValueError("command receipt argv is invalid")
        if item["status"] not in {"completed", "failed", "launch_failed", "transport_timeout"} or item["stage"] != expected_stage or item["status"] != "completed" or item["exit_code"] != 0:
            raise ValueError("command receipt status is invalid")
        if any(not isinstance(item[field], str) or not item[field] for field in ("started_at_utc", "ended_at_utc")):
            raise ValueError("command receipt timestamp is invalid")
        for field in optional & set(item):
            if not isinstance(item[field], str) or len(item[field]) > _COMMAND_LOG_TAIL_LIMIT:
                raise ValueError("command receipt diagnostic is invalid")

    scan = _bounded_json_file(output_dir / "scan-receipt.json")
    if not isinstance(scan, dict) or set(scan) != {"schema", "status", "inventory_scope", "text_only", "artifacts", "vision_projection_present"} or scan["schema"] != "local_bmo.j1m.scan-receipt.v1" or scan["status"] != "verified" or scan["inventory_scope"] != "pre_cleanup_conversion_outputs" or scan["text_only"] is not True or scan["vision_projection_present"] is not False:
        raise ValueError("scan receipt is invalid")
    scan_artifacts = scan["artifacts"]
    expected_names = {"Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"}
    if not isinstance(scan_artifacts, list) or len(scan_artifacts) != 3 or {item.get("name") for item in scan_artifacts if isinstance(item, dict)} != expected_names or any(not isinstance(item, dict) or set(item) != {"name", "size_bytes", "sha256"} or not isinstance(item["name"], str) or isinstance(item["size_bytes"], bool) or not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0 for item in scan_artifacts):
        raise ValueError("scan artifact inventory is invalid")
    for item in scan_artifacts:
        _receipt_sha(item["sha256"], "scan artifact hash")
    post = _bounded_json_file(output_dir / "post-cleanup-receipt.json")
    if not isinstance(post, dict) or set(post) != {"schema", "status", "inventory_scope", "intermediates_absent", "remaining_gguf", "forbidden_artifacts", "q4"} or post["schema"] != "local_bmo.j1m.post-cleanup-receipt.v1" or post["status"] != "verified" or post["inventory_scope"] != "post_cleanup_filesystem" or post["intermediates_absent"] is not True or post["remaining_gguf"] != ["Qwen3.5-9B-Q4_K_M.gguf"] or post["forbidden_artifacts"] != []:
        raise ValueError("post-cleanup receipt is invalid")
    q4_scan = next(item for item in scan_artifacts if item["name"] == "Qwen3.5-9B-Q4_K_M.gguf")
    if (not isinstance(post["q4"], dict) or set(post["q4"]) != {"size_bytes", "sha256"} or
            post["q4"] != {"size_bytes": q4_scan["size_bytes"], "sha256": q4_scan["sha256"]}):
        raise ValueError("post-cleanup Q4 identity is invalid")
    return source, tensor, toolchain, command_receipt, scan, post


def _normalize_gguf_value(value: Any) -> Any:
    """Turn gguf ReaderField contents into JSON/scalar text without ndarray reprs."""

    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, dict):
        return {str(key): _normalize_gguf_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_gguf_value(item) for item in value]
        return normalized[0] if len(normalized) == 1 else normalized
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    if hasattr(value, "item"):
        try:
            return _normalize_gguf_value(value.item())
        except ValueError:
            pass
    return value


def _reader_field_value(field: Any) -> Any:
    contents = field.contents() if callable(getattr(field, "contents", None)) else getattr(field, "contents", None)
    if contents is None and hasattr(field, "parts"):
        contents = field.parts[-1]
    return _normalize_gguf_value(contents)


def verify_source(source_dir: Path, lock_path: Path = SOURCE_LOCK) -> dict[str, Any]:
    """Verify the local source checkout against the frozen lock.

    A checkout must carry ``.source-revision`` (or ``REVISION``) containing the
    exact HF commit.  Hash verification is performed before conversion starts.
    """

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    revision = str(lock.get("revision", ""))
    marker = next((source_dir / name for name in (".source-revision", "REVISION") if (source_dir / name).is_file()), None)
    if marker is None or marker.read_text(encoding="utf-8").strip() != revision:
        raise ValueError("HF source revision marker does not match the immutable lock")
    checked: list[str] = []
    for item in lock.get("source_files", []):
        if not isinstance(item, dict) or item.get("excluded_from_text_only"):
            continue
        expected = item.get("sha256") or item.get("lfs_sha256")
        if not expected:
            continue
        path = source_dir / str(item["path"])
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError(f"source hash mismatch: {item.get('path')}")
        checked.append(str(item["path"]))
    return {"schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified", "model_id": lock["model_id"], "revision": revision, "checked_files": checked, "file_hashes": {str(item["path"]): str(item.get("sha256") or item.get("lfs_sha256")) for item in lock.get("source_files", []) if isinstance(item, dict) and str(item.get("path")) in checked}, "license_sha256": lock["source_receipts"]["license_sha256"], "tokenizer_sha256": lock["source_receipts"]["tokenizer_sha256"], "chat_template_sha256": lock["source_receipts"]["chat_template_sha256"], "verified_at_utc": utc_now()}


def mark_source(source_dir: Path, revision: str) -> None:
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("source marker requires a full lowercase commit SHA")
    source_dir.mkdir(parents=True, exist_ok=True)
    marker = source_dir / ".source-revision"
    marker.write_text(revision + "\n", encoding="utf-8")


def check_scratch(path: Path, required_gib: int) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    available_gib = usage.free / (1024 ** 3)
    if available_gib < required_gib:
        raise RuntimeError(f"insufficient scratch: {available_gib:.1f} GiB available; {required_gib} GiB required")
    return {"path": str(path), "available_gib": round(available_gib, 2), "required_gib": required_gib}


def scan_artifacts(output_dir: Path) -> dict[str, Any]:
    names = ["Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"]
    records = [{"name": name, "size_bytes": (output_dir / name).stat().st_size, "sha256": _sha256(output_dir / name)} for name in names]
    if any("mmproj" in path.name.lower() for path in output_dir.iterdir()):
        raise ValueError("vision/mmproj artifact is forbidden")
    tensor_path = output_dir / "tensor-metadata.json"
    if not tensor_path.is_file():
        raise ValueError("tensor metadata is required before the artifact scan")
    tensor_metadata = json.loads(tensor_path.read_text(encoding="utf-8"))
    vision_present = tensor_metadata.get("vision_projection_present")
    if vision_present is not False:
        raise ValueError("GGUF inspection did not prove absence of vision/mmproj tensors")
    payload = {"schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True, "artifacts": records, "vision_projection_present": vision_present}
    (output_dir / "scan-receipt.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def post_cleanup_verify(output_dir: Path) -> dict[str, Any]:
    """Prove the remote filesystem no longer contains conversion intermediates."""

    remaining_gguf = {path.name for path in output_dir.glob("*.gguf") if path.is_file()}
    expected = {"Qwen3.5-9B-Q4_K_M.gguf"}
    if remaining_gguf != expected:
        raise ValueError("post-cleanup verification requires exactly the Q4 deployable GGUF")
    forbidden_names = {
        path.name for path in output_dir.iterdir()
        if path.is_file() and any(term in path.name.lower() for term in ("mmproj", "vision"))
    }
    if forbidden_names:
        raise ValueError("post-cleanup verification found a vision/mmproj artifact")
    q4 = output_dir / "Qwen3.5-9B-Q4_K_M.gguf"
    payload = {"schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": sorted(remaining_gguf), "forbidden_artifacts": [], "q4": {"size_bytes": q4.stat().st_size, "sha256": _sha256(q4)}}
    (output_dir / "post-cleanup-receipt.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


@contextlib.contextmanager
def hf_token_file(token: str | None = None) -> Iterator[Path]:
    """Yield a 0600 token file and remove it on every exit path."""

    value = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
    if not value:
        raise ValueError("HF_TOKEN is required only when executing the remote build")
    fd, name = tempfile.mkstemp(prefix="j1m-hf-", suffix=".env")
    path = Path(name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"HF_TOKEN={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PermissionError("HF token file is not private")
        yield path
    finally:
        path.unlink(missing_ok=True)


def _safe_artifact_name(value: str) -> str:
    path = Path(value)
    if path.name != value or value in {"", ".", ".."} or "\\" in value:
        raise ValueError(f"artifact is outside the allowlist: {value!r}")
    return value


def artifact_manifest(output_dir: Path, names: list[str], *, tensor_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    artifacts = []
    for raw_name in names:
        name = _safe_artifact_name(raw_name)
        if name in {"manifest.json", "checksums.sha256"}:
            continue
        path = output_dir / name
        if not path.is_file():
            raise FileNotFoundError(path)
        artifacts.append({"name": name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    return {
        "schema": "local_bmo.j1m.artifact-manifest.v1",
        "created_at_utc": utc_now(),
        "inventory_scope": "post_cleanup_deployable_allowlist",
        "deployable_model_artifacts": ["Qwen3.5-9B-Q4_K_M.gguf"],
        "text_only": True,
        "artifacts": artifacts,
        "tensor_metadata": tensor_metadata or {"status": "pending_converter_receipt"},
    }


def write_wheelhouse_lock(wheelhouse: Path, lock_path: Path, *, llama_revision: str) -> dict[str, Any]:
    if len(llama_revision) != 40 or any(character not in "0123456789abcdef" for character in llama_revision):
        raise ValueError("wheelhouse lock requires the full pinned llama.cpp revision")
    files = sorted(path for path in wheelhouse.iterdir() if path.is_file()) if wheelhouse.is_dir() else []
    if not files:
        raise ValueError("dependency wheelhouse is empty")
    entries = [{"name": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)} for path in files]
    payload = {"schema": "local_bmo.j1m.wheelhouse-lock.v1", "llama_cpp_revision": llama_revision, "artifacts": entries, "created_at_utc": utc_now()}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def verify_wheelhouse(wheelhouse: Path, lock_path: Path) -> dict[str, Any]:
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    entries = payload.get("artifacts")
    if payload.get("schema") != "local_bmo.j1m.wheelhouse-lock.v1" or not isinstance(entries, list) or not entries:
        raise ValueError("invalid dependency wheelhouse lock")
    expected = {str(item.get("name")): item for item in entries if isinstance(item, dict)}
    if len(expected) != len(entries):
        raise ValueError("dependency wheelhouse lock has duplicate or malformed entries")
    actual_files = {path.name: path for path in wheelhouse.iterdir() if path.is_file()}
    if set(actual_files) != set(expected):
        raise ValueError("dependency wheelhouse changed after hash lock")
    for name, item in expected.items():
        if item.get("size_bytes") != actual_files[name].stat().st_size or item.get("sha256") != _sha256(actual_files[name]):
            raise ValueError(f"dependency wheel hash mismatch: {name}")
    return {"status": "verified", "artifact_count": len(expected), "llama_cpp_revision": payload.get("llama_cpp_revision")}


def write_artifacts(output_dir: Path, names: list[str], *, source_lock: Path = SOURCE_LOCK, commands: list[list[str]] | None = None, llama_revision: str | None = None) -> dict[str, Any]:
    """Write receipts in dependency order, avoiding a self-referential manifest."""

    output_dir.mkdir(parents=True, exist_ok=True)
    primary = [name for name in names if name.endswith(".gguf")]
    tensor_path = output_dir / "tensor-metadata.json"
    if not tensor_path.is_file():
        tensor_path.write_text(json.dumps({"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "converter-inspection-pending", "text_only": True}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    source_receipt = output_dir / "source-model-receipt.json"
    if not source_receipt.is_file():
        raise ValueError("source-model-receipt.json must be emitted by hash verification before artifacts")
    toolchain_path = output_dir / "toolchain.json"
    if not toolchain_path.is_file():
        raise ValueError("toolchain receipt is required before model receipt")
    for required in ("command-receipt.json", "scan-receipt.json", "post-cleanup-receipt.json"):
        if not (output_dir / required).is_file():
            raise ValueError(f"{required} is required before final manifest")
    source, tensor, toolchain, command_receipt, scan_receipt, post_cleanup_receipt = _validate_producer_receipts(
        output_dir, source_lock, llama_revision=llama_revision,
    )
    scan_artifacts = scan_receipt.get("artifacts")
    artifact_hashes = {item["name"]: {"size_bytes": item["size_bytes"], "sha256": item["sha256"]} for item in scan_artifacts}
    q4 = output_dir / "Qwen3.5-9B-Q4_K_M.gguf"
    current_q4 = {"size_bytes": q4.stat().st_size, "sha256": _sha256(q4)} if q4.is_file() else None
    post_q4 = post_cleanup_receipt.get("q4")
    if current_q4 != artifact_hashes.get("Qwen3.5-9B-Q4_K_M.gguf") or post_q4 != current_q4:
        raise ValueError("Q4 hash/size does not agree across pre- and post-cleanup receipts")
    converter_commands = [command for command in (commands or []) if any("convert_hf_to_gguf.py" in part for part in command) or "Q4_K_M" in command]
    (output_dir / "conversion-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.conversion-receipt.v1", "status": "conversion-complete", "text_only": True, "source_revision": source.get("revision"), "llama_cpp_revision": llama_revision, "artifacts": artifact_hashes, "converter_and_quantizer_argv": converter_commands, "command_receipt_sha256": _sha256(output_dir / "command-receipt.json"), "toolchain": toolchain, "no_mmproj": True, "no_mtp": True}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "model-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.model-receipt.v1", "status": "checksums-and-tensor-inventory-verified", "text_only": True, "q4_artifact": artifact_hashes.get("Qwen3.5-9B-Q4_K_M.gguf"), "tensor_metadata_sha256": _sha256(tensor_path), "gguf_metadata": tensor.get("gguf_metadata", {}), "vision_projection_present": tensor.get("vision_projection_present"), "scan_receipt_sha256": _sha256(output_dir / "scan-receipt.json"), "tokenizer_sha256": source.get("tokenizer_sha256"), "chat_template_sha256": source.get("chat_template_sha256"), "license_sha256": source.get("license_sha256")}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The deployable manifest is self-verifiable after intermediates are
    # securely removed. Intermediate BF16/Q8 hashes remain in conversion
    # receipt, but are intentionally absent from the shipped bundle.
    deployable = ["Qwen3.5-9B-Q4_K_M.gguf", "tensor-metadata.json", "source-model-receipt.json", "conversion-receipt.json", "model-receipt.json", "toolchain.json", "post-cleanup-receipt.json"]
    bounded_tensor_metadata = {
        "status": tensor["status"],
        "tensor_count": tensor.get("tensor_count"),
        "tensor_inventory_sha256": _sha256(tensor_path),
        "gguf_metadata": {key: value for key, value in tensor.get("gguf_metadata", {}).items() if key != "tokenizer.chat_template"},
        "chat_template_sha256": tensor.get("chat_template_sha256"),
        "vision_projection_present": tensor.get("vision_projection_present"),
    }
    manifest = artifact_manifest(output_dir, [*deployable, "command-receipt.json", "scan-receipt.json"], tensor_metadata=bounded_tensor_metadata)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksum_names = [item["name"] for item in manifest["artifacts"]] + ["manifest.json"]
    checksums = "".join(f"{_sha256(output_dir / name)}  {name}\n" for name in checksum_names)
    (output_dir / "checksums.sha256").write_text(checksums, encoding="utf-8")
    return manifest


def command_plan(config: dict[str, Any], source: str = "/scratch/hf/Qwen3.5-9B", output: str = "/scratch/j1m/artifacts", runner: str = "scripts/j1m_runner.py", config_path: str = "model/conversion/j1m-config.json") -> list[list[str]]:
    llama = config["llama_cpp"]
    converter = f"{llama['checkout']}/convert_hf_to_gguf.py"
    python_exec = "/scratch/j1m/venv/bin/python"
    hf_exec = "/scratch/j1m/venv/bin/hf"
    wheelhouse = "/scratch/j1m/wheelhouse"
    wheelhouse_lock = f"{output}/wheelhouse-lock.json"
    return [
        ["git", "clone", "--filter=blob:none", config["llama_cpp"]["repository"], llama["checkout"]],
        ["git", "-C", llama["checkout"], "checkout", "--detach", llama["revision"]],
        ["sudo", "apt-get", "update"],
        ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "python3-venv", "cmake", "build-essential"],
        ["python3", "-m", "venv", "/scratch/j1m/venv"],
        ["mkdir", "-p", wheelhouse],
        ["/scratch/j1m/venv/bin/pip", "wheel", "--disable-pip-version-check", "--no-input", "--wheel-dir", wheelhouse, "-r", f"{llama['checkout']}/{config['python_dependencies']['requirements_file']}", f"{llama['checkout']}/{config['python_dependencies']['local_gguf_package']}"],
        [python_exec, runner, "--config", config_path, "--wheelhouse-lock", wheelhouse_lock, "--wheelhouse", wheelhouse, "--llama-revision", llama["revision"]],
        ["/scratch/j1m/venv/bin/pip", "install", "--disable-pip-version-check", "--no-input", "--no-index", "--find-links", wheelhouse, "-r", f"{llama['checkout']}/{config['python_dependencies']['requirements_file']}", "gguf"],
        [python_exec, runner, "--config", config_path, "--wheelhouse-lock", wheelhouse_lock, "--verify-wheelhouse", "--wheelhouse", wheelhouse],
        [python_exec, runner, "--config", config_path, "--pip-freeze", f"{output}/pip-freeze.txt"],
        [python_exec, runner, "--config", config_path, "--toolchain", f"{output}/toolchain.json", "--llama-checkout", llama["checkout"], "--wheelhouse-lock", wheelhouse_lock],
        ["git", "--version"],
        ["cmake", "--version"],
        ["python3", "--version"],
        ["mkdir", "-p", source, output],
        ["python3", runner, "--config", config_path, "--verify-llama", llama["checkout"], llama["revision"]],
        [
            "cmake", "-S", llama["checkout"], "-B", f"{llama['checkout']}/build",
            "-DGGML_CUDA=OFF",
            "-DLLAMA_BUILD_TOOLS=ON",
            "-DLLAMA_BUILD_TESTS=OFF",
            "-DLLAMA_BUILD_EXAMPLES=OFF",
            "-DLLAMA_BUILD_SERVER=OFF",
            "-DLLAMA_BUILD_APP=OFF",
            "-DLLAMA_BUILD_UI=OFF",
            "-DLLAMA_OPENSSL=OFF",
        ],
        ["cmake", "--build", f"{llama['checkout']}/build", "--target", "llama-quantize", "-j2"],
        [python_exec, runner, "--config", config_path, "--scratch", "/scratch", "--min-scratch-gib", str(config["resources"]["required_scratch_gib"])],
        [hf_exec, "download", config["source"]["model_id"], "--revision", config["source"]["revision"], "--local-dir", source],
        [python_exec, runner, "--config", config_path, "--mark-source", source, "--revision", config["source"]["revision"]],
        [python_exec, "-u", runner, "--config", config_path, "--verify-source", source, "--lock", "/scratch/j1m/qwen35-9b.source-lock.json", "--receipt", f"{output}/source-model-receipt.json"],
        [python_exec, converter, source, "--outfile", f"{output}/Qwen3.5-9B-bf16.gguf", "--outtype", "bf16", "--no-mtp"],
        [python_exec, converter, source, "--outfile", f"{output}/Qwen3.5-9B-Q8_0.gguf", "--outtype", "q8_0", "--no-mtp"],
        [f"{llama['quantizer']}", f"{output}/Qwen3.5-9B-bf16.gguf", f"{output}/Qwen3.5-9B-Q4_K_M.gguf", "Q4_K_M"],
        [python_exec, runner, "--config", config_path, "--inspect-tensors", f"{output}/Qwen3.5-9B-Q4_K_M.gguf", f"{output}/tensor-metadata.json", "--source-receipt", f"{output}/source-model-receipt.json"],
        [python_exec, runner, "--config", config_path, "--scan", output],
        ["rm", "-f", f"{output}/Qwen3.5-9B-bf16.gguf", f"{output}/Qwen3.5-9B-Q8_0.gguf"],
        [python_exec, runner, "--config", config_path, "--post-cleanup", output],
        [python_exec, runner, "--config", config_path, "--manifest", output],
    ]


def read_token_file(path: Path) -> str:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError("HF token file must have mode 0600")
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            entries[key.strip()] = value.strip()
    token = entries.get(TOKEN_ENV, "")
    if not token:
        raise ValueError("private token file has no HF_TOKEN")
    return token


def _bounded_command_tail(stream: Any) -> str:
    stream.flush()
    size = stream.tell()
    stream.seek(max(0, size - (_COMMAND_LOG_TAIL_LIMIT * 4)))
    value = stream.read().decode("utf-8", errors="replace")
    value = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+", r"\1\2<redacted>", value)
    return value[-_COMMAND_LOG_TAIL_LIMIT:]


def run_commands(commands: list[list[str]], progress_path: Path, *, cwd: Path | None = None, token_file: Path | None = None, receipt_path: Path | None = None) -> list[dict[str, Any]]:
    """Run an already-reviewed argv plan, recording progress before each stage."""

    receipts = []
    all_stage_receipts = []
    if receipt_path:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("[]\n", encoding="utf-8")
    for index, command in enumerate(commands):
        if not command or any("\x00" in str(part) for part in command):
            raise ValueError("invalid empty/NUL command")
        write_progress(progress_path, f"stage-{index + 1}-starting", argv=command)
        started_at = utc_now()
        try:
            # Never inherit dotenv/API credentials into child tools. The token
            # is added only to the one exact HF download subprocess.
            environment = {key: os.environ[key] for key in CHILD_ENV_ALLOWLIST if key in os.environ}
            if token_file is not None and any(part == "download" for part in command):
                environment[TOKEN_ENV] = read_token_file(token_file)
            # Keep verbose converter/download output off the SSH transport and
            # retain only bounded, redacted tails when a stage fails.
            with tempfile.TemporaryFile() as stdout_log, tempfile.TemporaryFile() as stderr_log:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    check=False,
                    timeout=6 * 60 * 60,
                    env=environment,
                    stdout=stdout_log,
                    stderr=stderr_log,
                )
                stdout_tail = _bounded_command_tail(stdout_log)
                stderr_tail = _bounded_command_tail(stderr_log)
            stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": completed.returncode, "status": "completed" if completed.returncode == 0 else "failed"}
            if completed.returncode != 0:
                stage_receipt["stdout_tail"] = stdout_tail
                stage_receipt["stderr_tail"] = stderr_tail
        except subprocess.TimeoutExpired:
            stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": None, "status": "transport_timeout"}
        except OSError as exc:
            message = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+", r"\1\2<redacted>", str(exc))
            stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": None, "status": "launch_failed", "error_type": type(exc).__name__, "stderr_tail": message[-_COMMAND_LOG_TAIL_LIMIT:]}
        # Manifest creation and intermediate cleanup are administrative stages;
        # they intentionally do not mutate the immutable conversion receipt.
        administrative = "--manifest" in command or "--post-cleanup" in command or (command and command[0] == "rm")
        all_stage_receipts.append(stage_receipt)
        if not administrative:
            receipts.append(stage_receipt)
            if receipt_path:
                receipt_path.write_text(json.dumps(receipts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        progress_details = {key: value for key, value in stage_receipt.items() if key != "stage"}
        write_progress(progress_path, f"stage-{index + 1}-{stage_receipt['status']}", **progress_details)
        if stage_receipt["status"] != "completed":
            break
    # Return administrative failures to the caller while keeping them out of
    # the immutable conversion receipt hashed by the manifest.
    return all_stage_receipts


def build_plan(config: dict[str, Any], mode: str = "prove") -> dict[str, Any]:
    target = config["shadeform_target"]
    selected_mode = config["modes"][mode]
    runtime = float(selected_mode["runtime_hours"])
    rate = float(target["hourly_usd"])
    return {
        "schema": "local_bmo.j1m.dry-run-plan.v1",
        "created_at_utc": utc_now(),
        "mutation": "refused: planning only; no provider API mutation",
        "candidate": target,
        "active_run_cost_usd": round(rate * runtime, 4),
        "provider_backstop_cost_usd": round(rate * float(selected_mode["provider_backstop_hours"]), 4),
        "commands": command_plan(config) if mode == "build" else [["python3", "scripts/j1m_runner.py", "--prove"]],
        "artifact_allowlist": config["artifacts"]["allowlist"],
        "required_scratch_gib": config["resources"]["required_scratch_gib"],
    }


def write_progress(path: Path, stage: str, **details: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stage": stage, "written_at_utc": utc_now(), **details}
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--verify-source", type=Path)
    parser.add_argument("--mark-source", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--lock", type=Path, default=SOURCE_LOCK)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--verify-llama", nargs=2, metavar=("CHECKOUT", "REVISION"))
    parser.add_argument("--prove", action="store_true", help="write a cheap host receipt; no model conversion")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--min-scratch-gib", type=int, default=0)
    parser.add_argument("--run", action="store_true", help="run the reviewed local argv plan")
    parser.add_argument("--token-file", type=Path, help="private remote token file; path only, never token material")
    parser.add_argument("--pip-freeze", type=Path)
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--wheelhouse-lock", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--verify-wheelhouse", action="store_true")
    parser.add_argument("--llama-revision")
    parser.add_argument("--llama-checkout", type=Path)
    parser.add_argument("--inspect-tensors", nargs=2, metavar=("GGUF", "OUTPUT"))
    parser.add_argument("--source-receipt", type=Path)
    parser.add_argument("--scan", type=Path)
    parser.add_argument("--post-cleanup", type=Path)
    parser.add_argument("--execute", action="store_true", help="reserved for an already-approved host; never provisions")
    args = parser.parse_args(argv)
    if args.verify_llama:
        checkout, expected = args.verify_llama
        actual = subprocess.run(["git", "-C", checkout, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if actual != expected:
            raise ValueError("llama.cpp checkout is not the pinned immutable revision")
        print(json.dumps({"revision": actual}, sort_keys=True))
        return 0
    if args.pip_freeze:
        freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], check=True, capture_output=True, text=True, timeout=120).stdout
        args.pip_freeze.write_text(freeze, encoding="utf-8")
        return 0
    if args.wheelhouse_lock and (args.wheelhouse is not None or args.verify_wheelhouse):
        if args.wheelhouse is None or not args.llama_revision and not args.verify_wheelhouse:
            raise ValueError("wheelhouse lock requires a wheelhouse; writing also requires the pinned llama revision")
        if args.verify_wheelhouse:
            verify_wheelhouse(args.wheelhouse, args.wheelhouse_lock)
        else:
            write_wheelhouse_lock(args.wheelhouse, args.wheelhouse_lock, llama_revision=args.llama_revision)
        return 0
    if args.toolchain:
        checkout = args.llama_checkout or Path(".")
        def version(command: list[str]) -> str:
            try:
                return subprocess.run(command, check=False, capture_output=True, text=True, timeout=30).stdout.splitlines()[0]
            except (OSError, IndexError):
                return "unavailable"
        freeze_path = args.toolchain.parent / "pip-freeze.txt"
        freeze = freeze_path.read_text(encoding="utf-8") if freeze_path.is_file() else ""
        dependency_lock = {}
        if args.wheelhouse_lock is not None:
            dependency_lock = json.loads(args.wheelhouse_lock.read_text(encoding="utf-8"))
            if dependency_lock.get("schema") != "local_bmo.j1m.wheelhouse-lock.v1" or not dependency_lock.get("artifacts"):
                raise ValueError("toolchain receipt requires a nonempty dependency wheelhouse lock")
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        try:
            os_packages = subprocess.run(
                ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\\n", "python3-venv", "cmake", "build-essential"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError):
            os_packages = ["unavailable"]
        payload = {"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": head, "python": version([sys.executable, "--version"]), "cmake": version(["cmake", "--version"]), "compiler": version(["cc", "--version"]), "os_packages": os_packages, "pip_freeze": freeze, "dependency_wheelhouse_lock": dependency_lock}
        args.toolchain.parent.mkdir(parents=True, exist_ok=True)
        args.toolchain.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.inspect_tensors:
        gguf_path, metadata_path = (Path(value) for value in args.inspect_tensors)
        try:
            from gguf import GGUFReader
            reader = GGUFReader(str(gguf_path))
            tensors = [{"name": tensor.name, "shape": [_normalize_gguf_value(dimension) for dimension in tensor.shape], "type": str(tensor.tensor_type)} for tensor in reader.tensors]
            wanted_fields = {"general.architecture", "general.file_type", "general.version", "tokenizer.chat_template"}
            fields = {str(key): _reader_field_value(value) for key, value in reader.fields.items() if str(key) in wanted_fields}
            architecture = str(fields.get("general.architecture", "")).lower().replace(".", "").replace("_", "")
            if architecture != "qwen35":
                raise ValueError("GGUF architecture is not qwen35")
            chat_template = str(fields.get("tokenizer.chat_template", ""))
            if not chat_template:
                raise ValueError("GGUF has no embedded tokenizer chat template")
            if args.source_receipt is not None:
                source_receipt = json.loads(args.source_receipt.read_text(encoding="utf-8"))
                expected_chat_hash = str(source_receipt.get("chat_template_sha256", ""))
                actual_chat_hash = hashlib.sha256(chat_template.encode("utf-8")).hexdigest()
                if not expected_chat_hash or actual_chat_hash != expected_chat_hash:
                    raise ValueError("GGUF chat template does not match the verified source receipt")
            metadata_names = [str(key).lower() for key in reader.fields]
            tensor_names = [str(tensor.name).lower() for tensor in reader.tensors]
            vision_terms = ("vision", "mmproj", "visual", "image")
            vision_projection_present = any(any(term in name for term in vision_terms) for name in [*metadata_names, *tensor_names])
            if vision_projection_present:
                raise ValueError("vision/mmproj metadata or tensor is forbidden")
            fields["gguf.version"] = _normalize_gguf_value(getattr(reader, "version", "unknown"))
            payload = {"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": len(tensors), "tensors": tensors, "gguf_metadata": fields, "vision_projection_present": vision_projection_present, "chat_template_sha256": hashlib.sha256(chat_template.encode("utf-8")).hexdigest()}
        except Exception as exc:
            payload = {"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "inspection_failed", "error_type": type(exc).__name__, "text_only": True}
            raise
        metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.scan:
        scan_artifacts(args.scan)
        return 0
    if args.post_cleanup:
        post_cleanup_verify(args.post_cleanup)
        return 0
    if args.verify_source:
        receipt = verify_source(args.verify_source, args.lock)
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.mark_source:
        if not args.revision:
            raise ValueError("--revision is required with --mark-source")
        mark_source(args.mark_source, args.revision)
        return 0
    if args.manifest:
        config = load_config(args.config)
        names = config["artifacts"]["allowlist"]
        write_artifacts(args.manifest, names, commands=command_plan(config), llama_revision=config["llama_cpp"]["revision"])
        return 0
    if args.prove:
        import platform
        receipt = {"schema": "local_bmo.j1m.proving-receipt.v1", "host": platform.node(), "python": platform.python_version(), "text_only": True, "conversion": "not-run"}
        if args.scratch and args.min_scratch_gib:
            receipt["scratch"] = check_scratch(args.scratch, args.min_scratch_gib)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.scratch and args.min_scratch_gib:
        print(json.dumps(check_scratch(args.scratch, args.min_scratch_gib), sort_keys=True))
        return 0
    if args.run:
        config = load_config(args.config)
        commands = command_plan(config, runner=str(Path(__file__).resolve()), config_path="/scratch/j1m/j1m-config.json")
        receipts = run_commands(commands, ROOT / config["resources"]["progress_path"], token_file=args.token_file, receipt_path=Path("/scratch/j1m/artifacts/command-receipt.json"))
        return 0 if receipts and all(item["status"] == "completed" for item in receipts) else 1
    config = load_config(args.config)
    plan = build_plan(config)
    if args.plan:
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(plan, indent=2, sort_keys=True))
    if args.execute:
        print("execution remains host-local and requires Sol's explicit review; no provider mutation was attempted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
