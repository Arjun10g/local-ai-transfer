# Status Packet — Governance Refresh v6

- **Session:** Luna docs
- **Required model:** GPT-5.6 Luna
- **Role:** Evidence-bound governance/status reconciler; no integration or gate authority
- **Timestamp (UTC):** 2026-09-11T20:20:00Z
- **Branch/worktree:** `luna/governance-refresh-v6` / `wt-governance-refresh-v6`
- **Current phase:** Source-hardening governance reconciliation; formal gates unchanged
- **Primary task ID:** GOV-TRUTH-008
- **Secondary task ID, if any:** none
- **Task state:** READY_FOR_REVIEW (Phase A complete; Phase B pending Sol's run results)
- **Last merged `main` commit:** `1e341e9145394f4ece2005058fe80f0a1270c470`
- **Audited integrated source baseline:** exact
  `main@f89c1684ea051e1c9c92f944cb080d424a086f55` (the last source merge)

## Objective for this work interval

Reconcile the program's current-truth governance, status, task, blocker, policy
and release-gate records to `main` after a large integration interval, and
record the Sol decisions and factual corrections established on 2026-09-11.
This is a docs-only interval; it advances no gate.

The interval is deliberately split. **Phase A (this packet)** covers everything
independent of the live evaluation run. **Phase B** folds in the results of
`remote-eval-20260911-b` once Sol relays them. Every current-truth file carries
an explicit PENDING marker for that run so no reader can mistake its absence
for a negative result.

## Base and scope

- Branch base: `1e341e9145394f4ece2005058fe80f0a1270c470`, the current `main`
  tip, taken directly as the worktree base. No merge was required and no
  conflict arose.
- Baseline convention: the audited integrated source baseline is the last
  source merge `f89c1684ea051e1c9c92f944cb080d424a086f55`. The docs descendants
  `1e341e9`, `aa1087a`, `25a0d95`, `74a43d0`, `335211f` and `1b22a40` (claims
  flips) and this refresh are documentation descendants, not self-referential
  source hashes. The previous baseline `263f114` and the v5 snapshots are
  retained and retitled historical/superseded.
- Files changed in Phase A: `coordination/STATUS.md`,
  `coordination/RELEASE_GATES.md`, `coordination/BLOCKERS.md`,
  `coordination/TASK_CLAIMS.md`, `coordination/POLICY_STATUS.md`,
  `coordination/DECISIONS.md`, `governance/MODEL_DECISION.md`,
  `governance/SECURITY_AND_TOOL_POLICY.md`, `coordination/status/S0.md`, the
  new `coordination/adrs/ADR-0005-spend-authorization-and-caps.md`, and this
  packet. Markdown only; no source, test, contract JSON, configuration, script,
  or `.gitignore` change. `execution/` was **not** touched: the one factual
  correction that might have lived there (M6b) turned out to live in
  `governance/SECURITY_AND_TOOL_POLICY.md`.
- Not changed, deliberately: no phase gate state cell, no blocker `State:`
  line, and no release/full-access state. All remain `BLOCKED` / `NOT_READY`.

## Inputs and dependencies

- Contract/version: no interface change is made here. The
  `max_output_tokens` ruling explicitly defers the engine-api reconciliation to
  a future version bump through `coordination/INTERFACE_CHANGE_REQUESTS.md`
  rather than editing a contract in this refresh.
- Required commits, all verified with `git rev-parse --verify`: `9a8519c` /
  `c4c0c82` (comparator evaluation phase), `943d013` / `997b3f1` (comparator
  engine transport), `0cea3ec` / `79a8888` (model quality corpus), `6c475d4`
  and `b11e617` / `f89c168` (salvage transport and key handle), `3605192`
  (GOV-TRUTH-007 merge), plus the docs descendants above and the historical
  references `2d7db4f`, `991b70e`, `8e3f599`, `263f114`.
- Model/build/profile IDs: fixed Qwen3.5-9B Q4_K_M decision; shipping
  evaluation fixture `tests/model/production_tool_call_eval.json` at 33 tools /
  37 cases, SHA-256
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`. No
  artifact, runtime, model, provider, or network was used by this refresh.
- Handoffs consumed: the five merged slice packets
  (`S2-j1m-comparator-eval-v1.md`, `S2-comparator-engine-v1.md`,
  `S2-model-quality-corpus-v1.md`, `S4-j1m-salvage-transport-v1.md`,
  `S4-j1m-key-handle-v1.md`), the five independent review reports, the
  Shadeform fast-path plan, the stopped first-attempt run summary, the worktree
  final cleanup report, and Sol's decisions for this refresh.

## Commands run

All from the worktree root, and all offline:

- `python3 -m unittest discover -s tests -t . -p 'test_*.py'`
- `python3 -m unittest discover -s tests -p 'test_*.py'` (the negative control
  for the `-t .` fact below)
- `npm test`
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`
- `python3 scripts/j1m_dry_run.py` (offline; no network, no provider call, no
  spend)
- `python3 scripts/model-artifact/validate_quality_corpus.py`, and again with
  `--require-complete`
- Strict tracked-JSON inventory: every `git ls-files '*.json'` parsed with a
  duplicate-key-rejecting `object_pairs_hook`
- Host import graph over `lae-host.mjs` plus `host/**/*.mjs`, by the v4/v5
  method
- `git worktree list`, `git for-each-ref refs/heads`, `git log --first-parent`
- `git diff --stat`, `git diff --check`, and `git rev-parse --verify` on every
  SHA written into these documents

No provider, network, orchestrator, lifecycle, ssh, or model command was run,
and nothing under `main`'s `.secrets/`, `artifacts/`, `experiments/` or `out/`
was read or modified.

## Evidence

Exact numbers, reproduced in this worktree on 2026-09-11:

- `python3 -m unittest discover -s tests -t . -p 'test_*.py'`: **`Ran 743
  tests` … OK**.
- **Invocation fact, recorded because it is load-bearing.** The `-t .` root
  argument is not optional. Without it, `tests/qa/` shadows the root `qa/`
  package and discovery collapses to **`Ran 646 tests`** with **8 module-level
  `ERROR`s**: `model.test_windows_artifact_handoff`,
  `performance.test_j1m_dry_run.DryRunRegistrationTests`, `qa.test_evidence`,
  `qa.test_safe_runner`, `release.test_package_scanner`,
  `release.test_windows_acceptance`,
  `release.test_windows_hardware_receipt_diagnostic`, and
  `security.test_adversarial`. Any future evidence claim over the Python suite
  must state the invocation, because the two forms differ by 97 tests and 8
  silently unloaded modules.
- `npm test`: **432 tests, 430 pass, 0 fail, 0 cancelled, 1 skipped, 1 todo**.
  The single skip and single todo are the pre-existing filesystem
  `KNOWN LIMITATION` pair. The wall clock for this single local run is a
  machine-specific observation, not reproducible evidence, which is why no
  duration value is carried into any current-truth file.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`: overall
  **`BLOCKED`**, **70 discovered, 0 missing, 0 unknown**, and **76 records
  (1 PASS / 75 expected SKIP)**. The brief anticipated 76 records with a
  question mark; 76 is confirmed, and the split is 1/75.
- `python3 scripts/j1m_dry_run.py`: **PASS**, **142 argv recorded (none
  executed), 0 refused**, network none, provider calls none, spend **USD 0.00**,
  **0 WARN**, 18 checks.
- **Second invocation fact, and the one genuine surprise of this interval.**
  On a freshly created worktree the gate does **not** pass. It **FAILS**
  `operator_artifact_destination_is_salvage_ready` with
  `artifact destination is not privately publishable (private ancestor is
  unsafe)`, naming its own remedy, and that single failure also fails **3 of
  the 743** Python tests
  (`tests.performance.test_j1m_dry_run.DryRunKeyRootResidueTests`). The cause
  is not a defect: `git worktree add` creates `artifacts/` at the umask
  default, Git does not record directory modes, and the merged key-handle slice
  requires the artifact destination and its ancestors to be owner-private
  before anything is billable. Applying the documented remedy —
  `chmod 700 artifacts artifacts/qwen35-9b` — turns the gate PASS and the suite
  OK. Both states were observed here, in that order. This is an operator
  precondition, and it is now recorded in BLOCKERS, TASK_CLAIMS, RELEASE_GATES
  and ADR-0005 so the next lane does not rediscover it by failing.
- `python3 scripts/model-artifact/validate_quality_corpus.py`: **PASS**, exit
  0, **1,336 cases** against a **1,180** minimum and a **1,298** target across
  **13 categories**. Identical PASS with `--require-complete`, which
  additionally enforces the split and the difficulty mix. Per-category counts:
  tool_selection_no_tool 220, tool_arguments 177, instruction 110,
  policy_safety 110, continuity_reset 92, thinking_control 85,
  long_short_integrity 80, extraction 88, file_task_planning 88, tool_recovery
  88, summarization 66, reasoning 66, code_command 66.
- Splits, summed by column from the validator's own table: **271 train / 250
  dev / 815 test**, totalling 1,336. This matches the hash-derived
  (`sha256(id) % 100`) figures recorded by the corpus lane exactly.
- Strict tracked-JSON inventory: **168 tracked JSON files**, of which **167**
  are strict-valid under a duplicate-key-rejecting parser and **1** is the
  intentional hostile fixture
  `tests/native/fixtures/windows_broker/duplicate-key.json` (duplicate key
  `deadline_ms`).
- Host import graph over `lae-host.mjs` plus every tracked `host/**/*.mjs`:
  **29 modules, 67 unique relative import edges, 0 unresolved specifiers, 0
  cycles**, from **72** relative import occurrences. Counting method identical
  to the v4/v5 packets: static `import`/`export … from` plus dynamic
  `import()`, resolved against the importing file's directory, each distinct
  importer/target pair counted once — five pairs appear twice in source, which
  is the whole difference between 72 and 67. Unchanged from v5.
- `git diff --check`: no output, **exit 0**. `git diff --stat`: **Markdown
  only**.
- `git rev-parse --verify` resolved every SHA written into these documents.
- Machine: development macOS host, Darwin arm64, Python 3.14.6. No native
  build, no model, no network, no provider contact, and no target equivalence
  is claimed.

## Recorded decisions

- **Baseline.** Audited integrated source baseline is exact
  `main@f89c1684ea051e1c9c92f944cb080d424a086f55`. The docs descendants
  `1e341e9`, `aa1087a`, `25a0d95`, `74a43d0`, `335211f`, `1b22a40` and this
  refresh are documentation descendants. Baseline `263f114` and the v5
  snapshots are retained and retitled historical/superseded.
- **Four merged slices**, verified in merge order on `main`'s first-parent
  chain (`c4c0c82` → `997b3f1` → `79a8888` → `f89c168`):
  - *Comparator evaluation phase* (`9a8519c`, merge `c4c0c82`). Default-OFF
    `--evaluate-comparators`; paired bootstrap with an unpaired stratified
    fallback; `comparator-eval-receipt.v1`. First-pass `ACCEPT_FOR_MERGE` with
    4 MINOR and 3 NIT. Default-OFF equivalence proved: with no selection the
    plans for every mode are byte-identical to the plans with the flag absent.
  - *Comparator engine transport* (`943d013`, merge `997b3f1`). Arms run on the
    pinned upstream `llama-server`, which is the specification's
    `runtime_oracle`, rather than a second product engine — the substitution
    Sol confirmed. Adds `--transport upstream-openai`; closed selection
    vocabulary `'' | q4-oracle | q8 | q8,bf16`; per-arm bearer via
    `--api-key-file`, minted `O_EXCL` at 0600 and present in no argv, log or
    receipt; `_COMPARATOR_ENGINE_AVAILABLE` flipped to `True`. First-pass
    `ACCEPT_FOR_MERGE` with 2 MINOR and 3 NIT, and it closed the prior slice's
    four MINORs and two NITs — one cross-slice repair round. `native/` is
    byte-unchanged, so the compiled Q4 identity is untouched.
  - *Model quality corpus* (`0cea3ec`, merge `79a8888`). 1,336 cases, 13
    categories, hash-derived splits 271/250/815, six author lanes merged
    conflict-free with each lane touching only its own category files, 103
    tests. Every scoring proposition carries an inline deterministic `match`
    object — 271 fact items (207 `key_facts` plus 64 `forbidden_facts`) and 284
    rubric items, **555** in total — so pass/fail is computed by string and
    regex evaluation with no model, judge or human rater in the path. First
    pass `ACCEPT_WITH_REQUIRED_FIXES`; final `ACCEPT_FOR_MERGE` after repair.
  - *Salvage transport and key handle* (`6c475d4` and `b11e617`, merge
    `f89c168`). Bounded, allowlisted, identity-bound receipt salvage; key at
    `.secrets/j1m/<run-id>-<nonce>/ssh-key`; validators byte-unchanged; offline
    dry-run gate added; build, prove and eval now fail when a required receipt
    is missing; comparator receipts salvageable and identity-bound. Salvage
    first pass `ACCEPT_WITH_REQUIRED_FIXES` (2 MAJOR, 3 MINOR, 3 NIT) with the
    repairs landing on the key-handle branch, which was itself
    `ACCEPT_FOR_MERGE` on its first pass.
- **Spend (ADR-0005, new).** Standing user authorization of 2026-09-11;
  program hard cap USD 50; per-run caps recorded per run
  (`remote-eval-20260911-b`: USD 10.00 / 4 h); lifecycle rules unchanged.
  Status Accepted, owner S0 Sol, recorded with the standard provenance line —
  decision made by S0 Sol and relayed for recording, Sol's merge is the
  ratifying act, bookkeeping is not gate approval. Added to `DECISIONS.md` and
  referenced from B-004 and `MODEL_DECISION.md`.
- **`max_output_tokens`.** The enforced engine contract bound is authoritative
  for the MVP; `MODEL_DECISION.md`'s larger budgets are aspirational and
  require an engine-api version bump. See the dedicated finding below.
- **Tool policy §10.** The shipping catalogue is authoritative; §10 corrected.
- **Corpus design decisions.** Sol accepted the 50% `no_call` share, with the
  trade-off recorded: a larger no-call denominator sharpens the false-positive
  rate and shrinks the positive-selection sample. Guide id-contiguity is
  relaxed to unique, strictly increasing, never reused or renumbered, with gaps
  permitted and expected — a necessary consequence of hash-derived splits,
  since an author must be free to skip an ordinal whose bucket would push a
  category outside its band.
- **B-004 canary deviation.** Sol accepted running the built-in probes on the
  A100 itself as the activation canary, an explicit deviation from B-004's
  "non-A100" wording, because the prescribed canary is unexecutable by the
  code. B-004's `State:` is unchanged.
- **Orphan receipt.** Sol adjudicated `j1m-loopback-no-orphan` /
  `instance-loopback-1` (`actual_cost_usd` 7.1e-05) as a non-billable loopback
  test artifact. No tooling command exists to apply this, so it stands as a
  recorded decision only.
- **Task claims.** `GOV-TRUTH-007` flipped to `MERGED_SOURCE_PENDING_GATE`,
  merged at `3605192`; `GOV-TRUTH-008` added `READY_FOR_REVIEW`; two new
  `UNCLAIMED` follow-ups, `COMPARATOR-BF16-ARM-001` (S2) and
  `LEDGER-GENESIS-001` (S4). `POLICY-DOC-PROCESS-TOOL-001` was **not** added,
  because its precondition did not hold: M6(a) was fixed in this refresh.

## Findings and changed assumptions

### The `max_output_tokens` finding, investigated end to end

Three recorded values disagreed, and the disagreement had never been resolved
because no one had stated which one the running system actually applies. Read
in source, all three, here is the answer.

- **Enforced today: `1..256`, and only this.**
  `native/server/chat_request.cpp:269` rejects any request outside
  `1 <= max_tokens <= 256` with `ParseFailure("max_tokens out of range")`, and
  `:271` defaults the field to **8** when it is absent. This is the sole
  runtime rejection path, so it is the only bound with teeth.
- **`contracts/engine-api` declares 64, and nothing enforces it.**
  `contract.json:4` carries `"max_tokens": 64` and `README.md:47` repeats "an
  integer from 1 through 64". The only consumer is `qa/conformance/runner.py`,
  which validates the contract document's *shape* against
  `engine-api.schema.json`; the schema requires the key to exist and does not
  constrain the engine. The declared 64 is therefore not merely unenforced but
  **narrower than, and out of step with, the engine that ships** — a contract
  understating its own implementation.
- **`MODEL_DECISION.md` describes 1,024 and 2,048, and they are unreachable.**
  `:357-358` give a UI output default of 1,024 tokens and a hard answer cap of
  2,048. Note these are two distinct quantities, not a range. A request
  carrying either is refused at parse time by the engine, not clamped.
- **The corpus and the lanes already bind to 256**, the value the shipping
  fixture uses: `tests/model/production_tool_call_eval.json` sets
  `max_output_tokens: 256` with `context_tokens: 8192`, and both
  `scripts/j1m_orchestrator.py:500` and
  `scripts/test/evaluate_tool_calls.py:335` independently enforce `1..256`.

**Sol's ruling, recorded:** the enforced engine contract bound is authoritative
for the MVP. `MODEL_DECISION.md`'s larger deep-mode budgets are aspirational
and are marked as such in place, with an explicit note that reaching them
requires an engine-api version bump through
`coordination/INTERFACE_CHANGE_REQUESTS.md` — not a documentation edit. The
`contracts/engine-api` 64 is flagged for reconciliation by that same bump. No
contract was edited in this docs-only refresh.

### Stale statements found, listed before they were changed

Per the M6(b) instruction, every stale statement the reports or source
contradict is listed here first. Each was then corrected in place with the
correction marked, rather than silently overwritten.

1. **"`REMOTE_EXECUTION_ENABLED=False` remains binding" on the eval lane**
   (B-004 source/evidence correction; `POLICY_STATUS.md`; `STATUS.md`). A
   conflation of two lanes. The flag exists only in
   `scripts/shadeform/remote_external_tools.py` and gates only the
   hostile-tools QA lane. **Corrected.**
2. **"an unrotated Hugging Face credential" listed among the blockers to a
   Shadeform measurement** (`STATUS.md` v5). The eval lane uses the public
   pinned revision with no token. **Corrected, with SI-002 preserved for
   authenticated use.**
3. **"any future quality run must budget a full Shadeform re-conversion"**
   (B-004, B-006, `MODEL_DECISION.md`, `RELEASE_GATES.md` Phase 2). The
   comparators are rebuilt every run. **Withdrawn and replaced with the
   marginal cost.**
4. **"the ≥95% retention comparator is unreachable"** (`RELEASE_GATES.md`
   Phase 2). It is reachable but unmeasured. **Corrected.**
5. **"the quality corpus does not exist yet" / "is not authored"** (B-006,
   `MODEL_DECISION.md`, `STATUS.md`). It is merged and complete. **Corrected.**
6. **`SECURITY_AND_TOOL_POLICY.md` §10's `process.run_allowlisted` schema.**
   No shipping component accepts it. **Corrected**, together with the
   dependent PowerShell subcommand-policy text, which described constraining a
   path the implementation refuses outright.
7. **`SAFE_TO_MIGRATE_NOW` written as an uppercase constant** (B-004, B-006,
   `STATUS.md`, `MODEL_DECISION.md`, several lane packets). There is no such
   source identifier. The source referent is the lowercase literal
   `"safe_to_migrate_now": False` at
   `scripts/shadeform_ledger_migration_preflight.py:1110`, and it is hardcoded
   rather than computed. **Corrected where it is load-bearing**, with the
   spelling issue recorded; the lane packets are historical and were left.
8. **"`_EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE` is a constant `False` with no
   consumer"** and **"`_salvage` still raises"** (the stopped run's summary,
   and the comparator-engine review, both true when written). At this baseline
   the constant is `True` (`scripts/j1m_orchestrator.py:63`) and the raise is a
   dormant guard. This is purely a sequencing artefact: the comparator-engine
   review was written against `943d013`, before the salvage slice merged at
   `f89c168`. **Recorded as superseded.**
9. **B-004's prescribed non-A100 canary.** Unexecutable by the code.
   **Recorded as a deviation**, with the workaround text left standing and
   superseded rather than rewritten.

### Corrections to premises supplied for this refresh

Four premises in the task framing did not survive checking. They are recorded
because a current-truth document that repeated them would be wrong.

- **"all 12x `luna/*` branch refs retained".** The cleanup report retained
  **10** branch refs, one per removed worktree, and deleted none; the
  repository-wide count at cleanup time moved 121 → 122 because another lane
  created a branch mid-run. At this baseline `git for-each-ref` reports **134**
  `luna/*` refs and **135** heads in total, `main` being the only non-`luna`
  head. All three figures are recorded rather than collapsed.
- **"Active lane worktrees created today are listed by `git worktree list`
  (record the names)".** There are none. `git worktree list` shows exactly two
  entries: the `main` checkout and this refresh worktree. The one unauthorized
  worktree the cleanup report had left in place for a human call has since been
  removed. **Nothing remains for Sol to remove after this refresh.**
- **"archived to `archive/worktree-runtime-state-20260911/`".** Correct as a
  path, but it sits **beside** the repository, not inside it, and is therefore
  untracked by design. It holds 7 tarballs, `SHA256SUMS` and `MANIFEST.md` —
  note the `.md` extension — at directory mode 0700, with checksums verified on
  all 8 entries (the eighth being the moved `.env`). Three of the 10 worktrees
  had only build output and needed no tarball.
- **"the `991b70e` key operand" defect.** `991b70e` did not introduce a defect.
  It is the commit that *tightened* `validate_persisted_argv` to require a
  canonical private handle, as `scripts/shadeform_lifecycle.py:58-68` records
  in prose. The latent refusal followed from the key's *location and basename*
  not satisfying that rule. The distinction matters because the fix was made at
  the operand and the validator was deliberately left byte-unchanged, with
  tests pinning that the old layout is still refused.

### Other findings

- **`run_qa.py` records: 76, split 1 PASS / 75 SKIP.** The brief carried this
  with a question mark; it is confirmed, along with 70 discovered and 0
  missing/unknown.
- **The 0.2% corpus residual must not be over-claimed.** It counts three cases
  and is safe **only in the under-crediting direction**: it can reject a
  correct answer, never admit a violation. A second residual — stemmed
  conjunction matchers blind to negation — errs the *opposite* way and is
  deliberately excluded from the 0.2%. The corpus is therefore conservative on
  the counted residual and permissive on one uncounted, judged-pathological
  case. Both must close before the corpus produces a retention or parity
  number. This is recorded in `STATUS.md`, B-006 and `MODEL_DECISION.md` in
  that exact shape.
- **Post-run verification is exact-instance, not account-wide.** The lifecycle
  module states at `:6-9` that "no account-wide instance-list operation exists
  in this module" and that a run may act only on the exact resource ID in its
  phase ledger. This is a deliberate ownership property, not a missing control,
  and ADR-0005 records it that way so no future document claims a zero-instance
  sweep that cannot exist.
- **The program cap lands on a control that already fails closed.**
  `scripts/shadeform_lifecycle.py:18-21` records that the whole-project cost
  control is applied by candidate selection, which subtracts recorded ledger
  spend and refuses to launch while any prior row's cost is unaccounted.
- **No `duration_ms` value appears in any current-truth file.** The three
  occurrences added by this refresh are statements of the prohibition itself,
  not measurements.

## Blockers

- No blocker prevents this docs-only reconciliation. Existing model, provider,
  native Windows compile and secure-owner, hardware-receipt, artifact-custody,
  credential-rotation, and production-journal gates remain binding and are
  recorded without advancement.
- The corrections recorded here make B-004 and B-006 **more precisely scoped,
  not closer to closure.** Removing two false blockers from B-006 — the
  `REMOTE_EXECUTION_ENABLED` conflation and the Hugging Face rotation claim —
  does not close it, because what actually blocks B-006 is the absence of a
  score. Both `State:` lines are unchanged.
- New follow-ups recorded as `UNCLAIMED`, neither gate-advancing:
  `COMPARATOR-BF16-ARM-001` and `LEDGER-GENESIS-001`.
- Open items carried from the merged reviews: the dry-run pre-launch gate is
  stateful and stale key-root residue makes it fail while its own detail line
  reports removal, which the reviewer recommends fixing before the first live
  run; the comparator cleanup tail is skipped when a run fails before the
  comparator phase; a `q4-oracle` refusal records an untyped skip; and the two
  corpus residuals above.

## Handoffs

- To: S0 and S4
- Handoff file: this packet plus the final branch tip
- Required by: governance review and Sol's merge decision
- Acknowledged: pending

## Next bounded action

**Phase B, on Sol's message only.** Fold the `remote-eval-20260911-b` results
into the PENDING markers in `STATUS.md`, `RELEASE_GATES.md`, `BLOCKERS.md`,
`coordination/status/S0.md` and this packet; record the actual cost against the
recorded USD 10.00 / 4 h caps and against the ADR-0005 program cap; record the
receipts and any score as **evidence**, leaving every gate state to a separate
Sol gate decision. No live, model, provider, native, browser, or target run
follows from this task, and this session will launch nothing.

## Sol action requested

Review and merge; no gate change is requested and none is implied. Two items
need a Sol decision beyond the merge: whether `contracts/engine-api`'s declared
`max_tokens: 64` should be reconciled to the engine's enforced `1..256` by an
interface change request now or deferred with the deep-mode budgets, and who
owns `LEDGER-GENESIS-001`, whose central obstacle — the hardcoded
`"safe_to_migrate_now": False` — requires its own ADR rather than a code edit.
Only Sol may merge or alter a phase or release gate.
