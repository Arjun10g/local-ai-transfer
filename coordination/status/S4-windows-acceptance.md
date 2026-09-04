# Status Packet

- **Session:** S4
- **Required model:** GPT-5.6 Luna
- **Role:** QA/release, target clean-machine acceptance harness
- **Timestamp (UTC):** 2026-09-04T17:32:44Z
- **Branch/worktree:** `luna/windows-acceptance` / `wt-windows-acceptance`
- **Current phase:** Phase 8 preparation only; no target execution
- **Primary task ID:** QA-020
- **Secondary task ID, if any:** PERF-019 receipt support
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `c7e14e0`

## Objective for this work interval

Provide a target-only, receipt-gated Windows acceptance procedure for the reported Dell Core Ultra 7 vPro Enterprise laptop. Collect exact OS/CPU/DIMM/baseboard/BIOS/display-PNP/Vulkan evidence, exercise the mandatory CPU and unpromoted Vulkan-candidate paths, validate the portable launcher/Job/named-pipe/browser-bootstrap lifecycle, and define opt-in synthetic Graph/Teams/Outlook/browser/Copilot checks with explicit operator consent and no secret/content receipt logging. Never claim target readiness from local fixtures.

## Inputs and dependencies

- Contract/version: `windows-hardware-receipt` 1.1.0; `local_bmo.windows-clean-machine-acceptance.v1`; current release/host/native contracts
- Required commits: current `main` `c7e14e0`; implementation `0c477f6`
- Model/build/profile IDs: immutable Qwen3.5-9B Q4_K_M identity; CPU mandatory; Intel Vulkan candidate unpromoted; Node v24.20.0 pinned
- Handoffs consumed: Sol assignment plus user-reported target values, treated only as expected assertions

## Work completed

- Extended the read-only Windows probe with exact board/BIOS/DIMM/CPU/display-PNP fields and hash-pinned bounded Vulkan summary/device identity.
- Added a standard-user PowerShell target harness that verifies the generated portable package, runs offline CPU chat/cancel, tests bootstrap hostile origin/referrer/replay/bearer behavior, opens the real browser through ShellExecute, checks graceful and kill-on-close process-tree cleanup, and exercises an exact hash-bound Vulkan candidate without fallback.
- Added explicitly opted-in, typed operator attestations for synthetic Outlook mail/Teams read and send, Outlook app open, browser open/fill, and prompt-only Copilot against a disposable workspace. No account/message/prompt/response/URL/token content enters the receipt.
- Added a strict offline verifier that binds the separate hardware receipt bytes/hash, refuses fixture/unbound/duplicate/oversized receipts, checks the exact reported laptop envelope, separates `core_ready` from `full_access_ready`, and leaves overall status `NOT_READY` unless every receipt passes.
- Added the target profile, operator runbook/checklist, release-operator pointer, and focused fail-closed tests.
- Rebased cleanly onto current `main` before final evidence.

## Evidence

- Commits: `e248e75` (claim), `0c477f6` (implementation)
- Commands: `python3 -m unittest -v tests.release.test_windows_acceptance tests.performance.test_probe_and_preflight.HardwareProbeTests tests.performance.test_j1m_lifecycle.StaticSafetyTests.test_hardware_receipt_has_no_serial_or_full_output_path`; targeted `py_compile`; JSON parse checks; `git diff --check main...HEAD`
- Tests: 11/11 focused tests pass after rebase; JSON and Python syntax checks pass; whitespace check passes
- Machine: macOS arm64 development machine, not Windows and not the Dell target
- Artifact/index: `qa/windows_acceptance/README.md`, `CHECKLIST.md`, `target-profile.json`; no real receipt checked in
- Metrics: none; no model, binary, Vulkan, browser, provider, or target process was run locally

## Findings and changed assumptions

- The reported Intel Graphics driver `32.0.101.8247`, 32 GB at 5600 MT/s, board `039NNG` A00, and Core Ultra 7/vPro Enterprise label remain assertions until exact Windows evidence is captured. System-visible RAM may be below 32 GiB; the gate requires a 32 GiB DIMM sum and a sane 31–32 GiB OS-visible envelope.
- `vulkaninfo --summary` can establish exact Intel integrated device/API identity only when its executable hash is approved. A candidate PASS is still not promotion; a clean explicit rejection is acceptable for a CPU-only core disposition.
- Current product browser mutation is disabled by default and Copilot is prompt-only. Therefore the full-access check will honestly remain NOT_READY unless the reviewed runtime configuration exposes the requested browser action and every live synthetic check succeeds; this task does not alter host/provider implementations.

## Blockers

- Fact/evidence: no approved target transfer, exact Windows receipt, reviewed Vulkan executable/candidate hash, browser observation, live synthetic account run, or target process receipt exists.
- Impact: current status is `NOT_READY`; QA-020 implementation is reviewable, but Phase 8 cannot pass.
- What was tried: static contract verification and narrowly focused mocked/fail-closed tests only, per the laptop-crash constraint.
- Proposed workaround: after REL-010, run the documented standard-user target sequence and return the two-file receipt bundle for offline verification/Sol decision.
- Decision/asset needed: approved CPU package/model/Vulkan candidate/vulkaninfo hashes, optional reviewed live host config/test accounts, and operator consent.
- Owner: Sol + operator; provider owners for any capability still disabled.
- Independent work continuing: none within this bounded task.

## Handoffs

- To: Sol
- Handoff file: `qa/windows_acceptance/README.md` and `qa/windows_acceptance/CHECKLIST.md`
- Required by: PERF-019/QA-020 target run and SOL-G8
- Acknowledged: assignment received; review pending

## Next bounded action

Review/merge `0c477f6`, then run the harness once on the approved Dell target. Do not run it locally or substitute a fixture for target evidence.

## Sol action requested

Review and merge. Keep Phase 8 and full-access status `NOT_READY` until a real receipt bundle passes `python -m qa.windows_acceptance.verify` and Sol reviews the residual Vulkan/provider limitations.
