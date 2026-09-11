# J1M receipt-salvage transport

Slice `J1M-SALVAGE-TRANSPORT-001`. Owner S4 Luna. Implements the bounded
external salvage path in `scripts/j1m_orchestrator.py:_salvage`, called once
from the teardown `finally` block of `execute()`.

## 1. Why salvage exists at all

A J1M remote run does its real work on an ephemeral, paid Shadeform instance.
Every receipt the run produces — the tool-call evaluation result, the artifact
attestation, the CUDA device and toolchain probes, the engine startup
preflight — is written on that host under `/scratch/j1m/artifacts/`. The
instance is destroyed in the same `finally` block moments later, and the
ephemeral SSH key lives in a `TemporaryDirectory` that is removed when
`execute()` returns. If the receipts are not pulled before teardown, they are
gone permanently and the run has bought nothing.

`execution/SHADEFORM_EXECUTION.md` therefore makes salvage-before-teardown a
lifecycle obligation, and makes "no idle instance" an absolute one. Those two
obligations are in tension, and the ordering below resolves it: salvage is
strictly best effort, teardown is not.

## 2. What `2d7db4f` removed, and why it was right

Commit `2d7db4f` ("security: refuse unbound external salvage", 2026-09-08)
deleted the SCP fetch loop and replaced it with an unconditional refusal:

```python
raise ValueError("external salvage transport is unavailable in this source slice")
```

The stated rationale, preserved verbatim in the constant it introduced, was
that *a pathname passed to SCP cannot remain bound to the validated directory
across an untrusted remote transfer*. That is a correct and specific
observation, not a vague misgiving. The removed loop did this:

```python
command = sf.scp_base(...) + [
    f"{user}@{ip}:/scratch/j1m/artifacts/{name}", str(destination / name),
]
```

Three concrete defects follow from that shape:

1. **TOCTOU on the destination.** `_salvage` validated `destination` (private
   directory, owner-only, stable ancestors) and then handed
   `str(destination / name)` to a separate process. Between the check and the
   `open()` inside `scp`, the directory could be replaced by a symlink and the
   transfer would follow it. The validation and the write were performed by
   different processes against a *pathname*, which is exactly the unbound
   reference the commit message names.
2. **Unbounded path input.** `name` came from `config["artifacts"][...]`, a
   JSON file. A configuration edit could name `../../..`-style paths or any
   remote file; nothing in source constrained the fetchable set.
3. **Unbounded content.** Whatever bytes arrived were written straight into the
   run's artifact directory with no size ceiling, no parse, no schema check and
   no binding to the run that requested them. The size-aware timeout branch in
   the removed loop (`name.endswith("Q4_K_M.gguf")` → minutes) shows the intent
   was to move multi-gigabyte artifacts through the same path.

A plain revert reinstates all three. The refusal was the correct interim
position.

## 3. What this slice restores, and how each concern is closed

### 3.1 SCP never receives the validated destination

The transfer target is a fresh private staging directory created per salvage
call (`tempfile.TemporaryDirectory`, mode `0700`, unpredictable name). Its
contents are treated as untrusted remote output for their whole lifetime. The
only path that ever names the run's artifact directory is
`j1m_runner._private_atomic_write`, which:

- re-snapshots every ancestor from the trusted root,
- opens the parent through `O_DIRECTORY | O_NOFOLLOW` descriptors walked from
  that root rather than by pathname,
- writes a `0600` temporary file by descriptor, `fsync`s it,
- `os.replace`s it into place with `src_dir_fd`/`dst_dir_fd`,
- re-opens and byte-compares the published result.

So there is no window in which a pathname held by another process decides where
bytes land. Concern (1) is closed by construction, not by validating twice.

`_salvage` additionally re-proves the destination inode (`st_dev`, `st_ino`,
owner, mode, not a symlink) immediately after the ancestor-stability check and
before any process can exist, so a swap in that window refuses the whole
operation rather than reaching a transfer.

### 3.2 The fetchable set is source-fixed

`_SALVAGE_RECEIPT_ALLOWLIST` maps each fetchable basename to the exact schema
string that receipt must declare:

| Name | Required schema |
|---|---|
| `eval-receipt.json` | `local_bmo.j1m.real-tool-eval-receipt.v1` |
| `eval-artifact-receipt.json` | `local_bmo.j1m.remote-eval-artifact-receipt.v1` |
| `startup-preflight-receipt.json` | `local_bmo.j1m.startup-preflight-receipt.v1` |
| `toolchain-receipt.json` | `local_bmo.j1m.remote-toolchain-receipt.v1` |
| `cuda-device-receipt.json` | `local_bmo.j1m.cuda-device-receipt.v1` |
| `proving-receipt.json` | `local_bmo.j1m.proving-receipt.v1` |

The remote directory is the single source constant
`_SALVAGE_REMOTE_DIRECTORY = "/scratch/j1m/artifacts"` — the same directory the
orchestrator itself created and named in `_eval_remote_commands` at launch. The
remote operand is always that constant joined with one allowlist key. There is
no globbing, no remote directory listing, no caller-supplied path, and no
configuration value in the constructed path. A name a configuration file
requests but source does not list is recorded as
`salvage_name_not_allowlisted` and never turned into a remote path. Concern (2)
is closed.

Consequence worth stating plainly: **the deployable Q4 GGUF is no longer
salvageable.** `build` mode's `local_fetch_allowlist` names
`Qwen3.5-9B-Q4_K_M.gguf`, `checksums.sha256` and several build receipts; under
this transport those are all refused as not-allowlisted. This transport carries
bounded receipt JSON only. Moving a multi-gigabyte artifact needs a separate,
separately reviewed capability with its own integrity and budget story; it is
deliberately not smuggled in here.

### 3.3 Bounds

| Bound | Value | Constant |
|---|---|---|
| Per file | 4 MiB, refused not truncated | `_SALVAGE_MAX_FILE_BYTES` |
| Effective JSON bound | 2 MiB (strict decoder) | `j1m_runner._RECEIPT_MAX_BYTES` |
| Total across the call | 32 MiB | `_SALVAGE_MAX_TOTAL_BYTES` |
| Files fetched | 6 = allowlist size | `_SALVAGE_MAX_FILES` |
| Candidate names accepted | 64 | `_SALVAGE_MAX_REQUESTED_NAMES` |
| Wall clock for the whole call | 300 s | `_SALVAGE_WALL_CLOCK_SECONDS` |
| Per transfer | 60 s | `_SALVAGE_FILE_TIMEOUT_SECONDS` |
| Cleanup reserve kept back | 660 s | `_DELETION_RESERVE_SECONDS` |

`-l` on `scp` is a *bandwidth* limit, not a size limit, so the per-file ceiling
is enforced locally after the transfer: the staged file is opened
`O_NOFOLLOW`, proved to be an ordinary single-link file owned by this process,
and read to at most the cap. A file at or beyond the cap is refused outright —
never truncated and published as if it were whole.

The effective per-file ceiling for a receipt that must parse is the strict
decoder's 2 MiB, which is checked first so an oversize file reports
`salvage_oversize` rather than a misleading parse failure.

### 3.4 Transport options

`_salvage_transport_argv` builds an argument vector — never a shell string —
from `sf.scp_base` plus a salvage-specific prefix:

- `-4` — IPv4 only.
- `-o ForwardAgent=no`, `-o ForwardX11=no`, `-o ClearAllForwardings=yes`,
  `-o ExitOnForwardFailure=yes`, `-o PermitLocalCommand=no`.
- From `scp_base`: `-F /dev/null`, `-o BatchMode=yes`,
  `-o StrictHostKeyChecking=yes`, `-o UserKnownHostsFile=<pinned>`,
  `-o ConnectTimeout=15`, `-o IdentitiesOnly=yes`, `ControlMaster/Path/Persist`
  disabled, `-i <ephemeral key>`, `-P <port>`.
- No `-r`: recursion is never enabled.
- Endpoint from `sf._endpoint(info["instance_info"])` — the same activation
  record the run used. Salvage never re-resolves a host or accepts a new
  address.

The ephemeral private key is passed as a file handle, never as a value, per
SI-002. `_salvage` proves the key path is a canonical private handle
(`0600`, owner, private parent) before spawning anything; an unusable handle is
the typed refusal `salvage_identity_handle_unusable`, not an opaque error.

### 3.5 Host-key pinning — exactly what is pinned, and when

Shadeform's instance-info response **does not** carry a host-key fingerprint.
`sf.acquire_pinned_host_key` therefore already runs before the first remote
command and pins in one of two ways:

- **Provider fingerprint (authoritative).** If `instance_info` ever exposes
  `ssh_host_key_fingerprint` / `host_key_fingerprint` / `ssh_fingerprint`, that
  value must identify exactly one scanned key, and only that key is written to
  `known_hosts`.
- **Two-scan trust-on-first-use (current reality).** Otherwise two independent
  bounded `ssh-keyscan` passes must return an identical key set; that set is
  written to `known_hosts` at mode `0600`. The function records this as
  `two-stable-bounded-scans-residual-tofu` — a deliberately recorded residual
  risk, not a claim of proof.

Salvage does not re-pin and does not re-scan. It reuses the file pinned at that
first connection, and before any transfer it proves that file is still an
owner-private, non-symlink, single-link regular file with a non-empty bounded
size and the same number of pinned keys recorded at acquisition. It records
`known_hosts_sha256` in the salvage receipt. Every transfer then runs with
`StrictHostKeyChecking=yes` against that pinned file, so a host swapped between
the run and teardown fails the transfer with a typed
`salvage_host_key_mismatch` instead of silently re-pinning and fetching from a
stranger.

Residual risk, stated rather than hidden: the initial pin is TOFU whenever the
provider exposes no fingerprint. Salvage inherits that and cannot improve on
it; what it guarantees is that the key trusted at teardown is the same key
trusted at the start of the run.

### 3.6 Validation before publication

Each staged file, in order:

1. Bounded descriptor read, size cap, no symlink, owner-only, single link.
2. `j1m_runner._bounded_json_loads` — strict JSON, **duplicate keys rejected**,
   non-finite numbers rejected.
3. Must be a JSON object.
4. `schema` must equal the allowlist's expected schema for that exact name.
5. Required top-level keys per receipt type (`_SALVAGE_REQUIRED_KEYS`).
6. `j1m_runner.validate_persisted_receipt` — no credential-shaped content may
   be persisted, even from a receipt.
7. Run-identity binding: a top-level `run_id` or `instance_id` must match this
   run; `eval-artifact-receipt.json` (`name`/`size_bytes`/`sha256`),
   `eval-receipt.json` (`artifact.*`) and a *verified*
   `startup-preflight-receipt.json` (`size_bytes`/`sha256`) must match the
   run's approved artifact identity.
8. Only then `_private_atomic_write` into `artifacts/qwen35-9b/<run-id>/`
   (whatever `--artifact-destination` names), byte-exact, mode `0600`.

The existing `_verify_eval_receipt` / `_verify_eval_artifact_receipt` /
`_verify_startup_preflight_receipt` checks still run afterwards on the
published file and remain the authority for run success. Step 4–7 are the
*transport's* gate on untrusted bytes, deliberately narrower than and prior to
those verifiers.

Concern (3) is closed.

## 4. Fail-closed behaviour and teardown ordering

Per-file failures never raise. Each allowlisted name is attempted
independently and records a typed reason:

| Code | Meaning |
|---|---|
| `salvage_name_not_allowlisted` | requested name is not in source |
| `salvage_duplicate_name` | already attempted in this call |
| `salvage_file_count_cap` | allowlist-size fetch budget spent |
| `salvage_total_size_cap` | 32 MiB budget spent or would be exceeded |
| `salvage_deadline_reserve` | wall clock or provider deadline reserve spent |
| `salvage_identity_handle_unusable` | key path is not a canonical private handle |
| `salvage_timeout` | transfer exceeded its bound |
| `salvage_transport_os` | local spawn failure |
| `salvage_host_key_mismatch` | strict host-key check failed |
| `salvage_transport_failed` | non-zero exit, including a missing remote file |
| `salvage_oversize` | beyond the per-file or decoder bound |
| `salvage_invalid_json` | malformed, duplicate-keyed, or non-finite JSON |
| `salvage_not_an_object` | top level is not an object |
| `salvage_schema_mismatch` | wrong schema for that name |
| `salvage_required_key_missing` | missing a required top-level key |
| `salvage_identity_mismatch` | describes another run, instance, or artifact |
| `salvage_receipt_content_refused` | credential-shaped receipt content |
| `salvage_not_private_regular_file`, `salvage_changed_during_read`, `salvage_read_failed`, `salvage_missing`, `salvage_descriptor_unavailable` | staged-file integrity failures |

Only three conditions refuse the *whole* call, and all three are proved before
any process exists: an unproved destination, a request exceeding the bounded
candidate count, and an unusable host-key pin. Those raise `ValueError`, which
the caller's `finally` already catches into
`lifecycle["salvage"] = [{"status": "salvage_failed", ...}]` before continuing
to `teardown_exact`. **Salvage failure can never leave an instance running.**

A missing allowlisted receipt is recorded as a failure, is *not* fatal to
teardown, and *is* fatal to run success: the existing `mode == "eval"` block
sets `receipt_error` and `lifecycle["status"] = "failed"` when the eval,
artifact, or preflight receipts did not land.

## 5. Local evidence: `salvage-receipt.json`

Written into the same destination directory through the same descriptor-safe
writer, mode `0600`. It is deliberately **not** in the fetch allowlist — it
describes the transfer and must never be supplied by the remote host.

```json
{
  "schema": "local_bmo.j1m.salvage-receipt.v1",
  "status": "salvaged | salvage_failed",
  "transport": "scp-argv-bounded-source-allowlist-v1",
  "remote_directory": "/scratch/j1m/artifacts",
  "host_key_proof": "provider-fingerprint | two-stable-bounded-scans-residual-tofu",
  "known_hosts_sha256": "<64 hex>",
  "caps": {"per_file_bytes": 4194304, "total_bytes": 33554432, "max_files": 6,
           "wall_clock_seconds": 300.0, "per_file_timeout_seconds": 60.0},
  "allowlist": ["cuda-device-receipt.json", "..."],
  "requested": 5, "completed": 4, "failed": 1,
  "total_bytes": 18342, "duration_ms": 2411,
  "files": [{"name": "...", "status": "completed", "size_bytes": 0,
             "sha256": "<64 hex>", "schema": "...", "exit_code": 0}]
}
```

`status` is `salvaged` only when every requested name landed. A failure to
write this receipt is swallowed: evidence matters, but it is never allowed to
strand a paid instance.

## 6. Logging policy

Metadata only, per `AGENTS.md` and SI-002: filenames, byte counts, SHA-256
digests, exit codes, typed error codes, durations, and the `known_hosts`
digest. Never key material, never tokens, never receipt contents, never a
resolved credential path. Transport stderr is already bounded and
credential-screened by `_remote`; salvage reads it only to classify a
host-key failure and stores the classification, not the text.

## 7. `_EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE`

Now `True`, and it is source-level truth rather than a dormant switch:
`_salvage` consults it and refuses if it is ever set back to `False`. Its
pinning test asserts the bounded behaviour — the allowlist contents, the fixed
remote directory, the fetch-count identity, and that no traversal, glob, GGUF
or local-evidence name is fetchable — instead of asserting the old blanket
refusal.

## 8. Known blocker outside this slice

`validate_persisted_argv` (introduced by `991b70e`) requires the `-i` operand
of any persisted argv to be a **canonical private handle**: basename matching
`_HANDLE_BASENAME` (`identity`, `ssh[_-]?key`, `token`, …), mode `0600`, a
private direct parent, and no world-writable ancestor.

`sf.create_keypair` names the ephemeral key `id_ed25519` inside a
`tempfile.TemporaryDirectory()`. That basename matches nothing in
`_HANDLE_BASENAME`, and the system temp location fails the ancestor rule on
both platforms in practice (`/var` is a symlink on macOS; `/tmp` is mode `1777`
and is only permitted as a *direct* parent on Linux).

Reproduction, against unmodified `main`:

```python
identity, _pub = sf.create_keypair(Path(tempfile.mkdtemp()) / "ssh")
j1m_runner.validate_persisted_argv(sf.ssh_base(info, identity, known_hosts) + ["df"])
# ValueError: credential-like command argument rejected before persistence
```

`_remote` calls `validate_persisted_argv` on every command, so **every**
ssh/scp invocation in `execute()` — the workspace stages, the three J1M
uploads, every eval stage, and this salvage — is refused before it spawns. The
run fails early and cheaply rather than silently, and no instance is stranded,
but it does mean a real eval cannot currently complete.

This slice does not fix it, because the fix is a decision about where
credential material lives and what a handle basename may be, which is Sol's to
make. Salvage reports it as the typed, value-free refusal
`salvage_identity_handle_unusable` rather than an opaque local failure. Two
candidate remediations, both of which conform the orchestrator to the existing
policy rather than weakening it:

1. Name the ephemeral key with a handle-shaped basename (for example
   `ssh-key`) **and** place the per-run private directory somewhere whose
   ancestors satisfy the rule — the `.secrets/`-style repository-local,
   mode-`0700`, gitignored layout established by `b306665`.
2. Extend `_HANDLE_BASENAME` to recognise OpenSSH's canonical private-key
   basenames (`id_ed25519`, `id_rsa`, …). This widens a credential-detection
   regex and is the weaker option; it also does not fix the ancestor rule.

Option 1 is recommended. Either way it is a separate, separately reviewed
slice.

## 9. Tests

`tests/performance/test_j1m_salvage_transport.py` (class `lifecycle` in
`scripts/test/run_qa.py`). No network, no provider, no real transfer: `_remote`
is always mocked and a real transfer would be a failure. It pins argv
exactness and credential-policy conformance, allowlist enforcement against
traversal/glob/GGUF/local-evidence names, every cap, every validation refusal,
host-key pin and mismatch handling, timeout classification, staging cleanup,
byte-exact `0600` publication, the `salvage-receipt.json` shape and its
metadata-only content, and that salvage failure still reaches teardown.

`tests/performance/test_remote_canary_secret_hardening.py` and
`tests/performance/test_j1m_lifecycle.py` had four pins on the old refusal;
all four now assert the bounded transport.
