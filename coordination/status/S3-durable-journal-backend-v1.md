# Status Packet

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Agent/Tools — durable journal and Graph recovery source slice
- **Timestamp (UTC):** 2026-09-09T04:10:44Z
- **Branch/worktree:** `luna/durable-journal-backend-v1` / `wt-durable-journal-backend-v1`
- **Current phase:** Phase 6 failure recovery, source-only
- **Primary task ID:** TOOL-DURABLE-JOURNAL-BACKEND
- **Secondary task ID, if any:** None
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `9c2d15f95618bfe85ad5c7a21d6f43a20d025215`

## Objective for this work interval

Implement the largest coherent, production-aligned durable local ActionJournal
and provider-owned Graph proof/tombstone/recovery source slice that can be
reviewed without provider, network, model, native-build, browser, credential, or
live execution, while preserving existing contracts and fail-closed gates.

## Inputs and dependencies

- Contract/version: existing ActionJournal transition/event contract v1 and
  current controller durable-action admission semantics.
- Required commits: exact `main@9c2d15f`; integrated Graph composition/auth
  `55c8a75`; manual Graph resolution guard `48312bf`; existing journal core,
  protocol, helper-model, and test-only client sources.
- Model/build/profile IDs: none; source/mock-only work.
- Handoffs consumed: Sol assignment to close the next production-aligned
  journal/recovery gap without asserting live or target readiness.

## Work completed

- Read the mandatory repository, governance, execution, and coordination
  instructions in the required order.
- Created this isolated branch/worktree and claimed the bounded task before
  editing journal/controller/provider source.
- Added an append-only, descriptor-authority ActionJournal WAL. It validates
  owner/mode/link/file identity, bounds the file and records, fsyncs and reads
  back every transition, checks the complete digest and per-operation chains,
  and never reopens or mutates a pathname.
- Added restart recovery which cancels only pre-dispatch records, converts a
  durable dispatch into an `unknown_manual` tombstone, and leaves a
  provider-acknowledged record nonterminal instead of manufacturing success.
- Moved the journal acknowledgement after the controller verifies the
  provider module's private, exact operation-bound completion attestation.
  Generic provider-shaped JSON now transitions directly to reconciliation.
- Wired exact inherited-descriptor configuration and isolated journal cleanup
  in HostServer shutdown; invalid journal configuration fails before engine
  creation.
- Added crash-boundary, restart, tamper, disclosure, provider-proof,
  composition, and teardown adversarial tests and QA inventory coverage.

## Evidence

- Commits: claim `775a321`; implementation `c1c8684d8b4eb7eac9f1a13717158dc072cd227a`.
- Commands:
  - `node --check host/agent/action-journal.mjs && node --check host/agent/controller.mjs && node --check host/server/host-server.mjs && node --check lae-host.mjs && git diff --check`
  - `node --test tests/host/descriptor-action-journal.test.mjs tests/host/action-journal.test.mjs tests/host/graph-production-composition-restart.test.mjs tests/host/graph-manual-resolution-guard.test.mjs tests/host/external-tools.test.mjs tests/security/permission-mode-adversarial.test.mjs tests/security/tool-calling-adversarial.test.mjs`
  - `python3 -m unittest -q tests.qa.test_safe_runner`
- Tests: Node 192 discovered, 191 passed, 0 failed, 1 existing TODO,
  1.271 seconds; Python QA inventory 20/20 passed in 0.037 seconds; all syntax
  and diff checks passed.
- Machine: local macOS development host; no target equivalence claimed.
- Artifact/index: this status packet.
- Metrics: none.

## Findings and changed assumptions

- The current pathname ActionJournal intentionally blocks production because
  Node lacks descriptor-relative `openat`/`renameat`/`unlinkat`; implementation
  must not relabel that seam as production-safe.
- A provider proof must be independently bound to the exact durable operation
  and provider-owned idempotency/reconciliation identity; a host assertion is
  not sufficient to resolve an ambiguous Graph mutation.
- An inherited file descriptor is sufficient to remove pathname lookup from
  the Node journal after bootstrap, but secure creation/reopen/publication,
  single-writer exclusion, and rollback anchoring remain launcher/native-owner
  responsibilities.

## Blockers

- Fact/evidence: Windows secure descriptor acquisition and owner-only DACL,
  durable directory-entry publication, cross-process single-writer exclusion,
  and anti-rollback anchoring are not supplied by this Node slice. Existing
  handle-relative storage/helper source remains inert, uncompiled, unimported,
  and without target evidence. The WAL is bounded and append-only, with no
  compaction; it fails closed at its record/event/32 MiB ceilings. Provider
  ambiguity is retained for manual/provider reconciliation, not automatically
  retried or promoted to a terminal result.
- Impact: this source slice cannot establish Windows production or release
  readiness.
- What was tried: implemented the bounded inherited-descriptor WAL and private
  provider-attestation ordering with source/mock crash and restart evidence;
  the unsafe pathname backend remains blocked.
- Proposed workaround: connect the accepted native single owner to an inherited
  descriptor plus durable publication/locking/anti-rollback evidence, and add
  provider reconciliation before allowing terminal recovery.
- Decision/asset needed: later S1/S4 native bridge, compile, crash, Windows, and
  target evidence.
- Owner: S1/S4/S0.
- Independent work continuing: source/mock implementation and adversarial tests.

## Handoffs

- To: S0/S1/S4
- Handoff file: this status packet
- Required by: review/merge and any later native production bridge
- Acknowledged: pending independent review

## Next bounded action

Independent source/security review of `c1c8684`; retain all production and
target gates until the native owner and provider recovery blockers are closed.

## Sol action requested

Review after the implementation commit; no gate change requested.
