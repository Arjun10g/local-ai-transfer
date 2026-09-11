# Status Packet — Governance Refresh v6

- **Session:** Luna docs
- **Required model:** GPT-5.6 Luna
- **Role:** Evidence-bound governance/status reconciler; no integration or gate authority
- **Timestamp (UTC):** 2026-09-11T20:20:00Z
- **Branch/worktree:** `luna/governance-refresh-v6` / `wt-governance-refresh-v6`
- **Current phase:** Source-hardening governance reconciliation; formal gates unchanged
- **Primary task ID:** GOV-TRUTH-008
- **Secondary task ID, if any:** none
- **Task state:** READY_FOR_REVIEW (Phase A and Phase B complete; the run's
  numbers will be a separate addendum when the run executes)
- **Last merged `main` commit:** `1e341e9145394f4ece2005058fe80f0a1270c470`
- **Audited integrated source baseline:** exact
  `main@f89c1684ea051e1c9c92f944cb080d424a086f55` (the last source merge)

## Objective for this work interval

Reconcile the program's current-truth governance, status, task, blocker, policy
and release-gate records to `main` after a large integration interval, and
record the Sol decisions and factual corrections established on 2026-09-11.
This is a docs-only interval; it advances no gate.

The interval was worked in two phases. **Phase A** covered everything
independent of the evaluation run. **Phase B** was expected to fold in that
run's results; instead it records why there are none. The run has **not
executed**, so every current-truth file carries an explicit
`PENDING — launch-ready, awaiting operator permission` marker rather than a
result, and no reader can mistake its absence for a negative result. The run's
numbers will be a separate addendum when they exist.

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
- Files changed across Phase A and Phase B: `coordination/STATUS.md`,
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
- Phase B, read-only verification only, no contents of `.secrets/*` read or
  printed: `shasum -a 256` and `wc -l` over the archived legacy ledger,
  `ls -la` metadata (size and mode) over the new ledger, the legacy directory,
  the archive copy, `.secrets/j1m/` and the run-b destination, and source reads
  of `prepare_artifact_destination`, `MIN_BACKSTOP_MARGIN`, the cost-event
  schema constant and canonicalization refusal, and the genesis script's
  argparse surface
- `git diff --stat`, `git diff --check`, and `git rev-parse --verify` on every
  SHA written into these documents

No provider, network, orchestrator, lifecycle, ssh, model, or ledger-mutating
command was run, and nothing under `main` was modified.

Stated precisely, because Phase A and Phase B differ here. During Phase A,
while a launch was believed to be in flight, nothing under `main`'s
`.secrets/`, `artifacts/`, `experiments/` or `out/` was read at all. During
Phase B, verifying the genesis and launch state required **read-only metadata**
from three of those: `shasum -a 256` and `wc -l` over the archived legacy
ledger, `ls` size and mode over the new ledger and the legacy directory, an
`ls -A` listing of `.secrets/j1m/` to confirm it is empty, and an `ls` of the
run-b destination to confirm it exists at 0700 and is empty. **No file content
under `.secrets/` was read, printed, or reproduced anywhere**, no ledger row was
printed, and nothing was written, moved, or deleted. The one local mutation
this session made at any point was `chmod 700` on its **own** worktree's
`artifacts` and `artifacts/qwen35-9b`, the documented operator precondition for
the dry-run gate; it is invisible to git, and `main`'s tree was never touched.

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
  In Phase B, `LEDGER-GENESIS-001` moved to `DONE_OPERATOR_ACTION` — a state
  defined in this refresh's claims vocabulary for operator/Sol action completed
  outside source, explicitly not a gate approval — and three further `UNCLAIMED`
  rows were added: `J1M-CLI-RELATIVE-DESTINATION-001`,
  `J1M-DRYRUN-REAL-ENV-BACKSTOP-001` and `J1M-DRYRUN-LEDGER-VALIDITY-001`, all
  S4.

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
  `:497-498` in the delivered tree — `:357-358` on `main`, since this same
  commit inserts lines above them — give a UI output default of 1,024 tokens and
  a hard answer cap of 2,048. Note these are two distinct quantities, not a range. A request
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

## Phase B — the run did not execute

Phase B was scoped to fold in the results of `remote-eval-20260911-b`. There
are none. What follows is the record of why, verified read-only in the tree.

Process note, for accuracy: the three causes below were found one per launch
attempt — `commands.txt:32-37` shows three sequential relaunches, each preceded
by `gates ok` and each ending `orchestrator_exit=2` — with Sol isolating the
failing gate after each. That is a slightly less tidy process than "stepping the
gates in isolation" suggests, and the substance is unaffected: three refusals,
three causes, all pre-spend at USD 0.00.

### Launch state

`remote-eval-20260911-b` is **`PENDING — launch-ready, awaiting operator
permission`**. It passed its pre-spend gates and was then blocked at the tool
layer by the Claude Code auto-mode permission classifier. That is a **harness
control, not a project gate**: nothing in this repository refused it, no
blocker fired, and no policy stopped it. The lane declined to re-shape, wrap,
or route around the denial, which is the correct response to a control whose
purpose is to hold a billable cloud-provisioning command for a human. **A human
approval is required.**

Verified state, all read-only, no contents of `.secrets/*` printed or
inspected:

| Check | Result |
|---|---|
| Spend | **USD 0.00** |
| Instance created | **none**; no teardown applicable |
| `.secrets/j1m/` | **empty** — zero residue, key directory destroyed |
| `artifacts/qwen35-9b/remote-eval-20260911-b/` | exists, mode **0700**, **0 entries** |
| Score / oracle delta / retention ratio | **none** |
| Untracked residue in the `main` checkout | one path, `artifacts/qwen35-9b/remote-eval-20260911-a/` (a 5,430-byte `plan-eval.json`, no secret-shaped content), at directory mode **0755** against run-b's 0700 — disclosed in run-b's own git-status row; the operator may want the mode aligned |
| Remote hash re-verification of the Q4 identity | **none** — recorded identity figures remain static file inspection |

### Three pre-spend refusals, traced by stepping the gates in isolation

All three returned typed `input_rejected` before any provider call, at USD 0.00.

1. **Staging — a relative `--artifact-destination`.** Verified:
   `prepare_artifact_destination` (`scripts/j1m_orchestrator.py:408`) computes
   `Path(destination).relative_to(j1m_runner.PRIVATE_OUTPUT_ROOT)`, and since
   that root is the absolute repository root a relative operand raises
   `ValueError`, surfaced as
   `artifact destination must live under the trusted output root`.
   **This is not a code defect.** Refusing an ambiguous destination before
   anything is billable is correct and must not be loosened. The recorded launch command **has since
   been corrected** by Sol: run-b summary §5 and `commands.txt:24` now read
   `--artifact-destination "$PWD/artifacts/qwen35-9b/remote-eval-20260911-b"`,
   corroborated by the absolute `dest=` in `commands.txt:34` and by the summary's
   new §7 "Sol post-staging corrections". Re-read after that correction; an
   earlier draft of this packet quoted the superseded relative form. The open
   defect is therefore **not** that the recorded command still carries a relative
   path, but that the CLI accepts one at all: ergonomic follow-up
   `J1M-CLI-RELATIVE-DESTINATION-001` (UNCLAIMED, S4), whose premise is the
   original staging error, not the current file.
2. **Provider backstop.** `.secrets/shadeform.env` carried
   `SHADEFORM_AUTO_TERMINATE_HOURS=2` against a 1.94 h eval plan. Verified:
   `scripts/shadeform_lifecycle.py:190` sets `MIN_BACKSTOP_MARGIN = 1.10` and
   `:3374` refuses when `hours < runtime_hours * MIN_BACKSTOP_MARGIN`; 2 / 1.94
   is 1.03x, below 1.10x, raising `BackstopError` (`:3379-3386`). Sol raised the
   ceiling to 3, which the code itself frames as the right kind of act —
   `:3384` calls it "a standing safety limit, so this is a decision, not a knob
   to turn to make a run fit". Worst case becomes 3 h × USD 1.35 = **USD 4.05**,
   inside the USD 10.00 run cap. **Only key names and that non-secret numeric ceiling
   are recorded anywhere in this refresh; no credential value from that file was
   read, printed, or reproduced.**
3. **Legacy cost ledger.** Verified: `sf.list_candidates` validates
   `experiments/runtime/cost-ledger.jsonl` against
   `local_bmo.shadeform.cost-event.v2` (`scripts/shadeform_lifecycle.py:80`),
   and the 108-line 2026-09-04 ledger failed canonicalization at line 1 with
   `stored cost event is not canonical` (`:1989`). **This blocked every run.**
   It is the concrete, reproducible form of the abstract `SAFE_TO_MIGRATE_NOW`
   / `legacy_schema_or_owner_binding_missing` condition that governance had
   been carrying without a mechanism — and it was discoverable offline, at zero
   cost, at any point.

### Reviewed cost-ledger genesis, executed

`LEDGER-GENESIS-001`, which Phase A had recorded as `UNCLAIMED`, was executed
by Sol the same day and is now `DONE_OPERATOR_ACTION` — a state **defined in
this refresh** in the claims-file vocabulary paragraph, for an operator or Sol
action completed outside source that no commit can carry, and which is
explicitly not a gate approval.

The legacy ledger — sha256
`756daa504fc9a1af40f32ce4af777935fb0bcdd00b01a1ad8688ab2c02f4c692`, 108 lines
holding **43 settled rows summing USD 6.767912** plus **65 pending rows across
22 pending-only owners** whose instances already hold deletion receipts, which
Sol adjudicated as historical stale estimates and non-billable — was moved to
`experiments/runtime/legacy/cost-ledger.legacy-20260904.jsonl` at 0600 inside a
0700 directory, and copied to
`archive/worktree-runtime-state-20260911/main-cost-ledger.legacy-20260904.jsonl`
with `SHA256SUMS.ledger`. `scripts/shadeform/initialize_cost_ledger.py` then
ran with `--program local-bmo-shadeform --currency USD --budget-cap-usd 50
--prior-settled-spend-usd 6.767912 --current-pending-owner-count 0`, the two
required evidence digests, and the reviewed-genesis confirmation, returning
**`status: created`**.

Reproduced read-only in this refresh, without printing any ledger content:

- the archived legacy file hashes to **exactly**
  `756daa504fc9a1af40f32ce4af777935fb0bcdd00b01a1ad8688ab2c02f4c692` at **108
  lines**;
- the new `experiments/runtime/cost-ledger.jsonl` is **438 bytes, 1 line** —
  the sole genesis event — at mode **0600**;
- `experiments/runtime/legacy/` is mode **0700** and its ledger copy is
  **0600**;
- all eight `initialize_cost_ledger.py` flags used exist in its argparse
  surface (`scripts/shadeform/initialize_cost_ledger.py:24-31`).

**Residual, explicitly not closed.** The migration preflight's hardcoded
literal `"safe_to_migrate_now": False`
(`scripts/shadeform_ledger_migration_preflight.py:1110`) is unchanged in
source, and the incidents-schema findings stand. **A genesis does not
retroactively validate the legacy rows**; it archives them and starts a
canonical ledger beside them. Revisiting the literal still requires its own
ADR. This is recorded in B-004, `MODEL_DECISION.md`, `STATUS.md`, `S0.md`,
ADR-0005 and the claims row, in that same shape, so the executed action cannot
be misread as closing the source defect.

### Gates that passed after genesis

`list_candidates` (read-only) returned **9 candidates**, exactly **one**
matching the pinned target — hyperstack / montreal-canada-2 / A100_80G /
USD 1.35 per hour.

No ephemeral key was ever minted by any launch attempt: all three refused earlier — at the destination, backstop and ledger gates — before key creation was reached. `.secrets/j1m/` is empty, and that emptiness is what proves the orchestrator lifecycle never began, not that a run's key was cleaned up. Separately, Sol's **isolated** gate probe called `create_ephemeral_ssh_key` and `assert_persisted_argv_handle` once and then `destroy_ephemeral_key_directory`, which is why the directory is empty rather than absent. That probe is not the orchestrator and it minted nothing for run b.
Nothing beyond the read-only catalogue query contacted a provider.

### Two dry-run gaps, recorded as follow-ups

The offline gate passed at **142 argv / 0 refused** while two of the three
blockers above stood. It exercises a fake environment and a fake ledger, so it
checked neither the real environment's backstop against the configured runtime
(`J1M-DRYRUN-REAL-ENV-BACKSTOP-001`, UNCLAIMED, S4) nor the real ledger's
canonical validity (`J1M-DRYRUN-LEDGER-VALIDITY-001`, UNCLAIMED, S4). **Either
check would have caught its blocker offline, before a launch attempt, at zero
cost.** That is the honest limit of the gate as it stands: a PASS proves the
argv surface, not the environment the run will actually meet, and ADR-0005's
validation section now says so. Both follow-ups are scoped read-only — validate
and report, never rewrite the real ledger, never adjust the real ceiling, and
never read a secret value.

## Independent audit disposition

The independent S0/S4 docs and evidence-scope audit of tip `97aec49` returned
`ACCEPT_WITH_REQUIRED_FIXES` with no BLOCKER: **3 MAJOR, 8 MINOR, 6 NIT, all
wording**. All fourteen reproducible measurements, nine source citations and
nine pieces of ledger metadata reproduced exactly on the auditor's own runs. No
gate cell, overall-state line, blocker `State:`, `Workaround:` or `Impact:` line
moved. Every MAJOR and MINOR is applied here, plus the cheap NITs.

- **MAJOR 1 — the key mint/destroy claim is withdrawn.** The refresh said
  `create_ephemeral_ssh_key` passed and the key directory "was destroyed leaving
  zero residue", which asserts a lifecycle step that never ran and inverts what
  the empty directory proves. Corrected in all five places to keep two distinct
  facts both true: **no ephemeral key was ever minted by any launch attempt** —
  all three refused earlier, at the destination, backstop and ledger gates,
  before key creation was reached — and, separately, Sol's **isolated** gate
  probe minted and destroyed one key, which is why `.secrets/j1m/` is empty
  rather than absent. Nothing now implies the orchestrator reached key creation.
- **MAJOR 2 — the `commands.txt:24` quotation is corrected.** Sol corrected the
  recorded command after this packet's Phase A read: run-b summary §5 and
  `commands.txt:24` now carry the absolute
  `"$PWD/artifacts/qwen35-9b/remote-eval-20260911-b"`, corroborated by the
  absolute `dest=` at `commands.txt:34` and by the summary's new §7 "Sol
  post-staging corrections". Both files were re-read. Every "still reads
  relative" statement is corrected, and the now-redundant operator instruction
  is dropped. `J1M-CLI-RELATIVE-DESTINATION-001` **stays** — the CLI should
  resolve relative paths against `ROOT` — but its premise is re-anchored to the
  original staging error rather than to the current file.
- **MAJOR 3 — the comparator budgeting claim is no longer a literal presented
  as a verdict.** The refresh quoted `raises_authorized_cost: False` four times;
  that is a hardcoded literal at `scripts/j1m_orchestrator.py:917`. The
  *computed* companion `fits_static_worst_case` (`:914`, `required <=
  static_slack`) is False for every selection, and the runtime gate
  `_comparator_clock_available` (`:921`) can refuse the phase with typed
  `comparator_clock_insufficient` (`:949`, wired `:2489`). So **a paid run can
  complete, pass the Q4 evaluation, and still return no retention number.**
  Every occurrence now carries that mechanism, and "reachable inside a single
  budgeted run" is softened to "only if the runtime clock gate admits the
  comparator phase; the static worst case does not fit". This is the same
  standard the refresh already applies seven times to
  `"safe_to_migrate_now": False` — the auditor was right that it was applied to
  one literal and not the other.
- **MINORs applied (8/8).** The false self-certification is narrowed to "no
  *credential* value" (the non-secret ceiling `=2` genuinely is reproduced); the
  dry-run gate is credited with **one** of the three refusals rather than two,
  since its other find was the artifact-destination mode; **two findings closed
  before merge** — the deferred comparator cleanup tail and the untyped
  `q4-oracle` refusal — are now marked closed with their source sites
  (`:2142`, `:1089-1097`) instead of recorded as open residuals; three dropped
  qualifiers are restored (the key survives one narrow `signal.signal` window,
  the dry-run gate needs the unit suites for two of its own properties, and the
  comparator arms are "plan- and argv-complete", never executed); the ADR's
  zero-pending-owner figure is marked **post-adjudication**, resolving its
  internal contradiction with the 65-pending/22-owner line; the
  `MODEL_DECISION.md` line citation is corrected to `:497-498` in the delivered
  tree, since this commit itself shifts it; and §10's two overstated agreement
  claims are fixed — `action_id` bounds differ across the three, and a **fourth**
  declaration in the frozen contract `contracts/external-tools/v0.1.0.json:72`
  agrees on names while constraining `parameters` more tightly, untested.
- **NITs applied.** The untracked run-a residue in the `main` checkout is now
  recorded rather than certified around; "stepping the gates in isolation" is
  re-described as one cause per attempt, which is what `commands.txt:32-37`
  shows; two imprecise ADR citations are corrected and the ADR Status line now
  carries its own non-advancement caveat, matching ADR-0003 and ADR-0004; and
  the 8,192 parameter bound is described as characters (UTF-16 code units),
  not bytes.
- **NIT 17 not applied, deliberately.** The broad tail of review residuals the
  auditor lists — corpus RES-3/RES-4/SYSTEMATIC-4 and the 0.6% all-residual
  rate, the comparator-engine literal-extractor and `wait_for_health` NITs, the
  key-handle same-uid race, and the `/private/tmp` suite-placement finding — is
  left for Sol to triage into rows. None is required before merge by any report,
  and inventing rows for them here would overstate this refresh's mandate.

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
- New follow-ups recorded as `UNCLAIMED`, none gate-advancing:
  `COMPARATOR-BF16-ARM-001`, `J1M-CLI-RELATIVE-DESTINATION-001`,
  `J1M-DRYRUN-REAL-ENV-BACKSTOP-001` and `J1M-DRYRUN-LEDGER-VALIDITY-001`.
  `LEDGER-GENESIS-001` was added as `UNCLAIMED` in Phase A and is
  `DONE_OPERATOR_ACTION` by the end of Phase B, with its source residual named
  rather than closed.
- **The only thing now blocking a measurement is a human permission decision**,
  not a project gate. Every project-side pre-spend gate passes. That is worth
  stating plainly because it is the first time in this program's record that
  the obstacle has been outside the repository rather than inside it.
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

This refresh is complete and `READY_FOR_REVIEW`. The next action is not this
session's: **a human decides whether to approve the launch** of
`remote-eval-20260911-b`, whose command is ready to run unchanged in the run-b
summary §5 and `commands.txt`, now carrying the corrected absolute
`--artifact-destination`.

When the run executes, its numbers arrive as a **separate addendum**, not as an
edit to this packet's evidence: the actual cost against the recorded USD 10.00
/ 4 h caps and the ADR-0005 program cap, the salvaged receipts, and any score —
all recorded as evidence, with every gate state left to a separate Sol gate
decision. This session launches nothing and has spent nothing.

## Sol action requested

Review and merge; no gate change is requested and none is implied. Four items
need a Sol decision beyond the merge:

1. Whether `contracts/engine-api`'s declared `max_tokens: 64` is reconciled to
   the engine's enforced `1..256` by an interface change request now, or
   deferred together with the aspirational deep-mode budgets.
2. Owners for the three new dry-run and CLI follow-ups, since two of them
   (`J1M-DRYRUN-REAL-ENV-BACKSTOP-001`, `J1M-DRYRUN-LEDGER-VALIDITY-001`) would
   have prevented two of the three pre-spend refusals recorded above and are
   cheap, offline, and read-only by design.
3. Whether the hardcoded `"safe_to_migrate_now": False` now gets its own ADR,
   given that the genesis has been executed around it and the literal is the
   last piece of that blocker still in source.
4. Confirmation that `DONE_OPERATOR_ACTION`, defined in this refresh, is the
   vocabulary Sol wants for operator actions completed outside source; it is
   deliberately not an approval state.

Only Sol may merge or alter a phase or release gate, and nothing here requests
either.
