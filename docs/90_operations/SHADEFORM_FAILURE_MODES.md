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
   death, attempt progressive/best-effort artifact salvage and write a receipt.
2. Delete the exact recorded instance and wait for provider confirmation.
3. Only after deletion, record cost/deletion bookkeeping and remove the exact
   SSH key. A bookkeeping failure must not prevent deletion.
4. The external watchdog is independent of the launcher and performs the same
   exact deletion before stopping a wedged launcher.

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
