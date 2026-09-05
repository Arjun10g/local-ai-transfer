# Status Packet

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Agent/Tools — durable generic action-journal core and controller dispatch barrier
- **Timestamp (UTC):** 2026-09-05T03:21:19Z
- **Branch/worktree:** `luna/durable-action-journal-core` / `wt-action-journal-core`
- **Current phase:** Phase 4/6 autonomous action recovery hardening
- **Primary task ID:** TOOL-032
- **Task state:** READY_FOR_REVIEW
- **Base:** exact `main` `37a134f`

## Outcome

Implemented a provider-independent, bounded per-operation JSONL/hash-chain journal and made its fsynced `dispatching` transition a hard controller barrier for side-effecting tools and confirmed Copilot egress. Missing or unhealthy journals remove those tools from every model generation and still reject hostile direct proposals. Reads remain unjournaled.

The POSIX store requires a precreated canonical current-user `0700` directory, creates `0600` records, uses bounded/O_NOFOLLOW/identity-checked I/O, and fails closed on malformed/torn/over-limit state. Windows is explicitly unavailable until a native ACL/reparse-safe handle-relative store exists. Receipts export digests, never request/call IDs, arguments, previews, content, results, prompts, or credentials.

Startup cancels pre-dispatch records, changes `dispatching` to `unknown_manual`, and never manufactures completion from `acknowledged`/`reconciling`. Exact unresolved tool+argument retries are refused across request/call IDs and nondeterministic preview revisions. Terminal records are durably pruned oldest-first; active records are never pruned.

Authenticated loopback summary/detail endpoints and assertion-only terminal resolution are bounded. Reconciliation returns typed HTTP 501 and never invokes/replays a provider. Unverified Graph/browser acknowledgements remain `reconciling` and are surfaced to the model as `action_completion_unverified`.

## Evidence

- Product commit: `2cf1f8bd4f5499e0091d663047b6aa6495cc070e`
- Focused syntax checks: `node --check` for journal, controller, HostServer, and launcher — PASS
- `node --test tests/host/action-journal.test.mjs tests/host/tool-chain-vertical.test.mjs` — 21 pass, 0 fail
- Targeted provider/controller compatibility: `node --test --test-name-pattern='controller advertises|controller stages|process schema' tests/host/external-tools.test.mjs tests/host/process-run.test.mjs` — 3 pass, 0 fail
- Additional mocked/static regression run before final commit: 58 pass, 1 unrelated pre-existing Copilot test-fixture failure, 1 TODO. The failure occurs during `CopilotCliProvider` construction because that older test injects `readContext` without the now-required `testOnly` gate, before any journal/controller code executes.
- No native build, model/provider/account/browser access, broad/heavy suite, or external network was used.

## Residual blockers

- Windows journal execution is NOT READY and intentionally fails `action_journal_platform_unavailable`; a native ACL/reparse-safe, handle-relative protected store is required.
- Graph/browser provider idempotency and reconciliation adapters are not implemented. Their mutations cannot be represented as completed by this core.
- Changing action arguments can evade core argument-digest duplicate refusal; provider-semantic idempotency/reconciliation remains mandatory.
- Deployment must provision the canonical private POSIX journal directory and set `LAE_ACTION_JOURNAL_DIR`; without it all durable actions/egress are truthfully hidden and fail closed.
- This slice does not establish provider, browser, model, target-laptop, or release readiness.

## Handoff

S0/S4 should independently review `host/agent/action-journal.mjs`, controller transition ordering, operator endpoint authorization/status mapping, and the crash-injection suite. Follow-on owners must add provider completion proof/reconciliation and the native Windows store before enabling those target paths.
