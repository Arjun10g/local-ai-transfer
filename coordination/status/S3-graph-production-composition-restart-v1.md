# Status Packet

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Agent/Tools
- **Timestamp (UTC):** 2026-09-08
- **Branch/worktree:** `luna/graph-production-composition-restart-v1` / `wt-graph-production-composition-restart-v1`
- **Current phase:** Phase 6 cleanup-order amendment
- **Primary task ID:** Graph production-composition/restart refusal test slice
- **Secondary task ID, if any:** None
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `b813709daaed5c77b985e35550a19876eb9faf54`

## Objective for this work interval

Add mocked behavioral coverage for Microsoft Graph production composition, journal-gated controller admission, restart non-persistence, host auth disclosure limits, and scope/account binding. Apply only the bounded auth-run lifecycle and public-status projection repairs; keep provider availability, credential storage, live transport, and remote enablement unchanged.

## Inputs and dependencies

- Contract/version: current host/provider/controller contracts on the pinned main baseline
- Required commits: `b813709daaed5c77b985e35550a19876eb9faf54`
- Model/build/profile IDs: None; no model or build work is in scope
- Handoffs consumed: Parent task briefing and prior Graph auth-boundary review

## Work completed

- Created a fresh sibling worktree and branch from the exact clean main baseline.
- Read `AGENTS.md` and required governance/execution policy documents.
- Mapped `lae-host`, registry, Graph provider/auth, controller, journal, grants, host server, and existing focused tests.

## Evidence

- Commit: final repair tip reported with this packet
- Commands: `node --test tests/host/graph-production-composition-restart.test.mjs`; selected Graph/host/controller/security Node inventory; `python3 scripts/test/run_qa.py --root . --output - --skip-native`; `git diff --check`
- Tests: Focused Graph auth/composition suite 18/18; selected action-journal/controller/Graph regression set 53/53 (the broader Graph/host/controller/security inventory remains 153/153); hostile serialized-output, importable bootstrap, all four Graph mutation pre-preview, provider `/me` race, failure-isolated lifecycle, and malformed/throwing journal-health assertions are included. QA safe inventory discovers 55 test files and 61 result records (1 PASS, 60 SKIP), with status BLOCKED by safe-mode mandatory-suite skips
- Machine: Not applicable
- Artifact/index: None
- Metrics: No live/provider/model/build execution; hidden durable-write case recorded zero transport/token access; late auth completions recorded no stale prompt/cache/callback mutation

## Findings and changed assumptions

- Production `lae-host` supplies Graph configuration and grant storage only; credential/transport injection is test-only composition.
- ActionJournal is absent by default and durable Graph writes are therefore expected to be withheld by the controller.
- Graph auth state, grants, proposals, write ledger, read cursors, and attestation markers are in-memory; restart coverage must assert behavior without exposing credential material.
- Host auth responses were checked for credential-like fields and bounded prompt fields across status/start/cancel/clear.
- Auth runs are identity-bound: cancellation detaches the old run immediately, and stale device/sleep/token completions cannot mutate a replacement run.
- Provider auth runs now also span post-token `/me` verification: stale success, rejection, unauthorized response, and finally paths cannot replace or clear a newer identity/grant; clear during a pending check stays cleared.
- Public host auth status/control responses expose only `state`, bounded prompt fields, and `account_verified`; the full fingerprint remains internal for account/grant binding.
- The public auth projection accepts only the provider module's frozen, private-capability status records; generic, forged, or mutated controls resolve to `unavailable`.
- The launcher now exposes an importable composition seam that builds the actual registry/controller/HostServer graph without listening; production defaults still select concrete engines and no credential/transport injection is wired through configuration.
- `account_verified` is emitted only from the provider's canonical identity match; arbitrary nonempty or malformed fingerprint values remain unverified.
- Graph `/me.id` follows the documented bounded, non-whitespace string identity contract (UUIDs are accepted but not required), and public device-code prompts accept bounded short protocol strings while refusing controls, whitespace, format characters, and credential-like aliases.
- These identity and prompt checks preserve the protocol's documented string semantics rather than imposing an unsupported UUID or segmented-code wire format.
- Durable Graph writes remain unavailable without a bound ActionJournal; existing Teams/send and other post-dispatch ambiguity paths remain at-most-once/manual-reconciliation cases and are not reopened or replayed here.
- Controller admission checks journal readiness before any durable-tool preview, authorization, or provider callback; all Graph mutation names are tested at zero token/transport calls when absent or unhealthy.
- Journal health diagnostics preserve only bounded canonical `action_journal_*` failure codes; missing, malformed, throwing, or noncanonical health is a finite unavailable refusal.
- Host close is idempotent and shuts down an unlistened engine; bootstrap closes a composition if listen fails.
- Host close now attempts revoke/cancel, provider shutdown, server closure, and engine shutdown independently exactly once; failures return only a finite `host_shutdown_failed` code after all attempts.

## Blockers

- Fact/evidence: No blocker remains in the bounded auth/controller/cleanup lifecycle under abort-insensitive mocked device, sleep, token, `/me`, provider-shutdown, server-close, and engine-shutdown failures.
- Impact: Live readiness remains unavailable by design; safe QA remains BLOCKED because mandatory suites and target/provider/model/native evidence are not executed in safe mode.
- What was tried: Deterministic same-instance coalescing, cancel→immediate restart, late completion in both identity orders plus old rejection/unauthorized, clear/revocation, all Graph mutation pre-preview, unlistened/listen-failure cleanup, listened provider-failure cleanup, and injected server/engine failure tests.
- Proposed workaround: None; retain explicit ActionJournal, credential, target, and live-provider gates.
- Decision/asset needed: Parent review of the committed candidate.
- Owner: S3
- Independent work continuing: None; candidate is ready for review.

## Handoffs

- To: S0 / parent
- Handoff file: This status packet and final commit report
- Required by: Review before merge
- Acknowledged: Pending

## Next bounded action

Review and, if accepted, merge the auth lifecycle/public projection repair; retain the existing separate gate that durable Graph writes need an ActionJournal.

## Sol action requested

Review when the test-only candidate is committed.
