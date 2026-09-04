#!/usr/bin/env python3
"""Verify the exact, bounded Vulkan closure vendored from llama.cpp.

The Vulkan backend is source-generated (including SPIR-V shader sources), so a
single directory-presence check is not sufficient provenance.  This module
accepts only the immutable upstream revision and an explicit per-file SHA-256
lock.  It rejects missing, modified, symlinked, or unexpected files before a
Vulkan plan can be made.
"""
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
DEFAULT_MANIFEST = DEFAULT_ROOT / "ggml-vulkan-source-lock.json"
MAX_MANIFEST_BYTES = 512 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_ROOT_FILES = {"ggml/include/ggml-vulkan.h", "LICENSE"}
ALLOWED_ROOT_DIR = "ggml/src/ggml-vulkan"


class VulkanClosureError(ValueError):
    """The pinned source closure cannot be authenticated."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_closure_files(root: Path) -> set[str]:
    files: set[str] = set()
    for rel in ALLOWED_ROOT_FILES:
        if (root / rel).exists():
            files.add(rel)
    backend_root = root / ALLOWED_ROOT_DIR
    if backend_root.exists():
        for path in backend_root.rglob("*"):
            if path.is_file() or path.is_symlink():
                files.add(path.relative_to(root).as_posix())
    return files


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise VulkanClosureError("Vulkan source lock must be a bounded regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VulkanClosureError("Vulkan source lock is not valid JSON") from exc
    if not isinstance(value, dict):
        raise VulkanClosureError("Vulkan source lock must be an object")
    return value


def load_expected(manifest_path: Path) -> tuple[str, dict[str, str]]:
    manifest = _load_manifest(manifest_path)
    if manifest.get("schema") != "local_bmo.llama-vulkan-source-lock.v1":
        raise VulkanClosureError("Vulkan source lock schema is invalid")
    if manifest.get("repository") != UPSTREAM_REPOSITORY or manifest.get("revision") != PINNED_REVISION:
        raise VulkanClosureError("Vulkan source lock is not for the pinned official revision")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > 512:
        raise VulkanClosureError("Vulkan source lock file list is invalid")
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise VulkanClosureError("Vulkan source lock has an invalid file entry")
        rel = entry["path"]
        digest = entry["sha256"]
        if not isinstance(rel, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise VulkanClosureError("Vulkan source lock has an invalid path or digest")
        normalized = Path(rel).as_posix()
        if normalized != rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise VulkanClosureError("Vulkan source lock contains an unsafe path")
        if rel not in ALLOWED_ROOT_FILES and not rel.startswith(ALLOWED_ROOT_DIR + "/"):
            raise VulkanClosureError("Vulkan source lock contains a path outside the backend closure")
        if rel in expected:
            raise VulkanClosureError("Vulkan source lock contains a duplicate path")
        expected[rel] = digest
    if list(expected) != sorted(expected):
        raise VulkanClosureError("Vulkan source lock entries must be sorted")
    return PINNED_REVISION, expected


def verify_closure(root: Path = DEFAULT_ROOT, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Verify exact closure contents and return bounded provenance metadata."""
    revision, expected = load_expected(manifest_path)
    actual = _relative_closure_files(root)
    expected_names = set(expected)
    if actual != expected_names:
        missing = sorted(expected_names - actual)
        unexpected = sorted(actual - expected_names)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing[:3]))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected[:3]))
        raise VulkanClosureError("Vulkan source closure file set mismatch (" + "; ".join(details) + ")")
    for rel, expected_digest in expected.items():
        path = root / rel
        if path.is_symlink() or not path.is_file():
            raise VulkanClosureError(f"Vulkan source closure entry is not a regular file: {rel}")
        actual_digest = sha256(path)
        if actual_digest != expected_digest:
            raise VulkanClosureError(f"Vulkan source closure hash mismatch: {rel}")
    lock_digest = sha256(manifest_path)
    return {
        "repository": UPSTREAM_REPOSITORY,
        "revision": revision,
        "manifest": manifest_path.name,
        "manifest_sha256": lock_digest,
        "file_count": len(expected),
        "verified": True,
    }


def write_manifest(root: Path, manifest_path: Path) -> None:
    paths = sorted(_relative_closure_files(root))
    if not paths:
        raise VulkanClosureError("no Vulkan closure files found")
    entries = [{"path": rel, "sha256": sha256(root / rel)} for rel in paths]
    manifest = {
        "schema": "local_bmo.llama-vulkan-source-lock.v1",
        "repository": UPSTREAM_REPOSITORY,
        "revision": PINNED_REVISION,
        "files": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--write", action="store_true", help="write the lock from the current closure")
    args = parser.parse_args()
    try:
        if args.write:
            write_manifest(args.root, args.manifest)
        print(json.dumps(verify_closure(args.root, args.manifest), sort_keys=True))
    except (OSError, VulkanClosureError) as exc:
        print(f"Vulkan source closure refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
