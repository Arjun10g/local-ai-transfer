#!/usr/bin/env python3
"""Select the small, deployable J1M receipt set for local transfer.

The remote build may use BF16 and Q8 intermediates, but this target has about
12 GiB free.  Local transfer is therefore an explicit Q4_K_M-plus-receipts
allowlist; no wildcard or whole-directory download is accepted.
"""

from __future__ import annotations

import argparse
import json
import shutil
import hashlib
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.j1m_runner import DEFAULT_CONFIG, _safe_artifact_name, load_config

LOCAL_ALLOWLIST = frozenset({
    "Qwen3.5-9B-Q4_K_M.gguf",
    "manifest.json",
    "checksums.sha256",
    "tensor-metadata.json",
    "source-model-receipt.json",
    "conversion-receipt.json",
    "model-receipt.json",
    "toolchain.json",
    "command-receipt.json",
    "scan-receipt.json",
})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def select_local_artifacts(names: list[str]) -> list[str]:
    selected = []
    for name in names:
        _safe_artifact_name(name)
        if name not in LOCAL_ALLOWLIST:
            raise ValueError(f"local transfer refuses non-deployable/intermediate artifact: {name}")
        selected.append(name)
    if "Qwen3.5-9B-Q4_K_M.gguf" not in selected:
        raise ValueError("local transfer requires the Q4_K_M deployable artifact")
    return selected


def copy_selected(remote_dir: Path, local_dir: Path, names: list[str]) -> list[dict[str, object]]:
    selected = select_local_artifacts(names)
    local_dir.mkdir(parents=True, exist_ok=True)
    receipt = []
    for name in selected:
        source = remote_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        target = local_dir / name
        temporary = target.with_name(f".{target.name}.partial")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        receipt.append({"name": name, "size_bytes": target.stat().st_size})
    return receipt


def verify_local_bundle(local_dir: Path) -> None:
    manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("inventory_scope") != "post_cleanup_deployable_allowlist" or manifest.get("deployable_model_artifacts") != ["Qwen3.5-9B-Q4_K_M.gguf"]:
        raise ValueError("manifest does not identify the post-cleanup Q4 deployable scope")
    expected = {}
    for item in manifest.get("artifacts", []):
        if not isinstance(item, dict) or set(item) != {"name", "size_bytes", "sha256"}:
            raise ValueError("manifest contains an invalid artifact entry")
        name = str(item["name"])
        _safe_artifact_name(name)
        if name not in LOCAL_ALLOWLIST or name in expected:
            raise ValueError(f"manifest contains a non-deployable or duplicate artifact: {name}")
        expected[name] = str(item["sha256"])
    if "Qwen3.5-9B-Q4_K_M.gguf" not in expected or any(name in expected for name in ("Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf")):
        raise ValueError("manifest does not contain the deployable Q4 artifact")
    checksum_lines = (local_dir / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    checksums: dict[str, str] = {}
    for line in checksum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError("malformed checksum entry")
        digest, name = parts
        _safe_artifact_name(name)
        if name in checksums or name not in {*expected, "manifest.json"}:
            raise ValueError(f"checksum contains an unexpected or duplicate artifact: {name}")
        checksums[name] = digest
    if set(checksums) != {*expected, "manifest.json"}:
        raise ValueError("checksum set does not exactly match the deployable manifest")
    for name, digest in expected.items():
        if checksums[name] != digest:
            raise ValueError(f"manifest/checksum disagreement: {name}")
        path = local_dir / name
        if not path.is_file() or path.stat().st_size != next(item["size_bytes"] for item in manifest["artifacts"] if item["name"] == name) or _sha256(path) != digest:
            raise ValueError(f"local bundle checksum mismatch: {name}")
    if _sha256(local_dir / "manifest.json") != checksums["manifest.json"]:
        raise ValueError("manifest checksum mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--remote-dir", type=Path, required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    names = list(config["artifacts"]["local_fetch_allowlist"])
    receipt = copy_selected(args.remote_dir, args.local_dir, names)
    verify_local_bundle(args.local_dir)
    print(json.dumps({"selected": receipt, "checksums": "verified"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
