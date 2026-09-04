# Handoff H-003 — Fixture Host Contracts and Engine Adapter

- **From:** S3
- **To:** S1 and S4
- **CC:** S0 Sol
- **Timestamp (UTC):** 2026-09-04T03:38:00Z
- **Related task IDs:** TOOL-001, TOOL-002, TOOL-004, TOOL-005, TOOL-006, TOOL-007, TOOL-008
- **Priority:** Critical path
- **Needed by milestone:** M1 / Phase 1 contract conformance

## Request

Review the frozen v0.1.0 tool-envelope, assistant-event, and config contracts and map S1’s native engine streaming/cancellation/session API to the replaceable `FixtureEngineClient` boundary used by `ConversationController`.

## Why this is needed

S3 has a runnable fixture vertical slice while S1’s `RUN-002` engine contract is still being built. A narrow adapter lets host/UI tests run now without inventing native engine semantics. S1 must confirm the real stream frames can provide equivalent text deltas, complete tool-call framing, usage/finish metadata, cancellation, and health/shutdown behavior before real integration.

## Inputs

- Files/contracts: `contracts/tool-envelope/v0.1.0.json`, `contracts/tool-envelope/result-v0.1.0.json`, `contracts/assistant-events/v0.1.0.json`, `contracts/config-schema/v0.1.0.json`, `host/engine/fixture-engine.mjs`, `host/agent/controller.mjs`
- Commit SHAs: `2b4df2b` (initial vertical slice), `2ea6bf3` (hardening)
- Model/build/profile IDs: fixture-only `fixture-0.1.0`; no model or backend dependency
- Reproduction command: `node --test tests/host/*.test.mjs`; `node lae-host.mjs`
- Constraints: Node.js 24 built-ins only, loopback-only, no network/provider, no arbitrary shell, no secrets/content logs

## Required output

- File/API/fixture/decision: S1 engine stream-to-host compatibility note or contract fixture; identify native equivalents for `text_delta`, `tool_call_chunk`, `done`, cancellation, health, and shutdown.
- Version: compatible with assistant/tool contracts v0.1.0, or raise an interface-change request before breaking changes.
- Acceptance tests: split tool-call chunks withheld until strict parse; cancellation during stream; deterministic event order; reset/session isolation.
- Error/edge cases: malformed/oversized tool frame, unknown tool, provider disabled, engine crash/disconnect, cancellation before and during decode.
- Evidence: S1 commit SHA plus commands/results, copied to `coordination/status/S1.md` and Sol.

## Proposed interface or example

```js
for await (const frame of engine.generate({ requestId, messages, mode, signal })) {
  // { kind: 'text_delta', text }
  // { kind: 'tool_call_chunk', text }  // complete template event is assembled by host
  // { kind: 'done', finish_reason, usage }
}
engine.cancel(requestId);
await engine.health();
await engine.shutdown();
```

## Fallback

Keep `FixtureEngineClient` as the Phase 1 mock. Do not add a cloud/provider fallback or silently alter event semantics.

## Risks

- Security: real engine must remain authenticated/loopback and never receive host tool authority.
- Correctness: Qwen3.5 template-specific tool framing may need an adapter before strict envelope parsing.
- Performance: fixture delays are not latency evidence.
- Schedule: real integration remains blocked until S1’s engine API is frozen.

## Recipient acknowledgement

- Accepted/rejected: pending
- Owner: S1 for engine mapping; S4 for independent conformance/security rerun
- Planned task ID: RUN-002 / QA-004
- Expected checkpoint: before M1 integration
- Questions: Does native API emit complete structured tool events or raw chunks requiring an adapter? What are the exact cancellation and session-reset guarantees?

## Completion

- Output commit: pending consumer response
- Evidence: S3 `node --test tests/host/*.test.mjs` — 7 passed
- Consumer verification: pending S1/S4
- Sol decision/merge: S0 review and merge of `2b4df2b`, `2ea6bf3`
