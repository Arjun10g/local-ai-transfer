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
| TOOL-014 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-007, TOOL-013 | S0, S4 | `197649d`, `2e4c2fc`; `host/tools/local/workspace-policy.mjs`, `filesystem.mjs`, tests |
| TOOL-015 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-013, TOOL-014 | S0, S4 | `197649d`, `2e4c2fc`; create-new/base-hash atomic patch + preview tests |
| TOOL-016 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-013 | S0, S4 | `197649d`, `2e4c2fc`; typed Windows clipboard modules/offline tests |
| TOOL-017 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-013 | S0, S4 | `197649d`, `2e4c2fc`; allowlisted app/browser argv modules/tests |
| QA-001 | S4 | `luna/qa-release` | CLAIMED | SOL-001 | S0 | pending |
| SEC-001 | S4 | `luna/qa-release` | CLAIMED | QA-001, SOL-003 | S0 | pending |
