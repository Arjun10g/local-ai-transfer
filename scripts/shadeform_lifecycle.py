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
import json
import os
import re
import secrets
import stat
import subprocess
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.shadeform.ai/v1"
RUNTIME_ROOT = ROOT / "experiments" / "runtime"
MARKDOWN_LEDGER = ROOT / "experiments" / "LEDGER.md"
COST_LEDGER = ROOT / "experiments" / "runtime" / "cost-ledger.jsonl"
INCIDENTS = ROOT / "experiments" / "runtime" / "incidents.jsonl"
# The donor's incident log lived in a sibling repository. Ours is in this lane,
# in this repository, because a preflight that depends on a file outside the
# checkout is a preflight that silently stops happening.
INCIDENT_LOG = ROOT / "docs" / "90_operations" / "SHADEFORM_FAILURE_MODES.md"

RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
PHASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")

LEDGER_HEADER = "| date | phase | instance id | gpu | $/hr | purpose | status | cost logged | idle min |"
# Statuses after which a ledger row is history and may not be rewritten.
TERMINAL_STATUSES = frozenset({"deleted", "deleted-key-cleanup-failed", "deleted-cost-bookkeeping-failed"})


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
    """The project budget in experiments/LEDGER.md forbids this action."""


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
    launcher_start_marker: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_env(path: Path) -> dict[str, str]:
    """Load simple dotenv entries without mutating or printing the process environment."""

    result: dict[str, str] = {}
    if not path.is_file():
        raise ShadeformError(f"environment file does not exist: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


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


def validate_nonce(value: str) -> str:
    if NONCE.fullmatch(value) is None:
        raise ValueError("ownership nonce must be 32 lowercase hexadecimal characters")
    return value


def new_ownership_nonce() -> str:
    """Generate the nonce binding one creation attempt to its owner."""

    return secrets.token_hex(16)


def runtime_ledger_path(phase_id: str) -> Path:
    return RUNTIME_ROOT / f"{validate_phase_id(phase_id)}.json"


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
    """Serialize launcher/watchdog teardown for one phase without account scope."""

    path = RUNTIME_ROOT / f"{validate_phase_id(phase_id)}.cleanup.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_owned_resource(phase_id: str) -> OwnedResource | None:
    path = runtime_ledger_path(phase_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ShadeformError(f"malformed phase ledger: {path}")
    record = OwnedResource(**payload)
    validate_resource_id(record.instance_id, field="ledger instance id")
    validate_resource_id(record.ssh_key_id, field="ledger SSH key id")
    validate_nonce(record.ownership_nonce)
    if record.phase_id != validate_phase_id(phase_id):
        raise ShadeformError("phase ledger is bound to a different phase")
    return record


def write_owned_resource(record: OwnedResource) -> None:
    validate_phase_id(record.phase_id)
    validate_resource_id(record.instance_id, field="ledger instance id")
    validate_resource_id(record.ssh_key_id, field="ledger SSH key id")
    validate_nonce(record.ownership_nonce)
    path = runtime_ledger_path(record.phase_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(record), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
    update_markdown_ledger(record)


def clear_owned_resource(phase_id: str, instance_id: str) -> None:
    current = read_owned_resource(phase_id)
    if current is None:
        return
    if current.instance_id != validate_resource_id(instance_id):
        raise ShadeformError("refusing to clear a ledger for a different instance")
    runtime_ledger_path(phase_id).unlink()


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


def ledger_spend() -> tuple[float, list[str]]:
    """Return (dollars already committed, instance IDs whose cost is still unrecorded).

    AGENTS.md 1.4: ``SHADEFORM_MAX_TOTAL_COST_USD`` is the budget for the whole
    project, not for one run. A launcher therefore has to know what has already
    been spent, and a row still reading ``pending`` means some earlier run's
    money was never accounted for -- which is a stop, not a rounding error.
    """

    if COST_LEDGER.is_file():
        latest: dict[str, dict[str, Any]] = {}
        for number, line in enumerate(COST_LEDGER.read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ShadeformError(f"cost ledger line {number} is invalid JSON") from exc
            instance_id = str(event.get("instance_id", "unknown"))
            latest[instance_id] = event
        spent = 0.0
        pending: list[str] = []
        for instance_id, event in latest.items():
            if event.get("status") == "pending":
                pending.append(instance_id)
            elif isinstance(event.get("actual_cost_usd"), (int, float)):
                spent += float(event["actual_cost_usd"])
        return round(spent, 6), pending
    if not MARKDOWN_LEDGER.is_file():
        return 0.0, []
    spent = 0.0
    pending: list[str] = []
    for line in MARKDOWN_LEDGER.read_text(encoding="utf-8").splitlines():
        row = _parse_ledger_row(line)
        if row is None:
            continue
        cost = row["cost"].strip().lstrip("$")
        try:
            spent += float(cost)
        except ValueError:
            pending.append(row["instance_id"])
    return round(spent, 6), pending


def append_cost_event(event: dict[str, Any]) -> None:
    """Append one immutable settled/pending cost event; never rewrite history."""

    if not isinstance(event, dict) or not event.get("instance_id") or not event.get("status"):
        raise ValueError("cost event requires instance_id and status")
    COST_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lock_path = COST_LEDGER.with_suffix(".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        with COST_LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**event, "recorded_at_utc": utc_now().isoformat()}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def reserve_create_attempt(phase_id: str, nonce: str, candidate: Candidate, *, backstop_hours: float, public_key_sha256: str, ssh_key_id: str | None = None) -> str:
    """Durably reserve one possible create POST before any provider mutation."""

    validate_phase_id(phase_id)
    validate_nonce(nonce)
    if backstop_hours <= 0 or not re.fullmatch(r"[0-9a-f]{64}", public_key_sha256):
        raise ValueError("invalid create-attempt reservation inputs")
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
    if ssh_key_id is not None:
        # The latest event enriches the same reservation with the provider key
        # ID, without rewriting its append-only history.
        event["ssh_key_id"] = ssh_key_id
    append_cost_event(event)
    return attempt_id


def append_incident(event: dict[str, Any]) -> None:
    """Persist bounded incident metadata without recording secret material."""

    if not isinstance(event, dict) or not event.get("incident") or not event.get("phase_id"):
        raise ValueError("incident requires incident and phase_id")
    INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
    lock_path = INCIDENTS.with_suffix(".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        with INCIDENTS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**event, "recorded_at_utc": utc_now().isoformat()}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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
    reached a terminal status with a recorded cost it is history and this
    refuses to touch it; nothing here can ever rewrite a different resource's row.
    """

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = RUNTIME_ROOT / "markdown-ledger.lock"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if not MARKDOWN_LEDGER.is_file():
                raise ShadeformError("experiments/LEDGER.md must exist before provider actions")
            existing = MARKDOWN_LEDGER.read_text(encoding="utf-8")
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
                    raise ShadeformError(
                        "refusing to rewrite a settled ledger row for "
                        f"{record.instance_id}; append a correction instead"
                    )
                lines[index] = row
                replaced = True
                break
            if not replaced:
                lines.append(row)
            payload = "\n".join(lines) + "\n"
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{MARKDOWN_LEDGER.name}.", dir=MARKDOWN_LEDGER.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, MARKDOWN_LEDGER)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_name)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _preflight(phase_id: str | None = None) -> None:
    """Mandatory state read before any provider action.

    Both files are read in full on purpose: the runbook requires a fresh ledger
    and failure-mode read immediately before every create, poll, or delete, and
    a preflight that can be satisfied by a stat() is not one.
    """

    if not MARKDOWN_LEDGER.is_file():
        raise ShadeformError("experiments/LEDGER.md must exist before provider actions")
    MARKDOWN_LEDGER.read_text(encoding="utf-8")
    if not INCIDENT_LOG.is_file():
        # Not relative_to(ROOT): the constant is monkeypatchable, and an error
        # path that can itself raise is worse than no error path.
        raise ShadeformError(
            f"the failure-mode catalogue must exist before provider actions: {INCIDENT_LOG}"
        )
    INCIDENT_LOG.read_text(encoding="utf-8")
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
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")[:1000]
        raise ShadeformHTTPError(exc.code, message.replace(api_key, "<redacted>")) from exc
    except (OSError, TimeoutError) as exc:
        raise ShadeformError(f"Shadeform request failed for {method} {path}: {exc}") from exc
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise MalformedProviderResponse("Shadeform response was not a JSON object")
    return parsed


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

    cap = float(env.get("SHADEFORM_MAX_TOTAL_COST_USD", "50") or "50")
    spent, pending = ledger_spend()
    if pending:
        raise BudgetError(
            "experiments/LEDGER.md still records cost 'pending' for "
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


def create_keypair(directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(stat.S_IRWXU)
    private = directory / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(private)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    private.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return private, private.with_suffix(".pub").read_text(encoding="utf-8").strip()


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
    response = request(
        api_key,
        "POST",
        "/sshkeys/add",
        {"name": name, "public_key": public_key},
        phase_id=phase_id,
    )
    # The create response is documented to carry only the ID. Name and public
    # key are verified immediately afterward through /sshkeys/{id}/info.
    return validate_resource_id(response.get("id"), field="created SSH key id")


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


def delete_ssh_key(api_key: str, phase_id: str, key_id: str) -> dict[str, Any]:
    exact = validate_resource_id(key_id, field="SSH key id")
    try:
        return request(api_key, "POST", f"/sshkeys/{exact}/delete", phase_id=phase_id)
    except ShadeformHTTPError as exc:
        if exc.status == 404:
            return {"already_absent": True}
        raise


def verify_ssh_key_ownership(api_key: str, phase_id: str, key_id: str, *, expected_name: str, expected_public_key: str) -> dict[str, Any]:
    """Verify the exact newly-created key through its provider info endpoint."""

    exact = validate_resource_id(key_id, field="SSH key id")
    info = request(api_key, "GET", f"/sshkeys/{exact}/info", phase_id=phase_id)
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


def _auto_delete(env: dict[str, str], runtime_hours: float) -> dict[str, str]:
    """Provider-side backstop, sized per run at duration + 25%.

    This deliberately overrides the dotenv ``SHADEFORM_AUTO_TERMINATE_HOURS``
    default in both directions: our jobs are minutes, and a two-hour backstop on
    a twelve-minute job is eleven-twelfths of a bill nobody meant to pay. The
    dotenv value remains the ceiling below.

    That ``min`` used to truncate silently, and on 2026-09-03 it set a two-hour
    backstop on an eight-hour run (EP-014). The provider deleted the instance at
    2.08 h with 239,088 records held in memory on it, and every projection
    computed that day was measured against a deadline the instance could not
    reach. A ceiling written as a spending guard had become a limit on how long
    a job was permitted to be, and nothing compared the two numbers.

    So a ceiling shorter than the work is now refused here rather than applied.
    This is the single point every creation passes through, which is the same
    reason the ownership nonce lives at this level: a guard one path can miss is
    a guard.
    """

    ceiling = float(env.get("SHADEFORM_AUTO_TERMINATE_HOURS", "2") or "2")
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
    max_total = float(env.get("SHADEFORM_MAX_TOTAL_COST_USD", "50") or "50")
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
) -> str:
    if read_owned_resource(phase_id) is not None:
        raise ShadeformError("phase already owns a recorded instance; reuse or clean it first")
    validate_nonce(nonce)
    name = owned_instance_name(run_id, nonce)
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
        "auto_delete": _auto_delete(env, max_runtime_hours),
    }
    try:
        response = request(
            api_key, "POST", "/instances/create", payload, phase_id=phase_id, timeout=180
        )
    except ShadeformHTTPError:
        # A non-2xx response is definitive: the provider rejected the create.
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


def instance_info(api_key: str, phase_id: str, instance_id: str) -> dict[str, Any]:
    exact = validate_resource_id(instance_id, field="instance id")
    info = request(api_key, "GET", f"/instances/{exact}/info", phase_id=phase_id)
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


def _delete_instance(api_key: str, phase_id: str, instance_id: str) -> dict[str, Any]:
    """Delete one exact instance and confirm it is gone. 404 counts as gone."""

    exact = validate_resource_id(instance_id, field="instance id")
    try:
        response = request(api_key, "POST", f"/instances/{exact}/delete", phase_id=phase_id)
    except ShadeformHTTPError as exc:
        if exc.status == 404:
            return {"success": True, "already_absent": True}
        raise
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            info = instance_info(api_key, phase_id, exact)
        except ShadeformHTTPError as exc:
            if exc.status == 404:
                return {"success": True, "response": response, "status": "absent"}
            raise
        if info.get("status") == "deleted":
            return {"success": True, "response": response, "status": "deleted"}
        time.sleep(5)
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
        scan = subprocess.run(["ssh-keyscan", "-T", "15", "-p", str(port), ip], check=False, capture_output=True, text=True, timeout=30)
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
        calculated = subprocess.run(["ssh-keygen", "-lf", "-", "-E", "sha256"], input=f"{fields[1]} {fields[2]}\n", check=False, capture_output=True, text=True, timeout=15)
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
    return {"status": "verified", "fingerprint": selected_fingerprint, "key_count": len(verified_lines), "proof": "provider-fingerprint" if fingerprint else "two-stable-bounded-scans-residual-tofu"}


def _transport_options(known_hosts: Path) -> list[str]:
    return [
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
        "ControlMaster=auto",
        "-o",
        "ControlPath=/tmp/ep-cm-%C",
        "-o",
        "ControlPersist=600",
        "-o",
        "IdentitiesOnly=yes",
    ]


def _endpoint(info: dict[str, Any]) -> tuple[str, str, Any]:
    ip, user, port = info.get("ip"), info.get("ssh_user"), info.get("ssh_port")
    if not isinstance(ip, str) or not isinstance(user, str) or not port:
        raise ShadeformError("instance has no complete SSH endpoint")
    return ip, user, port


def ssh_base(info: dict[str, Any], identity: Path, known_hosts: Path) -> list[str]:
    ip, user, port = _endpoint(info)
    return [
        "ssh",
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
        "scp",
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
