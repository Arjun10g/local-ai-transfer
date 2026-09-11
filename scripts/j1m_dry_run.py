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

No network, no provider call, no process launch, no spend.  Run it as the final
pre-launch step of the live-run lane:

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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path
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


def _candidate(config: dict[str, Any]):
    target = config["shadeform_target"]
    return sf.Candidate(
        target["gpu"], target["cloud"], target["region"], target["gpu"],
        float(target["hourly_usd"]), 80, "ubuntu22.04_cuda12.2_shade_os", False,
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
          fail_eval_stage: str = "") -> dict[str, Any]:
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
            mock.patch.object(sf, "list_candidates", return_value=[_candidate(config)]),
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
            "comparator_phase": final.get("comparator_phase"),
            "comparator_cleanup_error": final.get("comparator_cleanup_error"),
            "comparator_cleanup_stages": _cleanup_stages(recorder, since=recorded_from),
            "comparison_receipt": _comparison_receipt(destination),
            "lifecycle_persisted": bool(persisted),
            # Read the binding out of every published receipt now: the isolated
            # namespace does not outlive this block.
            "published_identity": _published_identity(destination),
        })
        key_directory = sf.ephemeral_key_directory(f"{run_id}-{nonces[-1]}") if nonces else None
        outcome["key_directory_present"] = bool(key_directory and key_directory.exists())
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
    root = key_root if key_root is not None else sf.EPHEMERAL_KEY_ROOT
    try:
        return sorted(item.name for item in Path(root).iterdir())
    except OSError:
        return []


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

    key_ok = all(
        run["key_cleanup"].get("status") in {"removed", "absent"}
        and not run["key_directory_present"] and not run["key_root_residue"]
        for run in results.values()
    )
    add("ephemeral_key_removed_on_every_path", key_ok,
        "; ".join(f"{name}={run['key_cleanup'].get('status')}" for name, run in results.items()))

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
        receipt_payload = run.get("comparison_receipt") or {}
        skipped = receipt_payload.get("skipped") or []
        typed = bool(skipped) and all(
            isinstance(item, dict) and item.get("reason") for item in skipped)
        add("comparator_refusal_is_typed",
            typed or bool(receipt_payload.get("comparisons")),
            f"comparison receipt records {len(skipped)} typed skip(s) "
            f"{sorted({item.get('reason') for item in skipped})} rather than an "
            f"empty list indistinguishable from asking for nothing")

    probe = Recorder()
    probe.record(["ssh", "-i", "/nonexistent/id_ed25519", "user@host", "df"],
                 phase="self-test", mode="self-test")
    add("injected_refusal_is_caught", probe.entries[0]["validator"] == "REFUSED",
        "a deliberately unusable -i operand is reported REFUSED by this harness")

    default_destination = ROOT / "artifacts" / "qwen35-9b"
    destination_ok, destination_detail = _destination_precondition(default_destination)
    add("operator_artifact_destination_is_salvage_ready", destination_ok, destination_detail)

    return checks


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


def run_dry_run(modes: tuple[str, ...] = ("eval",), *, key_root: Path | None = None,
                receipt_path: Path | None = None,
                comparators: str = DEFAULT_COMPARATORS) -> dict[str, Any]:
    """Drive every requested mode offline and return the complete receipt."""

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
    primary = modes[0]
    results["__injected_failure__"] = drive(
        primary, inject_failure=True, key_root=key_root, recorder=Recorder())
    results["__missing_receipt__"] = _missing_receipt_run(primary, key_root)

    checks = evaluate(results, recorder, modes=modes)
    failed = [item for item in checks if item["status"] == "FAIL"]
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS" if not failed else "FAIL",
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
                              "fail_eval_stage"}}
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
