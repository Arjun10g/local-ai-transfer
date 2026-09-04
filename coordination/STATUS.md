# Program Status

- Current phase: Phase 0 — constraint, approval, and contract freeze
- Gate state: `IN_PROGRESS`
- Build ID convention: `lae-<UTC YYYYMMDDTHHMMSSZ>-<12-char source SHA>-<profile>`
- Model source for this execution: official `Qwen/Qwen3.5-9B` repository on Hugging Face, immutable revision required before download
- Deployable artifact: project-produced text-only `Q4_K_M` GGUF; weights remain outside Git and release packages
- Backend ladder: CPU mandatory; Vulkan candidate; SYCL experimental
- Critical path: contracts → fixture vertical slice → controlled model artifact → real CPU slice → tools → hardening/release
- Shadeform policy: project `.env`, ownership-bound lifecycle, read-only catalogue before create, cost preflight, provider backstop longer than run, salvage before teardown, no idle instance
- Known target: Dell Intel Core Ultra 7 vPro Enterprise-class platform; exact CPU SKU, GPU device ID/driver, and memory topology still require the read-only receipt, so accelerated target promotion and final Phase 8 acceptance cannot yet be claimed
- Security note: `coordination/SECURITY_INCIDENTS.md` records a legacy reference credential exposure; the credential was removed from project env copies and requires rotation at the source

## Check-in cadence

Each lane updates its session packet before work, at material contract discoveries, at review readiness, and at blockers. Each lane owns at most one primary and one small secondary task.
