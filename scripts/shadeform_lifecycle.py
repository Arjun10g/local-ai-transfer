"""Strict, exact-resource Shadeform lifecycle primitives.

Ported from the NanoQuant orchestration lane. The ownership model is the part
worth keeping and is deliberately unchanged:

* **No account-wide instance-list operation exists in this module.** A run may
  inspect, update, or delete only the exact resource ID recorded in its phase
  ledger. There is no code path that can enumerate the account and act on
  whatever it finds.
* Every provider action is gated on a preflight that reads
  ``experiments/LEDGER.md`` and ``docs/90_operations/SHADEFORM_FAILURE_MODES.md``.
* Each creation attempt carries a 32-hex **ownership nonce** that binds the
  instance, its SSH key, and its host-key ledger to one attempt.

What this repo changed from the donor:

* the four path constants now point inside this repo, and the incident log is a
  first-class file in this lane rather than a pointer into a sibling project;
* ``SHADEFORM_MAX_TOTAL_COST_USD`` is treated as the **whole-project** budget
  (AGENTS.md 1.4), so candidate selection subtracts what the ledger already
  records and refuses to launch while any prior row's cost is unaccounted;
* a row that has reached ``deleted`` with a recorded cost is frozen: the ledger
  upsert will not overwrite it.
"""

from __future__ import annotations

import contextlib
import base64
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MUTATION_ENV_FILE = ROOT / ".secrets" / "shadeform.env"
# Ephemeral SSH material lives in the same protected, Git-ignored, owner-private
# area as the mutation environment projection (``b306665``).
#
# ``j1m_runner.validate_persisted_argv`` (``991b70e``) accepts the ``-i``
# operand of a persisted argv only when it is a *canonical private handle*:
# a basename matching ``_HANDLE_BASENAME``, an ordinary single-link file owned
# by this process at mode ``0600``, an owner-private direct parent, and no
# group/world-writable ancestor up to ``/``.  A system temporary directory
# satisfies none of that in practice -- on macOS ``/var`` is a symlink, and on
# Linux ``/tmp`` is only accepted as a *direct* parent -- and ``id_ed25519``
# matches no handle basename at all, so every ssh/scp argv the orchestrator
# builds was refused before it could spawn.  The per-run key directory is
# therefore created here, under a root the validator already trusts.
EPHEMERAL_KEY_ROOT = ROOT / ".secrets" / "j1m"
# Recognised by ``j1m_runner._HANDLE_BASENAME`` through its ``ssh[_-]?key``
# alternative.  ``ssh-keygen`` writes the public half as ``<name>.pub``.
EPHEMERAL_KEY_BASENAME = "ssh-key"
_EPHEMERAL_KEY_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
API_BASE = "https://api.shadeform.ai/v1"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
RUNTIME_ROOT = ROOT / "experiments" / "runtime"
MARKDOWN_LEDGER = ROOT / "experiments" / "LEDGER.md"
COST_LEDGER = ROOT / "experiments" / "runtime" / "cost-ledger.jsonl"
INCIDENTS = ROOT / "experiments" / "runtime" / "incidents.jsonl"
COST_EVENT_SCHEMA = "local_bmo.shadeform.cost-event.v2"
COST_LEDGER_PROGRAM = "local-bmo-shadeform"
COST_LEDGER_CURRENCY = "USD"
COST_LEDGER_GENESIS_CONFIRMATION = (
    "I_HAVE_REVIEWED_THE_COMPLETE_SHADEFORM_COST_BASELINE"
)
MAX_COST_LEDGER_BYTES = 1_048_576
MAX_COST_LEDGER_LINES = 4096
MAX_COST_EVENT_BYTES = 65_536
MAX_MARKDOWN_LEDGER_BYTES = 1_048_576
MAX_INCIDENT_CATALOG_BYTES = 1_048_576
MAX_INCIDENT_EVIDENCE_BYTES = 1_048_576
MAX_MUTATION_ENV_BYTES = 16 * 1024
MAX_MUTATION_ENV_LINES = 128
MAX_MUTATION_ENV_LINE_BYTES = 4096
MAX_MUTATION_ENV_VALUE_CHARS = 2048
OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
LINK_SUPPORTS_DIR_FD = os.link in os.supports_dir_fd
UNLINK_SUPPORTS_DIR_FD = os.unlink in os.supports_dir_fd
_SECURE_SUBPROCESS_ENV = MappingProxyType({
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
})
_SECURE_EXECUTABLES = {
    "ssh-keygen": ("/usr/bin/ssh-keygen", "/bin/ssh-keygen", "/usr/local/bin/ssh-keygen", "/opt/homebrew/bin/ssh-keygen"),
    "ssh-keyscan": ("/usr/bin/ssh-keyscan", "/bin/ssh-keyscan", "/usr/local/bin/ssh-keyscan", "/opt/homebrew/bin/ssh-keyscan"),
    "ssh": ("/usr/bin/ssh", "/bin/ssh", "/usr/local/bin/ssh", "/opt/homebrew/bin/ssh"),
    "scp": ("/usr/bin/scp", "/bin/scp", "/usr/local/bin/scp", "/opt/homebrew/bin/scp"),
}
COST_EVENT_FIELDS = frozenset({
    "schema", "instance_id", "phase_id", "ownership_nonce",
    "owner_binding_sha256", "status", "estimated_cost_usd",
    "actual_cost_usd", "recorded_at_utc", "reservation",
    "ssh_key_name", "ssh_public_key_sha256", "candidate", "ssh_key_id",
    "ssh_public_key_fingerprint", "intent_schema", "instance_create_intent",
    "instance_name", "hourly_usd", "backstop_hours", "create_started_at_utc",
    "provider_delete_deadline_utc",
})
COST_GENESIS_FIELDS = frozenset({
    "schema", "event_kind", "program", "currency", "budget_cap_usd",
    "prior_settled_spend_usd", "current_pending_owner_count",
    "display_ledger_sha256", "incidents_sha256", "recorded_at_utc",
})
# The donor's incident log lived in a sibling repository. Ours is in this lane,
# in this repository, because a preflight that depends on a file outside the
# checkout is a preflight that silently stops happening.
INCIDENT_LOG = ROOT / "docs" / "90_operations" / "SHADEFORM_FAILURE_MODES.md"

# This is intentionally narrower than the read-only preflight's catalogue
# policy.  Mutation callers may consume only keys used by the lifecycle and
# its reviewed external-tools gate.  In particular, a typo or a legacy key
# must not silently become part of the provider request.
MUTATION_ENV_KEYS = frozenset({
    "SHADEFORM_API_KEY",
    "SHADEFORM_SSH",
    "SHADEFORM_MAX_TOTAL_COST_USD",
    "SHADEFORM_GPU_TYPES",
    "SHADEFORM_CLOUD",
    "SHADEFORM_EXCLUDED_CLOUDS",
    "SHADEFORM_REGION",
    "SHADEFORM_GPU_COUNT",
    "SHADEFORM_MAX_HOURLY_COST_USD",
    "SHADEFORM_IMAGE",
    "SHADEFORM_AUTO_TERMINATE_HOURS",
    "SHADEFORM_QA_APPROVED_GPU",
    "SHADEFORM_QA_APPROVED_CLOUD",
    "SHADEFORM_QA_APPROVED_REGION",
    "SHADEFORM_QA_APPROVED_INSTANCE_TYPE",
    "SOL_SHADEFORM_REVIEWED",
    "SOL_REMOTE_EXTERNAL_TOOLS_REVIEWED",
})

RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
PHASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
LEGACY_DELETION_EVIDENCE_SUFFIXES = (
    "deletion-intent.json", "deletion-confirmation.json", "deletion-receipt.json",
)

LEDGER_HEADER = "| date | phase | instance id | gpu | $/hr | purpose | status | cost logged | idle min |"
# Statuses after which a ledger row is history and may not be rewritten.
TERMINAL_STATUSES = frozenset({"deleted", "deleted-key-cleanup-failed", "deleted-cost-bookkeeping-failed"})
LEDGER_LIFECYCLE_STATUSES = frozenset({"created", "active", "delete-failed"}) | TERMINAL_STATUSES


class ShadeformError(RuntimeError):
    """Provider or lifecycle policy failure."""


class ShadeformHTTPError(ShadeformError):
    """HTTP error with a stable status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Shadeform HTTP {status}: {message}")
        self.status = status


class AmbiguousProviderOutcome(ShadeformError):
    """A create POST may have reached the provider but its result is unknown."""


class MalformedProviderResponse(ShadeformError):
    """A successful provider response could not be interpreted as its schema."""


#: A backstop must exceed the run by this factor. Equality is not enough: at
#: runtime == ceiling the backstop coincides with the cap and provisioning
#: decides the race, which is how EP-014 read at its boundary.
MIN_BACKSTOP_MARGIN = 1.10


class BackstopError(ShadeformError):
    """The provider backstop would fire before the run could finish.

    Kept distinct from BudgetError because the remedy is the opposite: a budget
    error means stop, this means the operator's own safety ceiling is shorter
    than the work and one of the two has to move, deliberately.
    """


class BudgetError(ShadeformError):
    """The reviewed authoritative JSON project budget forbids this action."""


@dataclass(frozen=True)
class Candidate:
    gpu: str
    cloud: str
    region: str
    instance_type: str
    hourly_usd: float
    vram_gb: int
    os_image: str
    interruptible: bool


@dataclass
class OwnedResource:
    phase_id: str
    run_id: str
    instance_id: str
    ownership_nonce: str
    ssh_key_id: str
    ssh_key_name: str
    gpu: str
    cloud: str
    region: str
    hourly_usd: float
    created_at_utc: str
    status: str = "created"
    cost_usd: float | None = None
    idle_minutes: float = 0.0
    # Observability, not lifecycle. A reader of this file has to be able to
    # answer "is anything driving this, and by when must it be over?" without
    # running ps and knowing what to look for. Inferring abandonment from a
    # quiet session nearly had two actors racing each other's teardown.
    launcher_pid: int | None = None
    provider_status: str | None = None
    active_deadline_utc: str | None = None
    run_deadline_utc: str | None = None
    instance_type: str | None = None
    gpu_count: int | None = None
    vram_gb: int | None = None
    os_image: str | None = None
    ssh_public_key: str | None = None
    launcher_start_marker: str | None = None
    ssh_public_key_fingerprint: str | None = None
    # Recovery-only records may not retain the original run ID.  Preserve the
    # exact provider name independently so a watchdog can still perform the
    # same full-profile proof as the launcher.
    instance_name: str | None = None
    # Exact provider-side auto-delete threshold committed before creation.
    # A late 404 is conservatively billed through this timestamp, never the
    # retry wall clock and never only the DELETE polling window.
    provider_delete_deadline_utc: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _verified_executable(name: str) -> str:
    """Resolve one fixed, owner-safe helper without consulting ambient PATH."""

    candidates = _SECURE_EXECUTABLES.get(name)
    if candidates is None or os.name != "posix":
        raise ShadeformError("required local helper is unavailable")
    for candidate in candidates:
        try:
            resolved = Path(os.path.realpath(candidate))
            ancestors = _env_ancestor_snapshot(resolved.parent)
            if not _env_ancestors_stable(ancestors):
                continue
            info = os.stat(resolved, follow_symlinks=False)
        except (OSError, ShadeformError):
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid in {0, os.getuid()}
            and not (stat.S_IMODE(info.st_mode) & 0o022)
            and stat.S_IMODE(info.st_mode) & 0o111
        ):
            return str(resolved)
    raise ShadeformError("required local helper is unavailable")


def _secure_subprocess_env() -> dict[str, str]:
    """Return a fresh, exact child environment from immutable source data."""

    return dict(_SECURE_SUBPROCESS_ENV)


def _verified_python_executable() -> str:
    """Return the current interpreter after a complete private-path check.

    Mutation children must run the interpreter that launched this process.  A
    fallback interpreter would silently change the executable identity when
    the configured one is unsafe, so an unsafe or changing path is a hard
    refusal.  The final file identity is checked twice; callers invoke this
    immediately while constructing each ``Popen`` argument vector.
    """

    if os.name != "posix":
        raise ShadeformError("required local helper is unavailable")
    try:
        candidate = Path(os.path.abspath(os.fspath(sys.executable)))
        if stat.S_ISLNK(os.lstat(candidate).st_mode):
            raise ShadeformError("required local helper is unavailable")
        ancestors = _env_ancestor_snapshot(candidate.parent)
        first = os.stat(candidate, follow_symlinks=False)
        safe = (
            stat.S_ISREG(first.st_mode)
            and first.st_nlink == 1
            and first.st_uid in {0, os.getuid()}
            and not (stat.S_IMODE(first.st_mode) & 0o022)
            and stat.S_IMODE(first.st_mode) & 0o111
        )
        second = os.stat(candidate, follow_symlinks=False)
    except (OSError, TypeError, ValueError, ShadeformError) as exc:
        raise ShadeformError("required local helper is unavailable") from exc
    if not safe or not _env_ancestors_stable(ancestors) or (
        first.st_dev, first.st_ino, first.st_mode, first.st_uid, first.st_nlink
    ) != (
        second.st_dev, second.st_ino, second.st_mode, second.st_uid, second.st_nlink
    ):
        raise ShadeformError("required local helper is unavailable")
    return str(candidate)


def load_env(path: Path) -> dict[str, str]:
    """Load an owner-private, descriptor-bound mutation environment.

    Read-only catalogue planning intentionally uses the separate, less
    privileged ``readonly_preflight.parse_env`` parser.  Every provider
    mutation/recovery caller in this module family comes through this stricter
    loader: the file and its canonical parent are opened by descriptor, the
    file identity is checked before and after the bounded read, and the
    assignment grammar is exact.  No value is included in an exception.
    """

    try:
        requested = Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError, OSError) as exc:
        raise ShadeformError("environment file path is invalid") from exc
    parent_descriptor = _open_private_canonical_parent(
        requested, label="environment file",
    )
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_SH)
        try:
            descriptor = os.open(
                requested.name, os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError as exc:
            raise ShadeformError("environment file is unavailable") from exc
        except OSError as exc:
            raise ShadeformError("environment file no-follow open failed") from exc
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        raw = _read_private_file_at(
            descriptor,
            parent_descriptor=parent_descriptor,
            path=requested,
            limit=MAX_MUTATION_ENV_BYTES,
            label="environment file",
        )
        return _parse_mutation_env(raw)
    except ShadeformError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ShadeformError("environment file is invalid") from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def _parse_mutation_env(raw: bytes) -> dict[str, str]:
    """Parse the bounded canonical dotenv grammar used by mutation paths."""

    if not isinstance(raw, bytes) or not raw or not raw.endswith(b"\n"):
        raise ShadeformError("environment file must be a complete bounded text file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShadeformError("environment file encoding is invalid") from exc
    if "\r" in text or "\x00" in text:
        raise ShadeformError("environment file contains invalid control data")
    lines = text.split("\n")[:-1]
    if len(lines) > MAX_MUTATION_ENV_LINES:
        raise ShadeformError("environment file has too many lines")
    values: dict[str, str] = {}
    for line in lines:
        if len(line.encode("utf-8")) > MAX_MUTATION_ENV_LINE_BYTES:
            raise ShadeformError("environment file line exceeds its bound")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ShadeformError("environment file assignment is malformed")
        key, value = line.split("=", 1)
        if (
            key not in MUTATION_ENV_KEYS
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None
            or key in values
        ):
            raise ShadeformError("environment file key is unknown or duplicated")
        values[key] = _parse_mutation_env_value(value)
    return values


def _parse_mutation_env_value(value: str) -> str:
    """Normalize one bounded value without retaining its source spelling."""

    if len(value) > MAX_MUTATION_ENV_VALUE_CHARS:
        raise ShadeformError("environment file value exceeds its bound")
    if value != value.strip():
        raise ShadeformError("environment file value has noncanonical whitespace")
    if len(value) >= 2 and value[0] in {"'", '"'}:
        if value[-1] != value[0] or len(value) == 2:
            raise ShadeformError("environment file quoting is malformed")
        value = value[1:-1]
        if value[0].isspace() or value[-1].isspace():
            raise ShadeformError("environment file quoted value has noncanonical whitespace")
    elif value[:1] in {"'", '"'} or value[-1:] in {"'", '"'}:
        raise ShadeformError("environment file quoting is malformed")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ShadeformError("environment file value contains control data")
    return value


def _env_ancestor_snapshot(parent: Path) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    """Snapshot non-writable directory ancestors for a bounded env transfer."""

    snapshot: list[tuple[Path, tuple[int, ...]]] = []
    cursor = parent
    current_uid = os.getuid()
    while True:
        try:
            info = os.stat(cursor, follow_symlinks=False)
        except OSError as exc:
            raise ShadeformError("environment directory identity is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {current_uid, 0}
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_nlink < 1
        ):
            raise ShadeformError("environment directory ancestor is unsafe")
        snapshot.append((
            cursor,
            (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink),
        ))
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return tuple(snapshot)


def _env_ancestors_stable(
    snapshot: tuple[tuple[Path, tuple[int, ...]], ...],
    *, allow_direct_parent_nlink_increment: bool = False,
) -> bool:
    for index, (path, expected) in enumerate(snapshot):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        observed = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
        if allow_direct_parent_nlink_increment and index == 0:
            # Some filesystems count regular children in a directory's link
            # count.  Publication legitimately adds one such child; identity,
            # owner, and mode remain exact and a larger unexpected change is
            # still refused.
            if observed[:4] != expected[:4] or observed[4] not in {expected[4], expected[4] + 1}:
                return False
        elif expected != observed:
            return False
    return True


def _open_env_parent_bound(
    path: Path, *, label: str, require_private: bool,
) -> tuple[int, tuple[tuple[Path, tuple[int, ...]], ...]]:
    """Bind ancestor identities before and after opening a migration parent."""

    before = _env_ancestor_snapshot(path.parent)
    descriptor = _open_private_canonical_parent(
        path, label=label, require_private=require_private,
    )
    if not _env_ancestors_stable(before):
        os.close(descriptor)
        raise ShadeformError(f"{label} directory changed before child open")
    return descriptor, before


def _read_mixed_env_source(path: Path) -> bytes:
    """Read a donor dotenv through a bound file and non-writable ancestors."""

    try:
        requested = Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError, OSError) as exc:
        raise ShadeformError("donor environment path is invalid") from exc
    parent_descriptor, ancestors = _open_env_parent_bound(
        requested, label="donor environment", require_private=False,
    )
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_SH)
        try:
            descriptor = os.open(
                requested.name, os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except (FileNotFoundError, OSError) as exc:
            raise ShadeformError("donor environment is unavailable") from exc
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        raw = _read_private_file_at(
            descriptor,
            parent_descriptor=parent_descriptor,
            path=requested,
            limit=MAX_MUTATION_ENV_BYTES,
            label="donor environment",
            require_private_parent=False,
        )
        if not _env_ancestors_stable(ancestors):
            raise ShadeformError("donor environment directory changed during read")
        return raw
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def _parse_mixed_env_source(raw: bytes) -> dict[str, str]:
    """Select only Local BMO mutation keys from a bounded mixed dotenv."""

    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > MAX_MUTATION_ENV_BYTES
        or not raw.endswith(b"\n")
    ):
        raise ShadeformError("donor environment must be a complete bounded text file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShadeformError("donor environment encoding is invalid") from exc
    if "\r" in text or "\x00" in text:
        raise ShadeformError("donor environment contains invalid control data")
    lines = text.split("\n")[:-1]
    if len(lines) > MAX_MUTATION_ENV_LINES:
        raise ShadeformError("donor environment has too many lines")
    selected: dict[str, str] = {}
    for line in lines:
        if len(line.encode("utf-8")) > MAX_MUTATION_ENV_LINE_BYTES:
            raise ShadeformError("donor environment line exceeds its bound")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ShadeformError("donor environment assignment is malformed")
        raw_key, value = line.split("=", 1)
        key = raw_key.strip(" \t")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ShadeformError("donor environment key is malformed")
        if key in MUTATION_ENV_KEYS:
            # Projection values keep the strict canonical grammar used by
            # mutation callers.  In particular, whitespace must not turn a
            # near-match into an allowlisted key.  Surrounding horizontal
            # whitespace is tolerated only for unrelated donor keys, whose
            # values are ignored and never enter the projected environment.
            if raw_key != key:
                raise ShadeformError(
                    "donor environment selected key has noncanonical whitespace"
                )
            if key in selected:
                raise ShadeformError("donor environment selected key is duplicated")
            selected[key] = _parse_mutation_env_value(value)
        else:
            # Unrelated donor values are deliberately opaque.  Their bytes
            # have already passed the bounded file/line and global UTF-8,
            # newline, NUL, and carriage-return checks; donor-specific value
            # syntax must not become Local BMO parsing or storage.
            continue
    if not {"SHADEFORM_API_KEY", "SHADEFORM_SSH"} <= selected.keys():
        raise ShadeformError("donor environment lacks required mutation keys")
    return selected


def _publish_projected_env(
    path: Path, payload: bytes,
) -> None:
    """Publish projected dotenv bytes relative to one owner-private dirfd.

    The no-overwrite hard link is the publication linearization point.  The
    bound ancestor snapshot is checked immediately before that link; after
    publication, later ancestor churn is not converted into an unsafe cleanup
    attempt and the published inode is never rolled back by pathname.
    """

    if not OPEN_SUPPORTS_DIR_FD or not STAT_SUPPORTS_DIR_FD or not UNLINK_SUPPORTS_DIR_FD or not LINK_SUPPORTS_DIR_FD:
        raise ShadeformError("environment publication requires descriptor-relative primitives")
    parent_descriptor, ancestors = _open_env_parent_bound(
        path, label="projected environment", require_private=True,
    )
    descriptor = -1
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    published = False
    published_identity: tuple[int, int, int, int, int, int] | None = None

    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        try:
            existing = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            existing = -1
        except OSError as exc:
            raise ShadeformError("projected environment destination is unsafe") from exc
        if existing >= 0:
            try:
                _require_private_file_identity(
                    existing, parent_descriptor=parent_descriptor, path=path,
                    label="projected environment",
                )
            finally:
                os.close(existing)
            raise ShadeformError("projected environment destination already exists")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("projected environment write made no progress")
            written += count
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != len(payload)
        ):
            raise ShadeformError("projected environment temporary file is unsafe")
        # Keep the temporary descriptor open through publication so its exact
        # inode is retained as the only identity that may be accepted below.
        published_identity = (
            info.st_dev, info.st_ino, info.st_mode,
            info.st_uid, info.st_nlink, info.st_size,
        )
        if not _env_ancestors_stable(
            ancestors, allow_direct_parent_nlink_increment=True,
        ):
            raise ShadeformError("projected environment directory changed before publication")
        try:
            os.link(
                temporary_name, path.name,
                src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ShadeformError("projected environment destination appeared during publication") from exc
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        published = True
        os.fsync(parent_descriptor)
        verification = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_descriptor,
        )
        try:
            identity = os.fstat(verification)
            observed_identity = (
                identity.st_dev, identity.st_ino, identity.st_mode,
                identity.st_uid, identity.st_nlink, identity.st_size,
            )
            if observed_identity != published_identity:
                raise ShadeformError("projected environment identity changed after publication")
            if _read_private_file_at(
                verification,
                parent_descriptor=parent_descriptor,
                path=path,
                limit=MAX_MUTATION_ENV_BYTES,
                label="projected environment",
            ) != payload:
                raise ShadeformError("projected environment verification failed")
        finally:
            os.close(verification)
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def project_mutation_env(
    source: Path, destination: Path,
) -> dict[str, object]:
    """Project selected donor assignments into a new owner-private dotenv.

    This is a one-way, offline migration helper.  It never invokes a provider,
    reads the process environment, or returns assignment values.
    """

    source_path = Path(os.path.abspath(os.fspath(source)))
    destination_path = Path(os.path.abspath(os.fspath(destination)))
    if source_path == destination_path:
        raise ShadeformError("donor and projected environment must differ")
    selected = _parse_mixed_env_source(_read_mixed_env_source(source_path))
    payload = (
        "".join(f"{key}={selected[key]}\n" for key in sorted(selected))
    ).encode("utf-8")
    if len(payload) > MAX_MUTATION_ENV_BYTES:
        raise ShadeformError("projected environment exceeds its byte bound")
    _publish_projected_env(destination_path, payload)
    return {
        "status": "published",
        "selected_key_count": len(selected),
        "selected_keys": sorted(selected),
    }


def require_env(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ShadeformError(f"{name} is required in the configured env file")
    return value


def validate_phase_id(value: str) -> str:
    if PHASE_ID.fullmatch(value) is None:
        raise ValueError("phase id must contain only lowercase letters, digits, and hyphens")
    return value


def validate_resource_id(value: object, *, field: str = "resource id") -> str:
    if not isinstance(value, str) or RESOURCE_ID.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def validate_ssh_user(value: object) -> str:
    """Validate the provider-selected Unix account before argv interpolation."""

    if not isinstance(value, str) or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value) is None:
        raise ShadeformError("provider returned an unsafe SSH username")
    return value


def validate_nonce(value: str) -> str:
    if NONCE.fullmatch(value) is None:
        raise ValueError("ownership nonce must be 32 lowercase hexadecimal characters")
    return value


def new_ownership_nonce() -> str:
    """Generate the nonce binding one creation attempt to its owner."""

    return secrets.token_hex(16)


def runtime_ledger_path(phase_id: str) -> Path:
    return RUNTIME_ROOT / f"{validate_phase_id(phase_id)}.json"


def legacy_deletion_evidence_paths(phase_id: str) -> tuple[Path, ...]:
    """Return phase-only deletion artifacts that cannot authorize a new owner."""

    phase = validate_phase_id(phase_id)
    return tuple(RUNTIME_ROOT / f"{phase}.{suffix}" for suffix in LEGACY_DELETION_EVIDENCE_SUFFIXES)


def preflight_legacy_deletion_evidence(phase_id: str) -> None:
    """Fail closed before any paid/provider action when legacy evidence exists.

    These phase-only artifacts predate the owner-bound deletion index and are
    not safe to associate with a new nonce.  This is deliberately read-only:
    it performs only bounded path validation and ``lstat`` calls, never opens,
    parses, removes, or mutates an evidence file.
    """

    for path in legacy_deletion_evidence_paths(phase_id):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ShadeformError("legacy deletion evidence requires manual recovery") from exc
        raise ShadeformError("legacy deletion evidence requires manual recovery")


def process_start_marker(pid: int | None) -> str | None:
    """Return Linux process start ticks, preventing PID reuse when available."""

    if pid is None or pid <= 0:
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        after_comm = stat_text.rsplit(")", 1)[1].split()
        return after_comm[19]  # field 22 (starttime), after pid/comm fields
    except (OSError, IndexError):
        return None


@contextmanager
def phase_cleanup_lock(phase_id: str):
    """Serialize teardown through one private, identity-bound lock handle."""

    path = RUNTIME_ROOT / f"{validate_phase_id(phase_id)}.cleanup.lock"
    _ensure_durable_directory(path.parent)
    parent_descriptor = _open_private_canonical_parent(
        path, label="phase cleanup lock",
    )
    descriptor = -1
    created = False
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(parent_descriptor)
        _require_private_file_identity(
            descriptor, parent_descriptor=parent_descriptor, path=path,
            label="phase cleanup lock", maximum_size=0,
        )
        try:
            yield
        finally:
            # A path replacement while the critical section runs is not a
            # successful lock acquisition.  The descriptor remains locked
            # until after this final identity check.
            _require_private_file_identity(
                descriptor, parent_descriptor=parent_descriptor, path=path,
                label="phase cleanup lock", maximum_size=0,
            )
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        raise ShadeformError("phase cleanup lock is unavailable or unsafe") from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        os.close(parent_descriptor)


def read_owned_resource(phase_id: str) -> OwnedResource | None:
    path = runtime_ledger_path(phase_id)
    try:
        payload = strict_json_object(
            private_bounded_stable_bytes(
                path, 65_536, label="phase ownership ledger",
            ),
            label="phase ownership ledger",
        )
        if set(payload) != set(OwnedResource.__dataclass_fields__):
            raise ValueError("phase ownership ledger has unknown or missing fields")
        record = OwnedResource(**payload)
        _validate_owned_resource(record)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
        raise ShadeformError(f"malformed phase ledger: {path}") from exc
    if record.phase_id != validate_phase_id(phase_id):
        raise ShadeformError("phase ledger is bound to a different phase")
    return record


def _validate_owned_resource(record: OwnedResource) -> None:
    """Validate every field used by exact cleanup before returning a record."""

    validate_phase_id(record.phase_id)
    for value, field, limit in (
        (record.run_id, "run id", 128),
        (record.ssh_key_name, "SSH key name", 256),
        (record.gpu, "GPU", 128),
        (record.cloud, "cloud", 128),
        (record.region, "region", 128),
    ):
        if (not isinstance(value, str) or not value or len(value) > limit or
                any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)):
            raise ValueError(f"{field} is invalid")
    validate_resource_id(record.instance_id, field="ledger instance id")
    validate_resource_id(record.ssh_key_id, field="ledger SSH key id")
    validate_nonce(record.ownership_nonce)
    if (isinstance(record.hourly_usd, bool) or not isinstance(record.hourly_usd, (int, float)) or
            not math.isfinite(float(record.hourly_usd)) or record.hourly_usd <= 0):
        raise ValueError("ledger hourly_usd must be finite and positive")
    if not isinstance(record.created_at_utc, str) or len(record.created_at_utc) > 64:
        raise ValueError("ledger created_at_utc is invalid")
    created = datetime.fromisoformat(record.created_at_utc)
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("ledger created_at_utc must be timezone-aware")
    if record.status not in LEDGER_LIFECYCLE_STATUSES:
        raise ValueError("ledger status is invalid")
    if record.cost_usd is not None and (not _valid_cost(record.cost_usd)):
        raise ValueError("ledger cost_usd is invalid")
    if (isinstance(record.idle_minutes, bool) or not isinstance(record.idle_minutes, (int, float)) or
            not math.isfinite(float(record.idle_minutes)) or record.idle_minutes < 0):
        raise ValueError("ledger idle_minutes is invalid")
    if record.launcher_pid is not None and (isinstance(record.launcher_pid, bool) or
            not isinstance(record.launcher_pid, int) or record.launcher_pid < 0):
        raise ValueError("ledger launcher_pid is invalid")
    for value, field in ((record.provider_status, "provider status"),
                         (record.instance_type, "instance type"),
                         (record.launcher_start_marker, "launcher start marker"),
                         (record.instance_name, "instance name")):
        if value is not None and (not isinstance(value, str) or len(value) > 256 or
                                   any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)):
            raise ValueError(f"ledger {field} is invalid")
    for value, field in ((record.active_deadline_utc, "active deadline"),
                         (record.run_deadline_utc, "run deadline"),
                         (record.provider_delete_deadline_utc, "provider delete deadline")):
        if value is not None:
            if not isinstance(value, str) or len(value) > 64:
                raise ValueError(f"ledger {field} is invalid")
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"ledger {field} must be timezone-aware")
    if (record.ssh_public_key_fingerprint is not None and
            (not isinstance(record.ssh_public_key_fingerprint, str) or
             re.fullmatch(r"[A-Za-z0-9+/]{43}", record.ssh_public_key_fingerprint) is None)):
        raise ValueError("malformed ledger SSH public-key fingerprint")
    if record.gpu_count is not None and (
        isinstance(record.gpu_count, bool) or not isinstance(record.gpu_count, int)
        or not 1 <= record.gpu_count <= 16
    ):
        raise ValueError("ledger gpu_count is invalid")
    if record.vram_gb is not None and (
        isinstance(record.vram_gb, bool) or not isinstance(record.vram_gb, int)
        or not 1 <= record.vram_gb <= 4096
    ):
        raise ValueError("ledger vram_gb is invalid")
    if record.os_image is not None and (
        not isinstance(record.os_image, str) or not 1 <= len(record.os_image) <= 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in record.os_image)
    ):
        raise ValueError("ledger os_image is invalid")
    if record.ssh_public_key is not None:
        algorithm, material = _canonical_public_key(record.ssh_public_key)
        if record.ssh_public_key_fingerprint is not None and ssh_public_key_fingerprint(record.ssh_public_key) != record.ssh_public_key_fingerprint:
            raise ValueError("ledger SSH public-key fingerprint does not match key material")


def _fsync_directory(path: Path) -> None:
    """Durably flush a directory entry after replace/unlink operations."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # Windows does not expose a portable directory-fsync primitive. The
        # caller still has file fsync and atomic replace semantics; production
        # Windows acceptance must provide an equivalent platform barrier.
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_durable_directory(path: Path) -> None:
    """Create a directory hierarchy and fsync every newly linked entry."""

    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if not cursor.is_dir() or cursor.is_symlink():
        raise OSError("durable directory ancestor is not a real directory")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not directory.is_dir() or directory.is_symlink():
            raise OSError("durable directory path changed during creation")
        # Flush the directory itself and, critically, the parent entry that
        # names a newly-created directory.  Fsyncing only the new directory is
        # insufficient after power loss on POSIX filesystems.
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def bounded_stable_bytes(path: Path, limit: int, *, label: str) -> bytes:
    """Read one bounded regular-file descriptor and reject path replacement."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("bounded read limit must be positive")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ShadeformError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ShadeformError(f"{label} is not bounded regular data")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ShadeformError(f"{label} exceeds its byte bound")
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or after.st_nlink != 1
            or current.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or (after.st_dev, after.st_ino, after.st_size)
            != (current.st_dev, current.st_ino, current.st_size)
            or total != after.st_size
        ):
            raise ShadeformError(f"{label} changed during bounded read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def required_bounded_text(path: Path, limit: int, *, label: str) -> str:
    """Read required UTF-8 policy/display data through the stable-file guard."""

    try:
        data = bounded_stable_bytes(path, limit, label=label)
    except FileNotFoundError as exc:
        raise ShadeformError(f"{label} is unavailable") from exc
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ShadeformError(f"{label} is not valid UTF-8") from exc


def strict_json_object(data: bytes, *, label: str) -> dict[str, Any]:
    """Decode strict UTF-8 JSON while rejecting duplicate/non-finite values."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ShadeformError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ShadeformError(f"{label} contains a non-finite number")
        return parsed

    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_float=finite_float,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ShadeformError(f"{label} contains a non-finite number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ShadeformError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ShadeformError(f"{label} is not an object")
    return payload


def _durable_atomic_write(path: Path, payload: bytes) -> None:
    """Write bounded bytes with file and parent-directory durability."""

    if not isinstance(payload, bytes):
        raise TypeError("durable atomic payload must be bytes")
    _ensure_durable_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
        if bounded_stable_bytes(path, max(1, len(payload)), label="durable atomic output") != payload:
            raise OSError("durable atomic output verification mismatch")
    finally:
        with contextlib.suppress(FileNotFoundError):
            durable_unlink(Path(temporary_name))


def durable_create_new(path: Path, payload: bytes) -> None:
    """Create one new regular file and durably publish its directory entry."""

    if not isinstance(payload, bytes):
        raise TypeError("durable create payload must be bytes")
    _ensure_durable_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        if bounded_stable_bytes(path, max(1, len(payload)), label="durable create output") != payload:
            raise OSError("durable create output verification mismatch")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def durable_unlink(path: Path) -> None:
    """Remove one file and durably flush the containing directory entry."""

    path.unlink()
    _fsync_directory(path.parent)


def private_durable_atomic_write(
    path: Path, payload: bytes, *, label: str,
) -> None:
    """Replace authoritative runtime data through one private parent handle."""

    if not isinstance(payload, bytes):
        raise TypeError("private durable atomic payload must be bytes")
    _ensure_durable_directory(path.parent)
    parent_descriptor = _open_private_canonical_parent(path, label=label)
    descriptor = -1
    verification_descriptor = -1
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    published = False
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        # Never replace an unsafe pre-existing path.  A normal update may
        # replace only another exact private regular file in this directory.
        try:
            existing_descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            existing_descriptor = -1
        except OSError as exc:
            raise ShadeformError(f"{label} existing path is unsafe") from exc
        if existing_descriptor >= 0:
            try:
                _require_private_file_identity(
                    existing_descriptor, parent_descriptor=parent_descriptor,
                    path=path, label=label,
                )
            finally:
                os.close(existing_descriptor)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("private durable atomic write made no progress")
            written += count
        os.fsync(descriptor)
        temporary_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_nlink != 1
            or temporary_stat.st_uid != os.getuid()
            or stat.S_IMODE(temporary_stat.st_mode) != 0o600
            or temporary_stat.st_size != len(payload)
        ):
            raise ShadeformError(f"{label} temporary output is unsafe")
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name, path.name,
            src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
        )
        published = True
        os.fsync(parent_descriptor)
        verification_descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        if _read_private_file_at(
            verification_descriptor, parent_descriptor=parent_descriptor,
            path=path, limit=max(1, len(payload)), label=label,
        ) != payload:
            raise ShadeformError(f"{label} verification mismatch")
    finally:
        if verification_descriptor >= 0:
            os.close(verification_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def private_durable_create_new(
    path: Path, payload: bytes, *, label: str,
) -> None:
    """Create immutable authoritative runtime data through a private dirfd."""

    if not isinstance(payload, bytes):
        raise TypeError("private durable create payload must be bytes")
    _ensure_durable_directory(path.parent)
    parent_descriptor = _open_private_canonical_parent(path, label=label)
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        descriptor = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("private durable create made no progress")
            written += count
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        if _read_private_file_at(
            descriptor, parent_descriptor=parent_descriptor, path=path,
            limit=max(1, len(payload)), label=label,
        ) != payload:
            raise ShadeformError(f"{label} verification mismatch")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def private_durable_unlink(path: Path, *, label: str) -> None:
    """Unlink exactly the private authoritative file opened under its dirfd."""

    parent_descriptor = _open_private_canonical_parent(path, label=label)
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        opened = _require_private_file_identity(
            descriptor, parent_descriptor=parent_descriptor, path=path,
            label=label,
        )
        current = os.stat(
            path.name, dir_fd=parent_descriptor, follow_symlinks=False,
        )
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ShadeformError(f"{label} changed before unlink")
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        try:
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ShadeformError(f"{label} remained after unlink")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def write_owned_resource(record: OwnedResource) -> None:
    _validate_owned_resource(record)
    path = runtime_ledger_path(record.phase_id)
    payload = (json.dumps(asdict(record), indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > 65_536:
        raise ShadeformError("phase ownership ledger exceeds its byte bound")
    private_durable_atomic_write(path, payload, label="phase ownership ledger")
    update_markdown_ledger(record)


def clear_owned_resource(phase_id: str, instance_id: str) -> None:
    current = read_owned_resource(phase_id)
    if current is None:
        return
    if current.instance_id != validate_resource_id(instance_id):
        raise ShadeformError("refusing to clear a ledger for a different instance")
    path = runtime_ledger_path(phase_id)
    private_durable_unlink(path, label="phase ownership ledger")


RECOVERY_OWNED_SCHEMA = "local_bmo.shadeform.recovery-owned-resource.v1"
INSTANCE_CREATE_INTENT_SCHEMA = "local_bmo.shadeform.instance-create-intent.v1"
SSH_KEY_DELETE_INTENT_SCHEMA = "local_bmo.shadeform.ssh-key-deletion-intent.v1"
SSH_KEY_DELETE_CONFIRMATION_SCHEMA = (
    "local_bmo.shadeform.ssh-key-deletion-confirmation.v1"
)
MAX_SSH_KEY_DELETE_EVIDENCE_BYTES = 65_536


def recovery_owned_resource_path(phase_id: str) -> Path:
    return RUNTIME_ROOT / f"{validate_phase_id(phase_id)}.recovery-owned.json"


def write_recovery_owned_resource(record: OwnedResource) -> None:
    """Durably retain an exact resource when the normal ledger cannot publish."""

    _validate_owned_resource(record)
    path = recovery_owned_resource_path(record.phase_id)
    payload = {
        "schema": RECOVERY_OWNED_SCHEMA,
        "record": asdict(record),
    }
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > 65_536:
        raise ShadeformError("recovery ownership record exceeds its byte bound")
    existing = read_recovery_owned_resource(record.phase_id)
    if existing is not None:
        if asdict(existing) != asdict(record):
            raise ShadeformError("refusing to replace a different recovery ownership record")
        return
    try:
        private_durable_create_new(
            path, data, label="recovery ownership record",
        )
    except FileExistsError:
        existing = read_recovery_owned_resource(record.phase_id)
        if existing is None or asdict(existing) != asdict(record):
            raise ShadeformError("a different recovery ownership record raced publication")


def read_recovery_owned_resource(phase_id: str) -> OwnedResource | None:
    path = recovery_owned_resource_path(phase_id)
    try:
        data = private_bounded_stable_bytes(
            path, 65_536, label="recovery ownership record",
        )
    except FileNotFoundError:
        return None
    payload = strict_json_object(
        data,
        label="recovery ownership record",
    )
    if set(payload) != {"schema", "record"} or payload.get("schema") != RECOVERY_OWNED_SCHEMA:
        raise ShadeformError("recovery ownership record has an unexpected schema")
    raw_record = payload.get("record")
    if not isinstance(raw_record, dict) or set(raw_record) != set(OwnedResource.__dataclass_fields__):
        raise ShadeformError("recovery ownership record has unknown or missing fields")
    try:
        record = OwnedResource(**raw_record)
        _validate_owned_resource(record)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShadeformError("recovery ownership record is malformed") from exc
    if record.phase_id != validate_phase_id(phase_id):
        raise ShadeformError("recovery ownership record is bound to another phase")
    return record


def read_phase_ownership(
    phase_id: str,
) -> tuple[OwnedResource | None, bool]:
    """Load one authoritative normal/recovery owner, rejecting conflicts."""

    phase = validate_phase_id(phase_id)
    normal = read_owned_resource(phase)
    recovery = read_recovery_owned_resource(phase)
    def immutable_owner(record: OwnedResource) -> tuple[Any, ...]:
        fingerprint = record.ssh_public_key_fingerprint
        if fingerprint is None and record.ssh_public_key is not None:
            fingerprint = ssh_public_key_fingerprint(record.ssh_public_key)
        return (
            record.phase_id, record.instance_id, record.instance_name,
            record.ownership_nonce, record.ssh_key_id, record.ssh_key_name,
            fingerprint, record.gpu, record.cloud, record.region,
            record.instance_type, record.gpu_count, record.vram_gb,
            record.os_image, record.hourly_usd, record.created_at_utc,
            record.provider_delete_deadline_utc,
        )

    if (
        normal is not None
        and recovery is not None
        and immutable_owner(normal) != immutable_owner(recovery)
    ):
        raise ShadeformError(
            "normal and recovery ownership records conflict; manual recovery is required"
        )
    return normal or recovery, normal is not None


def clear_recovery_owned_resource(phase_id: str, instance_id: str) -> None:
    current = read_recovery_owned_resource(phase_id)
    if current is None:
        return
    if current.instance_id != validate_resource_id(instance_id):
        raise ShadeformError("refusing to clear a different recovery ownership record")
    path = recovery_owned_resource_path(phase_id)
    private_durable_unlink(path, label="recovery ownership record")


def _ledger_row(record: OwnedResource) -> str:
    cost = "pending" if record.cost_usd is None else f"${record.cost_usd:.4f}"
    return (
        f"| {record.created_at_utc[:10]} | {record.phase_id} | {record.instance_id} | "
        f"{record.gpu} | ${record.hourly_usd:.4f} | {record.run_id} | {record.status} | "
        f"{cost} | {record.idle_minutes:.1f} |"
    )


def _parse_ledger_row(line: str) -> dict[str, str] | None:
    """Split one markdown ledger row, or return None if the line is not a data row."""

    if not line.lstrip().startswith("|"):
        return None
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if len(cells) != 9 or cells[0] in {"date", "---"} or set(cells[0]) <= {"-", ":"}:
        return None
    names = (
        "date",
        "phase",
        "instance_id",
        "gpu",
        "hourly",
        "purpose",
        "status",
        "cost",
        "idle",
    )
    return dict(zip(names, cells))


def _valid_cost(value: object) -> bool:
    """Accept only JSON-number-like, finite, nonnegative cost values."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def _canonical_cost_genesis(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the single reviewed baseline that anchors the JSON ledger."""

    if set(event) != COST_GENESIS_FIELDS:
        raise ValueError("cost ledger genesis has unknown or missing fields")
    if event.get("schema") != COST_EVENT_SCHEMA or event.get("event_kind") != "genesis":
        raise ValueError("cost ledger genesis schema is invalid")
    if event.get("program") != COST_LEDGER_PROGRAM:
        raise ValueError("cost ledger genesis program is invalid")
    if event.get("currency") != COST_LEDGER_CURRENCY:
        raise ValueError("cost ledger genesis currency is invalid")
    cap = event.get("budget_cap_usd")
    prior = event.get("prior_settled_spend_usd")
    if not _valid_cost(cap) or float(cap) <= 0:
        raise ValueError("cost ledger genesis cap must be finite and positive")
    if not _valid_cost(prior) or float(prior) > float(cap):
        raise ValueError("cost ledger genesis prior spend is invalid")
    pending = event.get("current_pending_owner_count")
    if isinstance(pending, bool) or not isinstance(pending, int) or pending != 0:
        raise ValueError("cost ledger genesis requires an explicit zero pending baseline")
    for field in ("display_ledger_sha256", "incidents_sha256"):
        value = event.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"cost ledger genesis {field} is invalid")
    recorded = event.get("recorded_at_utc")
    if not isinstance(recorded, str) or not 1 <= len(recorded) <= 64:
        raise ValueError("cost ledger genesis timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(recorded)
    except ValueError as exc:
        raise ValueError("cost ledger genesis timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cost ledger genesis timestamp must be timezone-aware")
    canonical = dict(event)
    canonical["recorded_at_utc"] = parsed.isoformat()
    try:
        encoded = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("cost ledger genesis is not bounded JSON data") from exc
    if len(encoded) > MAX_COST_EVENT_BYTES:
        raise ValueError("cost ledger genesis exceeds its byte bound")
    if canonical != event:
        raise ValueError("stored cost ledger genesis is not canonical")
    return canonical


def _open_private_canonical_parent(
    path: Path, *, label: str = "cost ledger", require_private: bool = True,
) -> int:
    """Open and identity-bind an existing private canonical parent directory."""

    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ShadeformError(f"{label} requires POSIX no-follow directory handles")
    if not OPEN_SUPPORTS_DIR_FD or not STAT_SUPPORTS_DIR_FD:
        raise ShadeformError(f"{label} requires handle-relative filesystem operations")
    requested = Path(path)
    if not requested.is_absolute() or Path(os.path.abspath(requested)) != requested:
        raise ShadeformError(f"{label} path must be absolute and normalized")
    parent = requested.parent
    try:
        if parent.resolve(strict=True) != parent:
            raise ShadeformError(f"{label} parent must have no symlink ancestors")
    except OSError as exc:
        raise ShadeformError(f"{label} parent is unavailable") from exc
    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ShadeformError(f"{label} parent no-follow open failed") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_uid != os.getuid()
            or current.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) & (0o077 if require_private else 0o022)
            or stat.S_IMODE(current.st_mode) & (0o077 if require_private else 0o022)
        ):
            raise ShadeformError(
                f"{label} parent must be identity-stable and owner-private"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_private_file_identity(
    descriptor: int,
    *,
    parent_descriptor: int,
    path: Path,
    label: str,
    maximum_size: int | None = None,
    require_private_parent: bool = True,
) -> os.stat_result:
    """Require one mode-0600 current-user file at an identity-stable path."""

    try:
        opened = os.fstat(descriptor)
        relative = os.stat(
            path.name, dir_fd=parent_descriptor, follow_symlinks=False,
        )
        current = os.stat(path, follow_symlinks=False)
        parent_opened = os.fstat(parent_descriptor)
        parent_current = os.stat(path.parent, follow_symlinks=False)
    except OSError as exc:
        raise ShadeformError(f"{label} path identity is unavailable") from exc
    opened_identity = (opened.st_dev, opened.st_ino, opened.st_size)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(relative.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != 1
        or relative.st_nlink != 1
        or current.st_nlink != 1
        or opened.st_uid != os.getuid()
        or relative.st_uid != os.getuid()
        or current.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(relative.st_mode) != 0o600
        or stat.S_IMODE(current.st_mode) != 0o600
        or opened_identity != (relative.st_dev, relative.st_ino, relative.st_size)
        or opened_identity != (current.st_dev, current.st_ino, current.st_size)
        or not stat.S_ISDIR(parent_opened.st_mode)
        or not stat.S_ISDIR(parent_current.st_mode)
        or (parent_opened.st_dev, parent_opened.st_ino)
        != (parent_current.st_dev, parent_current.st_ino)
        or parent_opened.st_uid != os.getuid()
        or parent_current.st_uid != os.getuid()
        or stat.S_IMODE(parent_opened.st_mode) & (0o077 if require_private_parent else 0o022)
        or stat.S_IMODE(parent_current.st_mode) & (0o077 if require_private_parent else 0o022)
        or (maximum_size is not None and opened.st_size > maximum_size)
    ):
        raise ShadeformError(
            f"{label} must be identity-stable owner-private single-link regular data"
        )
    return opened


def _read_private_file_at(
    descriptor: int,
    *,
    parent_descriptor: int,
    path: Path,
    limit: int,
    label: str,
    require_private_parent: bool = True,
) -> bytes:
    """Read one already-open private file and revalidate its exact path."""

    before = _require_private_file_identity(
        descriptor, parent_descriptor=parent_descriptor, path=path,
        label=label, maximum_size=limit,
        require_private_parent=require_private_parent,
    )
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(descriptor, min(65_536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ShadeformError(f"{label} exceeds its byte bound")
    after = _require_private_file_identity(
        descriptor, parent_descriptor=parent_descriptor, path=path,
        label=label, maximum_size=limit,
        require_private_parent=require_private_parent,
    )
    if (
        (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or total != after.st_size
    ):
        raise ShadeformError(f"{label} changed during bounded read")
    return b"".join(chunks)


def private_bounded_stable_bytes(path: Path, limit: int, *, label: str) -> bytes:
    """Read authoritative runtime evidence through private dir/file handles."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("bounded read limit must be positive")
    parent_descriptor = _open_private_canonical_parent(path, label=label)
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_SH)
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ShadeformError(f"{label} no-follow open failed") from exc
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        return _read_private_file_at(
            descriptor, parent_descriptor=parent_descriptor, path=path,
            limit=limit, label=label,
        )
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def _initialize_cost_ledger_genesis(
    *,
    program: str,
    currency: str,
    budget_cap_usd: float,
    prior_settled_spend_usd: float,
    current_pending_owner_count: int,
    expected_display_ledger_sha256: str,
    expected_incidents_sha256: str,
    confirmation: str,
) -> tuple[dict[str, Any], str]:
    """Explicitly create the reviewed v2 JSON cost baseline exactly once.

    This offline operation is deliberately not called by either launcher.  It
    binds a human-reviewed spend assertion to exact bounded display/incident
    bytes and has no credential, catalogue, provider, or process path.
    """

    if confirmation != COST_LEDGER_GENESIS_CONFIRMATION:
        raise ShadeformError("cost ledger genesis review confirmation is absent")
    for value, field in (
        (expected_display_ledger_sha256, "display ledger hash"),
        (expected_incidents_sha256, "incidents hash"),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"cost ledger genesis {field} is invalid")
    try:
        display = bounded_stable_bytes(
            MARKDOWN_LEDGER,
            MAX_MARKDOWN_LEDGER_BYTES,
            label="cost genesis display ledger evidence",
        )
        incidents = bounded_stable_bytes(
            INCIDENTS,
            MAX_INCIDENT_EVIDENCE_BYTES,
            label="cost genesis incident evidence",
        )
    except FileNotFoundError as exc:
        raise ShadeformError("cost ledger genesis evidence is unavailable") from exc
    if hashlib.sha256(display).hexdigest() != expected_display_ledger_sha256:
        raise ShadeformError("cost ledger genesis display evidence hash does not match")
    if hashlib.sha256(incidents).hexdigest() != expected_incidents_sha256:
        raise ShadeformError("cost ledger genesis incident evidence hash does not match")

    genesis = _canonical_cost_genesis({
        "schema": COST_EVENT_SCHEMA,
        "event_kind": "genesis",
        "program": program,
        "currency": currency,
        "budget_cap_usd": budget_cap_usd,
        "prior_settled_spend_usd": prior_settled_spend_usd,
        "current_pending_owner_count": current_pending_owner_count,
        "display_ledger_sha256": expected_display_ledger_sha256,
        "incidents_sha256": expected_incidents_sha256,
        "recorded_at_utc": utc_now().isoformat(),
    })
    payload = (
        json.dumps(genesis, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_COST_EVENT_BYTES:
        raise ValueError("cost ledger genesis exceeds its byte bound")

    ledger_path = Path(COST_LEDGER)
    parent_descriptor = _open_private_canonical_parent(ledger_path)
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        try:
            os.stat(ledger_path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ShadeformError("cost ledger genesis target identity is unavailable") from exc
        else:
            try:
                descriptor = os.open(
                    ledger_path.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                raise ShadeformError(
                    "existing authoritative JSON cost ledger is unavailable"
                ) from exc
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            existing_bytes, _ = _read_open_cost_ledger(
                descriptor, parent_descriptor=parent_descriptor,
            )
            existing_events = _cost_ledger_events(existing_bytes)
            if len(existing_events) != 1 or existing_events[0].get("event_kind") != "genesis":
                raise ShadeformError(
                    "existing authoritative JSON cost ledger is not a sole genesis"
                )
            expected = {
                key: value for key, value in genesis.items()
                if key != "recorded_at_utc"
            }
            actual = {
                key: value for key, value in existing_events[0].items()
                if key != "recorded_at_utc"
            }
            if actual != expected:
                raise ShadeformError(
                    "authoritative JSON cost ledger already exists with different genesis"
                )
            # A prior caller may have observed a fault after the file write but
            # before the final durability barrier. Re-running the exact reviewed
            # request completes those barriers without replacing history.
            os.fsync(descriptor)
            os.fsync(parent_descriptor)
            return dict(existing_events[0]), "recovered_existing"

        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(
                ledger_path.name, flags, 0o600, dir_fd=parent_descriptor,
            )
        except FileExistsError as exc:
            raise ShadeformError("authoritative JSON cost ledger already exists") from exc
        except OSError as exc:
            raise ShadeformError("cost ledger genesis no-follow creation failed") from exc
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("cost ledger genesis write made no progress")
            written += count
        os.fsync(descriptor)
        read_back, _ = _read_open_cost_ledger(
            descriptor, parent_descriptor=parent_descriptor,
        )
        if read_back != payload:
            raise ShadeformError("cost ledger genesis bytes changed during publication")
        opened = os.fstat(descriptor)
        relative = os.stat(
            ledger_path.name, dir_fd=parent_descriptor, follow_symlinks=False,
        )
        absolute = os.stat(ledger_path, follow_symlinks=False)
        parent_opened = os.fstat(parent_descriptor)
        parent_current = os.stat(ledger_path.parent, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino, opened.st_size)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or relative.st_nlink != 1
            or absolute.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or identity != (relative.st_dev, relative.st_ino, relative.st_size)
            or identity != (absolute.st_dev, absolute.st_ino, absolute.st_size)
            or opened.st_size != len(payload)
            or (parent_opened.st_dev, parent_opened.st_ino)
            != (parent_current.st_dev, parent_current.st_ino)
            or parent_opened.st_uid != os.getuid()
            or parent_current.st_uid != os.getuid()
            or stat.S_IMODE(parent_opened.st_mode) & 0o077
            or stat.S_IMODE(parent_current.st_mode) & 0o077
        ):
            raise ShadeformError("cost ledger genesis publication identity is unsafe")
        os.fsync(parent_descriptor)
        verified = _cost_ledger_events(read_back)
        if verified != [genesis]:
            raise ShadeformError("cost ledger genesis verification failed")
        return dict(genesis), "created"
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def initialize_cost_ledger_genesis(**kwargs: Any) -> dict[str, Any]:
    """Create or exactly validate the reviewed baseline, returning its event."""

    genesis, _ = _initialize_cost_ledger_genesis(**kwargs)
    return genesis


def initialize_cost_ledger_genesis_with_status(
    **kwargs: Any,
) -> tuple[dict[str, Any], str]:
    """Offline CLI variant that distinguishes create from crash-safe recovery."""

    return _initialize_cost_ledger_genesis(**kwargs)


def cost_owner_binding_sha256(phase_id: str, ownership_nonce: str, instance_id: str) -> str:
    """Bind one cost stream to an exact phase, owner nonce, and resource ID."""

    phase = validate_phase_id(phase_id)
    nonce = validate_nonce(ownership_nonce)
    exact = validate_resource_id(instance_id, field="cost event instance id")
    payload = json.dumps(
        {"instance_id": exact, "ownership_nonce": nonce, "phase_id": phase},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_cost_event(event: dict[str, Any], *, stored: bool) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("cost event must be an object")
    if event.get("event_kind") == "genesis":
        if not stored:
            raise ValueError("cost ledger genesis may only be created by the explicit initializer")
        return _canonical_cost_genesis(event)
    if not set(event) <= COST_EVENT_FIELDS:
        raise ValueError("cost event contains unknown fields")
    if stored and event.get("schema") != COST_EVENT_SCHEMA:
        raise ValueError("cost event schema is invalid")
    if not stored and any(
        field in event for field in ("schema", "recorded_at_utc", "owner_binding_sha256")
    ):
        raise ValueError("cost event storage fields are host-owned")
    instance_id = event.get("instance_id")
    phase_id = event.get("phase_id")
    ownership_nonce = event.get("ownership_nonce")
    if not isinstance(instance_id, str) or not isinstance(phase_id, str) or not isinstance(ownership_nonce, str):
        raise ValueError("cost event requires exact instance, phase, and owner nonce")
    owner_binding = cost_owner_binding_sha256(phase_id, ownership_nonce, instance_id)
    supplied_binding = event.get("owner_binding_sha256")
    if (stored and supplied_binding != owner_binding) or (
        not stored and supplied_binding is not None
    ):
        raise ValueError("cost event owner binding is invalid")
    if instance_id.startswith("attempt-") and instance_id != f"attempt-{ownership_nonce}":
        raise ValueError("cost attempt is not bound to its owner nonce")
    status = event.get("status")
    if status not in {"pending", "settled"}:
        raise ValueError("cost event status is invalid")
    if status == "pending":
        if "actual_cost_usd" in event or not _valid_cost(event.get("estimated_cost_usd")):
            raise ValueError("pending cost event requires a finite nonnegative estimate only")
    elif "estimated_cost_usd" in event or not _valid_cost(event.get("actual_cost_usd")):
        raise ValueError("settled cost event requires a finite nonnegative actual only")
    canonical = dict(event)
    canonical["schema"] = COST_EVENT_SCHEMA
    canonical["phase_id"] = validate_phase_id(phase_id)
    canonical["ownership_nonce"] = validate_nonce(ownership_nonce)
    canonical["instance_id"] = validate_resource_id(instance_id, field="cost event instance id")
    canonical["owner_binding_sha256"] = owner_binding
    if stored:
        recorded = event.get("recorded_at_utc")
        if not isinstance(recorded, str) or not 1 <= len(recorded) <= 64:
            raise ValueError("cost event recorded timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(recorded)
        except ValueError as exc:
            raise ValueError("cost event recorded timestamp is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("cost event recorded timestamp must be timezone-aware")
        canonical["recorded_at_utc"] = parsed.isoformat()
    try:
        encoded = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("cost event is not bounded JSON data") from exc
    if len(encoded) > MAX_COST_EVENT_BYTES:
        raise ValueError("cost event exceeds its byte bound")
    if stored and canonical != event:
        raise ValueError("stored cost event is not canonical")
    return canonical


def _cost_ledger_events(data: bytes) -> list[dict[str, Any]]:
    if not data or len(data) > MAX_COST_LEDGER_BYTES or not data.endswith(b"\n"):
        raise ShadeformError("cost ledger is empty, partial, or oversized")
    lines = data.splitlines()
    if not lines or len(lines) > MAX_COST_LEDGER_LINES:
        raise ShadeformError("cost ledger exceeds the bounded recovery line count")
    events: list[dict[str, Any]] = []
    owners_by_instance: dict[str, str] = {}
    state_by_owner: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(lines, 1):
        if not line or len(line) > MAX_COST_EVENT_BYTES:
            raise ShadeformError(f"cost ledger line {number} is empty or oversized")
        try:
            event = _canonical_cost_event(
                strict_json_object(line, label=f"cost ledger line {number}"),
                stored=True,
            )
        except (TypeError, ValueError) as exc:
            raise ShadeformError(f"cost ledger line {number} is invalid") from exc
        if event.get("event_kind") == "genesis":
            if number != 1 or any(item.get("event_kind") == "genesis" for item in events):
                raise ShadeformError("cost ledger genesis must be the unique first event")
            events.append(event)
            continue
        instance_id = event["instance_id"]
        owner_binding = event["owner_binding_sha256"]
        previous_owner = owners_by_instance.setdefault(instance_id, owner_binding)
        if previous_owner != owner_binding:
            raise ShadeformError(
                f"cost ledger line {number} reuses an instance for a different owner"
            )
        previous = state_by_owner.get(owner_binding)
        if previous is None and event["status"] != "pending":
            raise ShadeformError(f"cost ledger line {number} settles an unknown owner")
        if (
            previous is not None
            and previous["status"] == "pending"
            and event["status"] == "pending"
            and event["estimated_cost_usd"] != previous["estimated_cost_usd"]
        ):
            raise ShadeformError(f"cost ledger line {number} changes a pending estimate")
        if previous is not None and previous["status"] == "settled":
            if event["status"] != "settled" or event["actual_cost_usd"] != previous["actual_cost_usd"]:
                raise ShadeformError(f"cost ledger line {number} changes a settled owner")
        state_by_owner[owner_binding] = event
        events.append(event)
    return events


def _require_cost_ledger_path_identity(
    opened: os.stat_result, *, parent_descriptor: int,
) -> None:
    try:
        relative = os.stat(
            Path(COST_LEDGER).name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        current = os.stat(COST_LEDGER, follow_symlinks=False)
        parent_opened = os.fstat(parent_descriptor)
        parent_current = os.stat(Path(COST_LEDGER).parent, follow_symlinks=False)
    except OSError as exc:
        raise ShadeformError("cost ledger path identity is unavailable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(relative.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != 1
        or relative.st_nlink != 1
        or current.st_nlink != 1
        or opened.st_uid != os.getuid()
        or relative.st_uid != os.getuid()
        or current.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(relative.st_mode) != 0o600
        or stat.S_IMODE(current.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino, opened.st_size)
        != (relative.st_dev, relative.st_ino, relative.st_size)
        or (opened.st_dev, opened.st_ino, opened.st_size)
        != (current.st_dev, current.st_ino, current.st_size)
        or not stat.S_ISDIR(parent_opened.st_mode)
        or not stat.S_ISDIR(parent_current.st_mode)
        or (parent_opened.st_dev, parent_opened.st_ino)
        != (parent_current.st_dev, parent_current.st_ino)
        or parent_opened.st_uid != os.getuid()
        or parent_current.st_uid != os.getuid()
        or stat.S_IMODE(parent_opened.st_mode) & 0o077
        or stat.S_IMODE(parent_current.st_mode) & 0o077
    ):
        raise ShadeformError("cost ledger path identity or permissions are unsafe")


def _read_open_cost_ledger(
    descriptor: int, *, parent_descriptor: int,
) -> tuple[bytes, os.stat_result]:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_COST_LEDGER_BYTES
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise ShadeformError(
            "cost ledger is not bounded owner-private single-link regular data"
        )
    _require_cost_ledger_path_identity(
        before, parent_descriptor=parent_descriptor,
    )
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_COST_LEDGER_BYTES:
        chunk = os.read(descriptor, min(65_536, MAX_COST_LEDGER_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_COST_LEDGER_BYTES:
            raise ShadeformError("cost ledger exceeds its byte bound")
    after = os.fstat(descriptor)
    if (
        after.st_nlink != 1
        or after.st_uid != os.getuid()
        or stat.S_IMODE(after.st_mode) != 0o600
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or total != after.st_size
    ):
        raise ShadeformError("cost ledger changed during descriptor read")
    _require_cost_ledger_path_identity(
        after, parent_descriptor=parent_descriptor,
    )
    return b"".join(chunks), after


def cost_ledger_events() -> list[dict[str, Any]]:
    """Load the authoritative ledger through its one strict filesystem/parser path."""

    ledger_path = Path(COST_LEDGER)
    parent_descriptor = _open_private_canonical_parent(ledger_path)
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_SH)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        try:
            descriptor = os.open(
                ledger_path.name, flags, dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ShadeformError("cost ledger no-follow open failed") from exc
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        data, _ = _read_open_cost_ledger(
            descriptor, parent_descriptor=parent_descriptor,
        )
        return _cost_ledger_events(data)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def ledger_spend(*, expected_budget_cap_usd: float | None = None) -> tuple[float, list[str]]:
    """Return (dollars already committed, instance IDs whose cost is still unrecorded).

    AGENTS.md 1.4: ``SHADEFORM_MAX_TOTAL_COST_USD`` is the budget for the whole
    project, not for one run. A launcher therefore has to know what has already
    been spent, and a row still reading ``pending`` means some earlier run's
    money was never accounted for -- which is a stop, not a rounding error.
    """

    try:
        events = cost_ledger_events()
    except FileNotFoundError as exc:
        # The Markdown ledger is an operator-readable projection. It is not an
        # append-only, owner-bound source of truth and therefore can never
        # authorize a provider mutation or establish a zero historical spend.
        raise ShadeformError(
            "authoritative JSON cost ledger is absent; budget state cannot be proven"
        ) from exc
    genesis = events[0] if events and events[0].get("event_kind") == "genesis" else None
    if expected_budget_cap_usd is not None:
        if not _valid_cost(expected_budget_cap_usd) or float(expected_budget_cap_usd) <= 0:
            raise BudgetError("SHADEFORM_MAX_TOTAL_COST_USD must be finite and positive")
        if genesis is None:
            # Owner-only ledgers remain readable for exact cleanup and incident
            # accounting, but can never authorize another provider mutation.
            raise BudgetError(
                "authoritative JSON cost ledger has no reviewed genesis baseline"
            )
        if float(genesis["budget_cap_usd"]) != float(expected_budget_cap_usd):
            raise BudgetError(
                "configured total-cost cap does not match the reviewed ledger genesis"
            )
    latest = {
        event["owner_binding_sha256"]: event
        for event in events
        if event.get("event_kind") != "genesis"
    }
    spent = float(genesis["prior_settled_spend_usd"]) if genesis is not None else 0.0
    pending: list[str] = []
    for event in latest.values():
        if event.get("status") == "pending":
            pending.append(event["instance_id"])
        else:
            actual = event.get("actual_cost_usd")
            spent += float(actual)
    if (
        not math.isfinite(spent)
        or (genesis is not None and spent > float(genesis["budget_cap_usd"]))
    ):
        raise BudgetError("authoritative cost ledger exceeds its reviewed cap")
    return round(spent, 6), pending


def exact_owner_cost_state(
    phase_id: str, ownership_nonce: str, instance_id: str,
) -> dict[str, Any] | None:
    """Return the latest canonical event for one exact owner/resource.

    Recovery uses this before creating an exact-instance reservation.  The
    complete ledger is still validated first, so a reused provider instance ID,
    changed pending estimate, partial row, or different owner cannot be hidden
    by selecting only the requested stream.
    """

    owner_binding = cost_owner_binding_sha256(
        phase_id, ownership_nonce, instance_id,
    )
    try:
        events = cost_ledger_events()
    except FileNotFoundError:
        return None
    matches = [
        event for event in events
        if event.get("event_kind") != "genesis"
        and event["owner_binding_sha256"] == owner_binding
    ]
    return dict(matches[-1]) if matches else None


def append_cost_event(event: dict[str, Any]) -> None:
    """Append one immutable settled/pending event to an existing authority.

    Only the explicit genesis initializer may create ``COST_LEDGER``.  Cleanup
    may append to a legacy owner-only ledger for exact recovery, but no generic
    append is allowed to manufacture a new budget authority.
    """

    canonical = _canonical_cost_event(event, stored=False)
    canonical["recorded_at_utc"] = utc_now().isoformat()
    canonical = _canonical_cost_event(canonical, stored=True)
    payload = (
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_COST_EVENT_BYTES:
        raise ValueError("cost event exceeds its byte bound")
    ledger_path = Path(COST_LEDGER)
    if not hasattr(os, "O_NOFOLLOW"):
        raise ShadeformError("cost ledger no-follow open is unavailable")
    parent_descriptor = _open_private_canonical_parent(ledger_path)
    descriptor = -1
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
    try:
        try:
            fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
            descriptor = os.open(
                ledger_path.name,
                flags,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError as exc:
            raise ShadeformError(
                "authoritative JSON cost ledger is absent; explicit genesis is required"
            ) from exc
        except OSError as exc:
            raise ShadeformError("cost ledger no-follow open failed") from exc
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existing, before = _read_open_cost_ledger(
            descriptor, parent_descriptor=parent_descriptor,
        )
        if not existing:
            raise ShadeformError("existing cost ledger is empty or incomplete")
        _cost_ledger_events(existing + payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("cost ledger append made no progress")
            written += count
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != before.st_size + len(payload)
        ):
            raise ShadeformError("cost ledger changed during append")
        _require_cost_ledger_path_identity(
            after, parent_descriptor=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def configured_auto_terminate_hours(env: dict[str, str]) -> float:
    """Return the operator's standing provider auto-delete ceiling, in hours.

    This is the outer bound on how long a created instance can exist, so it is
    also the outer bound on what one run can cost. ``_auto_delete`` uses it to
    size the per-run backstop; a pre-spend cost projection uses it to bound the
    bill. Only the key name and the numeric ceiling are read; no value from the
    environment is logged or returned anywhere else.
    """

    try:
        ceiling = float(env.get("SHADEFORM_AUTO_TERMINATE_HOURS", "2.5") or "2.5")
    except (TypeError, ValueError) as exc:
        raise BackstopError("SHADEFORM_AUTO_TERMINATE_HOURS must be finite and positive") from exc
    if not math.isfinite(ceiling) or ceiling <= 0:
        raise BackstopError("SHADEFORM_AUTO_TERMINATE_HOURS must be finite and positive")
    return ceiling


def configured_budget_cap_usd(env: dict[str, str]) -> float:
    """Return the exact reviewed project cap requested by configuration."""

    try:
        cap = float(env.get("SHADEFORM_MAX_TOTAL_COST_USD", "50") or "50")
    except (TypeError, ValueError) as exc:
        raise BudgetError("SHADEFORM_MAX_TOTAL_COST_USD must be finite and positive") from exc
    if not math.isfinite(cap) or cap <= 0:
        raise BudgetError("SHADEFORM_MAX_TOTAL_COST_USD must be finite and positive")
    return cap


def _append_reserved_cost_event(
    canonical: dict[str, Any], payload: bytes, *, expected_budget_cap_usd: float,
) -> None:
    """Atomically authorize and append one pre-create reservation."""

    if not _valid_cost(expected_budget_cap_usd) or float(expected_budget_cap_usd) <= 0:
        raise BudgetError("configured total-cost cap must be finite and positive")
    ledger_path = Path(COST_LEDGER)
    parent_descriptor = _open_private_canonical_parent(ledger_path)
    descriptor = -1
    try:
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        try:
            descriptor = os.open(
                ledger_path.name,
                os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError as exc:
            raise BudgetError(
                "authoritative JSON cost ledger is absent; explicit genesis is required"
            ) from exc
        except OSError as exc:
            raise ShadeformError("cost ledger no-follow open failed") from exc
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existing, before = _read_open_cost_ledger(
            descriptor, parent_descriptor=parent_descriptor,
        )
        events = _cost_ledger_events(existing)
        if not events or events[0].get("event_kind") != "genesis":
            raise BudgetError(
                "authoritative JSON cost ledger has no reviewed genesis baseline"
            )
        genesis = events[0]
        if (
            genesis.get("program") != COST_LEDGER_PROGRAM
            or genesis.get("currency") != COST_LEDGER_CURRENCY
            or float(genesis["budget_cap_usd"]) != float(expected_budget_cap_usd)
        ):
            raise BudgetError(
                "configured budget authority does not match the reviewed ledger genesis"
            )

        latest: dict[str, dict[str, Any]] = {}
        for event in events[1:]:
            latest[event["owner_binding_sha256"]] = event
        owner = canonical["owner_binding_sha256"]
        prior = latest.get(owner)
        committed = float(genesis["prior_settled_spend_usd"])
        for binding, event in latest.items():
            if binding == owner:
                continue
            amount = (
                event["estimated_cost_usd"]
                if event["status"] == "pending"
                else event["actual_cost_usd"]
            )
            committed = math.fsum((committed, float(amount)))
        if prior is None:
            proposal = float(canonical["estimated_cost_usd"])
        elif prior["status"] != "pending":
            raise BudgetError("create-attempt reservation is already settled")
        else:
            # The second reservation binds the exact provider SSH-key ID.  It
            # costs no additional money and may not change any existing field.
            if prior["estimated_cost_usd"] != canonical["estimated_cost_usd"]:
                raise BudgetError("create-attempt reservation estimate changed")
            for field, value in prior.items():
                if field in {"schema", "recorded_at_utc", "owner_binding_sha256"}:
                    continue
                if field not in canonical or canonical[field] != value:
                    raise BudgetError("create-attempt reservation binding changed")
            proposal = float(prior["estimated_cost_usd"])
        total = math.fsum((committed, proposal))
        if not math.isfinite(total) or total > float(genesis["budget_cap_usd"]):
            raise BudgetError("create-attempt reservation exceeds reviewed project budget")

        _cost_ledger_events(existing + payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("cost ledger reservation append made no progress")
            written += count
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            after.st_nlink != 1
            or after.st_uid != os.getuid()
            or stat.S_IMODE(after.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != before.st_size + len(payload)
        ):
            raise ShadeformError("cost ledger changed during reservation append")
        _require_cost_ledger_path_identity(after, parent_descriptor=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        with contextlib.suppress(OSError):
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def reserve_create_attempt(
    phase_id: str,
    nonce: str,
    candidate: Candidate,
    *,
    backstop_hours: float,
    public_key_sha256: str,
    expected_budget_cap_usd: float,
    public_key_fingerprint: str | None = None,
    ssh_key_id: str | None = None,
    approved_target_index: int | None = None,
) -> str:
    """Atomically reserve one possible create POST before provider mutation.

    ``approved_target_index`` names which entry of the caller's ordered
    approved-target list this reservation is for. It is recorded inside the
    existing ``candidate`` object -- the top-level event field set and the
    cost-event schema are unchanged -- so the ledger row says not just what was
    rented but which approved alternate it was.
    """

    validate_phase_id(phase_id)
    validate_nonce(nonce)
    if backstop_hours <= 0 or not re.fullmatch(r"[0-9a-f]{64}", public_key_sha256):
        raise ValueError("invalid create-attempt reservation inputs")
    if approved_target_index is not None and (
            isinstance(approved_target_index, bool)
            or not isinstance(approved_target_index, int)
            or not 0 <= approved_target_index <= 63):
        raise ValueError("approved target index is not a bounded list position")
    if public_key_fingerprint is not None and re.fullmatch(r"[A-Za-z0-9+/]{43}", public_key_fingerprint) is None:
        raise ValueError("invalid SSH public-key fingerprint reservation input")
    if ssh_key_id is not None:
        validate_resource_id(ssh_key_id, field="SSH key id")
    attempt_id = f"attempt-{nonce}"
    event = {
        "instance_id": attempt_id,
        "phase_id": phase_id,
        "status": "pending",
        "estimated_cost_usd": round(candidate.hourly_usd * backstop_hours, 6),
        "reservation": "pre-create-attempt",
        "ownership_nonce": nonce,
        "ssh_key_name": f"j1m-{nonce}",
        "ssh_public_key_sha256": public_key_sha256,
        "candidate": {"cloud": candidate.cloud, "region": candidate.region, "gpu": candidate.gpu, "instance_type": candidate.instance_type, "vram_gb": candidate.vram_gb, "hourly_usd": candidate.hourly_usd},
    }
    if approved_target_index is not None:
        event["candidate"]["approved_target_index"] = approved_target_index
    if ssh_key_id is not None:
        # The latest event enriches the same reservation with the provider key
        # ID, without rewriting its append-only history.
        event["ssh_key_id"] = ssh_key_id
    if public_key_fingerprint is not None:
        event["ssh_public_key_fingerprint"] = public_key_fingerprint
    canonical = _canonical_cost_event(event, stored=False)
    canonical["recorded_at_utc"] = utc_now().isoformat()
    canonical = _canonical_cost_event(canonical, stored=True)
    payload = (
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _append_reserved_cost_event(
        canonical, payload, expected_budget_cap_usd=expected_budget_cap_usd,
    )
    return attempt_id


def append_instance_create_intent(
    phase_id: str,
    nonce: str,
    *,
    instance_name: str,
    ssh_key_id: str,
    hourly_usd: float,
    backstop_hours: float,
    provider_delete_deadline_utc: str,
    started_at_utc: str | None = None,
) -> str:
    """Persist the exact instance POST intent immediately before dispatch."""

    validate_phase_id(phase_id)
    validate_nonce(nonce)
    validate_resource_id(ssh_key_id, field="SSH key id")
    if (not isinstance(instance_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", instance_name)
            or not _valid_cost(hourly_usd) or not isinstance(backstop_hours, (int, float))
            or isinstance(backstop_hours, bool) or not math.isfinite(float(backstop_hours)) or backstop_hours <= 0):
        raise ValueError("invalid instance create intent")
    started = started_at_utc or utc_now().isoformat()
    try:
        parsed = datetime.fromisoformat(started)
    except (TypeError, ValueError) as exc:
        raise ValueError("instance create intent timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("instance create intent timestamp must be timezone-aware")
    try:
        provider_deadline = datetime.fromisoformat(provider_delete_deadline_utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("instance create intent provider deadline is invalid") from exc
    if (
        provider_deadline.tzinfo is None
        or provider_deadline.utcoffset() is None
        or not parsed < provider_deadline
        or (provider_deadline - parsed).total_seconds() > 259_200
    ):
        raise ValueError("instance create intent provider deadline is outside its bounded window")
    append_cost_event({
        "intent_schema": INSTANCE_CREATE_INTENT_SCHEMA,
        "instance_id": f"attempt-{nonce}",
        "phase_id": phase_id,
        "ownership_nonce": nonce,
        "status": "pending",
        "estimated_cost_usd": round(float(hourly_usd) * float(backstop_hours), 6),
        "reservation": "instance-create-intent",
        "instance_create_intent": True,
        "instance_name": instance_name,
        "ssh_key_id": ssh_key_id,
        "hourly_usd": float(hourly_usd),
        "backstop_hours": float(backstop_hours),
        "create_started_at_utc": parsed.isoformat(),
        "provider_delete_deadline_utc": provider_deadline.isoformat(),
    })
    return f"attempt-{nonce}"


def append_incident(event: dict[str, Any]) -> None:
    """Persist bounded incident metadata without recording secret material."""

    if not isinstance(event, dict) or not event.get("incident") or not event.get("phase_id"):
        raise ValueError("incident requires incident and phase_id")
    _ensure_durable_directory(INCIDENTS.parent)
    lock_path = INCIDENTS.with_suffix(".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        with INCIDENTS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**event, "recorded_at_utc": utc_now().isoformat()}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(INCIDENTS.parent)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def is_ambiguous_transport(exc: BaseException) -> bool:
    """Classify only an explicitly wrapped create outcome as ambiguous.

    ``HTTPError`` is a ``URLError`` subclass, so inspecting exception causes
    here would incorrectly turn a definitive provider rejection into a pending
    bill.  ``create_instance`` is the only caller that is allowed to establish
    this classification.
    """

    return isinstance(exc, AmbiguousProviderOutcome)


def update_markdown_ledger(record: OwnedResource) -> None:
    """Upsert exactly one human-readable row for one provisioned resource.

    One resource is one row, filled in as its lifecycle advances. Once a row has
    reached a terminal status with a recorded cost, only a terminal-status
    refinement with the same identity and accounting cells is permitted;
    nothing here can rewrite a different resource or its recorded cost.
    """

    _ensure_durable_directory(RUNTIME_ROOT)
    lock_path = RUNTIME_ROOT / "markdown-ledger.lock"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            existing = required_bounded_text(
                MARKDOWN_LEDGER,
                MAX_MARKDOWN_LEDGER_BYTES,
                label="experiments/LEDGER.md",
            )
            if LEDGER_HEADER not in existing:
                raise ShadeformError("experiments/LEDGER.md has an unexpected schema")
            row = _ledger_row(record)
            lines = existing.rstrip("\n").splitlines()
            match = f"| {record.instance_id} |"
            replaced = False
            for index, line in enumerate(lines):
                if match not in line:
                    continue
                previous = _parse_ledger_row(line)
                if (
                    previous is not None
                    and previous["status"] in TERMINAL_STATUSES
                    and previous["cost"].strip().lower() != "pending"
                    and line.strip() != row.strip()
                ):
                    replacement = _parse_ledger_row(row)
                    immutable = (
                        "date", "phase", "instance_id", "gpu", "hourly",
                        "purpose", "cost", "idle",
                    )
                    if (
                        replacement is None
                        or replacement["status"] not in TERMINAL_STATUSES
                        or any(previous[key] != replacement[key] for key in immutable)
                    ):
                        raise ShadeformError(
                            "refusing to rewrite settled ledger identity/cost for "
                            f"{record.instance_id}; append a correction instead"
                        )
                lines[index] = row
                replaced = True
                break
            if not replaced:
                lines.append(row)
            payload = "\n".join(lines) + "\n"
            if len(payload.encode("utf-8")) > MAX_MARKDOWN_LEDGER_BYTES:
                raise ShadeformError("experiments/LEDGER.md would exceed its byte bound")
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{MARKDOWN_LEDGER.name}.", dir=MARKDOWN_LEDGER.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, MARKDOWN_LEDGER)
                _fsync_directory(MARKDOWN_LEDGER.parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    durable_unlink(Path(temp_name))
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _preflight(phase_id: str | None = None) -> None:
    """Mandatory state read before any provider action.

    Both files are read in full on purpose: the runbook requires a fresh ledger
    and failure-mode read immediately before every create, poll, or delete, and
    a preflight that can be satisfied by a stat() is not one.
    """

    markdown = required_bounded_text(
        MARKDOWN_LEDGER,
        MAX_MARKDOWN_LEDGER_BYTES,
        label="experiments/LEDGER.md",
    )
    if LEDGER_HEADER not in markdown:
        raise ShadeformError("experiments/LEDGER.md has an unexpected schema")
    incident_catalog = required_bounded_text(
        INCIDENT_LOG,
        MAX_INCIDENT_CATALOG_BYTES,
        label="failure-mode catalogue",
    )
    if not incident_catalog.strip():
        raise ShadeformError("failure-mode catalogue is empty")
    if phase_id is not None:
        validate_phase_id(phase_id)


def api_base() -> str:
    """The provider endpoint.

    ``EP_SHADEFORM_API_BASE_FOR_TESTS`` exists so the teardown-under-failure
    test can drive real launcher, watchdog, and teardown *processes* against a
    stub provider, mocking nothing but the provider itself. It is confined to a
    loopback URL so it can never redirect a real run somewhere else.
    """

    override = os.environ.get("EP_SHADEFORM_API_BASE_FOR_TESTS", "").strip()
    if not override:
        return API_BASE
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}(/[A-Za-z0-9._/-]*)?", override):
        raise ShadeformError("the test API base override must be a loopback http:// URL")
    return override.rstrip("/")


def request(
    api_key: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    phase_id: str | None = None,
    timeout: float = 90,
) -> dict[str, Any]:
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("provider request timeout must be positive")
    _preflight(phase_id)
    url = f"{api_base()}{path}"
    body: bytes | None = None
    headers = {"X-API-KEY": api_key}
    if method == "GET" and payload:
        url = f"{url}?{urllib.parse.urlencode(payload)}"
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = _read_provider_response(response).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            message = _read_provider_response(exc).decode("utf-8", errors="replace")[:1000]
        except MalformedProviderResponse:
            message = "provider error body exceeded the bounded response size"
        raise ShadeformHTTPError(exc.code, message.replace(api_key, "<redacted>")) from exc
    except (OSError, TimeoutError) as exc:
        raise ShadeformError(f"Shadeform request failed for {method} {path}: {exc}") from exc
    if len(raw.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES:
        raise MalformedProviderResponse("Shadeform response exceeded the bounded response size")
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise MalformedProviderResponse("Shadeform response was not a JSON object")
    return parsed


def _read_provider_response(stream: Any) -> bytes:
    """Read provider success/error bodies with a hard pre-parse byte cap."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(65_536, MAX_PROVIDER_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise MalformedProviderResponse("Shadeform response body is not bytes")
        total += len(chunk)
        if total > MAX_PROVIDER_RESPONSE_BYTES:
            raise MalformedProviderResponse("Shadeform response exceeded the bounded response size")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _reject_provider_failure(response: object, action: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise MalformedProviderResponse(f"Shadeform {action} response was not an object")
    if any(response.get(field) is False for field in ("success", "accepted", "ok")) or response.get("status") in {"failed", "failure", "error"} or response.get("error"):
        raise ShadeformError(f"Shadeform {action} was not accepted")
    return response


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _norm_gpu(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().removeprefix("nvidia"))


def _gpu_rank(gpu: str, configured: list[str]) -> int | None:
    """Position of ``gpu`` in the configured preference list, or None if excluded.

    Matching is by prefix in both directions, not equality. Shadeform spells the
    same card several ways -- ``H100``, ``H100 80GB``, ``A100_80G`` -- so an
    allowlist of ``H100,A100`` matched every H100 in the catalogue and *none* of
    the seventeen A100 offerings, silently making the cheap card we wanted to
    prove the path on unreachable. ``min_vram_gb`` remains the real capability
    gate; this only decides which family a row belongs to.
    """

    normalized = _norm_gpu(gpu)
    if not normalized:
        return None
    for index, name in enumerate(configured):
        candidate = _norm_gpu(name)
        if not candidate:
            continue
        if normalized.startswith(candidate) or candidate.startswith(normalized):
            return index
    return None


def _hourly_usd(value: object) -> float | None:
    """Shadeform quotes hourly price in cents."""

    try:
        cents = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return cents / 100 if cents >= 0 else None


def remaining_budget_usd(env: dict[str, str]) -> float:
    """Dollars left in the project budget, refusing to guess past an unaccounted row."""

    cap = configured_budget_cap_usd(env)
    spent, pending = ledger_spend(expected_budget_cap_usd=cap)
    if pending:
        raise BudgetError(
            "authoritative JSON cost ledger still records cost 'pending' for "
            f"{', '.join(pending)}; account for that run before launching another"
        )
    return round(cap - spent, 6)


def list_candidates(
    api_key: str,
    env: dict[str, str],
    *,
    phase_id: str,
    min_vram_gb: int,
    max_runtime_hours: float,
    budget_usd: float | None = None,
) -> list[Candidate]:
    """Rank the eligible instance types, cheapest acceptable first.

    ``budget_usd`` defaults to what is left of the project budget after the
    ledger, not to the standing cap: the worst case of this run has to fit in
    what remains, not in what the account was originally allowed.
    """

    if budget_usd is None:
        budget_usd = remaining_budget_usd(env)
    return _rank_candidates(
        _fetch_instance_types(api_key, phase_id),
        env,
        min_vram_gb=min_vram_gb,
        max_runtime_hours=max_runtime_hours,
        budget_usd=budget_usd,
    )


def _fetch_instance_types(api_key: str, phase_id: str) -> list[Any]:
    response = request(api_key, "GET", "/instances/types", {"available": "true"}, phase_id=phase_id)
    raw_types = response.get("instance_types")
    if not isinstance(raw_types, list):
        raise ShadeformError("malformed instance-types response")
    return raw_types


def price_report(
    api_key: str,
    env: dict[str, str],
    *,
    phase_id: str,
    min_vram_gb: int,
    max_runtime_hours: float,
    budget_usd: float,
) -> dict[str, Any]:
    """Rank candidates with and without the ``SHADEFORM_CLOUD`` allowlist.

    One catalogue fetch, two rankings. A borrowed allowlist that quietly hides
    the cheapest acceptable GPU is a real and expensive mistake -- ours hid a
    $2.50/hr H100 behind a $4.36/hr one -- and the only way to notice is to
    price both and say so out loud.
    """

    raw_types = _fetch_instance_types(api_key, phase_id)
    kwargs = {
        "min_vram_gb": min_vram_gb,
        "max_runtime_hours": max_runtime_hours,
        "budget_usd": budget_usd,
    }
    allowed = _rank_candidates(raw_types, env, **kwargs)
    unfiltered = _rank_candidates(raw_types, {**env, "SHADEFORM_CLOUD": ""}, **kwargs)
    report: dict[str, Any] = {
        "chosen": allowed[0] if allowed else None,
        "candidates": allowed,
        "cheapest_without_cloud_allowlist": unfiltered[0] if unfiltered else None,
        "forgone_usd_per_hour": None,
    }
    if allowed and unfiltered and unfiltered[0].hourly_usd < allowed[0].hourly_usd:
        report["forgone_usd_per_hour"] = round(
            allowed[0].hourly_usd - unfiltered[0].hourly_usd, 4
        )
    return report


def _rank_candidates(
    raw_types: list[Any],
    env: dict[str, str],
    *,
    min_vram_gb: int,
    max_runtime_hours: float,
    budget_usd: float,
) -> list[Candidate]:
    configured = _csv(
        env.get("SHADEFORM_GPU_TYPES", "H100 80GB,H100 SXM,H100 PCIe,H100,A100 80GB")
    )
    cloud_order = [name.lower() for name in _csv(env.get("SHADEFORM_CLOUD", ""))]
    clouds = set(cloud_order)
    cloud_preference = {name: index for index, name in enumerate(cloud_order)}
    # massedcompute reclaims instances out from under a live run (donor SF log).
    # It is often the cheapest quote, which is exactly why it needs a default.
    excluded_clouds = {
        name.lower() for name in _csv(env.get("SHADEFORM_EXCLUDED_CLOUDS", "massedcompute"))
    }
    region = env.get("SHADEFORM_REGION", "").strip()
    gpu_count = int(env.get("SHADEFORM_GPU_COUNT", "1") or "1")
    max_hourly = float(env.get("SHADEFORM_MAX_HOURLY_COST_USD", "4.50") or "4.50")
    candidates: list[Candidate] = []
    for raw in raw_types:
        if not isinstance(raw, dict):
            continue
        gpu = str(raw.get("gpu_type", ""))
        gpu_rank = _gpu_rank(gpu, configured)
        if configured and gpu_rank is None:
            continue
        if int(raw.get("num_gpus", 0) or 0) != gpu_count:
            continue
        cloud = str(raw.get("cloud", ""))
        if clouds and cloud.lower() not in clouds:
            continue
        if cloud.lower() in excluded_clouds:
            continue
        price = _hourly_usd(raw.get("hourly_price"))
        if price is None or price > max_hourly:
            continue
        # Reserve the provider safety backstop, not merely active work time.
        backstop_hours = max(0.25, max_runtime_hours * 1.25)
        if price * backstop_hours > budget_usd:
            continue
        config = raw.get("configuration") if isinstance(raw.get("configuration"), dict) else {}
        vram_value = config.get("vram_per_gpu_in_gb", raw.get("vram_per_gpu_in_gb", 0))
        try:
            vram = int(vram_value)
        except (TypeError, ValueError):
            continue
        if vram < min_vram_gb:
            continue
        availability = raw.get("availability")
        if not isinstance(availability, list):
            continue
        regions = [
            str(item.get("region"))
            for item in availability
            if isinstance(item, dict) and item.get("available") is True and item.get("region")
        ]
        if region:
            regions = [item for item in regions if item == region]
        instance_type = str(raw.get("shade_instance_type", ""))
        if not instance_type:
            continue
        options = config.get("os_options") if isinstance(config.get("os_options"), list) else []
        configured_os = env.get("SHADEFORM_IMAGE", "").strip()
        os_image = configured_os or next(
            (str(item) for item in options if "cuda" in str(item).lower()),
            str(options[0]) if options else "ubuntu22.04_cuda12.2_shade_os",
        )
        interruptible = bool(raw.get("interruptible") or raw.get("is_interruptible"))
        # Short conversion jobs may prefer a lower-cost interruptible offer;
        # long jobs refuse interruption because retry cost dominates.
        if max_runtime_hours > 8 and interruptible:
            continue
        for available_region in regions:
            candidates.append(
                Candidate(
                    gpu=gpu,
                    cloud=cloud,
                    region=available_region,
                    instance_type=instance_type,
                    hourly_usd=price,
                    vram_gb=vram,
                    os_image=os_image,
                    interruptible=interruptible,
                )
            )
    # Price beats cloud preference. The donor ranked cloud first because it was
    # steering away from providers it distrusted; we express distrust in
    # SHADEFORM_EXCLUDED_CLOUDS instead, so the configured cloud order is only a
    # tie-break. Ranking cloud first would have had us pay $4.36/hr for an H100
    # while a $2.50/hr one sat in the same catalogue.
    return sorted(
        candidates,
        key=lambda item: (
            _gpu_rank(item.gpu, configured) if configured else 0,
            item.hourly_usd,
            cloud_preference.get(item.cloud.lower(), len(cloud_preference)),
            item.cloud,
            item.region,
        ),
    )


def ephemeral_key_directory(run_token: str, *, root: Path | None = None) -> Path:
    """Return the owner-private per-run key directory for one run token.

    The token is bound to a single run (the orchestrator uses its run id plus
    the fresh ownership nonce), so no two runs can share a directory and the
    "never reuse a key" rule is enforced by the filesystem: ``create_keypair``
    refuses a directory that already exists.
    """

    if not isinstance(run_token, str) or not _EPHEMERAL_KEY_TOKEN.fullmatch(run_token):
        raise ShadeformError("ephemeral key run token is invalid")
    base = EPHEMERAL_KEY_ROOT if root is None else Path(root)
    if not base.is_absolute():
        raise ShadeformError("ephemeral key root must be absolute")
    return base / run_token


def _private_directory(path: Path, *, create: bool = True) -> None:
    """Create or prove one owner-private ``0700`` directory, never widening it.

    An existing directory is *proved*, never ``chmod``-ed: the protected
    ``.secrets`` layout is operator-owned and a silent permission change would
    hide a misconfiguration rather than surface it.
    """

    if create:
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as exc:
            raise ShadeformError("ephemeral key directory could not be created") from exc
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ShadeformError("ephemeral key directory is unavailable") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ShadeformError("ephemeral key directory is not owner-private")


def create_keypair(directory: Path) -> tuple[Path, str]:
    """Generate one fresh ed25519 key inside a private, never-reused directory.

    ``directory`` must not already exist: an ephemeral key is created once, for
    one run, and its whole directory is securely removed by
    ``destroy_ephemeral_key_directory`` on every exit path.  Every ancestor the
    call creates is ``0700``, every pre-existing ancestor is proved ``0700``,
    and the private half is ``0600`` with a basename the persisted-argv policy
    recognises, so the resulting handle is accepted by
    ``j1m_runner.validate_persisted_argv`` rather than refused at the first
    ``ssh``/``scp`` invocation.
    """

    directory = Path(directory)
    if directory.is_symlink() or directory.exists():
        raise ShadeformError("ephemeral key directory already exists; keys are never reused")
    missing: list[Path] = []
    ancestor = directory.parent
    while not ancestor.exists():
        if ancestor.parent == ancestor:
            raise ShadeformError("ephemeral key directory has no usable ancestor")
        missing.append(ancestor)
        ancestor = ancestor.parent
    _private_directory(ancestor, create=False)
    for parent in reversed(missing):
        _private_directory(parent)
    _private_directory(directory)
    private = directory / EPHEMERAL_KEY_BASENAME
    subprocess.run(
        [_verified_executable("ssh-keygen"), "-t", "ed25519", "-N", "", "-q", "-f", str(private)],
        check=True,
        capture_output=True,
        timeout=30,
        env=_secure_subprocess_env(),
    )
    private.chmod(stat.S_IRUSR | stat.S_IWUSR)
    public = private.with_name(private.name + ".pub").read_text(encoding="utf-8").strip()
    return private, public


def assert_persisted_argv_handle(identity: Path) -> None:
    """Prove one key is usable as the ``-i`` operand of a persisted argv.

    Callers that record their ssh/scp commands as evidence -- the J1M
    orchestrator does, through ``_remote`` -- must prove this *before* any
    billable provider POST exists.  A key the argv policy would refuse is a
    local configuration fault, and discovering it after an instance is running
    means paying for a machine that can never be reached.  Callers that do not
    persist argv (the remote external-tools lane, which still uses a private
    system temporary directory) are unaffected and do not call this.
    """

    from scripts import j1m_runner as _persisted_argv_policy

    if not _persisted_argv_policy._canonical_private_handle_path(str(identity)):
        raise ShadeformError(
            "ephemeral key is not a canonical private handle; every persisted "
            "ssh/scp argv would be refused before it could spawn"
        )


def destroy_ephemeral_key_directory(directory: Path | None) -> dict[str, Any]:
    """Securely remove one per-run key directory; never raise, never print.

    Called from the orchestrator's ``finally`` on both the success and every
    failure path.  Key material is overwritten before unlinking and the result
    is metadata only -- a status, a file count, and the run token -- so the run
    receipt can record that the ephemeral key is gone without naming or
    disclosing any of it.
    """

    if directory is None:
        return {"status": "absent", "files_removed": 0}
    directory = Path(directory)
    # ``run_label``, not ``run_token``: a receipt field whose name ends in
    # ``_token`` is credential-shaped, and ``validate_persisted_output``
    # rejects the whole lifecycle receipt for carrying it -- silently, since
    # both persistence call sites swallow evidence failures so cleanup can
    # never be stranded.  The offline dry run caught it.
    receipt: dict[str, Any] = {"status": "removed", "files_removed": 0, "run_label": directory.name[:96]}
    try:
        if directory.is_symlink():
            os.unlink(directory)
            receipt["status"] = "refused_symlink"
            return receipt
        entries = sorted(os.listdir(directory))
    except FileNotFoundError:
        receipt["status"] = "absent"
        return receipt
    except OSError:
        receipt["status"] = "incomplete"
        receipt["error_type"] = "key_directory_unreadable"
        return receipt
    for name in entries:
        target = directory / name
        try:
            info = os.lstat(target)
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    remaining = os.fstat(descriptor).st_size
                    while remaining > 0:
                        chunk = secrets.token_bytes(min(remaining, 1 << 16))
                        remaining -= os.write(descriptor, chunk)
                    os.fsync(descriptor)
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            os.unlink(target)
            receipt["files_removed"] += 1
        except OSError:
            receipt["status"] = "incomplete"
            receipt["error_type"] = "key_file_not_removed"
    try:
        os.rmdir(directory)
    except OSError:
        receipt["status"] = "incomplete"
        receipt.setdefault("error_type", "key_directory_not_removed")
    return receipt


def create_ephemeral_ssh_key(env: dict[str, str], directory: Path) -> tuple[Path, str]:
    """Generate a fresh per-attempt key; ``SHADEFORM_SSH`` is only an ownership input.

    The borrowed dotenv value can be a provider-style UUID rather than key
    bytes. It is deliberately never interpreted as a path or printed. The
    public half is uploaded as a new provider key and its ID is recorded in the
    nonce-bound ownership record; no pre-existing key ID is reused.
    """

    require_env(env, "SHADEFORM_SSH")
    return create_keypair(directory)


def add_ssh_key(api_key: str, phase_id: str, name: str, public_key: str) -> str:
    # Repeat the launcher's early guard at the shared mutation boundary.
    # Legacy phase-only evidence can appear after caller preflight and cannot
    # safely be associated with this owner. No provider POST may be dispatched
    # once it exists.
    preflight_legacy_deletion_evidence(phase_id)
    try:
        response = request(
            api_key,
            "POST",
            "/sshkeys/add",
            {"name": name, "public_key": public_key},
            phase_id=phase_id,
        )
        # The create response is documented to carry only the ID. Name and
        # public key are verified immediately afterward through
        # /sshkeys/{id}/info.
        return validate_resource_id(response.get("id"), field="created SSH key id")
    except ShadeformHTTPError as exc:
        # A 5xx, timeout-like 4xx, or rate-limit response can be emitted after
        # the provider committed the POST. Reconcile those outcomes by the
        # nonce/fingerprint; ordinary validation/auth 4xx responses are safe
        # definitive rejections.
        if exc.status >= 500 or exc.status in {408, 409, 425, 429}:
            raise AmbiguousProviderOutcome("SSH key create outcome is unknown after provider response") from exc
        raise
    except (ShadeformError, OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        # A timeout or malformed successful response may follow a provider-side
        # key creation. The caller must reconcile by the unique nonce name and
        # public-key fingerprint before it can revoke anything or create an
        # instance; never treat this as a definitive no-op.
        raise AmbiguousProviderOutcome("SSH key create outcome is unknown after transport/schema failure") from exc


def _canonical_public_key(value: object) -> tuple[str, str]:
    """Validate one authorized-key line and ignore only its optional comment."""

    if not isinstance(value, str):
        raise ValueError("SSH public key is not text")
    fields = value.strip().split()
    if len(fields) < 2 or len(fields) > 3:
        raise ValueError("SSH public key has malformed or extra fields")
    algorithm, material = fields[:2]
    if algorithm != "ssh-ed25519":
        raise ValueError("ephemeral key must use ssh-ed25519")
    try:
        base64.b64decode(material.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise ValueError("SSH public key material is not valid base64") from None
    return algorithm, material


def ssh_public_key_fingerprint(value: object) -> str:
    """Return the OpenSSH-style SHA-256 fingerprint input for one key."""
    _algorithm, material = _canonical_public_key(value)
    try:
        decoded = base64.b64decode(material.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise ValueError("SSH public key material is not valid base64") from None
    return base64.b64encode(hashlib.sha256(decoded).digest()).decode("ascii").rstrip("=")


def reconcile_ssh_key(
    api_key: str,
    phase_id: str,
    *,
    expected_name: str,
    expected_public_key: str | None = None,
    expected_fingerprint: str | None = None,
    deadline: float | None = None,
) -> str:
    """Reconcile one ambiguous key create, then return only an exact unique ID.

    The list is bounded and used only to identify a key with both the nonce-bound
    name and the expected public-key fingerprint. Any zero or multiple matches
    remains unresolved; no broad delete or instance create is permitted.
    """
    if expected_public_key is not None:
        calculated_fingerprint = ssh_public_key_fingerprint(expected_public_key)
        if expected_fingerprint is not None and expected_fingerprint != calculated_fingerprint:
            raise ValueError("SSH key fingerprint binding mismatch")
        expected_fingerprint = calculated_fingerprint
    if expected_fingerprint is None or re.fullmatch(r"[A-Za-z0-9+/]{43}", expected_fingerprint) is None:
        raise ValueError("SSH key reconciliation requires a bounded fingerprint")
    timeout = 90.0 if deadline is None else min(90.0, deadline - time.monotonic())
    if timeout < 1.0:
        raise TimeoutError("SSH key reconciliation deadline exhausted")
    response = request(api_key, "GET", "/sshkeys", phase_id=phase_id, timeout=timeout)
    entries = response.get("ssh_keys") if isinstance(response, dict) else None
    if not isinstance(entries, list) or len(entries) > 256:
        raise AmbiguousProviderOutcome("SSH key reconciliation response is unavailable or unbounded")
    matches: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise MalformedProviderResponse("SSH key reconciliation entry is malformed")
        if entry.get("name") != expected_name:
            continue
        provider_key = entry.get("public_key")
        try:
            fingerprint = ssh_public_key_fingerprint(provider_key)
            key_id = validate_resource_id(entry.get("id"), field="reconciled SSH key id")
        except (TypeError, ValueError) as exc:
            raise AmbiguousProviderOutcome("matching SSH key identity is malformed") from exc
        if fingerprint == expected_fingerprint:
            matches.append(key_id)
    if len(matches) != 1:
        raise AmbiguousProviderOutcome("SSH key reconciliation did not identify exactly one nonce-bound key")
    if expected_public_key is not None:
        verify_timeout = 90.0 if deadline is None else min(90.0, deadline - time.monotonic())
        if verify_timeout < 1.0:
            raise TimeoutError("SSH key reconciliation verification deadline exhausted")
        verify_ssh_key_ownership(
            api_key, phase_id, matches[0], expected_name=expected_name,
            expected_public_key=expected_public_key, timeout=verify_timeout,
        )
    return matches[0]


def _delete_ssh_key_once(api_key: str, phase_id: str, key_id: str, *, deadline: float | None = None) -> dict[str, Any]:
    """Issue the transport request only; callers need durable reconciliation."""

    exact = validate_resource_id(key_id, field="SSH key id")
    timeout = min(90.0, deadline - time.monotonic()) if deadline is not None else 90.0
    if timeout < 1.0:
        raise TimeoutError("SSH key deletion deadline exhausted")
    try:
        response = request(api_key, "POST", f"/sshkeys/{exact}/delete", phase_id=phase_id, timeout=timeout)
        return _reject_provider_failure(response, "SSH key deletion")
    except ShadeformHTTPError as exc:
        if exc.status == 404:
            return {"already_absent": True}
        raise


def verify_ssh_key_ownership(api_key: str, phase_id: str, key_id: str, *, expected_name: str, expected_public_key: str, timeout: float = 90) -> dict[str, Any]:
    """Verify the exact newly-created key through its provider info endpoint."""

    exact = validate_resource_id(key_id, field="SSH key id")
    info = request(api_key, "GET", f"/sshkeys/{exact}/info", phase_id=phase_id, timeout=timeout)
    if validate_resource_id(info.get("id"), field="SSH key info id") != exact:
        raise ShadeformError("provider returned a different SSH key ID")
    try:
        provider_key = _canonical_public_key(info.get("public_key"))
        expected_key = _canonical_public_key(expected_public_key)
    except ValueError as exc:
        raise ShadeformError(f"provider SSH key is malformed: {exc}") from None
    if info.get("name") != expected_name or provider_key != expected_key:
        raise ShadeformError("provider SSH key info does not match this ephemeral ownership record")
    return info


def verify_ssh_key_fingerprint(
    api_key: str,
    phase_id: str,
    key_id: str,
    *,
    expected_name: str,
    expected_fingerprint: str,
    timeout: float = 90,
) -> dict[str, Any]:
    """Verify key identity immediately before revocation when private material is unavailable."""

    exact = validate_resource_id(key_id, field="SSH key id")
    if re.fullmatch(r"[A-Za-z0-9+/]{43}", expected_fingerprint) is None:
        raise ValueError("expected SSH key fingerprint is malformed")
    info = request(api_key, "GET", f"/sshkeys/{exact}/info", phase_id=phase_id, timeout=timeout)
    if validate_resource_id(info.get("id"), field="SSH key info id") != exact or info.get("name") != expected_name:
        raise ShadeformError("provider SSH key identity does not match the deletion record")
    actual = ssh_public_key_fingerprint(info.get("public_key"))
    if actual != expected_fingerprint:
        raise ShadeformError("provider SSH key fingerprint does not match the deletion record")
    return info


def _auto_delete(env: dict[str, str], runtime_hours: float) -> dict[str, str]:
    """Provider-side backstop, sized per run at duration + 25%.

    This deliberately overrides the dotenv ``SHADEFORM_AUTO_TERMINATE_HOURS``
    default in both directions: our jobs are minutes, and a 2.5-hour backstop on
    a twelve-minute job is eleven-twelfths of a bill nobody meant to pay. The
    dotenv value remains the ceiling below.

    That ``min`` used to truncate silently, and on 2026-09-03 it set an
    insufficient backstop on an eight-hour run (EP-014). The provider deleted the instance at
    2.08 h with 239,088 records held in memory on it, and every projection
    computed that day was measured against a deadline the instance could not
    reach. A ceiling written as a spending guard had become a limit on how long
    a job was permitted to be, and nothing compared the two numbers.

    So a ceiling shorter than the work is now refused here rather than applied.
    This is the single point every creation passes through, which is the same
    reason the ownership nonce lives at this level: a guard one path can miss is
    a guard.
    """

    # Keep the packaged default above the 1.25x provider backstop for the
    # longest supported model-evaluation run (2.425h). Operators may set a
    # stricter ceiling explicitly, in which case the guard below refuses it.
    if (isinstance(runtime_hours, bool) or not isinstance(runtime_hours, (int, float)) or
            not math.isfinite(float(runtime_hours)) or runtime_hours <= 0):
        raise BackstopError("runtime must be finite and positive before provider mutation")
    ceiling = configured_auto_terminate_hours(env)
    hours = min(max(0.25, runtime_hours * 1.25), max(0.25, ceiling))
    # Assert the EFFECTIVE backstop against the run, not the ceiling against the
    # run. Those differ exactly where it matters: at runtime == ceiling the
    # cruder test passes, and the backstop then coincides with the cap, so any
    # provisioning or setup overhead makes the provider win the race. A
    # 119-minute config would launch with 36 seconds of margin -- EP-014 again,
    # at the one point where the two clocks are equal rather than unequal.
    if runtime_hours > 0 and hours < runtime_hours * MIN_BACKSTOP_MARGIN:
        truncated = "" if hours >= runtime_hours * 1.25 - 1e-9 else (
            f" The ceiling has already truncated the intended 1.25x headroom to "
            f"{hours / runtime_hours:.2f}x."
        )
        raise BackstopError(
            f"the provider backstop would fire {hours:g} h into a {runtime_hours:g} h "
            f"run, leaving {hours / runtime_hours:.2f}x headroom against a required "
            f"{MIN_BACKSTOP_MARGIN:g}x.{truncated} The instance would be deleted with "
            f"nothing banked -- a conversion writes its artifacts only after the whole "
            f"config completes. Raise SHADEFORM_AUTO_TERMINATE_HOURS deliberately "
            f"(it is a standing safety limit, so this is a decision, not a knob to "
            f"turn to make a run fit), or shorten the run."
        )
    try:
        max_total = float(env.get("SHADEFORM_MAX_TOTAL_COST_USD", "50") or "50")
    except (TypeError, ValueError) as exc:
        raise BackstopError("SHADEFORM_MAX_TOTAL_COST_USD must be finite and positive") from exc
    if not math.isfinite(max_total) or max_total <= 0:
        raise BackstopError("SHADEFORM_MAX_TOTAL_COST_USD must be finite and positive")
    return {
        "date_threshold": (utc_now() + timedelta(hours=hours)).isoformat(),
        "spend_threshold": f"{max(1.0, max_total):.2f}",
    }


def create_instance(
    api_key: str,
    env: dict[str, str],
    *,
    phase_id: str,
    run_id: str,
    candidate: Candidate,
    ssh_key_id: str,
    nonce: str,
    max_runtime_hours: float,
    auto_delete_contract: dict[str, str] | None = None,
) -> str:
    if read_owned_resource(phase_id) is not None:
        raise ShadeformError("phase already owns a recorded instance; reuse or clean it first")
    validate_nonce(nonce)
    name = owned_instance_name(run_id, nonce)
    auto_delete = (
        _auto_delete(env, max_runtime_hours)
        if auto_delete_contract is None
        else auto_delete_contract
    )
    if not isinstance(auto_delete, dict) or set(auto_delete) != {"date_threshold", "spend_threshold"}:
        raise ShadeformError("provider auto-delete contract is invalid")
    try:
        provider_deadline = datetime.fromisoformat(auto_delete["date_threshold"])
    except (TypeError, ValueError) as exc:
        raise ShadeformError("provider auto-delete deadline is invalid") from exc
    if (
        provider_deadline.tzinfo is None
        or provider_deadline.utcoffset() is None
        or provider_deadline <= utc_now()
        or not isinstance(auto_delete["spend_threshold"], str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", auto_delete["spend_threshold"]) is None
    ):
        raise ShadeformError("provider auto-delete contract is invalid")
    payload = {
        "cloud": candidate.cloud,
        "region": candidate.region,
        "shade_instance_type": candidate.instance_type,
        "shade_cloud": True,
        "name": name,
        "os": candidate.os_image,
        "ssh_key_id": validate_resource_id(ssh_key_id, field="SSH key id"),
        "tags": [
            "local-bmo-j1m",
            f"ep-phase-{phase_id}",
            f"ep-run-{nonce}",
        ],
        "auto_delete": dict(auto_delete),
    }
    try:
        # This is the final shared boundary before the instance POST.  Keep
        # the earlier caller check for fail-fast behavior, but repeat it here
        # so evidence introduced between caller validation and dispatch stops
        # the mutation.
        preflight_legacy_deletion_evidence(phase_id)
        response = request(
            api_key, "POST", "/instances/create", payload, phase_id=phase_id, timeout=180
        )
    except ShadeformHTTPError as exc:
        # A 5xx response is not proof that the provider did not commit the
        # create.  Keep the durable nonce reservation pending so a later
        # exact reconciliation can inspect only this name/nonce; never allow
        # the caller to treat it as a safe, definitive rejection.
        if exc.status >= 500 or exc.status in {408, 409, 425, 429}:
            raise AmbiguousProviderOutcome("instance create outcome is unknown after a possibly committed provider response") from exc
        raise
    except (ShadeformError, TimeoutError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        # The request wrapper deliberately preserves its transport cause. Only
        # this create boundary converts that into an unknown POST outcome.
        cause = exc.__cause__
        if isinstance(exc, (MalformedProviderResponse, json.JSONDecodeError)) or isinstance(exc, AmbiguousProviderOutcome) or isinstance(
            exc, (TimeoutError, OSError, urllib.error.URLError)
        ) or isinstance(cause, (TimeoutError, OSError, urllib.error.URLError)):
            raise AmbiguousProviderOutcome("instance create outcome is unknown after transport failure") from exc
        raise
    try:
        instance_id = response.get("id") if isinstance(response, dict) else None
        return validate_resource_id(instance_id, field="created instance id")
    except (TypeError, ValueError) as exc:
        # A successful HTTP response with malformed JSON/schema may still have
        # created a resource; preserve the nonce/key incident and pending cap.
        raise AmbiguousProviderOutcome("instance create returned an unusable success response") from exc


def owned_instance_name(run_id: str, nonce: str) -> str:
    validate_nonce(nonce)
    safe_run = re.sub(r"[^a-z0-9-]", "-", run_id.lower()).strip("-")[:14] or "run"
    return f"ep-{safe_run}-{nonce}"


def reconcile_instance_by_nonce(
    api_key: str, phase_id: str, *, expected_name: str, nonce: str,
    ssh_key_id: str | None = None, expected_cloud: str | None = None,
    expected_region: str | None = None, expected_instance_type: str | None = None,
    expected_hourly_usd: float | None = None, expected_gpu: str | None = None,
    expected_gpu_count: int | None = None, expected_vram_gb: int | None = None,
    expected_os_image: str | None = None,
    allow_absent: bool = False,
    deadline: float | None = None,
) -> str | None:
    """Find exactly one instance with the phase/name/nonce ownership tuple.

    The provider endpoint is account-scoped, so the query is deliberately
    narrow and the response is still filtered locally before any deletion.
    Zero, duplicate, malformed, or unsupported results remain ambiguous.
    """

    validate_phase_id(phase_id)
    validate_nonce(nonce)
    if not isinstance(expected_name, str) or len(expected_name) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", expected_name) is None:
        raise ValueError("invalid expected instance name")
    if any(value is None for value in (
        ssh_key_id, expected_cloud, expected_region, expected_instance_type,
        expected_hourly_usd, expected_gpu, expected_gpu_count, expected_vram_gb,
        expected_os_image,
    )):
        raise ValueError("exact instance reconciliation requires the complete approved profile")
    timeout = 90.0 if deadline is None else min(90.0, deadline - time.monotonic())
    if timeout < 1.0:
        raise TimeoutError("instance reconciliation deadline exhausted")
    response = request(
        api_key, "GET", "/instances",
        {"name": expected_name, "tag": f"ep-run-{nonce}"},
        phase_id=phase_id, timeout=timeout,
    )
    if not isinstance(response, dict):
        raise AmbiguousProviderOutcome("instance reconciliation response is malformed")
    entries = response.get("instances", response.get("data", response.get("value")))
    if not isinstance(entries, list) or len(entries) > 256:
        raise AmbiguousProviderOutcome("instance reconciliation response is unavailable or unbounded")
    matches: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise AmbiguousProviderOutcome("instance reconciliation entry is malformed")
        if entry.get("name") != expected_name:
            continue
        tags = entry.get("tags")
        if not isinstance(tags, list) or len(tags) > 32 or f"ep-run-{nonce}" not in tags or f"ep-phase-{phase_id}" not in tags:
            continue
        try:
            matches.append(validate_resource_id(entry.get("id"), field="reconciled instance id"))
        except (TypeError, ValueError) as exc:
            raise AmbiguousProviderOutcome("matching instance identity is malformed") from exc
    if not matches and allow_absent:
        return None
    if len(matches) != 1:
        raise AmbiguousProviderOutcome("instance reconciliation did not identify exactly one nonce-bound instance")
    # A list response is only a locator.  Before deletion, bind the exact ID
    # through the provider's authoritative info response and the full approved
    # profile; never delete from a name/tag match alone.
    info = instance_info(
        api_key,
        phase_id,
        matches[0],
        timeout=90.0 if deadline is None else min(90.0, deadline - time.monotonic()),
    )
    verify_instance_ownership(
        info, instance_id=matches[0], phase_id=phase_id, nonce=nonce,
        expected_name=expected_name, ssh_key_id=ssh_key_id,
        expected_cloud=expected_cloud, expected_region=expected_region,
        expected_instance_type=expected_instance_type,
        expected_hourly_usd=expected_hourly_usd, expected_gpu=expected_gpu,
        expected_gpu_count=expected_gpu_count, expected_vram_gb=expected_vram_gb,
        expected_os_image=expected_os_image,
    )
    return matches[0]


def instance_info(api_key: str, phase_id: str, instance_id: str, *, timeout: float = 90) -> dict[str, Any]:
    exact = validate_resource_id(instance_id, field="instance id")
    info = request(api_key, "GET", f"/instances/{exact}/info", phase_id=phase_id, timeout=timeout)
    if validate_resource_id(info.get("id"), field="instance info id") != exact:
        raise ShadeformError("provider returned information for a different instance")
    return info


def verify_instance_ownership(
    info: dict[str, Any],
    *,
    instance_id: str,
    phase_id: str,
    nonce: str,
    expected_name: str | None = None,
    ssh_key_id: str | None = None,
    expected_cloud: str | None = None,
    expected_region: str | None = None,
    expected_instance_type: str | None = None,
    expected_hourly_usd: float | None = None,
    expected_gpu: str | None = None,
    expected_gpu_count: int | None = None,
    expected_vram_gb: int | None = None,
    expected_os_image: str | None = None,
) -> dict[str, Any]:
    """Require provider-returned identity/tags before opening SSH or HF custody."""

    exact = validate_resource_id(instance_id, field="instance id")
    validate_phase_id(phase_id)
    validate_nonce(nonce)
    if validate_resource_id(info.get("id"), field="instance info id") != exact:
        raise ShadeformError("provider ownership response has a different instance ID")
    name = info.get("name")
    if not isinstance(name, str) or nonce not in name or (expected_name is not None and name != expected_name):
        raise ShadeformError("provider ownership response has no exact nonce-bound instance name")
    tags = info.get("tags")
    if not isinstance(tags, list):
        raise ShadeformError("provider ownership response has no verifiable tags")
    tag_set = {str(tag) for tag in tags}
    required = {"local-bmo-j1m", f"ep-phase-{phase_id}", f"ep-run-{nonce}"}
    if not required.issubset(tag_set):
        raise ShadeformError("provider ownership tags do not match this phase and nonce")
    if ssh_key_id is not None and validate_resource_id(info.get("ssh_key_id"), field="instance SSH key id") != validate_resource_id(ssh_key_id, field="expected SSH key id"):
        raise ShadeformError("provider instance is attached to a different SSH key")
    if expected_cloud is not None and str(info.get("cloud", "")).lower() != expected_cloud.lower():
        raise ShadeformError("provider instance cloud does not match the approved candidate")
    if expected_region is not None and str(info.get("region", "")).lower() != expected_region.lower():
        raise ShadeformError("provider instance region does not match the approved candidate")
    if expected_instance_type is not None and info.get("shade_instance_type") != expected_instance_type:
        raise ShadeformError("provider instance type does not match the approved candidate")
    configuration = info.get("configuration")
    if any(value is not None for value in (expected_gpu, expected_gpu_count, expected_vram_gb, expected_os_image)) and not isinstance(configuration, dict):
        raise ShadeformError("provider instance has no exact hardware configuration")
    if not isinstance(configuration, dict):
        configuration = {}
    if expected_gpu is not None and configuration.get("gpu_type") != expected_gpu:
        raise ShadeformError("provider GPU type does not match the approved candidate")
    if expected_gpu_count is not None and configuration.get("num_gpus") != expected_gpu_count:
        raise ShadeformError("provider GPU count does not match the approved candidate")
    if expected_vram_gb is not None and configuration.get("vram_per_gpu_in_gb") != expected_vram_gb:
        raise ShadeformError("provider VRAM does not match the approved candidate")
    if expected_os_image is not None and configuration.get("os") != expected_os_image:
        raise ShadeformError("provider OS image does not match the approved candidate")
    if expected_hourly_usd is not None and info.get("hourly_price") is not None:
        try:
            if abs(float(info["hourly_price"]) / 100.0 - expected_hourly_usd) > 1e-6:
                raise ShadeformError("provider hourly cents price does not match the approved candidate")
        except (TypeError, ValueError):
            raise ShadeformError("provider hourly price is not a valid cents value") from None
    elif expected_hourly_usd is not None:
        raise ShadeformError("provider hourly cents price is missing")
    return info


def verify_owned_instance_before_delete(
    api_key: str,
    phase_id: str,
    record: OwnedResource,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Rebind a phase-owned record to authoritative provider identity before delete."""

    _validate_owned_resource(record)
    if record.gpu_count is None or record.vram_gb is None or record.os_image is None or record.instance_type is None:
        raise ShadeformError("owned record lacks the complete deletion profile")
    timeout = 90.0 if deadline is None else min(90.0, deadline - time.monotonic())
    if timeout < 1.0:
        raise TimeoutError("instance ownership verification deadline exhausted")
    info = instance_info(api_key, phase_id, record.instance_id, timeout=timeout)
    return verify_instance_ownership(
        info,
        instance_id=record.instance_id,
        phase_id=phase_id,
        nonce=record.ownership_nonce,
        expected_name=record.instance_name or owned_instance_name(record.run_id, record.ownership_nonce),
        ssh_key_id=record.ssh_key_id,
        expected_cloud=record.cloud,
        expected_region=record.region,
        expected_instance_type=record.instance_type,
        expected_hourly_usd=record.hourly_usd,
        expected_gpu=record.gpu,
        expected_gpu_count=record.gpu_count,
        expected_vram_gb=record.vram_gb,
        expected_os_image=record.os_image,
    )


def verify_owned_ssh_key_before_delete(
    api_key: str,
    phase_id: str,
    record: OwnedResource,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    _validate_owned_resource(record)
    timeout = 90.0 if deadline is None else min(90.0, deadline - time.monotonic())
    if timeout < 1.0:
        raise TimeoutError("SSH key ownership verification deadline exhausted")
    if record.ssh_public_key is not None:
        return verify_ssh_key_ownership(
            api_key,
            phase_id,
            record.ssh_key_id,
            expected_name=record.ssh_key_name,
            expected_public_key=record.ssh_public_key,
            timeout=timeout,
        )
    if record.ssh_public_key_fingerprint is not None:
        return verify_ssh_key_fingerprint(
            api_key,
            phase_id,
            record.ssh_key_id,
            expected_name=record.ssh_key_name,
            expected_fingerprint=record.ssh_public_key_fingerprint,
            timeout=timeout,
        )
    raise ShadeformError("owned record lacks SSH key material for deletion proof")


def _ssh_key_delete_owner(
    phase_id: str,
    ownership_nonce: str,
    ssh_key_id: str,
    *,
    expected_name: str,
    expected_public_key: str | None,
    expected_fingerprint: str | None,
    record: OwnedResource | None,
) -> dict[str, str]:
    """Reconstruct one exact key owner from durable local authority."""

    phase = validate_phase_id(phase_id)
    nonce = validate_nonce(ownership_nonce)
    key_id = validate_resource_id(ssh_key_id, field="SSH key id")
    if (
        not isinstance(expected_name, str)
        or not expected_name
        or len(expected_name) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in expected_name)
    ):
        raise ValueError("expected SSH key name is invalid")
    calculated_fingerprint: str | None = None
    public_key_sha256: str | None = None
    if expected_public_key is not None:
        calculated_fingerprint = ssh_public_key_fingerprint(expected_public_key)
        public_key_sha256 = hashlib.sha256(expected_public_key.encode("utf-8")).hexdigest()
        if expected_fingerprint is not None and expected_fingerprint != calculated_fingerprint:
            raise ShadeformError("SSH key deletion fingerprint binding changed")
    fingerprint = expected_fingerprint or calculated_fingerprint
    if not isinstance(fingerprint, str) or re.fullmatch(r"[A-Za-z0-9+/]{43}", fingerprint) is None:
        raise ValueError("SSH key deletion requires an exact fingerprint")

    if record is not None:
        _validate_owned_resource(record)
        if (
            record.phase_id != phase
            or record.ownership_nonce != nonce
            or record.ssh_key_id != key_id
            or record.ssh_key_name != expected_name
        ):
            raise ShadeformError("owned record does not match the SSH key delete owner")
        durable_record, _ = read_phase_ownership(phase)
        if durable_record is None or asdict(durable_record) != asdict(record):
            raise ShadeformError("SSH key delete owner is not durably recorded")

    # The enriched pre-create reservation is the common durable authority for
    # normal, recovery, watchdog, and key-only cleanup paths.
    try:
        events = cost_ledger_events()
    except FileNotFoundError as exc:
        raise ShadeformError("SSH key deletion cost authority is absent") from exc
    attempt_events = [
        event for event in events
        if event.get("event_kind") != "genesis"
        and event.get("phase_id") == phase
        and event.get("ownership_nonce") == nonce
        and event.get("instance_id") == f"attempt-{nonce}"
    ]
    if any(
        (event.get("ssh_key_id") is not None and event.get("ssh_key_id") != key_id)
        or (event.get("ssh_key_name") is not None and event.get("ssh_key_name") != expected_name)
        or (
            event.get("ssh_public_key_fingerprint") is not None
            and event.get("ssh_public_key_fingerprint") != fingerprint
        )
        for event in attempt_events
    ):
        raise ShadeformError("SSH key deletion reservation history changed owner")
    matching = [
        event for event in attempt_events
        if event.get("reservation") == "pre-create-attempt"
        and event.get("ssh_key_name") == expected_name
        and event.get("ssh_public_key_fingerprint") == fingerprint
    ]
    if not matching or any(
        event.get("ssh_key_id") not in {None, key_id} for event in matching
    ):
        raise ShadeformError("SSH key deletion lacks an exact reservation binding")
    reserved_public_digests = {
        event.get("ssh_public_key_sha256") for event in matching
    }
    if len(reserved_public_digests) != 1:
        raise ShadeformError("SSH key deletion reservation public-key binding changed")
    reserved_public_sha = next(iter(reserved_public_digests))
    if not isinstance(reserved_public_sha, str) or re.fullmatch(r"[0-9a-f]{64}", reserved_public_sha) is None:
        raise ShadeformError("SSH key deletion reservation lacks its public-key digest")
    if public_key_sha256 is not None and public_key_sha256 != reserved_public_sha:
        raise ShadeformError("SSH key deletion public-key binding changed")
    return {
        "phase_id": phase,
        "ownership_nonce": nonce,
        "ssh_key_id": key_id,
        "ssh_key_name": expected_name,
        "ssh_public_key_fingerprint": fingerprint,
        "ssh_public_key_sha256": reserved_public_sha,
    }


def _ssh_key_delete_evidence_path(owner: dict[str, str], kind: str) -> Path:
    if kind not in {"intent", "confirmation"}:
        raise ValueError("invalid SSH key deletion evidence kind")
    digest = hashlib.sha256(
        json.dumps(owner, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RUNTIME_ROOT / f"{owner['phase_id']}.{digest}.ssh-key-deletion-{kind}.json"


def _canonical_ssh_key_delete_evidence(
    payload: dict[str, Any], *, owner: dict[str, str], kind: str,
) -> dict[str, Any]:
    schema = (
        SSH_KEY_DELETE_INTENT_SCHEMA
        if kind == "intent"
        else SSH_KEY_DELETE_CONFIRMATION_SCHEMA
    )
    fields = {"schema", "owner", "status", "dispatched_at_utc"}
    if kind == "confirmation":
        fields |= {"confirmed_at_utc", "evidence"}
    if set(payload) != fields or payload.get("schema") != schema:
        raise ShadeformError(f"SSH key deletion {kind} schema is invalid")
    if payload.get("owner") != owner:
        raise ShadeformError(f"SSH key deletion {kind} owner binding is invalid")
    expected_status = "dispatched" if kind == "intent" else "confirmed"
    if payload.get("status") != expected_status:
        raise ShadeformError(f"SSH key deletion {kind} status is invalid")
    for field in ("dispatched_at_utc", "confirmed_at_utc"):
        if field not in payload:
            continue
        value = payload.get(field)
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ShadeformError(f"SSH key deletion {kind} timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ShadeformError(f"SSH key deletion {kind} timestamp is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
            raise ShadeformError(f"SSH key deletion {kind} timestamp is invalid")
    if kind == "confirmation":
        if payload.get("evidence") not in {"provider-404", "provider-deleted"}:
            raise ShadeformError("SSH key deletion confirmation evidence is invalid")
        if datetime.fromisoformat(payload["confirmed_at_utc"]) < datetime.fromisoformat(
            payload["dispatched_at_utc"]
        ):
            raise ShadeformError("SSH key deletion confirmation predates dispatch")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_SSH_KEY_DELETE_EVIDENCE_BYTES:
        raise ShadeformError(f"SSH key deletion {kind} exceeds its byte bound")
    return dict(payload)


def _read_ssh_key_delete_evidence(
    owner: dict[str, str], kind: str,
) -> dict[str, Any] | None:
    path = _ssh_key_delete_evidence_path(owner, kind)
    try:
        data = private_bounded_stable_bytes(
            path, MAX_SSH_KEY_DELETE_EVIDENCE_BYTES,
            label=f"SSH key deletion {kind}",
        )
    except FileNotFoundError:
        return None
    payload = strict_json_object(data, label=f"SSH key deletion {kind}")
    return _canonical_ssh_key_delete_evidence(payload, owner=owner, kind=kind)


def _create_ssh_key_delete_evidence(
    owner: dict[str, str], kind: str, payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    canonical = _canonical_ssh_key_delete_evidence(payload, owner=owner, kind=kind)
    data = (json.dumps(canonical, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        private_durable_create_new(
            _ssh_key_delete_evidence_path(owner, kind), data,
            label=f"SSH key deletion {kind}",
        )
        return canonical, True
    except FileExistsError:
        existing = _read_ssh_key_delete_evidence(owner, kind)
        if existing is None:
            raise ShadeformError(f"SSH key deletion {kind} raced publication")
        if kind == "intent":
            # A timestamp chosen by another exact-owner actor is authoritative.
            return existing, False
        if existing != canonical:
            raise ShadeformError("SSH key deletion confirmation is immutable")
        return existing, False


def _remaining_key_delete_timeout(deadline: float | None) -> float:
    timeout = 90.0 if deadline is None else min(90.0, deadline - time.monotonic())
    if timeout < 1.0:
        raise TimeoutError("SSH key deletion deadline exhausted")
    return timeout


def _exact_ssh_key_provider_state(
    api_key: str,
    owner: dict[str, str],
    *,
    expected_public_key: str | None,
    deadline: float | None,
) -> tuple[str, str | None]:
    try:
        if expected_public_key is not None:
            info = verify_ssh_key_ownership(
                api_key,
                owner["phase_id"],
                owner["ssh_key_id"],
                expected_name=owner["ssh_key_name"],
                expected_public_key=expected_public_key,
                timeout=_remaining_key_delete_timeout(deadline),
            )
        else:
            info = verify_ssh_key_fingerprint(
                api_key,
                owner["phase_id"],
                owner["ssh_key_id"],
                expected_name=owner["ssh_key_name"],
                expected_fingerprint=owner["ssh_public_key_fingerprint"],
                timeout=_remaining_key_delete_timeout(deadline),
            )
    except ShadeformHTTPError as exc:
        if exc.status == 404:
            return "absent", "provider-404"
        raise
    if info.get("status") == "deleted":
        return "absent", "provider-deleted"
    return "present", None


def delete_owned_ssh_key_exact(
    api_key: str,
    phase_id: str,
    ssh_key_id: str,
    *,
    ownership_nonce: str,
    expected_name: str,
    expected_public_key: str | None = None,
    expected_fingerprint: str | None = None,
    record: OwnedResource | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Delete one exact owned key through a durable, no-replay state machine."""

    owner = _ssh_key_delete_owner(
        phase_id,
        ownership_nonce,
        ssh_key_id,
        expected_name=expected_name,
        expected_public_key=expected_public_key,
        expected_fingerprint=expected_fingerprint,
        record=record,
    )
    confirmation = _read_ssh_key_delete_evidence(owner, "confirmation")
    if confirmation is not None:
        intent = _read_ssh_key_delete_evidence(owner, "intent")
        if intent is None or confirmation["dispatched_at_utc"] != intent["dispatched_at_utc"]:
            raise ShadeformError("SSH key deletion confirmation lacks its exact intent")
        return {"status": "confirmed", "evidence": confirmation["evidence"]}

    intent = _read_ssh_key_delete_evidence(owner, "intent")
    if intent is None:
        state, _ = _exact_ssh_key_provider_state(
            api_key, owner, expected_public_key=expected_public_key, deadline=deadline,
        )
        if state != "present":
            raise ShadeformError("SSH key absence without a durable intent requires manual recovery")
        requested_intent = {
            "schema": SSH_KEY_DELETE_INTENT_SCHEMA,
            "owner": owner,
            "status": "dispatched",
            "dispatched_at_utc": utc_now().isoformat(),
        }
        intent, claimed = _create_ssh_key_delete_evidence(
            owner, "intent", requested_intent,
        )
        if claimed:
            _delete_ssh_key_once(
                api_key, owner["phase_id"], owner["ssh_key_id"], deadline=deadline,
            )

    # Once the intent exists, every caller queries first and never reissues the
    # DELETE.  Accepted-but-present/deleting and transport ambiguity therefore
    # retain exact recovery evidence and ownership for a later retry.
    state, evidence = _exact_ssh_key_provider_state(
        api_key, owner, expected_public_key=expected_public_key, deadline=deadline,
    )
    if state != "absent" or evidence is None:
        raise AmbiguousProviderOutcome(
            "SSH key deletion is not yet authoritatively confirmed"
        )
    confirmed = {
        "schema": SSH_KEY_DELETE_CONFIRMATION_SCHEMA,
        "owner": owner,
        "status": "confirmed",
        "dispatched_at_utc": intent["dispatched_at_utc"],
        "confirmed_at_utc": utc_now().isoformat(),
        "evidence": evidence,
    }
    confirmation, _ = _create_ssh_key_delete_evidence(
        owner, "confirmation", confirmed,
    )
    return {"status": "confirmed", "evidence": confirmation["evidence"]}


def ssh_key_deletion_is_confirmed(
    phase_id: str,
    ssh_key_id: str,
    *,
    ownership_nonce: str,
    expected_name: str,
    expected_fingerprint: str,
) -> bool:
    """Validate retained exact-owner key intent/confirmation without a request."""

    owner = _ssh_key_delete_owner(
        phase_id,
        ownership_nonce,
        ssh_key_id,
        expected_name=expected_name,
        expected_public_key=None,
        expected_fingerprint=expected_fingerprint,
        record=None,
    )
    intent = _read_ssh_key_delete_evidence(owner, "intent")
    confirmation = _read_ssh_key_delete_evidence(owner, "confirmation")
    return bool(
        intent is not None
        and confirmation is not None
        and confirmation["dispatched_at_utc"] == intent["dispatched_at_utc"]
    )


def wait_active(
    api_key: str,
    phase_id: str,
    instance_id: str,
    *,
    timeout_seconds: int = 1800,
    on_status: "Callable[[str], None] | None" = None,
) -> dict[str, Any]:
    """Wait for one exact instance to reach ``active`` with a complete SSH endpoint.

    ``pending_provider`` can persist for a very long time (donor SF-063 onward
    logged dozens of consecutive polls on one Scaleway H100). The timeout is the
    only thing that turns that into a decision instead of a bill.
    """

    deadline = time.monotonic() + timeout_seconds
    last = "unknown"
    while time.monotonic() < deadline:
        info = instance_info(api_key, phase_id, instance_id)
        status = str(info.get("status", "unknown"))
        if status != last and on_status is not None:
            # `pending_provider` can persist for many minutes. Surfacing it
            # stops a healthy wait from looking like a stalled one.
            on_status(status)
        last = status
        if last == "active" and all(
            info.get(key) not in {None, ""} for key in ("ip", "ssh_user", "ssh_port")
        ):
            return info
        if last in {"error", "deleting", "deleted"}:
            raise ShadeformError(f"instance entered {last}: {info.get('status_details')}")
        time.sleep(15)
    raise TimeoutError(f"instance did not become active; last status={last}")


def _delete_instance(api_key: str, phase_id: str, instance_id: str, *, deadline: float | None = None) -> dict[str, Any]:
    """Delete one exact instance and confirm it is gone. 404 counts as gone."""

    exact = validate_resource_id(instance_id, field="instance id")
    def remaining() -> float:
        return deadline - time.monotonic() if deadline is not None else float("inf")

    request_timeout = min(90.0, remaining())
    if request_timeout < 1.0:
        return {"success": False, "instance_id": exact, "error_type": "deletion_deadline_exhausted"}
    try:
        response = _reject_provider_failure(
            request(api_key, "POST", f"/instances/{exact}/delete", phase_id=phase_id, timeout=request_timeout),
            "instance deletion",
        )
    except ShadeformHTTPError as exc:
        if exc.status == 404:
            return {"success": True, "already_absent": True}
        raise
    poll_deadline = min(time.monotonic() + 240.0, deadline) if deadline is not None else time.monotonic() + 240.0
    while time.monotonic() < poll_deadline:
        request_timeout = min(90.0, poll_deadline - time.monotonic())
        if request_timeout < 1.0:
            break
        try:
            info = instance_info(api_key, phase_id, exact, timeout=request_timeout)
        except ShadeformHTTPError as exc:
            if exc.status == 404:
                return {"success": True, "response": response, "status": "absent"}
            raise
        if info.get("status") == "deleted":
            return {"success": True, "response": response, "status": "deleted"}
        sleep_for = min(5.0, poll_deadline - time.monotonic())
        if sleep_for < 0.1:
            break
        time.sleep(sleep_for)
    return {"success": False, "response": response, "instance_id": exact}


def _ssh_config_path(path: Path) -> str:
    """Quote one path for OpenSSH's ``-o Keyword=value`` config parser.

    ``subprocess`` preserves an argv item containing spaces, but OpenSSH parses
    the value of ``UserKnownHostsFile`` a second time as a list of paths. Double
    quotes are therefore required even though no shell is involved -- and this
    repository lives under a path with a space in it.
    """

    value = str(path)
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ShadeformError("SSH config paths may not contain NUL or newlines")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def acquire_pinned_host_key(info: dict[str, Any], known_hosts: Path, *, provider_fingerprint: str | None = None) -> dict[str, Any]:
    """Acquire a bounded host key with provider proof or stable two-scan proof.

    Shadeform currently exposes no host-key fingerprint in instance info. When
    a provider fingerprint is present it is authoritative; otherwise two
    independent bounded scans must return the exact same key set. This is a
    deliberately recorded residual TOFU risk, and ``StrictHostKeyChecking``
    remains enabled for all subsequent connections.
    """

    ip, _user, port = _endpoint(info)
    fingerprint = provider_fingerprint or next(
        (str(info.get(key, "")).strip() for key in ("ssh_host_key_fingerprint", "host_key_fingerprint", "ssh_fingerprint") if info.get(key)),
        "",
    )
    def scan_once() -> list[str]:
        scan = subprocess.run(
            [_verified_executable("ssh-keyscan"), "-T", "15", "-p", str(port), ip],
            check=False, capture_output=True, text=True, timeout=30,
            env=_secure_subprocess_env(),
        )
        if scan.returncode != 0 or not scan.stdout.strip():
            raise ShadeformError("bounded host-key acquisition failed")
        return [line.strip() for line in scan.stdout.splitlines() if line.strip() and not line.lstrip().startswith("#")]

    acquisition_deadline = time.monotonic() + 120
    def acquire_scan() -> list[str]:
        last_error: Exception | None = None
        while time.monotonic() < acquisition_deadline:
            try:
                return scan_once()
            except (OSError, ShadeformError) as exc:
                last_error = exc
                time.sleep(min(5.0, max(0.1, acquisition_deadline - time.monotonic())))
        raise ShadeformError("host-key acquisition exceeded its 120-second readiness window") from last_error

    first_lines = acquire_scan()
    second_lines = first_lines if fingerprint else acquire_scan()
    first_keys = {" ".join(line.split()[:3]) for line in first_lines if len(line.split()) >= 3}
    second_keys = {" ".join(line.split()[:3]) for line in second_lines if len(line.split()) >= 3}
    if not first_keys or first_keys != second_keys:
        raise ShadeformError("independent host-key scans were empty or unstable")
    fingerprint_lines: dict[str, list[str]] = {}
    for line in sorted(first_keys):
        fields = line.split()
        calculated = subprocess.run(
            [_verified_executable("ssh-keygen"), "-lf", "-", "-E", "sha256"],
            input=f"{fields[1]} {fields[2]}\n", check=False, capture_output=True,
            text=True, timeout=15, env=_secure_subprocess_env(),
        )
        if calculated.returncode != 0 or len(calculated.stdout.split()) < 2:
            raise ShadeformError("host-key fingerprint calculation failed")
        fingerprint_lines.setdefault(calculated.stdout.split()[1], []).append(line)
    if fingerprint:
        matching_lines = fingerprint_lines.get(fingerprint, [])
        if len(matching_lines) != 1:
            raise ShadeformError("provider fingerprint did not identify exactly one scanned host key")
        verified_lines = matching_lines
    else:
        # OpenSSH commonly publishes RSA, ECDSA, and Ed25519 host keys. A
        # stable set across both scans is the proof; rejecting that normal set
        # would make a fresh ephemeral host unusable.
        verified_lines = sorted(first_keys)
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.write_text("\n".join(verified_lines) + "\n", encoding="utf-8")
    known_hosts.chmod(stat.S_IRUSR | stat.S_IWUSR)
    selected_fingerprint = fingerprint if fingerprint else next(iter(fingerprint_lines))
    # Record the exact bytes that were pinned, not just how many lines they
    # occupied.  Teardown-time salvage re-proves this digest, so a known_hosts
    # rewritten with a different key of the same line count is refused instead
    # of silently accepted.
    return {
        "status": "verified",
        "fingerprint": selected_fingerprint,
        "key_count": len(verified_lines),
        "known_hosts_sha256": hashlib.sha256(known_hosts.read_bytes()).hexdigest(),
        "proof": "provider-fingerprint" if fingerprint else "two-stable-bounded-scans-residual-tofu",
    }


def _transport_options(known_hosts: Path) -> list[str]:
    return [
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={_ssh_config_path(known_hosts)}",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=120",
        "-o",
        "TCPKeepAlive=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-o",
        "IdentitiesOnly=yes",
    ]


def _endpoint(info: dict[str, Any]) -> tuple[str, str, Any]:
    ip, user, port = info.get("ip"), info.get("ssh_user"), info.get("ssh_port")
    if not isinstance(ip, str) or not isinstance(user, str) or isinstance(port, bool) or not isinstance(port, int):
        raise ShadeformError("instance has no complete SSH endpoint")
    try:
        parsed_ip = str(ipaddress.ip_address(ip))
    except ValueError:
        raise ShadeformError("provider returned an invalid SSH IP address") from None
    if not 1 <= port <= 65535:
        raise ShadeformError("provider returned an invalid SSH port")
    return parsed_ip, validate_ssh_user(user), port


def ssh_base(info: dict[str, Any], identity: Path, known_hosts: Path) -> list[str]:
    ip, user, port = _endpoint(info)
    return [
        _verified_executable("ssh"),
        *_transport_options(known_hosts),
        "-i",
        str(identity),
        "-p",
        str(port),
        f"{user}@{ip}",
    ]


def scp_base(info: dict[str, Any], identity: Path, known_hosts: Path) -> list[str]:
    _, _, port = _endpoint(info)
    return [
        _verified_executable("scp"),
        "-q",
        *_transport_options(known_hosts),
        "-i",
        str(identity),
        "-P",
        str(port),
    ]


def source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
