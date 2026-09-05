# Status Packet

- **Session:** S1
- **Required model:** GPT-5.6 Luna
- **Role:** Runtime/security boundary — B-005 Windows native process-broker source and protocol skeleton
- **Timestamp (UTC):** 2026-09-05T03:18:17Z
- **Branch/worktree:** `luna/windows-native-process-broker-37a134f` / `wt-windows-native-process-broker`
- **Current phase:** Phase 3/4 safety prerequisite; source-only research lane
- **Primary task ID:** B-005-SOURCE
- **Secondary task ID, if any:** none
- **Task state:** IN_PROGRESS
- **Base `main` commit:** `37a134f16c936849d5dd32c96fcd8c0f7d90608e`

## Objective for this work interval

Create an inactive, reviewable Win32 broker source/protocol skeleton for all
future process, application, browser, clipboard, and Copilot launch boundaries.
The design must pin exact executable identity through suspended creation, assign
a kill-on-close Job before resume, use no shell or inherited environment, bound
all I/O/deadlines, and emit content-free receipts. It must not re-enable tools,
join the native build, enter the release package, or claim target readiness.

## Inputs and dependencies

- Open blocker `coordination/BLOCKERS.md` B-005.
- Existing strict tool/config contracts and S3 capability-advertisement filters.
- TOOL-017/TOOL-021/TOOL-024 semantics for application/browser, allowlisted
  process, and Copilot actions; this task does not modify those implementations.
- Existing vendored nlohmann JSON headers may be used by a later remote build;
  no new target dependency is authorized.
- Sol assignment fixes the scope to new broker source, schemas, static fixtures,
  and documentation only.

## Assumptions

- The accepted target volume is local NTFS; other filesystems fail closed until
  their sharing/reparse semantics receive separate evidence.
- Holding the image handle without WRITE/DELETE sharing plus every mutable
  ancestor directory without DELETE sharing keeps the exact canonical path,
  bytes, and file identity stable while `CreateProcessAsUserW` reopens it.
- Launch actions require a restricted primary token, suspended creation, exact
  application name/cwd, explicit inherited-handle list, Job assignment, image
  recheck, then resume. Failure at any step terminates before user child code can
  escape the Job.
- The host will later communicate only through inherited framed stdin/stdout;
  no listening transport or generic command operation is needed.

## Uncertainties and blockers retained

- No Windows compiler or target execution is authorized in this interval, so
  API/ABI correctness, enterprise policy behavior, nested Jobs, GUI desktop
  access, and real race resistance remain unproven.
- The broker binary itself still needs an identity-pinned signed supervisor
  launch; a Node pathname spawn cannot be the release trust boundary.
- Each approved executable's DLL/plugin/config dependency closure remains a
  separate activation blocker even after the primary image is pinned.
- Copilot may require profile/credential state incompatible with the proposed
  three-entry environment. No exception is assumed.
- Tool confirmation/action-journal integration is outside this task and remains
  authoritative before a future broker request is issued.

## Planned evidence

- Strict JSON schemas plus positive/negative fixtures.
- Static tests for inactive wiring, duplicate/unknown schema rejection,
  environment non-inheritance, fixed argv placement, executable/file-ID/hash
  mismatch, suspended→Job→resume ordering, cancellation/deadline behavior, and
  proof that the entire Job reaches zero active processes before success.
- `git diff --check`; no compile, model, provider, browser, package, or Windows
  execution.

## Readiness

`NOT_READY`. B-005 stays open until an authenticated remote Windows build,
independent security review, package/supervisor integration, and exact-target
acceptance receipts all pass.
