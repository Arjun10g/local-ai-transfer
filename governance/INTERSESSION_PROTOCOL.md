# Five-Session Coordination Protocol

## 1. Objective

Five independent model sessions must operate as one engineering team without duplicating work or relying on private chat state. The repository is the shared blackboard. GPT-5.6 Sol owns orchestration; four GPT-5.6 Luna sessions own execution lanes.

## 2. Session identities

| ID | Model | Name | Primary ownership |
|---|---|---|---|
| S0 | GPT-5.6 Sol | Orchestrator | Planning, contracts, review, integration, gates |
| S1 | GPT-5.6 Luna | Runtime | Native engine, backend adapter, serving, session/KV lifecycle |
| S2 | GPT-5.6 Luna | Model/Performance | Model verification, hardware, reference, acceleration, caching, benchmarks |
| S3 | GPT-5.6 Luna | Agent/Tools | Host, UI, tool parser, tools, confirmations, web/local integration |
| S4 | GPT-5.6 Luna | QA/Release | Tests, security, fuzzing, packaging, SBOM, reproducibility, evidence |

## 3. Branch and worktree model

Recommended branches:

```text
main
sol/orchestrator
luna/runtime
luna/model-performance
luna/agent-tools
luna/qa-release
```

Recommended worktrees:

```text
../wt-sol
../wt-runtime
../wt-model-performance
../wt-agent-tools
../wt-qa-release
```

Rules:

- Sol creates the branches/worktrees.
- Each Luna writes only to its branch unless Sol creates an integration branch.
- Sol merges reviewed commits to `main`.
- Luna sessions rebase/merge from `main` only at Sol-specified checkpoints.
- Cross-lane changes are handled through a handoff, not an unannounced edit.
- Generated test evidence is stored outside Git when large; the index and checksum are committed.

## 4. Coordination directory created at kickoff

Sol creates:

```text
coordination/
  STATUS.md
  TASK_CLAIMS.md
  BLOCKERS.md
  DECISIONS.md
  INTERFACE_CHANGE_REQUESTS.md
  RELEASE_GATES.md
  status/
    S0.md
    S1.md
    S2.md
    S3.md
    S4.md
  inbox/
    S0/
    S1/
    S2/
    S3/
    S4/
  handoffs/
  adrs/
  evidence-index/
```

These are operational files for the actual repository. The templates in this pack are copied into them.

## 5. Single source of truth

- `execution/TASK_BOARD.md` defines initial tasks.
- `coordination/TASK_CLAIMS.md` records live ownership and state.
- `coordination/DECISIONS.md` points to accepted ADRs.
- Interface schemas under `contracts/` are authoritative.
- A chat statement that is not reflected in the repository is not binding.
- Sol resolves disagreement and records the decision.

## 6. Task states

Use exactly:

- `UNCLAIMED`
- `CLAIMED`
- `IN_PROGRESS`
- `BLOCKED`
- `READY_FOR_REVIEW`
- `CHANGES_REQUESTED`
- `MERGED`
- `DEFERRED`
- `CANCELLED`

Every active task has:

- Task ID.
- Owner.
- Branch.
- Inputs and dependency task IDs.
- Expected outputs.
- Acceptance tests.
- Current state.
- Last update timestamp.
- Review owner.
- Evidence path.
- Commit SHA when ready.

## 7. Work-in-progress limits

- Each Luna may have one primary implementation task and one small independent task.
- Sol may have at most four reviews open simultaneously.
- A worker may not claim a third task because another worker is blocked.
- High-risk interface work is serialized.
- Tests can be prepared in parallel before implementation merges.

## 8. Status cadence

A session posts a status packet:

- At startup.
- Before beginning a task.
- After any interface discovery.
- At a meaningful checkpoint.
- Within the repository before requesting review.
- Immediately on blocker.
- At session end.

The packet uses `templates/STATUS_PACKET.md`.

A status update must be concise enough for Sol to scan and detailed enough to reproduce the claim.

## 9. Peer-to-peer handoffs

Workers are expected to communicate directly through files while copying Sol.

Example:

- S1 needs a tool-event schema from S3.
- S1 writes `coordination/handoffs/H-014-S1-to-S3-tool-events.md`.
- S3 acknowledges in the file, provides a contract commit, and writes compatibility notes.
- Sol reviews the contract or delegates review.
- Both status files link the handoff.

A handoff includes:

- Producer and consumer.
- Exact request.
- Why it blocks or improves parallel work.
- Inputs.
- Required output.
- Interface/acceptance criteria.
- Due milestone.
- Proposed fallback.
- Sol decision required, if any.

Do not use a handoff to transfer vague responsibility.

## 10. Interface-first development

Sol freezes versioned contracts before dependent code:

```text
contracts/
  engine-api/
  assistant-events/
  tool-envelope/
  config-schema/
  model-manifest/
  metrics-schema/
  error-codes/
```

Rules:

- Consumers may build mocks against a frozen contract.
- Producers build conformance tests.
- Every contract has a semantic version.
- Breaking changes require an interface-change request and Sol approval.
- Additive changes still require notification.
- Contract examples are executable fixtures where possible.
- S4 owns contract conformance tests but not the product semantics.

This enables S1 and S3 to work in parallel while S2 and S4 build baselines/tests.

## 11. Interface change process

1. Requester writes to `INTERFACE_CHANGE_REQUESTS.md`.
2. Include current behavior, proposed behavior, affected tasks, migration, and test impact.
3. Affected workers comment.
4. Sol accepts, rejects, or requests a narrower change.
5. Accepted change gets an ADR or contract-version bump.
6. Producer and consumers update in coordinated commits.
7. S4 updates conformance tests.
8. Sol merges in dependency order.

No worker may “temporarily” break a contract on a shared branch.

## 12. Review flow

For a Luna task:

1. Worker runs all required tests.
2. Worker posts evidence and marks `READY_FOR_REVIEW`.
3. Sol assigns:
   - semantic review to Sol or a relevant peer,
   - test/security review to S4 when appropriate,
   - performance review to S2 when appropriate.
4. Reviewer records findings with severity:
   - `BLOCKER`
   - `MAJOR`
   - `MINOR`
   - `NIT`
5. Worker resolves findings and updates evidence.
6. Sol merges only after mandatory reviewers approve.
7. Task state becomes `MERGED`.

Sol must not approve based only on a prose summary when code/test artifacts are available.

## 13. Phase gate flow

At each phase end:

1. S4 produces a gate evidence index.
2. S1–S3 update risks and limitations.
3. Sol checks every exit criterion in `execution/PHASES.md`.
4. Sol records:
   - `PASS`
   - `CONDITIONAL_PASS`
   - `FAIL`
5. A conditional pass lists owner, deadline, scope, and rollback.
6. The next phase starts only after contracts and task assignments are updated.

## 14. Conflict resolution

Priority order:

1. Safety and policy.
2. Correctness.
3. Reproducibility.
4. Memory stability.
5. User experience.
6. Performance.
7. Convenience.
8. Code elegance.

When two workers propose different implementations:

- Write measurable decision criteria.
- Build the smallest fair experiment when feasible.
- S4 verifies methodology.
- Sol decides and records an ADR.
- Do not maintain two permanent MVP paths unless the architecture requires CPU/GPU fallback.

## 15. Blocker protocol

A blocker entry must include:

- Blocking fact, not speculation.
- Task and milestone impact.
- Evidence.
- What was tried.
- At least one safe workaround.
- Independent work the session will continue.
- Decision/asset needed and owner.
- Time when it becomes critical.

Workers do not wait silently. They continue on mocks, tests, docs, or another independent task.

## 16. Parallelization map

At a high level:

- S1 builds engine lifecycle/API while S2 establishes the model oracle and hardware profile.
- S3 builds host/UI/tool mocks against contracts while S1 builds the engine.
- S4 builds conformance, malformed-input, security, and packaging harnesses from day one.
- Once M2 exists, S3 integrates real streaming and tool calls.
- S1 and S2 optimize CPU/GPU/cache in parallel with S3 tool hardening.
- S4 continuously runs regression and release builds.
- Sol reviews contracts and merges narrow vertical slices rather than waiting for large branches.

## 17. Daily/iteration orchestrator loop

Sol repeats:

1. Read all five status files.
2. Reconcile task claims.
3. Identify critical path and idle capacity.
4. Resolve or route blockers.
5. Review new interfaces before implementation spreads.
6. Assign code reviews.
7. Merge small, green changes.
8. Update gate confidence.
9. Issue the next bounded task to each Luna.
10. Record decisions.

Sol should prefer a working vertical slice over broad partially complete subsystems.

## 18. Commit and evidence conventions

Commit prefix:

- `runtime:`
- `model:`
- `perf:`
- `tools:`
- `ui:`
- `qa:`
- `security:`
- `build:`
- `docs:`
- `contracts:`

Evidence directory example:

```text
artifacts/
  <build-id>/
    machine.json
    commands.txt
    tests/
    benchmarks/
    logs-redacted/
    checksums.txt
```

Large artifacts live in the approved artifact store. The repository stores a manifest and immutable identifier.

## 19. Manual relay fallback

When sessions do not share a filesystem:

- Sol is the relay.
- Workers return complete status/handoff Markdown and patch/commit references.
- Sol pastes the exact handoff into the target session.
- Sol records the relay in `coordination/`.
- Workers may not assume another session saw a message until acknowledgement is recorded.
- Interface files are always transmitted in full, not paraphrased.

## 20. Completion behavior

At the end of each session run, the model must state:

- What changed.
- What is proven.
- What remains uncertain.
- Exact next task.
- Handoffs waiting.
- Whether Sol action is required.

No session ends with an untracked local change or an undocumented blocker.
