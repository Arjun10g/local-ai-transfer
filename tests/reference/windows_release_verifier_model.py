"""Platform-neutral fault model for the inert Windows release verifier.

This module is test support, not a filesystem implementation or production
authority. It operates only on explicit in-memory metadata and byte fixtures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any


MAX_ENTRIES = 64
MAX_COMPONENTS = 16
MAX_COMPONENT_CHARS = 255
MAX_FILE_BYTES = 268_435_456
MAX_TREE_BYTES = 1_073_741_824
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MANIFEST_KEYS = {"schema", "release_manifest_schema", "package_kind", "activation", "model", "entries"}
MODEL_KEYS = {"included", "disposition"}
ENTRY_KEYS = {"path", "size_bytes", "sha256", "signer_policy", "signer_subject_sha256"}
TREE_KEYS = {"volume", "root", "objects", "second_objects"}
VOLUME_KEYS = {"kind", "filesystem", "hotplug", "network", "volume_token"}
ROOT_KEYS = {
    "file_id_before", "file_id_after", "volume_token", "private_current_user",
    "reparse", "delete_pending", "write_share_denied",
}
OBJECT_KEYS = {
    "path", "kind", "file_id_before", "file_id_after", "volume_token",
    "private_current_user", "reparse", "delete_pending", "write_share_denied",
    "link_count", "short_name", "streams", "content_base64", "signer_status",
    "signer_subject_sha256",
}
RECEIPT_KEYS = (
    "schema", "status", "reason", "manifest_sha256", "verified_files",
    "verified_directories", "verified_bytes", "root_identity_retained",
    "object_identities_retained", "signatures_verified", "model_external",
    "activated",
)


class ReferenceContractError(ValueError):
    """The synthetic contract input is malformed or unsafe."""


class TrackedTree:
    """In-memory tree that records whether verification accessed it."""

    def __init__(self, value: dict[str, Any]):
        self._value = value
        self.access_count = 0

    def snapshot(self) -> dict[str, Any]:
        self.access_count += 1
        return self._value


class HostileRequest:
    """Any property access proves public refusal ordering is wrong."""

    def __getattribute__(self, name: str):  # pragma: no cover - called only on failure
        raise AssertionError(f"request dereferenced: {name}")


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReferenceContractError(f"{label}_keys")
    return value


def _scalar_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ReferenceContractError(f"{label}_text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ReferenceContractError(f"{label}_unicode") from exc
    if any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value):
        raise ReferenceContractError(f"{label}_control")
    return value


def _components(path: Any) -> tuple[str, ...]:
    path = _scalar_text(path, "path", MAX_COMPONENTS * (MAX_COMPONENT_CHARS + 1))
    if path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        raise ReferenceContractError("unsafe_name")
    components = tuple(path.split("/"))
    if not 1 <= len(components) <= MAX_COMPONENTS:
        raise ReferenceContractError("unsafe_name")
    for component in components:
        _scalar_text(component, "component", MAX_COMPONENT_CHARS)
        stem = component.split(".", 1)[0].casefold()
        if (
            component in {".", ".."}
            or component.endswith((".", " "))
            or "~" in component
            or stem in RESERVED
            or any(character in '<>"|?*' for character in component)
        ):
            raise ReferenceContractError("unsafe_name")
    return components


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    manifest = _exact(manifest, MANIFEST_KEYS, "manifest")
    if (
        manifest["schema"] != "local_bmo.windows-release-manifest-identity.v0.1.0"
        or manifest["release_manifest_schema"] != "release-manifest.v1"
        or manifest["package_kind"] not in {"fixture-skeleton", "portable-release"}
        or manifest["activation"] is not False
    ):
        raise ReferenceContractError("manifest_identity")
    model = _exact(manifest["model"], MODEL_KEYS, "model")
    if model != {"included": False, "disposition": "EXTERNAL_AND_VERIFIED_SEPARATELY"}:
        raise ReferenceContractError("model_must_be_external")
    entries = manifest["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ReferenceContractError("manifest_entry_count")
    previous: str | None = None
    seen: set[str] = set()
    total = 0
    for index, raw in enumerate(entries):
        entry = _exact(raw, ENTRY_KEYS, f"entry_{index}")
        path = "/".join(_components(entry["path"]))
        folded = path.casefold()
        if folded in seen or (previous is not None and folded <= previous):
            raise ReferenceContractError("duplicate_or_case_colliding_manifest_path")
        seen.add(folded)
        previous = folded
        size = entry["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise ReferenceContractError("manifest_size")
        if total > MAX_TREE_BYTES - size:
            raise ReferenceContractError("manifest_total_size")
        total += size
        if not isinstance(entry["sha256"], str) or SHA256.fullmatch(entry["sha256"]) is None:
            raise ReferenceContractError("manifest_hash")
        extension = "." + path.rsplit(".", 1)[-1].casefold() if "." in path.rsplit("/", 1)[-1] else ""
        if extension == ".gguf":
            raise ReferenceContractError("model_must_be_external")
        signed_type = extension in {".exe", ".dll", ".ps1", ".py", ".js", ".mjs"}
        required_policy = "offline_authenticode_required"
        if signed_type:
            if entry["signer_policy"] != required_policy:
                raise ReferenceContractError("signature_policy")
            if not isinstance(entry["signer_subject_sha256"], str) or SHA256.fullmatch(entry["signer_subject_sha256"]) is None:
                raise ReferenceContractError("signature_identity")
        elif entry["signer_policy"] != "not_applicable" or entry["signer_subject_sha256"] is not None:
            raise ReferenceContractError("signature_policy")
    return entries


def _receipt(reason: str, manifest_digest: str, *, files: int = 0, directories: int = 0,
             size: int = 0, verified: bool = False) -> dict[str, Any]:
    value = {
        "schema": "local_bmo.windows-release-verification-receipt.v0.1.0",
        "status": "VERIFIED" if verified else ("REFUSED_NOT_ACTIVATED" if reason == "trust_anchor_unavailable" else "FAILED"),
        "reason": "none" if verified else reason,
        "manifest_sha256": manifest_digest if SHA256.fullmatch(manifest_digest or "") else "0" * 64,
        "verified_files": files,
        "verified_directories": directories,
        "verified_bytes": size,
        "root_identity_retained": verified,
        "object_identities_retained": verified,
        "signatures_verified": verified,
        "model_external": True,
        "activated": False,
    }
    if tuple(value) != RECEIPT_KEYS:
        raise AssertionError("receipt key order drift")
    return value


def canonical_receipt(receipt: dict[str, Any]) -> bytes:
    if tuple(receipt) != RECEIPT_KEYS or set(receipt) != set(RECEIPT_KEYS):
        raise ReferenceContractError("receipt_keys")
    return json.dumps(receipt, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def verify_public(_request: Any, _tree: TrackedTree, compiled_manifest_sha256: str) -> dict[str, Any]:
    """Model the immutable pre-access refusal; arguments are never inspected."""

    return _receipt("trust_anchor_unavailable", compiled_manifest_sha256)


def _derived_directories(paths: list[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    return directories


def _object_map(objects: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(objects, list) or len(objects) > MAX_ENTRIES * MAX_COMPONENTS:
        raise ReferenceContractError("inventory_mismatch")
    mapped: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    file_ids: set[tuple[str, str]] = set()
    for index, raw in enumerate(objects):
        item = _exact(raw, OBJECT_KEYS, f"object_{index}")
        path = "/".join(_components(item["path"]))
        key = path.casefold()
        if key in folded or path in mapped:
            raise ReferenceContractError("inventory_mismatch")
        folded.add(key)
        mapped[path] = item
        identity_key = (str(item["volume_token"]), str(item["file_id_before"]))
        if identity_key in file_ids:
            raise ReferenceContractError("identity_mismatch")
        file_ids.add(identity_key)
    return mapped


def _verify_tree_shape(tree: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    tree = _exact(tree, TREE_KEYS, "tree")
    volume = _exact(tree["volume"], VOLUME_KEYS, "volume")
    root = _exact(tree["root"], ROOT_KEYS, "root")
    first = _object_map(tree["objects"])
    second = _object_map(tree["second_objects"])
    return volume, root, first, second


def verify_reference(manifest: Any, tree: TrackedTree, compiled_manifest_sha256: str,
                     *, trusted_test_boundary: bool) -> dict[str, Any]:
    """Exercise the latent algorithm on synthetic metadata only.

    `trusted_test_boundary` cannot affect the public function. It exists solely
    so fault fixtures can evaluate the contract that a future Windows
    implementation must satisfy.
    """

    if not trusted_test_boundary:
        return verify_public(None, tree, compiled_manifest_sha256)
    try:
        entries = validate_manifest(manifest)
        actual_manifest_sha = manifest_sha256(manifest)
        if actual_manifest_sha != compiled_manifest_sha256:
            return _receipt("manifest_identity_mismatch", compiled_manifest_sha256)
        volume, root, first, second = _verify_tree_shape(tree.snapshot())
        if volume != {
            "kind": "fixed", "filesystem": "NTFS", "hotplug": False,
            "network": False, "volume_token": volume.get("volume_token"),
        } or not _scalar_text(volume["volume_token"], "volume_token", 64):
            return _receipt("unsafe_volume", compiled_manifest_sha256)
        root_ok = (
            root["file_id_before"] == root["file_id_after"]
            and root["volume_token"] == volume["volume_token"]
            and root["private_current_user"] is True
            and root["reparse"] is False
            and root["delete_pending"] is False
            and root["write_share_denied"] is True
        )
        if not root_ok:
            return _receipt("root_authority_invalid", compiled_manifest_sha256)
        paths = [entry["path"] for entry in entries]
        directories = _derived_directories(paths)
        expected = set(paths) | directories
        if set(first) != expected or set(second) != expected:
            return _receipt("inventory_mismatch", compiled_manifest_sha256)
        total = 0
        for path in sorted(expected):
            item = first[path]
            later = second[path]
            is_directory = path in directories
            if item["kind"] != ("directory" if is_directory else "file"):
                return _receipt("unsafe_object", compiled_manifest_sha256)
            if (
                item["file_id_before"] != item["file_id_after"]
                or item["file_id_after"] != later["file_id_before"]
                or later["file_id_before"] != later["file_id_after"]
                or item["volume_token"] != volume["volume_token"]
                or later["volume_token"] != volume["volume_token"]
            ):
                return _receipt("identity_mismatch", compiled_manifest_sha256)
            if (
                item["private_current_user"] is not True
                or item["reparse"] is not False
                or item["delete_pending"] is not False
                or item["write_share_denied"] is not True
                or item["short_name"] is not None
            ):
                return _receipt("unsafe_object", compiled_manifest_sha256)
            if is_directory:
                if item["link_count"] != 1 or item["streams"] != [] or item["content_base64"] is not None:
                    return _receipt("unsafe_object", compiled_manifest_sha256)
                continue
            entry = next(candidate for candidate in entries if candidate["path"] == path)
            if item["link_count"] != 1 or item["streams"] != ["::$DATA"]:
                return _receipt("unsafe_object", compiled_manifest_sha256)
            try:
                content = base64.b64decode(item["content_base64"], validate=True)
            except (TypeError, ValueError) as exc:
                raise ReferenceContractError("content_encoding") from exc
            if len(content) != entry["size_bytes"]:
                return _receipt("size_mismatch", compiled_manifest_sha256)
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                return _receipt("hash_mismatch", compiled_manifest_sha256)
            if entry["signer_policy"] == "offline_authenticode_required":
                if (
                    item["signer_status"] != "valid_offline_exact_handle"
                    or item["signer_subject_sha256"] != entry["signer_subject_sha256"]
                ):
                    return _receipt("signature_untrusted", compiled_manifest_sha256)
            elif item["signer_status"] != "not_applicable" or item["signer_subject_sha256"] is not None:
                return _receipt("signature_untrusted", compiled_manifest_sha256)
            total += len(content)
            if total > MAX_TREE_BYTES:
                return _receipt("size_mismatch", compiled_manifest_sha256)
        return _receipt("none", compiled_manifest_sha256, files=len(entries),
                        directories=len(directories), size=total, verified=True)
    except ReferenceContractError as exc:
        reason = str(exc)
        if reason in {"unsafe_name"}:
            mapped = "unsafe_name"
        elif "signature" in reason:
            mapped = "signature_untrusted"
        elif "manifest" in reason or "model" in reason or "duplicate_or_case" in reason:
            mapped = "manifest_identity_mismatch"
        elif "identity" in reason:
            mapped = "identity_mismatch"
        else:
            mapped = "inventory_mismatch"
        return _receipt(mapped, compiled_manifest_sha256)
