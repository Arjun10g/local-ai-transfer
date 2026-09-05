"""Bounded advisory scanner for Windows release sources.

The source lint is useful on non-Windows CI, but it never authorizes a runtime
package. Runtime-package scans refuse before touching the supplied path until a
handle-relative, no-follow verifier can bind the complete tree.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable

HOST_RUNTIME_FILES = frozenset({
    "portable-supervisor.mjs",
    "host/agent/assistant-events.mjs",
    "host/agent/config.mjs",
    "host/agent/controller.mjs",
    "host/agent/tool-envelope.mjs",
    "host/engine/native-engine-client.mjs",
    "host/providers/browser-actions.mjs",
    "host/providers/copilot-cli.mjs",
    "host/providers/copilot-context.mjs",
    "host/providers/index.mjs",
    "host/providers/microsoft-graph-auth.mjs",
    "host/providers/microsoft-graph.mjs",
    "host/providers/operator-grants.mjs",
    "host/providers/operator-tool-policy.mjs",
    "host/providers/provider-common.mjs",
    "host/server/host-server.mjs",
    "host/tools/local/argument-validation.mjs",
    "host/tools/local/filesystem.mjs",
    "host/tools/local/index.mjs",
    "host/tools/local/platform-safety.mjs",
    "host/tools/local/process-run.mjs",
    "host/tools/local/system-tools.mjs",
    "host/tools/local/workspace-policy.mjs",
    "host/tools/time-now.mjs",
    "ui/index.html",
    "ui/app.js",
    "ui/styles.css",
})
PACKAGE_ALLOWLIST = HOST_RUNTIME_FILES | frozenset({
    "lae-engine-cpu.exe",
    "runtime/node.exe",
    "Start-LocalAssistant.ps1",
    "Build-WindowsBackend.ps1",
    "Run-WindowsBackend.ps1",
    "windows_backend_plan.py",
    "Verify-Release.ps1",
    "config.example.json",
    "host-config.example.json",
    "backend-profiles.json",
    "backend-runtime.example.json",
    "ui/index.html",
    "ui/app.js",
    "ui/app.css",
    "schemas/README.md",
    "licenses/README.md",
    "licenses/Node.js-LICENSE.txt",
    "licenses/llama.cpp-LICENSE.txt",
    "THIRD_PARTY_NOTICES.md",
    "SBOM.spdx.json",
    "RELEASE_MANIFEST.json",
    "CHECKSUMS.sha256",
    "README-OPERATOR.md",
    "node-provenance.json",
})
REQUIRED_STATIC = frozenset({
    "Start-LocalAssistant.ps1",
    "Run-WindowsBackend.ps1",
    "windows_backend_plan.py",
    "config.example.json",
    "ui/index.html",
    "THIRD_PARTY_NOTICES.md",
    "SBOM.spdx.json",
    "RELEASE_MANIFEST.json",
    "CHECKSUMS.sha256",
    "README-OPERATOR.md",
})
PORTABLE_RUNTIME_REQUIRED = frozenset()
FORBIDDEN_PACKAGE_PATHS = frozenset({"lae-host.mjs"})
FORBIDDEN_SUFFIXES = (".gguf", ".safetensors", ".pt", ".pth", ".onnx", ".bin", ".pem", ".key", ".pfx")
SECRET_RE = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret(?:[_-]?key)?)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{8,})")
KNOWN_DLLS = frozenset({"kernel32.dll", "user32.dll", "advapi32.dll", "bcrypt.dll", "crypt32.dll", "dbghelp.dll", "iphlpapi.dll", "ole32.dll", "powrprof.dll", "psapi.dll", "shell32.dll", "userenv.dll", "version.dll", "winmm.dll", "ws2_32.dll", "vcruntime140.dll", "ucrtbase.dll", "vulkan-1.dll", "ntdll.dll"})
MAX_TREE_ENTRIES = 512
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 256 * 1024
MAX_TEXT_BYTES = 64 * 1024 * 1024
MAX_BINARY_BYTES = 256 * 1024 * 1024


class BoundedFileError(ValueError):
    """A source file is not a bounded stable regular file."""


def file_size_limit(path: Path) -> int:
    if path.name == "RELEASE_MANIFEST.json":
        return MAX_MANIFEST_BYTES
    if path.name == "CHECKSUMS.sha256":
        return MAX_CHECKSUM_BYTES
    if path.suffix.lower() in {".exe", ".dll"}:
        return MAX_BINARY_BYTES
    return MAX_TEXT_BYTES


def read_bounded_file(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > maximum:
            raise BoundedFileError("file is not regular or exceeds its byte bound")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum or after.st_size > maximum or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise BoundedFileError("file changed or exceeds its byte bound")
    return data


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or (os.name == "nt" and bool(getattr(info, "st_file_attributes", 0) & reparse_flag))


def path_chain_has_reparse(path: str | Path) -> bool:
    original = Path(os.path.abspath(os.fspath(path)))
    current = original
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            # On non-Windows CI, reject the supplied link but do not reject a
            # system alias such as macOS /var. Windows rejects every ancestor.
            if is_reparse(current) and (os.name == "nt" or current == original):
                return True
        if current.parent == current:
            return False
        current = current.parent


def _tree_entries(root: Path) -> tuple[list[Path], bool, bool]:
    entries: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                for raw in iterator:
                    path = Path(raw.path)
                    entries.append(path)
                    if len(entries) > MAX_TREE_ENTRIES:
                        return entries, True, False
                    if not is_reparse(path) and raw.is_dir(follow_symlinks=False):
                        pending.append(path)
        except OSError:
            return entries, False, True
    return entries, False, False


def scan_secrets(data: bytes) -> bool:
    return SECRET_RE.search(data.decode("utf-8", errors="ignore")) is not None


def scan_tree(root: str | Path, *, require_runtime: bool = False, metadata_only: bool = False) -> dict[str, object]:
    if require_runtime:
        return {
            "status": "FAIL",
            "readiness": "NOT_READY",
            "files": [],
            "findings": ["secure-handle-relative-package-scan-unavailable"],
            "dependencies": "SKIP-package-scan-refused",
            "native_windows_launch": "REFUSED",
            "authorization": "NONE",
        }

    base = Path(os.path.abspath(os.fspath(root)))
    findings: list[str] = []
    linked_chain = path_chain_has_reparse(base)
    if linked_chain:
        findings.append("linked-root-or-parent")
    entries, truncated, failed = _tree_entries(base) if base.is_dir() and not linked_chain else ([], False, False)
    if truncated:
        findings.append(f"tree-exceeds-{MAX_TREE_ENTRIES}-entries")
    if failed:
        findings.append("tree-enumeration-failed")
    files = {path.relative_to(base).as_posix() for path in entries if not is_reparse(path) and path.is_file()}
    findings.extend(f"linked-path:{path.relative_to(base).as_posix()}" for path in sorted(entries) if is_reparse(path))
    findings.extend(f"unallowlisted:{name}" for name in sorted(files - PACKAGE_ALLOWLIST))
    findings.extend(f"forbidden-entrypoint:{name}" for name in sorted(files & FORBIDDEN_PACKAGE_PATHS))
    findings.extend(f"forbidden-artifact:{name}" for name in sorted(files) if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES or "node_modules" in Path(name).parts)
    findings.extend(f"missing-required:{name}" for name in sorted(REQUIRED_STATIC - files))

    # Safe plan mode is metadata/path classification only. In particular, do
    # not open model weights, native binaries, or any other package content
    # merely to produce an advisory local plan.
    if metadata_only:
        return {
            "status": "PASS" if not findings else "FAIL",
            "files": sorted(files),
            "findings": findings,
            "dependencies": "SKIP-metadata-only",
            "native_windows_launch": "SKIP",
            "authorization": "ADVISORY-METADATA-ONLY",
        }

    if "RELEASE_MANIFEST.json" in files:
        try:
            manifest = __import__("json").loads(read_bounded_file(base / "RELEASE_MANIFEST.json", MAX_MANIFEST_BYTES).decode("utf-8"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise TypeError("manifest must contain a file list")
            listed = {str(name).replace("\\", "/") for name in manifest["files"]}
            if listed != files:
                findings.append("manifest-file-list-mismatch")
            if listed & FORBIDDEN_PACKAGE_PATHS:
                findings.append("manifest-exposes-generic-host-entrypoint")
            if manifest.get("model_included") is not False:
                findings.append("manifest-model-included")
        except (OSError, UnicodeError, ValueError, TypeError):
            findings.append("manifest-invalid")

    dependency_results: list[dict[str, object]] = []
    for name in sorted(files):
        path = base / name
        try:
            data = read_bounded_file(path, file_size_limit(path))
        except (BoundedFileError, OSError):
            findings.append(f"unreadable-or-oversized:{name}")
            continue
        if scan_secrets(data):
            findings.append(f"secret-pattern:{name}")
        if path.suffix.lower() in {".exe", ".dll"}:
            dependency = _scan_binary_data(path.name, data)
            dependency_results.append(dependency)
            findings.extend(f"unresolved-dll:{name}:{dll}" for dll in dependency["unresolved"])

    return {
        "status": "PASS" if not findings else "FAIL",
        "files": sorted(files),
        "findings": findings,
        "dependencies": dependency_results or "SKIP-no-binaries",
        "native_windows_launch": "SKIP",
        "authorization": "ADVISORY-SOURCE-LINT-ONLY",
    }


def _scan_binary_data(name: str, data: bytes) -> dict[str, object]:
    imports = {match.decode("ascii").lower() for match in re.findall(rb"[A-Za-z0-9_.-]+\.dll", data, flags=re.I)}
    unresolved = sorted(imports - KNOWN_DLLS)
    return {"path": name, "imports": sorted(imports), "unresolved": unresolved, "status": "PASS" if not unresolved else "FAIL", "native_loader_test": "SKIP"}


def scan_binary_dependencies(path: str | Path) -> dict[str, object]:
    file = Path(path)
    try:
        return _scan_binary_data(file.name, read_bounded_file(file, MAX_BINARY_BYTES))
    except (BoundedFileError, OSError):
        return {"path": file.name, "imports": [], "unresolved": [], "status": "FAIL", "error": "binary-unreadable-or-oversized", "native_loader_test": "SKIP"}


def checksums(root: str | Path, files: Iterable[str]) -> dict[str, str]:
    del root, files
    raise BoundedFileError("secure handle-relative package checksums are unavailable")
