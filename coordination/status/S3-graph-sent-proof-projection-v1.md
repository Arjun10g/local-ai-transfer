# S3 Graph Sent-Mail Proof Projection v1 Status

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Tools / Host Integration
- **Timestamp (UTC):** 2026-09-11T13:52:51Z
- **Branch/worktree:** `luna/graph-sent-proof-projection-v1` / `wt-graph-sent-proof-projection-v1`
- **Current phase:** Source-only hardening
- **Primary task ID:** GRAPH-SENT-PROOF-PROJECTION
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `fdfed07692ae35713432e02a2c1d7f729267acb4`

## Objective for this work interval

Resolve the follow-up recorded by the accepted Graph restart reconciliation
slice: `listSentForDigest` still asked a Sent Items collection query for
`internetMessageHeaders`, which Microsoft Graph returns only on a
single-message projection. Either apply the bounded ID-page-plus-per-message-GET
shape the draft path was already repaired to, sharing the code rather than
duplicating it, or record a documented reason the two paths differ. Source and
tests only; no live account, provider, gate, or readiness work.

## Inputs and dependencies

- Contract/version: existing host/tool/action-journal contracts; no contract
  changed and none added.
- Required commits: exact base `fdfed07692ae35713432e02a2c1d7f729267acb4`.
- Dependency task: GRAPH-RESTART-RECONCILIATION (merged source, pending gate).
- Handoffs consumed: `coordination/status/S3-graph-restart-reconciliation-v1.md`
  Disclosure + Findings; the prior independent review's finding F-3; the
  existing mock-transport test patterns in
  `tests/host/graph-restart-reconciliation.test.mjs` and
  `tests/host/external-tools.test.mjs`.

## The defect, confirmed

### Where it lives and what uses it

At exact base `fdfed07`:

- `host/providers/microsoft-graph.mjs:429-439` — `listSentForDigest`.
- `host/providers/microsoft-graph.mjs:430` — one Sent Items **collection**
  query: `GET /v1.0/me/mailFolders/sentitems/messages` with `$top=50`,
  `$orderby=sentDateTime desc`, and
  `$select=id,subject,body,toRecipients,ccRecipients,changeKey,sentDateTime,internetMessageHeaders`.
- `host/providers/microsoft-graph.mjs:434` — per item,
  `if (!Array.isArray(value?.internetMessageHeaders) || typeof value?.sentDateTime !== 'string') return null;`
  Because `uniqueProofMap` (`:211-220`) aborts the whole mapping the moment one
  item maps to `null`, a single unheadered item nulls the entire collection.
- `host/providers/microsoft-graph.mjs:438` — the surviving filter additionally
  requires `item.operation_marker === sentMarker`, which is `null` whenever the
  projection carried no headers.

Its only caller is `reconcileSendDraft`
(`host/providers/microsoft-graph.mjs:462-471`, call at `:468`), reached from
exactly two places, both for `mail.send_draft`:

- `host/providers/microsoft-graph.mjs:538` — the normal path, immediately after
  `POST /me/messages/{id}/send`.
- `host/providers/microsoft-graph.mjs:564` — the `provider_timeout` /
  `provider_failed` recovery path.

Reconciliation states it can produce: `send_reconciliation_not_authorized`,
`draft_state_unavailable`, `draft_still_present`, `sent_collection_truncated`,
`sent_collection_unavailable`, `sent_item_not_found`,
`multiple_matching_sent_items`, and the single completing state
`unique_sent_item`. `unique_sent_item` is the **only** member of that set in the
controller's `SAFE_RECONCILIATIONS` allow-list
(`host/agent/controller.mjs:84`), and Graph answers `message: send` with a
bodyless `202 Accepted`, so this proof is the only completion evidence
`mail.send_draft` has at all.

### Consequence today

Two consequences, both reproduced against the exact base source with an
injected transport that models Graph honestly (the collection listing omits
`internetMessageHeaders`; the per-message projection carries it):

1. **The proof is dead.** Every Sent Items item fails the `:434` guard,
   `uniqueProofMap` returns `null`, and the adapter answers
   `sent_collection_unavailable`. Measured on base `fdfed07`:
   `state = reconciling`, `completed = false`,
   `reconciliation = sent_collection_unavailable`, **0** per-message GETs, with
   the second Sent Items read carrying
   `$select=id,subject,body,toRecipients,ccRecipients,changeKey,sentDateTime,internetMessageHeaders`.
   `unique_sent_item` is unreachable, so no `mail.send_draft` can ever complete
   against a real account; every send stays `reconciling` and therefore manual,
   and manual `POST .../reconcile` is HTTP 501. This is a **safe false
   negative**: the record is never wrongly completed.
2. **It can escalate a recoverable record.** `listSentForDigest` threw on a
   transport fault. Measured on base `fdfed07` with HTTP 500 on the proof query:
   the tool result is `status: 'failed'`, `{"code":"provider_failed"}`, after
   **2** proof attempts (the `:564` recovery path retries it). The controller
   routes a non-`ok` result for a dispatched durable action to
   `markUnknown()` (`host/agent/controller.mjs:540`), so the record becomes
   `unknown_manual` — the one state every automatic recovery path, including
   the restart pass, is forbidden to touch.

The underlying Graph claim — that `internetMessageHeaders` is withheld from
collection listings — is **not verified against a live account** and cannot be
here. It is the same documented rationale the already-merged draft path
depends on, and the repair is fail-closed in either direction: a message that
would have carried the marker on a collection listing still carries it on the
per-message GET.

## Work completed

### `security: prove sent mail from a per-message projection instead of a collection` (`6eb8b34`)

- `collectDraftProof` generalized to `collectMailProof({ folder, listQuery,
  select, project, marker, matches, signal, deadline })`
  (`host/providers/microsoft-graph.mjs:377`). All three marker-proof seams —
  in-flight draft, restart draft, Sent Items — now run the *same* retrieval:
  one bounded folder ID page, then one explicit
  `GET /me/messages/{id}` per returned ID with an explicit `$select` naming
  `internetMessageHeaders`. Code is shared, not duplicated:
  `collectDraftProof` (`:398`) and `listSentForDigest` (`:465`) are thin
  wrappers that differ only in folder, list query, `$select`, projection, and
  match predicate.
- **Request bound, explicit and documented:** exactly **1 list page + at most
  `MAX_MAIL_PROOF_CANDIDATES` (20) GETs** per call, so at most 21 provider
  requests per reconciliation, on top of the 3 pre-reconcile requests
  (`mail.send_draft` therefore costs at most 25 provider requests end to end:
  draft identity GET, Sent Items baseline, `POST .../send`, draft-presence GET,
  proof ID page, and up to 20 exact GETs).
- `MAX_DRAFT_PROOF_CANDIDATES` → `MAX_MAIL_PROOF_CANDIDATES` and
  `DRAFT_PROOF_SELECT` → `MAIL_PROOF_SELECT`, because three seams now share
  them. `SENT_PROOF_SELECT` = `MAIL_PROOF_SELECT` + `sentDateTime`. Both were
  module-private with zero references outside the file; every reference and
  test was updated (see Findings for the two historical packets that name the
  old constant).
- `OPERATION_MARKER` hoisted out of `projectionDraft` and used as a
  precondition inside the shared retrieval: an absent or malformed marker is
  refused **before any request**, so a `null` expectation can never equal a
  projection's own absent marker. This replaces and strengthens the previous
  `if (!marker)` guards in `listDraftsForMarker` / `listDraftsForRestartProof`.
- `listSentForDigest` (`:456`) now requires a 64-hex bound content digest, a
  real `Set` of pre-existing Sent Items IDs, a finite pre-send snapshot, and a
  marker string before any call, and returns the same typed
  `{ values, truncated, exhausted }` shape as the draft seam instead of
  throwing. Operator cancellation (`provider_cancelled`, or an aborted signal)
  still propagates.
- `reconcileSendDraft` (`:494`) takes the deadline derived from
  `mail.send_draft`'s own published 10 s `timeout_ms` minus the existing 2.5 s
  margin, caps the draft-presence GET and every proof request to the remainder,
  and returns the typed `sent_proof_budget_exhausted` reconciling result rather
  than overrunning into `tool_timeout` → `markUnknown()`. The deadline is
  sampled once at the start of the `mail.send_draft` branch and reused by the
  recovery path.
- Fail-closed set unchanged in direction and widened in coverage: duplicate
  IDs, non-unique matches, malformed projections, `@odata.nextLink`, over-cap
  pages, ID mismatch on the exact GET, duplicated `x-lae-operation` headers,
  foreign-account (three-part) markers, content mismatch, pre-snapshot or
  undated timestamps, and budget exhaustion all end `reconciling`.

### `security: make the shared candidate cap un-overridable by a caller query` (`686ea9b`)

Self-review fix: `$top` is applied after the caller's `listQuery`, so a future
seam cannot widen the shared candidate cap through its own query. No behavior
change for either current caller.

### `qa: cover the Sent Items proof projection with hostile provider fixtures` (`85c0c1d`)

- New `tests/host/graph-sent-proof-projection.test.mjs`: 6 cases, 17 hostile
  sub-cases. Registered in `scripts/test/run_qa.py` `TEST_INVENTORY` as
  `provider_fixture`.
- Three existing send-proof fixtures in `tests/host/external-tools.test.mjs`
  served the full single-message projection from the collection listing, which
  Graph never does; they now serve an ID page and answer the per-message GET.

### `docs: describe the sent-mail proof path as it is now implemented` (`5bc6fda`)

`host/providers/MICROSOFT_GRAPH_RECONCILIATION.md` no longer carries the "open
question" paragraph. It states the pre-send baseline collection query (which a
collection query *can* answer), the shared marker retrieval, the exact
per-message `$select`, the per-call request bound, the marker-shape
precondition, both budget-exhaustion codes, what the old query actually did
against a real account, and that the Graph collection-projection rationale is
still unverified without a live account.

## Evidence

All commands run from the worktree root
`/Users/arjunghumman/Downloads/VS Code Stuff/Python/Local BMO MVP/wt-graph-sent-proof-projection-v1`
on 2026-09-11. Numbers are exact.

- Commits on `luna/graph-sent-proof-projection-v1` from exact base `fdfed07`:
  `e0d5d72` (docs: claim), `6eb8b34` (security: the repair), `85c0c1d` (qa:
  tests + inventory), `5bc6fda` (docs: design doc), `686ea9b` (security: cap
  hardening), plus this packet commit. Tip is recorded in
  `coordination/TASK_CLAIMS.md`.
- `npm test` (`node --test tests/host/*.test.mjs tests/security/*.test.mjs`):
  **432 tests, 430 pass, 0 fail, 0 cancelled, 1 skipped, 1 todo,
  duration_ms 41755.443166**. The single skip and single todo are the
  pre-existing filesystem `KNOWN LIMITATION` pair, unchanged. The count moved
  426 → 432 because this slice adds six test cases.
- `node --test tests/host/graph-restart-reconciliation.test.mjs
  tests/host/external-tools.test.mjs
  tests/host/graph-sent-proof-projection.test.mjs`: **109 tests, 109 pass,
  0 fail, 0 skipped, 0 todo**.
- Wider focused set, adding
  `tests/host/graph-production-composition-restart.test.mjs` and
  `tests/host/graph-manual-resolution-guard.test.mjs`: **130 tests, 130 pass,
  0 fail, 0 skipped, 0 todo, duration_ms 1099.769542**.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`:
  **status `BLOCKED`**, inventory **0 missing / 0 unknown**, 63 discovered,
  69 results (**1 PASS / 68 expected SKIP**), 69 release blockers,
  `passed false`, `release_passed false`. `BLOCKED` is the expected safe-mode
  result: safe mode executes no suite, so every mandatory suite is an unproven
  SKIP.
- `git diff --check main...HEAD`: no output, **exit 0**.
- `git status --short`: empty (**0 lines**).
- Machine: local source workspace, Darwin arm64, Node v25.9.0, Python 3.14.6.
  No native build, no model, no network, no provider contact.
- Artifact/index: none. Numbers above are raw runner output.

### Regression evidence against the pre-repair source

A pristine copy of exact `main@fdfed07` was extracted with
`git archive fdfed07692ae35713432e02a2c1d7f729267acb4 | tar -x -C <scratch>/baseline`,
and the new test files were copied into it unchanged.

- `node --test tests/host/graph-sent-proof-projection.test.mjs` in the
  pre-repair tree: **6 tests, 0 pass, 6 fail**. The headline case,
  *"Sent Items proof lists IDs then GETs each message with an explicit header
  projection"*, fails there with `actual 'reconciling'` /
  `expected 'completed'` — the defect itself, not a shape assertion. The same
  file in this worktree is **6 tests, 6 pass, 0 fail**, reproduced on five
  consecutive runs.
- `node --test tests/host/external-tools.test.mjs` (this slice's updated
  fixtures) in the pre-repair tree: **89 tests, 87 pass, 2 fail** — *"Graph
  send-draft 202 is verified only by draft disappearance and one new Sent
  Item"* and *"Graph send-draft never verifies an old or concurrently matching
  Sent Item"*. Both pass here.
- The two defect probes quoted under "Consequence today" were run directly
  against that baseline tree and then against this worktree; the repaired
  results are `state = completed`, `reconciliation = unique_sent_item`, 1 proof
  ID page, 1 per-message GET; and for the HTTP 500 case,
  `status = ok`, `reconciliation = sent_collection_unavailable`, 1 proof
  attempt, no `provider_failed`.

### New test cases (`tests/host/graph-sent-proof-projection.test.mjs`)

Every fixture drives the provider from an injected clock, so the bounded
deterministic send window and the tool budget are exact rather than wall-clock
dependent.

1. **Exact request shape.** One `$top=20`, `$orderby=sentDateTime desc`,
   `$select=id` Sent Items ID page, then one
   `GET /v1.0/me/messages/{id}` whose `$select` is exactly
   `id,subject,body,toRecipients,ccRecipients,internetMessageHeaders,changeKey,sentDateTime`
   with the text `Prefer` header. The collection query is asserted **not** to
   ask for `internetMessageHeaders`. The pre-send baseline query is asserted
   unchanged at `$top=50`, `$select=id,sentDateTime`.
2. **Request bound.** 20 candidates cost exactly 1 list page + 20 GETs.
3. **Hostile responses (17 sub-cases), each asserted twice** — on the provider
   payload, and on the durable record after the identical send is driven
   through a real `ConversationController` and a real `ActionJournal`: transport
   throw, HTTP 500, malformed `value`, `@odata.nextLink`, over-cap page,
   duplicate IDs, unusable ID, ID mismatch on the exact GET, two matches,
   duplicated operation header, missing marker, empty header collection,
   foreign-account marker, content mismatch, pre-snapshot timestamp, undated
   match, and no sent item at all. Every case ends with the record
   `reconciling` — never `completed`, never `unknown_manual`.
4. **Budget exhaustion** returns the typed `sent_proof_budget_exhausted`
   reconciling result with `status: 'ok'`, a partial bounded walk, and a total
   elapsed provider clock under the 10 s `mail.send_draft` budget; through the
   real controller the record still ends `reconciling`.
5. **Operator cancellation** mid-walk propagates as `provider_cancelled` and
   stops the walk.
6. **Preconditions** (absent, empty, malformed, or over-long marker; short
   digest; non-`Set` ID collection; non-finite snapshot) cost **zero** provider
   requests.

## Findings and changed assumptions

- **`mail.send_draft` could not complete against a real account before this
  slice.** That is stronger than the follow-up row's "plausibly"; the pre-repair
  source reproduces it deterministically against a Graph-faithful transport.
  It was always a safe false negative, never a false completion.
- **A second, separate defect was found and fixed.** The pre-repair
  `listSentForDigest` threw on transport faults, which reaches the controller
  as a failed tool result and is recorded as `unknown_manual` — the exact
  regression class the restart slice repaired on the draft path. It is now
  typed inconclusive data, matching the draft seam.
- **Behavior changes on already-merged paths, disclosed deliberately.**
  (a) `reconcileSendDraft`'s draft-presence GET now propagates operator
  cancellation instead of reporting `draft_state_unavailable`; this matches the
  draft seam, and the outcome moves from `reconciling` to a cancelled call,
  which the controller records as `unknown_manual` for a dispatched action. That
  is a deliberate choice for symmetry with the reviewed draft path: an operator
  decision is not ambiguity. (b) `mail.send_draft` is now subject to a proof
  deadline it did not have; if the three pre-reconcile requests consume the
  budget, reconciliation returns `sent_proof_budget_exhausted` with zero proof
  requests instead of attempting a walk the controller would kill at 10 s
  anyway. (c) The marker-shape precondition means a truthy but malformed marker
  now yields `*_collection_unavailable` with zero requests instead of a
  request-spending walk that could never match.
- **Constant rename leaves two historical documents naming the old symbol.**
  `coordination/status/governance-refresh-v4.md:119` and
  `coordination/status/S3-graph-restart-reconciliation-v1.md:46` cite
  `MAX_DRAFT_PROOF_CANDIDATES = 20`. Those are accepted historical packets and
  were **not** rewritten; the value, the bound, and the file are unchanged, only
  the identifier is now `MAX_MAIL_PROOF_CANDIDATES`. Flagged here rather than
  falsifying history.
- **Unchanged and out of scope, but worth recording:** the pre-send Sent Items
  baseline (`host/providers/microsoft-graph.mjs`, the `mail.send_draft` branch)
  still refuses the send outright when the mailbox's first `$top=50` Sent Items
  page carries `@odata.nextLink`. Against a mailbox with more than 50 sent
  items that refuses every send pre-dispatch. It is fail-closed and it is a
  valid collection query (no projection defect), so it was left alone rather
  than folded into this slice. **Recommended follow-up.**
- **The Graph projection rationale remains unverified.** Whether Graph truly
  withholds `internetMessageHeaders` from collection listings cannot be
  confirmed without a live account. The repair is fail-closed either way, and
  it makes the two proof paths consistent, which the prior review asked for
  explicitly.
- `reconciling` Graph records still have no resolution path: manual
  `POST /api/action-journal/<id>/reconcile` remains HTTP 501 and the journal
  still refuses Graph mutation records with
  `action_journal_provider_proof_required`. Unchanged by this slice.
- No new public route, no new dependency, no new contract, no journal cap,
  transition, or integrity change, and no content, token, or secret is logged.

## Blockers

- Fact/evidence: no live Microsoft account or provider receipt is authorized,
  and none was obtained.
- Impact: this slice proves behavior only with injected transports and
  synthetic credentials. It establishes no Graph, account, network, or
  readiness claim, and the Graph collection-projection rationale it acts on
  remains unverifiable here.
- What was tried: adversarial unit coverage against a Graph-faithful mock,
  plus regression runs of the new and changed tests against a pristine
  extraction of exact `main@fdfed07`.
- Proposed workaround: the mock-transport coverage above; the fail-closed
  direction holds whichever way the live projection behaves.
- Decision/asset needed: independent source/security review.
- Owner: S0/S4.
- Independent work continuing: no; the bounded repair is ready for review.

## Handoffs

- To: S0 / S4
- Handoff file: this status packet
- Required by: review/merge decision
- Acknowledged: pending

## Next bounded action

Independent source/security review of `fdfed07..HEAD` — especially the shared
`collectMailProof` seam and its marker precondition, the Sent Items match
predicate and window, the new `mail.send_draft` proof deadline and its two
`sent_proof_budget_exhausted` exits, the three disclosed behavior changes on
already-merged paths, and the unchanged pre-send baseline refusal recorded
under Findings.

## Sol action requested

Independent source/security review; no readiness approval requested. No gate,
phase, or release change is requested, and this slice makes no claim of live
Microsoft Graph, account, network, model, browser, native, or target
readiness.
