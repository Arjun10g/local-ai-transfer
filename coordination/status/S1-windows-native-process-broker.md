# Status Packet

- **Session:** S1
- **Required model:** GPT-5.6 Luna
- **Role:** Runtime/security boundary — B-005 Windows native process-broker source and protocol skeleton
- **Timestamp (UTC):** 2026-09-05T03:18:17Z
- **Branch/worktree:** `luna/windows-native-process-broker-37a134f` / `wt-windows-native-process-broker`
- **Current phase:** Phase 3/4 safety prerequisite; source-only research lane
- **Primary task ID:** B-005-SOURCE
- **Secondary task ID, if any:** none
- **Task state:** READY_FOR_REVIEW
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

## Work completed

- Added a strict length-prefixed inherited-pipe protocol and duplicate/unknown
  key rejecting request parser with no raw executable/argv/cwd/env surface.
- Added an exact, compile-digest-pinned product manifest parser. The compiled
  digest defaults empty and the checked-in fixture manifest cannot activate.
- Added NTFS-only no-reparse leases for the image, manifest, cwd, runtime
  directory, and every mutable ancestor. Image size/SHA-256/volume/file ID are
  checked from the held handle and cooperatively honor cancellation/deadline.
- Added fixed template rendering, explicit `lpApplicationName`, minimal
  three-entry environment, restricted token, explicit inherited handle list,
  suspended create, image recheck, Job assignment, then resume.
- Added bounded stream collectors/stdin, deadline/cancel Job termination, and a
  completion-port plus accounting proof that the whole Job—not only its primary
  process—has zero active processes before any success receipt.
- Added in-process bounded text clipboard handling and a Copilot fixture whose
  prompt placement is stdin only.
- Kept root CMake, release package, host tools, controller, server, action
  journal, and capability advertisement unchanged.

## Evidence produced

- Commits: `d2f29a6` claim packet; `8b01290` source/contracts/static tests.
- Focused test: `python3 -m unittest tests.native.test_windows_process_broker_static -v`
  — 11/11 passed in 0.004 seconds on the local macOS development host.
- Schema syntax: all three schemas and fixture manifest parsed with
  `python3 -m json.tool`.
- Hygiene: `git diff --cached --check` passed before the implementation commit.
- No CMake configure, compile, native binary, Windows process, package, model,
  provider, browser, account, or live action was used.

## Review request

S0/S4 should review the lease/share-mode invariant, strict manifest/protocol,
restricted-token/Job ordering, whole-Job zero proof, and explicit residuals.
Merging this source is not authorization to add it to CMake or expose Windows
tools. B-005 and all target/full-access/release gates remain open.
