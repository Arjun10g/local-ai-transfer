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
        shutil.copy2(source, target)
        receipt.append({"name": name, "size_bytes": target.stat().st_size})
    return receipt


def verify_local_bundle(local_dir: Path) -> None:
    manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = {item["name"]: item["sha256"] for item in manifest["artifacts"]}
    for name, digest in expected.items():
        path = local_dir / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"local bundle checksum mismatch: {name}")


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
