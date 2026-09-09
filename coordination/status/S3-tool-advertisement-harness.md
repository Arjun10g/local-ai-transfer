# Status Packet

## Historical-status banner — superseded 2026-09-08

This packet predates the current `main@ec84d9e` truth refresh. Its 28-tool
catalogue and interval claims are historical. The current evaluation fixture
identity is 33 tools and 37 cases with `max_cases=64` ceiling semantics; this
does not authorize runtime advertisement, live provider use, compile, or target
execution. Remote execution remains false and release/full access remains
`BLOCKED` / `NOT_READY`. Read-only ledger preflight is source-merged by
`83cffab` (candidate `b777b54`) and independently audited, but
`SAFE_TO_MIGRATE_NOW=NO`; unsafe legacy evidence, orphan receipts, and
unmatched incidents remain adjudication blockers. No genesis, spend
authorization, or provider execution is supplied. Prefix fix `089d05d` and
31/31 postmerge integration are source facts, but evidence remains
parse-refused/unavailable under unsafe permissions. Phase-2a single-owner
topology is merged at `2ad3006` from `03885df` with two independent audits and
postmerge integration passed; proof issuer, process mutation, product
activation, and compile/live/target evidence remain absent. Phase-3 controller
arbitration is merged at `71537d0` after two final source accepts, but remains
inert with no native bridge, proof issuer, or live process. Strict lifecycle
sync `0bbfc77` records 117 lifecycle tests plus separately scoped related audit
evidence. Final checks report broad Node 343/342/0/1 TODO and independent Node
187/186/0/1 same TODO (parent-directory replacement race), Python 241/241, and
diff-check pass. QA discovered 54 with 0 missing/unknown, 1 skeleton pass, 59
safe-mode skips, and 60 expected release blockers; overall QA remains
`BLOCKED`. Phase-2b is design-only/in progress.

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Agent/Tools — production capability advertisement and mocked vertical controller evidence
- **Timestamp (UTC):** 2026-09-05T02:36:51Z
- **Branch/worktree:** `luna/production-tool-advertisement-harness` / `wt-tool-advertisement-harness`
- **Current phase:** Phase 4 autonomous tool controller, first production slice only
- **Primary task ID:** TOOL-018
- **Secondary task ID, if any:** TOOL-025 configured capability bundle
- **Task state:** READY_FOR_REVIEW
- **Last merged `main` commit:** `6dc9fb9`

## Objective for this work interval

Make the model-visible tool bundle truthful by excluding providers and local capabilities that immutable startup configuration cannot support, while retaining configured Graph authentication bootstrap semantics, then add a lightweight mocked HostServer/controller harness proving strict proposal, schema validation, preview/confirmation, authorization, execution, tool-result insertion, and same-turn model continuation for representative read and mutating tools.

## Inputs and dependencies

- Contract/version: tool envelope and assistant events v0.1.0; strict host config and production tool registry on `main`
- Required commits: current `main` `6dc9fb9`; parent Sol assignment for blocker B-003's first production slice
- Model/build/profile IDs: fixture/mock engine only; no weights, provider, browser, native build, or target execution
- Handoffs consumed: B-003; Sol clarification to filter by explicit immutable configured capability metadata, not volatile auth/session readiness

## Work completed

- Read the mandatory repository, governance, execution, coordination, and S3 session instructions.
- Created a fresh branch/worktree at exact current `main` `6dc9fb9`; no product edit preceded this packet.
- Changed production local and external registries to insert only capabilities supported by an immutable startup-configuration snapshot. The snapshots are frozen, non-enumerable metadata and are not sent to the model.
- Kept only `time.now` and `system.get_info` as unconditional local tools. Workspace reads/writes now track read/write configuration; disabled process/browser/app capabilities and non-Windows clipboard are absent. Windows filesystem and subprocess-backed clipboard/app/default-browser/process/Copilot/browser-action capabilities remain absent even when configured because their required safe native boundaries do not exist; explicit immutable test-only injections retain mocked unit seams.
- Limited configured Graph advertisement to explicitly requested delegated scopes while keeping device-code-authenticated tools visible before sign-in. Copilot additionally requires a workspace and browser actions require an executable allowlist; browser mutations retain their separate safe-actions gate.
- Separated the all-definition evaluation catalog from the production registries so model-evaluation schema parity cannot accidentally become an execution bundle.
- Added a mocked loopback HostServer/controller test covering valid read proposal/preview/policy execution, confirmed mutation with exact user authorization, two tool-result continuations, final answer, ordered typed events, and invalid-schema refusal before preview/execution.

## Evidence

- Commits: `92bcbdc` startup claim; `cbf63a2` implementation and focused tests
- Commands: `node --check` on changed production modules and the vertical test; three bounded `node --test` invocations; `git diff --check`
- Tests: 2/2 new capability/HostServer vertical tests; 3/3 selected existing external-registry/controller tests; 2/2 production definition-catalog parity tests; all pass in 1.5 seconds total command time
- Machine: local macOS development host; mocked/static evidence only
- Artifact/index: `tests/host/tool-chain-vertical.test.mjs`
- Metrics: no model/provider/browser/native process was started; no account, network, build, or broad suite was used

## Findings and changed assumptions

- Capability advertisement must distinguish immutable configuration support from transient readiness. A configured Graph provider may remain model-visible while unauthenticated because the provider owns a bounded authentication bootstrap; disabled or structurally unconfigured providers must not appear.
- Local filesystem, process, browser, application, and clipboard advertisement must likewise fail closed when their required configured/platform boundary is absent.
- The current process implementation resolves cwd with write authorization. This slice therefore omits the entire configured process tool if any action lacks a configured writable cwd workspace; changing that contract requires a separate policy decision.
- The 28-tool production evaluation fixture is now explicitly a definition catalog, not a claim about one runtime's advertised bundle.

## Blockers

- Fact/evidence: the mocked vertical harness proves host/controller semantics only; executable identity, durable ambiguous-write receipts, real provider/account behavior, browser safety, Windows package execution, and production model selection remain unproven.
- Impact: B-003 and B-005 remain open; no target, live-tool, full-access, or release-readiness claim is made.
- What was tried: immutable fail-closed registry filtering plus representative synthetic read/mutation and invalid-schema HTTP paths.
- Proposed workaround: keep affected Windows tools absent and providers disabled by default until their independent implementation/audit/target gates pass.
- Decision/asset needed: S0/S4 semantic and security review; later approved synthetic account/browser/Windows evidence.
- Owner: S0/S4 and the remaining implementation lanes.
- Independent work continuing: none within this bounded first slice.

## Handoffs

- To: S0/S4
- Handoff file: this packet and `tests/host/tool-chain-vertical.test.mjs`
- Required by: B-003 first-slice review
- Acknowledged: S0 assignment received

## Next bounded action

S4 independently review registry closure, scope mapping, Windows exclusions, and the HTTP confirmation/continuation harness; Sol may then merge `cbf63a2` without treating either blocker or readiness gate as closed.

## Sol action requested

Review/merge `cbf63a2`; keep B-003/B-005 and all target/live/full-access gates open.
