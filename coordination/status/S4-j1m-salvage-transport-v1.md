# Status packet — J1M bounded receipt-salvage transport

- **Role:** Luna implementation worker (S4).
- **Branch/worktree:** `luna/j1m-salvage-transport-v1` in sibling worktree
  `wt-j1m-salvage-transport-v1`.
- **Base:** exact `main@360519274b2be2f04a61297650a4b0586016201d`. `main` was
  not modified. No provider, network, orchestrator, lifecycle, teardown, or
  catalogue command was run.
- **Claimed task:** `J1M-SALVAGE-TRANSPORT-001` — restore a bounded,
  fail-closed receipt-salvage transport so a remote evaluation run can return
  its receipts, without discarding the security rationale of `2d7db4f`.
- **Dependencies:** B-004, SOL-003. Review owners S0, S2.
- **Design note:** `scripts/shadeform/SALVAGE_TRANSPORT.md`.

## Problem

`scripts/j1m_orchestrator.py:_salvage` raised unconditionally. Its sole call
site is inside the teardown `finally`, so `--mode eval --execute` would create
a paid A100, run the full pipeline, write all five receipts on the host, fail
to retrieve any of them, mark the run `failed`, and destroy the instance —
spending money for nothing. The refusal came from `2d7db4f`, whose rationale
(a pathname handed to SCP cannot stay bound to the validated destination
across the transfer) is correct, so a revert was not an option.

## Approach

Preserve the rationale by removing its premise. SCP is never given the
validated destination. Transfers land in a fresh private staging directory
whose contents stay untrusted for their whole lifetime; the only path that
ever names the run's artifact directory is
`j1m_runner._private_atomic_write`, which walks `O_DIRECTORY | O_NOFOLLOW`
descriptors from the trusted root, writes a `0600` temp file by descriptor,
`fsync`s, `os.replace`s with `src_dir_fd`/`dst_dir_fd`, and re-reads to
byte-compare. `_salvage` additionally re-proves the destination inode
immediately after the ancestor-stability check and before any process exists.

## Bounds and gates

- Allowlist: six receipt basenames fixed in source, each mapped to its exact
  required schema, under the single source constant `/scratch/j1m/artifacts`
  that the orchestrator itself named at launch. No glob, no directory listing,
  no caller or configuration path reaches a remote operand.
- Caps: 4 MiB per file (refused, never truncated; the strict decoder's 2 MiB
  bound is checked first so oversize reports oversize), 32 MiB total, fetches
  bounded by the allowlist size, 64 candidate names, 300 s wall clock, 60 s per
  transfer, and the existing 660 s deletion reserve retained.
- Argv: an argument vector, never a shell string — `-4`, `ForwardAgent=no`,
  `ForwardX11=no`, `ClearAllForwardings=yes`, `ExitOnForwardFailure=yes`,
  `PermitLocalCommand=no`, plus `scp_base`'s `-F /dev/null`, `BatchMode=yes`,
  `StrictHostKeyChecking=yes`, pinned `UserKnownHostsFile`, `ConnectTimeout=15`,
  `IdentitiesOnly=yes`, disabled control master, `-i <ephemeral key>`,
  `-P <port>`. No `-r`. Endpoint from the same activation record the run used.
- Validation before publication: descriptor-bounded read; strict JSON with
  duplicate-key rejection; exact schema for that exact name; required
  top-level keys; credential-content screening; and run-identity binding
  (`run_id`, `instance_id`, and the approved artifact `name`/`size_bytes`/
  `sha256` wherever the receipt asserts them).
- Host-key pinning: salvage never re-pins and never re-scans. It reuses the
  `known_hosts` written by `sf.acquire_pinned_host_key` before the first remote
  command — authoritative when the provider exposes a fingerprint, otherwise
  two independent stable `ssh-keyscan` passes recorded as
  `two-stable-bounded-scans-residual-tofu`. Before any transfer it proves that
  file is still owner-private, non-symlink, single-link, bounded, non-empty,
  and carries the same key count, and records its SHA-256. A host swapped
  between run and teardown fails `StrictHostKeyChecking` with a typed
  `salvage_host_key_mismatch` instead of silently re-pinning.

## Fail-closed and teardown

Per-file failures never raise; each records a typed, value-free code (18 of
them, tabulated in the design note). Only three conditions refuse the whole
call — an unproved destination, an over-count request, an unusable host-key
pin — and all three are proved before any process exists; the caller's
`finally` already converts that into `salvage_failed` and continues to
`teardown_exact`. Salvage failure cannot leave an instance running. A missing
allowlisted receipt is non-fatal for teardown and fatal for run success via the
existing `receipt_error` path. Local evidence is `salvage-receipt.json`, which
is itself not fetchable; a failure to write it is swallowed.

## Scope decision recorded

The deployable Q4 GGUF is no longer salvageable. `build` mode's
`local_fetch_allowlist` names it and several non-JSON build outputs; under this
transport they are refused as not-allowlisted. Moving a multi-gigabyte artifact
needs its own integrity and budget story and is deliberately not smuggled into
a receipt transport.

## Evidence (reproduced in-worktree)

- `python3 -m unittest tests.performance.test_remote_canary_secret_hardening
  tests.performance.test_j1m_lifecycle tests.performance.test_cost_ledger_genesis
  tests.performance.test_j1m_salvage_transport` → **Ran 208 tests, OK**
  (46 of them new).
- `python3 scripts/test/run_qa.py --root . --skip-native --output -` →
  **65 discovered, 0 missing, 0 unknown**, 71 records (1 PASS / 70 expected
  SKIP), overall `BLOCKED` (unchanged, pre-existing).
- `git diff --check main...HEAD` → exit 0.
- `git status --short` → empty.

No network, provider, model, remote, or live execution occurred; `_remote` is
mocked in every test and a real transfer would be a test failure. No spend.

## Disclosed blocker outside this slice

`validate_persisted_argv` (introduced by `991b70e`) requires the `-i` operand
of any persisted argv to be a canonical private handle. `sf.create_keypair`
names the ephemeral key `id_ed25519` inside a system `TemporaryDirectory`,
which satisfies neither `_HANDLE_BASENAME` nor the ancestor rule (`/var` is a
symlink on macOS; `/tmp` is mode `1777` and is permitted only as a *direct*
parent). `_remote` validates every command, so **every** ssh/scp invocation in
`execute()` — workspace stages, the three J1M uploads, every eval stage, and
this salvage — is refused before it spawns. Reproduction against unmodified
`main` is in the design note §8.

This slice does not fix it: the fix is a decision about where credential
material lives and what a handle basename may be, which is Sol's. Salvage
surfaces it as the typed, value-free `salvage_identity_handle_unusable` rather
than an opaque local failure. Recommended remediation is to give the ephemeral
key a handle-shaped basename *and* place the per-run private directory under
the `.secrets/`-style repository-local mode-`0700` layout established by
`b306665`; widening `_HANDLE_BASENAME` is the weaker option and does not fix
the ancestor rule. A real eval run cannot complete until that is resolved.

## Assumptions

- `--artifact-destination` is the per-run directory
  (`artifacts/qwen35-9b/<run-id>/`) and must already be owner-private `0700`
  with owner-private ancestors; that precondition is unchanged from the prior
  implementation.
- The config `eval_fetch_allowlist` and `prove_fetch_allowlist` are the
  operator-visible parity view; source remains the only thing that makes a name
  fetchable.
- There is no comparison receipt in the current configuration; the allowlist is
  the five eval receipts plus `proving-receipt.json`.

## Uncertainties

- The initial host-key pin remains TOFU whenever Shadeform exposes no
  fingerprint. Salvage inherits that and cannot improve on it; it guarantees
  only that the key trusted at teardown is the key trusted at run start.
- No compile, provider, live, remote, or target evidence. No gate approval is
  requested, and no readiness claim is made.
