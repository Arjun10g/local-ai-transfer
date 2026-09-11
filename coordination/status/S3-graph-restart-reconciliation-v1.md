# S3 Graph Restart Reconciliation v1 Status

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Tools / Host Integration
- **Timestamp (UTC):** 2026-09-11T06:57:32Z
- **Branch/worktree:** `luna/graph-restart-reconciliation-v1` / `wt-graph-restart-reconciliation-v1`
- **Current phase:** Source-only hardening
- **Primary task ID:** GRAPH-RESTART-RECONCILIATION
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `d723c43263ee34211abe12c414aefa1c290a3ec1`

## Objective for this work interval

Add bounded automatic restart reconciliation only for Microsoft Graph journal records whose durable operation/account identity can be checked against fresh provider-owned immutable evidence, without converting ambiguous dispatches or locally inferred state into success. This revision resolves the independent S0/S4 review (`ACCEPT_WITH_REQUIRED_FIXES`, 2026-09-11) findings F-1 through F-8.

## Inputs and dependencies

- Contract/version: existing host/tool/action-journal contracts; additive source-only behavior under review
- Required commits: exact base `d723c43263ee34211abe12c414aefa1c290a3ec1`
- Model/build/profile IDs: none
- Handoffs consumed: accepted Graph composition/manual-resolution source and durable journal source already merged on the base; independent S0/S4 review report

## Disclosure: this slice changed already-merged in-flight reconciliation behavior

This is stated separately because the previous packet did not say it, and the review was right that it should have.

- **What was already merged.** `listDraftsForMarker` (`host/providers/microsoft-graph.mjs`) is the in-flight recovery path for `mail.create_draft`: when the POST dispatches but the response is lost, it looks for the marked draft. Before this slice it issued **one** Drafts collection query that asked for `internetMessageHeaders` in `$select`.
- **What this slice changed it to.** One bounded Drafts **ID** page followed by one explicit per-message `GET` for each returned ID.
- **Why.** Microsoft Graph populates `internetMessageHeaders` only on a single-message projection. A `/mailFolders/drafts/messages` collection query cannot return the custom `x-lae-operation` marker no matter what `$select` requests, so the merged single-query form could not actually prove anything against real Graph; it would have failed its own `Array.isArray(value.internetMessageHeaders)` guard and returned `draft_collection_unavailable` forever. Exact per-message proof is the correct shape.
- **The regression that introduced.** As first written, the rewrite could issue 1 + up to 50 sequential Graph requests inside an unchanged 10 s `mail.create_draft` `timeout_ms`. An overrun surfaces as `tool_timeout`; the controller then calls `markUnknown()`, and the record becomes `unknown_manual` — which the new restart path explicitly excludes. That converted a recoverable `reconciling` outcome into a permanently manual one. This was a real regression against `main@d723c43` and is fixed in `71f7fb5`.
- **Also changed on the merged path.** `reconcileCreateDraft` gained a `marker` parameter (this slice) and a `deadline` parameter (`71f7fb5`); the old "fail the whole collection if any draft lacks `internetMessageHeaders`" rule is gone, replaced by a strict `operation_marker === marker` filter, which is a net tightening of what can match but a relaxation of that specific guard.

## Work completed

### Base slice (`e6e1a55`, `31ab96d`)

- Created a fresh isolated worktree and branch at the exact requested base.
- Added a three-part immutable custom draft marker binding the journal operation ID/digest to the canonical device-authenticated account fingerprint; legacy/unverified identities remain ineligible for restart completion.
- Added a coalesced controller recovery pass at composition startup and after successful Graph authentication. It reads at most eight acknowledged records and permits completion only from a module-private exact provider attestation.
- Added conservative Graph proof retrieval: one bounded Drafts ID page followed by explicit GETs; pagination, duplicate IDs/headers/resources, malformed projections, normalization mismatch, content mismatch, account mismatch, epoch change, and non-unique matches fail closed.
- Kept `dispatching` startup recovery as `unknown_manual`, retained `reconciling`/`unknown_manual`, other Graph operations, legacy markers, and the manual-resolution refusal unchanged.

### Review repair (`71f7fb5`, `37bbf19`, `438795f`, `c549a8f`)

- **F-1 (MAJOR).** One shared `MAX_DRAFT_PROOF_CANDIDATES = 20` now bounds candidate GETs for both the in-flight and the restart path. The in-flight path derives a deadline from `mail.create_draft`'s own published `timeout_ms` minus a 2.5 s margin, refuses to start a proof request that cannot finish in the remainder (250 ms floor), and caps each request it does issue to the remaining budget. Budget exhaustion returns the typed `draft_proof_budget_exhausted` reconciling result; transport faults, malformed collections, and truncation are likewise returned as `reconciling` data instead of thrown. Operator cancellation still propagates. The seam can therefore no longer produce `tool_timeout`, so it can no longer drive a record to `unknown_manual`.
- **F-4 / F-8.** The restart pass no longer calls `status()`. It samples the auth epoch and verified account fingerprint read-only from module-private state *before* any provider call, and refuses to issue a request unless a live delegated token is already held with more margin (60 s) than the 30 s pass can consume. Measured revocations for the reviewer's 8-candidate scenario go from 32 to 0, and the epoch guard is now sampled before anything that could reinstall the fingerprint.
- **F-7.** A durable `complete()` rejection is counted and returned as a typed metadata-only `blocked` count (`state: 'degraded'`). The record keeps its durable state and stays eligible for a later pass. The pass result shape is now `{ state, examined, completed, blocked, code }`. The code is attacker-influenceable metadata because the journal object can be injected, so only a journal-owned `action_journal_*` identifier is surfaced; every other error collapses to `action_journal_complete_failed` (`c549a8f`).
- **F-3 / F-4 (tests).** Eight new cases; see Evidence.
- **Inventory.** `tests/host/graph-restart-reconciliation.test.mjs` was missing from `scripts/test/run_qa.py`, which reported it as an unknown entry and failed the inventory check. It is now classified `provider_fixture`.
- Design docs updated to match the final code: shared budget and rationale, the deadline and typed inconclusive result, the read-only authorization snapshot, the two deliberate operational limits, and what actually happens to a `reconciling` Graph record.

## Evidence

All commands run from the worktree root on 2026-09-11. Numbers are exact.

- Repair commits: `71f7fb5` (`security: bound in-flight Graph proof retrieval to a safe inconclusive result`), `37bbf19` (`qa: cover budget exhaustion, legacy markers, and both production restart triggers`), `438795f` (`docs: disclose the in-flight reconciliation change and record the repair evidence`), `c549a8f` (`security: restrict the restart failure code to journal-owned identifiers`), plus this final documentation commit. Base slice commits are `c3ca108`, `e6e1a55`, `31ab96d`.
- `npm test` (`node --test tests/host/*.test.mjs tests/security/*.test.mjs`): **425 discovered, 423 pass, 0 fail, 0 cancelled, 1 skipped, 1 todo, duration_ms 41197.132959** (wall 41.784 s). The single skip and single todo are the pre-existing filesystem `KNOWN LIMITATION` pair, unchanged. The count moved 417 -> 425 because this repair adds eight test cases.
- `node --test tests/host/graph-restart-reconciliation.test.mjs tests/host/graph-production-composition-restart.test.mjs tests/host/graph-manual-resolution-guard.test.mjs tests/host/external-tools.test.mjs`: **123 tests, 123 pass, 0 fail, 0 skipped, 0 todo, duration_ms 988.663**.
- `git diff --check main...HEAD`: no output, **exit 0**.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`: **status `BLOCKED`**, inventory **0 missing / 0 unknown**, 60 discovered, `passed false`, `release_passed false`. `BLOCKED` is the expected safe-mode result: safe mode executes no suite, so every mandatory suite is an unproven SKIP.
- Machine: local source workspace, Darwin arm64, Python 3.14.6. No native build, no model, no network, no provider contact.
- Artifact/index: none. Metrics above are the raw runner output.

New test cases added in `37bbf19` (all in `tests/host/graph-restart-reconciliation.test.mjs`):

1. In-flight proof stops inside the tool budget, returns `draft_proof_budget_exhausted`, and — through the real controller and a real durable journal — leaves the record `reconciling`, not `unknown_manual`.
2. Proof request budgets clamp to the transport timeout, refuse an unusable remainder, reject an out-of-range internal budget, and actually shorten the transport deadline.
3. A restart pass issues no request and revokes no grant without a live token, and performs no `/me` account check even when authenticated.
4. An auth epoch change mid-pass blocks completion even when the same account is re-verified.
5. A `complete()` rejection is surfaced as a typed metadata-only blocked count for a journal-typed, an untyped, and a foreign-typed failure, leaving the record `acknowledged`.
6. A legacy two-part marker is ineligible for restart completion.
7. An acknowledged non-`mail.create_draft` record is skipped with zero provider callbacks.
8. Composition startup invokes the pass on its own controller; a successful device-code authentication invokes it exactly once and a failed one never does.

## Findings and changed assumptions

- **`listSentForDigest` plausibly has the same latent collection-projection defect** (`host/providers/microsoft-graph.mjs`, Sent Items proof for `mail.send_draft`). It still requests `...,sentDateTime,internetMessageHeaders` in a single Sent Items collection query and requires `Array.isArray(value.internetMessageHeaders)` per item. If Graph's single-message-only projection behavior is what forced the draft path to per-ID GETs, then against real Graph every `mail.send_draft` completion attempt finds no headers, no item matches `sentMarker`, and the send stays reconciling/manual forever. This is deliberately **not fixed here** to keep the slice bounded, and it is unverifiable without a live account. **Recommended follow-up task: GRAPH-SENT-PROOF-PROJECTION** — apply the same bounded ID-page-plus-GET shape and shared candidate budget to the Sent Items proof, or record a documented reason the two paths differ.
- **The composition-startup trigger is operationally inert in production.** `MicrosoftDeviceCodeCredential` holds tokens only in memory, so a freshly restarted host has no verified account; the pass now returns without issuing a single Graph request. Only the post-authentication trigger can complete a record. The design doc states this plainly. Recommended follow-up: either remove the startup call or give the Graph lane a durable-credential story before presenting startup recovery as a working trigger.
- **The pass is bounded, not cancellable.** Both production call sites invoke `reconcileRestartActions()` with no signal. It is limited by eight candidates, 20 candidate GETs per candidate, a per-request transport timeout, and a 30-second pass deadline; host shutdown invalidates the credential rather than interrupting the pass. Recommended follow-up: thread the host shutdown/emergency-stop signal into both call sites.
- **An expired credential discovered mid-request can still clear the session.** `token()` clears auth (and revokes grants) on a credential error. The pass no longer initiates that: it refuses to start unless a cached token has more than 60 s of life, which exceeds the 30 s pass deadline, so the pass cannot be the cause. An operator-initiated clear during the pass still revokes, but the operator already revoked. This is fail-safe in direction (privilege removed, never granted) and is recorded rather than further changed.
- **A `reconciling` Graph record has no resolution path in this slice.** It is not eligible for restart completion, `POST .../reconcile` is HTTP 501, and both the operator `resolve` route and the journal itself refuse Graph mutation records with `action_journal_provider_proof_required`. Such a record stays active and visible through the bounded summary/detail reads until a future provider-owned adapter can return it to `acknowledged` or prove a terminal state. Ambiguity staying ambiguous is intended; the honest consequence is that ambiguous Graph drafts accumulate as unresolved active records against the 256-record active cap.
- **`summary()` ordering can starve older acknowledged records.** The pass reads the eight most recently updated acknowledged records each time; with more than eight, older ones are not reached until newer ones go terminal. Fail-closed, but starvation rather than round-robin. Recommended follow-up if the eight-record bound is ever raised.
- **Protocol deviation on an existing commit.** `e6e1a55` uses a `feat:` prefix, which is not in the `INTERSESSION_PROTOCOL.md` §18 list. Existing commits were not amended or rebased per the handoff constraints; the repair commits use `security:`, `qa:`, and `docs:`.
- Restart eligibility remains intentionally only `mail.create_draft`, only after an acknowledgement was durably written, and only for new three-part markers created under provider-attested device identity.
- Provider normalization, an account with more than 20 drafts, legacy two-part markers, budget exhaustion, or more than eight acknowledged candidates can produce a safe false negative; records remain active/manual and are never inferred failed or successful.

## Blockers

- Fact/evidence: no live Microsoft account/provider receipt is authorized.
- Impact: this slice proves behavior only with injected/mock transports and does not establish Graph/account readiness. The `listSentForDigest` finding above and the Graph collection-projection rationale itself cannot be confirmed without a live account.
- What was tried: exhaustive source/mock unit coverage within the repository's ordinary Node inventory, including regression tests that fail against the pre-repair source.
- Proposed workaround: adversarial unit coverage with mock provider evidence.
- Decision/asset needed: independent re-review of the repair commits.
- Owner: S0/S4.
- Independent work continuing: no; the bounded repair is ready for independent re-review.

## Handoffs

- To: S0 / S4
- Handoff file: this status packet
- Required by: review/merge decision
- Acknowledged: pending

## Next bounded action

Independent re-review of `31ab96d..HEAD` (`71f7fb5`, `37bbf19`, `438795f`, `c549a8f`, and this documentation commit), especially the in-flight proof budget and its typed inconclusive result, the read-only authorization snapshot and epoch ordering, the typed `blocked` result shape, and the unchanged journal durability and manual-resolution gates.

## Sol action requested

Independent source/security re-review of the repair commits, and disposition of the recommended follow-up tasks listed under Findings. **No readiness approval requested; no gate change requested.** This slice makes no claim of live Microsoft Graph, account, network, model, browser, native, or target readiness.
