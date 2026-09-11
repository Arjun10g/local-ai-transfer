# Status Packet

- **Session:** S1
- **Required model:** GPT-5.6 Luna
- **Role:** Runtime — dormant Windows DescriptorActionJournal bootstrap
- **Timestamp (UTC):** 2026-09-11T07:15:16Z
- **Branch/worktree:** `luna/windows-descriptor-journal-bootstrap-v1` / `wt-windows-descriptor-journal-bootstrap-v1`
- **Current phase:** Phase 6 source hardening
- **Primary task ID:** RUN-WINDOWS-DESCRIPTOR-JOURNAL-BOOTSTRAP
- **Secondary task ID, if any:** None
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `d723c43263ee34211abe12c414aefa1c290a3ec1`

## Objective for this work interval

Resolve every finding from the independent S0/S4 source and security review
(`ACCEPT_WITH_REQUIRED_FIXES`, 0 BLOCKER, 1 MAJOR, 12 MINOR/NOTE) on the
dormant native Windows bootstrap for the accepted descriptor-backed
ActionJournal, re-run evidence, and keep the slice inert.

## Inputs and dependencies

- Accepted descriptor journal source merged by `6e0d12c`.
- Existing Windows storage, helper protocol/client, journal owner, and
  supervisor authority sources and their frozen contracts.
- The sibling launch contract predicate `no_ambient_handle_inheritance`
  (`luna/windows-process-authority-gap-v1`), read only, not modified.
- Exact `main@d723c43263ee34211abe12c414aefa1c290a3ec1`.

## Work completed

Original implementation (`06e045e`, `e5b707f`, `874942e`, `97a9d7d`, `340ebbc`)
as previously recorded: a separate fixed `action-journal-v2.wal` lease matching
the descriptor journal v2 header and 32 MiB limit; reused no-follow ancestors,
fixed local NTFS, protected exact-user DACL, identity, link/delete/reparse,
final-path and volume validation; atomic `CREATE_NEW`; reopen requiring an
external trusted identity; bounded write-through publication with
flush/readback; conservative empty/exact-partial-header reopen; a noncopyable
single-writer lease and a one-shot duplicate.

Review repair (`347d57d`, `44b8ff1`, `d2af452`):

- MAJOR 1. The duplicate is no longer born inheritable and is no longer
  unrevocable. `DuplicateHandle` passes `bInheritHandle = FALSE` with an
  explicit `GENERIC_READ | GENERIC_WRITE` mask, so no `DELETE` and no
  `READ_CONTROL` can cross a future process boundary, and the duplicate is
  verified non-inheritable after creation. `DescriptorWalHandoff` gained
  idempotent typed `arm_inheritance()` / `revoke_inheritance()` over
  `SetHandleInformation(HANDLE_FLAG_INHERIT, ...)`, an `inheritance_armed()`
  observation that always reads the kernel flag, a borrowed `handle()` for the
  allowlist, and `take_handle()`, refused while inheritance is armed. The
  header and `DESCRIPTOR_WAL_BOOTSTRAP.md` state the mandatory launcher
  precondition: arm only immediately before the one `CreateProcess*`, allowlist
  exactly this handle through `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, revoke or
  close immediately after the child exists and on every failure — the
  storage-side half of `no_ambient_handle_inheritance`.
- MINOR 2. The one-shot guard and the full recheck run before `output.reset()`;
  a second call reports `kHandoffAlreadyTransferred` and cannot destroy the
  handoff a first call produced.
- MINOR 3 and reviewer R2. Every `CreateFileW` call in the translation unit
  now sets `SECURITY_SQOS_PRESENT | SECURITY_ANONYMOUS`: both descriptor-WAL
  opens, the shared ancestor walk in `acquire_directories`, the volume-device
  open in `filesystem_policy`, and both v1 container opens. The ancestor opens
  are the first path-derived opens in the boundary, so an ancestor, not the
  leaf, would be the impersonation vector if path validation ever regressed.
  The flags change no behaviour for a disk-file or volume-device target, so no
  v1 semantics are altered. The test extracts every call by balanced
  parentheses, requires the extracted count to equal the number of call sites,
  and requires both flags on each.
- MINOR 4. Every modeled property now also has a source-anchored assertion
  regexed against the relevant C++ function body, and the models are
  parameterised from values parsed out of that source. Nine injected C++
  regressions were each caught (inclusive-bound flip, inheritable duplicate,
  guard reordering, dropped SQOS, discard armed on reopen, dropped final-path
  recheck, status conflation, dropped prefix-count guard, restored dead store).
- MINOR 5. Sol's option (b): a leaf created by `CREATE_NEW` and not yet
  published is discarded through its own open handle when any later check or
  publication step fails. `DELETE` is requested only when this call creates the
  leaf; the reopen path never requests it and can never delete. The
  identity-reopen handle concedes `FILE_SHARE_DELETE`, which the first handle
  still denies to everyone else. The doc states explicitly that discarding a
  never-published private artifact is not a WAL repair.
- Findings 6–9. Shared file object and file position documented, with a
  transferred state that refuses parent-side I/O; dead size store replaced by an
  explicit post-publication equality assertion; pre-duplication recheck set now
  equals the acquisition check set (final-path and volume rechecks added);
  conflated refusals split into distinct typed statuses in both paths.
- Finding 10 documented as pre-existing merged behaviour shared with the v1
  container lease and out of scope. Finding 11 fixed by naming the exact focused
  command below. Findings 12 and 13 recorded in source comments and the doc.

Re-review residuals (`26b6b73`):

- Sol residual 2. The two "never" assertions in the merged v1 suite now apply
  to the whole translation unit minus the v2 descriptor-WAL bodies, instead of
  to four v1 function bodies, so all shared helpers stay covered. A new guard
  test asserts the excision keeps eighteen named v1/shared definitions and drops
  only the v2 ones. Verified by injection in a scratch copy: `FILE_SHARE_DELETE`
  in `acquire_directories` fails
  `test_ancestors_and_leaf_are_nofollow_identity_held_with_strict_sharing`, and
  `SetFileInformationByHandle` in `write_exact` fails
  `test_genesis_is_full_bounded_zero_flush_readback_rng_and_header_readback`.
  Both mutations were reverted; the tree was left clean. This inversion is
  carried in `26b6b73`, whose message describes only the SQOS and prose work.
- Sol residual 3 and reviewer R1. The duplicate-access prose was wrong.
  `GENERIC_READ`/`GENERIC_WRITE` map through `FILE_GENERIC_READ`/`WRITE`, whose
  `STANDARD_RIGHTS_READ`/`STANDARD_RIGHTS_WRITE` are `READ_CONTROL`. The
  comment and the doc now state: no `DELETE`, no `WRITE_DAC`, no `WRITE_OWNER`
  cross; `READ_CONTROL` does, and only lets the child read a DACL naming its
  own user.
- Reviewer R3, fixed. The reopen handle's `FILE_SHARE_DELETE` reasoning is
  mirrored into `DESCRIPTOR_WAL_BOOTSTRAP.md`, including why the `DELETE` right
  cannot be shed after publication.
- Reviewer R5, partly fixed. The third independent copy of the WAL header
  literal in `tests/host/descriptor-action-journal.test.mjs:19` is now parsed
  and byte-compared against the C++ literal by the parity test, closing the
  drift gap. The remaining §5 items 12-18 stand and are listed under Blockers.

## Evidence

Commits: claim `06e045e`; implementation `e5b707f`; atomic duplicate ownership
repair `97a9d7d`; review repair `347d57d`, `44b8ff1`, `d2af452`, `d53a628`;
re-review residual repair `26b6b73`.

All commands run from the worktree root with `PYTHONDONTWRITEBYTECODE=1`.

- `python3 -m unittest discover -s tests/native -p 'test_windows_*static.py'`:
  PASS, `Ran 248 tests` / OK (237 before repair; the bootstrap module grew from
  10 to 20 tests and the storage module from 16 to 17).
- `python3 -m unittest tests.qa.test_safe_runner`: PASS, `Ran 20 tests` / OK.
- `python3 tests/native/test_windows_descriptor_journal_bootstrap_static.py`:
  PASS, `Ran 20 tests` / OK.
- `python3 tests/native/test_windows_action_journal_storage_static.py`:
  PASS, `Ran 17 tests` / OK.
- Focused command, named exactly:
  `python3 -m unittest tests.native.test_windows_descriptor_journal_bootstrap_static tests.native.test_windows_action_journal_storage_static tests.native.test_windows_inert_compile_harness_static tests.qa.test_safe_runner.SafeRunnerTests.test_inventory_exactly_matches_current_tests_without_content_reads`:
  PASS, `Ran 51 tests` / OK (40 before repair). Per module: bootstrap 20,
  storage 17, inert harness 13, plus the single inventory test.
- `node --test tests/host/descriptor-action-journal.test.mjs tests/host/action-journal-protocol.test.mjs`:
  PASS, `tests 63 / pass 63 / fail 0 / cancelled 0 / skipped 0 / todo 0`.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`:
  `status: BLOCKED`, `passed: false`; inventory 60 discovered / 0 unknown /
  0 missing, `discovery_error: null`; 66 result rows = 65 SKIP + 1 PASS.
  `--output -` is required for a re-runnable command: the default output target
  is create-new and aborts on a second run.
- `git diff --check main...HEAD`: PASS, exit 0, no output.
- Machine: macOS source/static review only; no Windows equivalence claimed.

## Findings and changed assumptions

- The accepted Node journal needs an already-open read/write descriptor and
  deliberately supplies no Windows secure acquisition, publication, locking,
  authenticity, or anti-rollback authority.
- Existing native storage/helper/owner sources are dormant and must remain
  outside product/package/activation graphs in this slice.
- A Win32 `HANDLE` is not a child-process CRT descriptor. This slice prepares a
  duplicate but deliberately does not serialize it as `LAE_ACTION_JOURNAL_FD`,
  launch a process, or claim Node compatibility.
- The duplicate and the retained handle share one file object and therefore one
  file position. The exclusive-writer claim is exact only against other
  processes; between the lease holder and its child it is a contract, now
  enforced by a transferred state that refuses parent-side I/O, and the child
  must use positional I/O only, which `DescriptorActionJournal` already does.
- Two boundaries now share `windows_storage.cpp`. Four additive source-only
  refusal statuses were added to `contracts/action-journal-storage/v0.1.0.json`
  so code and contract still agree exactly, and notified as ICR-RUN-WDJB-001.
  Two file-wide "never" assertions in the merged v1 suite were scoped to the v1
  container functions; the v2 equivalents are pinned function-anchored in the
  bootstrap suite, and both scoped assertions were mutation-checked.
- Reviewer R4 is deliberately not implemented. Shedding `DELETE` from the
  create-path handle after `discard.disarm()`, by duplicating down and closing
  the original, would add a failure path after a WAL is already published and
  verified: a `DuplicateHandle` failure there would have to fail an acquisition
  whose artifact is correct and can no longer be discarded. The security-
  relevant property, the share reservation seen by other processes, is
  unchanged either way because share bookkeeping lives on the file object. On a
  platform where none of this can be compiled or executed, the added
  post-publication failure mode outweighs the handle-local least-privilege gain.
  Recommend revisiting it with the launcher bridge, under real Windows tests.
- Delete-on-failure required `DELETE` on the create-path handle, which requires
  the identity-reopen handle to concede `FILE_SHARE_DELETE`. Windows share
  bookkeeping lives on the file object, so the right cannot be shed after
  publication; the effective restriction on other processes is unchanged
  because the first handle still denies write and delete sharing.

## Blockers

- Windows/MSVC compile and static analysis; exact-target owner/DACL/share-mode,
  short-write/flush/restart, filter-driver, create-failure discard, SQOS
  behaviour, and power-loss tests are absent. The share-mode and disposition
  reasoning above is static reading, not execution evidence. Reviewer R5
  items 12-18 stand: no compile, no `/analyze`, no Windows execution, and no
  exact-target path-rejection, link-count, or `kAlreadyExists` mapping tests for
  the v2 leaf.
- A reviewed launcher must use an explicit handle allowlist, convert the
  inherited `HANDLE` into a readable/writable CRT fd inside the child, prove
  acknowledgement/ownership transfer, and close every failure path.
- A durable external identity/anti-rollback anchor is still absent. Existing WAL
  SHA-256 values detect corruption but do not authenticate or prevent a
  self-consistent rollback.
- Production and target readiness remain blocked pending independent native
  build and exact Windows execution.

## Handoffs

- Review owners: S0, S3, S4.
- No activation or release-gate change requested.
- Sol decision open: whether the four new statuses stay in the v1 storage
  contract or move to a separate descriptor-WAL contract (ICR-RUN-WDJB-001).

## Next bounded action

Merge by Sol, then remote Windows compilation only under the existing
OFF-by-default inert compile-check gate.

## Sol action requested

Merge, and record the ICR-RUN-WDJB-001 decision in the governance refresh. The
ICR entry is deliberately left at pending-Sol.
