# Status Packet — Governance Refresh v5

- **Session:** Luna docs
- **Required model:** GPT-5.6 Luna
- **Role:** Evidence-bound governance/status reconciler; no integration or gate authority
- **Timestamp (UTC):** 2026-09-11T17:10:00Z
- **Branch/worktree:** `luna/governance-refresh-v5` / `wt-governance-refresh-v5`
- **Current phase:** Source-hardening governance reconciliation; formal gates unchanged
- **Primary task ID:** GOV-TRUTH-007
- **Secondary task ID, if any:** none
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f`
- **Audited integrated source baseline:** exact
  `main@263f11413d1746044a6cc13062ad2b1f821c4d11` (the last source merge)

## Objective for this work interval

Reconcile only the program's current-truth governance, status, task, blocker,
and release-gate records to the newly integrated `main` after two independently
reviewed slices landed, and record the Sol decisions established today: the new
baseline, the corrected model-testing truth (including the 27/34 → 28/34
correction), the deleted-comparator consequence for the quality-retention gate,
the unauthored quality corpus, the aborted 2026-09-11 local development run,
and the worktree housekeeping record. This is a docs-only interval; it advances
no gate.

## Base and scope

- Branch base: `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f`, the current `main`
  tip, taken directly as the worktree base. No merge was required and no
  conflict arose.
- Baseline convention: the audited integrated source baseline is the last
  source merge `263f11413d1746044a6cc13062ad2b1f821c4d11`. The docs
  descendants `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f` and
  `1b22a40e21dce0ebc903bc3c2024ab162adc2d56` (task-claim flips) and this
  refresh are documentation descendants, not self-referential source hashes.
  The previous baseline `7239b7e` and the v4 snapshot are retained but
  retitled historical/superseded.
- Files changed: `coordination/STATUS.md`, `coordination/RELEASE_GATES.md`,
  `coordination/BLOCKERS.md`, `coordination/TASK_CLAIMS.md`,
  `governance/MODEL_DECISION.md`, `coordination/status/S0.md`, this packet
  (`coordination/status/governance-refresh-v5.md`), and one new receipt file
  `artifacts/dev-evidence/local-macos-20260911-aborted/README.md`. Markdown
  only; no source, test, contract JSON, configuration, script, or `.gitignore`
  change.

## Inputs and dependencies

- Contract/version: no interface change is made here. The descriptor-WAL
  contract `v0.1.0` is recorded as frozen on merge, and
  `contracts/action-journal-storage/v0.1.0.json` is confirmed byte-identical to
  `main`.
- Required commits: `9f136a0`/`237d59b30ac3e3424990636595cc5dab88480ad0`
  (descriptor WAL contract), `26beb82`/`263f11413d1746044a6cc13062ad2b1f821c4d11`
  (Graph sent-mail proof projection),
  `fdfed07692ae35713432e02a2c1d7f729267acb4` (GOV-TRUTH-006 merge),
  `1b22a40e21dce0ebc903bc3c2024ab162adc2d56` and
  `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f` (claims flips).
- Model/build/profile IDs: fixed Qwen3.5-9B Q4_K_M decision; production
  evaluation fixture `tests/model/production_tool_call_eval.json` at 33 tools /
  37 cases, SHA-256
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`, hashed in
  this worktree. No artifact, runtime, model, provider, or network was used.
- Handoffs consumed: the two merged slice packets
  (`coordination/status/S1-descriptor-wal-contract-v1.md`,
  `coordination/status/S3-graph-sent-proof-projection-v1.md`), the two
  independent review reports (each returning `ACCEPT_FOR_MERGE` on the first
  pass with NOTE/MINOR findings only), the model-testing status report, the
  aborted local-run receipt, the worktree cleanup report, and Sol's decisions
  for this refresh.

## Commands run

All from the worktree root:

- `npm test`
- `python3 -m unittest discover -s tests/native -p 'test_*static.py'`
- `python3 -m unittest discover -s tests/native -p 'test_windows_*static.py'`
- `python3 -m unittest discover -s tests/native -p 'test_descriptor_wal_contract_static.py'`
- `python3 -m unittest tests.qa.test_safe_runner`
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`
- Strict tracked-JSON inventory: every `git ls-files '*.json'` parsed with a
  duplicate-key-rejecting `object_pairs_hook`
- Host import graph over `lae-host.mjs` plus `host/**/*.mjs`
- `shasum -a 256 tests/model/production_tool_call_eval.json`
- `git ls-files artifacts/qwen35-9b/remote-eval-20260904-j/eval-receipt.json`
  plus a read of its `metrics`
- `git diff --stat main..HEAD`, `git diff --check main..HEAD`,
  `git rev-parse --verify` on every SHA written into these documents

## Evidence

Exact numbers, reproduced in this worktree on 2026-09-11:

- `npm test`: **432 tests, 430 pass, 0 fail, 0 cancelled, 1 skipped, 1 todo**.
  The single skip and single todo are the pre-existing filesystem
  `KNOWN LIMITATION` pair. Wall clock for this one local run was `duration_ms
  41183.627041` — a single machine-specific observation, not reproducible
  evidence, which is why no duration is carried into any current-truth file.
- `python3 -m unittest discover -s tests/native -p 'test_*static.py'`:
  **`Ran 309 tests` … OK**.
- `python3 -m unittest discover -s tests/native -p 'test_windows_*static.py'`:
  **`Ran 289 tests` … OK**. The narrower Windows-only pattern is the figure the
  previous refreshes recorded; **309 = 289 + 20**, the extra 20 being the new
  `tests/native/test_descriptor_wal_contract_static.py` contract suite, which
  runs `Ran 20 tests … OK` on its own and is not matched by the
  `test_windows_*` pattern.
- `python3 -m unittest tests.qa.test_safe_runner`: **`Ran 20 tests` … OK**.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`:
  **status `BLOCKED`**, `passed false`, `release_passed false`, inventory
  **64 discovered / 0 missing / 0 unknown**, `discovery_error null`, **70
  records = 1 PASS / 69 SKIP**, process exit code 1. `BLOCKED` is the expected
  safe-mode result: safe mode executes no suite, so every mandatory suite is an
  unproven SKIP.
- Strict tracked-JSON inventory: **154 tracked JSON files, 153 strict-valid**
  under a duplicate-key-rejecting parser, **1 intentional hostile fixture** —
  `tests/native/fixtures/windows_broker/duplicate-key.json` (duplicate key
  `deadline_ms`).
- Host import graph: **29 modules, 67 unique relative import edges, 0
  unresolved specifiers, 0 cycles**, from 72 relative import occurrences.
  *Counting method* (identical to the v4 packet's stated method): the graph
  covers `lae-host.mjs` plus every tracked `host/**/*.mjs`, collecting relative
  specifiers from static `import`/`export … from` and dynamic `import()` forms,
  resolving each against the importing file's directory, and counting each
  distinct importer/target pair once — five pairs appear twice in source, which
  is the whole difference between 72 occurrences and 67 unique edges. The
  figures are unchanged from the v4 interval.
- Production fixture identity: `shasum -a 256
  tests/model/production_tool_call_eval.json` is
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`, with
  `tools` length 33 and `cases` length 37 — matching the recorded profile
  identity exactly.
- Quality spec: `model/quality-eval/quality-fixture-spec.json` has 13
  `categories` whose `minimum_cases` fields sum to **1,180**, against **3**
  `fixture_cases`.
- Comparators: `artifacts/qwen35-9b/scan-receipt.json` lists `Qwen3.5-9B-bf16`
  and `Qwen3.5-9B-Q8_0` with hashes and sizes only, and
  `artifacts/qwen35-9b/post-cleanup-receipt.json` records `remaining_gguf` as
  `Qwen3.5-9B-Q4_K_M.gguf` alone with `intermediates_absent: true`. No Q8_0 or
  bf16 file exists in the tree.
- `git diff --check main..HEAD`: no output, **exit 0**.
- `git diff --stat main..HEAD`: **Markdown files only**.
- Machine: development macOS host, Darwin arm64. No native build, no model, no
  network, no provider contact, and no target equivalence is claimed.
- Artifact/index: this packet, the six reconciled documents, and the one new
  development-evidence receipt held outside `artifacts/evidence-index/`.
  Metrics: none beyond the raw runner output above.

## Recorded decisions

- **Baseline.** Audited integrated source baseline is exact
  `main@263f11413d1746044a6cc13062ad2b1f821c4d11`;
  `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f`,
  `1b22a40e21dce0ebc903bc3c2024ab162adc2d56`, and this refresh are
  documentation descendants. Previous baseline `7239b7e` and the v4 snapshot
  are retained and retitled historical/superseded.
- **Two merged slices**, each independently S0/S4 source-reviewed with a
  first-pass `ACCEPT_FOR_MERGE` and NOTE/MINOR findings only, neither adding
  compile, live, provider, production, Windows, or target evidence:
  - *Descriptor WAL contract* (`9f136a0`, merge `237d59b`). Freezes
    `contracts/action-journal-descriptor-wal/v0.1.0`, the separate contract
    ADR-0004 requires for the v2 boundary, statically bound to the C++ and Node
    constants (48-byte header, 32 MiB) and to the derived v2 status set: 26 v2
    codes against 25 v1, with `v1 ∪ v2 ∪ {platform_unavailable}` equal to the
    30-code storage set. Sol accepted that corrected invariant because
    `platform_unavailable` is declared but emitted by no function body. Storage
    `v0.1.0.json` is byte-identical. R4 (dropping `DELETE` after publication) is
    closed as a documented, test-pinned accepted limitation: Win32 cannot narrow
    an open handle's access, and `ReOpenFile` with reduced access collides with
    the `FILE_SHARE_READ` reservation. ICR-RUN-WDJB-001 carries the dated note.
    No C++/Node/CMake/package change.
  - *Graph sent-mail proof projection* (`26beb82`, merge `263f114`). The defect
    is confirmed against the pristine base: `listSentForDigest` asked a Sent
    Items collection query for `internetMessageHeaders`, which Graph returns
    only on single-message projections, so `mail.send_draft` could never
    complete against a real account (a safe false negative) and a transport
    fault escalated to `unknown_manual`. Repaired by sharing the bounded
    per-message retrieval (`collectMailProof`: one folder ID page plus at most
    `MAX_MAIL_PROOF_CANDIDATES` = 20 exact GETs per call), refusing
    absent/malformed markers before any request, typed inconclusive results, a
    `mail.send_draft` proof deadline (`sent_proof_budget_exhausted`), and
    preserved cancellation. Behavior changes on merged paths were disclosed.
- **Task claims.** GOV-TRUTH-006 (`luna/governance-refresh-v4`) flipped to
  `MERGED_SOURCE_PENDING_GATE`, merged at
  `fdfed07692ae35713432e02a2c1d7f729267acb4`; GOV-TRUTH-007 added as
  `READY_FOR_REVIEW`. New `UNCLAIMED` rows: `GRAPH-SENT-PAGINATION-PRECHECK`
  (owner S3, dependency GRAPH-SENT-PROOF-PROJECTION) and
  `MODEL-QUALITY-CORPUS-001` (owner S2, dependency MODEL_DECISION quality gate
  / SOL-003). The `RUN-DESCRIPTOR-WAL-CONTRACT` row's unescaped `|` inside
  `` `GENERIC_READ | GENERIC_WRITE` `` is escaped as `\|` so the row renders as
  7 columns; nothing else in that row changed. Both integrator-flipped rows
  (`RUN-DESCRIPTOR-WAL-CONTRACT`, `GRAPH-SENT-PROOF-PROJECTION`) were verified
  correct in owner, state, tip, and merge SHA.
- **Model-testing truth.** The model has NOT been tested against the profile it
  must ship against. All recorded scores date from 2026-09-04 and were measured
  on retired fixtures on CUDA/A100, never CPU or Intel Vulkan. The current
  33-tool/37-case profile has no recorded score, so the model's score on the
  shipping profile is unknown, not merely below gate. The strongest general
  result is corrected from 27/34 to **28/34** — see the verification note below.
  The 13/32 production-profile failure statement is kept.
- **Deleted comparators.** Q8_0 and bf16 exist only as hashes, so the ≥95%
  quality-retention gate has no reachable reference artifact and any future
  quality run must budget a full Shadeform re-conversion, changing B-004
  candidate cost planning. Recorded in B-004, B-006, and MODEL_DECISION.
- **Quality corpus.** 13 categories, 1,180 minimum cases, 3 sample cases;
  authoring is unblocked (no model, no spend, no credential).
- **Local run 2026-09-11.** A Sol-authorized single development-only exception
  to the B-006 "do not load local bytes" workaround, granted because the local
  bytes were re-verified byte-exact against the pinned identity. ABORTED before
  any model load on two stop conditions: host memory (8 GiB Apple M2 MacBook
  Air, ~94% swap in use, `kern.memorystatus_vm_pressure_level` at WARNING
  before start, against a ~5.2 GiB resident requirement) and a stale prebuilt
  engine (`out/build-real/native/lae-engine`, built 2026-09-04, 532 commits
  behind `fdfed07` with 79 touching `native/`, rejecting current `serve` flags
  with `unknown argument`). No score, no bytes loaded, nothing bound or
  downloaded. Positive result: model identity re-verified exactly at
  5,629,109,088 bytes, SHA-256 `c654bc40…68873b`. The three `real_model` tests
  fail at argument parsing. A real measurement requires a rebuild from current
  source on a ≥16 GiB machine, i.e. Shadeform, once the human-only blockers
  clear.
- **Housekeeping.** 112 worktrees inventoried, 103 clean stale directories
  removed, no branch ref deleted (120 `luna/*` before and after),
  `git worktree prune` not yet run, 9 kept (2 active lanes, 6 holding ignored
  `experiments/runtime/` state, 1 holding a `.env`), 7 needing an operator
  decision. No secret was read or printed.

## Findings and changed assumptions

- **27/34 → 28/34, verified before changing.** The receipt
  `artifacts/qwen35-9b/remote-eval-20260904-j/eval-receipt.json` **is
  git-tracked** (`git ls-files` returns it) and records `metrics.passed = 28`,
  `metrics.case_count = 34`, `metrics.failed = 6`, `metrics.errors = 0`,
  `metrics.canary.tool_count = 11`, and `status: completed_with_failures` on an
  A100 profile. Because it is tracked and the total is 34, the condition Sol set
  is satisfied and the correction was applied. It was made in
  `governance/MODEL_DECISION.md` (both the current-truth section and the
  "strongest recorded" line), `coordination/BLOCKERS.md` (B-006),
  `coordination/STATUS.md`, `coordination/TASK_CLAIMS.md` (MODEL-006), and
  `coordination/status/S0.md`.
- **Residual, for Sol to direct.** Two other lanes' own packets still state the
  superseded figure: `coordination/status/S2.md:74` and
  `coordination/status/S4.md:113`. They were left untouched because they are
  those lanes' session records rather than program current-truth files, and
  they fall outside the file list for this task. S2 and S4 should correct them.
- Every other number above reproduces the previous interval's method exactly.
  The import-graph figures (29/67/0/0 from 72 occurrences) are unchanged from
  v4; the Node, native-static, JSON, and QA figures moved with the two merged
  slices and are recorded with their deltas.
- The `listSentForDigest` projection defect, recorded in v4 as "plausible", is
  now confirmed and repaired. Its residue is narrower and is tracked as
  `GRAPH-SENT-PAGINATION-PRECHECK` rather than left in a packet.
- Merged source remains distinct from evidence and from approval. Both slices
  are source-only; the descriptor-WAL boundary has still never been compiled and
  sits outside every product/package/activation graph.
- Release/full access remains `BLOCKED` / `NOT_READY`. All nine Phase gate state
  cells in `coordination/RELEASE_GATES.md` and all six blocker `State:` lines in
  `coordination/BLOCKERS.md` are byte-identical to `main`.
- The aborted-run receipt was scanned before copying: it contains no credential
  value, token, secret, or external URL, so nothing required redaction. Every
  long hexadecimal string in it is a content hash or a commit SHA. Its own body
  originally said "must not be committed"; that clause was reconciled in place
  with a parenthetical recording Sol's later direction, so the committed file
  does not contradict itself.
- Wall-clock durations appear only in this packet and are labelled
  non-reproducible. No `duration_ms` value appears in any current-truth file.

## Blockers

- No blocker prevents this docs-only reconciliation. Existing model, provider,
  native Windows compile/secure-owner, hardware-receipt, artifact-custody,
  credential-rotation, and production-journal gates remain binding and are
  recorded without advancement. The newly recorded model-testing facts make
  B-006 broader in scope, not closer to closure: its `State:` line is unchanged.

## Handoffs

- To: S0 and S4
- Handoff file: this packet plus the final branch tip
- Required by: governance review and Sol's merge decision
- Acknowledged: pending

## Next bounded action

Independent S0/S4 docs and evidence-scope audit, followed by Sol's merge
decision. Separately, S2/S4 should correct the superseded 27/34 figure in their
own packets, and Sol owes an operator decision on the 7 retained worktrees and a
scoping decision on the re-conversion cost implied by the deleted comparators.
No live, model, provider, native, browser, or target run follows from this task.

## Sol action requested

Review and merge; no gate change. Only Sol may merge or alter a phase/release
gate, and nothing in this refresh requests either.
