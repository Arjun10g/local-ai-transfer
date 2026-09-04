"""Allowlist-driven Windows portable package scanner.

The scanner is intentionally usable on non-Windows CI. Native Windows launch,
DLL loading, job objects, and Unicode-path behavior require a separate native
Windows acceptance run and are reported as limitations.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

PACKAGE_ALLOWLIST = frozenset({
    "lae-engine-cpu.exe",
    "lae-engine-vulkan.exe",
    "lae-host.mjs",
    "Start-LocalAssistant.ps1",
    "Verify-Release.ps1",
    "config.example.json",
    "backend-profiles.json",
    "ui/index.html",
    "ui/app.js",
    "ui/app.css",
    "schemas/README.md",
    "licenses/README.md",
    "THIRD_PARTY_NOTICES.md",
    "SBOM.spdx.json",
    "RELEASE_MANIFEST.json",
    "CHECKSUMS.sha256",
    "README-OPERATOR.md",
})
REQUIRED_STATIC = frozenset({
    "lae-host.mjs", "Start-LocalAssistant.ps1", "config.example.json",
    "ui/index.html", "THIRD_PARTY_NOTICES.md", "SBOM.spdx.json",
    "RELEASE_MANIFEST.json", "CHECKSUMS.sha256", "README-OPERATOR.md",
})
FORBIDDEN_SUFFIXES = (".gguf", ".safetensors", ".pt", ".pth", ".onnx", ".bin", ".pem", ".key", ".pfx")
SECRET_RE = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret(?:[_-]?key)?)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{8,})")
KNOWN_DLLS = frozenset({"kernel32.dll", "user32.dll", "advapi32.dll", "ws2_32.dll", "vcruntime140.dll", "ucrtbase.dll", "vulkan-1.dll", "ntdll.dll", "shell32.dll", "ole32.dll"})


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def scan_secrets(data: bytes) -> bool:
    return SECRET_RE.search(data.decode("utf-8", errors="ignore")) is not None


def scan_tree(root: str | Path, *, require_runtime: bool = False) -> dict[str, object]:
    base = Path(root).resolve()
    files = _relative_files(base) if base.is_dir() else set()
    findings: list[str] = []
    findings.extend(f"unallowlisted:{name}" for name in sorted(files - PACKAGE_ALLOWLIST))
    findings.extend(f"forbidden-artifact:{name}" for name in sorted(files) if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES or "node_modules" in Path(name).parts)
    if not REQUIRED_STATIC.issubset(files):
        findings.extend(f"missing-required:{name}" for name in sorted(REQUIRED_STATIC - files))
    if require_runtime and "lae-engine-cpu.exe" not in files:
        findings.append("missing-required:lae-engine-cpu.exe")
    secret_files: list[str] = []
    for name in sorted(files):
        path = base / name
        data = path.read_bytes()
        if scan_secrets(data):
            secret_files.append(name)
        if len(data) > 64 * 1024 * 1024:
            findings.append(f"oversized:{name}")
    findings.extend(f"secret-pattern:{name}" for name in secret_files)
    return {"status": "PASS" if not findings else "FAIL", "files": sorted(files), "findings": findings, "native_windows_launch": "SKIP"}


def scan_binary_dependencies(path: str | Path) -> dict[str, object]:
    """Best-effort PE import string scan; native loader evidence remains separate."""
    file = Path(path)
    data = file.read_bytes()
    candidates = {match.decode("ascii").lower() for match in re.findall(rb"[A-Za-z0-9_.-]+\.dll", data, flags=re.I)}
    unresolved = sorted(candidates - KNOWN_DLLS)
    return {"path": file.name, "imports": sorted(candidates), "unresolved": unresolved, "status": "PASS" if not unresolved else "FAIL", "native_loader_test": "SKIP"}


def checksums(root: str | Path, files: Iterable[str]) -> dict[str, str]:
    base = Path(root)
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest() for name in sorted(files)}
