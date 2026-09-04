#!/usr/bin/env python3
"""Build a portable package from an explicit allowlist.

This command never downloads or resolves dependencies. It requires a supplied
engine binary so a fixture skeleton cannot accidentally be called a release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from .package import HOST_RUNTIME_FILES, checksums, scan_tree


RELEASE_FILES = frozenset({
    "Start-LocalAssistant.ps1", "Verify-Release.ps1", "host-config.example.json",
    "THIRD_PARTY_NOTICES.md", "README-OPERATOR.md",
})
NODE_VERSION = "24.20.0"
NODE_EXE_SHA256 = "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"
NODE_LICENSE_SHA256 = "5888dbb9a1d2b18f2c3e6c5f6af1b39de658372b402a0577b002777f14c62ace"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_package(source: Path, engine: Path, node: Path, node_license: Path, output: Path) -> dict[str, object]:
    """Build the CPU portable tree from repository sources and one explicit PE."""
    source = source.resolve()
    engine_input = engine.absolute()
    node_input = node.absolute()
    node_license_input = node_license.absolute()
    output_input = output.absolute()
    if engine_input.is_symlink() or node_input.is_symlink() or node_license_input.is_symlink():
        return {"status": "FAIL", "stage": "inputs", "findings": ["linked-runtime-input"]}
    if output_input.is_symlink():
        return {"status": "FAIL", "stage": "output", "findings": ["linked-output-directory"]}
    engine = engine_input.resolve()
    node = node_input.resolve()
    node_license = node_license_input.resolve()
    output = output_input.resolve()
    release_source = source / "release" / "windows"
    source_result = scan_tree(release_source, require_runtime=False)
    if source_result["status"] != "PASS":
        return {**source_result, "stage": "source-skeleton"}
    if not engine.is_file() or engine.is_symlink() or engine.name.lower() != "lae-engine-cpu.exe":
        return {"status": "FAIL", "stage": "engine", "findings": ["engine-must-be-regular-lae-engine-cpu.exe"]}
    try:
        with engine.open("rb") as stream:
            magic = stream.read(2)
        if magic != b"MZ":
            return {"status": "FAIL", "stage": "engine", "findings": ["engine-is-not-a-windows-pe"]}
    except OSError:
        return {"status": "FAIL", "stage": "engine", "findings": ["engine-unreadable"]}
    if not node.is_file() or node.is_symlink() or node.name.lower() != "node.exe":
        return {"status": "FAIL", "stage": "node", "findings": ["node-must-be-regular-node.exe"]}
    try:
        if file_sha256(node) != NODE_EXE_SHA256:
            return {"status": "FAIL", "stage": "node", "findings": [f"node-identity-mismatch-required-{NODE_VERSION}-win-x64"]}
    except OSError:
        return {"status": "FAIL", "stage": "node", "findings": ["node-unreadable"]}
    try:
        valid_node_license = node_license.is_file() and not node_license.is_symlink() and file_sha256(node_license) == NODE_LICENSE_SHA256
    except OSError:
        valid_node_license = False
    if not valid_node_license:
        return {"status": "FAIL", "stage": "node-license", "findings": [f"node-license-mismatch-required-{NODE_VERSION}"]}
    if output.exists() and any(output.iterdir()):
        return {"status": "FAIL", "stage": "output", "findings": ["output-directory-not-empty"]}
    output.mkdir(parents=True, exist_ok=True)

    source_map = {relative: source / relative for relative in HOST_RUNTIME_FILES}
    source_map.update({relative: release_source / relative for relative in RELEASE_FILES})
    source_map["lae-engine-cpu.exe"] = engine
    source_map["runtime/node.exe"] = node
    source_map["licenses/Node.js-LICENSE.txt"] = node_license
    source_map["licenses/llama.cpp-LICENSE.txt"] = source / "vendor" / "llama.cpp" / "LICENSE"
    missing = sorted(relative for relative, path in source_map.items() if not path.is_file() or path.is_symlink())
    if missing:
        return {"status": "FAIL", "stage": "source-closure", "findings": [f"missing-or-linked-source:{name}" for name in missing]}
    for relative, source_file in sorted(source_map.items()):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, destination)

    sbom = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": "local-assistant-engine-portable-windows-x64-cpu",
        "documentNamespace": "https://example.invalid/local-assistant-engine/sbom/portable-windows-x64-cpu",
        "creationInfo": {"createdBy": ["Tool: qa.clean_machine.package_runner"], "created": "1970-01-01T00:00:00Z"},
        "packages": [
            {"name": "local-assistant-engine", "SPDXID": "SPDXRef-Package-LAE", "versionInfo": "0.1.0", "downloadLocation": "NOASSERTION", "filesAnalyzed": False, "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION", "copyrightText": "NOASSERTION"},
            {"name": "llama.cpp", "SPDXID": "SPDXRef-Package-LlamaCpp", "versionInfo": "3581ba0cf591b3f772fbb002de0f70e294bc0396", "downloadLocation": "https://github.com/ggml-org/llama.cpp", "filesAnalyzed": False, "licenseConcluded": "MIT", "licenseDeclared": "MIT", "copyrightText": "NOASSERTION"},
            {"name": "Node.js", "SPDXID": "SPDXRef-Package-Node", "versionInfo": NODE_VERSION, "downloadLocation": f"https://nodejs.org/download/release/v{NODE_VERSION}/win-x64/node.exe", "checksums": [{"algorithm": "SHA256", "checksumValue": NODE_EXE_SHA256}], "filesAnalyzed": False, "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION", "copyrightText": "NOASSERTION"},
        ],
    }
    (output / "SBOM.spdx.json").write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    files = sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file() and path.name not in {"RELEASE_MANIFEST.json", "CHECKSUMS.sha256"})
    manifest = {
        "schema_version": "release-manifest.v1",
        "kind": "portable-windows-x64-cpu",
        "model_included": False,
        "files": files + ["RELEASE_MANIFEST.json", "CHECKSUMS.sha256"],
        "native_windows_launch": "REQUIRES_NATIVE_ACCEPTANCE",
        "model_artifact": "EXTERNAL_AND_VERIFIED_BY_ENGINE",
        "target_prerequisites": ["Windows-x64"],
        "bundled_node": {"version": NODE_VERSION, "sha256": NODE_EXE_SHA256},
        "python_required_on_target": False,
    }
    (output / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest_lines = [f"{digest}  {name}" for name, digest in checksums(output, files + ["RELEASE_MANIFEST.json"]).items()]
    (output / "CHECKSUMS.sha256").write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
    result = scan_tree(output, require_runtime=True)
    result["stage"] = "package"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an allowlist-only portable package")
    parser.add_argument("--source", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--node-license", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = build_package(Path(args.source), Path(args.engine), Path(args.node), Path(args.node_license), Path(args.output))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
