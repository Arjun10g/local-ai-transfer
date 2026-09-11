# Program Status

## Current governance snapshot — 2026-09-11 (refresh v5)

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
