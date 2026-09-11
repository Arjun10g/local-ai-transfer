# Status Packet

- **Session:** S1
- **Required model:** GPT-5.6 Luna
- **Role:** Runtime — frozen descriptor-WAL contract and R4 closure
- **Timestamp (UTC):** 2026-09-11T13:49:30Z
- **Branch/worktree:** `luna/descriptor-wal-contract-v1` / `wt-descriptor-wal-contract-v1`
- **Current phase:** Phase 6 source hardening
- **Primary task ID:** RUN-DESCRIPTOR-WAL-CONTRACT
- **Secondary task ID, if any:** None
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `fdfed07692ae35713432e02a2c1d7f729267acb4`

## Objective for this work interval

Close the two items Sol deferred on 2026-09-11: (1) create the separate frozen
contract that ADR-0004 requires for the v2 descriptor-WAL boundary before any
transport, import, package, or activation path exists, and (2) formally close
independent-review item R4 (dropping `DELETE` access after WAL publication).
Source-only; no C++ behaviour, CMake, packaging, or activation change.

## Inputs and dependencies

- `RUN-WINDOWS-DESCRIPTOR-JOURNAL-BOOTSTRAP`, merged `4de01f7` under integrated
  baseline `7239b7e`, and its design note
  `native/action_journal_storage/DESCRIPTOR_WAL_BOOTSTRAP.md`.
- [ADR-0004](../adrs/ADR-0004-inert-contract-additive-extension.md), which makes
  a separate frozen contract for the v2 boundary REQUIRED, and ADR-0003 as its
  precedent.
- ICR-RUN-WDJB-001 and Sol's 2026-09-11 decision recorded there.
- `contracts/windows-process-authority/v1.0.0.json` as the recent house-style
  reference for a dormant boundary contract and its predicate cross-check.
- The independent S0/S4 review sections "R4", "3a", and "3b" (read-only).
- Exact `main@fdfed07692ae35713432e02a2c1d7f729267acb4`.

## Work completed

**A. New frozen contract (`e09852e`).**
`contracts/action-journal-descriptor-wal/v0.1.0.json` plus `v0.1.0.md`,
authoritative for the v2 boundary only. It carries the identity
(`lae.action-journal-descriptor-wal`, version `0.1.0`, distinct `$id` and
`schema_version` from the storage boundary), the exact 48-byte header as hex
(`7b22…7d0a`) and UTF-8, the inclusive 33,554,432-byte bound, the fixed leaf
`action-journal-v2.wal`, the 26 status codes the v2 functions actually emit,
the lease/handoff/inheritance state lists, the launcher preconditions
(`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, arm immediately before the single
`CreateProcess*`, revoke or close after the child and on every failure,
`take_handle` refused while armed, `no_ambient_handle_inheritance`), the
duplicate's exact `GENERIC_READ | GENERIC_WRITE` mask with `bInheritHandle =
FALSE`, options `0`, no `DUPLICATE_SAME_ACCESS` and no `DELETE`, the
create-only delete-on-failure rule, the shared-file-position rule that refuses
parent-side I/O after transfer, and the receipt shape. All seven availability
gates are literal `false`. The `.md` states that this contract is required by
ADR-0004 before any transport, import, package, or activation path and that no
such path exists.

*House-style note:* `additionalProperties` is used in this repository only by
contracts that are themselves JSON Schemas (`tool-envelope`, `config-schema`,
`windows-process-broker`, …). The descriptive boundary contracts this one
belongs with — `action-journal-storage/v0.1.0.json` and
`windows-process-authority/v1.0.0.json` — do not use it, so it is not used
here. Strict shape is enforced instead by the new suite, which compares the
declared sets against the source.

**B. Storage contract untouched (`170f708`).**
`contracts/action-journal-storage/v0.1.0.json` is byte-identical to `main`
(`git diff main...HEAD` reports no change to it). Its `.md` gained one section
recording that its `status_codes` are a documented superset: the four ADR-0004
codes are emitted only by the v2 boundary sharing the translation unit, are now
also declared by the new contract, and are retained so the merged status-set
equality assertion keeps its force; a future storage version may drop them only
under a new ICR and a version bump.

**C. Static pinning (`6b7b59b`).**
New `tests/native/test_descriptor_wal_contract_static.py`, 20 tests, registered
in `scripts/test/run_qa.py` `TEST_INVENTORY` as `native_static`. Both contracts
are parsed with a duplicate-key-rejecting hook under a byte bound. The v2 status
set is **derived, not transcribed**: every `StorageStatus`-returning definition
is extracted by signature, a call graph is built over those definitions, and the
statuses reachable from the six v2 entry points (`acquire_descriptor_wal`,
`DescriptorWalLease::recheck` / `revalidate` / `prepare_inheritable_handoff`,
`DescriptorWalHandoff::arm_inheritance` / `revoke_inheritance`) are compared
against the contract. Also pinned: header and limit byte-for-byte against both
the C++ and the Node constants; every gate `false`; `required_predicates` all
present in `windows_storage.hpp` (mirroring the process-authority cross-check);
the access mask and launcher-precondition strings present in the contract and in
the header/design note; the receipt field list equal to the `DescriptorWalReceipt`
members; and no consumer, transport, or package entry for the new contract path.

**D. R4 closed (`2507fdc`).**
`DESCRIPTOR_WAL_BOOTSTRAP.md` gained an "Accepted design limitations" heading
recording why R4 is documented rather than implemented. Win32 has no operation
that narrows the access already granted to an open handle. Duplicating down and
closing the original leaves the file object's granted access and share
bookkeeping unchanged — only handle-local rights shrink — while inserting a new
`DuplicateHandle` failure path *after* the WAL has been created, written,
flushed, read back, size-asserted, identity-reopened and verified, and after
`discard.disarm()`, so the acquisition would have to refuse a correct artifact
it may no longer delete. A `ReOpenFile` with reduced access is a new open that
collides with the retained handle's exclusive `FILE_SHARE_READ` reservation and
fails with a sharing violation. The restriction other processes see is identical
either way. No C++ change. The three bounding properties are pinned statically:
(i) the exact duplicate mask with `DuplicateHandle(..., FALSE, 0)` and no
`DUPLICATE_SAME_ACCESS` — already pinned by
`test_duplicate_is_not_born_inheritable_and_carries_no_delete`, referenced not
duplicated; (ii) no accessor exposes the lease's raw handle — **had no pin**,
added as `test_no_accessor_exposes_the_leases_raw_handle`; (iii) the discard
RAII is disarmed before lease transfer — already pinned by
`test_failed_create_discards_the_unpublished_leaf_and_reopen_never_deletes`,
referenced not duplicated.

**E. ICR (`c3c3f31`).** A dated follow-up note on ICR-RUN-WDJB-001.

**F. Claims and packet (`5c23585`, this file).**

## Evidence

Commits, base exact `main@fdfed07`: claim `5c23585`; contract `e09852e`;
storage `.md` `170f708`; R4 `2507fdc`; tests `6b7b59b`; ICR `c3c3f31`.
Last content commit `c3c3f31ef42365f9806ebe54fc984666a7d6da6a`; this packet
and the claims-row update are committed on top of it, and the branch tip is
therefore a later docs-only commit.

All commands run from the worktree root with `PYTHONDONTWRITEBYTECODE=1`.
Machine: macOS source/static review only; no Windows equivalence is claimed.

- `python3 -m unittest discover -s tests/native -p 'test_*static.py'`:
  PASS, `Ran 309 tests` / OK (289 before this slice; the new module contributes
  20). The `-v` listing confirms the new file is discovered by that pattern:
  20 `DescriptorWalContractStaticTests` lines.
- `python3 tests/native/test_descriptor_wal_contract_static.py`:
  PASS, `Ran 20 tests` / OK.
- Focused command, named exactly:
  `python3 -m unittest tests.native.test_descriptor_wal_contract_static tests.native.test_windows_descriptor_journal_bootstrap_static tests.native.test_windows_action_journal_storage_static tests.qa.test_safe_runner.SafeRunnerTests.test_inventory_exactly_matches_current_tests_without_content_reads`:
  PASS, `Ran 58 tests` / OK (new 20, bootstrap 20, storage 17, inventory 1).
- `python3 -m unittest tests.qa.test_safe_runner`: PASS, `Ran 20 tests` / OK.
- `node --test tests/host/descriptor-action-journal.test.mjs tests/host/action-journal-protocol.test.mjs`:
  PASS, `tests 63 / pass 63 / fail 0 / cancelled 0 / skipped 0 / todo 0`.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`:
  `status: BLOCKED`, `passed: false`; inventory 63 discovered / 0 unknown /
  0 missing, `discovery_error: null`; 69 result rows = 68 SKIP + 1 PASS. The new
  file is classified `native_static`. (62 discovered / 68 rows before this
  slice.)
- Strict duplicate-key JSON parse of both contracts: PASS.
  `contracts/action-journal-descriptor-wal/v0.1.0.json` 11,365 bytes, 43
  top-level keys, 26 status codes;
  `contracts/action-journal-storage/v0.1.0.json` 6,217 bytes, 28 top-level keys,
  30 status codes.
- Derived status arithmetic: v2 emitted set = 26 = the new contract's
  `status_codes` exactly; v1 emitted set = 25; `v1 ∪ v2` = 29 ⊂ the storage
  contract's 30; `storage − (v1 ∪ v2)` = exactly `{platform_unavailable}`, which
  is declared in the enum and `status_name` and emitted by no function body;
  `storage − v2` = `{platform_unavailable, rng_failed, hash_failed,
  container_unformatted}`, matching the contract's declared `storage_only`
  field. All four ADR-0004 codes are in v2 and in none of v1.
- Mutation check, 12 injected regressions in a scratch copy outside the
  repository, each reverted: new-status-in-v2, `DUPLICATE_SAME_ACCESS`,
  `DELETE` on the reopen path, a lease raw-handle accessor, Node limit drift to
  16 MiB, native header literal drift to version 3, a gate flipped `true`, a
  predicate absent from the header, a dropped contract status code, the R4
  section removed, the size bound flipped to exclusive, and a duplicate JSON
  key — 11 fail the new suite and the duplicate key fails it at `setUpClass`.
  A twelfth, the discard armed on the reopen path, fails the existing
  `test_failed_create_discards_the_unpublished_leaf_and_reopen_never_deletes`,
  which this suite references rather than duplicates.
- `git diff --check main...HEAD`: PASS, exit 0, no output.
- `git status --short`: empty.
- `git diff --name-only main...HEAD` touches no `CMakeLists.txt`, no `.cmake`,
  no `.cpp`, no `.hpp`, no `.mjs`, and no `package.json`. Eight files changed,
  1,184 insertions, 0 deletions.

## Findings and changed assumptions

- The brief named the Node constants as living in
  `host/agent/descriptor-action-journal.mjs`. That file does not exist;
  `DESCRIPTOR_WAL_HEADER` and `MAX_DESCRIPTOR_WAL_BYTES` are in
  `host/agent/action-journal.mjs`, which is what the existing bootstrap suite
  also reads. The new suite uses that path.
- `platform_unavailable` is declared by the enum, the `status_name` switch, and
  the storage contract, but is emitted by no function body in the translation
  unit. The superset invariant is therefore recorded in its exact true form —
  `v1 ∪ v2 ∪ {platform_unavailable} == storage`, with the single unemitted code
  named and pinned — rather than as a plain set equality that would not hold.
- `additionalProperties` is a JSON-Schema-contract convention in this
  repository, not a descriptive-boundary-contract convention; see the
  house-style note above.
- R4 is closed as an accepted limitation, not fixed. The security-relevant
  property (what a foreign process can obtain) is unchanged by the proposed
  hardening, because Windows share and granted-access bookkeeping lives on the
  file object rather than the handle.

## Blockers

- Windows/MSVC compile and static analysis, and exact-target DACL, share-mode,
  inheritance-allowlist, `HANDLE`-to-CRT conversion, filter-driver,
  create-failure discard, and power-loss tests remain absent. Every statement in
  the new contract about Win32 behaviour is static reading, not execution
  evidence.
- A reviewed launcher bridge is still required before the boundary can be
  reached at all; adding one is the event that makes this contract load-bearing
  and is out of scope here.
- A durable external identity and anti-rollback anchor is still absent.
- Sol review and merge of the new contract are pending; the ICR note is left at
  pending-Sol.

## Handoffs

- Review owners: S0, S4.
- No activation or release-gate change requested.
- No activation, Windows, production, or target claim is made by this slice.

## Next bounded action

Sol review and merge. No follow-on work is claimed.

## Sol action requested

Review and merge the new contract, and record the ICR-RUN-WDJB-001 follow-up in
the next governance refresh. Confirm the recorded form of the superset invariant
(`v1 ∪ v2 ∪ {platform_unavailable} == storage`) is the intended reading, and
confirm the `additionalProperties` house-style judgement.
