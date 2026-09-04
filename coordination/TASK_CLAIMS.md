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
| TOOL-001 | S3 | `luna/agent-tools` | CLAIMED | SOL-002 | S0, S1, S4 | pending |
| TOOL-002 | S3 | `luna/agent-tools` | CLAIMED | TOOL-001, RUN-002 | S0, S1, S4 | pending |
| QA-001 | S4 | `luna/qa-release` | CLAIMED | SOL-001 | S0 | pending |
| SEC-001 | S4 | `luna/qa-release` | CLAIMED | QA-001, SOL-003 | S0 | pending |

