# Blockers

## Current governance reconciliation — 2026-09-11 (refresh v5)

- Exact audited integrated source baseline is
  `main@263f11413d1746044a6cc13062ad2b1f821c4d11`, the last source merge. The
  docs descendants `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f` and
  `1b22a40e21dce0ebc903bc3c2024ab162adc2d56` and this refresh are
  documentation descendants, not self-hashes. The previous baseline `7239b7e`
  is historical and superseded. Release/full access remains `BLOCKED` /
  `NOT_READY`, and no blocker state below changes.
- Two slices were integrated this interval, each independently S0/S4
  source-reviewed and returning `ACCEPT_FOR_MERGE` on the first pass with
  NOTE/MINOR findings only: the frozen descriptor WAL contract (`9f136a0`,
  merged `237d59b`) and the Graph sent-mail proof projection repair
  (`26beb82`, merged `263f114`). Neither adds compile, live, provider,
  production, Windows, or target evidence.
- The descriptor WAL contract freezes
  `contracts/action-journal-descriptor-wal/v0.1.0`, the separate contract
  ADR-0004 requires for the dormant v2 boundary, statically bound to the C++
  and Node constants (48-byte header, 32 MiB) and to the derived v2 status set
  (26 v2 codes against 25 v1, with `v1 ∪ v2 ∪ {platform_unavailable}` equal to
  the 30-code storage set — Sol accepted that corrected invariant because
  `platform_unavailable` is declared but emitted by no function body).
  `contracts/action-journal-storage/v0.1.0.json` is byte-identical to `main`.
  Review item R4 is closed as a documented, test-pinned accepted limitation
  resting on a cost/ordering trade rather than an impossibility: Win32 offers
  no operation that narrows an open handle's own access, and `ReOpenFile` with
  reduced access collides with the retained handle's exclusive
  `FILE_SHARE_READ` reservation. The source note concedes a
  duplicate-down-and-close would shrink the surviving handle's rights and
  argues the cost, the extra `DuplicateHandle` being itself fallible and
  landing after publication and `discard.disarm()`. No C++/Node/CMake/package change; the boundary stays
  uncompiled and unlinked.
- Protected `.secrets/shadeform.env` projection/setup source (`c8c28a9`), the
  metadata-only Windows/HF artifact handoff (`c2801ec`, merged by `e2e5156`),
  and the inherited-descriptor ActionJournal WAL (`2dda060`, merged by
  `6e0d12c`) remain source-present. They do not establish credential approval,
  provider execution, signed artifact custody/trust, native Windows authority,
  production activation, or target readiness.
- The WAL has descriptor-bound durable integrity/recovery behavior, and the
  earlier bootstrap adds a dormant, uncompiled Win32 secure create/trusted
  reopen, single-writer lease, and one-shot non-inheritable handoff for
  `action-journal-v2.wal`, but native secure owner publication, an
  authenticity/anti-rollback anchor, compaction, a reviewed launcher, and the
  HANDLE-to-CRT child bridge are still absent. The artifact handoff's public
  validator always returns `REFUSED_NOT_ACTIVATED` pending real signed
  artifacts and an approved trust anchor. Graph mutation ambiguity outside the
  narrow restart path remains fail-closed.
- ICR-RUN-WDJB-001 is Sol-approved as an additive source-only extension: four
  additive status codes in `contracts/action-journal-storage/v0.1.0.json` with
  version `0.1.0` retained, merged at `4de01f7`, generalized by ADR-0004, and
  now carrying a dated note recording that the separate-contract alternative
  was taken. Independent source review is complete; formal gate approval is
  still required.
- Current reproduced evidence is `npm test` 432 tests/430 pass/0 fail/0
  cancelled/1 skipped/1 todo; native static `Ran 309 tests` OK, of which the
  narrower Windows-only pattern is `Ran 289 tests` OK — 309 is 289 plus the 20
  tests of the new descriptor-WAL contract suite; QA safe-runner `Ran 20
  tests` OK. Static inventory is 154 tracked JSON files (153 strict-valid
  under a duplicate-key-rejecting parser plus one intentional hostile fixture)
  and a host import graph of 29 modules/67 unique relative import edges/0
  unresolved/0 cycles. QA has 64 discovered, 0 missing/unknown, and 70 records
  (1 PASS/69 expected SKIP); overall QA remains `BLOCKED`. The prior `7239b7e`
  numbers (Node 426/424, native static 289, JSON 153/152, QA 62 discovered/68
  records) and the older `6e0d12c` focused numbers (Node 210/209,
  handoff/release 63/63, env 26/26, QA-runner 27/27, conformance 11/11, JSON
  152/151, imports 29/71, QA 59 discovered/65 records) are historical.
- Broader Node 350/349/0/0/1 TODO and Python 663/663 across 30/33 safe files
  are explicitly historical `d195235` evidence, not current `263f114` proof.
  Remote execution remains false, ledger migration remains
  `SAFE_TO_MIGRATE_NOW=NO`, and previously exposed credentials—including the
  leaked HF token—require source-side rotation/revocation before reuse.
- Repository housekeeping, recorded for traceability and advancing nothing: on
  2026-09-11 Sol had 112 worktrees inventoried and 103 clean, stale worktree
  directories removed, with no branch ref deleted (120 `luna/*` before and
  after) and `git worktree prune` not yet run. Nine were kept — 2 lanes active
  at the time, 6 holding ignored `experiments/runtime/` state not recoverable
  from git, and 1 (`wt-model-performance`) holding a `.env` — and those 7 need
  an operator decision. No secret was read or printed.

## B-001 — Exact target receipt incomplete

- Fact: operator-supplied partial facts identify a Dell Intel Core Ultra 7 vPro Enterprise-class platform, one integrated `Intel Graphics` adapter with no discrete GPU, display driver `32.0.101.8247`, 32 GB DDR5-class memory reported at 5600 MT/s, and motherboard `039NNG` revision `A00`. No redacted `hardware-receipt.json`, exact CPU SKU, GPU PNP/device ID, usable/shared GPU memory, Windows build, Vulkan capability, or measured available-memory topology is present.
- Impact: Intel backend promotion and Phase 8 target acceptance cannot complete.
- Workaround: run the implemented read-only probe, use Core Ultra 7 as the CPU-family matching input, keep CPU mandatory, and label all GPU Shadeform machines as directional analogs until the remaining adapter/runtime fields are captured.
- Needed from: target operator, after S2/S4 approve the probe.
- State: OPEN; does not block fixture, model-build, CPU, host, tool, or packaging work.

## B-002 — Corporate approval references unavailable

- Fact: repository, model-transfer, signing, scanning, and live-provider approval identifiers have not been supplied.
- Impact: formal enterprise release sign-off and live provider enablement cannot complete.
- Workaround: use explicit `UNASSESSED` states, synthetic data, HF source only on Shadeform, and keep live web/provider behavior disabled by default.
- Needed from: user/organization before release sign-off.
- State: OPEN; does not block implementation or non-sensitive evidence.

## B-003 — Production external-tool chain lacks activated durable authority and live proof

- Fact: production model evaluation proves only tool proposal/arguments. The
  hostile external-tools harness uses injected Graph/CDP/Copilot fakes and does
  not execute a real account, browser, Copilot service, or Windows process.
  Disabled/unconfigured provider definitions are now withheld from the model,
  and the reviewed action-journal/controller barrier is source-merged
  (`55f3dfd`, merge `65decba`). The descriptor-backed WAL accepted at
  `2dda060` and integrated by `6e0d12c` supplies canonical append/replay,
  crash-tail recovery, restart tombstones, and provider-proof ordering once an
  already-authoritative descriptor is injected. It does not securely acquire,
  publish, exclusively own, compact, or anti-rollback-anchor that descriptor,
  and it does not automatically reconcile provider ambiguity. Microsoft Graph
  reconciliation source is merged at `b4702a5` after two independent source
  audits but has no live-provider evidence or approval. Graph restart
  reconciliation source is accepted at `4280e95` and merged by `61c9475` after
  an independent S0/S4 review with a repair round; it permits bounded automatic
  completion only for durably acknowledged, newly account-bound
  `mail.create_draft` records with a fresh unique exact provider `GET` proof.
  The bound is per candidate: at most 8 acknowledged candidate records, each
  checked with one bounded Drafts ID page and at most 20 exact proof GETs, an
  upper bound of 8 x 21 = 168 provider requests per pass, all under the pass
  deadline. Its budget-aware
  inconclusive-safe in-flight retrieval means the seam can no longer escalate a
  record to `unknown_manual`. Its limits are material: the startup pass is
  operationally inert because tokens are memory-only, so only the
  post-authentication trigger can complete a record; the pass is bounded but
  not cancellable, because both production call sites invoke it with no signal
  and it is limited only by its own 30 s deadline rather than host shutdown or
  the emergency stop; manual
  `POST .../reconcile` remains HTTP 501; `reconciling` records still have no
  automatic resolution path and accumulate against the 256-record active cap;
  and no live Microsoft account or provider evidence exists. The
  `listSentForDigest` collection-projection defect is no longer plausible but
  confirmed at code level and repaired: at the pristine base it asked a Sent
  Items collection query for `internetMessageHeaders` and hard-required that
  property on every item, so the sole completing state `unique_sent_item` was
  unreachable in code. On the documented Graph behavior that
  `internetMessageHeaders` is returned only on single-message projections — a
  premise that cannot be confirmed without a live account; the repair is
  fail-closed under either behavior — `mail.send_draft` could never complete
  against a real account (a safe false negative, never a false completion).
  Independently of that premise, a transport fault on the query escalated the
  record to `unknown_manual`.
  The repair, accepted at `26beb82` and merged by `263f114`, shares the
  bounded per-message retrieval: `collectMailProof` issues one folder ID page
  plus at most `MAX_MAIL_PROOF_CANDIDATES` = 20 exact GETs per call, refuses
  absent or malformed markers before any request, returns typed inconclusive
  results instead of throwing, adds a `mail.send_draft` proof deadline with a
  typed `sent_proof_budget_exhausted` exit, and preserves cancellation. Three
  limits remain, all pre-existing and unchanged by that slice: the pre-send
  Sent Items `@odata.nextLink` refusal still blocks every send for a mailbox
  holding more than 50 sent items (follow-up
  `GRAPH-SENT-PAGINATION-PRECHECK`); a pre-dispatch provider refusal still
  lands as `unknown_manual` even though nothing was sent, because the journal
  cannot distinguish "provider refused before writing" from post-dispatch
  ambiguity; and three pre-proof requests in the send branch remain uncapped,
  so the seam is bounded while the tool as a whole is not. Browser/Copilot proposal state
  remains memory-only, and browser/Copilot executable paths are not bound to
  immutable file identity across preview and spawn.
- Impact: a passing model score or mocked provider run cannot establish safe
  end-to-end mail, Teams, browser-action, or Copilot readiness. After an
  ambiguous provider write, no definitive result or safe automatic retry can be
  established until provider reconciliation completes; retrying outside that
  boundary could duplicate work. A mutable executable can also change after
  preview/version inspection.
- Workaround: keep all external providers disabled by default and all writes
  confirmation-bound. Preserve per-generation active-tool filtering and the
  fail-closed journal barrier. Connect the inert storage/helper sources and
  descriptor WAL to a native secure single owner with durable publication,
  locking, authenticity/anti-rollback, bounded compaction, package wiring, and
  compile/target evidence. Complete provider-owned reconciliation and
  executable identity pinning before any write-capable live test.
- Needed from: S3 implementation, S4 independent review, and S0 gate decision.
- State: OPEN; blocks Phase 4/6 readiness and all full-access claims.

## B-004 — Current A100 provider profile is activation-unreliable

- Fact: two consecutive bounded corrected-evaluator attempts (`remote-l` and
  `remote-m`) timed out before the instance became active. Neither reached HF
  acquisition, conversion, or evaluation. Exact cleanup succeeded, the cost
  ledger is settled with zero pending reservations, and cumulative remote spend
  is `$6.767912`.
- Impact: the corrected production tool-quality result is unavailable. Further
  blind retries would spend budget without testing the model or product.
- Workaround: stop retrying this profile. Use a fresh read-only catalogue, then
  a cheaper non-A100 activation/SSH/CUDA canary with no model download. Only a
  candidate that passes that canary is eligible for the HF-backed evaluator.
- Fact (cost-planning correction, 2026-09-11): the quality comparators are
  gone. The Q8_0 and bf16 conversion outputs exist only as hashes in
  `artifacts/qwen35-9b/scan-receipt.json`, and
  `artifacts/qwen35-9b/post-cleanup-receipt.json` records `Qwen3.5-9B-Q4_K_M`
  as the only remaining GGUF. The ≥95% quality-retention gate
  (`execution/ACCEPTANCE_CRITERIA.md:215`) therefore has no reachable
  reference artifact, so any future quality run must budget a full Shadeform
  re-conversion rather than an evaluation alone. Any candidate plan written
  against this blocker must carry that larger cost, and the decision should be
  taken before a profile is chosen.
- Needed from: S2 candidate plan, S4 lifecycle review, and S0 authorization.
- State: OPEN; does not block local mocked/source hardening.
- Source/evidence correction: lifecycle authority hardening is source-merged
  at `91de464`, but `REMOTE_EXECUTION_ENABLED=False` remains binding and no
  approved/committed cost-ledger genesis exists. The merge neither authorizes
  a live retry nor supplies model-quality evidence.
- Environment correction: mutation/recovery CLIs now default to protected
  `.secrets/shadeform.env` after accepted `c8c28a9`; project-root `.env` remains
  a separate read-only-catalogue input and is refused for mutations. An operator
  must create the protected owner-private directory and project the selected
  keys offline; source support is not credential approval or spend authority.

## B-005 — Windows process/application launch boundary is not identity-pinned

- Fact: `process.run_allowlisted` repeats canonical pathname checks before
  spawn, but Node cannot hold a deny-write/delete Windows image handle through
  `CreateProcess`. `app.open`, `browser.open_url`, and clipboard subprocesses
  currently spawn configured/bare executable paths without equivalent canonical
  identity checks and inherit the host environment. The visible browser opener
  uses bare `rundll32.exe`; the application config does not require an absolute
  path. These boundaries are mocked on non-Windows hosts only. An independently
  reviewed native broker contract and source skeleton is merged, but it contains
  no process-creation implementation, has empty trust/containment/confinement
  activation prerequisites, is excluded from build/package/host integration,
  and is explicitly `QUARANTINED` / `NOT_READY`. Later process implementation
  `6167ef6` remains rejected and unmerged. The rejected supervisor-authority
  predecessor `81cfd79` is superseded by independently accepted inert source
  `8c34cca`, merged by `9f6bbb6` after 24/24 focused source checks. The rejected
  clipboard predecessor `0ba98d5` is superseded by inert repair `04d6860` and
  audited handoff `393189f`, merged by `0622713` after 26/26 focused source plus
  2/2 inventory checks. Dormant supervisor-owned process transaction `64b3947`
  is merged by `ca2d893` after 56/56 source plus 2/2 inventory checks, but its
  durable adapter remains null/unrecovered and it has no public launch API,
  CMake, or package path. Dormant launch-authority contract
  `windows-process-authority` v1.0.0 and a private, noncopyable, move-only
  `LaunchAuthority` are merged by `7239b7e`; they replace the previous public
  POD proof with supervisor-owned `UniqueHandle` ownership, bind executable/
  working-directory/token/Job/cancellation handles to recorded identity values
  plus operation ID/generation/nonce, require the absolute-manifest-path-binding
  and no-reparse-component predicates on both bound file identities, carry an
  ordered argument-vector policy with one fixed `CommandLineToArgvW` escaping
  algorithm and required round-trip parse verification, add a cancellation state
  machine whose generations are issuer-derived and strictly monotonic and whose
  `unknown_manual` state is terminal, and close a latent gap by adding
  `kNestedJobPolicyProven` to `kProductionAvailable`. They supply no mechanism:
  the Windows process-creation API accepts a path rather than a handle, no
  image-section retention, `NtCreateUserProcess`, `GetFinalPathNameByHandle`
  re-derivation, or post-launch image verification exists, and
  `valid_for_admission()` only tests that declarative fields are set and that
  recorded values are non-zero — it never re-derives identity from the live
  handle at consume time, so a mint-to-spawn executable or junction swap remains
  undetected. No command line is built, no argument is escaped, and no path is
  opened. The header is outside the product CMake graph, has no proof issuer,
  and has never been compiled by any executed target. All merged boundaries
  remain unlinked, uncompiled,
  activation-gated false, and explicitly unavailable for production/target use.
- Impact: a path replacement/search-path race can change executed bytes, and a
  launched application can inherit provider credentials or other host secrets.
  Current process/app/browser/clipboard tests do not establish safe laptop
  execution.
- Workaround: keep these launch-capable tools disabled in the Windows product
  profile. Complete the quarantined skeleton with an authenticated supervisor,
  pre-child containment, real least-privilege confinement, identity-pinned
  executable handling, and minimal environment; independently review its host
  integration, then run real Windows cancellation/orphan/path-replacement
  acceptance.
- Needed from: S1/S3 implementation, S4 security review, and target operator.
- State: OPEN; blocks Phase 3/4/6 readiness and full laptop-control claims.

## B-006 — Model/tool quality and artifact acceptance remain below gate

- Fact (corrected 2026-09-11): the model has NOT been tested against the
  profile it must ship against. The strongest recorded general remote tool
  evaluation completed at 28/34, not 27/34 — the receipt
  `artifacts/qwen35-9b/remote-eval-20260904-j/eval-receipt.json` is
  git-tracked and records `metrics.passed = 28` against `metrics.case_count =
  34` (`metrics.failed = 6`, `metrics.errors = 0`, canary `tool_count = 11`,
  status `completed_with_failures`). That result is on the retired
  11-tool/34-case canary fixture, below its gate, and not on the current
  profile. A separate production-profile run completed at 13/32 with 19
  failures and therefore failed that profile; it too used a retired fixture,
  the 28-tool/32-case profile. Both scores date from 2026-09-04 and were
  measured on CUDA/A100 hardware — never on CPU and never on Intel Vulkan,
  which are the mandatory and candidate target backends. Subsequent corrected
  attempts did not reach evaluation because provider activation timed out.
- Fact: the current production profile is 33 tools and 37 cases, fixture
  `tests/model/production_tool_call_eval.json` with SHA-256
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`. It has
  NO recorded score. The model's score on the shipping profile is unknown, not
  merely below gate, and no comparison between the retired-fixture results and
  this profile is admissible.
- Fact: the quality comparators were deleted. Q8_0 and bf16 exist only as
  hashes in `artifacts/qwen35-9b/scan-receipt.json`;
  `artifacts/qwen35-9b/post-cleanup-receipt.json` records `Qwen3.5-9B-Q4_K_M`
  as the only remaining GGUF. The ≥95% quality-retention gate
  (`execution/ACCEPTANCE_CRITERIA.md:215`) consequently has no reachable
  reference artifact, and any future quality run must budget a full Shadeform
  re-conversion — a change to B-004 candidate cost planning.
- Fact: the quality corpus does not exist yet.
  `model/quality-eval/quality-fixture-spec.json` defines 13 categories whose
  `minimum_cases` fields total 1,180, and only 3 `fixture_cases` are present.
  Authoring it needs no model, no spend, and no credential, so it is unblocked
  and tracked as `UNCLAIMED` task `MODEL-QUALITY-CORPUS-001`.
- Fact: a local run on 2026-09-11 — a Sol-authorized single development-only
  exception to the workaround below, granted on the basis that the local bytes
  were re-verified byte-exact against the pinned identity — ABORTED before any
  model load, on two independent stop conditions. (1) Host memory: the
  development host is an 8 GiB Apple M2 MacBook Air with roughly 94% of swap
  in use and `kern.memorystatus_vm_pressure_level` at WARNING before the run
  started, against an artifact needing about 5.2 GiB resident. (2) A stale
  prebuilt engine: `out/build-real/native/lae-engine`, built 2026-09-04, is
  532 commits behind `fdfed07` with 79 of those touching `native/`, and
  rejects the current `serve` flags with `unknown argument`. No score was
  produced, no bytes were loaded, and nothing was bound or downloaded. The one
  positive result is that the model identity re-verified exactly at
  5,629,109,088 bytes and SHA-256 `c654bc40…68873b`. The three `real_model`
  tests fail at argument parsing against the stale binary. Conclusion: no
  model evidence can be produced on this laptop; a real measurement requires a
  rebuild from current source on a machine with at least 16 GiB — i.e.
  Shadeform — once the human-only blockers clear:
  `REMOTE_EXECUTION_ENABLED = False`
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
- Fact: the product verifier pins `Qwen3.5-9B-Q4_K_M.gguf` to exactly
  5,629,109,088 bytes and SHA-256
  `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`.
  That technical identity does not approve ignored local bytes or establish
  transfer custody. Local revalidation, an approved transfer receipt, and the
  chain of custody remain unset.
- Fact: accepted source `c2801ec`, integrated by `e2e5156`, adds a bounded
  metadata-only artifact-handoff schema/validator. Its public path has no trust
  anchor and always returns `REFUSED_NOT_ACTIVATED`; the private future seam can
  validate an approved signature but never activates or reads model bytes.
  No real signed handoff, approved key, trust-store integration, or Windows
  release-verifier receipt is present.
- Impact: neither model/tool quality nor the deployable local artifact is
  accepted for release.
- Workaround: keep the known identity and activation-refused handoff as
  fail-closed constraints; do not load or distribute local bytes until real
  signed receipts, an approved trust anchor/transfer, and fresh verification
  evidence exist. Resume quality work only through the separately authorized,
  lifecycle-gated route.
- Needed from: S0/S2 artifact and quality approval, S4 evidence review, and the
  target operator for the approved transfer/acceptance route.
- State: OPEN; blocks Phase 1/2/4/7/8 and every readiness claim.
