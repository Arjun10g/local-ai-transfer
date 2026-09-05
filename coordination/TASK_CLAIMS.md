# Live Task Claims

State semantics: source implementation, evidence, and gate approval are
separate. `MERGED_SOURCE_PENDING_GATE` means source is on `main` only; it is
not a release or live-readiness approval. `PENDING_INDEPENDENT_AUDIT` and
`REJECTED_REPAIR_PENDING` are not approval states.

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
| TOOL-018 | S3 | `luna/production-tool-advertisement-harness` | READY_FOR_REVIEW | TOOL-013, TOOL-022, RUN-020 | S0, S4 | `cbf63a2`; immutable configured-capability filtering, Windows subprocess exclusions, and mocked HostServer/controller vertical harness |
| TOOL-022 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-003, SOL-003 | S0, S4 | `2f811f1`, `e30b06c`, `95805dc`, `4405ba7`, `bdc78df`, `83931e7`; strict provider contract/controller schema wiring; explicit config mapping; T0 enum validation; browser-action schemas/status; exact-IP DNS/CDP/startup/cleanup/replay/path hardening; disabled/unconfigured/fake-only evidence; focused 71-pass run and full npm 105-pass run |
| TOOL-023 | S3 | `main` (merged from `luna/graph-action-reconciliation`) | MERGED_SOURCE_PENDING_GATE | TOOL-022, SOL-003 | S0, S4 | `b4702a5`; two independent source-safety approvals; 96-pass mocked post-merge run. Production journal and live-provider evidence remain unavailable |
| TOOL-024 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-022, SOL-003 | S0, S4 | `2f811f1`, `e30b06c`, `a1230c8`; prompt-only Copilot stdin bridge with supported flags, minimal env, redaction/version/cancel tests, isolated provider status |
| TOOL-025 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-022, SOL-003 | S0, S4 | `f30ad1b`, `d5cc74c`; bounded operator-configured process.run_allowlisted with strict per-action schemas, absolute executable/interpreter denylist, writable workspace cwd binding, canonical executable identity preview/dispatch binding, exact authorization objects, integer argv parity, minimal env/stdin/output bounds, cancellation/tree cleanup, at-most-once ledger, grant revocation, and focused/full green evidence |
| TOOL-026 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-023, TOOL-024, SOL-003 | S0, S4 | `7e723d6`, `47f072a`, `890f600`, `de8d674`, `c6052d3`, `8c2c0dd`, `39a8be8`, `b2dac29`; historical Graph/UI predecessor plus browser/Copilot hardening; Graph reconciliation is tracked separately by TOOL-023 at `4332e1a` and remains pending audit, not merged/live |
| QA-REMOTE-001 | S3 | `main` | MERGED_SOURCE_PENDING_GATE | TOOL-023, TOOL-024, TOOL-026, SOL-003 | S0, S4 | marker-gated hostile harness source is present on `main`; its remote/live execution, evidence, and gate approval remain pending |
| QA-REMOTE-002 | S3 | `luna/agent-tools` | REJECTED_REPAIR_PENDING | QA-REMOTE-001, SOL-003 | S0, S4 | lifecycle/evidence line `e5` rejected; repair pending. Remote Shadeform execution remains disabled; no target receipt or live result claimed |
| TOOL-027 | S3 | `luna/agent-tools` | READY_FOR_REVIEW | TOOL-022, SOL-003 | S0, S4 | pending commit; opt-in browser safe-actions gate with configured HTTPS origins, inspected opaque handle/page bindings, strict text-field/no-navigation-button classes, T3 confirmation, post-action URL and validating-proxy egress checks; generic mutations remain test-only |
| TOOL-032 | S3 | `main` (merged from `luna/durable-action-journal-core`) | MERGED_SOURCE_PENDING_GATE | TOOL-013, TOOL-018 | S0, S4 | `55f3dfd` implementation; `65decba` merge; durable action/egress journal and controller barrier are source-merged, but independent evidence, production pathname-store availability, and gate approval remain pending |
| TOOL-WINFS-REFUSAL | S3 | `luna/windows-fs-refusal-slice` | READY_FOR_REVIEW | B-005, TOOL-014, TOOL-015 | S0, S4 | post-rebase repair commit; explicit Windows filesystem/package NOT_READY gates, outermost refusal, preserved `local_capabilities` status, and focused zero-mutation evidence in `coordination/status/S3.md`; no helper activation; `SAFE_FOR_TARGET_EXECUTION=NO` |
| TOOL-034 | S3 | `luna/native-journal-protocol-slice` | READY_FOR_REVIEW | TOOL-032, B-005-SOURCE | S0, S1, S4 | `6c43290`, `813914a`; bounded canonical/HMAC journal protocol, strict query/high-watermark and manual-resolution correlation, synthetic vectors, and focused adversarial tests only; no helper/store/transport/activation and production availability remains false |
| QA-001 | S4 | `luna/qa-release` | CLAIMED | SOL-001 | S0 | pending |
| QA-SAFE-RUNNER | S4 | `luna/qa-safe-runner` | READY_FOR_REVIEW | QA-001, SOL-003 | rebased repair `4c0aa29` plus new-main inventory port `ff02b0a` on `main@91de464`; exact whole-test-tree classification includes the merged lifecycle tests, strict bounded TAP, POSIX dir-fd/reopened-identity create-new output with exact durable failure cleanup and private final parent, early Windows file-output refusal, metadata-only package planning; 21 repair-focused tests pass; release remains BLOCKED |
| SEC-001 | S4 | `luna/qa-release` | CLAIMED | QA-001, SOL-003 | S0 | pending |
| RUN-019 | S1 | `main` | IN_PROGRESS | RUN-010, MODEL-004 | S0, S4 | real tool-role/template pipeline and tests pending |
| RUN-020 | S1 | `main` | IN_PROGRESS | RUN-019, TOOL-001 | S0, S4 | structured Qwen call normalization evidence pending |
| MODEL-006 | S2 | `main` | IN_PROGRESS | MODEL-002, TOOL-014 | S0, S4 | bounded local GGUF evaluation pending |
| MODEL-007 | S2 | `main` | IN_PROGRESS | MODEL-006, RUN-020 | S0, S4 | tool prompt/bundle evaluation pending |
| QA-010 | S4 | `main` | IN_PROGRESS | TOOL-018, MODEL-006 | S0 | heavy autonomous tool tests pending |
| SEC-006 | S4 | `main` | IN_PROGRESS | RUN-020, TOOL-019 | S0 | parser/schema adversarial evidence pending |
| SOL-G4 | S0 | `main` | IN_PROGRESS | RUN-020, MODEL-007, QA-010 | S4 | release-hardening review in progress |
| RUN-025 | S1 | `luna/native-release-hardening` | READY_FOR_REVIEW | independent native/release audit findings | S0, S4 | `4d98354`, `37da175`; immutable model identity, strict runtime config, validation lease, exact Windows plan launch |
| REL-004 | S1 | `luna/native-release-hardening` | READY_FOR_REVIEW | RUN-025, QA-001 | S0, S4 | `65e990c`, `305ec99`, `983c340`, `843d179`, `e3c42f3`, `1f9ca92`; canonical QA skip semantics, pinned portable Node/host closure, packaged Copilot context wiring, bounded PowerShell pipes, ReparsePoint rejection, byte-safe bearer checks, one-shot bootstrap and Job supervisor; native Windows acceptance unproven |
| B-005-SOURCE | S1 | `main` | QUARANTINED | B-005, TOOL-017, TOOL-021, TOOL-024 | S0, S4 | `89313bf`, merge `fb62c20`; independent source audit passed and 11 tiny static tests passed on main; process creation absent, launch refused before child creation, trust/containment/confinement gates false; no compile/runtime proof, build/package/integration, target execution, or activation |
| QA-020 | S4 + operator | `luna/windows-acceptance` | READY_FOR_REVIEW | REL-010, PERF-019 | S0 | `0c477f6`; exact Dell/Intel/Vulkan receipt, CPU/candidate/portable lifecycle harness, consented live-check plan, 11 focused static/gate tests; target execution remains unproven |
