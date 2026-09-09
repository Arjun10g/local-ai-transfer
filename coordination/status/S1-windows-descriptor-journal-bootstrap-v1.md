# Status Packet

- **Session:** S1
- **Required model:** GPT-5.6 Luna
- **Role:** Runtime — dormant Windows DescriptorActionJournal bootstrap
- **Timestamp (UTC):** 2026-09-09T00:00:00Z
- **Branch/worktree:** `luna/windows-descriptor-journal-bootstrap-v1` / `wt-windows-descriptor-journal-bootstrap-v1`
- **Current phase:** Phase 6 source hardening
- **Primary task ID:** RUN-WINDOWS-DESCRIPTOR-JOURNAL-BOOTSTRAP
- **Secondary task ID, if any:** None
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `d723c43263ee34211abe12c414aefa1c290a3ec1`

## Objective for this work interval

Implement the largest coherent dormant native Windows bootstrap step for the
accepted descriptor-backed ActionJournal: secure local storage acquisition,
single-owner authority, and an inherited descriptor handoff contract, without
activating or packaging native code or asserting Windows/production evidence.

## Inputs and dependencies

- Accepted descriptor journal source merged by `6e0d12c`.
- Existing Windows storage, helper protocol/client, journal owner, and
  supervisor authority sources and their frozen contracts.
- Exact `main@d723c43263ee34211abe12c414aefa1c290a3ec1`.

## Work completed

- Created the isolated branch/worktree and read all mandatory repository,
  governance, execution, and coordination instructions in required order.
- Extended the inert Windows storage boundary with a separate fixed
  `action-journal-v2.wal` lease matching the descriptor journal v2 header and
  32 MiB limit.
- Reused retained no-follow ancestors, fixed local NTFS, protected exact-user
  DACL, file identity, link/delete/reparse, final-path, and volume validation.
  Create is atomic `CREATE_NEW`; reopen requires an external trusted volume/file
  identity and never treats candidate-path metadata as its trust anchor.
- Added bounded write-through header publication with flush/readback and
  conservative empty/exact-partial-header reopen. Frame replay and torn-frame
  recovery remain owned by `DescriptorActionJournal`; native code never
  truncates or repairs a WAL.
- Added a noncopyable single-writer lease and one-shot inheritable same-access
  duplicate. Source-handle inheritance is refused and identity/DACL/size/prefix
  checks repeat immediately before duplication.
- Added explicit dormant bridge design and source/protocol/model tests, then
  registered the new test in the exact QA inventory.

## Evidence

- Commits: claim `06e045e`; implementation `e5b707f`; atomic duplicate
  ownership repair `97a9d7d`.
- `python3 -m unittest discover -s tests/native -p
  'test_windows_*static.py'`: PASS, 237/237 after repair.
- `python3 -m unittest tests.qa.test_safe_runner`: PASS, 20/20.
- Focused bootstrap/storage/harness/inventory command: PASS, 40/40 after repair.
- `node --test tests/host/descriptor-action-journal.test.mjs
  tests/host/action-journal-protocol.test.mjs`: PASS, 63/63.
- `python3 scripts/test/run_qa.py --root . --skip-native`: expected `BLOCKED`;
  exact inventory 60 discovered / 0 unknown / 0 missing and bounded skeleton
  scan PASS with no findings. Native/package/target execution stayed skipped.
- `git diff --check`: PASS.
- Machine: macOS source/static review only; no Windows equivalence claimed.

## Findings and changed assumptions

- The accepted Node journal needs an already-open read/write descriptor and
  deliberately supplies no Windows secure acquisition, publication, locking,
  authenticity, or anti-rollback authority.
- Existing native storage/helper/owner sources are dormant and must remain
  outside product/package/activation graphs in this slice.
- A Win32 `HANDLE` is not a child-process CRT descriptor. This slice prepares
  an inheritable duplicate but deliberately does not serialize it as
  `LAE_ACTION_JOURNAL_FD`, launch a process, or claim Node compatibility.
- Repair: the handoff destination and its `UniqueHandle` now allocate before
  `DuplicateHandle`, and the API writes the result directly into RAII-owned
  storage. Allocation failure occurs before handle creation; API/flag refusal
  destroys the owner and closes once; only successful transfer marks the lease
  one-shot. All failure outcomes remain retry-safe.

## Blockers

- Windows/MSVC compile and static analysis; exact-target owner/DACL/share-mode,
  short-write/flush/restart, filter-driver, and power-loss tests are absent.
- A reviewed launcher must use an explicit handle allowlist, convert the
  inherited `HANDLE` into a readable/writable CRT fd inside the child, prove
  acknowledgement/ownership transfer, and close every failure path.
- A durable external identity/anti-rollback anchor is still absent. Existing
  WAL SHA-256 values detect corruption but do not authenticate or prevent a
  self-consistent rollback.
- Production and target readiness remain blocked pending independent native
  build and exact Windows execution.

## Handoffs

- Review owners: S0, S3, S4.
- No activation or release-gate change requested.

## Next bounded action

Independent source/security review, then remote Windows compilation only under
the existing OFF-by-default inert compile-check gate.

## Sol action requested

Independent source/security review after a committed implementation.
