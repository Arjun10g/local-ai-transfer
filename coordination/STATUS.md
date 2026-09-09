# Program Status

## Current governance snapshot — 2026-09-09

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
- Current evidence for the integrated delta is separately scoped: focused
  journal/Graph Node 210 discovered, 209 passed, 0 failed, 1 existing TODO;
  handoff/release 63/63; env 26/26; QA-runner unit checks 27/27; conformance
  11/11. Static inventory records 152 tracked JSON files, of which 151 are
  strict-valid and one is an intentional duplicate-key hostile fixture; the
  host import graph is 29 modules/71 relative edges/0 cycles. Safe QA discovers
  59 tests with 0 missing/unknown and 65 records (1 PASS/64 expected SKIP), so
  overall QA remains `BLOCKED`.
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
are superseded by the 2026-09-09 snapshot wherever they state current truth.

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
  can reconcile ambiguous outcomes.
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
  runtime CMake linkage. Its only CMake presence is the default-off,
  unconfigured/unbuilt static target `lae_compilecheck_windows_supervisor`,
  which compiles `authority.cpp` with `process_transaction.inc` marked
  `HEADER_FILE_ONLY`; `SAFE_TO_COMPILE` remains unknown/`NO`.
  Rejected supervisor predecessor `81cfd79` and clipboard predecessor
  `0ba98d5` are historical and were superseded by the inert merged source above;
  neither predecessor nor either merged source confers an executable capability.
- The inert Win32 journal-storage boundary is source-merged from `d0ed670` by
  `3d46ccb`; the journal container source is source-merged by `16b4b0e`.
  Even with the inert helper source merged by `645f348`, test-only client
  merged by `4b8e737`, and descriptor WAL merged by `6e0d12c`, the native
  secure owner, production bridge/package wiring, compile, authenticity/
  anti-rollback, single-writer, compaction, provider-reconciliation, and target
  evidence remain absent.
- The external lifecycle hardening is source-merged at `91de464`. Remote
  execution remains disabled by the source guard (`REMOTE_EXECUTION_ENABLED=False`),
  and no approved/committed cost-ledger genesis or new provider run is claimed.
- Graph read tools `ed9d1cb` are merged on `main` by `c2154ba`. Graph
  reconciliation source `b4702a5` is merged on `main` after two
  independent source-safety approvals and a 96-pass mocked integration run.
  This does not establish live Graph readiness; production action dispatch and
  live-provider evidence remain unavailable.
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
- The strongest recorded general remote tool evaluation is 27/34, below its
  gate. The later production-profile result is 13/32 and failed that profile;
  these are distinct results and neither establishes model/tool readiness.

## Check-in cadence

Each lane updates its session packet before work, at material contract discoveries, at review readiness, and at blockers. Each lane owns at most one primary and one small secondary task.
