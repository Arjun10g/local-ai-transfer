# J1M remote host privacy

Slice `J1M-HOST-PRIVACY-001`. Owner S4 Luna. Depends on `J1M-KEY-HANDLE-001`.
Companion to `scripts/shadeform/SALVAGE_TRANSPORT.md`: that document explains
how a receipt is *fetched* from the ephemeral host; this one explains how a
receipt comes to exist on that host in a state the fetch will accept.

## 1. The incident

Run `j1m-eval-20260911-remote-d`, 2026-09-11.

| Fact | Value |
| --- | --- |
| Instance | `d75747f8-4801-4f72-8a5f-5713ef333905`, hyperstack, `A100_80G`, montreal-canada-2 |
| Active | 2026-09-11T20:24:07Z created, 20:28:44Z active |
| Booked cost | USD 3.273486 (`deletion.actual_cost_usd`) |
| Failed stage | `eval-stage:j1m_runner` |
| Salvage | 8 requested, 0 completed |
| Teardown | deletion confirmed, no orphan, ephemeral key removed |

Eval stages 1–4 completed (`mkdir`, `apt-get update`, `apt-get install`, the
toolchain probe). Stage 5 — `python3 /scratch/j1m/j1m_runner.py --run …` —
failed with `remote_exit`, exit code 2, and an **empty** `stderr_tail`. Stage 7
then failed 127 with `bash: line 1: /scratch/j1m/venv/bin/python: No such file
or directory`, because the venv stage inside the runner had never run.

Salvage reported seven `salvage_transport_failed` (exit 1 — the files had never
been written) and one `salvage_not_private_regular_file` for
`toolchain-receipt.json`, which *had* been written, at mode `0644`.

Nothing in the plan was malformed. Every command passed
`j1m_runner.validate_persisted_argv`, and `scripts/j1m_dry_run.py` had passed.

## 2. Root cause

Two independent defects, both invisible to any local validator.

### 2.1 The remote login shell's umask

`sf.ssh_base(...) + argv` is executed by the login shell OpenSSH starts on the
host, so that shell's umask decides the mode of everything the command creates.
The provider image ships the distribution default `022`.

- `mkdir -p /scratch/j1m/model … /scratch/j1m/artifacts` therefore created
  `0755` directories.
- `j1m_runner.PRIVATE_OUTPUT_ROOT` is `ROOT`
  (`scripts/j1m_runner.py:32`), and on the host the uploaded runner is
  `/scratch/j1m/j1m_runner.py`, so `parents[1]` makes the trusted root
  **`/scratch`**, not `/scratch/j1m`.
- `_private_ancestor_snapshot` (`:474-520`) accepts the configured root when it
  is owned by the current uid and not group/other writable (`& 0o022`), but
  requires **every descendant directory** to be fully owner-private
  (`& 0o077`), owned by the current uid, and not a symlink.
- `run_commands` (`:1697`) writes `/scratch/j1m/artifacts/command-receipt.json`
  through `_private_atomic_write` *before the first stage runs*. With
  `/scratch/j1m` at `0755` that raises `private ancestor is unsafe`,
  `_safe_cli` prints its typed JSON refusal and returns **2**.

The same `umask 022` gave the toolchain probe's receipt `0644`, which
`_salvage_staged_bytes` refuses as `salvage_not_private_regular_file`. That
refusal was correct and is unchanged.

### 2.2 The progress directory that no plan created

Found while proving the first fix, and independently fatal. `run_commands` is
called with `ROOT / config["resources"]["progress_path"]`, which on the host is
`/scratch/experiments/runtime/J1M.progress.json`. No remote plan ever created
`/scratch/experiments` or `/scratch/experiments/runtime`, so even a correctly
permissioned `/scratch/j1m/artifacts` would have left the runner refusing on
its first progress write with `private ancestor is unavailable`.

### 2.3 Why the receipt could not say any of this

`_remote` recorded only `stderr_tail`. `j1m_runner._safe_cli` prints its typed
refusal on **stdout**. The lifecycle receipt for a billed failure therefore
carried `exit 2` and an empty string.

## 3. The fix

No privacy predicate was weakened. `_private_ancestor_snapshot`,
`_private_atomic_write` and the salvage refusal codes are byte-unchanged.

### 3.1 One umask, one place

`scripts/shadeform_lifecycle.py` gains `REMOTE_SHELL_UMASK = "077"` and
`remote_shell_prefix() -> ["umask", "077", "&&"]`, and `ssh_base` now **ends**
with that prefix. Every remote command built anywhere in the tree — the J1M
orchestrator's workspace preflight, eval, comparator, canary and cleanup plans,
and `scripts/shadeform/remote_external_tools.py` — inherits it, so no
individual command can forget it.

- `&&`, not `;`: a shell that cannot set the umask must not go on to create
  world-readable state.
- `scp_base` is deliberately untouched. `scp` runs no login shell, and a
  `umask` operand there would be interpreted as a filename.
- The commands themselves stay argument vectors. The prefix is the only shell
  text the transport ever adds, and
  `tests/performance/test_j1m_host_privacy.py` pins that.
- `sudo` preserves it: sudo's umask is the union of the caller's umask and the
  sudoers value, so `sudo mkdir -p /scratch` under `077` still creates `0700`.

### 3.2 Explicit modes, because a umask cannot repair what already exists

Every directory-creating command is now followed by an explicit `chmod 700` of
every path it names, intermediate components included (`chmod` is not
recursive, and `mkdir -p` leaves a pre-existing directory alone).

- `_remote_workspace_stages(ssh_user, remote_root, progress_relative)` is now a
  module-level function with six stages: `scratch_root`, `scratch_owner`,
  `remote_workspace`, `remote_workspace_private`, `scratch_df`,
  `scratch_writable`. The paths it creates come from
  `_host_private_directories`, which derives them from the configured
  `resources.progress_path` rather than hardcoding them:
  `/scratch/j1m`, `/scratch/j1m/artifacts`, `/scratch/experiments`,
  `/scratch/experiments/runtime`.
- `_eval_remote_commands` now emits `mkdir -p …` followed by `chmod 700 …` over
  the same eight paths, including the `engine` and `engine/tests` parents the
  old plan created implicitly.
- `/scratch` itself is **not** forced to `0700`. It is the policy boundary, not
  a private directory: the root check is `& 0o022` only, so an image's own
  root-owned `0755` `/scratch` is acceptable once `scratch_owner` chowns it to
  the ssh user. Forcing `0700` on a shared mountpoint would be a change to the
  image, not to this run.

### 3.3 Receipts are published private

The five uploaded host-side writers — `remote_toolchain_probe.py`,
`cuda_device_probe.py`, `remote_eval_prepare.py`, `remote_model_eval.py`
(startup preflight and eval receipt) and `remote_comparator_eval.py` — each
publish through an identical local `_publish_private_receipt(output, encoded)`:
`mkdir -p` the parent, `chmod 0700` it, `mkstemp` + `fchmod 0600` + `fsync` in
that same directory, then `os.replace` over the final name. Duplicated per file
rather than shared, because these scripts are uploaded individually and no
shared module travels with them — the same pattern `_run_identity` already
uses.

Result: mode `0600`, single link, owner `= uid`, atomic, in a `0700`
directory — the exact shape `_salvage_staged_bytes` requires, so our own
receipts can never trip `salvage_not_private_regular_file` again.

### 3.4 A failed stage is readable

`_remote` now records `stdout_tail` for a failed stage and for a transport
timeout, through `_failed_stage_output_tail`: the same `_redacted_output_tail`
screening every other persisted value gets, plus a re-validation of the
**truncated** tail, so a fragment the persisted-value validator would reject
becomes `<redacted>` rather than a receipt this process cannot write.

Deliberate scope change, recorded here because it replaces a previously pinned
invariant ("a failed remote receipt retains no stdout"): a completed stage
still retains nothing, the tail is bounded to `_STDERR_TAIL_LIMIT` = 1200
bytes, and credential-shaped output is still `<redacted>`. Residual: a failed
stage's ordinary (non-credential) stdout is now persisted. The evaluator stages
print a single JSON summary — `schema`, `status`, `metrics` — and no prompt or
response text, so the practical exposure is the typed refusal this change
exists to surface.

## 4. The offline proof

`scripts/j1m_dry_run.py` gains `host_tree_simulation()`. Argv inspection cannot
see a `0755` directory, so the plan's own directory text is executed.

1. `tempfile.mkdtemp()` stands in for `/scratch`, chmodded **0755** — the shape
   an image's `/scratch` has after `scratch_owner` chowns it. A simulation that
   quietly made the root `0700` would prove nothing.
2. The directory-creating commands are taken from the production builders
   (`_remote_workspace_stages`, `_eval_remote_commands`), filtered to
   `{mkdir, chmod, touch}`, `/scratch` is substituted for the temporary root,
   and each is run as `sh -c "umask 077 && <command>"` — the exact text
   `ssh_base` will send. One synthetic `touch` is appended so "would a file
   created by this shell be 0600?" is answered by the shell.
3. The result is judged with the production predicates:
   `_private_ancestor_snapshot` for all 11 receipt paths the widest comparator
   selection can publish, then a real `_private_atomic_write` of the progress
   file and the command receipt.

Two gate checks, because a gate that cannot reproduce the failure it was
written for proves nothing:

| Check | Result |
| --- | --- |
| `host_tree_simulation_accepts_every_private_write` | 5 directory commands replayed under `umask 077 &&`; 11/11 paths accepted; 2 published; a file created by the plan's own shell is `0600`; refused: none |
| `host_tree_simulation_reproduces_the_run_d_refusal` | the transcribed run-d plan (`umask 022`, `mkdir` without `chmod`, no progress directory) refuses all 11 paths and both publish attempts — 13 refusals — with `private ancestor is unavailable` and `private ancestor is unsafe`, and creates `0644` files |

The control's commands are `_RUN_D_HOST_COMMANDS`, transcribed verbatim from
`main@a0aa02cbf1be459df0eda1c4ecff2ae2e9741f9d`, so the simulation demonstrably
fails on `main`'s plan and passes on this one.

This is the only thing `j1m_dry_run.py` executes. There is still no network, no
provider call and no spend; the module docstring says so explicitly.

## 5. What is still not proved

The simulation proves what the plan *would* create, using this machine's
filesystem semantics. It does not prove that the provider image's `/scratch` is
a directory rather than a symlink, that it is on a filesystem honouring POSIX
modes, or that `sudo chown` succeeds there. Those remain the
`scratch_root`/`scratch_owner`/`scratch_writable` preflight stages' job on the
live host, and each of them is fail-closed.
