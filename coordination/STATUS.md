# Program Status

- Current phase: Phase 0 — constraint, approval, and contract freeze
- Gate state: `IN_PROGRESS`
- Build ID convention: `lae-<UTC YYYYMMDDTHHMMSSZ>-<12-char source SHA>-<profile>`
- Model source for this execution: official `Qwen/Qwen3.5-9B` repository on Hugging Face, immutable revision required before download
- Deployable artifact: project-produced text-only `Q4_K_M` GGUF; weights remain outside Git and release packages
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
  pathname-store and live-provider limitations remain blockers.
- The external lifecycle line at `e5` was rejected; its repair is pending and
  remote execution remains disabled. No new provider or target evidence is
  claimed.
- Graph reconciliation source `b4702a5` is merged on `main` after two
  independent source-safety approvals and a 96-pass mocked integration run.
  This does not establish live Graph readiness; production action dispatch and
  live-provider evidence remain unavailable.

## Check-in cadence

Each lane updates its session packet before work, at material contract discoveries, at review readiness, and at blockers. Each lane owns at most one primary and one small secondary task.
