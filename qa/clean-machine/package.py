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
    "Start-LocalAssistant.ps1", "Run-WindowsBackend.ps1",
    "windows_backend_plan.py", "config.example.json",
    "ui/index.html", "THIRD_PARTY_NOTICES.md", "SBOM.spdx.json",
    "RELEASE_MANIFEST.json", "CHECKSUMS.sha256", "README-OPERATOR.md",
})
PORTABLE_RUNTIME_REQUIRED = HOST_RUNTIME_FILES | frozenset({
    "lae-engine-cpu.exe", "runtime/node.exe", "Start-LocalAssistant.ps1", "Verify-Release.ps1", "host-config.example.json",
    "THIRD_PARTY_NOTICES.md", "SBOM.spdx.json", "RELEASE_MANIFEST.json",
    "CHECKSUMS.sha256", "README-OPERATOR.md", "licenses/Node.js-LICENSE.txt",
    "licenses/llama.cpp-LICENSE.txt",
    "node-provenance.json",
})
FORBIDDEN_SUFFIXES = (".gguf", ".safetensors", ".pt", ".pth", ".onnx", ".bin", ".pem", ".key", ".pfx")
SECRET_RE = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret(?:[_-]?key)?)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{8,})")
KNOWN_DLLS = frozenset({"kernel32.dll", "user32.dll", "advapi32.dll", "bcrypt.dll", "crypt32.dll", "dbghelp.dll", "iphlpapi.dll", "ole32.dll", "powrprof.dll", "psapi.dll", "shell32.dll", "userenv.dll", "version.dll", "winmm.dll", "ws2_32.dll", "vcruntime140.dll", "ucrtbase.dll", "vulkan-1.dll", "ntdll.dll"})


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def scan_secrets(data: bytes) -> bool:
    return SECRET_RE.search(data.decode("utf-8", errors="ignore")) is not None


def scan_tree(root: str | Path, *, require_runtime: bool = False) -> dict[str, object]:
    base = Path(root).resolve()
    files = _relative_files(base) if base.is_dir() else set()
    findings: list[str] = []
    findings.extend(f"linked-file:{path.relative_to(base).as_posix()}" for path in sorted(base.rglob("*")) if path.is_symlink())
    allowed = PORTABLE_RUNTIME_REQUIRED if require_runtime else PACKAGE_ALLOWLIST
    findings.extend(f"unallowlisted:{name}" for name in sorted(files - allowed))
    findings.extend(f"forbidden-artifact:{name}" for name in sorted(files) if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES or "node_modules" in Path(name).parts)
    required = PORTABLE_RUNTIME_REQUIRED if require_runtime else REQUIRED_STATIC
    if not required.issubset(files):
        prefix = "missing-runtime" if require_runtime else "missing-required"
        findings.extend(f"{prefix}:{name}" for name in sorted(required - files))
    manifest_path = base / "RELEASE_MANIFEST.json"
    if manifest_path.is_file():
        try:
            manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
            listed = {str(name).replace("\\", "/") for name in manifest.get("files", [])}
            if listed != files:
                findings.append("manifest-file-list-mismatch")
            if manifest.get("model_included") is not False:
                findings.append("manifest-model-included")
        except (OSError, ValueError, TypeError):
            findings.append("manifest-invalid")
    secret_files: list[str] = []
    for name in sorted(files):
        path = base / name
        data = path.read_bytes()
        if scan_secrets(data):
            secret_files.append(name)
        size_limit = 256 * 1024 * 1024 if Path(name).suffix.lower() in {".exe", ".dll"} else 64 * 1024 * 1024
        if len(data) > size_limit:
            findings.append(f"oversized:{name}")
    findings.extend(f"secret-pattern:{name}" for name in secret_files)
    dependency_results = []
    for name in sorted(files):
        if Path(name).suffix.lower() in {".exe", ".dll"}:
            dependency = scan_binary_dependencies(base / name)
            dependency_results.append(dependency)
            findings.extend(f"unresolved-dll:{name}:{dll}" for dll in dependency["unresolved"])
    return {"status": "PASS" if not findings else "FAIL", "files": sorted(files), "findings": findings, "dependencies": dependency_results or "SKIP-no-binaries", "native_windows_launch": "SKIP"}


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
