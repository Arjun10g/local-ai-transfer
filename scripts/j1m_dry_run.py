#!/usr/bin/env python3
"""End-to-end offline dry run of a full J1M lifecycle, before any money moves.

Three separate refusals have now been found by reading rather than by running:
an unconditional salvage refusal, an ``-i`` operand no persisted argv could
accept, and a ``--token-file`` operand naming a path on a host that does not
exist yet.  Each would have created and billed an instance and then returned
nothing.  Reading found them late and one at a time; this harness finds them
all, every time, before the launch.

It drives the *real* ``j1m_orchestrator.execute()`` against a fake provider and
a subprocess shim that records argv instead of executing it, then asserts that
nothing in the resulting run would be refused by a local validator.  Nothing
here is a simulation of the orchestrator: the command construction, the argv
policy, the salvage transport, the receipt verifiers, the caps, the ledger
writes and the teardown ordering are the production code paths.  Only the
provider responses, the key material, and the process launches are fake.

What is deliberately NOT proved: that the remote host behaves, that the model
converts, or that the provider honours its contract.  This gate answers one
question -- "would any command in this run be refused locally?" -- and a PASS
means the run gets as far as the network.

No network, no provider call, no spend.  The one thing that does execute is the
host-tree simulation: the plan's own ``mkdir``/``chmod`` text, replayed by
``/bin/sh`` against a temporary directory standing in for ``/scratch``, because
a ``0755`` directory on the host is invisible to every local validator and cost
one paid run its entire receipt set.  Run it as the final pre-launch step of the
live-run lane:

    python3 scripts/j1m_dry_run.py --mode eval

Exit status is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import j1m_orchestrator as orchestrator
from scripts import j1m_runner, shadeform_lifecycle as sf

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA = "local_bmo.j1m.dry-run-receipt.v1"
MODES = ("eval", "prove", "build")
# Every comparator arm the reviewed selections can request, so the gate covers
# the widest argv the phase can build.
DEFAULT_COMPARATORS = "q8,bf16"

# Values that must never appear in any recorded argv.  They are planted in the
# fake environment and configuration exactly where a real credential would be,
# so "no secret reached a command line" is checked against something that would
# actually be a secret rather than against an empty set.
SENTINELS = {
    "SHADEFORM_API_KEY": "DRYRUN-SENTINEL-API-KEY-4f2b9c1d8e7a6053",
    "SHADEFORM_SSH": "DRYRUN-SENTINEL-SSH-UUID-1a2b3c4d5e6f7081",
    "HF_TOKEN": "DRYRUN-SENTINEL-HF-TOKEN-90ab1c2d3e4f5061",
}
FAKE_INSTANCE_ID = "instance-dryrun-00000001"
FAKE_KEY_ID = "key-dryrun-00000001"
FAKE_IP = "203.0.113.17"
FAKE_SSH_USER = "ubuntu"
FAKE_SSH_PORT = 22
def _ed25519_public_blob(seed: bytes) -> str:
    """Return a well-formed ed25519 public-key blob with no private half.

    The real ``ssh_public_key_fingerprint`` parses this, so a hand-waved
    base64 string would fail for the wrong reason and hide a real defect.
    """

    body = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + hashlib.sha256(seed).digest()
    return base64.b64encode(body).decode("ascii")


FAKE_HOST_KEY_LINE = f"{FAKE_IP} ssh-ed25519 {_ed25519_public_blob(b'dry-run-host')}"
FAKE_CLIENT_PUBLIC_KEY = (
    f"ssh-ed25519 {_ed25519_public_blob(b'dry-run-client')} j1m-dry-run"
)
FAKE_HOST_FINGERPRINT = "SHA256:" + "D" * 43
FAKE_HOURLY_COST_PLACEHOLDER = "from the approved catalogue target"


class DryRunError(RuntimeError):
    """The harness itself could not run; distinct from a failing check."""


# ---------------------------------------------------------------- recording


class Recorder:
    """Every argv the run would have executed, with why it was accepted."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, argv: list[str], *, phase: str, mode: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "mode": mode,
            "phase": phase,
            "program": Path(argv[0]).name if argv else "",
            "argv": redact(argv),
            "validator": "accepted",
        }
        try:
            j1m_runner.validate_persisted_argv(argv)
        except Exception as exc:
            entry["validator"] = "REFUSED"
            entry["refusal"] = type(exc).__name__
        entry["secret_leak"] = sorted(
            name for name, value in SENTINELS.items()
            if any(value in item for item in argv)
        )
        self.entries.append(entry)
        return entry


def redact(argv: list[str]) -> list[str]:
    """Return argv safe to publish: no key path, no sentinel, no host user."""

    out = []
    expecting_handle = False
    for item in argv:
        text = item
        for value in SENTINELS.values():
            text = text.replace(value, "<redacted-secret>")
        if expecting_handle:
            text = "<ephemeral-private-handle>"
            expecting_handle = False
        elif item in {"-i", "--token-file"}:
            expecting_handle = True
        out.append(text[:512])
    return out


# ------------------------------------------------------------ process shim


class SubprocessShim:
    """Answer every child process from source data; never execute anything.

    The shim is deliberately narrow.  It knows how to look like ``ssh-keygen``,
    ``ssh-keyscan``, ``ssh`` and ``scp``, and it refuses anything else loudly,
    so a new subprocess introduced into the lifecycle shows up as a harness
    failure rather than silently succeeding.
    """

    def __init__(self, recorder: Recorder, *, mode: str, receipts: dict[str, bytes],
                 fail_eval_stage: str = "") -> None:
        self.recorder = recorder
        self.mode = mode
        self.receipts = receipts
        self.phase = "setup"
        self.fail_next_remote = False
        self.fail_eval_stage = fail_eval_stage
        self.failed_stage_once = False
        self.unknown: list[str] = []

    def __call__(self, argv, *args, **kwargs):
        if not isinstance(argv, (list, tuple)) or not argv:
            raise DryRunError("dry run refused a non-argv child process")
        argv = [str(item) for item in argv]
        program = Path(argv[0]).name
        if program == "ssh-keygen":
            return self._ssh_keygen(argv, kwargs)
        if program not in {"ssh", "scp"}:
            # ssh/scp are recorded by the ``_remote`` wrapper, before the argv
            # policy can refuse them.  Anything else is recorded here.
            self.recorder.record(argv, phase=self.phase, mode=self.mode)
        if program == "ssh-keyscan":
            return _completed(stdout=FAKE_HOST_KEY_LINE + "\n")
        if program == "ssh":
            if (self.fail_eval_stage and not self.failed_stage_once
                    and any(self.fail_eval_stage in item for item in argv)):
                self.failed_stage_once = True
                return _completed(returncode=1, stderr="dry-run injected stage failure")
            return _completed(stdout="", stderr="")
        if program == "scp":
            return self._scp(argv)
        self.unknown.append(program)
        raise DryRunError(f"dry run refused an unexpected child process: {program}")

    def _ssh_keygen(self, argv: list[str], kwargs: dict[str, Any]):
        if "-lf" in argv:
            # Host-key fingerprint calculation reads the key from stdin.
            return _completed(stdout=f"256 {FAKE_HOST_FINGERPRINT} dry-run (ED25519)\n")
        if "-f" not in argv:
            raise DryRunError("dry run refused an unrecognised ssh-keygen invocation")
        target = Path(argv[argv.index("-f") + 1])
        # A fake keypair, in the real key location, created through the real
        # ``create_keypair`` path: the directory modes, the basename and the
        # 0600 private half are the production ones.
        target.write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nDRY-RUN-NOT-A-KEY\n"
            "-----END OPENSSH PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        target.chmod(0o600)
        target.with_name(target.name + ".pub").write_text(
            FAKE_CLIENT_PUBLIC_KEY + "\n", encoding="utf-8")
        return _completed()

    def _scp(self, argv: list[str]):
        if self.fail_next_remote:
            self.fail_next_remote = False
            return _completed(returncode=1, stderr="dry-run injected transfer failure")
        source, destination = argv[-2], argv[-1]
        if ":" in source and "@" in source:
            # A fetch: stage the source-allowlisted receipt this run expects.
            name = source.rsplit("/", 1)[-1]
            payload = self.receipts.get(name)
            if payload is None:
                return _completed(returncode=1, stderr="scp: No such file or directory")
            staged = Path(destination)
            staged.write_bytes(payload)
            staged.chmod(0o600)
        return _completed()


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr, args=[])


class FakeWatchdog:
    """Stand in for the recovery watchdog process without launching one."""

    pid = 424242

    def __init__(self) -> None:
        self.stopped = False

    def poll(self):
        return None if not self.stopped else 0

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        self.stopped = True
        return 0

    def kill(self):
        self.stopped = True


# ------------------------------------------------------------- fake receipts


def fake_receipts(config: dict[str, Any], eval_artifact: dict[str, Any] | None,
                  *, run_id: str, instance_id: str) -> dict[str, bytes]:
    """Build the receipts a correct host would write, bound to this run.

    These are what the *real* host-side writers now produce: every one carries
    the run-identity binding read from the uploaded ``run-identity.json``.  If
    a writer ever stops stamping it, the identity check below fails here before
    a live run discovers it after paying for a conversion.
    """

    bound = {"run_id": run_id, "instance_id": instance_id}
    revision = config["source"]["revision"]
    llama_revision = config["llama_cpp"]["revision"]
    artifact = eval_artifact or {
        "name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 1, "sha256": "0" * 64,
    }
    digest = orchestrator._APPROVED_EVAL_MANIFEST_SHA256

    def arm_digest(arm: str) -> str:
        """The Q4 arm scores the run's own artifact; the others score rebuilds.

        Only the Q4 arm's digest is knowable to the orchestrator, and the
        transport binds exactly that one. The other two are required to declare
        a well-formed digest, which is what this stands in for.
        """

        if arm == "q4_k_m":
            return artifact["sha256"]
        return hashlib.sha256(arm.encode("utf-8")).hexdigest()
    contract = orchestrator._tool_eval_contract()
    fixture_identity = contract["fixture_identity"]
    counts = contract["category_counts"]
    category_summary = {
        category: {"case_count": count, "passed": count, "failed": 0, "errors": 0}
        for category, count in counts.items()
    }
    case_count = contract["case_count"]
    toolchain_receipt = {
        "schema": "local_bmo.j1m.remote-toolchain-receipt.v1", "status": "verified",
        "required": {"python3": ">=3.8", "git": ">=2.30", "cmake": ">=3.18", "g++": ">=9.0", "nvcc": ">=12.0"},
        "versions": {
            name: {"major": major, "minor": minor, "reported": reported, "executable": executable}
            for name, major, minor, reported, executable in (
                ("python3", 3, 10, "Python 3.10.12", "/usr/bin/python3"),
                ("git", 2, 39, "git version 2.39.2", "/usr/bin/git"),
                ("cmake", 3, 22, "cmake version 3.22.1", "/usr/bin/cmake"),
                ("g++", 11, 4, "g++ (Ubuntu 11.4.0)", "/usr/bin/g++"),
                ("nvcc", 12, 2, "Cuda compilation tools, release 12.2", "/usr/local/cuda/bin/nvcc"),
            )
        },
        "packages": {
            "ca-certificates": "20240203", "cmake": "3.22.1", "build-essential": "12.9ubuntu3",
            "git": "1:2.39.2", "python3": "3.10.12", "python3-venv": "3.10.12",
        },
        "package_install": "ubuntu apt repositories; exact resolved package versions captured by dpkg-query",
    }
    receipts: dict[str, Any] = {
        "proving-receipt.json": {
            "schema": "local_bmo.j1m.proving-receipt.v1", "host": "dry-run-host",
            "python": "3.10.12", "text_only": True, "conversion": "not-run",
        },
        "toolchain-receipt.json": toolchain_receipt,
        "cuda-device-receipt.json": {
            "schema": "local_bmo.j1m.cuda-device-receipt.v1", "status": "verified",
            "selector": "CUDA0", "device_count": 1,
            "device": {"index": 0, "name": "NVIDIA A100 80GB PCIe", "memory_total_mib": 81920,
                       "driver_version": "550.54.15"},
            "source": "nvidia-smi bounded query",
        },
        "eval-artifact-receipt.json": {
            "schema": "local_bmo.j1m.remote-eval-artifact-receipt.v1", "status": "verified",
            "name": artifact["name"], "size_bytes": artifact["size_bytes"],
            "sha256": artifact["sha256"], "manifest_sha256": digest,
            "manifest_lock_sha256": digest,
        },
        "startup-preflight-receipt.json": {
            "schema": "local_bmo.j1m.startup-preflight-receipt.v1", "status": "verified",
            "size_bytes": artifact["size_bytes"], "sha256": artifact["sha256"], "gguf_version": 3,
        },
        "eval-receipt.json": {
            "schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "verified",
            "artifact": {key: artifact[key] for key in (
                "name", "size_bytes", "sha256", "source_revision", "llama_cpp_revision",
                "modality", "quantization") if key in artifact},
            "fixture": fixture_identity,
            "engine": {
                "engine_version": "0.1.0", "api_version": "0.1.0",
                "compiled_backend": f"llama.cpp/{llama_revision[:8]}/cuda",
                "llama_cpp_revision": llama_revision, "model": "qwen35-9b-q4-k-m",
            },
            "model_preflight": {
                "valid": True, "code": "ok", "status": "verified",
                "size_bytes": artifact["size_bytes"], "sha256": artifact["sha256"], "gguf_version": 3,
            },
            "cuda_device": {
                "schema": "local_bmo.j1m.cuda-device-receipt.v1", "status": "verified",
                "selector": "CUDA0", "device_count": 1,
                "device": {"index": 0, "name": "NVIDIA A100 80GB PCIe",
                           "memory_total_mib": 81920, "driver_version": "550.54.15"},
                "source": "nvidia-smi bounded query",
            },
            "toolchain": toolchain_receipt,
            "metrics": {
                "case_count": case_count, "passed": case_count, "failed": 0, "errors": 0,
                "peak_rss_kib": 1024, "category_summary": category_summary,
                "canary": {
                    "attempted": True, "passed": True, "error_code": None,
                    "tool_count": contract["tool_count"], "message_chars": 2400,
                    "prompt_tokens": 700, "context_tokens": contract["context_tokens"],
                    "output_reserve_tokens": contract["output_reserve_tokens"],
                },
                "error_diagnostics": {
                    "schema": "local_bmo.tool-call-eval-diagnostics.v1", "total_errors": 0,
                    "overall": {}, "by_category": {category: {} for category in category_summary},
                },
                "quality_diagnostics": {
                    "schema": "local_bmo.tool-call-quality-diagnostics.v1", "total_failed": 0,
                    "overall": {}, "by_category": {category: {} for category in category_summary},
                },
            },
            "prompt_response_logging": False, "tokens_logged": False,
        },
        # Build-mode receipts, in the shape ``j1m_runner`` writes them.
        "source-model-receipt.json": {
            "schema": "local_bmo.j1m.source-model-receipt.v1", "status": "verified",
            "model_id": config["source"]["model_id"], "revision": revision,
            "checked_files": ["config.json"], "file_hashes": {"config.json": "a" * 64},
            "license_sha256": "b" * 64, "tokenizer_sha256": "c" * 64,
            "chat_template_sha256": "d" * 64, "verified_at_utc": "2026-01-01T00:00:00+00:00",
        },
        "tensor-metadata.json": {
            "schema": "local_bmo.j1m.tensor-metadata.v1", "status": "verified", "text_only": True,
            "tensor_count": 0, "tensors": [], "gguf_metadata": {"general.architecture": "qwen35"},
            "vision_projection_present": False, "chat_template_sha256": "d" * 64,
        },
        "toolchain.json": {
            "schema": "local_bmo.j1m.toolchain.v1", "llama_cpp_head": llama_revision,
            "python": "Python 3.10.12", "cmake": "cmake version 3.22.1",
            "compiler": "cc (Ubuntu 11.4.0)", "os_packages": [], "pip_freeze": "",
            "dependency_wheelhouse_lock": {},
        },
        "scan-receipt.json": {
            "schema": "local_bmo.j1m.scan-receipt.v1", "status": "verified",
            "inventory_scope": "pre_cleanup_conversion_outputs", "text_only": True,
            "artifacts": [{"name": name, "size_bytes": 1, "sha256": "e" * 64} for name in (
                "Qwen3.5-9B-bf16.gguf", "Qwen3.5-9B-Q8_0.gguf", "Qwen3.5-9B-Q4_K_M.gguf")],
            "vision_projection_present": False,
        },
        "post-cleanup-receipt.json": {
            "schema": "local_bmo.j1m.post-cleanup-receipt.v1", "status": "verified",
            "inventory_scope": "post_cleanup_filesystem", "intermediates_absent": True,
            "remaining_gguf": ["Qwen3.5-9B-Q4_K_M.gguf"], "forbidden_artifacts": [],
            "q4": {"size_bytes": 1, "sha256": "e" * 64},
        },
        "conversion-receipt.json": {
            "schema": "local_bmo.j1m.conversion-receipt.v1", "status": "conversion-complete",
            "text_only": True, "source_revision": revision, "llama_cpp_revision": llama_revision,
            "artifacts": {"Qwen3.5-9B-Q4_K_M.gguf": {"size_bytes": 1, "sha256": "e" * 64}},
            "converter_and_quantizer_argv": [], "command_receipt_sha256": "f" * 64,
            "toolchain": {"schema": "local_bmo.j1m.toolchain.v1"}, "no_mmproj": True, "no_mtp": True,
        },
        "model-receipt.json": {
            "schema": "local_bmo.j1m.model-receipt.v1",
            "status": "checksums-and-tensor-inventory-verified", "text_only": True,
            "q4_artifact": {"size_bytes": 1, "sha256": "e" * 64},
            "tensor_metadata_sha256": "a" * 64, "gguf_metadata": {},
            "vision_projection_present": False, "scan_receipt_sha256": "b" * 64,
            "tokenizer_sha256": "c" * 64, "chat_template_sha256": "d" * 64,
            "license_sha256": "b" * 64,
        },
        **{
            f"comparator-receipt-{arm}.json": {
                "schema": "local_bmo.j1m.comparator-eval-receipt.v1",
                "status": "verified", "arm": arm,
                "artifact": {"name": filename, "size_bytes": artifact["size_bytes"],
                             "sha256": arm_digest(arm), "quantization": quantization},
                "artifact_sha256": arm_digest(arm),
                "fixture": {"sha256": fixture_identity["sha256"],
                            "case_count": case_count},
                "fixture_sha256": fixture_identity["sha256"],
                "host": {"kind": "pinned-upstream-llama-server",
                         "llama_cpp_revision": llama_revision, "backend": "cuda"},
                "settings": {"context_tokens": contract["context_tokens"],
                             "temperature": 0, "max_cases": case_count},
                "metrics": {"case_count": case_count, "passed": case_count,
                            "failed": 0, "errors": 0,
                            "category_summary": category_summary},
            }
            for arm, (filename, quantization) in (
                ("q4_k_m", ("Qwen3.5-9B-Q4_K_M.gguf", "Q4_K_M")),
                ("q8_0", ("Qwen3.5-9B-Q8_0.gguf", "Q8_0")),
                ("bf16", ("Qwen3.5-9B-bf16.gguf", "bf16")),
            )
        },
        "manifest.json": {
            "schema": "local_bmo.j1m.artifact-manifest.v1",
            "created_at_utc": "2026-01-01T00:00:00+00:00",
            "inventory_scope": "post_cleanup_deployable_allowlist",
            "deployable_model_artifacts": ["Qwen3.5-9B-Q4_K_M.gguf"], "text_only": True,
            "artifacts": [{"name": "Qwen3.5-9B-Q4_K_M.gguf", "size_bytes": 1, "sha256": "e" * 64}],
            "tensor_metadata": {"status": "verified"},
        },
    }
    return {
        name: (json.dumps({**payload, **bound}, sort_keys=True) + "\n").encode("utf-8")
        for name, payload in receipts.items()
    }


# --------------------------------------------------------------- isolation


DRY_RUN_ROOT = ROOT / ".secrets" / "j1m-dry-run"


@contextlib.contextmanager
def isolated_runtime():
    """Bind every mutable lifecycle path to one temporary namespace.

    The dry run must never touch the checkout's real ledgers, incidents, or
    owner records: it writes a complete, valid cost history for a run that
    never happened, and that history must not be mistakable for evidence.

    The namespace lives under the protected secrets root rather than in a
    system temporary directory, because ``PRIVATE_OUTPUT_ROOT`` stays pointed
    at the real checkout.  That is deliberate: the salvage publisher, the
    config loader and the receipt readers all walk descriptors from that root,
    and repointing it to somewhere else would exercise a path the live run
    never takes.  Everything written here is removed on exit.
    """

    DRY_RUN_ROOT.parent.mkdir(mode=0o700, exist_ok=True)
    DRY_RUN_ROOT.mkdir(mode=0o700, exist_ok=True)
    directory = tempfile.mkdtemp(prefix="run-", dir=DRY_RUN_ROOT)
    try:
            base = Path(directory).resolve()
            os.chmod(base, 0o700)
            runtime = base / "runtime"
            runtime.mkdir(mode=0o700)
            markdown = base / "LEDGER.md"
            cost = runtime / "cost-ledger.jsonl"
            incidents = runtime / "incidents.jsonl"
            markdown.write_text(sf.LEDGER_HEADER + "\n", encoding="utf-8")
            incidents.write_text('{"incident":"dry-run-fixture"}\n', encoding="utf-8")
            with contextlib.ExitStack() as stack:
                for name, value in (("RUNTIME_ROOT", runtime), ("MARKDOWN_LEDGER", markdown),
                                    ("COST_LEDGER", cost), ("INCIDENTS", incidents)):
                    stack.enter_context(mock.patch.object(sf, name, value))
                stack.enter_context(mock.patch.object(
                    sf, "_verified_python_executable", return_value="/usr/bin/python3"))
                # "No network" is enforced here, not merely asserted in the
                # receipt: every outbound boundary raises if the run reaches it.
                for module, attribute in ((sf, "request"),
                                          (urllib.request, "urlopen"),
                                          (socket, "create_connection")):
                    stack.enter_context(mock.patch.object(
                        module, attribute,
                        side_effect=DryRunError("dry run attempted network access")))
                sf.initialize_cost_ledger_genesis(
                    program=sf.COST_LEDGER_PROGRAM,
                    currency=sf.COST_LEDGER_CURRENCY,
                    budget_cap_usd=50.0,
                    prior_settled_spend_usd=0.0,
                    current_pending_owner_count=0,
                    expected_display_ledger_sha256=hashlib.sha256(markdown.read_bytes()).hexdigest(),
                    expected_incidents_sha256=hashlib.sha256(incidents.read_bytes()).hexdigest(),
                    confirmation=sf.COST_LEDGER_GENESIS_CONFIRMATION,
                )
                yield types.SimpleNamespace(base=base, runtime=runtime, cost_ledger=cost,
                                            markdown=markdown, incidents=incidents)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            DRY_RUN_ROOT.rmdir()


def _candidate(config: dict[str, Any], index: int = 0, **overrides: Any):
    """Build the live catalogue row one approved target would exactly match.

    ``overrides`` is how a near-miss is written: the same offer with one
    dimension changed is what the selector must refuse, and building it from
    the real entry keeps the near-miss honest instead of hand-typed.
    """

    target = config["shadeform_targets"][index]
    fields = {
        "gpu": target["gpu"],
        "cloud": target["cloud"],
        "region": target["region"],
        "instance_type": target.get("instance_type", target["gpu"]),
        "hourly_usd": float(target["hourly_usd"]),
        "vram_gb": int(target["vram_gib"]),
        "os_image": target.get("os_image", "ubuntu22.04_cuda12.2_shade_os"),
        "interruptible": False,
    }
    fields.update(overrides)
    return sf.Candidate(**fields)


def _unapproved_candidate():
    """A live offer that is not on the approved list at any index."""

    return sf.Candidate(
        "H100_80G", "hyperstack", "montreal-canada-2", "H100_80G",
        2.50, 80, "ubuntu22.04_cuda12.2_shade_os", False,
    )


def _instance_info() -> dict[str, Any]:
    """A provider activation record shaped like the real one."""

    return {
        "id": FAKE_INSTANCE_ID, "status": "active", "ip": FAKE_IP,
        "ssh_user": FAKE_SSH_USER, "ssh_port": FAKE_SSH_PORT,
        "ssh_key_id": FAKE_KEY_ID, "name": "dry-run-instance",
    }


# ------------------------------------------------------------------- driver


def drive(mode: str, *, inject_failure: bool, key_root: Path | None,
          recorder: Recorder, comparators: str = "",
          fail_eval_stage: str = "",
          catalogue: list[Any] | None = None) -> dict[str, Any]:
    """Run one complete lifecycle offline and return everything it produced.

    ``comparators`` drives the default-OFF comparator phase exactly as
    ``--evaluate-comparators`` does.  ``fail_eval_stage`` makes the shim fail the
    first remote command containing that substring, which is how the deferred
    intermediate-deletion tail is proved to run on a failure path: the phase's
    own ``finally`` sits below the eval-stage loop's ``raise``, so that tail used
    to be skipped entirely and silently.
    """

    config = j1m_runner.load_config()
    run_id = "J1MDRY"
    phase_id = f"j1m-dry-run-{mode}"
    eval_artifact = None
    if mode == "eval":
        eval_artifact = orchestrator._verify_eval_artifact(
            None, ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json", config)
    receipts = fake_receipts(config, eval_artifact, run_id=run_id, instance_id=FAKE_INSTANCE_ID)
    shim = SubprocessShim(recorder, mode=mode, receipts=receipts,
                          fail_eval_stage=fail_eval_stage)
    progress: list[str] = []
    cost_events: list[dict[str, Any]] = []
    teardown_calls: list[dict[str, Any]] = []
    key_cleanup: dict[str, Any] = {}
    outcome: dict[str, Any] = {"mode": mode, "inject_failure": inject_failure,
                               "comparators": comparators,
                               "fail_eval_stage": fail_eval_stage}

    persisted: list[dict[str, Any]] = []
    nonces: list[str] = []
    real_append_cost_event = sf.append_cost_event
    real_persist = orchestrator._persist_lifecycle
    real_nonce = sf.new_ownership_nonce

    def recording_cost_event(event):
        cost_events.append(copy.deepcopy(event))
        return real_append_cost_event(event)

    def recording_persist(phase, value):
        # Call through: the lifecycle receipt must still satisfy
        # ``validate_persisted_receipt``, including the new key-cleanup block.
        real_persist(phase, value)
        persisted.append(copy.deepcopy(value))

    def recording_nonce():
        nonce = real_nonce()
        nonces.append(nonce)
        return nonce

    recorded_from = len(recorder.entries)
    real_remote = orchestrator._remote

    def recording_remote(command, *, timeout):
        # Record before the production validator runs.  A refused command never
        # spawns a process, so a harness that only watched the process boundary
        # would report zero refusals for a run that could not execute at all --
        # exactly the failure mode this gate exists to prevent.
        recorder.record(list(command), phase="remote", mode=mode)
        return real_remote(command, timeout=timeout)

    def fake_teardown(phase, instance_id, *, env_file=None, deadline=None, **kwargs):
        teardown_calls.append({"instance_id": instance_id, "deadline_supplied": deadline is not None})
        return {"status": "complete", "deletion": {"success": True}, "instance_id": instance_id}

    def refuse_recovered_teardown(*args, **kwargs):
        raise DryRunError("dry run reached the unrecorded-instance recovery path")

    with isolated_runtime() as runtime, contextlib.ExitStack() as stack:
        patches = [
            mock.patch.object(subprocess, "run", side_effect=shim),
            mock.patch.object(subprocess, "Popen", side_effect=lambda *a, **k: FakeWatchdog()),
            mock.patch.object(sf, "load_env", return_value=dict(SENTINELS)),
            mock.patch.object(sf, "list_candidates",
                              return_value=list(catalogue) if catalogue is not None
                              else [_candidate(config)]),
            mock.patch.object(sf, "add_ssh_key", return_value=FAKE_KEY_ID),
            mock.patch.object(sf, "verify_ssh_key_ownership", return_value={"id": FAKE_KEY_ID}),
            mock.patch.object(sf, "create_instance", return_value=FAKE_INSTANCE_ID),
            mock.patch.object(sf, "wait_active", return_value=_instance_info()),
            mock.patch.object(sf, "verify_instance_ownership", return_value=_instance_info()),
            mock.patch.object(sf, "delete_owned_ssh_key_exact", return_value={"status": "confirmed"}),
            mock.patch.object(sf, "append_cost_event", side_effect=recording_cost_event),
            mock.patch.object(orchestrator, "teardown_exact", side_effect=fake_teardown),
            mock.patch.object(orchestrator, "teardown_recovered_exact", side_effect=refuse_recovered_teardown),
            mock.patch.object(j1m_runner, "write_progress",
                              side_effect=lambda path, event, **details: progress.append(event)),
            mock.patch.object(orchestrator, "_remote", side_effect=recording_remote),
            mock.patch.object(orchestrator, "_persist_lifecycle", side_effect=recording_persist),
            mock.patch.object(sf, "new_ownership_nonce", side_effect=recording_nonce),
        ]
        if key_root is not None:
            patches.append(mock.patch.object(sf, "EPHEMERAL_KEY_ROOT", key_root))
        for patcher in patches:
            stack.enter_context(patcher)
        destination = runtime.base / "artifacts"
        destination.mkdir(mode=0o700)
        if inject_failure:
            # A definitive teardown failure on a run that otherwise succeeded:
            # the key must still be destroyed and the failure must surface.
            stack.enter_context(mock.patch.object(
                orchestrator, "teardown_exact",
                side_effect=RuntimeError("dry-run injected teardown failure")))

        lifecycle: dict[str, Any] | None = None
        error: str | None = None
        try:
            lifecycle = orchestrator.execute(
                runtime.base / "shadeform.env",
                config_path=j1m_runner.DEFAULT_CONFIG,
                phase_id=phase_id, run_id=run_id,
                artifact_destination=destination, mode=mode,
                evaluate_comparators=comparators,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        # ``execute`` raises its cleanup failure *after* the finally block, so
        # the lifecycle it built is only visible through what it persisted.
        final = lifecycle if lifecycle is not None else (persisted[-1] if persisted else {})
        key_cleanup = dict(final.get("ephemeral_key_cleanup") or {})
        run_identity = {"run_id": run_id, "instance_id": FAKE_INSTANCE_ID}
        outcome.update({
            "status": final.get("status", "raised"),
            "error": error,
            "receipt_error": final.get("receipt_error"),
            "salvage": final.get("salvage", []),
            "build_receipts": final.get("build_receipts"),
            "key_cleanup": key_cleanup,
            "teardown_calls": teardown_calls,
            "cost_events": cost_events,
            "progress": progress,
            "published": sorted(item.name for item in destination.iterdir()),
            "unknown_subprocesses": sorted(set(shim.unknown)),
            "salvage_codes": {
                str(item.get("name")): str(item.get("error_code") or item.get("status"))
                for item in final.get("salvage", []) if isinstance(item, dict)
            },
            "run_identity": run_identity,
            "selected_target": final.get("selected_target"),
            "comparator_phase": final.get("comparator_phase"),
            "comparator_cleanup_error": final.get("comparator_cleanup_error"),
            "comparator_cleanup_stages": _cleanup_stages(recorder, since=recorded_from),
            "comparison_receipt": _comparison_receipt(destination),
            "lifecycle_persisted": bool(persisted),
            # Read the binding out of every published receipt now: the isolated
            # namespace does not outlive this block.
            "published_identity": _published_identity(destination),
        })
        key_directory = (sf.ephemeral_key_directory(f"{run_id}-{nonces[-1]}", root=key_root)
                         if nonces else None)
        present = bool(key_directory and key_directory.exists())
        outcome["key_directory_present"] = present
        if present:
            # Report why it survived rather than just that it did.
            try:
                os.rmdir(key_directory)
                outcome["key_directory_present"] = False
            except OSError as exc:
                outcome["key_directory_errno"] = exc.errno
        outcome["key_root_residue"] = _key_root_residue(key_root)
    return outcome


# The deferred intermediate-deletion tail, identified by what it actually runs
# rather than by a label: `rm -f` over the comparator weights, then the runner's
# own post-cleanup receipt and manifest stages.
_CLEANUP_MARKERS = ("rm", "--post-cleanup", "--manifest")


def _cleanup_stages(recorder: Recorder, *, since: int) -> list[str]:
    """Return which of the deferred cleanup stages this run actually reached."""

    found = []
    for entry in recorder.entries[since:]:
        argv = entry["argv"]
        if "rm" in argv and any(part.endswith(".gguf") for part in argv):
            found.append("rm")
        for marker in ("--post-cleanup", "--manifest"):
            if marker in argv:
                found.append(marker)
    return sorted(set(found))


def _comparison_receipt(destination: Path) -> dict[str, Any] | None:
    """Return the comparison receipt the run published, if it published one."""

    path = destination / "comparison-receipt.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _published_identity(destination: Path) -> dict[str, Any]:
    """Return each published receipt's run-identity binding, or why it is absent."""

    found: dict[str, Any] = {}
    for item in sorted(destination.iterdir()):
        # Only *salvaged* receipts carry a run binding: `salvage-receipt.json`
        # and `comparison-receipt.json` are written locally by this run and are
        # not fetched from the host, so they are not in scope for this check.
        if item.name not in orchestrator._SALVAGE_RECEIPT_ALLOWLIST:
            continue
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            found[item.name] = "unreadable"
            continue
        found[item.name] = {
            field: payload.get(field, "<absent>")
            for field in sorted(orchestrator._SALVAGE_REQUIRED_IDENTITY)
        }
    return found


def _key_root_residue(key_root: Path | None) -> list[str]:
    """Residue under the root THIS invocation owns, never the real-run root.

    The gate used to list the whole of ``.secrets/j1m/`` here, so a single
    stranded directory from an unrelated run made it fail forever -- and it is
    not this gate's business to judge, or to delete, a real run's leftovers.
    ``run_dry_run`` always hands down a private subtree, so this is empty unless
    a fake run genuinely failed to clean up after itself.
    """

    if key_root is None:
        return []
    try:
        return sorted(item.name for item in Path(key_root).iterdir())
    except OSError:
        return []


def _key_outcome(run: dict[str, Any]) -> str:
    """Say what actually happened to one run's key directory.

    Never a constant: "removed" was printed for every run regardless, which is
    exactly the sort of reassuring-but-unearned line this harness exists to stop
    shipping.
    """

    status = (run.get("key_cleanup") or {}).get("status")
    if run.get("key_directory_present"):
        errno_value = run.get("key_directory_errno")
        detail = f"errno={errno_value}" if errno_value else "still present"
        return f"removal-failed({detail})"
    if run.get("pre_spend_refusal"):
        # A run refused before key generation has no key to remove. That is the
        # stronger outcome, not a missing record -- but it only counts as such
        # when the run really did stop before anything was created.
        if status is None and not run.get("cost_events") and not run.get("teardown_calls"):
            return "never-created"
        return f"refusal-created-state({status or 'unknown'})"
    if status == "removed":
        return "removed"
    if status == "absent":
        return "not-present"
    if status is None:
        return "no-cleanup-record"
    return f"{status}({(run.get('key_cleanup') or {}).get('error_type', 'unspecified')})"


# -------------------------------------------------------------------- checks


def evaluate(results: dict[str, dict[str, Any]], recorder: Recorder,
             *, modes: tuple[str, ...]) -> list[dict[str, Any]]:
    """Turn one complete offline run into an ordered PASS/FAIL check list."""

    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"check": check_id, "status": "PASS" if passed else "FAIL", "detail": detail})

    refused = [entry for entry in recorder.entries if entry["validator"] != "accepted"]
    # A run that produced no commands proved nothing; treat an empty recording
    # as a failure rather than a vacuous pass.
    add("argv_validator_accepts_every_command",
        bool(recorder.entries) and not refused,
        f"{len(recorder.entries)} argv recorded; {len(refused)} refused by "
        f"validate_persisted_argv" + (f": {refused[0]['argv'][:6]}" if refused else ""))

    remote_operands = [
        item for entry in recorder.entries if entry["program"] == "scp"
        for item in entry["argv"] if ":" in item and "@" in item
    ]
    fetches = [item.rsplit("/", 1)[-1] for item in remote_operands
               if item.split(":", 1)[1].startswith(orchestrator._SALVAGE_REMOTE_DIRECTORY)]
    stray = sorted(set(fetches) - set(orchestrator._SALVAGE_RECEIPT_ALLOWLIST))
    add("remote_operands_allowlisted", not stray,
        f"{len(fetches)} salvage fetches, all from {orchestrator._SALVAGE_REMOTE_DIRECTORY}; "
        f"outside the source allowlist: {stray or 'none'}")

    leaks = [entry for entry in recorder.entries if entry["secret_leak"]]
    add("no_secret_value_in_any_argv", not leaks,
        f"{len(SENTINELS)} sentinel credentials planted in env/config; "
        f"{len(leaks)} argv carried one")

    config = j1m_runner.load_config()
    coverage = {}
    for key in ("eval_fetch_allowlist", "prove_fetch_allowlist", "local_fetch_allowlist"):
        names = set(config["artifacts"][key])
        known = set(orchestrator._SALVAGE_RECEIPT_ALLOWLIST) | set(orchestrator._SALVAGE_NON_RECEIPT_NAMES)
        coverage[key] = sorted(names - known)
    unaccounted = {key: value for key, value in coverage.items() if value}
    add("salvage_allowlist_matches_host_writers", not unaccounted,
        f"every configured fetch name is either source-allowlisted or an explicit "
        f"non-receipt refusal; unaccounted: {unaccounted or 'none'}")

    identity_fields = sorted(orchestrator._SALVAGE_REQUIRED_IDENTITY)
    unbound = []
    published_total = 0
    for name, run in results.items():
        for receipt_name, binding in run["published_identity"].items():
            published_total += 1
            if not isinstance(binding, dict) or any(
                    binding.get(field) != run["run_identity"][field] for field in identity_fields):
                unbound.append(f"{name}:{receipt_name}")
    add("receipts_carry_required_identity", bool(published_total) and not unbound,
        f"{published_total} published receipts checked for {identity_fields}; "
        f"unbound: {unbound or 'none'}")

    success = results[modes[0]]
    add("teardown_reached_on_success", bool(success["teardown_calls"]),
        f"teardown_exact called {len(success['teardown_calls'])}x with the exact instance id")

    failed_run = results.get("__injected_failure__", {})
    add("teardown_reached_on_injected_failure",
        bool(failed_run) and failed_run.get("error") is not None
        and failed_run.get("key_directory_present") is False,
        f"injected teardown failure surfaced as {failed_run.get('error')!r} and the "
        f"ephemeral key directory was still removed")

    # Judged only on the directories this invocation created, so residue from
    # an unrelated run can never hold the gate down (see the warning below).
    key_ok = all(
        _key_outcome(run) in {"removed", "not-present", "never-created"}
        and not run["key_root_residue"]
        for run in results.values()
    )
    add("ephemeral_key_removed_on_every_path", key_ok,
        "; ".join(f"{name}={_key_outcome(run)}" for name, run in results.items()))

    caps = {
        "deletion_reserve_seconds": orchestrator._DELETION_RESERVE_SECONDS,
        "per_file_bytes": orchestrator._SALVAGE_MAX_FILE_BYTES,
        "total_bytes": orchestrator._SALVAGE_MAX_TOTAL_BYTES,
        "max_files": orchestrator._SALVAGE_MAX_FILES,
        "min_free_bytes": orchestrator._SALVAGE_MIN_FREE_BYTES,
        "wall_clock_seconds": orchestrator._SALVAGE_WALL_CLOCK_SECONDS,
    }
    caps_ok = (
        caps["deletion_reserve_seconds"] == 660.0
        and caps["per_file_bytes"] == j1m_runner._RECEIPT_MAX_BYTES
        and caps["max_files"] == len(orchestrator._SALVAGE_RECEIPT_ALLOWLIST)
        and caps["total_bytes"] == caps["max_files"] * caps["per_file_bytes"]
        and caps["min_free_bytes"] == caps["total_bytes"] * 2
        and all(len(run["salvage"]) <= orchestrator._SALVAGE_MAX_REQUESTED_NAMES
                for run in results.values())
    )
    add("caps_and_reserves_respected", caps_ok, json.dumps(caps, sort_keys=True))

    ledger_ok = True
    ledger_detail = []
    for name, run in results.items():
        if run.get("pre_spend_refusal"):
            # The opposite obligation: a refused run must have written nothing.
            ledger_ok = ledger_ok and not run["cost_events"]
            ledger_detail.append(f"{name}=no row (refused pre-spend)")
            continue
        pending = [event for event in run["cost_events"]
                   if event.get("status") == "pending" and event.get("instance_id") == FAKE_INSTANCE_ID]
        ledger_ok = ledger_ok and len(pending) == 1 and pending[0].get("estimated_cost_usd", 0) > 0
        if pending:
            ledger_detail.append(f"{name}=${pending[0].get('estimated_cost_usd')}")
    add("ledger_append_recorded_the_fake_cost", ledger_ok,
        f"one pending cost row per run bound to the exact instance id: {', '.join(ledger_detail)}")

    build = results.get("build")
    if build is None:
        add("build_salvage_returns_receipts_and_refuses_weights", True, "build mode not selected")
    else:
        salvaged = {item["name"] for item in build["salvage"] if item.get("status") == "completed"}
        refused_weights = {
            item["name"] for item in build["salvage"]
            if item.get("error_code") == "salvage_refused_non_receipt"
            and item.get("refusal_class") == "weights"
        }
        add("build_salvage_returns_receipts_and_refuses_weights",
            bool(salvaged) and bool(refused_weights) and build["status"] != "failed",
            f"salvaged {sorted(salvaged)}; refused as weights {sorted(refused_weights)}; "
            f"status={build['status']}")

    missing = results.get("__missing_receipt__", {})
    add("missing_required_receipt_fails_the_run",
        missing.get("status") == "failed" and bool(missing.get("receipt_error")),
        f"a run whose required receipt never arrived reports status="
        f"{missing.get('status')!r} receipt_error={missing.get('receipt_error')!r}")

    comparator_runs = {name: run for name, run in results.items()
                       if name.startswith("__comparators__")}
    if not comparator_runs:
        add("comparator_phase_argv_and_cleanup", True, "comparator phase not selected")
    else:
        run = next(iter(comparator_runs.values()))
        phase = run.get("comparator_phase") or {}
        cleanup = run.get("comparator_cleanup_stages") or []
        failure_run = results.get("__comparator_cleanup_on_failure__", {})
        failure_cleanup = failure_run.get("comparator_cleanup_stages") or []
        comparator_argv = [
            entry for entry in recorder.entries
            if any("remote_comparator_eval.py" in item for item in entry["argv"])
        ]
        ok = (
            phase.get("status") == "approved"
            and bool(comparator_argv)
            and all(entry["validator"] == "accepted" for entry in comparator_argv)
            and cleanup == ["--manifest", "--post-cleanup", "rm"]
            and failure_cleanup == cleanup
        )
        add("comparator_phase_argv_and_cleanup", ok,
            f"selection {run['comparators']!r}: phase={phase.get('status')}, "
            f"{len(comparator_argv)} arm stages all accepted; deferred cleanup {cleanup} "
            f"ran on the success path and {failure_cleanup} on an injected "
            f"eval-stage failure before the phase")
        # What this selection actually asked for, from the same source vocabulary
        # the orchestrator uses: `q4-oracle` requests one arm and zero
        # comparators, so zero comparisons is its *correct* outcome.
        selection = orchestrator._comparator_selection(run["comparators"])
        wanted_arms = [f"comparator-receipt-{arm}.json"
                       for arm in orchestrator._comparator_arms(selection)]
        wanted_comparators = orchestrator._comparator_comparators(selection)
        receipt_payload = run.get("comparison_receipt") or {}
        skipped = receipt_payload.get("skipped") or []
        comparisons = receipt_payload.get("comparisons") or []
        typed = all(isinstance(item, dict) and item.get("reason") for item in skipped)
        accounted = len(comparisons) + len(skipped) >= len(wanted_comparators)
        add("comparator_refusal_is_typed", typed and accounted,
            f"selection requested {len(wanted_comparators)} comparator(s); receipt "
            f"carries {len(comparisons)} comparison(s) and {len(skipped)} skip(s) "
            f"{sorted({item.get('reason') for item in skipped})}, every skip typed")

        codes = run.get("salvage_codes") or {}
        salvaged_arms = [name for name in wanted_arms if codes.get(name) == "completed"]
        bound = {name: run["published_identity"].get(name) for name in salvaged_arms}
        arms_ok = (
            bool(wanted_arms)
            and salvaged_arms == wanted_arms
            and all(isinstance(value, dict) and value.get("run_id") == run["run_identity"]["run_id"]
                    and value.get("instance_id") == run["run_identity"]["instance_id"]
                    for value in bound.values())
        )
        add("comparator_receipts_salvaged_and_identity_bound", arms_ok,
            f"{len(salvaged_arms)}/{len(wanted_arms)} enumerated arm receipts the "
            f"{run['comparators']!r} selection requests were fetched from the source "
            f"allowlist and bound to this run: {salvaged_arms}")

        unbound_run = results.get("__comparator_unbound__", {})
        unbound_codes = unbound_run.get("salvage_codes") or {}
        stripped = unbound_run.get("stripped_receipt")
        add("an_unbound_comparator_receipt_is_refused",
            unbound_codes.get(stripped) in {"salvage_identity_missing", "salvage_identity_mismatch"},
            f"{stripped} with its run binding stripped is refused as "
            f"{unbound_codes.get(stripped)!r} and never published")

    _add_target_selection_checks(results, add)

    probe = Recorder()
    probe.record(["ssh", "-i", "/nonexistent/id_ed25519", "user@host", "df"],
                 phase="self-test", mode="self-test")
    add("injected_refusal_is_caught", probe.entries[0]["validator"] == "REFUSED",
        "a deliberately unusable -i operand is reported REFUSED by this harness")

    default_destination = ROOT / "artifacts" / "qwen35-9b"
    destination_ok, destination_detail = _destination_precondition(default_destination)
    add("operator_artifact_destination_is_salvage_ready", destination_ok, destination_detail)

    simulated = host_tree_simulation(umask_prefix=sf.remote_shell_prefix())
    add("host_tree_simulation_accepts_every_private_write",
        not simulated["refused"] and simulated["created_file_mode"] == "0600"
        and len(simulated["written_paths"]) == 2,
        f"{simulated['commands_replayed']} directory command(s) replayed under "
        f"'{simulated['umask_prefix']}' against a 0755 stand-in /scratch; "
        f"{simulated['probed_paths']} receipt path(s) accepted by "
        f"_private_ancestor_snapshot, {len(simulated['written_paths'])} published by "
        f"_private_atomic_write, a file created by the plan's own shell is "
        f"{simulated['created_file_mode']}; refused: "
        f"{[item['path'] for item in simulated['refused']] or 'none'}")

    # The same simulation against the shape run j1m-eval-20260911-remote-d
    # actually executed: the image's default umask and no explicit chmod.
    # A gate that cannot reproduce the failure it was written for proves
    # nothing, so this one has to come back refusing.
    control = host_tree_simulation(
        umask_prefix=_IMAGE_DEFAULT_UMASK_PREFIX, legacy=True)
    add("host_tree_simulation_reproduces_the_run_d_refusal",
        bool(control["refused"]) and control["created_file_mode"] != "0600"
        and not control["written_paths"],
        f"replaying the failed run's own plan (umask 022, mkdir without chmod, "
        f"no progress directory) "
        f"refuses all {control['probed_paths']} receipt path(s) and both "
        f"publish attempts ({len(control['refused'])} refusals) with "
        f"{sorted({item['error'] for item in control['refused']})} "
        f"and creates {control['created_file_mode']} files -- exactly the "
        f"exit-2 runner refusal and the 0644 salvage_not_private_regular_file "
        f"booked on 2026-09-11 for USD 3.273486")

    return checks


# Every approved-target scenario this gate drives, and what each one must
# produce.  ``expect`` is the approved-target index the run has to select;
# ``None`` means the run must be refused before anything is created.
_SELECTION_SCENARIOS = (
    ("__target_primary_only__", 0),
    ("__target_denvr_alternate__", 1),
    ("__target_crusoe_alternate__", 2),
    ("__target_catalogue_order_ignored__", 0),
    ("__target_none_approved__", None),
    ("__target_price_near_miss__", None),
    ("__target_region_near_miss__", None),
)


def _selection_runs(config: dict[str, Any], key_root: Path | None) -> dict[str, dict[str, Any]]:
    """Drive one bounded prove-mode run per approved-target scenario.

    Each scenario differs only in the fake catalogue, so the thing under test
    is the selector and nothing else.  A refusal scenario is driven the same
    way and is required to end with no cost row at all -- "refused" only counts
    when it is refused *before* the reservation, which is the property that
    makes an unavailable target cost USD 0.00 instead of a wasted instance.
    """

    catalogues = {
        "__target_primary_only__": [_candidate(config, 0)],
        "__target_denvr_alternate__": [_candidate(config, 1)],
        "__target_crusoe_alternate__": [_candidate(config, 2)],
        # Catalogue order is evidence about availability, never preference:
        # with all three present the cheapest approved entry still wins.
        "__target_catalogue_order_ignored__": [
            _candidate(config, 2), _candidate(config, 1), _candidate(config, 0),
        ],
        "__target_none_approved__": [_unapproved_candidate()],
        # A cent off the approved rate is a different offer, not a bargain.
        "__target_price_near_miss__": [_candidate(config, 1, hourly_usd=1.55)],
        "__target_region_near_miss__": [_candidate(config, 2, region="culpeper-usa-2")],
    }
    runs: dict[str, dict[str, Any]] = {}
    for name, expected in _SELECTION_SCENARIOS:
        runs[name] = drive("prove", inject_failure=False, key_root=key_root,
                           recorder=Recorder(), catalogue=catalogues[name])
        if expected is None:
            runs[name]["pre_spend_refusal"] = True
    # A full eval on the most expensive approved entry, with the comparator
    # phase on. The phase is re-derived *in run*, after the eval stages, and
    # that re-derivation used to drop the rate and republish the primary's
    # numbers over the real ones -- a 22% understatement in a receipt that
    # reads as this run's own cost evidence.
    runs["__target_crusoe_eval_comparators__"] = drive(
        "eval", inject_failure=False, key_root=key_root, recorder=Recorder(),
        comparators=DEFAULT_COMPARATORS, catalogue=[_candidate(config, 2)])
    return runs


def _add_target_selection_checks(results: dict[str, dict[str, Any]], add) -> None:
    """Assert the ordered approved-target contract over the scenario runs."""

    present = [name for name, _ in _SELECTION_SCENARIOS if name in results]
    if len(present) != len(_SELECTION_SCENARIOS):
        add("approved_target_selection_follows_the_list", False,
            "approved-target scenarios were not driven")
        return
    selected_details = []
    selection_ok = True
    for name, expected in _SELECTION_SCENARIOS:
        if expected is None:
            continue
        run = results[name]
        selection = run.get("selected_target") or {}
        index = selection.get("approved_target_index")
        if index != expected or run.get("status") != "completed":
            selection_ok = False
        selected_details.append(f"{name}->#{index}")
    add("approved_target_selection_follows_the_list", selection_ok,
        "; ".join(selected_details))

    refusal_details = []
    refusal_ok = True
    for name, expected in _SELECTION_SCENARIOS:
        if expected is not None:
            continue
        run = results[name]
        error = str(run.get("error") or "")
        refused = "no approved J1M target is an eligible current catalogue candidate" in error
        # Nothing was reserved, so nothing could be billed.
        unspent = not run.get("cost_events") and not run.get("teardown_calls")
        if not (refused and unspent):
            refusal_ok = False
        refusal_details.append(f"{name}->{'refused pre-spend' if refused and unspent else error[:60]}")
    add("unapproved_catalogue_is_refused_pre_spend", refusal_ok,
        "; ".join(refusal_details))

    cost_ok = True
    cost_details = []
    for name, expected in _SELECTION_SCENARIOS:
        if expected is None:
            continue
        run = results[name]
        selection = run.get("selected_target") or {}
        projection = selection.get("cost_projection") or {}
        events = [event for event in run.get("cost_events", [])
                  if event.get("status") == "pending"
                  and event.get("instance_id") == FAKE_INSTANCE_ID]
        rate = projection.get("hourly_usd")
        expected_rate = float(j1m_runner.load_config()["shadeform_targets"][expected]["hourly_usd"])
        reserved = events[0]["estimated_cost_usd"] if events else None
        # The reservation is priced at the selected entry's own rate, and the
        # recorded worst case stays inside the per-run cap.
        if (rate != expected_rate or not events
                or reserved != round(expected_rate * 0.3125, 6)
                or projection.get("worst_case_usd", 0) > projection.get("per_run_cap_usd", 0)):
            cost_ok = False
        cost_details.append(
            f"#{expected} @ ${expected_rate}/h reserved ${reserved} "
            f"worst case ${projection.get('worst_case_usd')} <= cap ${projection.get('per_run_cap_usd')}")
    add("selected_target_prices_every_recorded_figure", cost_ok, "; ".join(cost_details))

    run = results.get("__target_crusoe_eval_comparators__") or {}
    phase = run.get("comparator_phase") or {}
    budget = phase.get("budget") or {}
    selection = run.get("selected_target") or {}
    required = budget.get("required_seconds")
    in_run_ok = (
        selection.get("approved_target_index") == 2
        and run.get("status") == "completed"
        and budget.get("hourly_usd") == 1.65
        and budget.get("authorized_active_cost_usd") == 3.201
        and required is not None
        and budget.get("projected_marginal_cost_usd") == round(1.65 * required / 3600.0, 6)
    )
    add("in_run_comparator_phase_keeps_the_selected_rate", in_run_ok,
        f"eval + '{DEFAULT_COMPARATORS}' on approved entry "
        f"#{selection.get('approved_target_index')}: phase={phase.get('status')} "
        f"hourly=${budget.get('hourly_usd')} "
        f"marginal=${budget.get('projected_marginal_cost_usd')} "
        f"authorized=${budget.get('authorized_active_cost_usd')}")


# ------------------------------------------------------ offline host tree


# The only programs this harness will execute while replaying the remote plan.
# Everything else in the plan (apt, cmake, python3, sudo, cp, df, test) is
# recorded and never run, exactly as in the rest of this gate.
_HOST_REPLAYABLE_PROGRAMS = frozenset({"mkdir", "chmod", "touch"})
# The image default the failed run inherited, for the control replay.
_IMAGE_DEFAULT_UMASK_PREFIX = ("umask", "022", "&&")
_UMASK_PROBE_NAME = "umask-probe.json"
# The exact directory-creating commands run ``j1m-eval-20260911-remote-d``
# executed, transcribed from ``main@a0aa02cbf1be459df0eda1c4ecff2ae2e9741f9d``:
# ``j1m_orchestrator.execute()``'s ``workspace_stages`` created only
# ``/scratch/j1m``, and ``_eval_remote_commands()[0]`` created the six eval
# directories.  Neither chmodded anything, and nothing in that plan created
# the runner's own progress directory at all.  Kept verbatim as the control
# for the simulation: a gate that cannot reproduce the failure it was written
# for proves nothing.
_RUN_D_HOST_COMMANDS = (
    ["mkdir", "-p", "/scratch/j1m"],
    ["mkdir", "-p", "/scratch/j1m/model", "/scratch/j1m/engine/native",
     "/scratch/j1m/engine/vendor", "/scratch/j1m/engine/scripts",
     "/scratch/j1m/engine/tests/native", "/scratch/j1m/artifacts"],
)


def _host_replay_commands(config: dict[str, Any], *, legacy: bool) -> list[list[str]]:
    """Every directory command the live plan runs on the host, in plan order.

    Taken from the production plan builders, never retyped here, so a plan
    that stops creating a directory or stops making it private fails this
    gate instead of failing on a paid host.  ``legacy=True`` replays the
    transcribed run-d plan instead.
    """

    remote_root = "/scratch/j1m"
    if legacy:
        replayable = [list(argv) for argv in _RUN_D_HOST_COMMANDS]
    else:
        progress_relative = str(config["resources"]["progress_path"])
        commands = [argv for _name, argv in orchestrator._remote_workspace_stages(
            "hostuser", remote_root, progress_relative)]
        commands.extend(orchestrator._eval_remote_commands(
            config, remote_root, orchestrator._comparator_selection(DEFAULT_COMPARATORS)))
        replayable = [argv for argv in commands
                      if argv and argv[0] in _HOST_REPLAYABLE_PROGRAMS]
    # One synthetic probe, appended after the plan: an ordinary file creation
    # under the replayed umask, so "would a receipt written by a host-side
    # tool be 0600?" is answered by the shell rather than by assertion.
    replayable.append(["touch", f"{remote_root}/artifacts/{_UMASK_PROBE_NAME}"])
    return replayable


def _host_receipt_targets(config: dict[str, Any]) -> list[str]:
    """Every host path the runner or a probe must be able to publish to."""

    remote_root = "/scratch/j1m"
    host_root = PurePosixPath(remote_root).parent
    names = orchestrator._eval_fetch_allowlist(
        config, orchestrator._comparator_selection(DEFAULT_COMPARATORS))
    return [
        # The runner's own progress marker: written before the first stage of
        # `--run`, from `ROOT / resources.progress_path`, where `ROOT` is the
        # uploaded runner's grandparent -- /scratch, not /scratch/j1m.
        str(host_root / config["resources"]["progress_path"]),
        f"{remote_root}/artifacts/command-receipt.json",
        *[f"{remote_root}/artifacts/{name}" for name in names],
    ]


def host_tree_simulation(*, umask_prefix: tuple[str, ...] | list[str],
                         legacy: bool = False) -> dict[str, Any]:
    """Replay the plan's host directory commands under a stand-in ``/scratch``.

    The failure this exists for is not reachable by argv inspection: run
    ``j1m-eval-20260911-remote-d`` built a perfectly valid plan whose commands
    were all accepted by every local validator, and then created ``0755``
    directories on the host because the remote login shell carried the image's
    ``umask 022``.  The runner's own private writer refused its first write
    and exited 2; the probe receipts landed ``0644`` and the salvage refused
    them.  Nothing local could see any of that.

    So the plan's directory-creating commands are executed here, as shell text,
    against a temporary directory standing in for ``/scratch`` -- deliberately
    created ``0755`` and owned by this user, the shape an image's own
    ``/scratch`` has after the plan chowns it -- and the result is judged with
    the *production* privacy predicates: ``_private_ancestor_snapshot`` for
    every receipt path the run will publish, and a real ``_private_atomic_write``
    for the two the runner writes first.
    """

    config = j1m_runner.load_config()
    commands = _host_replay_commands(config, legacy=legacy)
    prefix = list(umask_prefix)
    scratch = Path(tempfile.mkdtemp(prefix="j1m-host-sim-"))
    # The replayed shell inherits THIS process's umask, so an operator whose
    # own umask is already 077 would watch this simulation pass with the
    # remote prefix deleted -- precisely the defect it exists to catch. Force
    # the provider image's default for the whole simulation: from here on only
    # the replayed prefix and the plan's own explicit chmods can make anything
    # private, and `_private_atomic_write` has to set its mode rather than
    # inherit a lucky one.
    previous_umask = os.umask(0o022)
    try:
        # The trusted root is a policy boundary, not a private directory: the
        # snapshot requires it to be owned by this user and not group/other
        # writable, which 0755 satisfies. Starting at 0755 proves that.
        os.chmod(scratch, 0o755)
        # A fresh stand-in lets every `mkdir -p` create its own target, which
        # would leave the plan's explicit `chmod 700` stages dead code this
        # gate never exercises -- delete them and it still passes. Seed one
        # private path as an ALREADY-EXISTING 0755 directory: the shape a
        # provider image leaves behind, and exactly the case `mkdir -p`
        # silently does nothing about, so only an explicit chmod can still
        # make it private.
        seeded = Path(str(scratch) + "/j1m/artifacts")
        seeded.mkdir(parents=True, exist_ok=True)
        os.chmod(seeded.parent, 0o755)
        os.chmod(seeded, 0o755)

        def host(path: str) -> Path:
            if not path.startswith("/scratch"):
                raise DryRunError("host simulation may only replay /scratch paths")
            return Path(str(scratch) + path[len("/scratch"):])

        replayed: list[str] = []
        for argv in commands:
            text = " ".join(prefix + [
                str(host(part)) if part.startswith("/scratch") else part
                for part in argv])
            result = subprocess.run(
                ["/bin/sh", "-c", text], check=False, capture_output=True,
                text=True, timeout=60, cwd=str(scratch),
            )
            replayed.append(" ".join(prefix + argv))
            if result.returncode != 0:
                raise DryRunError(
                    f"host simulation could not replay {argv[0]!r} "
                    f"(exit {result.returncode})")

        probe = host(f"/scratch/j1m/artifacts/{_UMASK_PROBE_NAME}")
        probe_mode = f"{stat.S_IMODE(os.stat(probe).st_mode):04o}"
        os.unlink(probe)

        refused: list[dict[str, str]] = []
        targets = _host_receipt_targets(config)
        for target in targets:
            try:
                j1m_runner._private_ancestor_snapshot(host(target), scratch)
            except ValueError as exc:
                refused.append({"path": target, "error": str(exc)})
        # The two the runner itself writes first are proven by writing them.
        written: list[str] = []
        for target in targets[:2]:
            try:
                j1m_runner._private_atomic_write(
                    host(target), b'{"host-simulation": true}\n', trusted_root=scratch)
                written.append(target)
            except ValueError as exc:
                refused.append({"path": target, "error": str(exc)})
        return {
            "umask_prefix": " ".join(prefix) or "<none>",
            "plan": "run-d" if legacy else "current",
            "commands_replayed": len(replayed),
            "root_mode": "0755",
            "process_umask": "0022",
            "seeded_existing_0755": "/scratch/j1m/artifacts",
            "probed_paths": len(targets),
            "written_paths": written,
            "created_file_mode": probe_mode,
            "refused": refused,
        }
    finally:
        os.umask(previous_umask)
        shutil.rmtree(scratch, ignore_errors=True)


def _destination_precondition(destination: Path) -> tuple[bool, str]:
    """Prove the live command's real destination would accept a publication.

    Salvage publishes only through a no-follow descriptor walk from the trusted
    root, which requires every component below that root to be owner-private.
    The checked-in default is an ordinary ``0755`` directory, so a live run
    would reach teardown with every receipt refused.  That is an operator
    precondition rather than a source defect, and this gate is where it has to
    be visible.
    """

    try:
        orchestrator.prepare_artifact_destination(destination)
    except Exception as exc:
        return False, f"{exc}"
    return True, f"{destination} accepts a descriptor-safe private publication"


# ------------------------------------------------------------------ reporting


def real_key_root_residue() -> list[str]:
    """Every directory sitting in the real per-run key root, with exact paths.

    Reported, never deleted. A live run's key directory is removed by the
    orchestrator's own teardown, so anything left here is either a crashed run
    or a cleanup that failed -- a signal worth surfacing to an operator and
    exactly the wrong thing for a pre-launch gate to quietly tidy away.
    """

    try:
        return sorted(str(item) for item in sf.EPHEMERAL_KEY_ROOT.iterdir())
    except OSError:
        return []


@contextlib.contextmanager
def dry_run_key_root(key_root: Path | None):
    """Yield the key root this invocation owns, and remove exactly it after.

    A caller-supplied root is used as-is and left alone. Otherwise the fake runs
    get their own uniquely named subtree of the real root, so they neither
    depend on nor touch a real run's residue.
    """

    if key_root is not None:
        yield Path(key_root)
        return
    sf.EPHEMERAL_KEY_ROOT.parent.mkdir(mode=0o700, exist_ok=True)
    sf.EPHEMERAL_KEY_ROOT.mkdir(mode=0o700, exist_ok=True)
    owned = sf.EPHEMERAL_KEY_ROOT / f"dryrun-{secrets.token_hex(8)}"
    owned.mkdir(mode=0o700)
    try:
        yield owned
    finally:
        shutil.rmtree(owned, ignore_errors=True)


def run_dry_run(modes: tuple[str, ...] = ("eval",), *, key_root: Path | None = None,
                receipt_path: Path | None = None,
                comparators: str = DEFAULT_COMPARATORS) -> dict[str, Any]:
    """Drive every requested mode offline and return the complete receipt."""

    # Snapshot residue *before* this invocation creates its own subtree, so the
    # warning reports what was already there and never this run's own workspace.
    residue = real_key_root_residue() if key_root is None else []
    with dry_run_key_root(key_root) as owned_root:
        return _run_dry_run(modes, key_root=owned_root, receipt_path=receipt_path,
                            comparators=comparators,
                            residue=[path for path in residue
                                     if path != str(owned_root)])


def _run_dry_run(modes: tuple[str, ...], *, key_root: Path, receipt_path: Path | None,
                 comparators: str, residue: list[str]) -> dict[str, Any]:
    recorder = Recorder()
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    for mode in modes:
        results[mode] = drive(mode, inject_failure=False, key_root=key_root, recorder=recorder)
    if comparators and "eval" in modes:
        # The comparator phase is default OFF, so it needs its own run: its
        # stages are argv like any other and must pass the same policy.
        results[f"__comparators__{comparators}"] = drive(
            "eval", inject_failure=False, key_root=key_root, recorder=recorder,
            comparators=comparators)
        # And its deferred cleanup tail has to survive a failure *before* the
        # comparator phase is ever reached -- the path that used to skip it.
        results["__comparator_cleanup_on_failure__"] = drive(
            "eval", inject_failure=False, key_root=key_root, recorder=Recorder(),
            comparators=comparators, fail_eval_stage="remote_model_eval.py")
        # And an arm receipt with its binding stripped must be refused rather
        # than becoming a number this run is about to pay for.
        results["__comparator_unbound__"] = _unbound_comparator_run(comparators, key_root)
    # The ordered approved-target list is exercised on its own catalogues: the
    # primary is the only entry the default fake catalogue offers, so without
    # these the alternate path would never be executed by this gate at all.
    results.update(_selection_runs(j1m_runner.load_config(), key_root))
    primary = modes[0]
    results["__injected_failure__"] = drive(
        primary, inject_failure=True, key_root=key_root, recorder=Recorder())
    results["__missing_receipt__"] = _missing_receipt_run(primary, key_root)

    checks = evaluate(results, recorder, modes=modes)
    failed = [item for item in checks if item["status"] == "FAIL"]
    warnings = []
    if residue:
        warnings.append({
            "warning": "stale_key_directories_in_the_real_key_root",
            "detail": (f"{len(residue)} directory(ies) left under {sf.EPHEMERAL_KEY_ROOT}; "
                       "a live run's key directory is removed by teardown, so these are "
                       "crashed or failed cleanups. Left in place deliberately: this gate "
                       "reports them and does not delete another run's evidence."),
            "paths": residue,
        })
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS" if not failed else "FAIL",
        "warnings": warnings,
        "modes": list(modes),
        "checks": checks,
        "failed_checks": [item["check"] for item in failed],
        "argv_count": len(recorder.entries),
        "commands": recorder.entries,
        "runs": {
            name: {key: value for key, value in run.items()
                   if key in {"mode", "status", "error", "receipt_error", "build_receipts",
                              "key_cleanup", "key_directory_present", "published",
                              "unknown_subprocesses", "inject_failure", "salvage_codes",
                              "comparators", "comparator_phase", "comparator_cleanup_error",
                              "comparator_cleanup_stages", "comparison_receipt",
                              "fail_eval_stage", "stripped_receipt",
                              "published_identity", "run_identity",
                              "selected_target", "pre_spend_refusal"}}
            for name, run in results.items()
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
        "network": "none",
        "provider_calls": "none",
        "spend_usd": 0.0,
    }
    _assert_no_sentinel(receipt)
    if receipt_path is not None:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _unbound_comparator_run(comparators: str, key_root: Path | None) -> dict[str, Any]:
    """Drive one run whose Q4 arm receipt has lost its run-identity binding."""

    stripped = "comparator-receipt-q4_k_m.json"
    real_fake_receipts = fake_receipts

    def without_binding(*args, **kwargs):
        receipts = real_fake_receipts(*args, **kwargs)
        payload = json.loads(receipts[stripped])
        payload.pop("run_id", None)
        receipts[stripped] = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        return receipts

    with mock.patch.object(sys.modules[__name__], "fake_receipts", side_effect=without_binding):
        outcome = drive("eval", inject_failure=False, key_root=key_root,
                        recorder=Recorder(), comparators=comparators)
    outcome["stripped_receipt"] = stripped
    return outcome


def _missing_receipt_run(mode: str, key_root: Path | None) -> dict[str, Any]:
    """Drive one run whose required receipt never arrives on the host."""

    recorder = Recorder()
    required = {
        "eval": "eval-receipt.json",
        "prove": "proving-receipt.json",
        "build": "manifest.json",
    }[mode]
    real_fake_receipts = fake_receipts

    def without_required(*args, **kwargs):
        receipts = real_fake_receipts(*args, **kwargs)
        receipts.pop(required, None)
        return receipts

    with mock.patch.object(sys.modules[__name__], "fake_receipts", side_effect=without_required):
        outcome = drive(mode, inject_failure=False, key_root=key_root, recorder=recorder)
    outcome["withheld_receipt"] = required
    return outcome


def _assert_no_sentinel(receipt: dict[str, Any]) -> None:
    """The receipt is published evidence; it must carry no planted secret."""

    serialized = json.dumps(receipt, sort_keys=True)
    for name, value in SENTINELS.items():
        if value in serialized:
            raise DryRunError(f"dry-run receipt would have disclosed {name}")


def render(receipt: dict[str, Any]) -> str:
    lines = [
        f"J1M offline dry run: {receipt['status']}",
        f"  modes        : {', '.join(receipt['modes'])}",
        f"  argv recorded: {receipt['argv_count']} (none executed)",
        f"  network      : none    provider calls: none    spend: $0.00",
        "",
    ]
    for item in receipt["checks"]:
        lines.append(f"  [{item['status']}] {item['check']}")
        lines.append(f"         {item['detail']}")
    for item in receipt.get("warnings", []):
        lines.append("")
        lines.append(f"  [WARN] {item['warning']}")
        lines.append(f"         {item['detail']}")
        for path in item["paths"]:
            lines.append(f"           {path}")
    if receipt["failed_checks"]:
        lines.append("")
        lines.append("  FAILED: " + ", ".join(receipt["failed_checks"]))
        lines.append("  Do not launch a paid run until every check passes.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", action="append", choices=MODES,
                        help="lifecycle mode to drive; repeatable, defaults to every mode")
    parser.add_argument("--evaluate-comparators", default=DEFAULT_COMPARATORS,
                        help="comparator selection to gate as well; '' skips the phase")
    parser.add_argument("--receipt", type=Path,
                        default=ROOT / "experiments" / "runtime" / "dry-run-receipt.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    modes = tuple(dict.fromkeys(args.mode or MODES))
    receipt = run_dry_run(modes, receipt_path=args.receipt,
                          comparators=args.evaluate_comparators)
    if not args.quiet:
        print(render(receipt))
        print(f"\n  receipt: {args.receipt}")
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
