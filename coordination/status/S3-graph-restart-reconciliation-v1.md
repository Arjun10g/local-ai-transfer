# S3 Graph Restart Reconciliation v1 Status

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Tools / Host Integration
- **Timestamp (UTC):** 2026-09-09T15:29:30Z
- **Branch/worktree:** `luna/graph-restart-reconciliation-v1` / `wt-graph-restart-reconciliation-v1`
- **Current phase:** Source-only hardening
- **Primary task ID:** GRAPH-RESTART-RECONCILIATION
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `d723c43263ee34211abe12c414aefa1c290a3ec1`

## Objective for this work interval

Add bounded automatic restart reconciliation only for Microsoft Graph journal records whose durable operation/account identity can be checked against fresh provider-owned immutable evidence, without converting ambiguous dispatches or locally inferred state into success.

## Inputs and dependencies

- Contract/version: existing host/tool/action-journal contracts; additive source-only behavior under review
- Required commits: exact base `d723c43263ee34211abe12c414aefa1c290a3ec1`
- Model/build/profile IDs: none
- Handoffs consumed: accepted Graph composition/manual-resolution source and durable journal source already merged on the base

## Work completed

- Created a fresh isolated worktree and branch at the exact requested base.
- Loaded mandatory governance and recorded a narrow eligibility boundary: fresh exact provider proof is required; ambiguous dispatch remains manual.
- Added a three-part immutable custom draft marker binding the journal operation ID/digest to the canonical device-authenticated account fingerprint; legacy/unverified identities remain ineligible for restart completion.
- Added a coalesced controller recovery pass at composition startup and after successful Graph authentication. It reads at most eight acknowledged records and permits completion only from a module-private exact provider attestation.
- Added conservative Graph proof retrieval: one bounded Drafts ID page followed by explicit GETs with `$select=internetMessageHeaders,...`; pagination, duplicate IDs/headers/resources, malformed projections, normalization mismatch, content mismatch, account mismatch, epoch change, and non-unique matches fail closed.
- Kept `dispatching` startup recovery as `unknown_manual`, retained `reconciling`/`unknown_manual`, other Graph operations, legacy markers, and the manual-resolution refusal unchanged.

## Evidence

- Commit: `e6e1a55` (`feat: reconcile acknowledged Graph drafts after restart`)
- Commands: `npm test`; focused `node --test` Graph restart/composition/manual guard suites; source import check; `git diff --check`
- Tests: full ordinary Node source/mock inventory 417 discovered, 415 pass, 0 fail, 1 explicit model-run skip, 1 existing filesystem TODO; final focused Graph/import run 26/26 plus `imports_ok`
- Machine: local source workspace; no native build
- Artifact/index: none
- Metrics: full Node duration 41.509 seconds; final focused duration 0.318 seconds

## Findings and changed assumptions

- Existing WAL/event framing did not need to change: the journal already preserves the operation and arguments digests, while the immutable provider header now carries the canonical account binding.
- Restart eligibility is intentionally only `mail.create_draft`, only after an acknowledgement was durably written, and only for new three-part markers created under provider-attested device identity.
- Provider normalization, an account with more than one bounded Drafts page, legacy two-part markers, or more than eight acknowledged candidates can produce a safe false negative; records remain active/manual and are never inferred failed or successful.
- Manual `POST .../reconcile` remains HTTP 501 and never invokes a provider; the new path is internal and authentication-triggered.

## Blockers

- Fact/evidence: no live Microsoft account/provider receipt is authorized.
- Impact: this slice proves behavior only with injected/mock transports and does not establish Graph/account readiness.
- What was tried: exhaustive source/mock unit coverage within the repository's ordinary Node inventory.
- Proposed workaround: adversarial unit coverage with mock provider evidence.
- Decision/asset needed: independent review after commit.
- Owner: S0/S4.
- Independent work continuing: no; bounded implementation is ready for independent review.

## Handoffs

- To: S0 / S4
- Handoff file: this status packet
- Required by: review/merge decision
- Acknowledged: pending

## Next bounded action

Independent audit of `d723c43..e6e1a55`, especially provider proof identity, account/operation binding, query bounds, auth epoch races, and unchanged manual-resolution/journal durability gates.

## Sol action requested

Independent source/security review after commit; no readiness approval requested.
