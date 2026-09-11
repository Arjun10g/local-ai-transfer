# Program Status

## Current governance snapshot — 2026-09-11 (refresh v7)

- Exact integrated source head is `c469ed1`. Two source integrations this
  interval: merge `2e8c86a` (`J1M-HOST-PRIVACY-001`, from
  `luna/j1m-host-privacy-v1`) and the direct integration `c469ed1`
  (`J1M-HOST-ROOT-001`). The previous baseline `f4bb424` and its docs
  descendant `a0aa02c` are historical and superseded. **Release/full access
  remains `BLOCKED` / `NOT_READY`, and no blocker `State:` line changes.**
  Nothing below advances a gate.
- **The first paid Shadeform run executed, and it FAILED.** This is the
  substantive change since v6, which recorded the run as pending. Run
  `j1m-eval-20260911-remote-d`, instance `d75747f8-4801-4f72-8a5f-5713ef333905`,
  hyperstack `A100_80G` ×1 in montreal-canada-2, created 2026-09-11T20:24:07Z
  and active 20:28:44Z. It failed at `eval-stage:j1m_runner` with **8 receipts
  requested and 0 salvaged**. Teardown is confirmed — deletion receipt written,
  no orphan, ephemeral key removed, and a read-only `GET /instances` returned
  zero afterwards. **USD 3.273486 was booked**: the provider charges the whole
  reservation, so roughly six metered minutes cost the same as two hours. The
  row is settled in `experiments/LEDGER.md` with pending `0.0`, so it blocks no
  later provisioning.
- **There is still no score.** No oracle delta, no retention ratio, no remote
  hash verification, and no measurement of any kind on the shipping 33/37
  profile. That gap is unchanged since the program began and is not narrowed by
  anything in this refresh.
- **Root cause, and why no local validator could have caught it.** Every
  command run `d` issued was accepted by `validate_persisted_argv`, and
  `scripts/j1m_dry_run.py` passed. Two independent defects, both invisible to
  argv inspection: (1) the remote login shell carried the image default
  `umask 022`, so the plan's `mkdir -p` created `0755` directories and the
  probes wrote `0644` receipts — `j1m_runner._private_atomic_write` refused its
  very first write and `_safe_cli` exited 2 with its typed refusal on **stdout**,
  which the lifecycle receipt never captured; and (2) the uploaded runner's
  `PRIVATE_OUTPUT_ROOT` is `/scratch`, not `/scratch/j1m`, so its progress file
  lives at `/scratch/experiments/runtime/` — a directory no remote plan ever
  created.
- **Two slices close it, and a third gap was found by review before spending
  again.** `J1M-HOST-PRIVACY-001` puts `umask 077 &&` at the end of
  `sf.ssh_base` (one place, every remote command; `scp_base` untouched, as it
  runs no login shell), chmods every created directory including intermediates,
  derives the progress directories from `resources.progress_path`, publishes
  the five host-side receipts `0600` atomically into `0700` directories, and
  records a bounded credential-screened `stdout_tail` for failed stages only.
  `J1M-HOST-ROOT-001` then establishes the trusted root's own shape — which
  `mkdir -p` is a no-op about, so the provider image had been deciding it — via
  `test ! -L /scratch` and `sudo chmod go-w /scratch`, and republishes
  `/etc/ssl/certs` readable so `umask 077` cannot leave a `0600` CA bundle that
  would break TLS for every later non-root stage.
- **A deliberate contract change, named rather than buried.** The two pinned
  tests asserting that a failed remote receipt retains no stdout were
  rewritten. A completed stage still retains nothing, the tail is bounded to
  1200 bytes, and credential-shaped output is still `<redacted>` — but ordinary
  stdout of a *failed* stage is now persisted. Without it a paid failure books
  cost and returns no diagnosis, which is exactly what run `d` did.
- **A protocol deviation, recorded rather than smoothed over.** The independent
  review of `J1M-HOST-PRIVACY-001` returned `ACCEPT_WITH_REQUIRED_FIXES`, and
  its required fixes were applied *after* the merge as `J1M-HOST-ROOT-001`,
  not as a pre-merge repair round. The operator directed a compressed
  integration path. Each finding was re-verified against the production
  predicates before being actioned, and none was a defect in what was merged —
  all three were gaps the merged slice did not yet close.
- **One reviewer item could not be closed by evidence.** Whether `apt` running
  under `umask 077` leaves a non-world-readable CA bundle was to be settled by
  a `docker run ubuntu:22.04` check; this host has no container runtime, so it
  is closed **by construction** (republish the trust store unconditionally)
  rather than by measurement. It remains an open evidence item, not a proven
  negative.
- **Run `j1m-eval-20260911-remote-e` is prepared but has NOT executed.** Phase
  id unused, destination `artifacts/qwen35-9b/remote-eval-20260911-e/` present
  at `0700` and empty, ledger carrying no pending row, comparator arms
  deliberately deselected (`--evaluate-comparators ""`) so the first run that
  must produce a score exercises no deferred-cleanup machinery that has never
  run on a real host. It is blocked at the tool layer by the Claude Code
  auto-mode permission classifier — **a harness control, not a project gate**.
  No project refusal, blocker, or policy stopped it, and the lane again
  declined to route around the denial, because a billable provisioning command
  is exactly what such a control exists to hold. USD 0.00 spent on it, no
  instance created, no reservation row.
- **Spend against the ADR-0005 ceiling.** Cap USD 50.00. Booked to date USD
  10.041398 — legacy settled 6.767912 plus run `d` 3.273486. Pending owners 0.
  A successful run `e` is projected at USD 2.60–3.30, worst case USD 4.05
  against the 3 h provider ceiling, inside the USD 10.00 per-run cap.
- **Evidence reproduced at `c469ed1` on 2026-09-11.** Python
  `python3 -m unittest discover -s tests -t . -p 'test_*.py'` **`Ran 840 tests`
  OK** (743 at v6; +96 across the comparator, corpus, host-privacy and
  host-root slices); `tests/native` **309 OK**; `tests.qa.test_safe_runner`
  **20 OK**; `npm test` **432 tests / 430 pass / 0 fail / 1 skipped / 1 todo**
  (the pre-existing filesystem `KNOWN LIMITATION` pair); the offline J1M
  dry-run gate **PASS, 24 checks, USD 0.00**; safe QA **`BLOCKED`** with 0
  missing and 0 unknown, unchanged and expected. The `-t .` root argument
  remains load-bearing.
- **What the dry-run gate now proves that it could not at v6.** It replays the
  production plan's own `mkdir`/`chmod` text through `/bin/sh` against a `0755`
  stand-in `/scratch` and judges the result with the production predicates,
  under a **forced `umask 022`** and against a **pre-seeded already-existing
  `0755`** directory — so neither the operator's own umask nor a fresh
  temporary tree can make a broken plan pass. It accepts 11/11 receipt paths
  and publishes 2 at `0600` on the current plan, and refuses all 11 plus both
  publish attempts (13 refusals) with `0644` files when replaying the run `d`
  plan, reproducing the booked failure offline.

## Previous governance snapshot — 2026-09-11 (refresh v6, superseded by v7)

- **Remote evaluation run 2026-09-11-b: PENDING — launch-ready, awaiting
  operator permission. It has not executed.** The run authorized under ADR-0005
  (`remote-eval-20260911-b`, caps USD 10.00 and 4 hours) reached a launch-ready
  state on 2026-09-11 and was then blocked at the tool layer by the Claude Code
  auto-mode permission classifier. That is a **harness control, not a project
  gate**: no project refusal, blocker, or policy stopped it, and the lane
  correctly declined to re-shape or route around the denial, because a
  billable cloud provisioning command is exactly what such a control exists to
  hold. A human approval is required. **USD 0.00 spent, no instance created,
  `.secrets/j1m/` empty (zero residue), no reservation row.** There is no
  score, no oracle delta, no retention ratio, and no remote hash verification.
  The destination `artifacts/qwen35-9b/remote-eval-20260911-b/` exists at 0700
  and is empty. The run's numbers will be recorded as a **separate addendum**
  when they exist; nothing in this document anticipates them.
- **Three launch attempts on 2026-09-11 returned typed `input_rejected` before
  any provider call**, each traced by Sol stepping the pre-spend gates in
  isolation. All three were pre-spend, at USD 0.00.
  - *Staging.* The recorded launch command used a **relative**
    `--artifact-destination`. `prepare_artifact_destination`
    (`scripts/j1m_orchestrator.py:408`) computes
    `Path(destination).relative_to(j1m_runner.PRIVATE_OUTPUT_ROOT)`, and since
    that root is the absolute repository root a relative operand raises and is
    surfaced as `artifact destination must live under the trusted output root`.
    **This is not a code defect** — refusing an ambiguous destination before
    anything is billable is the correct behaviour and must not be loosened. The
    ergonomic fix is recorded as `J1M-CLI-RELATIVE-DESTINATION-001`, and until
    it lands every launch command must carry an absolute destination.
  - *Provider backstop.* `.secrets/shadeform.env` carried
    `SHADEFORM_AUTO_TERMINATE_HOURS=2` against a 1.94 h eval plan, giving
    1.03x headroom where `scripts/shadeform_lifecycle.py:190` requires
    `MIN_BACKSTOP_MARGIN = 1.10`; the refusal is a typed `BackstopError`
    (`:3374-3386`). Sol raised the ceiling to 3, a deliberate decision rather
    than a fitted knob — the code says so itself at `:3384` — putting the worst
    case at 3 h × USD 1.35 = USD 4.05, inside the USD 10.00 run cap. Only key
    names and that non-secret numeric ceiling are recorded here; **no credential
    value** from that file was read, printed, or reproduced anywhere.
  - *Legacy cost ledger.* `sf.list_candidates` validates
    `experiments/runtime/cost-ledger.jsonl` against
    `local_bmo.shadeform.cost-event.v2`
    (`scripts/shadeform_lifecycle.py:80`), and the 108-line 2026-09-04 ledger
    failed canonicalization at line 1 with `stored cost event is not canonical`
    (`:1989`). **This blocked every run**, and it is the concrete, reproducible
    form of the abstract `SAFE_TO_MIGRATE_NOW` /
    `legacy_schema_or_owner_binding_missing` blocker that governance had been
    carrying in the abstract for weeks.
- **Reviewed cost-ledger genesis executed (LEDGER-GENESIS-001 → done).** Sol
  moved the legacy ledger — sha256
  `756daa504fc9a1af40f32ce4af777935fb0bcdd00b01a1ad8688ab2c02f4c692`, 108 lines
  holding 43 settled rows summing USD 6.767912 plus 65 pending rows across 22
  pending-only owners whose instances already hold deletion receipts,
  adjudicated by Sol as historical stale estimates and non-billable — to
  `experiments/runtime/legacy/cost-ledger.legacy-20260904.jsonl` at 0600 inside
  a 0700 directory, and copied it to
  `archive/worktree-runtime-state-20260911/main-cost-ledger.legacy-20260904.jsonl`
  with `SHA256SUMS.ledger`. `scripts/shadeform/initialize_cost_ledger.py` then
  ran with `--program local-bmo-shadeform --currency USD --budget-cap-usd 50
  --prior-settled-spend-usd 6.767912 --current-pending-owner-count 0`, the two
  required evidence digests, and the reviewed-genesis confirmation, returning
  `status: created`. Verified read-only in this refresh: the legacy file hashes
  to exactly that sha256 at 108 lines, and the new
  `experiments/runtime/cost-ledger.jsonl` is 438 bytes, one line — the sole
  genesis event — at mode 0600. **Residual, explicitly not closed:** the
  migration preflight's hardcoded literal `"safe_to_migrate_now": False`
  (`scripts/shadeform_ledger_migration_preflight.py:1110`) is unchanged in
  source and the incidents-schema findings stand. A genesis does not
  retroactively validate the legacy rows; it archives them and starts a
  canonical ledger beside them.
- **Pre-spend gates after genesis, all read-only or local.**
  `list_candidates` returned **9 candidates**, exactly one matching the pinned
  target (hyperstack / montreal-canada-2 / A100_80G / USD 1.35 per hour).
  No ephemeral key was ever minted by any launch attempt: all three refused earlier, at the destination, backstop and ledger gates, before key creation was reached. `.secrets/j1m/` is empty, and that emptiness is what proves the orchestrator lifecycle never began — not that a run's key was cleaned up. Separately, Sol's **isolated** gate probe called `create_ephemeral_ssh_key` and `assert_persisted_argv_handle` once, then `destroy_ephemeral_key_directory`, which is why the directory is empty rather than absent. That probe is not the orchestrator, and it minted nothing for run b. Nothing beyond the read-only catalogue query contacted a provider.
- **Two offline dry-run gaps recorded as follow-ups.** The gate passed at 142
  argv / 0 refused while two live blockers stood, because it exercises a fake
  environment and a fake ledger. It did not check the real environment's
  backstop against the configured runtime
  (`J1M-DRYRUN-REAL-ENV-BACKSTOP-001`) or the real ledger's canonical validity
  (`J1M-DRYRUN-LEDGER-VALIDITY-001`). Either check would have caught its
  blocker offline, before a launch attempt and at zero cost. This is the
  honest limit of the current gate: a PASS proves the argv surface, not the
  environment the run will actually meet.
- Audited integrated source baseline: exact
  `main@f89c1684ea051e1c9c92f944cb080d424a086f55`, the last source merge. The
  docs descendants `1e341e9`, `aa1087a`, `25a0d95`, `74a43d0`, `335211f`,
  `1b22a40` (claims flips) and this refresh are documentation descendants, not
  self-referential source hashes. The previous baseline `263f114` and the
  refresh v5 snapshot below are historical and superseded. Overall
  release/full-access state remains `BLOCKED` / `NOT_READY`; no phase or
  release gate is advanced.
- Four slices were integrated this interval, each independently reviewed.
  Comparator evaluation phase (`9a8519c`, merge `c4c0c82`) and comparator
  engine transport (`943d013`, merge `997b3f1`) each returned
  `ACCEPT_FOR_MERGE` on the first pass. The model quality corpus (`0cea3ec`,
  merge `79a8888`) and the paired salvage-transport / key-handle slices
  (`6c475d4` and `b11e617`, merge `f89c168`) each returned
  `ACCEPT_WITH_REQUIRED_FIXES` on the first pass and `ACCEPT_FOR_MERGE` after
  repair. None adds compile, live, provider, production, Windows, or target
  evidence.
- **Comparator evaluation phase.** Adds a default-OFF `--evaluate-comparators`
  flag that retains the comparator arms, scores them with the same evaluator,
  and writes a `comparator-eval-receipt.v1`. Comparison mathematics use a
  paired bootstrap with an unpaired stratified fallback. Default-OFF
  equivalence is proved: with no selection, the plans for every mode are
  byte-identical to the plans with the flag absent.
- **Comparator engine transport.** Makes the arms runnable by running them
  against the pinned upstream `llama-server` — the `runtime_oracle` the
  specification already names — rather than building a second product engine.
  This is the substitution Sol confirmed, and it leaves `native/` byte-unchanged,
  so the compiled Q4 identity is untouched. Adds `--transport upstream-openai`,
  moves the selection vocabulary to the closed set `'' | q4-oracle | q8 |
  q8,bf16`, carries the per-arm bearer via `--api-key-file` (minted `O_EXCL`
  0600, never present in any argv, log, or receipt), binds each arm to
  `127.0.0.1` on its own port, and flips `_COMPARATOR_ENGINE_AVAILABLE` to
  `True`. Honest residual recorded by the reviewer: the sampler chains are
  argmax-equivalent, not identical, and token-sequence equivalence cannot be
  proved offline because it needs the GGUF.
- **Model quality corpus.** 1,336 cases across 13 categories against a 1,180
  floor and a 1,298 target; hash-derived splits (`sha256(id) % 100`) of 271
  train / 250 dev / 815 test; six author lanes merged conflict-free, each
  touching only its own category files; 103 tests. Every scoring proposition
  carries an inline deterministic `match` object — 271 fact items (207
  `key_facts` plus 64 `forbidden_facts`) and 284 rubric items, 555 in total —
  so pass/fail is computed by string and regex evaluation with no model, judge,
  or human rater anywhere in the path. That is what makes the corpus gateable;
  before it, 176 cases depended on a matcher the corpus never defined.
  Independent audit returned `ACCEPT_FOR_MERGE` with a residual
  scoring-affecting defect estimate of **0.2%** (3 cases), safe in the
  under-crediting direction only — it can reject a correct answer but cannot
  admit a violation. A further residual errs the opposite way and is excluded
  from that 0.2%; both must close before the corpus produces a retention or
  parity number. A fixture is not a score: the corpus advances no gate.
- **Salvage transport and private key handle.** Restores a bounded,
  allowlisted, identity-bound receipt salvage. It is not a revert: the TOCTOU
  rationale of `2d7db4f` is preserved by never giving SCP the validated
  destination — transfers land in a private staging directory and are published
  by no-follow descriptor. The ephemeral SSH key moves to
  `.secrets/j1m/<run-id>-<nonce>/ssh-key` and is destroyed on every exit path
  except one narrow window: between the handle proof and the protected region a
  `signal.signal` `ValueError` raised off the main thread leaves the per-run key
  directory in place, at 0700/0600 and gitignored;
  the validators are byte-unchanged, and tests pin that the old temporary-
  directory layout is still refused, so the fix is at the operand rather than
  the gate. Build, prove and eval now fail when a required receipt is missing,
  and comparator receipts are salvageable and identity-bound.
- **Three latent refusals are fixed.** Salvage had been unconditionally refused
  since `2d7db4f`, so a paid eval run would have returned `failed` with zero
  receipts. `_persist_lifecycle` raised `NameError` on an undefined
  `MAX_RECEIPT_BYTES` since `8e3f599`, and because both call sites swallow
  exceptions, no lifecycle receipt had ever been written. The key operand shape
  failed the canonical-private-handle rule `validate_persisted_argv` has
  enforced since `991b70e`, refusing every ssh/scp argv before it could spawn.
  One of the three — the `_persist_lifecycle` `NameError` — was found by the
  offline dry-run gate. The gate's other find was the artifact-destination mode,
  which is not one of these three; the key-operand refusal was self-disclosed by
  the salvage packet and the salvage refusal was the deliberate `2d7db4f` state.
- **First launch attempt stopped pre-spend.** On 2026-09-11
  `remote-eval-20260911-a` stopped at the pre-launch gate at **USD 0.00**. No
  instance was created, no provider mutation of any kind was issued,
  `--execute` was never passed, and `SOL_J1M_REVIEWED=1` was never set. It
  stopped because salvage was refused, so the run could not have returned the
  receipt it exists to produce. Nothing required teardown.
- **Model-lane factual corrections, each verified in source.** (a) The
  evaluation lane is gated by `SOL_J1M_REVIEWED=1`
  (`scripts/j1m_orchestrator.py:2840-2841`), **not** by
  `REMOTE_EXECUTION_ENABLED`, which exists only in
  `scripts/shadeform/remote_external_tools.py` (`:85`, enforced `:804` and
  `:1255`) and gates only the hostile-tools QA lane. (b) The lane reproduces
  the Q4 from the **public** pinned revision with no token: `HF_TOKEN` is not
  in `MUTATION_ENV_KEYS` (`scripts/shadeform_lifecycle.py:134-152`), and the
  orchestrator records at `:2410-2412` that it must not be placed on the
  ephemeral host. Any claim that Hugging Face rotation blocks evaluation is
  withdrawn; SI-002 rotation remains required for authenticated Hugging Face
  use. (c) bf16, Q8_0 and Q4 are rebuilt in **every** eval run
  (`scripts/j1m_runner.py:1420-1422`), so the "must budget a full
  re-conversion" planning statement in B-004, B-006 and `MODEL_DECISION.md` is
  withdrawn in favour of the marginal comparator cost — but only if the runtime clock gate admits the comparator phase: `fits_static_worst_case` (`scripts/j1m_orchestrator.py:914`) is computed False for every selection, the frequently quoted `raises_authorized_cost: False` (`:917`) is a hardcoded literal rather than a verdict, and `_comparator_clock_available` (`:921`) can refuse the phase at run time with typed `comparator_clock_insufficient` (`:949`), so a budgeted run can pass the Q4 evaluation, spend the money, and still return no retention number. (d) The ledger migration
  preflight's verdict is not computed: `"safe_to_migrate_now": False` is a
  hardcoded literal at
  `scripts/shadeform_ledger_migration_preflight.py:1110`, alongside
  `adjudication_required: True` and `genesis_emission_forbidden: True`; four
  structural findings previously masked by permission refusals are now visible
  and `evidence_complete` remains false. (e) B-004's prescribed non-A100 canary
  is unexecutable — `execute()` refuses canary mode (`:2023`) and
  `scripts/test/cuda_device_probe.py:67-68` demands a single A100 — so Sol
  accepted running the built-in probes on the A100 itself, recorded as an
  explicit deviation.
- **Spend authorization (ADR-0005).** On 2026-09-11 the user granted standing
  authorization to launch provider runs for this program. Sol set the program
  hard cap at USD 50, consistent with the USD 43.232088 that remains after the
  settled USD 6.767912 is subtracted, and records per-run caps per run.
  Lifecycle rules are unchanged: read-only catalogue → cost preflight →
  dry-run gate → watchdog/backstop → salvage before teardown on every exit path
  → exact teardown → post-run verification. That last step is **exact-instance,
  not account-wide**: by design "no account-wide instance-list operation exists
  in this module" (`scripts/shadeform_lifecycle.py:6-9`). Bookkeeping is not
  gate approval.
- **Ledger and permissions.** `experiments/` moved 0755 → 0700 and 22
  `*.deletion-receipt.json` files 0644 → 0600, content byte-identical with zero
  git diff. The **legacy** ledger settled at USD 6.767912 across 108 rows and
  43 distinct identities; it has since been archived and superseded by the
  genesis recorded above, so `experiments/runtime/cost-ledger.jsonl` is now the
  sole genesis event carrying that figure forward as
  `prior_settled_spend_usd`. The one orphan receipt `j1m-loopback-no-orphan` /
  `instance-loopback-1` (`actual_cost_usd` 7.1e-05) is adjudicated by Sol as a
  non-billable loopback test artifact; the other 21 match. No tooling command
  exists to apply that adjudication, so it stands as a recorded decision.
- **Governance document correction.** `SECURITY_AND_TOOL_POLICY.md` §10
  documented `process.run_allowlisted` as
  `executable_id`/`arguments`/`workspace_id`/`timeout_ms`. No shipping
  component accepts that shape; the advertised catalogue, the tool definition
  (`host/tools/local/process-run.mjs:23-25`) and the controller validator
  (`host/agent/controller.mjs:127`) all use `action_id` plus an optional
  `parameters` object, agreeing on the field names and on top-level
  `additionalProperties: false`. A fourth definition in the frozen contract
  `contracts/external-tools/v0.1.0.json:72` agrees on the names but constrains
  the nested `parameters` object more tightly than the three do; nothing tests
  the pair. Per Sol's ruling the
  shipping catalogue is authoritative and §10 is corrected to it, including
  removal of the obsolete PowerShell subcommand-policy text — the shipping
  implementation refuses PowerShell and every other interpreter outright. No
  source, schema, capability, or gate changed.
- **`max_output_tokens`, ruled.** Three values disagreed. Only the engine's is
  enforced: `native/server/chat_request.cpp:269` rejects anything outside
  `1..256` with `max_tokens out of range` and defaults to 8 when the field is
  absent (`:271`). `contracts/engine-api/contract.json:4` declares
  `"max_tokens": 64` and its `README.md:47` repeats it, but nothing enforces
  that at runtime and it is now narrower than the shipping engine.
  `MODEL_DECISION.md` describes a 1,024 UI default and a 2,048 hard answer cap,
  which the engine would refuse outright. **Sol's ruling: the enforced engine
  contract bound is authoritative for the MVP.** The corpus, the shipping
  fixture and the lane validators already bind to 256. The larger deep-mode
  budgets are marked aspirational in `MODEL_DECISION.md` and require an
  engine-api version bump through `INTERFACE_CHANGE_REQUESTS.md`; the
  `contracts/engine-api` 64 should be reconciled by that same bump.
- **Worktree housekeeping.** After the 103 removals recorded in v5, Sol had the
  remaining 10 removed on 2026-09-11. All 10 `git worktree remove` calls
  succeeded with no `--force` and no `rm -rf`; `git worktree prune` then exited
  cleanly. Non-git runtime state was archived first to
  `archive/worktree-runtime-state-20260911/` — which sits **beside** the
  repository, not inside it — as 7 tarballs plus `SHA256SUMS` and `MANIFEST.md`,
  directory mode 0700, with `shasum -a 256 -c SHA256SUMS` passing on all 8
  entries. Three of the 10 had only build output and needed no tarball. One
  differing `.env` was moved there at mode 0600 and **never opened**; hashes,
  sizes and modes only. **No branch ref was deleted:** all 10 removed
  worktrees' `luna/*` refs remain and every tip is reachable by name. At this
  baseline the repository holds **134 `luna/*` refs** (135 heads including
  `main`), up from the 121→122 recorded at cleanup time because lanes continued
  to be created afterwards. `git worktree list` in this refresh shows exactly
  **two** worktrees — the `main` checkout and this refresh worktree — so no
  active lane worktree remains for Sol to remove.
- **Current evidence reproduced on this baseline** (worktree root, 2026-09-11):
  `python3 -m unittest discover -s tests -t . -p 'test_*.py'` **`Ran 743
  tests` OK**; `npm test` **432 tests / 430 pass / 0 fail / 0 cancelled / 1
  skipped / 1 todo**; safe QA **`BLOCKED`**, 70 discovered / 0 missing / 0
  unknown, **76 records (1 PASS / 75 SKIP)**; offline J1M dry-run gate
  **PASS**, 142 argv recorded, 0 refused, **USD 0.00**, 0 WARN;
  quality-corpus validator **PASS** in both modes over **1,336** cases; strict
  tracked-JSON **168 tracked / 167 strict-valid / 1 intentional hostile
  fixture**; host import graph **29 modules / 67 unique relative import edges /
  0 unresolved / 0 cycles** from 72 occurrences. Two invocation facts belong
  with these: the `-t .` root argument is load-bearing, because without it
  `tests/qa/` shadows the root `qa/` package and discovery collapses to `Ran
  646 tests` with **8 module-level errors**; and the dry-run gate **FAILS** in a
  fresh worktree on `operator_artifact_destination_is_salvage_ready` until
  `chmod 700 artifacts artifacts/qwen35-9b` is applied, because git cannot
  carry directory modes. No `duration_ms` figure is carried into this record:
  a single wall clock on one development host is not reproducible evidence.
- No readiness follows from any of the above. Authorized spend is not evidence;
  merged source is not evidence; evidence is not approval. Previously exposed
  credentials, including the leaked Hugging Face token, still require
  source-side rotation or revocation before any authenticated use. Missing real
  signed artifact custody, model-quality evidence, provider accounts and
  consent, native compile and secure-owner evidence, an exact Windows
  hardware/backend receipt, live tool evidence, and target acceptance all
  remain release gates.

## HISTORICAL / SUPERSEDED governance snapshot — 2026-09-11 (refresh v5)

> Historical interval snapshot for `main@263f114`; superseded by the refresh v6
> snapshot above wherever it states current truth. In particular its
> `REMOTE_EXECUTION_ENABLED`, re-conversion-budget, unauthored-corpus, and
> worktree-retention statements are corrected above.

- Audited integrated source baseline: exact
  `main@263f11413d1746044a6cc13062ad2b1f821c4d11`, the last source merge. The
  docs descendants `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f` and
  `1b22a40e21dce0ebc903bc3c2024ab162adc2d56` (task-claim flips) and this
  refresh are documentation descendants, not self-referential source hashes.
  The previous baseline `7239b7e` and the refresh v4 snapshot below are
  historical and superseded. Overall release/full-access state remains
  `BLOCKED` / `NOT_READY`; no phase or release gate is advanced.
- Two slices were integrated this interval. Each was independently S0/S4
  source-reviewed and returned `ACCEPT_FOR_MERGE` on the first pass with
  NOTE/MINOR findings only. Neither adds compile, live, provider, production,
  Windows, or target evidence.
- Descriptor WAL contract: accepted tip `9f136a0`, merged by
  `237d59b30ac3e3424990636595cc5dab88480ad0`. It freezes
  `contracts/action-journal-descriptor-wal/v0.1.0`, the separate contract
  ADR-0004 requires for the dormant v2 WAL boundary. The contract is
  statically bound to the C++ and Node constants (48-byte header, 32 MiB
  limit) and to the derived v2 status set: 26 codes for v2 against 25 for v1,
  with `v1 ∪ v2 ∪ {platform_unavailable}` equal to the 30-code storage set.
  Sol accepted that corrected invariant because `platform_unavailable` is
  declared in the boundary but emitted by no function body.
  `contracts/action-journal-storage/v0.1.0.json` is byte-identical to `main`.
  Review item R4 — dropping `DELETE` access after WAL publication — is closed
  as a documented, test-pinned accepted limitation on a cost/ordering trade
  rather than an impossibility: Win32 offers no operation that narrows an open
  handle's own access, and a `ReOpenFile` with reduced access is a new open
  that collides with the retained handle's exclusive `FILE_SHARE_READ`
  reservation. The source note concedes that a duplicate-down-and-close would
  shrink the surviving handle's rights, and argues the cost — the extra
  `DuplicateHandle` can itself fail, after publication and after
  `discard.disarm()` — not that the narrowing cannot be done. ICR-RUN-WDJB-001 carries the dated note. No C++, Node, CMake,
  or package change; the boundary stays uncompiled, unlinked, and outside
  every product/package/activation graph.
- Graph sent-mail proof projection: accepted tip `26beb82`, merged by
  `263f11413d1746044a6cc13062ad2b1f821c4d11`. The defect previously recorded
  as merely plausible is confirmed against the pristine base:
  `listSentForDigest` asked a Sent Items collection query for
  `internetMessageHeaders` and hard-required that property on every item, so
  the sole completing state `unique_sent_item` was unreachable in code. On the
  documented Graph behavior that `internetMessageHeaders` is returned only on
  single-message projections — a premise that cannot be confirmed without a
  live account; the repair is fail-closed under either behavior —
  `mail.send_draft` could never complete against a real account, a safe false
  negative and never a false completion. Independently of that premise, a
  transport fault on the query escalated the record to `unknown_manual`. The repair
  shares the bounded per-message retrieval rather than duplicating it:
  `collectMailProof` issues one folder ID page plus at most
  `MAX_MAIL_PROOF_CANDIDATES` = 20 exact GETs per call, refuses absent or
  malformed markers before any request, returns typed inconclusive results
  instead of throwing, adds a `mail.send_draft` proof deadline with a typed
  `sent_proof_budget_exhausted` exit, and preserves operator cancellation. The
  slice disclosed its behavior changes on already-merged paths. Follow-up
  recorded as `GRAPH-SENT-PAGINATION-PRECHECK`: the unchanged pre-send Sent
  Items `@odata.nextLink` refusal blocks every send for a mailbox holding more
  than 50 sent items. Two NOTE-level limits also stand, both pre-existing and
  unchanged by this slice: a pre-dispatch provider refusal still lands as
  `unknown_manual` even though nothing was sent, and three pre-proof requests
  in the send branch remain uncapped, so the seam is bounded while the tool as
  a whole is not and a slow provider can still surface as `tool_timeout` →
  `unknown_manual`. No live Microsoft account or provider evidence exists.
- Model testing, corrected current truth: the model has NOT been tested
  against the profile it must ship against. Every recorded score dates from
  2026-09-04 and was measured on retired fixtures — an 11-tool/34-case canary
  fixture and a 28-tool/32-case production profile — on CUDA/A100 hardware,
  never on CPU and never on Intel Vulkan. The current production profile is 33
  tools and 37 cases, fixture `tests/model/production_tool_call_eval.json`
  SHA-256
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`
  (hashed in this worktree), and it has no recorded score at all. The model's
  score on the shipping profile is unknown, not merely below gate.
- Deleted comparators: the Q8_0 and bf16 conversion outputs exist only as
  hashes in `artifacts/qwen35-9b/scan-receipt.json`, and
  `artifacts/qwen35-9b/post-cleanup-receipt.json` records `Qwen3.5-9B-Q4_K_M`
  as the only remaining GGUF. The ≥95% quality-retention gate
  (`execution/ACCEPTANCE_CRITERIA.md:215`) therefore has no reachable
  reference artifact, so any future quality run must budget a full Shadeform
  re-conversion rather than an evaluation alone. That materially changes B-004
  candidate cost planning.
- Quality corpus: `model/quality-eval/quality-fixture-spec.json` defines 13
  categories whose `minimum_cases` fields total 1,180, against only 3
  `fixture_cases` present. Authoring the corpus is unblocked — it needs no
  model, no spend, and no credential — and is recorded as `UNCLAIMED` task
  `MODEL-QUALITY-CORPUS-001`.
- Local development run on 2026-09-11, a Sol-authorized single
  development-only exception to the B-006 "do not load local bytes"
  workaround on the basis that the local bytes were re-verified byte-exact
  against the pinned identity: ABORTED before any model load by two
  independent stop conditions. (1) Host memory — this development host is an
  8 GiB Apple M2 MacBook Air with roughly 94% of swap in use and
  `kern.memorystatus_vm_pressure_level` at WARNING before the run started,
  against an artifact needing about 5.2 GiB resident. (2) A stale prebuilt
  engine — `out/build-real/native/lae-engine`, built 2026-09-04, is 532
  commits behind `fdfed07` with 79 of those touching `native/`, and rejects
  the current `serve` flags with `unknown argument`. No score was produced, no
  bytes were loaded, and nothing was bound or downloaded. The one positive
  result is that the model identity re-verified exactly: 5,629,109,088 bytes,
  SHA-256 `c654bc40…68873b`. The three `real_model` tests fail at argument
  parsing against the stale binary. Conclusion: no model evidence can be
  produced on this laptop. A real measurement requires a rebuild from current
  source on a machine with at least 16 GiB — i.e. Shadeform — once the
  human-only blockers clear: `REMOTE_EXECUTION_ENABLED = False`
  (`scripts/shadeform/remote_external_tools.py:85`, enforced at `:804`), no
  approved cost-ledger genesis (`coordination/POLICY_STATUS.md:19`),
  `SAFE_TO_MIGRATE_NOW=NO` (`governance/MODEL_DECISION.md`, current-truth
  section) with the
  legacy ledger files reported world-readable, an unrotated Hugging Face
  credential (`coordination/SECURITY_INCIDENTS.md:22`), and B-004's
  activation-unreliable A100 profile (`coordination/BLOCKERS.md`, B-004). The
  aborted-run receipt is held at
  `artifacts/dev-evidence/local-macos-20260911-aborted/README.md`, outside
  `artifacts/evidence-index/`; it is development evidence only and advances no
  gate.
- Repository housekeeping: on 2026-09-11 Sol had 112 worktrees inventoried and
  103 clean, stale worktree directories removed. No branch ref was deleted —
  120 `luna/*` branches before and after — and `git worktree prune` has not
  been run. Nine worktrees were kept: 2 lanes active at the time, 6 holding
  ignored runtime state under `experiments/runtime/` (incident logs, deletion
  and recovery receipts, lock files) that is not recoverable from git, and 1
  (`wt-model-performance`) holding a `.env`. Those 7 need an operator
  decision. No secret was read or printed.
- Current evidence reproduced on this baseline: `npm test` 432 tests/430
  pass/0 fail/0 cancelled/1 skipped/1 todo; native static `Ran 309 tests` OK,
  of which the narrower Windows-only pattern is `Ran 289 tests` OK — 309 is
  289 plus the 20 tests of the new descriptor-WAL contract suite; QA
  safe-runner `Ran 20 tests` OK; safe QA `BLOCKED` with 64 discovered, 0
  missing, 0 unknown and 70 records (1 PASS/69 SKIP). Strict tracked-JSON
  inventory records 154 tracked JSON files, of which 153 are strict-valid
  under a duplicate-key-rejecting parser and one is the intentional hostile
  fixture `tests/native/fixtures/windows_broker/duplicate-key.json`. The host
  import graph over `lae-host.mjs` plus `host/**/*.mjs` is 29 modules, 67
  unique relative import edges (72 relative import occurrences, counting each
  unique importer/target pair once), 0 unresolved specifiers, and 0 cycles.
  The single skip and single todo are the pre-existing filesystem
  `KNOWN LIMITATION` pair.
- No readiness follows from any of the above. Previously exposed credentials,
  including the leaked HF token, still require source-side
  rotation/revocation. Missing real signed artifact custody and model-quality
  evidence, provider accounts/consent, native compile/secure owner/broker
  evidence, an exact Windows hardware/backend receipt, live tool evidence, and
  target acceptance all remain release gates.

## HISTORICAL / SUPERSEDED governance snapshot — 2026-09-11 (refresh v4)

> Historical interval snapshot for `main@7239b7e`; superseded by the
> 2026-09-11 refresh v5 snapshot above wherever it states current truth.

- Audited integrated source baseline: exact
  `main@7239b7e1a6a512cabf9e5ab18ba463a7fac351fc`, the last source merge. The
  docs descendant `6c3a125749581554b88ade018181f61667990ac8` (task-claim flip)
  and this refresh are documentation descendants, not self-referential source
  hashes. Overall release/full-access state remains `BLOCKED` / `NOT_READY`;
  no phase or release gate is advanced.
- Three slices were integrated this interval. Each was independently S0/S4
  source-reviewed, went through one repair round, and its re-review returned
  `ACCEPT_FOR_MERGE`. None adds compile, live, provider, production, Windows,
  or target evidence.
- Graph restart reconciliation: accepted tip `4280e95` from base `d723c43`,
  merged by `61c9475`. It permits bounded automatic restart completion only for
  durably acknowledged, newly account-bound `mail.create_draft` records backed
  by a fresh unique exact provider `GET` proof. The bound is per candidate: at
  most 8 acknowledged candidate records, each checked with one bounded Drafts
  ID page and at most 20 exact proof GETs, an upper bound of 8 x 21 = 168
  provider requests per pass, all under the pass deadline. In-flight proof
  retrieval is now budget-aware and
  inconclusive-safe (typed results such as `draft_proof_budget_exhausted`), so
  the seam can no longer escalate a record to `unknown_manual`. `status()` was
  removed from the pass, giving 0 grant revocations; the auth epoch is sampled
  before the account fingerprint; a `complete()` failure is surfaced as a typed
  metadata-only degraded state; the token liveness margin is 90 s. The slice
  disclosed that it changed already-merged in-flight `listDraftsForMarker`
  behavior, because a collection `$select` cannot return
  `internetMessageHeaders`. Honest limits: the startup pass is operationally
  inert because tokens are memory-only; the pass is bounded but not
  cancellable, since both production call sites invoke it with no signal and it
  is limited only by its own 30 s deadline rather than host shutdown or the
  emergency stop; manual `POST .../reconcile` remains HTTP 501; `reconciling`
  records have no automatic resolution path; and
  `listSentForDigest` plausibly carries the same collection-projection defect,
  recorded as follow-up task `GRAPH-SENT-PROOF-PROJECTION`. No live Microsoft
  account or provider evidence exists.
- Windows descriptor journal bootstrap: accepted tip `e4ca09b`, merged by
  `4de01f7`. It adds a dormant Win32 `action-journal-v2.wal` secure create
  (`CREATE_NEW`) and trusted reopen bound to external volume plus file identity
  on the open handle, an exact-user protected DACL, no-follow ancestors, a
  fixed local NTFS requirement, a single-writer lease, header publication with
  flush and readback, a 48-byte header and 32 MiB limit byte-identical to the
  Node journal, a non-inheritable one-shot duplicate with explicit idempotent
  arm/revoke and a documented `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` launcher
  precondition, delete-on-failure for never-published just-created leaves, and
  anonymous SQOS on every path-derived open. The reviewer confirmed
  single-writer exclusion remains intact under the `FILE_SHARE_DELETE`
  concession on the reopen handle. The unit is uncompiled, unlinked, and
  outside all product/package/activation graphs; no Windows, production, or
  target claim is made. Still absent: native secure owner publication, an
  authenticity/anti-rollback anchor, compaction, a reviewed launcher, MSVC
  compile, and exact-target tests.
- Windows process authority: accepted tip `cfe8136`, merged by `7239b7e`. It
  adds the dormant `windows-process-authority` v1.0.0 contract and a private,
  move-only `LaunchAuthority`. It narrows B-005 in ownership and shape only;
  the mechanism — handle-relative launch, image-section retention, consume-time
  identity re-derivation, Job/token implementation, compile, and target —
  remains absent.
- ICR-RUN-WDJB-001 is Sol-approved as an additive source-only extension: four
  additive status codes (`handoff_already_transferred`,
  `source_handle_inheritable`, `inheritance_control_failed`,
  `final_path_mismatch`) in `contracts/action-journal-storage/v0.1.0.json` with
  version `0.1.0` retained, merged at `4de01f7`. The generalizing rule is
  recorded as [ADR-0004](adrs/ADR-0004-inert-contract-additive-extension.md).
  Independent source review is complete; formal gate approval is still
  required.
- Current evidence reproduced on this baseline: `npm test` 426 tests/424
  pass/0 fail/0 cancelled/1 skipped/1 todo; Windows
  native static `Ran 289 tests` OK; QA safe-runner `Ran 20 tests` OK; safe QA
  `BLOCKED` with 62 discovered, 0 missing, 0 unknown and 68 records (1 PASS/67
  SKIP). Strict tracked-JSON inventory records 153 tracked JSON files, of which
  152 are strict-valid under a duplicate-key-rejecting parser and one is the
  intentional hostile fixture
  `tests/native/fixtures/windows_broker/duplicate-key.json`. The host import
  graph over `lae-host.mjs` plus `host/**/*.mjs` is 29 modules, 67 unique
  relative import edges (72 relative import occurrences, counting each unique
  importer/target pair once), 0 unresolved specifiers, and 0 cycles. The single
  skip and single todo are the pre-existing filesystem `KNOWN LIMITATION` pair.
- No readiness follows from any of the above. Previously exposed credentials,
  including the leaked HF token, still require source-side
  rotation/revocation. Missing real signed artifact custody and model-quality
  evidence, provider accounts/consent, native compile/secure owner/broker
  evidence, an exact Windows hardware/backend receipt, live tool evidence, and
  target acceptance all remain release gates.

## HISTORICAL / SUPERSEDED governance snapshot — 2026-09-09

> Historical interval snapshot for `main@6e0d12c`; superseded by the
> 2026-09-11 snapshot above wherever it states current truth.

- Audited integrated source baseline: exact `main@6e0d12c0023068456b97fc9c857a3538ca612421`.
  This documentation descendant is not a self-referential source hash. Overall
  release/full-access state remains `BLOCKED` / `NOT_READY`; no phase or release
  gate is advanced.
- Protected Shadeform mutation-environment source is merged through accepted
  `c8c28a9`. Mutation commands default to ignored
  `.secrets/shadeform.env`; the operator must first create a nonsymlink,
  real-UID-owned parent with no group/world permission bits (`0700`
  recommended for projection, `0500` accepted for loading). The projection is
  one-way and create-once at mode `0600`. A normal project-root `.env` remains
  intentionally refused for mutations, and no secret or provider execution is
  accepted by this source/setup work.
- Metadata-only Windows/HF artifact-handoff source is accepted at `c2801ec` and
  merged by `e2e5156`. It validates the fixed external model identity, bounded
  receipt digests, package exclusion, canonical signed payload, and detached
  signature shape. The public validator has no trust anchor and always returns
  `REFUSED_NOT_ACTIVATED`; no real signed artifact, model bytes, custody,
  Windows verifier activation, or release acceptance exists.
- The inherited-descriptor ActionJournal WAL is accepted at `2dda060` and
  merged by `6e0d12c`. It supplies canonical integrity-bound frames, ordered
  replay, two-stage fsync commit, exact incomplete-tail recovery, provider-
  private proof ordering, restart tombstones, and failure-isolated cleanup.
  It does not supply the native secure descriptor owner/publication boundary,
  cross-process single-writer exclusion, authenticity/anti-rollback anchor,
  compaction, or automatic provider reconciliation. Graph mutations therefore
  remain unavailable for production use and ambiguous outcomes remain
  unresolved rather than retried or manufactured as success.
- HISTORICAL evidence for that interval's integrated delta, belonging to
  `6e0d12c` and not to current `7239b7e`: focused journal/Graph Node 210
  discovered, 209 passed, 0 failed, 1 existing TODO; handoff/release 63/63;
  env 26/26; QA-runner unit checks 27/27; conformance 11/11. Static inventory
  then recorded 152 tracked JSON files, of which 151 were strict-valid and one
  was an intentional duplicate-key hostile fixture; the host import graph was
  recorded as 29 modules/71 relative edges/0 cycles. Safe QA then discovered
  59 tests with 0 missing/unknown and 65 records (1 PASS/64 expected SKIP), so
  overall QA remained `BLOCKED`. Current numbers are in the 2026-09-11 snapshot
  above.
- The broader Node 350 total/349 passed/0 failed/0 skipped/1 known TODO and
  Python 663/663 across 30/33 safe files are historical evidence from
  `d195235b6a370d377785b6340b15ebc8e47585e3`, not current `6e0d12c` proof.
  Historical evaluation results 13/32 and 28/32/11/34 also remain separate and
  do not establish model/tool acceptance.
- Current evaluation identity remains 33 tools/37 cases with `max_cases=64`
  ceiling semantics. Remote execution is false; only two no-model canary probes
  are permitted, with no fall-through or direct execution. External salvage is
  unavailable, legacy lifecycle is non-green, and ledger preflight remains
  `SAFE_TO_MIGRATE_NOW=NO`; bookkeeping is not spend authorization.
- Previously exposed credentials, including the leaked HF token, require
  source-side rotation/revocation before reuse. Missing real signed artifact
  custody and model-quality evidence, provider accounts/consent, native
  compile/secure owner/broker evidence, exact Windows hardware/backend receipt,
  live tool evidence, and target acceptance remain release gates.

The older status bullets below are retained as historical interval evidence and
are superseded by the 2026-09-11 snapshot wherever they state current truth.

- Overall release/full-access state: `BLOCKED` / `NOT_READY`
- Authoritative source baseline: `main@fa5aa38c806ba98d269ce304325e178416584bbe`
- Working source-hardening stream: Phase 6
- Formal gate state: Phase 0 `IN_PROGRESS` and unapproved; Phases 1–7 have
  incomplete/unapproved evidence; Phase 8 is `BLOCKED`
- Build ID convention: `lae-<UTC YYYYMMDDTHHMMSSZ>-<12-char source SHA>-<profile>`
- Model source: official `Qwen/Qwen3.5-9B` repository on Hugging Face at
  revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`
- Deployable artifact: project-produced text-only `Q4_K_M` GGUF; weights remain outside Git and release packages
- Expected technical artifact identity: `Qwen3.5-9B-Q4_K_M.gguf`, exactly
  5,629,109,088 bytes, SHA-256
  `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`.
  This identity is a verifier/runtime constraint, not approval of any ignored
  local file. Local bytes are unapproved and have not been revalidated in this
  governance interval; custody, transfer acceptance, and the corporate
  approval reference remain unset.
- Backend ladder: CPU mandatory; Vulkan candidate; SYCL experimental
- Critical path: contracts → fixture vertical slice → controlled model artifact → real CPU slice → tools → hardening/release
- Shadeform policy: mutation credentials use protected `.secrets/shadeform.env`; the read-only catalogue may separately accept an explicit project-root `.env`. Lifecycle remains ownership-bound with read-only catalogue before create, cost preflight, provider backstop longer than run, salvage before teardown, and no idle instance
- Known target: Dell Intel Core Ultra 7 vPro Enterprise-class platform; integrated `Intel Graphics` only, driver `32.0.101.8247`, 32 GB memory reported at 5600 MT/s, motherboard `039NNG A00`. Exact CPU SKU, GPU PNP/device ID/shared memory, OS/Vulkan facts, and measured available-memory topology still require the read-only receipt, so accelerated target promotion and final Phase 8 acceptance cannot yet be claimed
- Security note: `coordination/SECURITY_INCIDENTS.md` records the legacy reference credential exposure and SI-002's stopped local transfer incident; both require source-side credential rotation/revocation. No credential value, URL, or secret is recorded here.

## Source, evidence, and gate truth

- Source state is tracked independently from evidence and release approval. A
  merged implementation is not a runtime receipt, an independent audit, or a
  gate approval; no Phase gate is advanced by this refresh.
- TOOL-032's action-journal core is merged from `55f3dfd` by `65decba`; the
  inherited-descriptor WAL is accepted at `2dda060` and integrated by
  `6e0d12c`. The WAL removes post-bootstrap pathname lookup and provides
  bounded durable integrity/recovery semantics, but production remains blocked
  until a native secure descriptor owner supplies durable publication,
  single-writer exclusion, and anti-rollback authority, and provider recovery
  can reconcile ambiguous outcomes. The dormant Win32 descriptor-WAL bootstrap
  accepted at `e4ca09b` and merged by `4de01f7` now supplies a source-only
  secure create/trusted reopen, single-writer lease, and one-shot
  non-inheritable handoff for `action-journal-v2.wal`, but it is uncompiled and
  unlinked, so it does not close that boundary: native secure owner
  publication, an authenticity/anti-rollback anchor, compaction, a reviewed
  launcher, MSVC compile, and exact-target tests all remain absent.
- The protected Shadeform mutation-env layout is source-accepted at `c8c28a9`.
  It requires explicit owner-private `.secrets/` setup and does not authorize a
  provider mutation, credential use, remote run, or spend.
- The Windows artifact-handoff contract/validator is accepted at `c2801ec` and
  integrated by `e2e5156`. Its public path is deliberately activation-refused;
  real signed receipts, an approved external trust anchor, custody, Windows
  verifier integration, model verification/load, and release acceptance remain
  absent.
- Eight independently source-reviewed Windows/runtime boundaries are on `main`:
  inert Windows read-only filesystem source by `1741c86`, inert hardware-
  attestor source by `e579d49`, inert journal-helper/transport source by
  `645f348`, inert release-tree verifier source by `2ec9c44`, inert supervisor-
  authority source `8c34cca` by `9f6bbb6`, inert clipboard source
  `04d6860`/`393189f` by `0622713`, test-only journal client `dc29ced` by
  `4b8e737`, and dormant supervisor-owned process transaction `64b3947` by
  `ca2d893`. Recorded focused evidence was 29/29 client plus 26/26 protocol,
  56/56 process-transaction source plus 2/2 inventory, 24/24 supervisor, and
  26/26 clipboard plus 2/2 inventory checks.
  These sources remain unlinked, uncompiled, absent from production activation/
  package paths, and explicitly `NO` for production and target execution.
- Process implementation candidate `6167ef6` remains rejected and unmerged.
  The later merged process transaction remains unreachable: its adapter is
  `nullptr`/unrecovered, with no public launch/package/activation or product/
  runtime CMake linkage. The dormant `windows-process-authority` v1.0.0
  contract and private move-only `LaunchAuthority` accepted at `cfe8136` and
  merged by `7239b7e` narrow B-005 in ownership and shape only; they supply no
  launch mechanism, never re-derive identity from a live handle at consume
  time, build no command line, open no path, sit outside the product CMake
  graph, and have never been compiled by any executed target.
  `kLaunchAuthorityAvailable` and every trust/containment/confinement gate stay
  false. Its only CMake presence is the default-off,
  unconfigured/unbuilt static target `lae_compilecheck_windows_supervisor`,
  which compiles `authority.cpp` with `process_transaction.inc` marked
  `HEADER_FILE_ONLY`; `SAFE_TO_COMPILE` remains unknown/`NO`.
  Rejected supervisor predecessor `81cfd79` and clipboard predecessor
  `0ba98d5` are historical and were superseded by the inert merged source above;
  neither predecessor nor either merged source confers an executable capability.
- The inert Win32 journal-storage boundary is source-merged from `d0ed670` by
  `3d46ccb`; the journal container source is source-merged by `16b4b0e`.
  Even with the inert helper source merged by `645f348`, test-only client
  merged by `4b8e737`, descriptor WAL merged by `6e0d12c`, and the dormant
  Win32 descriptor-WAL bootstrap merged by `4de01f7`, the native secure owner
  publication, production bridge/package wiring, compile, authenticity/
  anti-rollback anchor, compaction, reviewed launcher, and target evidence
  remain absent. The bootstrap adds four additive source-only status codes to
  `contracts/action-journal-storage/v0.1.0.json` under retained version
  `0.1.0`, recorded as ICR-RUN-WDJB-001 and ADR-0004.
- The external lifecycle hardening is source-merged at `91de464`. Remote
  execution remains disabled by the source guard (`REMOTE_EXECUTION_ENABLED=False`),
  and no approved/committed cost-ledger genesis or new provider run is claimed.
- Graph read tools `ed9d1cb` are merged on `main` by `c2154ba`. Graph
  reconciliation source `b4702a5` is merged on `main` after two
  independent source-safety approvals and a 96-pass mocked integration run.
  Graph restart reconciliation is accepted at `4280e95` and merged by
  `61c9475`; it adds bounded automatic completion only for durably
  acknowledged, newly account-bound `mail.create_draft` records proved by a
  fresh unique exact provider `GET`, and it removed the seam's ability to
  escalate to `unknown_manual`. Its startup trigger is operationally inert
  because tokens are memory-only, manual `POST .../reconcile` is still HTTP
  501, and `reconciling` records have no automatic resolution path. The
  `listSentForDigest` collection-projection defect is no longer merely
  plausible: it was confirmed against the pristine base and repaired by the
  sent-mail proof projection slice, accepted at `26beb82` and merged by
  `263f114`, which shares the bounded per-message retrieval (`collectMailProof`
  — one folder ID page plus at most 20 exact GETs per call), refuses
  absent/malformed markers before any request, types inconclusive results, and
  adds a `mail.send_draft` proof deadline. Its own follow-up is
  `GRAPH-SENT-PAGINATION-PRECHECK`. None of this establishes live Graph
  readiness; production action dispatch and live-provider evidence remain
  unavailable.
- The Windows filesystem refusal boundary is source-merged at `7cea137`.
  Windows filesystem mutations and helper-backed reads are refused; this is a
  safety boundary, not Windows functionality or target acceptance.
- The plan-only QA runner is source-merged at `d704b816`. It classifies unsafe
  work as `SKIP`/`UNPROVEN` and always reports release `BLOCKED`; it is not a
  substitute for the missing native/model/provider/Windows receipts.
- The bounded action-journal wire protocol is source-merged at `3746421`.
  Later inert container, storage, helper, and test-only client slices do not
  add an activated trust anchor, production transport/import, packaging, or
  production availability.
- Browser reconciliation source `fc317fce023f9364e7f19b69a700124d1936f8ca`
  is merged by `976aeff` after independent source/mock review. The rejected
  predecessor `5dc2ad2` remains historical. No live browser, Windows process,
  approved-executable, or target evidence exists, so live readiness is not
  claimed.
- Copilot source hardening `c41b97b` is merged by `a39a09f`, but Copilot is
  globally omitted/unavailable in production and has no live evidence. It
  provides no production or live capability on `main`.
- The non-accepting hardware diagnostic receipt is source-merged by `a4c5f3b`;
  it cannot emit target acceptance and its PowerShell collector remains a
  refusal stub. The default-off Windows compile-check graph was repaired by
  `9f20fae` and expanded from audited handoff `c9ae88d` by `fa5aa38` to six
  isolated static targets. Its recorded scoped evidence is 13/13 plus 2/2
  inventory checks; it was not configured or built, so `SAFE_TO_COMPILE`
  remains unknown/`NO` and production/target remain `NO`.
- The strongest recorded general remote tool evaluation is 28/34 on the
  retired 11-tool/34-case canary fixture (receipt
  `artifacts/qwen35-9b/remote-eval-20260904-j/eval-receipt.json`, which is
  git-tracked and records `metrics.passed = 28` against `metrics.case_count =
  34`), below its gate and not on the current profile. The earlier 27/34
  figure understated that receipt. The later production-profile result is
  13/32 and failed that profile; these are distinct results, both were
  measured on retired fixtures on CUDA/A100, and neither establishes
  model/tool readiness. The current 33-tool/37-case shipping profile has no
  recorded score.

## Check-in cadence

Each lane updates its session packet before work, at material contract discoveries, at review readiness, and at blockers. Each lane owns at most one primary and one small secondary task.
