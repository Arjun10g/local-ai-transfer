# Live Task Claims

| Task | Owner | Branch | State | Dependencies | Review owner | Evidence |
|---|---|---|---|---|---|---|
| SOL-001 | S0 | `main` | IN_PROGRESS | none | S4 | `coordination/` |
| SOL-002 | S0 | `main` | READY_FOR_REVIEW | SOL-001 | S1–S4 | `coordination/adrs/ADR-0001.md` |
| SOL-003 | S0 | `main` | IN_PROGRESS | SOL-001 | S4 | `coordination/POLICY_STATUS.md` |
| SOL-005 | S0 | `main` | IN_PROGRESS | SOL-003 | S2, S4 | `coordination/STATUS.md` |
| RUN-001 | S1 | `luna/runtime` | CLAIMED | SOL-002 | S0, S3, S4 | pending |
| RUN-002 | S1 | `luna/runtime` | CLAIMED | RUN-001 | S0, S3, S4 | pending |
| MODEL-001 | S2 | `luna/model-performance` | CLAIMED | SOL-002 | S0, S1, S4 | pending |
| PERF-001 | S2 | `luna/model-performance` | CLAIMED | SOL-003 | S0, S4 | pending |
| TOOL-001 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | SOL-002 | S0, S1, S4 | `2b4df2b`, `2ea6bf3`, `fc25f71`; `tests/host/fixture-host.test.mjs` |
| TOOL-002 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-001, RUN-002 | S0, S1, S4 | `2b4df2b`, `2ea6bf3`, `fc25f71`; `contracts/assistant-events/`, `host/agent/controller.mjs` |
| TOOL-004 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | RUN-002, TOOL-002 | S0, S1, S4 | `2b4df2b`, `a8e601a`; `lae-host.mjs`, `host/server/`, `host/engine/native-engine-client.mjs` |
| TOOL-005 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-002 | S0, S4 | `2b4df2b`; `ui/` |
| TOOL-006 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | RUN-007, TOOL-004 | S0, S4 | `2b4df2b`; `host/agent/controller.mjs`, tests |
| TOOL-007 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-001 | S0, S4 | `2b4df2b`; `host/agent/tool-envelope.mjs` |
| TOOL-008 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-006, TOOL-007 | S0, S4 | `2b4df2b`; `host/tools/time-now.mjs`, tests |
| TOOL-014 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-007, TOOL-013 | S0, S4 | `197649d`, `2e4c2fc`, `c706a2b`, `acca535`; `host/tools/local/workspace-policy.mjs`, `filesystem.mjs`, strict schemas, nested-search/race tests |
| TOOL-015 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-013, TOOL-014 | S0, S4 | `197649d`, `2e4c2fc`, `c706a2b`, `acca535`; create-new/base-hash atomic patch, final target recheck, preview tests |
| TOOL-016 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-013 | S0, S4 | `197649d`, `2e4c2fc`, `c706a2b`; typed Windows clipboard modules/offline tests and strict args |
| TOOL-017 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-013 | S0, S4 | `197649d`, `2e4c2fc`, `c706a2b`; allowlisted app/browser argv, external-egress gating/preview, strict args tests |
| TOOL-022 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-003, SOL-003 | S0, S4 | `2f811f1`, `e30b06c`, `95805dc`, `4405ba7`, `bdc78df`, `83931e7`; strict provider contract/controller schema wiring; explicit config mapping; T0 enum validation; browser-action schemas/status; exact-IP DNS/CDP/startup/cleanup/replay/path hardening; disabled/unconfigured/fake-only evidence; focused 71-pass run and full npm 105-pass run |
| TOOL-023 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-022, SOL-003 | S0, S4 | `2f811f1`, `e30b06c`; Graph Outlook/Teams endpoints, bounded hostile response projections, host revisions, at-most-once writes, profile/grant tests, explicit account/profile/scope mapping |
| TOOL-024 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-022, SOL-003 | S0, S4 | `2f811f1`, `e30b06c`, `a1230c8`; prompt-only Copilot stdin bridge with supported flags, minimal env, redaction/version/cancel tests, isolated provider status |
| TOOL-025 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-022, SOL-003 | S0, S4 | `f30ad1b`, `d5cc74c`; bounded operator-configured process.run_allowlisted with strict per-action schemas, absolute executable/interpreter denylist, writable workspace cwd binding, canonical executable identity preview/dispatch binding, exact authorization objects, integer argv parity, minimal env/stdin/output bounds, cancellation/tree cleanup, at-most-once ledger, grant revocation, and focused/full green evidence |
| TOOL-026 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-023, TOOL-024, SOL-003 | S0, S4 | `7e723d6`, `47f072a`, `890f600`; Graph/UI hardening plus rebased browser-control and production selected-workspace Copilot context tranche (pending tranche commit); focused mocked 47-pass evidence, no heavy suite per laptop constraint |
| QA-001 | S4 | `luna/qa-release` | CLAIMED | SOL-001 | S0 | pending |
| SEC-001 | S4 | `luna/qa-release` | CLAIMED | QA-001, SOL-003 | S0 | pending |
| RUN-019 | S1 | `main` | IN_PROGRESS | RUN-010, MODEL-004 | S0, S4 | real tool-role/template pipeline and tests pending |
| RUN-020 | S1 | `main` | IN_PROGRESS | RUN-019, TOOL-001 | S0, S4 | structured Qwen call normalization evidence pending |
| MODEL-006 | S2 | `main` | IN_PROGRESS | MODEL-002, TOOL-014 | S0, S4 | bounded local GGUF evaluation pending |
| MODEL-007 | S2 | `main` | IN_PROGRESS | MODEL-006, RUN-020 | S0, S4 | tool prompt/bundle evaluation pending |
| QA-010 | S4 | `main` | IN_PROGRESS | TOOL-018, MODEL-006 | S0 | heavy autonomous tool tests pending |
| SEC-006 | S4 | `main` | IN_PROGRESS | RUN-020, TOOL-019 | S0 | parser/schema adversarial evidence pending |
| SOL-G4 | S0 | `main` | IN_PROGRESS | RUN-020, MODEL-007, QA-010 | S4 | release-hardening review in progress |
