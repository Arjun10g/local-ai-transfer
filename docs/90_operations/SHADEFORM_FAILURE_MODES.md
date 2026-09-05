# Shadeform incident log and runbook

This file is read in full immediately before every provider action. It is a
runbook as well as an append-only incident index; no account-wide instance list
or delete operation is permitted.

## Required ownership

One phase owns one exact instance ID, one ephemeral SSH key, and one random
32-hex ownership nonce. The ownership record is written before polling or
teardown. All provider calls validate the phase and exact resource ID.

## Teardown order

1. On cancellation, timeout, transport failure, host shutdown, or launcher
   death, attempt progressive/best-effort artifact salvage and write a
   receipt. The approved public Qwen source is unauthenticated, so no HF token
   is uploaded to the ephemeral host.
2. Delete the exact recorded instance and wait for provider confirmation.
3. Only after confirmed instance deletion and durable cost/deletion
   bookkeeping, persist an exact-owner SSH-key deletion intent, remove the
   exact SSH key once, and query that exact key until an exact 404 or a
   full-identity `deleted` response is durably confirmed. Accepted-but-present,
   `deleting`, timeout, and malformed responses retain the key owner and intent
   for retry/manual recovery; a retry queries first and never repeats DELETE.
   A bookkeeping failure must not prevent instance deletion, but the key is
   always retained until instance deletion, bookkeeping, and key absence are
   all confirmed. Key revocation is never an independent fallback for an
   unconfirmed instance deletion.
4. The external watchdog is independent of the launcher and performs the same
   exact deletion before stopping a wedged launcher; it has no SSH/token
   custody path.

## Create-response ambiguity

Before the create POST, the append-only cost ledger receives an
`attempt-<nonce>` pending reservation containing the candidate, key name/public
key hash, and provider-backstop estimate. A failed reservation is a hard stop.
The authoritative reservation re-reads the strict genesis and every owner
under one exclusive parent/file lock and enforces the reviewed total cap before
the append; candidate selection before it is advisory and cannot authorize a
provider mutation.
After durable exact ownership, watchdog start, and instance pending-cost entry,
the attempt reservation is settled to zero. Definitive pre-create failures are
settled to zero only after key cleanup; an ambiguous transport or unusable 2xx
response leaves the attempt pending.

The official [create API](https://docs.shadeform.ai/api-reference/instances/instances-create)
returns only an instance ID and documents no idempotency key/header. The
official [exact instance info API](https://docs.shadeform.ai/api-reference/instances/instances-info)
and [SSH-key info API](https://docs.shadeform.ai/api-reference/sshkeys/sshkeys-info)
are therefore used after a successful response to verify the nonce-bound name,
tags, attached key ID, key name, and public key before SSH or model work. If a
create POST times out before returning an ID, there is no safe exact-ID
reconciliation endpoint: the lifecycle records an `ambiguous-create` pending
incident, preserves the key/nonce, refuses subsequent launches, and relies on
the provider auto-delete backstop. This residual risk is intentionally not
resolved by an account-wide list or delete operation.

## Host-key proof

Instance info currently exposes no host-key fingerprint. The first connection
therefore requires two bounded `ssh-keyscan` calls returning an identical key
set, records the resulting fingerprint and residual-TOFU proof, and uses
`StrictHostKeyChecking=yes` thereafter. A provider fingerprint, when exposed in
future, supersedes the two-scan proof; unstable or empty scans fail closed.

## Backstops

Provider auto-delete, host shutdown, and the external watchdog each exceed the
complete run deadline. The active proving-run projection is 0.25 h at $1.35/h
($0.3375); the longer provider backstop is reported separately as a safety
ceiling. If the remaining project budget cannot cover the selected policy,
refuse to launch. Never trade away a backstop to fit a nominal estimate.

## Incident entries

| date | phase | symptom | response | receipt |
| --- | --- | --- | --- | --- |
| 2026-09-04 | J1M | no provider mutation; dry-run only | lifecycle and loopback tests | pending Sol review |

## Operator cancellation

SIGINT/SIGTERM is an explicit operator signal. The launcher records a
cancelled result, salvages available artifacts, deletes its exact resource, and
then exits. A transport timeout is a result requiring teardown; it is never an
implicit retry that can create a second owner.
