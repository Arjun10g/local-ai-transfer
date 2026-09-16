# ADR-0007 — Local-first scope reduction: a personal assistant, not an enterprise release

- **Status:** **Accepted — ratified 2026-09-16 by explicit operator decision.**
  Records a deliberate reduction of scope and assurance. It approves no gate,
  produces no evidence, and makes no capability claim: it changes what the
  program is trying to prove, not what it has proved. It was drafted on
  2026-09-14 on the operator's instruction to re-scope the phases to a much
  lower burden of proof, and held as `Proposed` for two days specifically
  because ratification of a redefinition of done was not to be inferred from
  an answer to a different question. On 2026-09-16 the operator was asked
  directly whether to ratify this document and answered yes; that answer, and
  its landing on `main`, is the ratification.
- **Date:** 2026-09-14
- **Decision owner:** Operator (Arjun), relayed for recording
- **Authors/reviewers:** recorded by the monitoring session; requires Sol
  ratification on merge
- **Related tasks/risks:** B-001, B-002, B-003, B-004, B-005, B-006; supersedes
  the Phase 0-8 gate set in `coordination/RELEASE_GATES.md` as the current
  definition of done
- **Provenance:** operator decision, taken 2026-09-14 in response to a
  readiness report showing 0 of 9 gates approved and 6 of 6 blockers open, with
  the remaining work dominated by evidence no agent can produce.

## Context

The program was scoped as an enterprise Windows release. Its nine phases
require code signing, an approved trust anchor, a reproducible signed Windows
release, hardware/driver attestation on an exact Dell machine, corporate
approval references, and live Microsoft Graph accounts with consent.

As of `main@3f50a75` that structure had produced: **0 of 9 gates approved, 6 of
6 blockers open.** The source tree is in good order — 881 Python tests, 309
native static tests, contracts, offline gates that catch real defects — but
every remaining gate depends on procurement, corporate access, or hardware the
implementation lanes cannot obtain. The distance to "ready" was not a function
of further engineering.

The operator's actual goal, stated plainly: **a local model running on their
laptop with an Ollama-like setup, with terminal access, that is secure.**

That is a materially smaller product than the one the gates describe, and it is
reachable. Continuing to measure it against the enterprise gate set would
report permanent failure against requirements nobody intends to satisfy.

## Fixed constraints

- **Target:** the Dell laptop running Windows, with 32 GiB or more of RAM.
  Explicitly NOT the 8 GiB development Mac, which is measured at ~200 s per
  evaluation case and pages the 5.6 GB artifact from disk.
- **Model:** Qwen3.5-9B Q4_K_M is retained. The RAM requirement follows from
  keeping it rather than substituting a smaller quant.
- **Offline:** unchanged. The engine binds loopback only and performs no
  network egress of its own.
- **Memory:** the target must hold the 5.6 GB artifact resident; that is the
  substance of the 32 GiB floor.
- **Security:** proportionate to a personal machine. Containment of what the
  model can cause is retained in full. Supply-chain assurance about the
  artifact's provenance is what is being given up.
- **Dependency:** no corporate account, no signing authority, no attestation
  service, no second party.
- **Schedule:** operator-driven; no external deadline.

## Decision criteria

1. **Reachable without a second party.** A gate an implementation lane cannot
   close by working is not a gate, it is a wish.
2. **Retains containment.** Lowering assurance about provenance must not lower
   containment of behaviour. The model gets more access under this scope, not
   less, so the blast radius must stay bounded.
3. **Honest about what was surrendered.** A reduced burden of proof must be
   recorded as reduced, never quietly redefined as satisfied.
4. **Preserves the option to restore.** Parked work stays in the tree and in
   history; nothing is deleted to make the report look better.

## Options considered

### Option A — Keep the nine phases, lower each threshold

- Description: retain Phase 0-8 and weaken the evidence each demands.
- Benefits: no structural change; history stays legible.
- Costs: the phases are organised around artefacts (signing, attestation,
  Graph) that the new product does not contain. Weakening them produces gates
  that are satisfied vacuously.
- Risks: a vacuous gate reads as a real one later. This is precisely the
  failure the program's own governance warns about.
- Evidence: Phase 7 and Phase 8 have no content at all once signing and
  hardware attestation leave scope.

### Option B — Replace with a local-first gate set, park the rest

- Description: define five gates describing the local assistant, and move the
  enterprise requirements to an explicitly deferred section that gates nothing.
- Benefits: every gate is closable by working. Current truth becomes readable
  at a glance. Parked work is retained, not destroyed.
- Costs: a governance rewrite, and the loss of comparability with prior
  readiness reports.
- Risks: someone later reads the reduced set as the original bar. Mitigated by
  stating the surrender explicitly in the gate document itself.
- Evidence: the operator's stated goal maps cleanly onto five conditions, none
  of which needs a second party.

## Decision

**Adopt Option B.** The definition of done becomes five local-first gates —
L0 engine, L1 model quality, L2 terminal access, L3 security, L4 usability —
against the Dell/Windows target. The Phase 0-8 set is retained in
`RELEASE_GATES.md` as superseded history and gates nothing.

Scope explicitly **surrendered**, not satisfied:

- Code signing, approved trust anchor, controlled artifact custody
- Reproducible signed Windows release
- Hardware/driver attestation and the exact Dell target receipt
- Corporate approval references
- Performance percentile evidence (cold/warm, p95, exact-device placement)

Scope explicitly **parked** and reversible, gating nothing:

- Microsoft Graph tools (mail, teams) and their live-account proof
- Browser automation and Copilot tools
- The attestation-grade Windows launch authority in its full form

Scope explicitly **retained in full**:

- Loopback-only binding, bearer auth, no engine-initiated network egress
- Tool-envelope validation and fail-closed parsing
- The action journal and its audit record
- Bounded output, bounded context, typed refusals

## Rationale

Criterion 1 eliminates Option A outright: Phases 7 and 8 cannot be lowered into
reachability, because with signing and attestation removed they describe
nothing. Criterion 2 is the reason terminal access does not travel with a
general loosening — see Consequences. Criterion 3 is served by naming the
surrendered items in the gate document rather than in this ADR alone, since
that is the file a future reader treats as truth. Criterion 4 is why the Graph
and browser tool work is parked rather than deleted; it is merged, tested
source that costs nothing to retain and would be expensive to rebuild.

## Consequences

### Positive

- Every gate becomes closable by an implementation lane without a second party.
- B-001, B-002 and B-004 leave the critical path; B-003 is parked with the
  Graph tools; B-006 survives as the real product-quality gate (now L1).
- Today's paid evaluation run produces evidence against L1 rather than against
  a Phase 2 that could not be approved regardless.

### Negative / accepted trade-offs

- **No provenance assurance.** Nothing will attest that the running binary is
  the reviewed one. On a personal machine the operator accepts this; it would
  be disqualifying for redistribution.
- **No target attestation.** "Works on the Dell" will rest on the operator's
  own observation, not a hardware receipt.
- **Not comparable to prior readiness reports.** A future "L-gates green" must
  never be reported as satisfying Phase 0-8.
- **Terminal access widens the blast radius**, deliberately, and the only
  control standing between a prompt-injected tool call and execution on the
  operator's machine is the confirmation step. That control is now
  load-bearing in a way no previous tool made it.

### Required implementation changes

- Owners/tasks: new `LOCAL-TERMINAL-CONFIRM-001` (design and review of the
  any-command-with-confirmation tool); `RELEASE_GATES.md` rewrite; `BLOCKERS.md`
  critical-path reclassification.
- Contracts: `contracts/external-tools` gains a terminal action whose schema
  admits an arbitrary command string; `SECURITY_AND_TOOL_POLICY.md` must state
  the confirmation requirement as a security control, not a UX affordance.
- Tests: confirmation cannot be bypassed, batched, or defaulted to yes; the
  exact command text presented equals the text executed; output is bounded; a
  refused command executes nothing and is journalled.
- Migration/rollback: reverting this ADR restores Phase 0-8 as current truth;
  no source is deleted by it.

## Validation

- Experiment/test: the L-gates in `RELEASE_GATES.md` each name their own
  acceptance evidence.
- Acceptance threshold: per gate; L1 inherits the tool-call score threshold
  from `execution/ACCEPTANCE_CRITERIA.md` rather than inventing a new one.
- Artifact: receipts on the Dell target, retained in `artifacts/`.
- Revisit trigger: any intent to distribute the product to a second machine or
  a second person immediately re-opens the surrendered supply-chain items.

## Approval

- Operator: **ratified 2026-09-16**, in answer to a direct question about this
  document specifically.
- Sol: recorded and merged on the operator's ratification.
- Affected Luna acknowledgements: not sought — no implementation lane was active
  at ratification.
