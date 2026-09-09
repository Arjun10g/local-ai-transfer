"""Fail-closed isolation for mocked lifecycle execute tests.

This module is test infrastructure only.  It redirects every mutable lifecycle
path to one temporary namespace and makes accidental checkout evidence,
provider, network, or process access a test failure.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import functools
import hashlib
import os
import socket
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no dir_fd os.open support.
    fcntl = None


PATH_METHODS = (
    "open",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "lstat",
    "stat",
    "exists",
    "is_file",
    "mkdir",
    "replace",
    "rename",
    "unlink",
    "rmdir",
)
MAX_TEST_SOURCES = 64
MAX_TEST_SOURCE_BYTES = 1_048_576


@dataclass(frozen=True)
class IsolatedLifecyclePaths:
    runtime_root: Path
    markdown_ledger: Path
    cost_ledger: Path
    incidents: Path


class CheckoutPathGuard:
    """Reject any access to exact protected paths or protected subtrees."""

    def __init__(self, *, roots=(), paths=()):
        self.roots = tuple(self._absolute(path) for path in roots)
        self.paths = frozenset(self._absolute(path) for path in paths)
        self.attempts = []

    @staticmethod
    def _absolute(value):
        raw = os.fsdecode(os.fspath(value))
        return Path(os.path.abspath(raw))

    def reject(self, value, operation):
        if isinstance(value, int):
            return
        try:
            candidate = self._absolute(value)
        except (TypeError, ValueError):
            return
        if candidate in self.paths or any(
            candidate == root or root in candidate.parents for root in self.roots
        ):
            self.attempts.append((operation, candidate))
            raise AssertionError(
                f"mocked execute test attempted {operation} on checkout operator evidence"
            )

    def reject_at(self, value, dir_fd, operation):
        if dir_fd is None or os.path.isabs(os.fsdecode(os.fspath(value))):
            self.reject(value, operation)
            return
        directory = None
        for link in (f"/dev/fd/{dir_fd}", f"/proc/self/fd/{dir_fd}"):
            try:
                directory = os.readlink(link)
                break
            except OSError:
                continue
        if directory is None and fcntl is not None and hasattr(fcntl, "F_GETPATH"):
            try:
                raw = fcntl.fcntl(dir_fd, fcntl.F_GETPATH, b"\0" * 1024)
                directory = os.fsdecode(raw.split(b"\0", 1)[0])
            except OSError:
                pass
        if directory is None:
            self.attempts.append((f"{operation}:unresolved_dir_fd", Path("<dir-fd>")))
            raise AssertionError(
                f"mocked execute test attempted {operation} with unverified dir_fd"
            )
        self.reject(Path(directory) / os.fsdecode(os.fspath(value)), operation)

    @contextlib.contextmanager
    def patches(self):
        with contextlib.ExitStack() as stack:
            for method in PATH_METHODS:
                original = getattr(Path, method)

                def guarded(path, *args, _method=method, _original=original, **kwargs):
                    self.reject(path, f"Path.{_method}")
                    if _method in {"replace", "rename"} and args:
                        self.reject(args[0], f"Path.{_method}:target")
                    return _original(path, *args, **kwargs)

                stack.enter_context(mock.patch.object(Path, method, new=guarded))

            original_open = os.open

            def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
                self.reject_at(path, dir_fd, "os.open")
                return original_open(path, flags, mode, dir_fd=dir_fd)

            original_replace = os.replace

            def guarded_os_replace(source, destination, *args, **kwargs):
                self.reject_at(source, kwargs.get("src_dir_fd"), "os.replace:source")
                self.reject_at(
                    destination,
                    kwargs.get("dst_dir_fd"),
                    "os.replace:destination",
                )
                return original_replace(source, destination, *args, **kwargs)

            original_builtin_open = builtins.open

            def guarded_builtin_open(file, *args, **kwargs):
                self.reject(file, "open")
                return original_builtin_open(file, *args, **kwargs)

            stack.enter_context(mock.patch("os.open", new=guarded_os_open))
            # Keep the descriptor capability probe truthful after wrapping
            # os.open for checkout-path isolation.
            supported_dir_fd = set(os.supports_dir_fd)
            supported_dir_fd.add(guarded_os_open)
            stack.enter_context(mock.patch.object(os, "supports_dir_fd", supported_dir_fd))
            stack.enter_context(mock.patch("os.replace", new=guarded_os_replace))
            stack.enter_context(mock.patch("builtins.open", new=guarded_builtin_open))
            yield self


@contextlib.contextmanager
def lifecycle_execute_isolation(lifecycle_module, *, prefix="lifecycle-execute-"):
    """Bind lifecycle output to temp paths and deny every external boundary."""

    original_runtime = Path(lifecycle_module.RUNTIME_ROOT)
    original_markdown = Path(lifecycle_module.MARKDOWN_LEDGER)
    original_cost = Path(lifecycle_module.COST_LEDGER)
    original_incidents = Path(lifecycle_module.INCIDENTS)
    guard = CheckoutPathGuard(
        roots=(original_runtime,),
        paths=(original_markdown, original_cost, original_incidents),
    )

    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        base = Path(directory).resolve()
        paths = IsolatedLifecyclePaths(
            runtime_root=base / "runtime",
            markdown_ledger=base / "LEDGER.md",
            cost_ledger=base / "runtime" / "cost-ledger.jsonl",
            incidents=base / "runtime" / "incidents.jsonl",
        )
        paths.runtime_root.mkdir(mode=0o700)
        paths.markdown_ledger.write_text(
            lifecycle_module.LEDGER_HEADER + "\n", encoding="utf-8"
        )
        paths.incidents.write_text(
            '{"incident":"isolated-execute-fixture"}\n', encoding="utf-8"
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(lifecycle_module, "RUNTIME_ROOT", paths.runtime_root))
            stack.enter_context(mock.patch.object(lifecycle_module, "MARKDOWN_LEDGER", paths.markdown_ledger))
            stack.enter_context(mock.patch.object(lifecycle_module, "COST_LEDGER", paths.cost_ledger))
            stack.enter_context(mock.patch.object(lifecycle_module, "INCIDENTS", paths.incidents))
            stack.enter_context(guard.patches())
            lifecycle_module.initialize_cost_ledger_genesis(
                program=lifecycle_module.COST_LEDGER_PROGRAM,
                currency=lifecycle_module.COST_LEDGER_CURRENCY,
                budget_cap_usd=50.0,
                prior_settled_spend_usd=0.0,
                current_pending_owner_count=0,
                expected_display_ledger_sha256=hashlib.sha256(
                    paths.markdown_ledger.read_bytes()
                ).hexdigest(),
                expected_incidents_sha256=hashlib.sha256(
                    paths.incidents.read_bytes()
                ).hexdigest(),
                confirmation=lifecycle_module.COST_LEDGER_GENESIS_CONFIRMATION,
            )
            stack.enter_context(
                mock.patch.object(
                    lifecycle_module,
                    "request",
                    side_effect=AssertionError("execute test attempted provider access"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    subprocess,
                    "Popen",
                    side_effect=AssertionError("execute test attempted process launch"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=AssertionError("execute test attempted network access"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("execute test attempted network access"),
                )
            )
            yield paths
        if guard.attempts:
            details = ", ".join(f"{operation}:{path}" for operation, path in guard.attempts)
            raise AssertionError(f"execute test touched checkout operator evidence: {details}")


def install_lifecycle_execute_isolation(test_case, lifecycle_module, *, prefix):
    """Install isolation for one unittest case and return its temporary paths."""

    manager = lifecycle_execute_isolation(lifecycle_module, prefix=prefix)
    paths = manager.__enter__()
    test_case.addCleanup(manager.__exit__, None, None, None)
    return paths


def isolated_lifecycle_execute(test):
    """Decorator used by direct J1M execute regressions."""

    @functools.wraps(test)
    def wrapped(self, *args, **kwargs):
        from scripts import shadeform_lifecycle

        with lifecycle_execute_isolation(
            shadeform_lifecycle, prefix="j1m-execute-runtime-"
        ):
            return test(self, *args, **kwargs)

    return wrapped


def direct_execute_methods(performance_tests_root):
    """Return every direct ``.execute()`` test helper under a bounded scan."""

    root = Path(performance_tests_root)
    sources = sorted(root.glob("test_*.py"))
    if not sources or len(sources) > MAX_TEST_SOURCES:
        raise AssertionError("unexpected performance test source count")
    found = []
    for source in sources:
        size = source.stat().st_size
        if size < 1 or size > MAX_TEST_SOURCE_BYTES:
            raise AssertionError(f"performance test source size is unsafe: {source.name}")
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            setup = next(
                (
                    node
                    for node in class_node.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "setUp"
                ),
                None,
            )
            class_isolated = setup is not None and any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "install_lifecycle_execute_isolation"
                for node in ast.walk(setup)
            )
            for method in (
                node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ):
                calls_execute = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                    for node in ast.walk(method)
                )
                if not calls_execute:
                    continue
                decorators = {
                    node.id for node in method.decorator_list if isinstance(node, ast.Name)
                }
                method_isolated = "isolated_lifecycle_execute" in decorators
                found.append(
                    {
                        "source": source.name,
                        "class": class_node.name,
                        "method": method.name,
                        "isolated": class_isolated or method_isolated,
                    }
                )
    return tuple(found)
