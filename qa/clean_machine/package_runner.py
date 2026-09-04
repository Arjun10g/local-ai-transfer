#!/usr/bin/env python3
"""Build a portable package from an explicit allowlist.

This command never downloads or resolves dependencies. It requires a supplied
engine binary so a fixture skeleton cannot accidentally be called a release.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from .package import PACKAGE_ALLOWLIST, checksums, scan_tree


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an allowlist-only portable package")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    source_result = scan_tree(source, require_runtime=True)
    if source_result["status"] != "PASS":
        print(json.dumps(source_result, indent=2, sort_keys=True))
        return 1
    output.mkdir(parents=True, exist_ok=True)
    for relative in sorted(PACKAGE_ALLOWLIST):
        source_file = source / relative
        if source_file.is_file():
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination)
    files = sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file() and path.name not in {"RELEASE_MANIFEST.json", "CHECKSUMS.sha256"})
    manifest = {
        "schema_version": "release-manifest.v1",
        "kind": "portable-windows-x64",
        "model_included": False,
        "files": files + ["RELEASE_MANIFEST.json", "CHECKSUMS.sha256"],
        "native_windows_launch": "REQUIRES_NATIVE_ACCEPTANCE",
        "model_artifact": "EXTERNAL_AND_VERIFIED_SEPARATELY",
    }
    (output / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    all_files = sorted(set(files + ["RELEASE_MANIFEST.json", "CHECKSUMS.sha256"]))
    # CHECKSUMS cannot include its own final digest; it records every other file.
    digest_lines = [f"{digest}  {name}" for name, digest in checksums(output, files + ["RELEASE_MANIFEST.json"]).items()]
    (output / "CHECKSUMS.sha256").write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
    result = scan_tree(output, require_runtime=True)
    result["files"] = all_files
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
