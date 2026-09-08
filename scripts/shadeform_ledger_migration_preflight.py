"""Read-only, bounded reconciliation of the pre-v2 Shadeform evidence.

This module is deliberately not a ledger migrator.  It never writes, repairs,
hashes, archives, changes permissions, or emits a genesis/prior-spend value.
Its output is a sanitized review report suitable for a human adjudicator.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import stat
import sys
import re
from typing import Any


SCHEMA = "local_bmo.shadeform.cost-ledger-migration-preflight.v1"
ADJUDICATION_SCHEMA = "local_bmo.shadeform.cost-ledger-adjudication-input.v1"
MAX_FILE_BYTES = 1_048_576
MAX_JSONL_LINES = 4096
MAX_RECEIPTS = 4096
MAX_JSON_OBJECT_BYTES = 65_536
MAX_JSON_DEPTH = 32
MAX_JSON_TOKEN_DIGITS = 64
MAX_USD = Decimal("1000000000")
MAX_RECEIPT_NAME_BYTES = 256
MAX_RECEIPT_DIRECTORY_BYTES = 1_048_576
MAX_IDENTITY_BYTES = 256
MAX_JSON_ELEMENTS = 1024
USD_QUANTUM = Decimal("0.000001")
ZERO_USD = Decimal("0")
_DISPLAY_HEADER = "| date | phase | instance id | gpu | $/hr | purpose | status | cost logged | idle min |"
_DISPLAY_SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"


class _EvidenceError(Exception):
    """An intentionally non-sensitive evidence failure."""


def _issue(issues: set[str], label: str) -> None:
    issues.add(label)


def _require_secure_capabilities(issues: set[str]) -> None:
    required = (
        isinstance(getattr(os, "O_NOFOLLOW", None), int), isinstance(getattr(os, "O_DIRECTORY", None), int),
        callable(getattr(os, "geteuid", None)),
        os.open in os.supports_dir_fd, os.stat in os.supports_dir_fd,
        os.scandir in os.supports_fd,
    )
    try:
        os.stat(os.curdir, follow_symlinks=False)
        os.lstat(os.curdir)
    except (TypeError, OSError, ValueError):
        required = (*required, False)
    if not all(required):
        _issue(issues, "secure_read_capability_unavailable")
        raise _EvidenceError("secure_read_capability_unavailable")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (int, Decimal, str)):
        raise InvalidOperation
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?", value) is None:
        raise InvalidOperation
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < ZERO_USD or parsed > MAX_USD:
        raise InvalidOperation
    if parsed.as_tuple().exponent < -6 or parsed.as_tuple().exponent > 0:
        raise InvalidOperation
    return parsed


def _money(value: Decimal) -> str:
    return f"${value.quantize(USD_QUANTUM):.6f}"


def _strict_object(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_JSON_OBJECT_BYTES:
        raise _EvidenceError("json_object_limit")
    depth = 0
    digits = 0
    string_bytes = 0
    in_string = escaped = False
    for character in data:
        if in_string:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                in_string = False
            else:
                string_bytes += 1
                if string_bytes > MAX_JSON_OBJECT_BYTES // 4:
                    raise _EvidenceError("json_string_limit")
            continue
        if character == 34:
            in_string = True
            string_bytes = 0
        elif character in (123, 91):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _EvidenceError("json_depth_limit")
        elif character in (125, 93):
            depth -= 1
        elif 48 <= character <= 57:
            digits += 1
            if digits > MAX_JSON_TOKEN_DIGITS:
                raise _EvidenceError("json_number_limit")
    if in_string or depth != 0:
        raise _EvidenceError("invalid_json")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(items) > 1024:
            raise _EvidenceError("json_container_limit")
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise _EvidenceError("duplicate_json_key")
            result[key] = value
        return result

    try:
        def bounded_number(value: str) -> int:
            if len(value.lstrip("-")) > MAX_JSON_TOKEN_DIGITS:
                raise _EvidenceError("json_number_limit")
            if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
                raise _EvidenceError("json_number_lexical")
            return int(value)

        def bounded_decimal(value: str) -> Decimal:
            if len(value.lstrip("-")) > MAX_JSON_TOKEN_DIGITS:
                raise _EvidenceError("json_number_limit")
            if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?", value) is None:
                raise _EvidenceError("json_number_lexical")
            parsed = Decimal(value)
            if not parsed.is_finite():
                raise _EvidenceError("nonfinite_number")
            if abs(parsed.adjusted()) > MAX_JSON_TOKEN_DIGITS:
                raise _EvidenceError("json_number_limit")
            return parsed

        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs,
            parse_float=bounded_decimal, parse_int=bounded_number,
            parse_constant=lambda _: (_ for _ in ()).throw(_EvidenceError("nonfinite_number")),
        )
        _validate_tree(value)
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, RecursionError,
            OverflowError, ValueError, TypeError) as exc:
        raise _EvidenceError("invalid_json") from exc
    if not isinstance(value, dict):
        raise _EvidenceError("json_not_object")
    return value


def _validate_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise _EvidenceError("json_depth_limit")
    if isinstance(value, dict):
        if len(value) > MAX_JSON_ELEMENTS:
            raise _EvidenceError("json_container_limit")
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > MAX_IDENTITY_BYTES or not key.isascii():
                raise _EvidenceError("json_key_limit")
            _validate_tree(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_JSON_ELEMENTS:
            raise _EvidenceError("json_container_limit")
        for child in value:
            _validate_tree(child, depth + 1)
    elif isinstance(value, str) and len(value.encode("utf-8")) > MAX_JSON_OBJECT_BYTES // 4:
        raise _EvidenceError("json_string_limit")


def _safe_identity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return bool(encoded) and len(encoded) <= MAX_IDENTITY_BYTES and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value
    ) is not None


def _canonical_signature(value: dict[str, Any]) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, dict):
            return {key: encode(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [encode(child) for child in item]
        return item
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _absolute_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise _EvidenceError("path_not_absolute")
    return Path(os.path.abspath(os.fspath(path)))


def _check_ancestors(path: Path, issues: set[str]) -> list[tuple[int, int, int, int]]:
    # lstat every named ancestor, so a symlink cannot hide an alternate root.
    cursor = path.parent
    chain: list[Path] = []
    while True:
        chain.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    identities: list[tuple[int, int, int, int]] = []
    for directory in reversed(chain):
        try:
            info = os.lstat(directory)
        except OSError:
            _issue(issues, "path_unavailable")
            raise _EvidenceError("path_unavailable")
        if stat.S_ISLNK(info.st_mode):
            _issue(issues, "symlink_path")
            raise _EvidenceError("symlink_path")
        if not stat.S_ISDIR(info.st_mode):
            _issue(issues, "parent_not_directory")
            raise _EvidenceError("parent_not_directory")
        # Ancestors are part of the evidence trust boundary.  System-owned
        # non-writable anchors (for example / on a normal Unix checkout) are
        # acceptable, but a foreign writable component is not.
        if stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            _issue(issues, "ancestor_unsafe_permissions")
            raise _EvidenceError("ancestor_unsafe_permissions")
        if hasattr(os, "geteuid") and int(info.st_uid) not in (os.geteuid(), 0):
            _issue(issues, "ancestor_unsafe_owner")
            # Keep the legacy parent label as a compatibility diagnostic; the
            # ancestor label is the authoritative refusal reason.
            _issue(issues, "parent_unsafe_owner")
            raise _EvidenceError("ancestor_unsafe_owner")
        identities.append((int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_uid)))
    return identities


def _ancestors_stable(path: Path, expected: list[tuple[int, int, int, int]], issues: set[str]) -> bool:
    try:
        actual = _check_ancestors(path, issues)
    except _EvidenceError:
        return False
    if actual != expected:
        _issue(issues, "ancestor_component_swap")
        return False
    return True


def _require_private_directory(info: os.stat_result, issues: set[str], label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        _issue(issues, f"{label}_not_directory")
        raise _EvidenceError(f"{label}_not_directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        _issue(issues, f"{label}_unsafe_permissions")
        raise _EvidenceError(f"{label}_unsafe_permissions")
    if hasattr(os, "geteuid") and int(info.st_uid) != os.geteuid():
        _issue(issues, f"{label}_unsafe_owner")
        raise _EvidenceError(f"{label}_unsafe_owner")


def _read_snapshot(path: Path, *, limit: int, issues: set[str]) -> tuple[bytes, dict[str, Any]]:
    try:
        _require_secure_capabilities(issues)
        path = _absolute_path(path)
        ancestors = _check_ancestors(path, issues)
        parent = path.parent
        preopen_info: os.stat_result | None = None
        try:
            preopen_info = os.lstat(path)
            if stat.S_ISLNK(preopen_info.st_mode):
                _issue(issues, "symlink_path")
                raise _EvidenceError("symlink_path")
        except FileNotFoundError:
            pass
        before_parent = os.stat(parent, follow_symlinks=False)
        _require_private_directory(before_parent, issues, "parent")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            parent_opened = os.fstat(parent_fd)
            if _file_identity(before_parent) != _file_identity(parent_opened):
                _issue(issues, "evidence_mutated_during_read")
                raise _EvidenceError("evidence_mutated_during_read")
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
            try:
                before = os.fstat(descriptor)
                if preopen_info is None or _file_identity(preopen_info) != _file_identity(before):
                    _issue(issues, "evidence_mutated_during_read")
                    raise _EvidenceError("evidence_mutated_during_read")
                if not stat.S_ISREG(before.st_mode):
                    _issue(issues, "not_regular_file")
                    raise _EvidenceError("not_regular_file")
                if before.st_nlink != 1:
                    _issue(issues, "hardlink_file")
                    raise _EvidenceError("hardlink_file")
                if stat.S_IMODE(before.st_mode) != 0o600:
                    _issue(issues, "unsafe_permissions")
                    raise _EvidenceError("unsafe_permissions")
                if hasattr(os, "geteuid") and int(before.st_uid) != os.geteuid():
                    _issue(issues, "unsafe_owner")
                    raise _EvidenceError("unsafe_owner")
                if before.st_size > limit:
                    _issue(issues, "byte_limit")
                    raise _EvidenceError("byte_limit")
                data = b""
                while len(data) <= limit:
                    chunk = os.read(descriptor, min(65_536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                after = os.fstat(descriptor)
                current_parent = os.stat(parent, follow_symlinks=False)
                current = os.stat(path, follow_symlinks=False)
                if _file_identity(before) != _file_identity(after) or \
                        _file_identity(after) != _file_identity(current) or \
                        _file_identity(before_parent) != _file_identity(current_parent):
                    _issue(issues, "evidence_mutated_during_read")
                    raise _EvidenceError("evidence_mutated_during_read")
                if stat.S_IMODE(after.st_mode) != stat.S_IMODE(before.st_mode) or int(after.st_uid) != int(before.st_uid):
                    _issue(issues, "evidence_mutated_during_read")
                    raise _EvidenceError("evidence_mutated_during_read")
                _require_private_directory(current_parent, issues, "parent")
                if not _ancestors_stable(path, ancestors, issues):
                    raise _EvidenceError("ancestor_component_swap")
                if len(data) > limit:
                    _issue(issues, "byte_limit")
                    raise _EvidenceError("byte_limit")
                mode = stat.S_IMODE(after.st_mode)
                if mode != 0o600:
                    _issue(issues, "unsafe_permissions")
                    raise _EvidenceError("unsafe_permissions")
                if hasattr(os, "geteuid") and int(after.st_uid) != os.geteuid():
                    _issue(issues, "unsafe_owner")
                    raise _EvidenceError("unsafe_owner")
                return data, {"private_permissions": not bool(mode & 0o077), "hardlink": after.st_nlink != 1}
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
    except _EvidenceError:
        raise
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        _issue(issues, "evidence_unavailable")
        raise _EvidenceError("evidence_unavailable") from exc


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_uid), int(info.st_nlink), int(info.st_size))


def _read_relative_snapshot(parent_fd: int, parent_before: os.stat_result, name: str, *, limit: int,
                             issues: set[str], expected: tuple[int, int, int, int, int, int] | None = None) -> bytes:
    """Read one receipt only through its already-open, private parent fd."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            before = os.fstat(descriptor)
            if expected is not None and _file_identity(before) != expected:
                _issue(issues, "receipt_identity_changed_before_read")
                raise _EvidenceError("receipt_identity_changed_before_read")
            if not stat.S_ISREG(before.st_mode):
                _issue(issues, "receipt_not_regular_file")
                raise _EvidenceError("receipt_not_regular_file")
            if before.st_nlink != 1:
                _issue(issues, "receipt_hardlink_file")
                raise _EvidenceError("receipt_hardlink_file")
            if stat.S_IMODE(before.st_mode) != 0o600:
                _issue(issues, "receipt_unsafe_permissions")
                raise _EvidenceError("receipt_unsafe_permissions")
            if int(before.st_uid) != os.geteuid():
                _issue(issues, "receipt_unsafe_owner")
                raise _EvidenceError("receipt_unsafe_owner")
            if before.st_size > limit:
                _issue(issues, "receipt_byte_limit")
                raise _EvidenceError("receipt_byte_limit")
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(descriptor, min(65_536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            parent_after = os.fstat(parent_fd)
            if len(b"".join(chunks)) > limit:
                _issue(issues, "receipt_byte_limit")
                raise _EvidenceError("receipt_byte_limit")
            if _file_identity(before) != _file_identity(after) or \
                    _file_identity(after) != _file_identity(current) or \
                    (parent_before.st_dev, parent_before.st_ino, parent_before.st_mode, parent_before.st_uid) != \
                    (parent_after.st_dev, parent_after.st_ino, parent_after.st_mode, parent_after.st_uid):
                _issue(issues, "evidence_mutated_during_read")
                raise _EvidenceError("evidence_mutated_during_read")
            _require_private_directory(parent_after, issues, "deletion_root")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except _EvidenceError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        _issue(issues, "receipt_unavailable")
        raise _EvidenceError("receipt_unavailable") from exc


def _read_jsonl(path: Path, *, label: str, issues: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        data, metadata = _read_snapshot(path, limit=MAX_FILE_BYTES, issues=issues)
    except _EvidenceError:
        return [], {"line_count": 0, "private_permissions": False, "parse_refused": True}
    if not data:
        _issue(issues, f"{label}_empty")
        return [], {**metadata, "line_count": 0, "parse_refused": True}
    if not data.endswith(b"\n"):
        _issue(issues, f"{label}_partial_line")
        return [], {**metadata, "line_count": 0, "parse_refused": True}
    lines = data.splitlines()
    if len(lines) > MAX_JSONL_LINES:
        _issue(issues, f"{label}_line_limit")
        return [], {**metadata, "line_count": len(lines), "parse_refused": True}
    rows: list[dict[str, Any]] = []
    malformed = 0
    for line in lines:
        if len(line) > MAX_JSON_OBJECT_BYTES:
            _issue(issues, f"{label}_json_object_limit")
            malformed += 1
            continue
        if not line:
            malformed += 1
            _issue(issues, f"{label}_blank_line")
            continue
        try:
            line.decode("utf-8")
            rows.append(_strict_object(line))
        except UnicodeDecodeError:
            malformed += 1
            _issue(issues, f"{label}_invalid_utf8")
        except _EvidenceError as exc:
            malformed += 1
            _issue(issues, f"{label}_{str(exc)}")
    if malformed:
        _issue(issues, f"{label}_malformed_rows")
        return [], {**metadata, "line_count": len(lines), "malformed_count": malformed, "parse_refused": True}
    return rows, {**metadata, "line_count": len(lines), "malformed_count": malformed, "parse_refused": False}


def _parse_legacy(path: Path, issues: set[str]) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    rows, metadata = _read_jsonl(path, label="legacy", issues=issues)
    allowed = {
        "actual_cost_usd", "candidate", "estimated_cost_usd", "instance_id", "ownership_nonce",
        "phase_id", "recorded_at_utc", "reservation", "ssh_key_id", "ssh_key_name",
        "ssh_public_key_fingerprint", "ssh_public_key_sha256", "status",
    }
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    signatures: set[str] = set()
    duplicate_count = 0
    settled = pending = 0
    total = ZERO_USD
    identity_absent = nonce_absent = 0
    for row in rows:
        signature = _canonical_signature(row)
        if signature in signatures:
            duplicate_count += 1
            _issue(issues, "duplicate_legacy_row")
        signatures.add(signature)
        if set(row) - allowed:
            _issue(issues, "legacy_future_or_unknown_fields")
        phase, instance = row.get("phase_id"), row.get("instance_id")
        if not _safe_identity(phase) or not _safe_identity(instance):
            _issue(issues, "legacy_identity_invalid")
            continue
        if "schema" not in row or "owner_binding_sha256" not in row:
            identity_absent += 1
        if row.get("status") == "settled":
            settled += 1
            if "ownership_nonce" not in row:
                nonce_absent += 1
            try:
                total += _decimal(row["actual_cost_usd"])
            except (KeyError, InvalidOperation):
                _issue(issues, "legacy_settled_cost_invalid")
        elif row.get("status") == "pending":
            pending += 1
            try:
                _decimal(row["estimated_cost_usd"])
            except (KeyError, InvalidOperation):
                _issue(issues, "legacy_pending_cost_invalid")
        else:
            _issue(issues, "legacy_status_invalid")
        key = (phase, instance)
        previous = groups.get(key)
        if previous is not None:
            if previous.get("status") == "pending" and row.get("status") == "pending" and \
                    previous.get("estimated_cost_usd") != row.get("estimated_cost_usd"):
                _issue(issues, "duplicate_or_changing_pending")
            if previous.get("status") == "settled" and (
                    row.get("status") != "settled" or row.get("actual_cost_usd") != previous.get("actual_cost_usd")):
                _issue(issues, "settled_rewrite")
        groups[key] = row
    # The old stream has no v2 schema, binding, or durable nonce semantics.
    if rows:
        _issue(issues, "legacy_schema_or_owner_binding_missing")
    refused = bool(metadata.get("parse_refused", False)) or any(
        issue.startswith("legacy_") or issue.startswith("duplicate_legacy") for issue in issues
    )
    return {
        "line_count": metadata.get("line_count", 0), "settled_row_count": None if refused else settled,
        "pending_row_count": None if refused else pending, "exact_settled_cost_usd": None if refused else _money(total),
        "latest_stream_pending_group_count": None if refused else sum(row.get("status") == "pending" for row in groups.values()),
        "duplicate_row_count": None if refused else duplicate_count, "legacy_owner_binding_absent_rows": None if refused else identity_absent,
        "legacy_settled_nonce_absent_rows": None if refused else nonce_absent, "v2_parser_accepts": False,
        "v2_rejection": "legacy_schema_or_owner_binding_missing",
        "malformed_count": metadata.get("malformed_count", 0),
        "parse_refused": refused,
    }, groups


def _parse_display(path: Path, groups: dict[tuple[str, str], dict[str, Any]], issues: set[str]) -> dict[str, Any]:
    try:
        data, metadata = _read_snapshot(path, limit=MAX_FILE_BYTES, issues=issues)
    except _EvidenceError:
        return {"row_count": None, "rounding_mismatch_count": None, "malformed_count": None, "parse_refused": True}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _issue(issues, "display_invalid_utf8")
        return {"row_count": None, "rounding_mismatch_count": None, "malformed_count": None, "parse_refused": True}
    if not data:
        _issue(issues, "display_empty")
        return {"row_count": None, "rounding_mismatch_count": None, "malformed_count": None, "parse_refused": True}
    if not data.endswith(b"\n"):
        _issue(issues, "display_partial_line")
    lines = text.splitlines()
    header_indexes = [index for index, line in enumerate(lines) if line.strip() == _DISPLAY_HEADER]
    if len(header_indexes) != 1:
        _issue(issues, "display_schema_invalid")
        header_index = -2
        parse_start = 0
    else:
        header_index = header_indexes[0]
        parse_start = header_index + 2
    if header_index >= 0 and (header_index + 1 >= len(lines) or lines[header_index + 1].strip() != _DISPLAY_SEPARATOR):
        _issue(issues, "display_columns_invalid")
        parse_start = header_index + 1

    rows: list[tuple[str, str, str, str]] = []
    malformed = 0
    for line in lines[parse_start:]:
        if line.strip() in {_DISPLAY_HEADER, _DISPLAY_SEPARATOR}:
            continue
        if not line.strip():
            _issue(issues, "display_blank_line")
            malformed += 1
            continue
        if not line.lstrip().startswith("|"):
            _issue(issues, "display_non_table_content")
            malformed += 1
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 9:
            malformed += 1
            _issue(issues, "display_malformed_row")
            continue
        try:
            if not cells[0] or not _safe_identity(cells[1]) or not _safe_identity(cells[2]):
                raise InvalidOperation
            if not cells[3] or not cells[4] or not cells[5] or cells[6] not in {"deleted", "pending", "created", "active", "delete-failed", "deleted-key-cleanup-failed", "deleted-cost-bookkeeping-failed"}:
                raise InvalidOperation
            cost = cells[7].strip()
            if cost != "pending":
                if not cost.startswith("$"):
                    raise InvalidOperation
                _decimal(cost[1:])
            rows.append((cells[1], cells[2], cells[6], cost))
        except (InvalidOperation, IndexError, TypeError):
            malformed += 1
            _issue(issues, "display_malformed_row")

    display_keys = {(phase, instance) for phase, instance, _, _ in rows}
    if len(display_keys) != len(rows):
        _issue(issues, "duplicate_display_row")
        malformed += 1
    if display_keys != set(groups):
        _issue(issues, "display_incomplete_against_ledger")
        malformed += 1
    mismatch = 0
    for phase, instance, status, shown in rows:
        actual = groups.get((phase, instance), {}).get("actual_cost_usd")
        if (phase, instance) not in groups:
            _issue(issues, "display_orphan_row")
            continue
        if status == "pending":
            if actual is not None:
                _issue(issues, "display_status_mismatch")
                malformed += 1
            continue
        if actual is None:
            _issue(issues, "display_status_mismatch")
            malformed += 1
            continue
        try:
            if _decimal(shown[1:]) != _decimal(actual):
                mismatch += 1
        except (InvalidOperation, TypeError):
            malformed += 1
            _issue(issues, "display_cost_invalid")
    if mismatch:
        _issue(issues, "display_rounding_mismatch")
    if malformed:
        _issue(issues, "display_malformed_rows")
    display_parse_errors = {"display_partial_line", "display_invalid_utf8", "display_empty", "display_schema_invalid", "display_columns_invalid", "display_blank_line", "display_non_table_content", "display_malformed_row", "display_malformed_rows", "display_orphan_row", "display_incomplete_against_ledger", "duplicate_display_row", "display_status_mismatch", "display_cost_invalid"}
    if mismatch or malformed or display_parse_errors & issues:
        return {"row_count": None, "rounding_mismatch_count": None, "malformed_count": None, "parse_refused": True}
    return {"row_count": len(rows), "rounding_mismatch_count": mismatch, "malformed_count": malformed, "parse_refused": False}


def _enumerate_receipts(root_fd: int, issues: set[str]) -> dict[str, tuple[int, int, int, int, int, int]]:
    entries: dict[str, tuple[int, int, int, int, int, int]] = {}
    entry_bytes = 0
    with os.scandir(root_fd) as scan:
        for entry in scan:
            child_info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(child_info.st_mode):
                _issue(issues, "symlink_receipt")
                raise _EvidenceError("symlink_receipt")
            if not entry.name.endswith(".deletion-receipt.json"):
                continue
            name_bytes = len(entry.name.encode("utf-8"))
            entry_bytes += name_bytes
            if name_bytes > MAX_RECEIPT_NAME_BYTES or entry_bytes > MAX_RECEIPT_DIRECTORY_BYTES or len(entries) >= MAX_RECEIPTS:
                _issue(issues, "receipt_count_or_directory_limit")
                raise _EvidenceError("receipt_count_or_directory_limit")
            entries[entry.name] = _file_identity(child_info)
    return entries


def _receipt_files(root: Path, issues: set[str]) -> tuple[int | None, os.stat_result | None, list[tuple[str, tuple[int, int, int, int, int, int]]]]:
    root_fd: int | None = None
    try:
        _require_secure_capabilities(issues)
        root = _absolute_path(root)
        ancestors = _check_ancestors(root / "placeholder", issues)
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _issue(issues, "deletion_root_not_directory")
            return None, None, []
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        anchored = os.fstat(root_fd)
        if _file_identity(info) != _file_identity(anchored):
            _issue(issues, "evidence_mutated_during_read")
            raise _EvidenceError("evidence_mutated_during_read")
        _require_private_directory(anchored, issues, "deletion_root")
        entries = _enumerate_receipts(root_fd, issues)
        current = os.fstat(root_fd)
        if (anchored.st_dev, anchored.st_ino, anchored.st_mode, anchored.st_uid) != \
                (current.st_dev, current.st_ino, current.st_mode, current.st_uid):
            _issue(issues, "evidence_mutated_during_read")
            raise _EvidenceError("evidence_mutated_during_read")
        if not _ancestors_stable(root / "placeholder", ancestors, issues):
            raise _EvidenceError("ancestor_component_swap")
        return root_fd, anchored, sorted(entries.items())
    except (OSError, _EvidenceError):
        _issue(issues, "deletion_root_unavailable")
        if root_fd is not None:
            os.close(root_fd)
        return None, None, []


def _parse_receipts(root: Path, groups: dict[tuple[str, str], dict[str, Any]], issues: set[str]) -> dict[str, Any]:
    root_fd, root_before, receipts = _receipt_files(root, issues)
    refused = any(issue.startswith("deletion_root_") for issue in issues)
    matched: set[tuple[str, str]] = set()
    unmatched = absent_unmatched = deleted = absent = malformed = duplicate_count = 0
    seen_keys: set[tuple[str, str]] = set()
    absent_amount = ZERO_USD
    if root_fd is None:
        refused = True
    for name, expected_identity in receipts:
        try:
            assert root_fd is not None and root_before is not None
            row = _read_relative_snapshot(
                root_fd, root_before, name, limit=MAX_JSON_OBJECT_BYTES,
                issues=issues, expected=expected_identity,
            )
            value = _strict_object(row)
            if value.get("schema") != "local_bmo.shadeform.deletion-receipt.v1":
                raise _EvidenceError("receipt_schema_invalid")
            phase, instance = value.get("phase_id"), value.get("instance_id")
            deletion = value.get("deletion")
            if not _safe_identity(phase) or not _safe_identity(instance) or not isinstance(deletion, dict):
                raise _EvidenceError("receipt_identity_invalid")
            status = deletion.get("status")
            if status == "deleted":
                deleted += 1
            elif status == "absent":
                absent += 1
                try:
                    amount = _decimal(value.get("actual_cost_usd", 0))
                except InvalidOperation:
                    _issue(issues, "receipt_cost_invalid")
                    amount = ZERO_USD
                    refused = True
                refused = True
            else:
                raise _EvidenceError("receipt_status_invalid")
            key = (phase, instance)
            if key in seen_keys:
                duplicate_count += 1
                _issue(issues, "duplicate_deletion_receipt")
                refused = True
            seen_keys.add(key)
            if key not in groups:
                unmatched += 1
                if status == "absent":
                    absent_unmatched += 1
                    absent_amount += amount
            else:
                matched.add(key)
        except _EvidenceError as exc:
            malformed += 1
            _issue(issues, f"receipt_{str(exc)}")
            refused = True
    if root_fd is not None:
        try:
            # A second bounded directory-fd enumeration is the generation
            # check: additions, removals, and replacements invalidate the
            # entire receipt stream rather than leaving partial totals.
            second = _enumerate_receipts(root_fd, issues)
            first = dict(receipts)
            if second != first:
                _issue(issues, "receipt_directory_changed")
                refused = True
            root_after = os.fstat(root_fd)
            if _file_identity(root_before) != _file_identity(root_after):
                _issue(issues, "evidence_mutated_during_read")
                refused = True
        except OSError:
            _issue(issues, "deletion_root_unavailable")
            refused = True
        finally:
            os.close(root_fd)
    if unmatched:
        _issue(issues, "orphan_deletion_receipt")
        refused = True
    if refused:
        receipt_count = deleted_count = absent_count = unmatched_count = unmatched_absent_count = duplicate_count = malformed_count = None
        absent_value: str | None = None
    else:
        receipt_count = len(receipts)
        deleted_count = deleted
        absent_count = absent
        unmatched_count = unmatched
        unmatched_absent_count = absent_unmatched
        duplicate_count = duplicate_count
        malformed_count = malformed
        absent_value = _money(absent_amount if absent_unmatched else ZERO_USD)
    return {
        "receipt_count": receipt_count, "deleted_count": deleted_count, "absent_count": absent_count,
        "unmatched_count": unmatched_count, "unmatched_absent_count": unmatched_absent_count,
        "unmatched_absent_actual_cost_usd": absent_value,
        "duplicate_receipt_count": duplicate_count, "malformed_count": malformed_count,
        "parse_refused": refused,
    }


def _parse_incidents(path: Path, groups: dict[tuple[str, str], dict[str, Any]], issues: set[str]) -> dict[str, Any]:
    rows, metadata = _read_jsonl(path, label="incidents", issues=issues)
    required = {"phase_id", "incident"}
    allowed = required | {
        "instance_id", "nonce", "ownership_nonce", "ssh_key_id", "ssh_key_name",
        "ssh_public_key_sha256", "error_type", "retry_required",
    }
    unmatched_teardown = unmatched_other = duplicate_count = 0
    signatures: set[str] = set()
    for row in rows:
        if set(row) - allowed or not required.issubset(row):
            _issue(issues, "incident_schema_invalid")
            continue
        incident = row.get("incident")
        signature = _canonical_signature(row)
        if signature in signatures:
            duplicate_count += 1
            _issue(issues, "duplicate_incident")
        signatures.add(signature)
        phase, instance = row.get("phase_id"), row.get("instance_id")
        identity_values = (incident, phase) + ((instance,) if instance is not None else ())
        if not all(_safe_identity(value) for value in identity_values):
            _issue(issues, "incident_identity_invalid")
            continue
        for field in ("nonce", "ownership_nonce", "ssh_key_id", "ssh_key_name", "ssh_public_key_sha256", "error_type"):
            if field in row and not _safe_identity(row[field]):
                _issue(issues, "incident_scalar_invalid")
        if "retry_required" in row and not isinstance(row["retry_required"], bool):
            _issue(issues, "incident_scalar_invalid")
        key = (phase, instance)
        if instance is not None and key not in groups:
            if incident == "post-instance-teardown-unconfirmed":
                unmatched_teardown += 1
            else:
                unmatched_other += 1
    if unmatched_teardown:
        _issue(issues, "unmatched_teardown_incidents")
    if unmatched_other:
        _issue(issues, "unmatched_incidents")
    refused = bool(metadata.get("parse_refused", False)) or any(
        issue in {"incident_identity_invalid", "incident_scalar_invalid", "incident_schema_invalid", "duplicate_incident"} for issue in issues
    ) or bool(unmatched_teardown or unmatched_other)
    if refused:
        unmatched_teardown = unmatched_other = duplicate_count = None
    return {
        "line_count": metadata.get("line_count", 0), "unmatched_teardown_incident_count": unmatched_teardown,
        "unmatched_non_teardown_incident_count": unmatched_other,
        "duplicate_incident_count": duplicate_count,
        "malformed_count": None if refused else metadata.get("malformed_count", 0),
        "parse_refused": refused,
    }


def run_preflight(*, legacy_ledger: Path, display_ledger: Path, deletion_root: Path, incidents: Path, mode: str = "preflight") -> dict[str, Any]:
    issues: set[str] = set()
    if mode != "preflight":
        raise ValueError("mode must be preflight")
    legacy, groups = _parse_legacy(legacy_ledger, issues)
    display = _parse_display(display_ledger, groups, issues)
    receipts = _parse_receipts(deletion_root, groups, issues)
    incident_report = _parse_incidents(incidents, groups, issues)
    evidence_complete = not any(
        bool(section.get("parse_refused"))
        for section in (legacy, display, receipts, incident_report)
    )
    # These are review facts, never authority.  In particular no field here can
    # be passed to the v2 genesis initializer.
    return {
        "schema": SCHEMA, "preflight_only": True,
        "bookkeeping_is_not_spend_authorization": True,
        "safe_to_migrate_now": False,
        "evidence_complete": evidence_complete,
        "cross_stream_reconciliation_available": evidence_complete,
        "legacy_ledger": legacy, "display_ledger": display,
        "deletion_receipts": receipts, "incidents": incident_report,
        "path_safety": {
            "issues": sorted(issues), "symlink_or_hardlink_refused": any(
                issue in issues for issue in ("symlink_path", "hardlink_file", "symlink_receipt")
            ),
            "evidence_mutation_detected": "evidence_mutated_during_read" in issues,
        },
        "adjudication_required": True,
        "authoritative_prior_spend_choice_proposed": False,
        "zero_pending_genesis_proposed": False,
        "adjudication_input": {
            "schema": ADJUDICATION_SCHEMA, "purpose": "human_review_only",
            "executes_nothing": True, "genesis_emission_forbidden": True,
            "required_decisions": ["prior_spend", "pending_resolution", "owner_binding", "evidence_identity"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-ledger", type=Path, required=True)
    parser.add_argument("--display-ledger", type=Path, required=True)
    parser.add_argument("--deletion-root", type=Path, required=True)
    parser.add_argument("--incidents", type=Path, required=True)
    parser.add_argument("--mode", choices=("preflight",), default="preflight")
    args = parser.parse_args(argv)
    report = run_preflight(
        legacy_ledger=args.legacy_ledger, display_ledger=args.display_ledger,
        deletion_root=args.deletion_root, incidents=args.incidents, mode=args.mode,
    )
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
