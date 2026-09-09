# Shadeform read-only controls

`readonly_preflight.py` implements this lane's audited catalogue controls. It
reads the ignored `.env` only to validate uppercase controls and
never prints credential values. It accepts a local redacted catalogue snapshot or
an explicitly supplied HTTPS GET-only catalogue endpoint, then writes a plan and
optionally an append-only usage ledger.

Shadeform's `hourly_price` API field is cents per hour; the plan preserves the
source cents and reports the converted USD rate. The requested runtime is required
and the plan computes worst-case cost against the remaining project budget after
ledger events. GPU family prefixes, count, minimum VRAM, cloud allow/exclude,
region, interruptible preference, and hourly cap are all applied and rejected
cheaper options are retained in the report.

For the official API, use `https://api.shadeform.ai/v1/instances/types` with
`available=true&sort=price`; the adapter sends the key only in the `X-API-KEY`
header, as required by Shadeform's API documentation. The endpoint is read-only.

The adapter has no provisioning, termination, delete, or account-wide mutation
code. Every plan is bound to an owner and run ID and carries these mandatory
controls: hourly and total caps, no-orphan backstop, salvage-before-teardown, and
Sol review before J1M or any spend. If an instance is eventually approved, the
operator must keep the ledger, set a bounded auto-terminate backstop, salvage
receipts before teardown, and verify ownership before stopping it.

Example with a redacted catalogue snapshot (no network or spend):

```text
python scripts/shadeform/readonly_preflight.py \
  --env-file .env --catalogue catalogue.redacted.json \
  --owner S2 --run-id J1M-preflight-001 \
  --hours 1 --ledger out/evidence/shadeform-cost-ledger.jsonl \
  --output out/evidence/shadeform-readonly-plan.json \
  --record-ledger out/evidence/shadeform-cost-ledger.jsonl
```

The script deliberately does not invent rates. A catalogue profile must supply
`hourly_usd`; missing/invalid rates are excluded from candidates.

## Protected mutation environment

The read-only preflight's project-root `.env` input is not the credential-file
layout for provider mutations. Mutation and recovery commands default to
`.secrets/shadeform.env`. The `.secrets/` directory is ignored by Git and must
already be a non-symlink directory whose owner UID equals the process real UID
reported by `os.getuid()` (UID 0 only when the process real UID is root), with
no group or world permission bits. Mode `0700` is the recommended writable
setup for projection; mode `0500` is also safe for read-only loading of an
existing file. The projected file is created once with exact mode `0600`. The
migration helper deliberately does not create or chmod the directory,
overwrite an existing destination, or relax these checks.

From the repository root, prepare the directory and perform the offline,
one-way projection explicitly:

```text
mkdir -m 700 .secrets
python3 scripts/shadeform/migrate_env.py \
  --source "/path/to/mixed-donor.env" \
  --destination .secrets/shadeform.env
```

`mkdir` must succeed by creating a new directory; do not follow it with a blind
`chmod` when `.secrets` already exists. The migration command independently
revalidates the complete path and refuses links or unsafe metadata. Do not use project-root `.env`
for a mutation command: a normal mode-`0755` repository root is intentionally
too broad to be the direct parent of credential material. Any group or world
permission bit causes refusal, including modes `0710` or `0755`. An explicit
`--env-file` override is permitted only when its direct parent satisfies the
same ownership, non-symlink, and zero-group/world-bits contract.

## Remote external-tools QA runner

`remote_external_tools.py` is the dedicated, plan-first Shadeform wrapper for
the hostile external-tools QA harness. It uploads only the explicit audited
provider/tool/test closure, installs no packages, and downloads Node.js v24.20.0
Linux x64 on the remote host from nodejs.org. The archive is checked against
the pinned SHA-256 `2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2`;
the local machine never downloads it.

The default command is read-only and prints a secret-free plan:

```text
python3 scripts/shadeform/remote_external_tools.py --phase-id qa-remote-tools --run-id remote-tools-001
```

An execution additionally requires `SOL_SHADEFORM_REVIEWED=1`,
`SOL_REMOTE_EXTERNAL_TOOLS_REVIEWED=1`, and explicit
`SHADEFORM_QA_APPROVED_GPU`, `SHADEFORM_QA_APPROVED_CLOUD`,
`SHADEFORM_QA_APPROVED_REGION`, and `SHADEFORM_QA_APPROVED_INSTANCE_TYPE`
values in the configured environment file. The GPU must be an A100 family
candidate. The runner reserves budget before key/instance mutation, verifies
the exact owned key/instance and pinned host key, starts the external watchdog,
arms a host shutdown backstop, salvages the bounded receipt, and tears down
only the exact owned resource. No provider mutation is attempted without both
review markers.

## Explicit cost-ledger genesis

Paid planning remains fail-closed until the authoritative
`experiments/runtime/cost-ledger.jsonl` starts with one reviewed v2 genesis
event. Launchers never create this event and never infer prior spend from the
human-readable Markdown ledger. The offline
`initialize_cost_ledger.py` command is the only source initializer; it has no
credential, catalogue, provider, network, or process path.

Before any future use, a reviewer must reconcile all prior settled spend,
prove that the current pending-owner count is exactly zero, review the fixed
program `local-bmo-shadeform`, the `USD` cap, and SHA-256 hashes of both the
existing display ledger and incident evidence. The canonical ledger parent
must already exist as an owner-private, non-symlink POSIX directory. The
command requires the exact confirmation phrase exported by
`COST_LEDGER_GENESIS_CONFIRMATION`; it creates one mode-0600 file with
no-follow, exclusive, handle-relative operations. An identical crash-retry
validates the existing sole genesis and reports `recovered_existing` without
writing it; any changed value is refused without overwriting evidence.

The launch-time candidate balance is advisory. Immediately before a possible
provider mutation, the pre-create reservation exclusively locks the private
parent and existing ledger, revalidates the exact genesis/program/currency/cap,
recomputes every settled and pending owner, and durably appends only if the
proposal remains within the reviewed cap. Generic cost-event append operations
cannot create a missing authority.

On POSIX this protects the immediate canonical parent and ledger identity.
A same-UID adversary able to rename a higher ancestor while the process runs is
not excluded by these primitives; remote execution remains gated until that
residual is independently accepted or replaced with a stronger filesystem
trust anchor.

This repository task deliberately does not execute the initializer against
operator evidence. A source merge is not permission to initialize the ledger
or enable remote execution; Sol must separately review the asserted baseline
values and invocation. `REMOTE_EXECUTION_ENABLED=False` remains independent
and binding.
