"""Importable advisory Windows-source scanner facade."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_source = Path(__file__).parents[1] / "clean-machine" / "package.py"
_spec = importlib.util.spec_from_file_location("qa_clean_machine_package", _source)
if _spec is None or _spec.loader is None:  # pragma: no cover - repository invariant
    raise ImportError(f"cannot load {_source}")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

PACKAGE_ALLOWLIST = _module.PACKAGE_ALLOWLIST
HOST_RUNTIME_FILES = _module.HOST_RUNTIME_FILES
PORTABLE_RUNTIME_REQUIRED = _module.PORTABLE_RUNTIME_REQUIRED
REQUIRED_STATIC = _module.REQUIRED_STATIC
scan_secrets = _module.scan_secrets
scan_tree = _module.scan_tree
scan_binary_dependencies = _module.scan_binary_dependencies
checksums = _module.checksums
BoundedFileError = _module.BoundedFileError
FORBIDDEN_PACKAGE_PATHS = _module.FORBIDDEN_PACKAGE_PATHS
MAX_BINARY_BYTES = _module.MAX_BINARY_BYTES
MAX_MANIFEST_BYTES = _module.MAX_MANIFEST_BYTES
MAX_TREE_ENTRIES = _module.MAX_TREE_ENTRIES
is_reparse = _module.is_reparse
path_chain_has_reparse = _module.path_chain_has_reparse
read_bounded_file = _module.read_bounded_file
