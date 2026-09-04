"""Small, reusable adversarial checks for SEC-001.

These checks are policy assertions and fixtures. They do not claim that a
future native engine or host has passed the corresponding live test.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit


def require_loopback(bind: str) -> bool:
    return bind == "127.0.0.1"


def reject_path_escape(relative_path: str) -> bool:
    """Reject traversal, UNC/device paths, and alternate data streams."""

    value = relative_path.replace("\\", "/")
    path = PurePosixPath(value)
    parts = path.parts
    return not (
        not value
        or value.startswith("/")
        or value.startswith("//")
        or value.startswith("\\\\")
        or value.lower().startswith("//?/" )
        or ".." in parts
        or any(":" in part for part in parts)
    )


def bounded_json(raw: str | bytes, *, max_bytes: int = 2 * 1024 * 1024, max_depth: int = 32) -> Any:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > max_bytes:
        raise ValueError("request_too_large")
    value = json.loads(data)

    def visit(node: Any, depth: int) -> None:
        if depth > max_depth:
            raise ValueError("json_too_deep")
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError("json_key_invalid")
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(value, 0)
    return value


def validate_tool_envelope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "name", "arguments"}
        and isinstance(value["id"], str)
        and 1 <= len(value["id"]) <= 128
        and isinstance(value["name"], str)
        and isinstance(value["arguments"], dict)
    )


def safe_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"}:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)
    except ValueError:
        return False


def no_shell_string(command: Any) -> bool:
    """The process boundary accepts an argv list, never a shell command."""
    return isinstance(command, list) and bool(command) and all(isinstance(item, str) for item in command)
