# Status Packet

- **Session:** S4
- **Required model:** GPT-5.6 Luna
- **Role:** QA/release, target clean-machine acceptance harness
- **Timestamp (UTC):** 2026-09-04T17:09:43Z
- **Branch/worktree:** `luna/windows-acceptance` / `wt-windows-acceptance`
- **Current phase:** Phase 8 preparation only; no target execution
- **Primary task ID:** QA-020
- **Secondary task ID, if any:** PERF-019 receipt support
- **Task state:** IN_PROGRESS
- **Last merged `main` commit:** `90a2d43`

## Objective for this work interval

Implement a read-only Windows clean-machine acceptance harness and operator checklist for the reported Dell Core Ultra 7 vPro Enterprise laptop. The harness will collect exact Windows/CPU/GPU/PNP/memory/Vulkan evidence, exercise the mandatory CPU and candidate Vulkan release paths on the target, validate portable launcher supervision and browser bootstrap, and define separately consented synthetic live checks for Graph mail, Teams, Outlook, Copilot, and browser actions. It will fail closed unless a complete target receipt proves the claim and will not label local fixtures or this development machine READY.

## Inputs and dependencies

- Contract/version: existing release manifest, backend profile, host API, and portable launcher contracts at `90a2d43`
- Required commits: current `main` at `90a2d43`; accepted release/model checksums and target transfer remain external dependencies
- Model/build/profile IDs: fixed product GGUF identity; CPU mandatory; Intel Vulkan unpromoted candidate
- Handoffs consumed: Sol's bounded QA-020/PERF-019-support assignment and user-reported hardware expectations

## Work completed

- Created a clean worktree and branch from current `main`.
- Read the mandatory governance and execution documents and claimed QA-020 before implementation.

## Evidence

- Commit: pending implementation; this startup packet is committed first
- Commands: read-only repository inventory only
- Tests: none yet
- Machine: macOS arm64 development machine, not the target
- Artifact/index: none; target receipt does not exist
- Metrics: none

## Findings and changed assumptions

- The user-reported Core Ultra 7 vPro, Intel integrated Graphics driver `32.0.101.8247`, 32 GB at 5600 MT/s, and Dell board `039NNG` revision `A00` are expected values to verify, not accepted machine evidence.
- Vulkan remains a candidate and must be exercised or explicitly rejected with evidence; it is not promoted by this task.
- Live account actions require separate, explicit operator consent and synthetic test targets. Presence of local model execution does not grant account or device authority.

## Blockers

- Fact/evidence: no accepted release transfer, exact target hardware receipt, Vulkan probe receipt, or target execution receipt is present.
- Impact: current release status remains UNPROVEN and this task cannot produce READY locally.
- What was tried: repository/governance inventory only; target work is intentionally not simulated as real evidence.
- Proposed workaround: implement a statically testable harness and run it later on the approved target/package.
- Decision/asset needed: approved release/model artifacts and operator-authorized target session.
- Owner: Sol + operator.
- Independent work continuing: harness, schema/gate, fixtures, and checklist.

## Handoffs

- To: Sol
- Handoff file: this packet; final evidence paths pending
- Required by: QA-020 review
- Acknowledged: assignment received

## Next bounded action

Add the target PowerShell collector/runner, offline receipt verifier, fixture-only regression tests, and operator checklist without changing model, host-tool, or provider implementations.

## Sol action requested

Review/merge after focused static tests; do not advance the target release gate without real Windows receipts.
