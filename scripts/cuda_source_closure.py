#!/usr/bin/env python3
"""Verify the exact pinned llama.cpp CUDA backend source closure."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

UPSTREAM_REPOSITORY = "https://github.com/ggml-org/llama.cpp"
PINNED_REVISION = "3581ba0cf591b3f772fbb002de0f70e294bc0396"
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "llama.cpp"
DEFAULT_MANIFEST = DEFAULT_ROOT / "ggml-cuda-source-lock.json"
BACKEND_DIR = "ggml/src/ggml-cuda"
ROOT_FILES = {"ggml/include/ggml-cuda.h", "LICENSE"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CudaClosureError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> set[str]:
    result = {rel for rel in ROOT_FILES if (root / rel).exists()}
    directory = root / BACKEND_DIR
    if directory.exists():
        result.update(path.relative_to(root).as_posix() for path in directory.rglob("*") if path.is_file() or path.is_symlink())
    return result


def _manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise CudaClosureError("CUDA source lock must be a bounded regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CudaClosureError("CUDA source lock is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != "local_bmo.llama-cuda-source-lock.v1" or value.get("repository") != UPSTREAM_REPOSITORY or value.get("revision") != PINNED_REVISION:
        raise CudaClosureError("CUDA source lock is not for the pinned official revision")
    entries = value.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > 512:
        raise CudaClosureError("CUDA source lock file list is invalid")
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"} or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str) or not SHA256.fullmatch(entry["sha256"]):
            raise CudaClosureError("CUDA source lock entry is invalid")
        rel = entry["path"]
        if Path(rel).as_posix() != rel or rel.startswith("/") or ".." in Path(rel).parts or (rel not in ROOT_FILES and not rel.startswith(BACKEND_DIR + "/")) or rel in expected:
            raise CudaClosureError("CUDA source lock contains an unsafe or duplicate path")
        expected[rel] = entry["sha256"]
    if list(expected) != sorted(expected):
        raise CudaClosureError("CUDA source lock entries must be sorted")
    return expected


def verify_closure(root: Path = DEFAULT_ROOT, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    expected = _manifest(manifest_path)
    actual = _files(root)
    if actual != set(expected):
        raise CudaClosureError("CUDA source closure file set mismatch")
    for rel, digest in expected.items():
        path = root / rel
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise CudaClosureError(f"CUDA source closure hash mismatch: {rel}")
    return {"repository": UPSTREAM_REPOSITORY, "revision": PINNED_REVISION, "manifest": manifest_path.name, "manifest_sha256": _sha256(manifest_path), "file_count": len(expected), "verified": True}


def write_manifest(root: Path, manifest_path: Path) -> None:
    paths = sorted(_files(root))
    manifest_path.write_text(json.dumps({"schema": "local_bmo.llama-cuda-source-lock.v1", "repository": UPSTREAM_REPOSITORY, "revision": PINNED_REVISION, "files": [{"path": rel, "sha256": _sha256(root / rel)} for rel in paths]}, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        if args.write:
            write_manifest(args.root, args.manifest)
        print(json.dumps(verify_closure(args.root, args.manifest), sort_keys=True))
    except (OSError, CudaClosureError) as exc:
        print(f"CUDA source closure refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
