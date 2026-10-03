# Release Gates

## CURRENT GATE SET — 2026-09-14 (v8, local-first rescope under ADR-0007, ratified 2026-09-16)

**This section is current truth. The Phase 0-8 set below it is superseded
history and gates nothing.** The operator rescoped the program on 2026-09-14
from an enterprise Windows release to a personal local assistant: a local model
on the Dell laptop, Ollama-like to run, with terminal access, secure. ADR-0007
records the decision and, more importantly, what it surrenders.

**Target:** the Dell laptop running Windows, 32 GiB RAM or more. The 8 GiB
development Mac is explicitly NOT an acceptance machine — it is measured at
~200 s per evaluation case and pages the 5.6 GB artifact from disk. Evidence
produced on the Mac or on a rented A100 is evaluation evidence, never
acceptance evidence.

**The burden of proof is deliberately lower than it was, and that is a
surrender rather than an achievement.** Five things are given up outright and
must never be reported as satisfied: code signing and an approved trust anchor;
a reproducible signed Windows release; hardware/driver attestation and the
exact Dell target receipt; corporate approval references; and performance
percentile evidence. Nothing will attest that the running binary is the
reviewed one. That is acceptable for one operator on their own machine and
would be disqualifying for redistribution — which is the revisit trigger.

**Containment is NOT reduced.** The model gains terminal access under this
scope, so its blast radius grows. Loopback-only binding, bearer auth, no
engine-initiated egress, tool-envelope validation, fail-closed parsing, the
action journal, and bounded output/context are all retained in full.

| Gate | State | What closes it |
|---|---|---|
| L0 — Engine runs on the target | `NOT_STARTED` | `lae-engine` configured, built and serving on the Dell/Windows target with the accepted Q4 artifact. `/healthz`, `/readyz`, `/version` and a non-streaming `/v1/chat/completions` all answer; bind is loopback-only and the bearer token is required. Evidence: a receipt produced ON the target. No cross-compilation claim and no Mac or A100 result substitutes. Note the engine has never run on Windows: this is real work, not a formality. The route to the receipt is `local/windows/` (`LOCAL-WINDOWS-TEST-KIT-001`); a MinGW cross-compile now exists and, per this row, proves nothing until it or the Visual Studio build runs on the Dell |
| L1 — Model answers usefully | `EVIDENCE_INCOMPLETE` | **Latest (2026-10-03, `j1m-eval-20261003-b`, A100, fixture `d3c4d457...25a452`, booked USD 0.437): 36/37 passed, 1 failed, 0 errors; canary passing at 5,788 prompt tokens with all 33 tools.** Single-tool end-to-end 36/37 = 97.3%, which clears the >=90% bar by three cases (the previous result cleared it by one). Both `argument_type_mismatch` cases (`prod-mail-list-001`, `prod-mail-read-state-001`) now pass; the only failure is `prod-fs-patch-001` (`missing_argument`), still undiagnosed. **This is a comparison across TWO simultaneous changes, not a controlled result:** the engine now tokenizes the chat markers as real control tokens (`ENGINE-SPECIAL-TOKENS-001`; the canary shrank 5,812 -> 5,788 tokens, the signature that the remote engine ran it) AND the scorer coerces Python-style `True`/`False` by declared parameter type (`TOOL-CALL-REMAINING-FAILURES-001`). This run cannot say which fixed the two boolean cases, or whether the tokenizer change helped anything else; a re-run with scorer coercion disabled (about USD 0.44) would separate them. One deterministic run, temperature 0, no raw output retained. **The >=95% retention comparator is still unmeasured**, so the gate stays `EVIDENCE_INCOMPLETE`; approval remains a separate decision. **Previous: 34/37 passed, 3 failed, 0 errors** (`j1m-eval-20260916-a`, A100, fixture `d3c4d457...25a452`, canary passing at 5,812 prompt tokens with all 33 tools). Against `execution/ACCEPTANCE_CRITERIA.md`: **single-tool end-to-end 34/37 = 91.9% now clears the ≥90% bar — by exactly one case** (33/37 would be 89.2%); no call was malformed and no case chose the wrong tool; `confirmation_sensitive` rose to 13/15. **The ≥95% retention comparator is still unmeasured** (comparators off), so the gate is not met and its state stays `EVIDENCE_INCOMPLETE`; gate approval remains a separate decision. **How to read the rise.** The previous result, 32/37, was measured on the superseded fixture `c75af520...` (runs `20260914-a`/`-c`, identical both times). Between the two, the tool-use policy and two tool descriptions changed (`899211c`), so this is a comparison, not a continuation, and **the rise is evidence the product's tool-use instructions improved — not that Qwen3.5-9B got better.** Per case: `prod-browser-url-001` wrong_tool now passes (description disambiguation); `prod-mail-send-001` malformed_call now passes, consistent with the no-trailing-text rule but not proof of it, since the policy change alters every case's prompt; `prod-fs-patch-001` moved from malformed_call to missing_argument — a well-formed call that omits an argument. **The JSON-literal encoding rule did NOT fix either case it targeted:** `prod-mail-read-state-001` (boolean `is_read`) and `prod-mail-list-001` (boolean + integer) still fail `argument_type_mismatch`. That diagnosis was rated high-confidence and is not borne out; whether the model ignores the instruction or the cause is elsewhere cannot be told without raw output, which is deliberately never logged. No new failures appeared. `B-006` is this gate |
| L2 — Terminal access works and is contained | `NOT_STARTED` | The any-command-with-confirmation tool, designed and reviewed under `LOCAL-TERMINAL-CONFIRM-001`. Closes when: the exact command text shown to the operator is byte-identical to what executes; confirmation cannot be bypassed, batched, defaulted to yes, or remembered across commands; a refused command executes nothing and is journalled; output is bounded and credential-screened. **The confirmation step is a security control, not a UX affordance** — under this scope it is the only thing between a prompt-injected tool call and code execution on the operator's machine, and it must be tested as such |
| L3 — Security proportionate to local use | `PARTIAL` | Most of this already exists in source and needs target evidence rather than new work: loopback binding and token auth (built), no engine-initiated egress (built), tool-envelope validation and fail-closed parsing (built, heavily tested), action-journal audit record (built), bounded output/context (built). Outstanding: the L2 confirmation control, a prompt-injection case set exercised against the terminal tool specifically, and confirmation that secrets never reach logs or receipts on the target. Windows launch identity-pinning is reduced to "the operator approves each command" rather than the attestation-grade authority `B-005` describes |
| L4 — Usable like Ollama | `NOT_STARTED` | Start, stop, model load and a chat interface the operator can actually live with on the target: a documented one-command start, model resident across turns rather than reloaded per request, and first-token latency the operator judges acceptable on 32 GiB. Deliberately judged by the operator, not by a percentile — performance evidence is among the surrendered items |

**Parked, reversible, gating nothing.** Retained in the tree and in history
because it is merged, tested source that costs nothing to keep and would be
expensive to rebuild: the Microsoft Graph tools (mail, teams) and their
live-account proof, browser automation, the Copilot tool, and the
attestation-grade Windows launch authority in its full form. `B-003` is parked
with them. Restoring any of it is a scope decision, not a repair.

**Blocker reclassification under this scope.** `B-001` (exact Dell hardware
receipt) and `B-002` (corporate approval references) leave the critical path
and become informational. `B-004` (A100 provider profile) only ever affected
paid evaluation runs, never the product, and is off the critical path. `B-003`
is parked. `B-005` is reduced to its proportionate form inside L3. **`B-006`
stays open and is now L1** — the rescope lowers assurance about provenance, not
about whether the model works.

**What this does not change.** No gate above is advanced by a source merge, a
status update, or this document. `L1` is the only gate with evidence in flight.
Four of five are `NOT_STARTED` or `PARTIAL`, and the honest summary of the
program on 2026-09-14 is that it has a well-tested source tree, a first quality
number arriving today, and no working build on its actual target.

## HISTORICAL / SUPERSEDED — the enterprise Phase 0-8 gate set

**Superseded by the v8 local-first set above, under ADR-0007 (2026-09-14). The
snapshot and table below gate nothing and are retained as history.** They
record an enterprise Windows release the program is no longer attempting. At
the moment of supersession the set stood at 0 of 9 gates approved and 6 of 6
blockers open, with the remaining work dominated by evidence no implementation
lane could produce. A future report of "L-gates green" must never be presented
as satisfying any phase below.

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

Overall release/full-access state: **BLOCKED / NOT_READY**. The states below
describe formal evidence/approval gates, not whether source work exists.

The integrated source head and the reproduced evidence are stated in the
refresh v7 block above; `c469ed1` supersedes the v6 baseline
`main@f89c1684ea051e1c9c92f944cb080d424a086f55` and the later `f4bb424`. The
paragraph and figures below are the **v6** record, retained for history and
superseded wherever the two disagree — most notably the Python suite, which
was `Ran 743 tests` at v6 and is `Ran 840 tests` at v7, and the dry-run gate,
which recorded 142 argv at v6 and 24 checks under a materially stronger host
simulation at v7.

Prior reproduced evidence, measured in the refresh v6 worktree on
2026-09-11: Python `python3 -m unittest discover -s tests -t . -p 'test_*.py'`
**`Ran 743 tests` OK**; `npm test` **432 tests / 430 pass / 0 fail / 0
cancelled / 1 skipped / 1 todo** (the single skip and single todo are the
pre-existing filesystem `KNOWN LIMITATION` pair); safe QA **`BLOCKED`** with
**70 discovered, 0 missing, 0 unknown, and 76 records (1 PASS / 75 expected
SKIP)**; the offline J1M dry-run gate **PASS** with **142 argv recorded, 0
refused, USD 0.00, 0 WARN**; the quality-corpus validator **PASS in both
default and `--require-complete` modes over 1,336 cases**; strict tracked-JSON
inventory **168 tracked / 167 strict-valid / 1 intentional hostile fixture**
(`tests/native/fixtures/windows_broker/duplicate-key.json`, duplicate key
`deadline_ms`); and a host import graph of **29 modules / 67 unique relative
import edges / 0 unresolved specifiers / 0 cycles** from 72 relative import
occurrences.

Two invocation facts belong with those numbers. First, the `-t .` root argument
is load-bearing: without it, `tests/qa/` shadows the root `qa/` package and
discovery collapses to `Ran 646 tests` with **8 module-level `ERROR`s**
(`model.test_windows_artifact_handoff`, `performance.test_j1m_dry_run`
registration, `qa.test_evidence`, `qa.test_safe_runner`,
`release.test_package_scanner`, `release.test_windows_acceptance`,
`release.test_windows_hardware_receipt_diagnostic`,
`security.test_adversarial`). Second, the dry-run gate has an operator
precondition git cannot carry: in a fresh worktree it **FAILS**
`operator_artifact_destination_is_salvage_ready` until
`chmod 700 artifacts artifacts/qwen35-9b` is applied, after which it passes.
Both were reproduced here.

The prior `263f114` numbers (Node 432/430, native static 309, JSON 154/153, QA
64 discovered/70 records), the `7239b7e` numbers (Node 426/424, native static
289, JSON 153/152, QA 62 discovered/68 records), and the older `6e0d12c`
focused numbers (journal/Graph Node 210/209, handoff/release 63/63, env 26/26,
QA-runner 27/27, conformance 11/11, JSON 152/151, imports 29/71, QA 59
discovered/65 records) are historical. Historical broad Node 350/349 and Python
663/663 evidence belongs only to `d195235`.

No model evidence is recorded against the current shipping profile. The
production evaluation fixture is 33 tools and 37 cases
(`tests/model/production_tool_call_eval.json`, SHA-256
`c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`) and has no
score. Every recorded score — 28/34 on the retired 11-tool/34-case canary and
13/32 on the retired 28-tool/32-case profile — was measured on 2026-09-04 on
CUDA/A100, never on CPU or Intel Vulkan, and neither fixture is the shipping
profile. **No run has executed.** `remote-eval-20260911-a` stopped pre-spend at
USD 0.00 with no instance created, and `remote-eval-20260911-b` — authorized
under ADR-0005 at USD 10.00 / 4 h — is `PENDING — launch-ready, awaiting
operator permission`, blocked at the tool layer by the Claude Code auto-mode
permission classifier, which is a harness control and not a project gate. USD
0.00 spent, no instance created, `.secrets/j1m/` empty. Three earlier attempts
returned typed `input_rejected` before any provider call (a relative
`--artifact-destination`, a provider backstop below the required 1.10x margin,
and the legacy cost ledger failing canonical validation); the ledger blocker is
resolved by an executed, reviewed genesis, and the other two have follow-up
rows. **No gate anticipates the run's result**, and its numbers will be a
separate addendum when they exist.

Spend authorization is not gate progress. ADR-0005 records standing user
authorization, a program hard cap of USD 50, and per-run caps; none of that
advances any gate below, and a run result that passes a threshold is evidence
to be recorded, not approval. Gate states remain a separate Sol decision.

| Gate | State | Required evidence / remaining work |
|---|---|---|
| Phase 0 | IN_PROGRESS / UNAPPROVED | Contracts, policy matrix, source pin procedure, Shadeform profile/cost preflight |
| Phase 1 | EVIDENCE_INCOMPLETE / UNAPPROVED | Fixture/source work and an activation-refused metadata handoff exist; real signed artifacts, approved trust anchor, controlled artifact acceptance/custody, and oracle evidence are missing |
| Phase 2 | EVIDENCE_INCOMPLETE / UNAPPROVED | Real accepted-artifact CPU inference, oracle parity, and quality gate. Fixture-identity gap: every recorded score was measured on 2026-09-04 against retired fixtures (11-tool/34-case canary 28/34; 28-tool/32-case profile 13/32) on CUDA/A100, never CPU or Intel Vulkan; the current 33-tool/37-case shipping profile (`tests/model/production_tool_call_eval.json`, sha `c75af520…c8ac6c`) has no recorded score, so the model's score on the profile it must ship against is unknown rather than merely below gate. The ≥95% retention comparator is now **reachable but unmeasured**, correcting the earlier "unreachable" wording: the Q8_0 and bf16 outputs survive only as hashes in `artifacts/qwen35-9b/scan-receipt.json` because the cleanup tail deletes them at the end of each run, but both are rebuilt in every eval run (`scripts/j1m_runner.py:1420-1422`) and the merged comparator slices make that tail deferrable (`--retain-comparators` / default-OFF `--evaluate-comparators`), so a retention number costs a marginal USD 0.8325–1.3725 inside one budgeted run rather than a separate full re-conversion — but only if the runtime clock gate admits the comparator phase: `fits_static_worst_case` (`scripts/j1m_orchestrator.py:914`) is computed False for every selection, the frequently quoted `raises_authorized_cost: False` (`:917`) is a hardcoded literal rather than a verdict, and `_comparator_clock_available` (`:921`) can refuse the phase at run time with typed `comparator_clock_insufficient` (`:949`), so a budgeted run can pass the Q4 evaluation, spend the money, and still return no retention number. No retention number exists yet. The quality corpus is merged and complete (1,336 cases, 13 categories, splits 271/250/815, deterministic `match` on all 555 scoring propositions, validator PASS in both modes) but a fixture is not a score and it advances nothing here; two audit residuals must close before it is used to produce a retention or parity number. A 2026-09-11 local development attempt aborted before any model load on host memory and a 532-commit-stale prebuilt engine, producing no score. The run authorized under ADR-0005 (`remote-eval-20260911-b`) has **not executed**: it is launch-ready and awaiting operator permission, blocked by a harness permission control rather than any project gate, at USD 0.00 with no instance created. Nothing here anticipates its result |
| Phase 3 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source sessions/local tools, the descriptor WAL, and a dormant uncompiled Win32 descriptor-WAL bootstrap (secure create/trusted reopen, single-writer lease, one-shot non-inheritable handoff) exist; native secure owner publication, an authenticity/anti-rollback anchor, compaction, a reviewed launcher and HANDLE-to-CRT child bridge, the production bridge, MSVC compile, and Windows capability evidence are missing/refused |
| Phase 4 | EVIDENCE_INCOMPLETE / UNAPPROVED | Graph reconciliation/WAL source exists, now including bounded restart completion for durably acknowledged, newly account-bound `mail.create_draft` records proved by a fresh unique exact provider GET; its startup trigger is operationally inert on memory-only tokens, manual `POST .../reconcile` is still HTTP 501, `reconciling` records have no automatic resolution path, the `listSentForDigest` collection-projection defect is confirmed at code level (the Graph API premise it rests on — that `internetMessageHeaders` is returned only on single-message projections — is unverified without a live account, and the repair is fail-closed under either behavior) and repaired by the merged sent-mail proof projection slice (`collectMailProof`: one folder ID page plus at most 20 exact GETs per call, typed inconclusive results, a `mail.send_draft` proof deadline), so the sent-mail proof path now requests per-message projections; whether `mail.send_draft` can complete against a real account remains unverified (no live-account evidence; all mock probes end without completion by design) — and the unchanged pre-send Sent Items `@odata.nextLink` refusal still blocks every send for a mailbox holding more than 50 sent items (follow-up `GRAPH-SENT-PAGINATION-PRECHECK`), a pre-dispatch provider refusal still lands as `unknown_manual`, three pre-proof requests in the send branch remain uncapped so the seam is bounded while the tool as a whole is not and a slow provider can still surface as `tool_timeout` → `unknown_manual`, and live Graph accounts/consent, browser/Copilot evidence, and model quality are missing. Documentation correction only (2026-09-11): `SECURITY_AND_TOOL_POLICY.md` §10 described `process.run_allowlisted` with an `executable_id`/`arguments`/`workspace_id`/`timeout_ms` schema that no shipping component accepts; all three shipping definitions use `action_id` plus optional `parameters`, and §10 is corrected to the shipping form. No capability, schema, or gate changed |
| Phase 5 | EVIDENCE_INCOMPLETE / UNAPPROVED | Performance, caches, and exact-device acceleration evidence. Still entirely absent: no cold/warm separation, no first-token/prefill/decode/cache/state-restore breakdown, no median/p95/dispersion with sample counts, and no exact-device placement receipt. The only hardware any recorded figure was measured on is CUDA/A100, which is neither the mandatory CPU backend nor the candidate Intel Vulkan backend, so nothing recorded is admissible here. A comparator arm can now produce a same-artifact upstream-oracle parity delta inside a budgeted eval run, but that is a quality/parity measurement, not performance evidence, and none has been produced. No `duration_ms` observation from a development host is carried into this record: single-run wall clocks on one machine are not reproducible evidence |
| Phase 6 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source hardening is active and now includes a dormant `windows-process-authority` v1.0.0 contract with a private move-only `LaunchAuthority`; that authority supplies no launch mechanism, no consume-time identity re-derivation, no command-line builder, no proof issuer, and no CMake/package/host path, and security, offline proof, fuzz/soak, compile, and target evidence remain incomplete |
| Phase 7 | EVIDENCE_INCOMPLETE / UNAPPROVED | Metadata handoff validation exists but remains activation-refused; real signed artifacts/trust anchor, reproducible executable Windows release, and approved Shadeform evidence are absent |
| Phase 8 | BLOCKED | Exact Dell hardware/driver receipt and bounded target execution required |

## Governance interpretation

Source implementation, evidence, and gate approval are separate records. A
source commit or merge can establish only what is present in the source tree;
it does not establish runtime/target evidence, independent review, or release
approval. `READY_FOR_REVIEW` means review is pending, not that the product is
ready. The working Phase 6 hardening stream does not advance the formal Phase 0
gate or make earlier/later gates complete. No gate state above is advanced by a
source-only merge or status update.
