# Status Packet — Governance Refresh v4

- **Session:** Luna docs
- **Required model:** GPT-5.6 Luna
- **Role:** Evidence-bound governance/status reconciler; no integration or gate authority
- **Timestamp (UTC):** 2026-09-11T07:35:18Z
- **Branch/worktree:** `luna/governance-refresh-v4` / `wt-governance-refresh-v4`
- **Current phase:** Source-hardening governance reconciliation; formal gates unchanged
- **Primary task ID:** GOV-TRUTH-006
- **Secondary task ID, if any:** none
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `6c3a125749581554b88ade018181f61667990ac8`
- **Audited integrated source baseline:** exact
  `main@7239b7e1a6a512cabf9e5ab18ba463a7fac351fc` (the last source merge)

## Objective for this work interval

Reconcile only the program's current-truth governance, status, task, blocker,
and release-gate records to the newly integrated `main` after three
independently reviewed slices landed, record Sol's ICR-RUN-WDJB-001 decision and
ADR-0004, and preserve every existing release/full-access refusal. This is a
docs-only interval; it advances no gate.

## Base and scope

- Branch base: claim-only commit `b50b31e` on top of `6e0d12c`; `main` merged in
  by `e8d66b3` (`git merge --no-ff`), clean, with `git merge-base HEAD main` ==
  `git rev-parse main` == `6c3a1257`.
- Baseline convention: the audited integrated source baseline is the last
  source merge `7239b7e`. The docs descendant `6c3a1257` (the integrator's
  task-claim flip) and this refresh are documentation descendants, not
  self-referential source hashes.
- Files changed: `coordination/STATUS.md`, `coordination/RELEASE_GATES.md`,
  `coordination/BLOCKERS.md`, `coordination/TASK_CLAIMS.md`,
  `coordination/DECISIONS.md`, `coordination/INTERFACE_CHANGE_REQUESTS.md`,
  `coordination/adrs/ADR-0004-inert-contract-additive-extension.md` (new),
  `coordination/status/S0.md`, this packet, and
  `governance/MODEL_DECISION.md`. Markdown only; no source, test, contract
  JSON, configuration, script, or `.gitignore` change.

## Inputs and dependencies

- Contract/version: no interface change is made here. ICR-RUN-WDJB-001 is
  recorded as Sol-approved with contract version `0.1.0` retained.
- Required commits: `4280e95`/`61c9475` (Graph restart reconciliation),
  `e4ca09b`/`4de01f7` (Windows descriptor journal bootstrap),
  `cfe8136`/`7239b7e` (Windows process authority), `d723c43` (common slice
  base), `b9521cd`/`01698e0`/`d723c43` (GOV-TRUTH-005, now on `main`).
- Model/build/profile IDs: fixed Qwen3.5-9B Q4_K_M decision; no artifact,
  runtime, model, provider, or network was used.
- Handoffs consumed: the three merged slice packets, the three independent
  review reports (each re-review returning `ACCEPT_FOR_MERGE`), and Sol's
  decisions for this refresh.

## Commands run

All from the worktree root, after merging `main`:

- `npm test`
- `python3 -m unittest discover -s tests/native -p 'test_windows_*static.py'`
- `python3 -m unittest tests.qa.test_safe_runner`
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`
- Strict tracked-JSON inventory: every `git ls-files '*.json'` parsed with a
  duplicate-key-rejecting `object_pairs_hook`
- Host import graph over `lae-host.mjs` plus `host/**/*.mjs`
- `git diff --stat main..HEAD`, `git diff --check main..HEAD`,
  `git rev-parse --verify` on every SHA written into these documents

## Evidence

Exact numbers, reproduced in this worktree on 2026-09-11:

- `npm test`: **426 tests, 424 pass, 0 fail, 0 cancelled, 1 skipped, 1 todo,
  duration_ms 40677.278375**. The single skip and single todo are the
  pre-existing filesystem `KNOWN LIMITATION` pair.
- `python3 -m unittest discover -s tests/native -p 'test_windows_*static.py'`:
  **`Ran 289 tests` … OK**.
- `python3 -m unittest tests.qa.test_safe_runner`: **`Ran 20 tests` … OK**.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`:
  **status `BLOCKED`**, `passed false`, `release_passed false`, inventory
  **62 discovered / 0 missing / 0 unknown**, `discovery_error null`, **68
  records = 1 PASS / 67 SKIP**, process exit code 1. `BLOCKED` is the expected
  safe-mode result: safe mode executes no suite, so every mandatory suite is an
  unproven SKIP.
- Strict tracked-JSON inventory: **153 tracked JSON files, 152 strict-valid**
  under a duplicate-key-rejecting parser, **1 intentional hostile fixture** —
  `tests/native/fixtures/windows_broker/duplicate-key.json` (duplicate key
  `deadline_ms`).
- Host import graph: **29 modules, 67 unique relative import edges, 0
  unresolved specifiers, 0 cycles**, from 72 relative import occurrences.
  *Counting method:* the graph covers `lae-host.mjs` plus every tracked
  `host/**/*.mjs`, collecting relative specifiers from static `import`/`export
  … from` and dynamic `import()` forms, resolving each against the importing
  file's directory, and counting each distinct importer/target pair once — five
  pairs appear twice in source (an import plus a re-export, for example), which
  is the whole difference between 72 occurrences and 67 unique edges. The
  2026-09-09 snapshot recorded 71 edges under an unstated method; no delta is
  asserted here, only this measurement and how it was taken.
- `git diff --check main..HEAD`: no output, **exit 0**.
- `git diff --stat main..HEAD`: **Markdown files only**.
- Machine: development macOS host, Darwin arm64. No native build, no model, no
  network, no provider contact, and no target equivalence is claimed.
- Artifact/index: this packet and the nine reconciled documents. Metrics: none
  beyond the raw runner output above.

## Recorded decisions

- **Baseline.** Audited integrated source baseline is exact
  `main@7239b7e1a6a512cabf9e5ab18ba463a7fac351fc`; `6c3a1257` and this refresh
  are documentation descendants.
- **Three merged slices**, each independently S0/S4 source-reviewed with one
  repair round and a re-review returning `ACCEPT_FOR_MERGE`, none adding
  compile, live, provider, production, Windows, or target evidence:
  Graph restart reconciliation (`4280e95`, base `d723c43`, merge `61c9475`);
  Windows descriptor journal bootstrap (`e4ca09b`, merge `4de01f7`); Windows
  process authority (`cfe8136`, merge `7239b7e`).
- **ICR-RUN-WDJB-001.** Sol approves the four additive status codes
  (`handoff_already_transferred`, `source_handle_inheritable`,
  `inheritance_control_failed`, `final_path_mismatch`) in
  `contracts/action-journal-storage/v0.1.0.json` with version `0.1.0` retained,
  merged at `4de01f7`. Recorded in
  `coordination/INTERFACE_CHANGE_REQUESTS.md` and generalized by new
  `coordination/adrs/ADR-0004-inert-contract-additive-extension.md`, which also
  records that a separate frozen contract is REQUIRED for the v2 WAL boundary
  before any transport, import, package, or activation path exists. ADR-0004 is
  indexed in `coordination/DECISIONS.md`.
- **B-005.** The process-authority packet's proposed wording is folded into the
  B-005 *Fact* paragraph with `<merge-sha>` replaced by `7239b7e`.
  `State: OPEN` is unchanged; the slice narrows B-005 in ownership and shape
  only.
- **B-003.** The *Fact* paragraph now records the restart reconciliation source
  and its limits.
- **Task claims.** GOV-TRUTH-005 flipped to `MERGED_SOURCE_PENDING_GATE` with
  `b9521cd`, `01698e0`, `d723c43`; GOV-TRUTH-006 added as `READY_FOR_REVIEW`
  with review owners S0/S4; `GRAPH-SENT-PROOF-PROJECTION` added `UNCLAIMED`
  with owner S3 and dependency GRAPH-RESTART-RECONCILIATION.

## Findings and changed assumptions

- Every number above reproduces the integrator's figures exactly, including the
  import-graph edge count of 67. The 2026-09-09 figure of 71 edges is left
  standing as that interval's recorded number; because its counting method was
  not stated, no delta is asserted.
- The prior `6e0d12c` focused numbers (journal/Graph Node 210/209,
  handoff/release 63/63, env 26/26, QA-runner 27/27, conformance 11/11, JSON
  152/151, imports 29/71, QA 59 discovered/65 records) are now explicitly
  marked historical wherever they appeared as current truth.
- Merged source remains distinct from evidence and from approval. All three
  slices are source-only. The Graph restart pass is operationally inert at
  startup because tokens are memory-only; the descriptor-WAL bootstrap and the
  launch authority have never been compiled and sit outside every
  product/package/activation graph.
- `listSentForDigest` plausibly carries the same collection-projection defect
  that forced the draft proof path to a bounded ID page plus per-message GETs.
  It is unverifiable without a live account and is now tracked as
  `GRAPH-SENT-PROOF-PROJECTION` rather than left in a packet.
- Release/full access remains `BLOCKED` / `NOT_READY`, and every Phase gate
  state cell in `coordination/RELEASE_GATES.md` is byte-identical to `main`.

## Blockers

- No blocker prevents this docs-only reconciliation. Existing model, provider,
  native Windows compile/secure-owner, hardware-receipt, artifact-custody,
  credential-rotation, and production-journal gates remain binding and are
  recorded without advancement.

## Handoffs

- To: S0 and S4
- Handoff file: this packet plus the final branch tip
- Required by: governance review and Sol's merge decision
- Acknowledged: pending

## Next bounded action

Independent S0/S4 docs and evidence-scope audit, followed by Sol's merge
decision. No live, model, provider, native, browser, or target run follows from
this task.

## Sol action requested

Review and merge; no gate change. Only Sol may merge or alter a phase/release
gate, and nothing in this refresh requests either.
