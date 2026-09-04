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
import secrets
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "model" / "conversion" / "j1m-config.json"
SOURCE_LOCK = ROOT / "model" / "source-lock" / "qwen35-9b.source-lock.json"
TOKEN_ENV = "HF_TOKEN"


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
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        "text_only": True,
        "artifacts": artifacts,
        "tensor_metadata": tensor_metadata or {"status": "pending_converter_receipt"},
    }


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
    source = json.loads(source_receipt.read_text(encoding="utf-8"))
    if source.get("status") != "verified":
        raise ValueError("source receipt is not verified")
    tensor = json.loads(tensor_path.read_text(encoding="utf-8"))
    if tensor.get("status") != "verified":
        raise ValueError("tensor metadata was not verified by the pinned GGUF reader")
    artifact_hashes = {name: {"size_bytes": (output_dir / name).stat().st_size, "sha256": _sha256(output_dir / name)} for name in primary}
    toolchain_path = output_dir / "toolchain.json"
    if not toolchain_path.is_file():
        raise ValueError("toolchain receipt is required before model receipt")
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    converter_commands = [command for command in (commands or []) if any("convert_hf_to_gguf.py" in part for part in command) or "Q4_K_M" in command]
    (output_dir / "conversion-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.conversion-receipt.v1", "status": "conversion-complete", "text_only": True, "source_revision": source.get("revision"), "llama_cpp_revision": llama_revision, "artifacts": artifact_hashes, "converter_and_quantizer_argv": converter_commands, "toolchain": toolchain, "no_mmproj": True, "no_mtp": True}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "model-receipt.json").write_text(json.dumps({"schema": "local_bmo.j1m.model-receipt.v1", "status": "checksums-and-tensor-inventory-verified", "text_only": True, "q4_artifact": artifact_hashes.get("Qwen3.5-9B-Q4_K_M.gguf"), "tensor_metadata_sha256": _sha256(tensor_path), "tokenizer_sha256": source.get("tokenizer_sha256"), "chat_template_sha256": source.get("chat_template_sha256"), "license_sha256": source.get("license_sha256")}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = artifact_manifest(output_dir, [*primary, "tensor-metadata.json", "source-model-receipt.json", "conversion-receipt.json", "model-receipt.json", "toolchain.json"])
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
    return [
        ["git", "clone", "--filter=blob:none", config["llama_cpp"]["repository"], llama["checkout"]],
        ["git", "-C", llama["checkout"], "checkout", "--detach", llama["revision"]],
        ["python3", "-m", "venv", "/scratch/j1m/venv"],
        ["/scratch/j1m/venv/bin/pip", "install", "--disable-pip-version-check", "--no-input", "-r", f"{llama['checkout']}/{config['python_dependencies']['requirements_file']}", "-e", f"{llama['checkout']}/{config['python_dependencies']['local_gguf_package']}"],
        [python_exec, runner, "--pip-freeze", "/scratch/j1m/pip-freeze.txt"],
        [python_exec, runner, "--toolchain", f"{output}/toolchain.json", "--llama-checkout", llama["checkout"]],
        ["git", "--version"],
        ["cmake", "--version"],
        ["python3", "--version"],
        ["mkdir", "-p", source, output],
        ["python3", runner, "--verify-llama", llama["checkout"], llama["revision"]],
        ["cmake", "-S", llama["checkout"], "-B", f"{llama['checkout']}/build", "-DGGML_CUDA=OFF", "-DLLAMA_BUILD_TOOLS=ON"],
        ["cmake", "--build", f"{llama['checkout']}/build", "--target", "llama-quantize", "-j2"],
        [hf_exec, "download", config["source"]["model_id"], "--revision", config["source"]["revision"], "--local-dir", source, "--local-dir-use-symlinks", "false"],
        [python_exec, runner, "--mark-source", source, "--revision", config["source"]["revision"]],
        [python_exec, "-u", runner, "--verify-source", source, "--lock", "/scratch/j1m/qwen35-9b.source-lock.json", "--receipt", f"{output}/source-model-receipt.json"],
        [python_exec, converter, source, "--outfile", f"{output}/Qwen3.5-9B-bf16.gguf", "--outtype", "bf16", "--no-mtp"],
        [python_exec, converter, source, "--outfile", f"{output}/Qwen3.5-9B-Q8_0.gguf", "--outtype", "q8_0", "--no-mtp"],
        [f"{llama['quantizer']}", f"{output}/Qwen3.5-9B-bf16.gguf", f"{output}/Qwen3.5-9B-Q4_K_M.gguf", "Q4_K_M"],
        [python_exec, runner, "--inspect-tensors", f"{output}/Qwen3.5-9B-Q4_K_M.gguf", f"{output}/tensor-metadata.json"],
        [python_exec, runner, "--config", config_path, "--manifest", output],
        ["rm", "-f", f"{output}/Qwen3.5-9B-bf16.gguf", f"{output}/Qwen3.5-9B-Q8_0.gguf"],
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


def run_commands(commands: list[list[str]], progress_path: Path, *, cwd: Path | None = None, token_file: Path | None = None) -> list[dict[str, Any]]:
    """Run an already-reviewed argv plan, recording progress before each stage."""

    receipts = []
    for index, command in enumerate(commands):
        if not command or any("\x00" in str(part) for part in command):
            raise ValueError("invalid empty/NUL command")
        write_progress(progress_path, f"stage-{index + 1}-starting", argv=command)
        try:
            environment = None
            if token_file is not None:
                environment = {**os.environ, TOKEN_ENV: read_token_file(token_file)}
            completed = subprocess.run(command, cwd=cwd, check=False, timeout=6 * 60 * 60, env=environment)
            receipt = {"stage": index + 1, "exit_code": completed.returncode, "status": "completed" if completed.returncode == 0 else "failed"}
        except subprocess.TimeoutExpired:
            receipt = {"stage": index + 1, "exit_code": None, "status": "transport_timeout"}
        receipts.append(receipt)
        write_progress(progress_path, f"stage-{index + 1}-{receipt['status']}", **receipt)
        if receipt["status"] != "completed":
            break
    return receipts


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
        "expected_scratch_gib": config["resources"]["scratch_gib"],
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
    parser.add_argument("--run", action="store_true", help="run the reviewed local argv plan")
    parser.add_argument("--token-file", type=Path, help="private remote token file; path only, never token material")
    parser.add_argument("--pip-freeze", type=Path)
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--llama-checkout", type=Path)
    parser.add_argument("--inspect-tensors", nargs=2, metavar=("GGUF", "OUTPUT"))
    parser.add_argument("--execute", action="store_true", help="reserved for an already-approved host; never provisions")
    args = parser.parse_args(argv)
    config = load_config(args.config)
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
    if args.toolchain:
        checkout = args.llama_checkout or Path(".")
        def version(command: list[str]) -> str:
            try:
                return subprocess.run(command, check=False, capture_output=True, text=True, timeout=30).stdout.splitlines()[0]
            except (OSError, IndexError):
                return "unavailable"
        freeze_path = args.toolchain.parent / "pip-freeze.txt"
        freeze = freeze_path.read_text(encoding="utf-8") if freeze_path.is_file() else ""
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        payload = {"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": head, "python": version([sys.executable, "--version"]), "cmake": version(["cmake", "--version"]), "compiler": version(["cc", "--version"]), "pip_freeze": freeze}
        args.toolchain.parent.mkdir(parents=True, exist_ok=True)
        args.toolchain.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.inspect_tensors:
        gguf_path, metadata_path = (Path(value) for value in args.inspect_tensors)
        try:
            from gguf import GGUFReader
            reader = GGUFReader(str(gguf_path))
            tensors = [{"name": tensor.name, "shape": list(tensor.shape), "type": str(tensor.tensor_type)} for tensor in reader.tensors]
            payload = {"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": len(tensors), "tensors": tensors}
        except Exception as exc:
            payload = {"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "inspection_failed", "error_type": type(exc).__name__, "text_only": True}
            raise
        metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        names = config["artifacts"]["allowlist"]
        write_artifacts(args.manifest, names, commands=command_plan(config), llama_revision=config["llama_cpp"]["revision"])
        return 0
    if args.prove:
        import platform
        receipt = {"schema": "local_bmo.j1m.proving-receipt.v1", "host": platform.node(), "python": platform.python_version(), "text_only": True, "conversion": "not-run"}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.run:
        commands = command_plan(config, runner=str(Path(__file__).resolve()), config_path="/scratch/j1m/j1m-config.json")
        receipts = run_commands(commands, ROOT / config["resources"]["progress_path"], token_file=args.token_file)
        return 0 if receipts and all(item["status"] == "completed" for item in receipts) else 1
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
