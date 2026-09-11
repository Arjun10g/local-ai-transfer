#!/usr/bin/env python3
"""Plan and run the controlled Qwen3.5-9B GGUF conversion job (J1M).

The default action is a no-spend plan.  Execution is deliberately limited to
an already-authorised host; this module never creates a Shadeform resource.
Commands are recorded as argv arrays, and the HF token is materialised only in
a private temporary file for the duration of a build.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import shutil
import tempfile
import unicodedata
from urllib.parse import unquote_to_bytes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_OUTPUT_ROOT = ROOT
DEFAULT_CONFIG = ROOT / "model" / "conversion" / "j1m-config.json"
SOURCE_LOCK = ROOT / "model" / "source-lock" / "qwen35-9b.source-lock.json"
TOKEN_ENV = "HF_TOKEN"
CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONUNBUFFERED",
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
})
_COMMAND_LOG_TAIL_LIMIT = 1200
_RECEIPT_MAX_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SECURITY_TEXT_MAX_CHARS = 64 * 1024
_SECURITY_PERCENT_MAX_PASSES = 8
_SECURITY_PERCENT_MAX_EXPANSION = 4
_TOKEN_MAX_BYTES = 16 * 1024
_TRUSTED_SHARED_HANDLE_PARENTS = frozenset({Path("/tmp"), Path("/var/tmp")})
_SECURITY_ASSIGNMENT_MAX_DEPTH = 16
_RECEIPT_MAX_DEPTH = 32
_RECEIPT_MAX_NODES = 4096
_RECEIPT_MAX_STRING_CHARS = 64 * 1024
_RECEIPT_MAX_NUMBER_DIGITS = 4096
_RECEIPT_MAX_INTEGER = 2**63 - 1
_RECEIPT_MAX_FLOAT = 1e308
_SECURITY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")

# Receipt values are an audit boundary, not a masking boundary.  A command
# may still receive a credential through its private file/header plumbing, but
# that command/output is not eligible for persisted evidence when the value is
# present in the value being recorded.  Path-bearing options are intentionally
# allowed: the path identifies a private handle and does not reveal key
# material.
_CREDENTIAL_NAME = re.compile(
    r"(?i)(?:^|[_-])(access[_-]?token|api[_-]?key|auth(?:orization)?|bearer|client[_-]?secret|credential|cookie|hf[_-]?token|password|passwd|secret|token|x[_-]api[_-]?key)(?:$|[_-])"
)
_CREDENTIAL_HEADER = re.compile(
    r"(?i)^(?:(?:-+h)\s*)?(?:authorization|proxy-authorization|x[-_]api[-_]key|api[-_]key|cookie)\s*:"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL_CREDENTIAL_QUERY = re.compile(
    r"(?i)[?&](?:access[_-]?token|api[_-]?key|auth(?:orization)?|credential|password|passwd|secret|signature|token|x[-_]api[-_]key|x[-_]amz[-_]credential)=[^&#\s]*"
)
_URL_USERINFO = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://[^/@\s]+@")
_ASSIGNMENT = re.compile(r"(?i)^([a-z_][a-z0-9_-]{1,80})\s*[:=]\s*(.*)$")
_ASSIGNMENT_IN_TEXT = re.compile(r"(?i)(?:^|[\s,;=])([a-z_][a-z0-9_-]{1,80})\s*[:=]\s*([^\s,;]+)")
_ASSIGNMENT_NAME_CANDIDATE = re.compile(r"(?:^|[\s,;=])([^\s,;:=]{1,128})\s*[:=]")
_URL_PARAMETER_NAME = re.compile(r"[?&]([^?&#=\s]{1,128})=")
_PATH_OPTION = re.compile(r"(?i)(?:^|[-_])(file|path|socket|identity)(?:$|[-_])")
_SPLIT_PRIVATE_HANDLE_OPTIONS = frozenset({"i", "token-file"})
_PATH_VALUE = re.compile(r"^(?:[a-z]:[\\/]|[/\\]|\.{1,2}[\\/])")
_HANDLE_BASENAME = re.compile(
    r"(?i)(?:access[_-]?token|api[_-]?key|auth(?:orization)?|bearer|client[_-]?secret|credential|cookie|hf[_-]?token|password|passwd|secret|token|x[_-]?api[_-]?key|ssh[_-]?key|identity)"
)
_HANDLE_OPTION = re.compile(r"(?i)(?:identity|ssh[-_]?key)")
def _reject_unsafe_unicode(value: str, *, allow_line_breaks: bool) -> None:
    """Reject controls/formatting that can hide a credential name."""

    for character in value:
        category = unicodedata.category(character)
        if category in {"Cf", "Mn", "Mc", "Cc"}:
            if allow_line_breaks and character in "\r\n\t":
                continue
            raise ValueError("unsafe Unicode in persisted security boundary")


def _decode_percent_once(candidate: str) -> str:
    """Decode one strict UTF-8 percent layer, rejecting malformed escapes."""

    if "%" not in candidate:
        return candidate
    for index, character in enumerate(candidate):
        if character == "%" and (
            index + 2 >= len(candidate)
            or candidate[index + 1] not in "0123456789abcdefABCDEF"
            or candidate[index + 2] not in "0123456789abcdefABCDEF"
        ):
            raise ValueError("invalid percent encoding in persisted security boundary")
    try:
        return unquote_to_bytes(candidate).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid percent encoding in persisted security boundary") from exc


def _security_variants(value: str, *, allow_line_breaks: bool = False) -> Iterator[str]:
    """Yield bounded security views until percent decoding reaches stability."""

    if len(value) > _SECURITY_TEXT_MAX_CHARS:
        raise ValueError("persisted security boundary exceeds its bound")
    _reject_unsafe_unicode(value, allow_line_breaks=allow_line_breaks)

    seen: set[str] = set()
    candidate = value
    for pass_index in range(_SECURITY_PERCENT_MAX_PASSES + 1):
        normalized = unicodedata.normalize("NFKC", candidate)
        _reject_unsafe_unicode(normalized, allow_line_breaks=allow_line_breaks)
        if normalized not in seen:
            seen.add(normalized)
            yield normalized
        decoded = _decode_percent_once(candidate)
        if decoded == candidate:
            return
        if (
            len(decoded) > _SECURITY_TEXT_MAX_CHARS
            or len(decoded) > max(len(value), 1) * _SECURITY_PERCENT_MAX_EXPANSION
        ):
            raise ValueError("percent decoding exceeds its bounded security limit")
        _reject_unsafe_unicode(decoded, allow_line_breaks=allow_line_breaks)
        if pass_index == _SECURITY_PERCENT_MAX_PASSES:
            raise ValueError("percent decoding did not reach stability")
        candidate = decoded


def _private_handle_snapshot(value: str) -> tuple[os.stat_result, tuple[tuple[Path, os.stat_result], ...]] | None:
    """Return a filesystem identity snapshot for one private handle path."""

    try:
        _reject_unsafe_unicode(value, allow_line_breaks=False)
    except ValueError:
        return None
    if (
        not _PATH_VALUE.match(value)
        or not value.startswith("/")
        or "%" in value
        or "\\" in value
        or value != os.path.normpath(value)
    ):
        return None
    path = Path(value)
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:-1]) or not parts[-1] or not _HANDLE_BASENAME.search(parts[-1]):
        return None
    try:
        current_uid = os.getuid()
        target = os.lstat(path)
        if (
            stat.S_ISLNK(target.st_mode)
            or not stat.S_ISREG(target.st_mode)
            or target.st_uid != current_uid
            or target.st_nlink != 1
            or stat.S_IMODE(target.st_mode) != 0o600
        ):
            return None
        parents: list[tuple[Path, os.stat_result]] = []
        parent = path.parent
        first_parent = True
        while True:
            parent_stat = os.lstat(parent)
            if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                return None
            parent_mode = stat.S_IMODE(parent_stat.st_mode)
            private_parent = parent_stat.st_uid == current_uid and not (parent_mode & 0o077)
            trusted_shared_parent = parent in _TRUSTED_SHARED_HANDLE_PARENTS and parent_stat.st_uid == 0 and parent_mode == 0o1777
            if parent != Path("/"):
                if first_parent and not (private_parent or trusted_shared_parent):
                    return None
                if not first_parent and (
                    parent_stat.st_uid not in {0, current_uid} or parent_mode & 0o022
                ):
                    return None
            parents.append((parent, parent_stat))
            if parent == Path("/"):
                break
            parent = parent.parent
            first_parent = False
    except (AttributeError, OSError):
        return None
    return target, tuple(parents)


def _canonical_private_handle_path(value: str) -> bool:
    """Accept only an existing canonical private credential/SSH handle."""

    return _private_handle_snapshot(value) is not None


def _open_private_handle(value: str) -> tuple[int, os.stat_result, tuple[tuple[Path, os.stat_result], ...]]:
    """Open a private handle without following links and recheck identity."""

    snapshot = _private_handle_snapshot(value)
    if snapshot is None or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("private credential handle is not a canonical private file")
    target, parents = snapshot
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    try:
        fd = os.open(value, flags)
    except OSError as exc:
        raise ValueError("private credential handle is not a canonical private file") from exc
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_nlink, stat.S_IMODE(opened.st_mode))
            != (target.st_dev, target.st_ino, target.st_uid, target.st_nlink, stat.S_IMODE(target.st_mode))
        ):
            raise ValueError("private credential handle changed during validation")
        for parent, expected in parents:
            current = os.lstat(parent)
            if (current.st_dev, current.st_ino, current.st_uid, stat.S_IMODE(current.st_mode)) != (
                expected.st_dev, expected.st_ino, expected.st_uid, stat.S_IMODE(expected.st_mode)
            ):
                raise ValueError("private credential handle changed during validation")
        return fd, opened, parents
    except Exception:
        os.close(fd)
        raise


def _is_private_handle_option(option: str) -> bool:
    normalized = option.lstrip("-").casefold()
    return normalized in _SPLIT_PRIVATE_HANDLE_OPTIONS or bool(_CREDENTIAL_NAME.search(option) or _HANDLE_OPTION.search(option))


def _is_private_handle_path_option(option: str) -> bool:
    normalized = option.lstrip("-").casefold()
    return normalized in _SPLIT_PRIVATE_HANDLE_OPTIONS or bool(_PATH_OPTION.search(option))


def _credential_like_text(value: str, *, allow_private_path: bool = False, _depth: int = 0) -> str | None:
    """Return a stable rejection reason for one persisted string, if unsafe."""

    if "\x00" in value:
        return "nul"
    if _depth > _SECURITY_ASSIGNMENT_MAX_DEPTH:
        raise ValueError("credential assignment nesting exceeds its bound")
    try:
        for variant in _security_variants(value):
            for match in _ASSIGNMENT_NAME_CANDIDATE.finditer(variant):
                if any(ord(character) > 127 for character in match.group(1)):
                    return "unsafe_security_name"
            # Quoting and shell escaping commonly surrounds object keys and
            # assignment names. Scan the decoded security view without
            # requiring whitespace before the name, then apply the same
            # credential-name policy to the candidate.
            assignment_view = re.sub(r"\\([\\\"'=:])", r"\1", variant)
            for match in re.finditer(
                    r"(?<![A-Za-z0-9])['\"\\s]*([A-Za-z][A-Za-z0-9_.-]{0,127})['\"\\s]*(?:=|:)",
                    assignment_view):
                if _CREDENTIAL_NAME.search(match.group(1)):
                    return "credential_assignment"
            for match in _URL_PARAMETER_NAME.finditer(variant):
                if any(ord(character) > 127 for character in match.group(1)):
                    return "unsafe_security_name"
            if _BEARER_VALUE.search(variant) or _URL_CREDENTIAL_QUERY.search(variant) or _URL_USERINFO.search(variant):
                return "credential_transport"
            if _CREDENTIAL_NAME.fullmatch(variant.strip()):
                return "credential_name"
            assignment = _ASSIGNMENT.fullmatch(variant.strip())
            assignments = [assignment] if assignment is not None else list(_ASSIGNMENT_IN_TEXT.finditer(variant))
            for assignment in assignments:
                name, assigned = assignment.groups()
                if _CREDENTIAL_NAME.search(name):
                    if allow_private_path and _is_private_handle_option(name) and _canonical_private_handle_path(assigned.strip()):
                        continue
                    return "credential_assignment"
                # Do not let a benign outer assignment hide a credential
                # assignment after its first equals sign (for example an
                # environment wrapper around a token assignment).
                if ("=" in assigned or ":" in assigned) and _credential_like_text(
                    assigned, allow_private_path=allow_private_path, _depth=_depth + 1
                ) is not None:
                    return "credential_assignment"
            # Header values are frequently represented as `Authorization: ...` or
            # `-H Authorization: ...`; inspect the suffix but do not reject ordinary
            # path arguments such as `--ssh-key-path=/private/key`.
            if _CREDENTIAL_HEADER.match(variant.strip()):
                return "credential_header"
    except ValueError:
        return "unsafe_security_text"
    return None


def validate_persisted_argv(argv: object) -> list[str]:
    """Reject credential-bearing argv before it can reach any receipt."""

    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        raise ValueError("persisted argv is invalid")
    previous_option = ""
    expecting_handle = False
    for item in argv:
        try:
            _reject_unsafe_unicode(item, allow_line_breaks=False)
        except ValueError as exc:
            raise ValueError("credential-like command argument rejected before persistence") from exc
        was_expecting_handle = expecting_handle
        if was_expecting_handle and item.startswith("-"):
            raise ValueError("credential-like command argument rejected before persistence")
        option = item.split("=", 1)[0]
        if item.startswith("-"):
            option_name, equals, option_value = item.lstrip("-").partition("=")
            if any(ord(character) > 127 for character in option_name):
                raise ValueError("credential-like command argument rejected before persistence")
            if _is_private_handle_option(option_name):
                if _is_private_handle_path_option(option_name):
                    # A split path option is checked against its following
                    # argument below; inline values must already be proven
                    # canonical private handles.
                    if equals and not _canonical_private_handle_path(option_value.strip()):
                        raise ValueError("credential-like command argument rejected before persistence")
                    expecting_handle = not equals
                else:
                    raise ValueError("credential-like command argument rejected before persistence")
            if "=" in item:
                # Catch env/define wrappers such as `--env=HF_TOKEN=value`,
                # where the credential assignment is nested after a benign
                # option name.
                nested = item.split("=", 1)[1]
                if _credential_like_text(nested, allow_private_path=True) is not None:
                    raise ValueError("credential-like command argument rejected before persistence")
        if was_expecting_handle:
            if not _canonical_private_handle_path(item):
                raise ValueError("credential-like command argument rejected before persistence")
            reason = None
            expecting_handle = False
        elif previous_option and _is_private_handle_path_option(previous_option):
            if _is_private_handle_option(previous_option):
                if not _canonical_private_handle_path(item):
                    raise ValueError("credential-like command argument rejected before persistence")
                reason = None
            elif _PATH_VALUE.match(item):
                reason = None
            else:
                reason = _credential_like_text(item, allow_private_path=True)
        else:
            reason = _credential_like_text(item, allow_private_path=True)
        if reason is not None:
            raise ValueError("credential-like command argument rejected before persistence")
        # An argv token may itself be a serialized JSON envelope. Inspect that
        # envelope too so escaping cannot hide a credential assignment.
        stripped_item = item.strip()
        if stripped_item and stripped_item[0] in "[{\"":
            try:
                validate_persisted_output(item)
            except ValueError as exc:
                raise ValueError("credential-like command argument rejected before persistence") from exc
        previous_option = option if item.startswith("-") else ""
    if expecting_handle:
        raise ValueError("credential-like command argument rejected before persistence")
    return list(argv)


def _precheck_json_bytes(raw: bytes) -> None:
    """Reject oversized JSON structure before the decoder allocates it."""

    depth = nodes = digits = string_chars = 0
    stack: list[int] = []
    in_string = escaped = False
    for character in raw:
        if in_string:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                in_string = False
            else:
                string_chars += 1
                if string_chars > _RECEIPT_MAX_STRING_CHARS:
                    raise ValueError("receipt string exceeds its bound")
            continue
        if character == 34:
            in_string = True
            string_chars = 0
        elif character in (123, 91):
            depth += 1
            nodes += 1
            if depth > _RECEIPT_MAX_DEPTH or nodes > _RECEIPT_MAX_NODES:
                raise ValueError("receipt structure exceeds its bound")
            stack.append(character)
        elif character in (125, 93):
            depth -= 1
            if depth < 0 or not stack:
                raise ValueError("persisted JSON is invalid")
            opener = stack.pop()
            if (opener == 123 and character != 125) or (opener == 91 and character != 93):
                raise ValueError("persisted JSON is invalid")
        elif character == 44:
            nodes += 1
            if nodes > _RECEIPT_MAX_NODES:
                raise ValueError("receipt node count exceeds its bound")
        elif 48 <= character <= 57:
            digits += 1
            if digits > _RECEIPT_MAX_NUMBER_DIGITS:
                raise ValueError("receipt number exceeds its bound")
    if in_string or depth != 0 or stack:
        raise ValueError("persisted JSON is invalid")


def _bounded_int(value: str) -> int:
    if len(value.lstrip("+-")) > 64:
        raise ValueError("receipt integer exceeds its bound")
    parsed = int(value)
    if abs(parsed) > _RECEIPT_MAX_INTEGER:
        raise ValueError("receipt integer exceeds its bound")
    return parsed


def _bounded_float(value: str) -> float:
    if len(value) > 128:
        raise ValueError("receipt number exceeds its bound")
    parsed = float(value)
    if not math.isfinite(parsed) or abs(parsed) > _RECEIPT_MAX_FLOAT:
        raise ValueError("receipt number is not finite")
    return parsed


def _reject_constant(_value: str) -> Any:
    raise ValueError("receipt number is not finite")


def _bounded_json_loads(raw: bytes | str) -> Any:
    """Decode bounded strict JSON with duplicate and numeric rejection."""

    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or len(encoded) > _RECEIPT_MAX_BYTES:
        raise ValueError("receipt exceeds its bounded size")
    _precheck_json_bytes(encoded)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate receipt key")
            result[key] = item
        return result

    try:
        return json.loads(
            encoded.decode("utf-8"), object_pairs_hook=reject_duplicates,
            parse_int=_bounded_int, parse_float=_bounded_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("invalid or unbounded receipt JSON") from exc


def _private_ancestor_snapshot(path: Path, trusted_root: Path | None = None,
                               *, strict_permissions: bool = True) -> tuple[tuple[Path, os.stat_result], ...]:
    """Validate a path beneath an explicitly scoped, link-free root.

    The root is intentionally scoped by the caller; ancestors above it are
    outside this policy. Every component from that root through the target
    parent is checked with lstat, and the snapshot is rechecked before and
    after operations that publish or consume a receipt.
    """

    if os.name != "posix" or not hasattr(os, "getuid"):
        raise ValueError("private ancestor policy is unavailable")
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise ValueError("private path must be absolute and normalized")
    root = trusted_root if trusted_root is not None else path.parent
    if not root.is_absolute() or root != Path(os.path.abspath(root)):
        raise ValueError("private trusted root must be absolute and normalized")
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError as exc:
        raise ValueError("private path is outside its trusted root") from exc
    components = [root]
    ancestor = root
    for part in relative_parent.parts:
        ancestor = ancestor / part
        components.append(ancestor)
    snapshot: list[tuple[Path, os.stat_result]] = []
    current_uid = os.getuid()
    for index, component in enumerate(components):
        try:
            info = os.lstat(component)
        except OSError as exc:
            raise ValueError("private ancestor is unavailable") from exc
        permissions = stat.S_IMODE(info.st_mode)
        # The explicitly configured root is trusted as the policy boundary;
        # it must not be writable by others, while every descendant in the
        # private path must be fully owner-private for writes.
        unsafe_permissions = (permissions & 0o022 if index == 0 else
                              permissions & (0o077 if strict_permissions else 0o022))
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
                info.st_uid != current_uid or info.st_nlink < 1 or
                unsafe_permissions):
            raise ValueError("private ancestor is unsafe")
        snapshot.append((component, info))
    return tuple(snapshot)


def _private_ancestors_stable(snapshot: tuple[tuple[Path, os.stat_result], ...]) -> bool:
    try:
        for component, expected in snapshot:
            current = os.lstat(component)
            if (not stat.S_ISDIR(current.st_mode) or
                    (current.st_dev, current.st_ino, current.st_uid,
                     stat.S_IMODE(current.st_mode)) != (
                    expected.st_dev, expected.st_ino, expected.st_uid,
                    stat.S_IMODE(expected.st_mode))):
                return False
        return True
    except OSError:
        return False


def _open_private_parent_descriptor(
        path: Path, trusted_root: Path, *, ancestors: tuple[tuple[Path, os.stat_result], ...]
) -> int:
    """Open each private ancestor from the trusted root without path traversal."""

    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or
            not hasattr(os, "supports_dir_fd") or os.open not in os.supports_dir_fd):
        raise ValueError("private descriptor-safe path is unavailable")
    try:
        relative_parent = path.parent.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("private path is outside its trusted root") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(trusted_root, flags)
        for part in relative_parent.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not _private_ancestors_stable(ancestors):
            raise ValueError("private ancestor changed")
        return descriptor
    except ValueError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError):
        if descriptor >= 0:
            os.close(descriptor)
        raise ValueError("private descriptor-safe path is unavailable") from None


def validate_persisted_output(value: object) -> str:
    """Reject credential-bearing output before it can reach a receipt."""

    text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else str(value or "")
    state = {"nodes": 0}
    if len(text) > _RECEIPT_MAX_STRING_CHARS:
        raise ValueError("persisted output exceeds its bounded size")
    # Unlike argv, output has no trusted option context: even a path that
    # happens to contain `token` is rejected only when it has credential
    # syntax.  This keeps diagnostics useful without persisting key material.
    for line in text.splitlines() or [text]:
        if _credential_like_text(line.strip()) is not None:
            raise ValueError("credential-like command output rejected before persistence")
    stripped = text.strip()
    if stripped and stripped[0] in "[{\"":
        try:
            decoded = _bounded_json_loads(stripped)
        except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
            raise ValueError("persisted JSON is invalid or unbounded") from exc
        _validate_persisted_value(decoded, depth=1, state=state, parse_json_strings=True)
    return text


def _validate_persisted_value(value: object, *, depth: int, state: dict[str, int], parse_json_strings: bool) -> None:
    if depth > _RECEIPT_MAX_DEPTH:
        raise ValueError("receipt nesting exceeds its bound")
    state["nodes"] += 1
    if state["nodes"] > _RECEIPT_MAX_NODES:
        raise ValueError("receipt node count exceeds its bound")
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > _RECEIPT_MAX_INTEGER:
            raise ValueError("receipt integer exceeds its bound")
        return
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _RECEIPT_MAX_FLOAT:
            raise ValueError("receipt number is not finite")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or any(ord(character) > 127 for character in key):
                raise ValueError("credential-like receipt key rejected before persistence")
            if _credential_like_text(key) is not None:
                raise ValueError("credential-like receipt key rejected before persistence")
            _validate_persisted_value(item, depth=depth + 1, state=state, parse_json_strings=parse_json_strings)
    elif isinstance(value, (list, tuple)):
        if len(value) > _RECEIPT_MAX_NODES:
            raise ValueError("receipt container exceeds its bound")
        # An argv list gets option-aware path handling; ordinary string lists
        # are treated as output and cannot carry credential syntax.
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            validate_persisted_argv(value)
            for item in value:
                _validate_persisted_value(item, depth=depth + 1, state=state, parse_json_strings=parse_json_strings)
            return
        for item in value:
            _validate_persisted_value(item, depth=depth + 1, state=state, parse_json_strings=parse_json_strings)
    elif isinstance(value, (str, bytes)):
        text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else value
        if len(text) > _RECEIPT_MAX_STRING_CHARS:
            raise ValueError("receipt string exceeds its bound")
        for line in text.splitlines() or [text]:
            if _credential_like_text(line.strip()) is not None:
                raise ValueError("credential-like command output rejected before persistence")
        if parse_json_strings:
            stripped = text.strip()
            if stripped and stripped[0] in "[{\"":
                try:
                    decoded = _bounded_json_loads(stripped)
                except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
                    raise ValueError("persisted JSON is invalid or unbounded") from exc
                _validate_persisted_value(decoded, depth=depth + 1, state=state, parse_json_strings=True)
    else:
        raise ValueError("receipt value type unsupported")


def validate_persisted_receipt(value: object) -> object:
    """Recursively enforce the no-credential invariant on receipt values."""

    try:
        _validate_persisted_value(value, depth=0, state={"nodes": 0}, parse_json_strings=True)
    except RecursionError as exc:
        raise ValueError("receipt nesting exceeds its bound") from exc
    return value


def _write_validated_json(path: Path, payload: object, *, trusted_root: Path | None = None) -> None:
    """Validate a producer payload completely before creating its receipt."""

    validate_persisted_receipt(payload)
    try:
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("receipt serialization refused") from exc
    validate_persisted_output(serialized)
    _private_atomic_write(
        path, serialized.encode("utf-8"),
        trusted_root=trusted_root or PRIVATE_OUTPUT_ROOT,
    )


def _private_atomic_write(path: Path, payload: bytes, *, trusted_root: Path | None = None) -> None:
    """Publish bytes only through a pinned private parent descriptor.

    This deliberately refuses missing parents and platforms without the
    required descriptor-relative/no-follow primitives. Callers must validate
    payloads before entering this helper, so a rejected receipt cannot create
    an output directory, temporary file, or placeholder.
    """

    if not isinstance(payload, bytes) or len(payload) > _RECEIPT_MAX_BYTES:
        raise ValueError("private output payload exceeds its bound")
    # Keep the helper itself an invariant boundary: no caller can accidentally
    # create a parent/temp file before content validation is complete.
    validate_persisted_output(payload)
    if not path.is_absolute() or path != Path(os.path.abspath(path)) or not path.name:
        raise ValueError("private output path must be absolute and normalized")
    if os.name != "posix" or not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "supports_dir_fd")):
        raise ValueError("private descriptor-safe output is unavailable")
    if not all(function in os.supports_dir_fd for function in (os.open, os.stat, os.unlink)):
        raise ValueError("private descriptor-relative output is unavailable")
    ancestors = _private_ancestor_snapshot(
        path, trusted_root or PRIVATE_OUTPUT_ROOT,
    )
    parent = path.parent
    try:
        parent_fd = _open_private_parent_descriptor(
            path, trusted_root or PRIVATE_OUTPUT_ROOT, ancestors=ancestors,
        )
    except ValueError as exc:
        raise ValueError("private output parent is unavailable") from exc
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    published = False
    try:
        parent_stat = os.fstat(parent_fd)
        current_parent = os.stat(parent, follow_symlinks=False)
        if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid() or
                stat.S_IMODE(parent_stat.st_mode) & 0o077 or
                (parent_stat.st_dev, parent_stat.st_ino) !=
                (current_parent.st_dev, current_parent.st_ino)):
            raise ValueError("private output parent is unsafe")
        if not _private_ancestors_stable(ancestors):
            raise ValueError("private output ancestor changed")
        try:
            existing = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW |
                getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        except FileNotFoundError:
            existing = -1
        except OSError as exc:
            raise ValueError("private output target is unsafe") from exc
        if existing >= 0:
            try:
                info = os.fstat(existing)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError("private output target is unsafe")
            finally:
                os.close(existing)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
            getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("private output made no progress")
            written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or
                info.st_size != len(payload)):
            raise ValueError("private temporary output is unsafe")
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        published = True
        os.fsync(parent_fd)
        if not _private_ancestors_stable(ancestors):
            raise ValueError("private output ancestor changed")
        verify_fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW |
            getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        try:
            verified = os.fstat(verify_fd)
            path_stat = os.stat(path, follow_symlinks=False)
            if ((verified.st_dev, verified.st_ino) != (path_stat.st_dev, path_stat.st_ino) or
                    verified.st_uid != os.getuid() or verified.st_nlink != 1 or
                    stat.S_IMODE(verified.st_mode) != 0o600 or
                    verified.st_size != len(payload)):
                raise ValueError("private output identity changed")
            actual = bytearray()
            while len(actual) <= len(payload):
                chunk = os.read(verify_fd, len(payload) + 1 - len(actual))
                if not chunk:
                    break
                actual.extend(chunk)
            if bytes(actual) != payload:
                raise ValueError("private output verification failed")
        finally:
            os.close(verify_fd)
    except OSError:
        raise ValueError("private output operation refused") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = _bounded_json_file(path)
    if not isinstance(payload, dict) or payload.get("schema") != "local_bmo.j1m.v1":
        raise ValueError("invalid J1M config schema")
    source = payload.get("source", {})
    if not isinstance(source, dict):
        raise ValueError("invalid J1M source configuration")
    if source.get("model_id") != "Qwen/Qwen3.5-9B":
        raise ValueError("J1M source model is not the approved Qwen3.5-9B")
    if len(str(source.get("revision", ""))) != 40:
        raise ValueError("J1M source revision must be an immutable commit SHA")
    llama = payload.get("llama_cpp", {})
    if not isinstance(llama, dict):
        raise ValueError("invalid J1M converter configuration")
    if len(str(llama.get("revision", ""))) != 40:
        raise ValueError("J1M llama.cpp revision must be an immutable commit SHA")
    if payload.get("text_only") is not True:
        raise ValueError("J1M must be text-only")
    modes = payload.get("modes", {})
    if not isinstance(modes, dict):
        raise ValueError("invalid J1M mode configuration")
    eval_mode = modes.get("eval", {})
    if not isinstance(eval_mode, dict):
        raise ValueError("invalid J1M evaluation mode")
    if eval_mode.get("backend") != "cuda" or eval_mode.get("cuda_device_name") != "CUDA0" or eval_mode.get("cuda_architecture") != 80 or eval_mode.get("gpu_layers") != 99:
        raise ValueError("eval must use the explicit CUDA A100 evaluation profile")
    if eval_mode.get("cuda_compiler") != "/usr/local/cuda/bin/nvcc":
        raise ValueError("eval must bind the approved absolute CUDA compiler path")
    if eval_mode.get("build_parallelism") != 8:
        raise ValueError("eval must use the reviewed bounded CUDA build parallelism")
    canary_mode = modes.get("canary", {})
    if not isinstance(canary_mode, dict):
        raise ValueError("invalid J1M canary mode")
    if (canary_mode.get("no_model") is not True or canary_mode.get("probe_only") is not True or
            canary_mode.get("salvage_required") is not True or canary_mode.get("teardown_required") is not True):
        raise ValueError("canary must be an explicit no-model probe-only mode")
    validate_persisted_receipt(payload)
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise ValueError("artifact hash read refused") from None
    return digest.hexdigest()


def _bounded_json_file(path: Path, *, limit: int = _RECEIPT_MAX_BYTES) -> Any:
    """Read one receipt snapshot, rejecting oversize and duplicate keys."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("descriptor-safe receipt read is unavailable")
    ancestors = _private_ancestor_snapshot(
        path, PRIVATE_OUTPUT_ROOT, strict_permissions=False,
    )
    parent_descriptor = -1
    descriptor = -1
    try:
        parent_descriptor = _open_private_parent_descriptor(
            path, PRIVATE_OUTPUT_ROOT, ancestors=ancestors,
        )
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW |
            getattr(os, "O_CLOEXEC", 0), dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022 or
                before.st_size > limit):
            raise ValueError("receipt is not a bounded private file")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (len(raw) > limit or before.st_size != after.st_size or
                after.st_size != len(raw) or
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino) or
                not _private_ancestors_stable(ancestors)):
            raise ValueError("receipt changed during bounded read")
    except OSError:
        raise ValueError("receipt read refused") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    payload = _bounded_json_loads(raw)
    validate_persisted_receipt(payload)
    return payload


def _receipt_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def _validate_producer_receipts(output_dir: Path, source_lock: Path, *, llama_revision: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Validate every producer receipt before a completion manifest is emitted."""
    source_lock_payload = _bounded_json_file(source_lock)
    if not isinstance(source_lock_payload, dict) or source_lock_payload.get("schema_version") != "1.0.0":
        raise ValueError("source lock schema is invalid")
    if not isinstance(source_lock_payload.get("source_files"), list):
        raise ValueError("source lock file inventory is invalid")
    source = _bounded_json_file(output_dir / "source-model-receipt.json")
    validate_persisted_receipt(source)
    source_keys = {"schema", "status", "model_id", "revision", "checked_files", "file_hashes", "license_sha256", "tokenizer_sha256", "chat_template_sha256", "verified_at_utc"}
    if (not isinstance(source, dict) or set(source) != source_keys or source["schema"] != "local_bmo.j1m.source-model-receipt.v1" or source["status"] != "verified" or source["model_id"] != source_lock_payload.get("model_id") or source["revision"] != source_lock_payload.get("revision")):
        raise ValueError("source receipt identity is invalid")
    checked = source["checked_files"]
    hashes = source["file_hashes"]
    if (not isinstance(checked, list) or len(checked) > 10000 or len(set(checked)) != len(checked) or any(not isinstance(item, str) or not item or len(item) > 512 for item in checked) or not isinstance(hashes, dict) or set(hashes) != set(checked)):
        raise ValueError("source receipt file inventory is invalid")
    for digest in hashes.values():
        _receipt_sha(digest, "source file hash")
    lock_hashes = {
        str(item["path"]): str(item.get("sha256") or item.get("lfs_sha256"))
        for item in source_lock_payload.get("source_files", [])
        if isinstance(item, dict) and not item.get("excluded_from_text_only") and (item.get("sha256") or item.get("lfs_sha256"))
    }
    if set(hashes) != set(lock_hashes) or any(lock_hashes.get(name) != digest for name, digest in hashes.items()):
        raise ValueError("source receipt hash is not bound to the immutable source lock")
    for field in ("license_sha256", "tokenizer_sha256", "chat_template_sha256"):
        _receipt_sha(source[field], field)
    if not isinstance(source["verified_at_utc"], str) or not source["verified_at_utc"]:
        raise ValueError("source receipt timestamp is invalid")

    tensor = _bounded_json_file(output_dir / "tensor-metadata.json")
    validate_persisted_receipt(tensor)
    tensor_keys = {"schema", "status", "text_only", "tensor_count", "tensors", "gguf_metadata", "vision_projection_present", "chat_template_sha256"}
    if (not isinstance(tensor, dict) or set(tensor) != tensor_keys or tensor["schema"] != "local_bmo.j1m.tensor-metadata.v1" or tensor["status"] != "verified" or tensor["text_only"] is not True or tensor["vision_projection_present"] is not False):
        raise ValueError("tensor receipt schema or policy is invalid")
    tensors = tensor["tensors"]
    if (not isinstance(tensors, list) or len(tensors) != tensor["tensor_count"] or len(tensors) > 200000 or isinstance(tensor["tensor_count"], bool) or not isinstance(tensor["tensor_count"], int) or any(not isinstance(item, dict) or set(item) != {"name", "shape", "type"} or not isinstance(item["name"], str) or not isinstance(item["shape"], list) or len(item["shape"]) > 16 or any(isinstance(dim, bool) or not isinstance(dim, int) or dim < 0 for dim in item["shape"]) or not isinstance(item["type"], str) for item in tensors)):
        raise ValueError("tensor inventory is invalid")
    metadata = tensor["gguf_metadata"]
    if not isinstance(metadata, dict) or set(metadata) - {"general.architecture", "general.file_type", "general.version", "tokenizer.chat_template", "gguf.version"} or str(metadata.get("general.architecture", "")).lower().replace(".", "").replace("_", "") != "qwen35":
        raise ValueError("GGUF metadata identity is invalid")
    _receipt_sha(tensor["chat_template_sha256"], "tensor chat template hash")
    if tensor["chat_template_sha256"] != source["chat_template_sha256"]:
        raise ValueError("tensor chat template hash is not bound to the source receipt")

    toolchain = _bounded_json_file(output_dir / "toolchain.json")
    toolchain_keys = {"schema", "llama_cpp_head", "python", "cmake", "compiler", "os_packages", "pip_freeze", "dependency_wheelhouse_lock"}
    if not isinstance(toolchain, dict) or set(toolchain) != toolchain_keys or toolchain.get("schema") != "local_bmo.j1m.toolchain.v1" or (llama_revision is not None and toolchain.get("llama_cpp_head") != llama_revision):
        raise ValueError("toolchain receipt identity is invalid")
    if any(not isinstance(toolchain.get(field), str) or not toolchain[field] or len(toolchain[field]) > 4096 for field in ("llama_cpp_head", "python", "cmake", "compiler")) or not isinstance(toolchain.get("pip_freeze"), str) or len(toolchain["pip_freeze"]) > _RECEIPT_MAX_BYTES or not isinstance(toolchain["os_packages"], list) or len(toolchain["os_packages"]) > 64 or any(not isinstance(item, str) or len(item) > 512 for item in toolchain["os_packages"]):
        raise ValueError("toolchain receipt shape is invalid")
    validate_persisted_receipt(toolchain)
    dependency_lock = toolchain["dependency_wheelhouse_lock"]
    if not isinstance(dependency_lock, dict) or (dependency_lock and dependency_lock.get("schema") != "local_bmo.j1m.wheelhouse-lock.v1"):
        raise ValueError("toolchain dependency lock is invalid")

    command_receipt = _bounded_json_file(output_dir / "command-receipt.json")
    validate_persisted_receipt(command_receipt)
    if not isinstance(command_receipt, list) or not command_receipt or len(command_receipt) > 128:
        raise ValueError("command receipt sequence is invalid")
    base = {"stage", "argv", "started_at_utc", "ended_at_utc", "exit_code", "status"}
    optional = {"stdout_tail", "stderr_tail", "error_type"}
    for expected_stage, item in enumerate(command_receipt, 1):
        if not isinstance(item, dict) or not base <= set(item) or set(item) - base - optional:
            raise ValueError("command receipt entry is invalid")
        if isinstance(item["stage"], bool) or not isinstance(item["stage"], int) or item["stage"] <= 0 or not isinstance(item["argv"], list) or not item["argv"] or any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 4096 for arg in item["argv"]):
            raise ValueError("command receipt argv is invalid")
        if (item["status"] not in {"completed", "failed", "launch_failed", "transport_timeout"} or
                item["stage"] != expected_stage or item["status"] != "completed" or
                isinstance(item["exit_code"], bool) or not isinstance(item["exit_code"], int) or
                item["exit_code"] != 0):
            raise ValueError("command receipt status is invalid")
        if any(not isinstance(item[field], str) or not item[field] for field in ("started_at_utc", "ended_at_utc")):
            raise ValueError("command receipt timestamp is invalid")
        validate_persisted_argv(item["argv"])
        for field in optional & set(item):
            if not isinstance(item[field], str) or len(item[field]) > _COMMAND_LOG_TAIL_LIMIT:
                raise ValueError("command receipt diagnostic is invalid")
            validate_persisted_output(item[field])

    scan = _bounded_json_file(output_dir / "scan-receipt.json")
    validate_persisted_receipt(scan)
    if not isinstance(scan, dict) or set(scan) != {"schema", "status", "inventory_scope", "text_only", "artifacts", "vision_projection_present"} or scan["schema"] != "local_bmo.j1m.scan-receipt.v1" or scan["status"] != "verified" or scan["inventory_scope"] != "pre_cleanup_conversion_outputs" or scan["text_only"] is not True or scan["vision_projection_present"] is not False:
        raise ValueError("scan receipt is invalid")
    scan_artifacts = scan["artifacts"]
    expected_names = {"Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"}
    if not isinstance(scan_artifacts, list) or len(scan_artifacts) != 3 or {item.get("name") for item in scan_artifacts if isinstance(item, dict)} != expected_names or any(not isinstance(item, dict) or set(item) != {"name", "size_bytes", "sha256"} or not isinstance(item["name"], str) or isinstance(item["size_bytes"], bool) or not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0 for item in scan_artifacts):
        raise ValueError("scan artifact inventory is invalid")
    for item in scan_artifacts:
        _receipt_sha(item["sha256"], "scan artifact hash")
    post = _bounded_json_file(output_dir / "post-cleanup-receipt.json")
    validate_persisted_receipt(post)
    if not isinstance(post, dict) or set(post) != {"schema", "status", "inventory_scope", "intermediates_absent", "remaining_gguf", "forbidden_artifacts", "q4"} or post["schema"] != "local_bmo.j1m.post-cleanup-receipt.v1" or post["status"] != "verified" or post["inventory_scope"] != "post_cleanup_filesystem" or post["intermediates_absent"] is not True or post["remaining_gguf"] != ["Qwen3.5-9B-Q4_K_M.gguf"] or post["forbidden_artifacts"] != []:
        raise ValueError("post-cleanup receipt is invalid")
    q4_scan = next(item for item in scan_artifacts if item["name"] == "Qwen3.5-9B-Q4_K_M.gguf")
    if (not isinstance(post["q4"], dict) or set(post["q4"]) != {"size_bytes", "sha256"} or
            post["q4"] != {"size_bytes": q4_scan["size_bytes"], "sha256": q4_scan["sha256"]}):
        raise ValueError("post-cleanup Q4 identity is invalid")
    return source, tensor, toolchain, command_receipt, scan, post


def _normalize_gguf_value(value: Any) -> Any:
    """Turn gguf ReaderField contents into JSON/scalar text without ndarray reprs."""

    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, dict):
        return {str(key): _normalize_gguf_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_gguf_value(item) for item in value]
        return normalized[0] if len(normalized) == 1 else normalized
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    if hasattr(value, "item"):
        try:
            return _normalize_gguf_value(value.item())
        except ValueError:
            pass
    return value


def _reader_field_value(field: Any) -> Any:
    contents = field.contents() if callable(getattr(field, "contents", None)) else getattr(field, "contents", None)
    if contents is None and hasattr(field, "parts"):
        contents = field.parts[-1]
    return _normalize_gguf_value(contents)


def verify_source(source_dir: Path, lock_path: Path = SOURCE_LOCK) -> dict[str, Any]:
    """Verify the local source checkout against the frozen lock.

    A checkout must carry ``.source-revision`` (or ``REVISION``) containing the
    exact HF commit.  Hash verification is performed before conversion starts.
    """

    lock = _bounded_json_file(lock_path)
    if not isinstance(lock, dict):
        raise ValueError("source lock is invalid")
    if not isinstance(lock.get("source_files"), list):
        raise ValueError("source lock file inventory is invalid")
    revision = str(lock.get("revision", ""))
    marker = next((source_dir / name for name in (".source-revision", "REVISION") if (source_dir / name).is_file()), None)
    try:
        marker_revision = marker.read_text(encoding="utf-8").strip() if marker is not None else ""
    except (OSError, UnicodeError):
        raise ValueError("source revision marker is unavailable") from None
    if marker is None or marker_revision != revision:
        raise ValueError("HF source revision marker does not match the immutable lock")
    checked: list[str] = []
    for item in lock.get("source_files", []):
        if not isinstance(item, dict) or item.get("excluded_from_text_only"):
            continue
        expected = item.get("sha256") or item.get("lfs_sha256")
        if not expected:
            continue
        path = source_dir / str(item["path"])
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError("source hash verification failed")
        checked.append(str(item["path"]))
    return {"schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified", "model_id": lock["model_id"], "revision": revision, "checked_files": checked, "file_hashes": {str(item["path"]): str(item.get("sha256") or item.get("lfs_sha256")) for item in lock.get("source_files", []) if isinstance(item, dict) and str(item.get("path")) in checked}, "license_sha256": lock["source_receipts"]["license_sha256"], "tokenizer_sha256": lock["source_receipts"]["tokenizer_sha256"], "chat_template_sha256": lock["source_receipts"]["chat_template_sha256"], "verified_at_utc": utc_now()}


def mark_source(source_dir: Path, revision: str) -> None:
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("source marker requires a full lowercase commit SHA")
    marker = source_dir / ".source-revision"
    _private_atomic_write(marker, (revision + "\n").encode("utf-8"))


def check_scratch(path: Path, required_gib: int) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    available_gib = usage.free / (1024 ** 3)
    if available_gib < required_gib:
        raise RuntimeError(f"insufficient scratch: {available_gib:.1f} GiB available; {required_gib} GiB required")
    return {"path": str(path), "available_gib": round(available_gib, 2), "required_gib": required_gib}


def scan_artifacts(output_dir: Path) -> dict[str, Any]:
    names = ["Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf"]
    records = [{"name": name, "size_bytes": (output_dir / name).stat().st_size, "sha256": _sha256(output_dir / name)} for name in names]
    if any("mmproj" in path.name.lower() for path in output_dir.iterdir()):
        raise ValueError("vision/mmproj artifact is forbidden")
    tensor_path = output_dir / "tensor-metadata.json"
    if not tensor_path.is_file():
        raise ValueError("tensor metadata is required before the artifact scan")
    tensor_metadata = _bounded_json_file(tensor_path)
    if not isinstance(tensor_metadata, dict):
        raise ValueError("tensor metadata receipt is invalid")
    vision_present = tensor_metadata.get("vision_projection_present")
    if vision_present is not False:
        raise ValueError("GGUF inspection did not prove absence of vision/mmproj tensors")
    payload = {"schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified", "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True, "artifacts": records, "vision_projection_present": vision_present}
    _write_validated_json(output_dir / "scan-receipt.json", payload)
    return payload


def post_cleanup_verify(output_dir: Path) -> dict[str, Any]:
    """Prove the remote filesystem no longer contains conversion intermediates."""

    remaining_gguf = {path.name for path in output_dir.glob("*.gguf") if path.is_file()}
    expected = {"Qwen3.5-9B-Q4_K_M.gguf"}
    if remaining_gguf != expected:
        raise ValueError("post-cleanup verification requires exactly the Q4 deployable GGUF")
    forbidden_names = {
        path.name for path in output_dir.iterdir()
        if path.is_file() and any(term in path.name.lower() for term in ("mmproj", "vision"))
    }
    if forbidden_names:
        raise ValueError("post-cleanup verification found a vision/mmproj artifact")
    q4 = output_dir / "Qwen3.5-9B-Q4_K_M.gguf"
    payload = {"schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified", "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True, "remaining_gguf": sorted(remaining_gguf), "forbidden_artifacts": [], "q4": {"size_bytes": q4.stat().st_size, "sha256": _sha256(q4)}}
    _write_validated_json(output_dir / "post-cleanup-receipt.json", payload)
    return payload


@contextlib.contextmanager
def hf_token_file(token: str | None = None) -> Iterator[Path]:
    """Yield a 0600 token file and remove it on every exit path."""

    value = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
    if not value:
        raise ValueError("HF_TOKEN is required only when executing the remote build")
    fd, name = tempfile.mkstemp(prefix="j1m-hf-", suffix=".env")
    path = Path(name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"HF_TOKEN={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PermissionError("HF token file is not private")
        yield path
    finally:
        path.unlink(missing_ok=True)


def _safe_artifact_name(value: str) -> str:
    path = Path(value)
    if path.name != value or value in {"", ".", ".."} or "\\" in value:
        raise ValueError("artifact is outside the allowlist")
    return value


def artifact_manifest(output_dir: Path, names: list[str], *, tensor_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    artifacts = []
    for raw_name in names:
        name = _safe_artifact_name(raw_name)
        if name in {"manifest.json", "checksums.sha256"}:
            continue
        path = output_dir / name
        if not path.is_file():
            raise FileNotFoundError(path)
        artifacts.append({"name": name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    return {
        "schema": "local_bmo.j1m.artifact-manifest.v1",
        "created_at_utc": utc_now(),
        "inventory_scope": "post_cleanup_deployable_allowlist",
        "deployable_model_artifacts": ["Qwen3.5-9B-Q4_K_M.gguf"],
        "text_only": True,
        "artifacts": artifacts,
        "tensor_metadata": tensor_metadata or {"status": "pending_converter_receipt"},
    }


def write_wheelhouse_lock(wheelhouse: Path, lock_path: Path, *, llama_revision: str) -> dict[str, Any]:
    if len(llama_revision) != 40 or any(character not in "0123456789abcdef" for character in llama_revision):
        raise ValueError("wheelhouse lock requires the full pinned llama.cpp revision")
    files = sorted(path for path in wheelhouse.iterdir() if path.is_file()) if wheelhouse.is_dir() else []
    if not files:
        raise ValueError("dependency wheelhouse is empty")
    entries = [{"name": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)} for path in files]
    payload = {"schema": "local_bmo.j1m.wheelhouse-lock.v1", "llama_cpp_revision": llama_revision, "artifacts": entries, "created_at_utc": utc_now()}
    _write_validated_json(lock_path, payload)
    return payload


def verify_wheelhouse(wheelhouse: Path, lock_path: Path) -> dict[str, Any]:
    payload = _bounded_json_file(lock_path)
    if not isinstance(payload, dict):
        raise ValueError("invalid dependency wheelhouse lock")
    entries = payload.get("artifacts")
    if payload.get("schema") != "local_bmo.j1m.wheelhouse-lock.v1" or not isinstance(entries, list) or not entries:
        raise ValueError("invalid dependency wheelhouse lock")
    expected = {str(item.get("name")): item for item in entries if isinstance(item, dict)}
    if len(expected) != len(entries):
        raise ValueError("dependency wheelhouse lock has duplicate or malformed entries")
    actual_files = {path.name: path for path in wheelhouse.iterdir() if path.is_file()}
    if set(actual_files) != set(expected):
        raise ValueError("dependency wheelhouse changed after hash lock")
    for name, item in expected.items():
        if item.get("size_bytes") != actual_files[name].stat().st_size or item.get("sha256") != _sha256(actual_files[name]):
            raise ValueError("dependency wheel hash verification failed")
    return {"status": "verified", "artifact_count": len(expected), "llama_cpp_revision": payload.get("llama_cpp_revision")}


def write_artifacts(output_dir: Path, names: list[str], *, source_lock: Path = SOURCE_LOCK, commands: list[list[str]] | None = None, llama_revision: str | None = None) -> dict[str, Any]:
    """Write receipts in dependency order, avoiding a self-referential manifest."""

    if not output_dir.is_dir():
        raise ValueError("artifact output directory must already exist")
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("artifact names are invalid")
    validate_persisted_argv(["artifact-names", *names])
    tensor_path = output_dir / "tensor-metadata.json"
    if not tensor_path.is_file():
        raise ValueError("tensor metadata is required before final artifacts")
    source_receipt = output_dir / "source-model-receipt.json"
    if not source_receipt.is_file():
        raise ValueError("source-model-receipt.json must be emitted by hash verification before artifacts")
    toolchain_path = output_dir / "toolchain.json"
    if not toolchain_path.is_file():
        raise ValueError("toolchain receipt is required before model receipt")
    for required in ("command-receipt.json", "scan-receipt.json", "post-cleanup-receipt.json"):
        if not (output_dir / required).is_file():
            raise ValueError(f"{required} is required before final manifest")
    source, tensor, toolchain, command_receipt, scan_receipt, post_cleanup_receipt = _validate_producer_receipts(
        output_dir, source_lock, llama_revision=llama_revision,
    )
    scan_artifacts = scan_receipt.get("artifacts")
    artifact_hashes = {item["name"]: {"size_bytes": item["size_bytes"], "sha256": item["sha256"]} for item in scan_artifacts}
    q4 = output_dir / "Qwen3.5-9B-Q4_K_M.gguf"
    current_q4 = {"size_bytes": q4.stat().st_size, "sha256": _sha256(q4)} if q4.is_file() else None
    post_q4 = post_cleanup_receipt.get("q4")
    if current_q4 != artifact_hashes.get("Qwen3.5-9B-Q4_K_M.gguf") or post_q4 != current_q4:
        raise ValueError("Q4 hash/size does not agree across pre- and post-cleanup receipts")
    converter_commands = [command for command in (commands or []) if any("convert_hf_to_gguf.py" in part for part in command) or "Q4_K_M" in command]
    for command in converter_commands:
        validate_persisted_argv(command)
    conversion_receipt = {"schema": "local_bmo.j1m.conversion-receipt.v1", "status": "conversion-complete", "text_only": True, "source_revision": source.get("revision"), "llama_cpp_revision": llama_revision, "artifacts": artifact_hashes, "converter_and_quantizer_argv": converter_commands, "command_receipt_sha256": _sha256(output_dir / "command-receipt.json"), "toolchain": toolchain, "no_mmproj": True, "no_mtp": True}
    model_receipt = {"schema": "local_bmo.j1m.model-receipt.v1", "status": "checksums-and-tensor-inventory-verified", "text_only": True, "q4_artifact": artifact_hashes.get("Qwen3.5-9B-Q4_K_M.gguf"), "tensor_metadata_sha256": _sha256(tensor_path), "gguf_metadata": tensor.get("gguf_metadata", {}), "vision_projection_present": tensor.get("vision_projection_present"), "scan_receipt_sha256": _sha256(output_dir / "scan-receipt.json"), "tokenizer_sha256": source.get("tokenizer_sha256"), "chat_template_sha256": source.get("chat_template_sha256"), "license_sha256": source.get("license_sha256")}
    validate_persisted_receipt(conversion_receipt)
    validate_persisted_receipt(model_receipt)
    _write_validated_json(output_dir / "conversion-receipt.json", conversion_receipt)
    _write_validated_json(output_dir / "model-receipt.json", model_receipt)
    # The deployable manifest is self-verifiable after intermediates are
    # securely removed. Intermediate BF16/Q8 hashes remain in conversion
    # receipt, but are intentionally absent from the shipped bundle.
    deployable = ["Qwen3.5-9B-Q4_K_M.gguf", "tensor-metadata.json", "source-model-receipt.json", "conversion-receipt.json", "model-receipt.json", "toolchain.json", "post-cleanup-receipt.json"]
    bounded_tensor_metadata = {
        "status": tensor["status"],
        "tensor_count": tensor.get("tensor_count"),
        "tensor_inventory_sha256": _sha256(tensor_path),
        "gguf_metadata": {key: value for key, value in tensor.get("gguf_metadata", {}).items() if key != "tokenizer.chat_template"},
        "chat_template_sha256": tensor.get("chat_template_sha256"),
        "vision_projection_present": tensor.get("vision_projection_present"),
    }
    manifest = artifact_manifest(output_dir, [*deployable, "command-receipt.json", "scan-receipt.json"], tensor_metadata=bounded_tensor_metadata)
    _write_validated_json(output_dir / "manifest.json", manifest)
    checksum_names = [item["name"] for item in manifest["artifacts"]] + ["manifest.json"]
    checksums = "".join(f"{_sha256(output_dir / name)}  {name}\n" for name in checksum_names)
    validate_persisted_output(checksums)
    _private_atomic_write(output_dir / "checksums.sha256", checksums.encode("utf-8"))
    return manifest


def comparator_cleanup_plan(output: str = "/scratch/j1m/artifacts", runner: str = "scripts/j1m_runner.py", config_path: str = "model/conversion/j1m-config.json", source_lock: str | None = None) -> list[list[str]]:
    """Return the intermediate-deletion tail that ``retain_comparators`` defers.

    These are the exact final three stages of :func:`command_plan`.  They are
    only ever *moved*, never dropped: a caller that retains the comparators for
    evaluation must append this plan so intermediate deletion, the
    post-cleanup receipt, and the manifest still happen on the same host.
    """

    python_exec = "/scratch/j1m/venv/bin/python"
    return [
        ["rm", "-f", f"{output}/Qwen3.5-9B-bf16.gguf", f"{output}/Qwen3.5-9B-Q8_0.gguf"],
        [python_exec, runner, "--config", config_path, "--post-cleanup", output],
        [python_exec, runner, "--config", config_path, "--manifest", output, "--lock", source_lock or str(SOURCE_LOCK)],
    ]


COMPARATOR_SERVER_BUILD_ROOT = "/scratch/llama-server-build"


def comparator_server_configure_flags(config: dict[str, Any]) -> list[str]:
    """Configure flags for the pinned upstream ``llama-server``.

    The compiler identity is deliberately the *same* as the product engine's
    CUDA eval build (``Release``, the same ``CMAKE_CUDA_ARCHITECTURES`` and
    the same ``CMAKE_CUDA_COMPILER``), because the comparator arms are only
    a meaningful oracle when the runtime differs in the weights and nothing
    else.  Everything the evaluator does not use is off: no tests, no
    examples, no unified app, no embedded web UI -- and in particular
    ``LLAMA_USE_PREBUILT_UI=OFF`` and ``LLAMA_OPENSSL=OFF``, so the build
    fetches nothing and the binary carries no HTTPS client.

    The returned list is recorded verbatim in the per-arm receipt, so the
    flags a published number was produced under are auditable from the
    receipt alone.
    """

    eval_mode = config["modes"]["eval"]
    return [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DGGML_CUDA=ON",
        f"-DCMAKE_CUDA_ARCHITECTURES={eval_mode['cuda_architecture']}",
        f"-DCMAKE_CUDA_COMPILER={eval_mode['cuda_compiler']}",
        "-DLLAMA_BUILD_COMMON=ON",
        "-DLLAMA_BUILD_TOOLS=ON",
        "-DLLAMA_BUILD_SERVER=ON",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_APP=OFF",
        "-DLLAMA_BUILD_UI=OFF",
        "-DLLAMA_USE_PREBUILT_UI=OFF",
        "-DLLAMA_OPENSSL=OFF",
    ]


def comparator_server_binary(build_root: str = COMPARATOR_SERVER_BUILD_ROOT) -> str:
    """Path of the built upstream server. Upstream emits tools into ``bin/``."""

    return f"{build_root}/bin/llama-server"


def comparator_server_plan(config: dict[str, Any], *, runner: str = "scripts/j1m_runner.py", config_path: str = "model/conversion/j1m-config.json", build_root: str = COMPARATOR_SERVER_BUILD_ROOT) -> list[list[str]]:
    """One-time upstream ``llama-server`` build from the pinned revision.

    A separate build tree from the conversion build (which is CPU-only and
    has ``LLAMA_BUILD_SERVER=OFF``) and from the product engine build (whose
    ``native/CMakeLists.txt`` pins ``LLAMA_BUILD_SERVER OFF ... FORCE``).
    The pinned revision is re-verified immediately before configuring, so an
    unexpected checkout refuses rather than building unknown sources.
    """

    llama = config["llama_cpp"]
    return [
        ["python3", runner, "--config", config_path, "--verify-llama", llama["checkout"], llama["revision"]],
        ["cmake", "-S", llama["checkout"], "-B", build_root, *comparator_server_configure_flags(config)],
        ["cmake", "--build", build_root, "--target", "llama-server", "--parallel", str(config["modes"]["eval"]["build_parallelism"])],
    ]


def command_plan(config: dict[str, Any], source: str = "/scratch/hf/Qwen3.5-9B", output: str = "/scratch/j1m/artifacts", runner: str = "scripts/j1m_runner.py", config_path: str = "model/conversion/j1m-config.json", source_lock: str | None = None, *, retain_comparators: bool = False) -> list[list[str]]:
    llama = config["llama_cpp"]
    converter = f"{llama['checkout']}/convert_hf_to_gguf.py"
    python_exec = "/scratch/j1m/venv/bin/python"
    hf_exec = "/scratch/j1m/venv/bin/hf"
    wheelhouse = "/scratch/j1m/wheelhouse"
    wheelhouse_lock = f"{output}/wheelhouse-lock.json"
    return [
        ["git", "clone", "--filter=blob:none", config["llama_cpp"]["repository"], llama["checkout"]],
        ["git", "-C", llama["checkout"], "checkout", "--detach", llama["revision"]],
        ["sudo", "apt-get", "update"],
        ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "python3-venv", "cmake", "build-essential"],
        ["python3", "-m", "venv", "/scratch/j1m/venv"],
        ["mkdir", "-p", wheelhouse],
        ["/scratch/j1m/venv/bin/pip", "wheel", "--disable-pip-version-check", "--no-input", "--wheel-dir", wheelhouse, "-r", f"{llama['checkout']}/{config['python_dependencies']['requirements_file']}", f"{llama['checkout']}/{config['python_dependencies']['local_gguf_package']}"],
        [python_exec, runner, "--config", config_path, "--wheelhouse-lock", wheelhouse_lock, "--wheelhouse", wheelhouse, "--llama-revision", llama["revision"]],
        ["/scratch/j1m/venv/bin/pip", "install", "--disable-pip-version-check", "--no-input", "--no-index", "--find-links", wheelhouse, "-r", f"{llama['checkout']}/{config['python_dependencies']['requirements_file']}", "gguf"],
        [python_exec, runner, "--config", config_path, "--wheelhouse-lock", wheelhouse_lock, "--verify-wheelhouse", "--wheelhouse", wheelhouse],
        [python_exec, runner, "--config", config_path, "--pip-freeze", f"{output}/pip-freeze.txt"],
        [python_exec, runner, "--config", config_path, "--toolchain", f"{output}/toolchain.json", "--llama-checkout", llama["checkout"], "--wheelhouse-lock", wheelhouse_lock],
        ["git", "--version"],
        ["cmake", "--version"],
        ["python3", "--version"],
        ["mkdir", "-p", source, output],
        ["python3", runner, "--config", config_path, "--verify-llama", llama["checkout"], llama["revision"]],
        [
            "cmake", "-S", llama["checkout"], "-B", f"{llama['checkout']}/build",
            "-DGGML_CUDA=OFF",
            "-DLLAMA_BUILD_TOOLS=ON",
            "-DLLAMA_BUILD_TESTS=OFF",
            "-DLLAMA_BUILD_EXAMPLES=OFF",
            "-DLLAMA_BUILD_SERVER=OFF",
            "-DLLAMA_BUILD_APP=OFF",
            "-DLLAMA_BUILD_UI=OFF",
            "-DLLAMA_OPENSSL=OFF",
        ],
        ["cmake", "--build", f"{llama['checkout']}/build", "--target", "llama-quantize", "-j2"],
        [python_exec, runner, "--config", config_path, "--scratch", "/scratch", "--min-scratch-gib", str(config["resources"]["required_scratch_gib"])],
        [hf_exec, "download", config["source"]["model_id"], "--revision", config["source"]["revision"], "--local-dir", source],
        [python_exec, runner, "--config", config_path, "--mark-source", source, "--revision", config["source"]["revision"]],
        [python_exec, "-u", runner, "--config", config_path, "--verify-source", source, "--lock", "/scratch/j1m/qwen35-9b.source-lock.json", "--receipt", f"{output}/source-model-receipt.json"],
        [python_exec, converter, source, "--outfile", f"{output}/Qwen3.5-9B-bf16.gguf", "--outtype", "bf16", "--no-mtp"],
        [python_exec, converter, source, "--outfile", f"{output}/Qwen3.5-9B-Q8_0.gguf", "--outtype", "q8_0", "--no-mtp"],
        [f"{llama['quantizer']}", f"{output}/Qwen3.5-9B-bf16.gguf", f"{output}/Qwen3.5-9B-Q4_K_M.gguf", "Q4_K_M"],
        [python_exec, runner, "--config", config_path, "--inspect-tensors", f"{output}/Qwen3.5-9B-Q4_K_M.gguf", f"{output}/tensor-metadata.json", "--source-receipt", f"{output}/source-model-receipt.json"],
        [python_exec, runner, "--config", config_path, "--scan", output],
        # The deployable-only tail is deferred, never dropped, when the
        # comparators must survive long enough to be evaluated.
        *([] if retain_comparators else comparator_cleanup_plan(output, runner, config_path, source_lock)),
    ]


def canary_command_plan(config: dict[str, Any], remote_root: str = "/scratch/j1m-canary") -> list[list[str]]:
    """Return the no-model remote validation command seam.

    This deliberately contains only bounded read-only prerequisite probes.
    It has no source/model paths, package installation, compiler invocation,
    conversion, quantization, engine build, or evaluator command.  Provider
    activation, SSH host-key pinning, receipt salvage, and exact teardown are
    lifecycle responsibilities and remain gated outside this pure plan.
    """

    canary = config["modes"]["canary"]
    if canary.get("no_model") is not True or canary.get("probe_only") is not True:
        raise ValueError("canary plan requires no-model probe-only configuration")
    return [
        ["python3", f"{remote_root}/remote_toolchain_probe.py", "--nvcc", "/usr/local/cuda/bin/nvcc", "--output", f"{remote_root}/artifacts/toolchain-receipt.json"],
        ["python3", f"{remote_root}/cuda_device_probe.py", "--output", f"{remote_root}/artifacts/cuda-device-receipt.json"],
    ]


def read_token_file(path: Path) -> str:
    fd, opened, parents = _open_private_handle(os.fspath(path))
    try:
        raw = bytearray()
        while len(raw) <= _TOKEN_MAX_BYTES:
            block = os.read(fd, min(4096, _TOKEN_MAX_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if len(raw) > _TOKEN_MAX_BYTES:
            raise ValueError("private token file exceeds its bound")
        after = os.fstat(fd)
        if (
            (after.st_dev, after.st_ino, after.st_uid, after.st_nlink, stat.S_IMODE(after.st_mode), after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_nlink, stat.S_IMODE(opened.st_mode), opened.st_size)
        ):
            raise ValueError("private token file changed during read")
        for parent, expected in parents:
            current = os.lstat(parent)
            if (current.st_dev, current.st_ino, current.st_uid, stat.S_IMODE(current.st_mode)) != (
                expected.st_dev, expected.st_ino, expected.st_uid, stat.S_IMODE(expected.st_mode)
            ):
                raise ValueError("private token file changed during read")
        try:
            text = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("private token file encoding is invalid") from exc
        entries: dict[str, str] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key != TOKEN_ENV:
                # Reject lookalike/case-variant credential names instead of
                # silently accepting an ambiguous environment file.
                if _credential_like_text(key) is not None or key.casefold() == TOKEN_ENV.casefold():
                    raise ValueError("private token file contains an invalid credential name")
                continue
            if key in entries:
                raise ValueError("private token file contains duplicate credential name")
            entries[key] = value.strip()
        token = entries.get(TOKEN_ENV, "")
        if not token:
            raise ValueError("private token file has no HF_TOKEN")
        return token
    except OSError:
        raise ValueError("private token file read refused") from None
    finally:
        os.close(fd)


def _bounded_command_tail(stream: Any) -> str:
    stream.flush()
    stream.seek(0)
    tail = bytearray()
    carry = ""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        text = carry + chunk.decode("utf-8", errors="strict")
        validate_persisted_output(text)
        carry = text[-256:]
        tail.extend(chunk)
        if len(tail) > _COMMAND_LOG_TAIL_LIMIT * 4:
            del tail[: len(tail) - (_COMMAND_LOG_TAIL_LIMIT * 4)]
    value = bytes(tail).decode("utf-8", errors="strict")
    validate_persisted_output(value)
    return value[-_COMMAND_LOG_TAIL_LIMIT:]


def run_commands(commands: list[list[str]], progress_path: Path, *, cwd: Path | None = None, token_file: Path | None = None, receipt_path: Path | None = None, trusted_root: Path | None = None) -> list[dict[str, Any]]:
    """Run an already-reviewed argv plan, recording progress before each stage."""

    for command in commands:
        if not command or any("\x00" in str(part) for part in command):
            raise ValueError("invalid empty/NUL command")
        validate_persisted_argv(command)
    receipts = []
    all_stage_receipts = []
    output_root = trusted_root or PRIVATE_OUTPUT_ROOT
    if receipt_path:
        _private_atomic_write(receipt_path, b"[]\n", trusted_root=output_root)
    for index, command in enumerate(commands):
        write_progress(progress_path, f"stage-{index + 1}-starting", argv=command,
                       trusted_root=output_root)
        started_at = utc_now()
        try:
            # Never inherit dotenv/API credentials into child tools. The token
            # is added only to the one exact HF download subprocess.
            environment = {key: os.environ[key] for key in CHILD_ENV_ALLOWLIST if key in os.environ}
            if token_file is not None and any(part == "download" for part in command):
                environment[TOKEN_ENV] = read_token_file(token_file)
            # Keep verbose converter/download output off the SSH transport and
            # retain only bounded, redacted tails when a stage fails.
            with tempfile.TemporaryFile() as stdout_log, tempfile.TemporaryFile() as stderr_log:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    check=False,
                    timeout=6 * 60 * 60,
                    env=environment,
                    stdout=stdout_log,
                    stderr=stderr_log,
                )
                try:
                    stdout_tail = _bounded_command_tail(stdout_log)
                    stderr_tail = _bounded_command_tail(stderr_log)
                except (ValueError, UnicodeError) as exc:
                    # The process may complete, but unsafe output is a hard
                    # receipt rejection. Persist only a finite code and no
                    # untrusted output/argv.
                    stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": completed.returncode, "status": "failed", "error_type": "unsafe_output", "stderr_tail": "<redacted>"}
                    validate_persisted_receipt(stage_receipt)
                    all_stage_receipts.append(stage_receipt)
                    if receipt_path:
                        receipts.append(stage_receipt)
                        _write_validated_json(receipt_path, receipts,
                                              trusted_root=output_root)
                    write_progress(progress_path, f"stage-{index + 1}-failed",
                                   trusted_root=output_root,
                                   **{key: value for key, value in stage_receipt.items() if key != "stage"})
                    break
            stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": completed.returncode, "status": "completed" if completed.returncode == 0 else "failed"}
            if completed.returncode != 0:
                stage_receipt["stdout_tail"] = stdout_tail
                stage_receipt["stderr_tail"] = stderr_tail
        except subprocess.TimeoutExpired:
            stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": None, "status": "transport_timeout"}
        except OSError:
            # OS exception text can contain command paths or credential
            # handles; retain only the finite category.
            message = "<redacted>"
            stage_receipt = {"stage": index + 1, "argv": command, "started_at_utc": started_at, "ended_at_utc": utc_now(), "exit_code": None, "status": "launch_failed", "error_type": "launch_failed", "stderr_tail": message[-_COMMAND_LOG_TAIL_LIMIT:]}
        # Manifest creation and intermediate cleanup are administrative stages;
        # they intentionally do not mutate the immutable conversion receipt.
        validate_persisted_receipt(stage_receipt)
        administrative = "--manifest" in command or "--post-cleanup" in command or (command and command[0] == "rm")
        all_stage_receipts.append(stage_receipt)
        if not administrative:
            receipts.append(stage_receipt)
            if receipt_path:
                _write_validated_json(receipt_path, receipts,
                                      trusted_root=output_root)
        progress_details = {key: value for key, value in stage_receipt.items() if key != "stage"}
        write_progress(progress_path, f"stage-{index + 1}-{stage_receipt['status']}",
                       trusted_root=output_root, **progress_details)
        if stage_receipt["status"] != "completed":
            break
    # Return administrative failures to the caller while keeping them out of
    # the immutable conversion receipt hashed by the manifest.
    return all_stage_receipts


def build_plan(config: dict[str, Any], mode: str = "prove") -> dict[str, Any]:
    target = config["shadeform_target"]
    selected_mode = config["modes"][mode]
    runtime = float(selected_mode["runtime_hours"])
    rate = float(target["hourly_usd"])
    return {
        "schema": "local_bmo.j1m.dry-run-plan.v1",
        "created_at_utc": utc_now(),
        "mutation": "refused: planning only; no provider API mutation",
        "candidate": target,
        "active_run_cost_usd": round(rate * runtime, 4),
        "provider_backstop_cost_usd": round(rate * float(selected_mode["provider_backstop_hours"]), 4),
        "commands": command_plan(config) if mode == "build" else canary_command_plan(config) if mode == "canary" else [["python3", "scripts/j1m_runner.py", "--prove"]],
        "artifact_allowlist": ["toolchain-receipt.json", "cuda-device-receipt.json"] if mode == "canary" else config["artifacts"]["allowlist"],
        "receipt_allowlist": ["toolchain-receipt.json", "cuda-device-receipt.json"] if mode == "canary" else config["artifacts"]["allowlist"],
        "required_scratch_gib": 0 if mode == "canary" else config["resources"]["required_scratch_gib"],
        **({"mode_policy": {"no_model": True, "probe_only": True, "salvage_required": True, "teardown_required": True}} if mode == "canary" else {}),
        **({"lifecycle_guards": {"activation": "existing_fail_closed_remote_gate", "host_key_pinning": "required", "salvage": "mandatory", "teardown": "mandatory"}} if mode == "canary" else {}),
    }


def write_progress(path: Path, stage: str, *, trusted_root: Path | None = None, **details: Any) -> None:
    validate_persisted_output(stage)
    validate_persisted_receipt(details)
    payload = {"stage": stage, "written_at_utc": utc_now(), **details}
    validate_persisted_receipt(payload)
    try:
        serialized = json.dumps(payload, sort_keys=True) + "\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("progress serialization refused") from exc
    validate_persisted_output(serialized)
    _private_atomic_write(path, serialized.encode("utf-8"),
                          trusted_root=trusted_root or PRIVATE_OUTPUT_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--verify-source", type=Path)
    parser.add_argument("--mark-source", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--lock", type=Path, default=SOURCE_LOCK)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--verify-llama", nargs=2, metavar=("CHECKOUT", "REVISION"))
    parser.add_argument("--prove", action="store_true", help="write a cheap host receipt; no model conversion")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--min-scratch-gib", type=int, default=0)
    parser.add_argument("--run", action="store_true", help="run the reviewed local argv plan")
    parser.add_argument("--token-file", type=Path, help="private remote token file; path only, never token material")
    parser.add_argument("--pip-freeze", type=Path)
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--wheelhouse-lock", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--verify-wheelhouse", action="store_true")
    parser.add_argument("--llama-revision")
    parser.add_argument("--llama-checkout", type=Path)
    parser.add_argument("--inspect-tensors", nargs=2, metavar=("GGUF", "OUTPUT"))
    parser.add_argument("--source-receipt", type=Path)
    parser.add_argument("--scan", type=Path)
    parser.add_argument("--post-cleanup", type=Path)
    parser.add_argument("--retain-comparators", action="store_true", help="defer intermediate deletion so the rebuilt comparators can be evaluated; the deferred stages must still be run")
    parser.add_argument("--execute", action="store_true", help="reserved for an already-approved host; never provisions")
    args = parser.parse_args(argv)
    if args.verify_llama:
        checkout, expected = args.verify_llama
        actual = subprocess.run(["git", "-C", checkout, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        validate_persisted_output(actual)
        if actual != expected:
            raise ValueError("llama.cpp checkout is not the pinned immutable revision")
        print(json.dumps({"revision": actual}, sort_keys=True))
        return 0
    if args.pip_freeze:
        freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], check=True, capture_output=True, text=True, timeout=120).stdout
        validate_persisted_output(freeze)
        _private_atomic_write(args.pip_freeze, freeze.encode("utf-8"))
        return 0
    if args.wheelhouse_lock and (args.wheelhouse is not None or args.verify_wheelhouse):
        if args.wheelhouse is None or not args.llama_revision and not args.verify_wheelhouse:
            raise ValueError("wheelhouse lock requires a wheelhouse; writing also requires the pinned llama revision")
        if args.verify_wheelhouse:
            verify_wheelhouse(args.wheelhouse, args.wheelhouse_lock)
        else:
            write_wheelhouse_lock(args.wheelhouse, args.wheelhouse_lock, llama_revision=args.llama_revision)
        return 0
    if args.toolchain:
        checkout = args.llama_checkout or Path(".")
        def version(command: list[str]) -> str:
            try:
                return subprocess.run(command, check=False, capture_output=True, text=True, timeout=30).stdout.splitlines()[0]
            except (OSError, IndexError):
                return "unavailable"
        freeze_path = args.toolchain.parent / "pip-freeze.txt"
        freeze = freeze_path.read_text(encoding="utf-8") if freeze_path.is_file() else ""
        dependency_lock = {}
        if args.wheelhouse_lock is not None:
            dependency_lock = _bounded_json_file(args.wheelhouse_lock)
            if dependency_lock.get("schema") != "local_bmo.j1m.wheelhouse-lock.v1" or not dependency_lock.get("artifacts"):
                raise ValueError("toolchain receipt requires a nonempty dependency wheelhouse lock")
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        try:
            os_packages = subprocess.run(
                ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\\n", "python3-venv", "cmake", "build-essential"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError):
            os_packages = ["unavailable"]
        payload = {"schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": head, "python": version([sys.executable, "--version"]), "cmake": version(["cmake", "--version"]), "compiler": version(["cc", "--version"]), "os_packages": os_packages, "pip_freeze": freeze, "dependency_wheelhouse_lock": dependency_lock}
        _write_validated_json(args.toolchain, payload)
        return 0
    if args.inspect_tensors:
        gguf_path, metadata_path = (Path(value) for value in args.inspect_tensors)
        try:
            from gguf import GGUFReader
            reader = GGUFReader(str(gguf_path))
            tensors = [{"name": tensor.name, "shape": [_normalize_gguf_value(dimension) for dimension in tensor.shape], "type": str(tensor.tensor_type)} for tensor in reader.tensors]
            wanted_fields = {"general.architecture", "general.file_type", "general.version", "tokenizer.chat_template"}
            fields = {str(key): _reader_field_value(value) for key, value in reader.fields.items() if str(key) in wanted_fields}
            architecture = str(fields.get("general.architecture", "")).lower().replace(".", "").replace("_", "")
            if architecture != "qwen35":
                raise ValueError("GGUF architecture is not qwen35")
            chat_template = str(fields.get("tokenizer.chat_template", ""))
            if not chat_template:
                raise ValueError("GGUF has no embedded tokenizer chat template")
            if args.source_receipt is not None:
                source_receipt = _bounded_json_file(args.source_receipt)
                expected_chat_hash = str(source_receipt.get("chat_template_sha256", ""))
                actual_chat_hash = hashlib.sha256(chat_template.encode("utf-8")).hexdigest()
                if not expected_chat_hash or actual_chat_hash != expected_chat_hash:
                    raise ValueError("GGUF chat template does not match the verified source receipt")
            metadata_names = [str(key).lower() for key in reader.fields]
            tensor_names = [str(tensor.name).lower() for tensor in reader.tensors]
            vision_terms = ("vision", "mmproj", "visual", "image")
            vision_projection_present = any(any(term in name for term in vision_terms) for name in [*metadata_names, *tensor_names])
            if vision_projection_present:
                raise ValueError("vision/mmproj metadata or tensor is forbidden")
            fields["gguf.version"] = _normalize_gguf_value(getattr(reader, "version", "unknown"))
            payload = {"schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True, "tensor_count": len(tensors), "tensors": tensors, "gguf_metadata": fields, "vision_projection_present": vision_projection_present, "chat_template_sha256": hashlib.sha256(chat_template.encode("utf-8")).hexdigest()}
        except Exception as exc:
            # Do not persist a failure payload containing untrusted producer
            # metadata or tensor names. The caller gets the bounded exception;
            # a finite placeholder is reserved for command-output receipts.
            raise ValueError("credential-like producer receipt rejected") from None
        _write_validated_json(metadata_path, payload)
        return 0
    if args.scan:
        scan_artifacts(args.scan)
        return 0
    if args.post_cleanup:
        post_cleanup_verify(args.post_cleanup)
        return 0
    if args.verify_source:
        receipt = verify_source(args.verify_source, args.lock)
        if args.receipt:
            _write_validated_json(args.receipt, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.mark_source:
        if not args.revision:
            raise ValueError("--revision is required with --mark-source")
        mark_source(args.mark_source, args.revision)
        return 0
    if args.manifest:
        config = load_config(args.config)
        names = config["artifacts"]["allowlist"]
        write_artifacts(args.manifest, names, source_lock=args.lock, commands=command_plan(config, source_lock=str(args.lock)), llama_revision=config["llama_cpp"]["revision"])
        return 0
    if args.prove:
        import platform
        receipt = {"schema": "local_bmo.j1m.proving-receipt.v1", "host": platform.node(), "python": platform.python_version(), "text_only": True, "conversion": "not-run"}
        if args.scratch and args.min_scratch_gib:
            receipt["scratch"] = check_scratch(args.scratch, args.min_scratch_gib)
        if args.output:
            _write_validated_json(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.scratch and args.min_scratch_gib:
        print(json.dumps(check_scratch(args.scratch, args.min_scratch_gib), sort_keys=True))
        return 0
    if args.run:
        config = load_config(args.config)
        commands = command_plan(config, runner=str(Path(__file__).resolve()), config_path="/scratch/j1m/j1m-config.json", source_lock=str(args.lock), retain_comparators=args.retain_comparators)
        receipts = run_commands(commands, ROOT / config["resources"]["progress_path"], token_file=args.token_file, receipt_path=Path("/scratch/j1m/artifacts/command-receipt.json"))
        return 0 if receipts and all(item["status"] == "completed" for item in receipts) else 1
    config = load_config(args.config)
    plan = build_plan(config)
    if args.plan:
        _write_validated_json(args.plan, plan)
    print(json.dumps(plan, indent=2, sort_keys=True))
    if args.execute:
        print("execution remains host-local and requires Sol's explicit review; no provider mutation was attempted")
    return 0


def _safe_cli(argv: list[str] | None = None) -> int:
    """Expose only a finite refusal code when invoked as a subprocess."""

    try:
        return main(argv)
    except SystemExit:
        raise
    except Exception:
        print(json.dumps({"status": "refused", "error_code": "input_rejected"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(_safe_cli())
