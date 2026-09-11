# J1M receipt-salvage transport

Slices `J1M-SALVAGE-TRANSPORT-001` and `J1M-KEY-HANDLE-001`. Owner S4 Luna.
Implements the bounded external salvage path in
`scripts/j1m_orchestrator.py:_salvage`, called once from the teardown `finally`
block of `execute()`; §10 covers the ephemeral key lifecycle and §11 the
end-to-end offline dry run that gates a live launch.

Revision note: the independent review of `J1M-SALVAGE-TRANSPORT-001` returned
ACCEPT_WITH_REQUIRED_FIXES. Its two MAJOR findings (build mode could salvage
nothing and still report success; run-identity binding was opportunistic) and
three MINORs are resolved here, in the slice that owns the same file. §3.2,
§3.3, §3.5, §3.6, §4 and §5 are the corrected text; the earlier claim that this
transport carries "receipts bound to this run's id, instance, and approved
artifact identity" is now true of every fetchable name rather than three of
them.

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

| Name | Required schema | Mode |
|---|---|---|
| `eval-receipt.json` | `local_bmo.j1m.real-tool-eval-receipt.v1` | eval |
| `eval-artifact-receipt.json` | `local_bmo.j1m.remote-eval-artifact-receipt.v1` | eval |
| `startup-preflight-receipt.json` | `local_bmo.j1m.startup-preflight-receipt.v1` | eval |
| `toolchain-receipt.json` | `local_bmo.j1m.remote-toolchain-receipt.v1` | eval |
| `cuda-device-receipt.json` | `local_bmo.j1m.cuda-device-receipt.v1` | eval |
| `proving-receipt.json` | `local_bmo.j1m.proving-receipt.v1` | prove |
| `manifest.json` | `local_bmo.j1m.artifact-manifest.v1` | build |
| `tensor-metadata.json` | `local_bmo.j1m.tensor-metadata.v1` | build |
| `source-model-receipt.json` | `local_bmo.j1m.source-model-receipt.v1` | build |
| `conversion-receipt.json` | `local_bmo.j1m.conversion-receipt.v1` | build |
| `model-receipt.json` | `local_bmo.j1m.model-receipt.v1` | build |
| `toolchain.json` | `local_bmo.j1m.toolchain.v1` | build |
| `scan-receipt.json` | `local_bmo.j1m.scan-receipt.v1` | build |
| `post-cleanup-receipt.json` | `local_bmo.j1m.post-cleanup-receipt.v1` | build |

The remote directory is the single source constant
`_SALVAGE_REMOTE_DIRECTORY = "/scratch/j1m/artifacts"` — the same directory the
orchestrator itself created and named in `_eval_remote_commands` at launch. The
remote operand is always that constant joined with one allowlist key. There is
no globbing, no remote directory listing, no caller-supplied path, and no
configuration value in the constructed path. A name a configuration file
requests but source does not list is recorded as
`salvage_name_not_allowlisted` and never turned into a remote path. Concern (2)
is closed.

The earlier revision of this slice listed only the six eval/prove names, which
meant `build` mode salvaged **nothing**: all eleven entries of
`local_fetch_allowlist` were refused `salvage_name_not_allowlisted`, and the
`receipt_error` gate covered only `prove` and `eval`, so a multi-hour paid
conversion returned an empty artifact directory and did *not* report failure.
That was the review's first MAJOR finding and it is corrected above and in §4.

Three configured names remain deliberately uncarryable, each with its own typed
reason (`salvage_refused_non_receipt`) and a refusal class, so a build receipt
says plainly why rather than implying a configuration mistake:

| Name | Class | Why |
|---|---|---|
| `Qwen3.5-9B-*.gguf` | `weights` | Weights are not evidence. A multi-gigabyte artifact is never pulled to the operator laptop; that needs a separate capability with its own integrity and budget story and is deliberately not smuggled in here. This is the `2d7db4f` scope decision, retained. |
| `checksums.sha256` | `not_schema_bound` | Not JSON; nothing to schema-check or bind to a run. |
| `command-receipt.json` | `not_schema_bound` | A JSON *array* of command records, so it can carry neither a top-level `schema` string nor the required run-identity binding. |

### 3.3 Bounds

Every number below is now the one that actually decides. The review's NIT was
correct: a 4 MiB per-file cap sitting behind a 2 MiB decoder bound, and a 32 MiB
total that six files could never reach, were advertised in the receipt's `caps`
block as if they were operative. They are derived from the binding bound now.

| Bound | Value | Constant |
|---|---|---|
| Per file | 2 MiB, refused not truncated | `_SALVAGE_MAX_FILE_BYTES` (= `j1m_runner._RECEIPT_MAX_BYTES`) |
| Files fetched | 14 = allowlist size | `_SALVAGE_MAX_FILES` |
| Total across the call | 28 MiB = files x per-file | `_SALVAGE_MAX_TOTAL_BYTES` |
| Free space required before any transfer | 56 MiB = total x 2 | `_SALVAGE_MIN_FREE_BYTES` |
| Candidate names accepted | 64 | `_SALVAGE_MAX_REQUESTED_NAMES` |
| Wall clock for the whole call | 300 s | `_SALVAGE_WALL_CLOCK_SECONDS` |
| Per transfer | 60 s | `_SALVAGE_FILE_TIMEOUT_SECONDS` |
| Cleanup reserve kept back | 660 s | `_DELETION_RESERVE_SECONDS` |

The free-space bound closes the review's disk MINOR. `scp` streams into the
private staging directory before any size check can run, so a hostile or broken
host had the full 60 s per-file window to fill the disk. The whole call is now
refused up front unless the worst case fits twice over -- once staged, once
published.

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

- `-4` — IPv4 only, and the endpoint is *proved* to be IPv4 rather than
  assumed: `sf._endpoint` accepts any `ipaddress.ip_address`, and an IPv6
  address would need bracketing in the operand and contradicts this flag, so it
  is refused as `salvage_endpoint_not_ipv4` instead of producing a malformed
  operand and an opaque transport error.
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
size **and the exact SHA-256 `acquire_pinned_host_key` recorded when it wrote
those bytes**. The earlier revision compared only the line count, so a
`known_hosts` rewritten with a different key of the same shape passed; that was
the review's host-key MINOR. The digest is the baseline now, the line count
survives only as a fallback for an acquisition record that predates it, and the
salvage receipt states which of the two was used (`host_key_reproof`). Every transfer then runs with
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
7. `j1m_runner.validate_persisted_output` over the exact bytes -- the same
   gate the descriptor-safe publisher applies -- so a receipt that cannot be
   published is refused as `salvage_receipt_content_refused` rather than
   surfacing later as a generic failure that says nothing.
8. Run-identity binding, **required on every allowlisted name**: `run_id` and
   `instance_id` must both be present and must both equal what this run
   uploaded to the host in `run-identity.json` before the first
   receipt-producing command. A missing field is `salvage_identity_missing`; a
   wrong one is `salvage_identity_mismatch`. The earlier revision compared
   these only `if field in payload`, so `toolchain-receipt.json`,
   `cuda-device-receipt.json`, `proving-receipt.json` and a non-verified
   preflight were published on schema alone -- meaning a receipt left in
   `/scratch/j1m/artifacts` by an *earlier* run could be published as this
   run's evidence, and for `prove` mode `proving-receipt.json` is exactly what
   run success gates on. That was the review's second MAJOR finding.
9. Artifact identity, required where the receipt declares it:
   `eval-artifact-receipt.json` (`name`/`size_bytes`/`sha256`) and
   `eval-receipt.json` (`artifact.*`) must carry all three fields and match the
   run's approved artifact; a *verified* `startup-preflight-receipt.json` must
   match on `size_bytes`/`sha256`. Omitting a declared field is a refusal, not
   a way to skip the comparison.
10. Only then `_private_atomic_write` into `artifacts/qwen35-9b/<run-id>/`
   (whatever `--artifact-destination` names), byte-exact, mode `0600`.

The existing `_verify_eval_receipt` / `_verify_eval_artifact_receipt` /
`_verify_startup_preflight_receipt` checks still run afterwards on the
published file and remain the authority for run success. Steps 4-9 are the
*transport's* gate on untrusted bytes, deliberately narrower than and prior to
those verifiers.

**How the host learns its binding.** `execute()` writes
`run-identity.json` (`{"schema": "local_bmo.j1m.run-identity.v1", "run_id",
"instance_id"}`) and uploads it to the single source-fixed path
`j1m_runner.RUN_IDENTITY_PATH` = `/scratch/j1m/run-identity.json`, immediately
after the config upload and before any command that writes a receipt; a failed
upload fails the run. Every host-side writer -- `j1m_runner`'s nine receipt
writers plus `remote_model_eval.py`, `remote_eval_prepare.py`,
`remote_toolchain_probe.py` and `cuda_device_probe.py` -- reads exactly that
path and stamps the two fields into the receipt it publishes. No caller,
configuration value, or remote response can redirect it. An absent or malformed
file yields `unbound`, which the transport then refuses: failing closed on the
host would strand a paid run mid-conversion, so the refusal belongs at
publication.

The remote command plan is unchanged by this: the identity travels in an
uploaded file, not in argv, so `_eval_remote_commands` and `_remote_job_command`
are byte-identical to the reviewed versions. What changed is the receipts'
*content*.

Concern (3) is closed.

## 4. Fail-closed behaviour and teardown ordering

Per-file failures never raise. Each allowlisted name is attempted
independently and records a typed reason:

| Code | Meaning |
|---|---|
| `salvage_name_not_allowlisted` | requested name is not in source |
| `salvage_refused_non_receipt` | configured, but weights or not schema-bound (§3.2) |
| `salvage_endpoint_not_ipv4` | the activation record's address is not IPv4 |
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
| `salvage_identity_missing` | required `run_id`/`instance_id`/artifact field absent |
| `salvage_identity_mismatch` | describes another run, instance, or artifact |
| `salvage_transport_refused` | exit 255 with unreadable stderr: a transport-layer refusal that may be a host-key failure |
| `salvage_receipt_content_refused` | credential-shaped receipt content |
| `salvage_not_private_regular_file`, `salvage_changed_during_read`, `salvage_read_failed`, `salvage_missing`, `salvage_descriptor_unavailable` | staged-file integrity failures |

Only four conditions refuse the *whole* call, and all four are proved before
any process exists: an unproved destination, a request exceeding the bounded
candidate count, an unusable host-key pin, and insufficient free space for the
worst-case staged-plus-published total. Those raise `ValueError`, which
the caller's `finally` already catches into
`lifecycle["salvage"] = [{"status": "salvage_failed", ...}]` before continuing
to `teardown_exact`. **Salvage failure can never leave an instance running.**

A missing allowlisted receipt is recorded as a failure, is *not* fatal to
teardown, and *is* fatal to run success. All three modes now share that rule:

| Mode | Required to land | On failure |
|---|---|---|
| `eval` | `eval-receipt.json`, `eval-artifact-receipt.json`, and `startup-preflight-receipt.json` when remote eval was attempted | `receipt_error` + `status = failed` |
| `prove` | `proving-receipt.json` | `receipt_error` + `status = failed` |
| `build` | every `local_fetch_allowlist` name that is source-allowlisted (the eight receipts); the three non-receipt names are excluded and recorded separately in `build_receipts.refused_non_receipt` | `receipt_error` + `status = failed` |

`prove` previously set `receipt_error` without setting `status`, so a run whose
proving receipt never arrived still reported `completed`; `build` had neither.
Both are corrected. A run that bought a machine and came back with no evidence
is a failed run.

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
  "host_key_reproof": "acquisition-digest | key-count-fallback",
  "known_hosts_sha256": "<64 hex>",
  "caps": {"per_file_bytes": 2097152, "total_bytes": 29360128,
           "min_free_bytes": 58720256, "max_files": 14,
           "wall_clock_seconds": 300.0, "per_file_timeout_seconds": 60.0},
  "allowlist": ["cuda-device-receipt.json", "..."],
  "requested": 5, "completed": 4, "failed": 1,
  "total_bytes": 18342, "duration_ms": 2411,
  "files": [{"name": "...", "status": "completed", "size_bytes": 0,
             "sha256": "<64 hex>", "schema": "...", "exit_code": 0},
            {"name": "Qwen3.5-9B-Q4_K_M.gguf", "status": "salvage_failed",
             "error_code": "salvage_refused_non_receipt", "refusal_class": "weights"}]
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

## 8. The argv blocker, resolved (`J1M-KEY-HANDLE-001`)

`validate_persisted_argv` (`991b70e`) requires the `-i` operand of any persisted
argv to be a **canonical private handle**: a basename matching
`_HANDLE_BASENAME` (`identity`, `ssh[_-]?key`, `token`, ...), an ordinary
single-link file owned by this process at mode `0600`, an owner-private direct
parent, and no group/world-writable ancestor up to `/`.

`sf.create_keypair` named the ephemeral key `id_ed25519` inside a
`tempfile.TemporaryDirectory()`. That basename matches nothing in
`_HANDLE_BASENAME`, and the system temp location fails the ancestor rule on both
platforms in practice (`/var` is a symlink on macOS; `/tmp` is mode `1777` and
is only permitted as a *direct* parent on Linux). `_remote` validates every
command, so **every** ssh/scp invocation in `execute()` was refused before it
spawned -- after an instance had been created and billed.

### 8.1 Where the key lives now

`.secrets/j1m/<run-id>-<ownership-nonce>/ssh-key`

- **Root** (`sf.EPHEMERAL_KEY_ROOT`): the protected secrets area established by
  `b306665` for the mutation env projection. It is Git-ignored, operator-owned,
  and already required to be owner-private. `experiments/runtime/` was
  considered and rejected: the mocked-execute isolation treats every path under
  `RUNTIME_ROOT` as operator evidence and fails any test that touches it, so a
  key placed there would be untestable.
- **Why the validator trusts it**: `_private_handle_snapshot` has no trusted-root
  concept of its own. It requires the *direct parent* to be owner-private
  (`uid == getuid()`, no group/world bits) and every ancestor above it to be
  owned by root or this user with no group/world write bit. The per-run
  directory and `.secrets/j1m` are created `0700`; the checkout and its
  ancestors satisfy the weaker ancestor rule on a normal developer machine.
  Being under `PRIVATE_OUTPUT_ROOT` as well is not required by this validator
  but keeps the key inside the same trust boundary as every other private
  output.
- **Basename**: `ssh-key`, matched by `_HANDLE_BASENAME`'s `ssh[_-]?key`
  alternative. `ssh-keygen` writes the public half as `ssh-key.pub`.
- **Mode**: `0600`, inside a `0700` directory, generated by
  `ssh-keygen -t ed25519 -N '' -q -f <path>` as an argv array with a sanitised
  child environment.

### 8.2 Never reused, always destroyed

`ephemeral_key_directory(run_token)` names the directory from the run id plus
this run's fresh ownership nonce, and `create_keypair` **refuses a directory
that already exists**, so the filesystem enforces "one key, one run". An
existing ancestor that is not owner-private is refused rather than `chmod`-ed:
a permission decision on the operator's tree is theirs.

`destroy_ephemeral_key_directory` overwrites each regular file with random
bytes, `fsync`s, truncates, unlinks, and removes the directory. It is called
from `execute()`'s teardown `finally` on the success path and on every failure
path, and again directly if key *generation* itself fails outside the protected
region. It never raises and never prints; the outcome is recorded in the run
receipt as `ephemeral_key_cleanup` -- status, file count, and the run label, no
paths into key material and no key bytes.

### 8.3 Refuse before spending, not after

`sf.assert_persisted_argv_handle` proves the new key against the exact argv
policy immediately after generation and before the first billable provider
call. A key the validator would refuse is now a local configuration fault that
costs nothing, not a discovery made after an instance is running.

### 8.4 Two more operands in the same class

The same reading found two further refusals that a paid run would have hit:

1. **`--token-file /scratch/j1m/engine-token`** in the eval argv named a path on
   a host this process has not contacted, and `token-file` is a private-handle
   option: no *local* canonical-handle proof can ever exist for it, so the whole
   eval command was refused. Renaming the option to dodge its credential
   detector would be obfuscation, and loosening the detector would weaken a real
   control. The orchestrator therefore stops naming a credential path it cannot
   prove: the operand is gone and `remote_model_eval.py` creates its bearer
   token in an owner-private temporary directory of its own.
2. **`"token_logging": false`** in the eval receipt. The flag asserts the engine
   did not log *model* tokens, but a field name delimited as `_token_` is
   credential-shaped, and `validate_persisted_output` -- which the
   descriptor-safe publisher applies to every byte it writes -- rejected the
   entire serialized receipt for carrying it. A paid eval produced an eval
   receipt that could never be published. The field is now `tokens_logged`;
   again, the name was the defect, not the detector.

Two defects of a different kind were found by *running* the dry run (§11):
`_persist_lifecycle` referenced an undefined `MAX_RECEIPT_BYTES` and so raised
`NameError` on every call since `8e3f599` -- silently, because both call sites
swallow evidence failures so cleanup can never be stranded -- and the default
`--artifact-destination` (`artifacts/qwen35-9b`, mode `0755`) fails the
descriptor walk the salvage publisher performs, which made salvage refuse the
whole call from inside the teardown `finally`. `execute()` now proves the
destination before anything is billable, creating missing components `0700` and
refusing an existing non-private one with the exact `chmod` that fixes it. Git
does not record directory modes, so on a fresh checkout this is an operator
step, and the dry run names it.

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

`tests/performance/test_j1m_key_handle.py` pins the key lifecycle of §8: the
root, the basename against `_HANDLE_BASENAME`, mode `0600` inside `0700`, that a
complete `ssh_base`/`scp_base` argv built from it is *accepted*, that the old
tempdir layout is still *refused*, single use, refusal of an unsafe ancestor
without widening it, overwrite-and-remove destruction, idempotent destruction,
and that the cleanup receipt survives the persisted-output policy. It also pins
the artifact-destination precondition. `ssh-keygen` is always shimmed; no key is
generated and no key material is logged.

`tests/performance/test_j1m_dry_run.py` (class `lifecycle`) is the harness of
§11 and is mostly negative: the old key location, a leaked secret, a receipt
with no run binding, a receipt claiming another run, a withheld required
receipt, an injected teardown failure, and an unexpected child process must each
turn the gate red.

## 11. The pre-launch gate: `scripts/j1m_dry_run.py`

Three refusals in a row -- the salvage refusal, the `-i` operand, the
`--token-file` operand -- each reached a state where a paid run would have
created an instance and then been unable to use it, and each was found by
reading the code. Reading found them late and one at a time. This harness is the
replacement.

```
python3 scripts/j1m_dry_run.py --mode eval      # gate one mode
python3 scripts/j1m_dry_run.py                  # gate eval, prove and build
```

Exit status is `0` only when every check passes. **The live-run lane runs this
as its final step before `--execute`.** A non-zero exit means do not launch.

### 11.1 What is real and what is fake

Real: `execute()` itself, command construction, `validate_persisted_argv`,
`acquire_pinned_host_key` and the `known_hosts` it writes, `create_keypair` in
the real key location, the salvage transport, every receipt verifier, the caps,
the cost-ledger writes, the lifecycle receipt writer, and teardown ordering.

Fake: provider responses (catalogue, key create, instance create, activation
record with host/port/user, delete), the keypair's bytes, and every child
process -- a shim answers `ssh-keygen`, `ssh-keyscan`, `ssh` and `scp` from
source data, records the argv, and *raises* on any other program so a newly
introduced subprocess shows up as a failure rather than silently succeeding.

Isolation: ledgers, incidents, owner records and the artifact destination live
in a temporary namespace under `.secrets/j1m-dry-run/`, removed on exit.
`PRIVATE_OUTPUT_ROOT` is deliberately *not* repointed -- the publisher, the
config loader and the receipt readers all walk descriptors from it, and moving
it would exercise a path the live run never takes. `sf.request`,
`urllib.request.urlopen` and `socket.create_connection` all raise: "no network"
is enforced, not asserted.

Argv is recorded at the *construction* boundary, wrapping `_remote`, not at the
process boundary. A command the validator refuses never reaches a subprocess, so
a harness watching only the process boundary would report zero refusals for a
run that could not execute at all -- precisely the failure this gate exists to
prevent. A run that recorded no commands fails the check rather than passing
vacuously.

### 11.2 Checks

| Check | What it proves |
|---|---|
| `argv_validator_accepts_every_command` | every argv in the run passes `validate_persisted_argv` |
| `remote_operands_allowlisted` | every salvage fetch names the fixed remote directory joined with a source-allowlisted basename |
| `no_secret_value_in_any_argv` | three sentinel credentials planted in the env reach no command line |
| `salvage_allowlist_matches_host_writers` | every configured fetch name is source-allowlisted or an explicit non-receipt refusal |
| `receipts_carry_required_identity` | every published receipt carries `run_id`/`instance_id` matching this run |
| `teardown_reached_on_success` | `teardown_exact` is called with the exact instance id |
| `teardown_reached_on_injected_failure` | an injected teardown failure surfaces and still destroys the key |
| `ephemeral_key_removed_on_every_path` | no per-run key directory and no residue survives any run |
| `caps_and_reserves_respected` | the deletion reserve and every salvage cap are the operative numbers |
| `ledger_append_recorded_the_fake_cost` | exactly one pending cost row per run, bound to the exact instance id |
| `build_salvage_returns_receipts_and_refuses_weights` | build salvages its receipts and refuses the GGUF |
| `missing_required_receipt_fails_the_run` | a run whose required receipt never arrived reports `failed` |
| `injected_refusal_is_caught` | a deliberately unusable `-i` operand is reported `REFUSED` by the harness itself |
| `operator_artifact_destination_is_salvage_ready` | the live command's real destination would accept a publication |

### 11.3 `dry-run-receipt.json`

Written to `experiments/runtime/dry-run-receipt.json` by default: overall
PASS/FAIL, one row per check with its detail, every recorded argv in redacted
form (the `-i` operand becomes `<ephemeral-private-handle>`, sentinels become
`<redacted-secret>`), a per-run summary with status, `receipt_error`, salvage
reason codes, key-cleanup outcome and published receipts, and
`network: none`, `provider_calls: none`, `spend_usd: 0.0`.

The receipt is written with plain `json.dumps` plus a sentinel scan rather than
through `validate_persisted_receipt`: its whole purpose is to quote argv that a
receipt validator would reject, in redacted form. The scan is the control that
matters, and it raises rather than publishing if a planted secret ever appears.

### 11.4 What it does not prove

That the remote host behaves, that the model converts, or that the provider
honours its contract. It answers one question -- *would any command in this run
be refused locally?* -- and a PASS means the run gets as far as the network.
