# Program Status

- Overall release/full-access state: `BLOCKED` / `NOT_READY`
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
- Shadeform policy: project `.env`, ownership-bound lifecycle, read-only catalogue before create, cost preflight, provider backstop longer than run, salvage before teardown, no idle instance
- Known target: Dell Intel Core Ultra 7 vPro Enterprise-class platform; integrated `Intel Graphics` only, driver `32.0.101.8247`, 32 GB memory reported at 5600 MT/s, motherboard `039NNG A00`. Exact CPU SKU, GPU PNP/device ID/shared memory, OS/Vulkan facts, and measured available-memory topology still require the read-only receipt, so accelerated target promotion and final Phase 8 acceptance cannot yet be claimed
- Security note: `coordination/SECURITY_INCIDENTS.md` records the legacy reference credential exposure and SI-002's stopped local transfer incident; both require source-side credential rotation/revocation. No credential value, URL, or secret is recorded here.

## Source, evidence, and gate truth

- Source state is tracked independently from evidence and release approval. A
  merged implementation is not a runtime receipt, an independent audit, or a
  gate approval; no Phase gate is advanced by this refresh.
- TOOL-032's durable action-journal source is merged on `main` from
  `55f3dfd` (implementation) through `65decba` (merge). Its production
  store is deliberately unavailable, so journal-dependent production actions
  remain hidden/refused.
- The external lifecycle hardening is source-merged at `91de464`. Remote
  execution remains disabled by the source guard (`REMOTE_EXECUTION_ENABLED=False`),
  and no approved/committed cost-ledger genesis or new provider run is claimed.
- Graph reconciliation source `b4702a5` is merged on `main` after two
  independent source-safety approvals and a 96-pass mocked integration run.
  This does not establish live Graph readiness; production action dispatch and
  live-provider evidence remain unavailable.
- The Windows filesystem refusal boundary is source-merged at `7cea137`.
  Windows filesystem mutations and helper-backed reads are refused; this is a
  safety boundary, not Windows functionality or target acceptance.
- The plan-only QA runner is source-merged at `d704b816`. It classifies unsafe
  work as `SKIP`/`UNPROVEN` and always reports release `BLOCKED`; it is not a
  substitute for the missing native/model/provider/Windows receipts.
- The bounded action-journal wire protocol is source-merged at `3746421`, but
  it is inert: no native helper, durable store, transport, trust anchor,
  packaging, activation, or production availability was added.
- Browser candidate `5dc2ad2` is rejected; repair
  `fc317fce023f9364e7f19b69a700124d1936f8ca` remains unmerged and unreviewed.
  No live browser readiness is claimed.
- The strongest recorded general remote tool evaluation is 27/34, below its
  gate. The later production-profile result is 13/32 and failed that profile;
  these are distinct results and neither establishes model/tool readiness.

## Check-in cadence

Each lane updates its session packet before work, at material contract discoveries, at review readiness, and at blockers. Each lane owns at most one primary and one small secondary task.
