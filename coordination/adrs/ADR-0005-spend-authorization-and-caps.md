# ADR-0005 — Standing provider-run spend authorization and program cost caps

- **Status:** Accepted
- **Date:** 2026-09-11
- **Decision owner:** S0 Sol
- **Authors/reviewers:** S0 Sol (decision); S2 and S4 (lifecycle and cost
  review); Luna docs (recording only)
- **Related tasks/risks:** B-004, B-006, SOL-003, `LEDGER-GENESIS-001`,
  `MODEL-COMPARATOR-EVAL-001`, `COMPARATOR-ENGINE-001`,
  `J1M-SALVAGE-TRANSPORT-001`, `J1M-KEY-HANDLE-001`; RISK register entries for
  remote spend and provider lifecycle
- **Provenance:** decision made by S0 Sol and relayed for recording. Sol's
  merge of this refresh to `main` is the ratifying act. Bookkeeping is not gate
  approval: this ADR authorizes bounded spend and records caps, and it advances
  no phase gate, no release gate, and no capability state.

## Context

Until 2026-09-11 no provider run could be launched for this program, because
there was no standing authorization to spend and no recorded cap against which
a run could be bounded. Every prior attempt was therefore either a read-only
catalogue query or a refusal.

Three facts changed on 2026-09-11 and together make a bounded authorization
decidable:

1. **The user granted standing authorization** to launch provider runs for this
   program, rather than approving each run individually by name.
2. **The lane that spends is now able to return what it is run to produce.**
   The first launch attempt (`remote-eval-20260911-a`) stopped *pre-spend* at
   USD 0.00 precisely because receipt salvage was unconditionally refused in
   source since `2d7db4f`; spending would have bought an unretrievable result.
   That refusal, and two further latent refusals found offline
   (`_persist_lifecycle`'s undefined `MAX_RECEIPT_BYTES` since `8e3f599`, and
   the ephemeral-key operand shape that `validate_persisted_argv` refused), are
   fixed and merged at `f89c1684ea051e1c9c92f944cb080d424a086f55`.
3. **An offline gate now proves the argv surface before any spend.**
   `scripts/j1m_dry_run.py` exercises the full command surface with no network,
   no provider call and USD 0.00, and refuses to pass when a precondition is
   unmet.

What was still missing was the cost boundary itself. This ADR supplies it.

## Fixed constraints

- **Target:** unchanged. No target-side execution, download, or configuration
  follows from this decision.
- **Model:** unchanged. Qwen3.5-9B, project-produced Q4_K_M from the pinned
  public revision; this ADR changes no model, quantization, or artifact
  identity.
- **Offline:** the dry-run gate must remain offline and must remain a
  precondition, not a formality.
- **Memory:** not affected.
- **Security:** no credential, token, private URL, or provider endpoint may
  enter Git as a consequence of this decision. The ephemeral key stays under
  `.secrets/`, which is gitignored, and is destroyed on every exit path.
- **Dependency:** no new runtime dependency.
- **Schedule:** none. A cap is not a schedule, and an unspent cap expires no
  authorization.

## Decision criteria

Ranked before options were considered:

1. **Fail closed on cost.** No run may be able to exceed a recorded cap, and no
   bookkeeping record may be readable as permission to exceed one.
2. **Every run must be retrievable.** Authorization is worthless if the run
   cannot return its receipts; a run that cannot publish evidence must refuse
   before it is billable rather than after.
3. **Bounded blast radius per run.** A single run's worst case must be small
   relative to the program cap, so that one bad run cannot consume the program.
4. **Separation of powers preserved.** Spend authorization must not become
   gate approval, capability approval, or release approval.
5. **Auditability.** Each run's cap must be recorded per run, and the ledger
   must reconcile.

## Options considered

### Option A — Per-run authorization by explicit name, no program cap

- **Description:** keep requiring a fresh named approval for each individual
  run, with no standing cap.
- **Benefits:** maximum control per run; no standing exposure.
- **Costs:** every run blocks on a human round trip, which is what produced the
  current state in which the model has never been measured against the profile
  it must ship against.
- **Risks:** the measurement backlog keeps growing; caps stay implicit and
  therefore unauditable.
- **Evidence:** the 2026-09-04 attempts and the 2026-09-11 attempt all stopped
  without a score.

### Option B — Standing authorization with a program hard cap and per-run caps

- **Description:** the user grants standing authorization to launch provider
  runs for this program; Sol records a program hard cap and a separate,
  explicitly recorded cap for each run; the existing lifecycle refusals are
  unchanged and remain the enforcement mechanism.
- **Benefits:** runs become possible without a per-run human round trip, while
  cost remains bounded at two independent levels and each run's bound is
  written down before it launches.
- **Costs:** requires the per-run cap to actually be recorded each time, which
  is a discipline cost on Sol.
- **Risks:** a standing authorization can be misread as a capability or gate
  approval. Mitigated by stating the opposite explicitly here and in every
  document that references this ADR.
- **Evidence:** the settled legacy ledger is USD 6.767912 across 108 rows and
  43 distinct identities, with a pending-owner count of exactly zero; the
  dry-run gate proves the argv surface at USD 0.00 over 142 recorded argv with
  0 refused; salvage, exact teardown, and exact-instance deletion verification
  are source-present and merged.

### Option C — Standing authorization with no per-run cap

- **Description:** a program cap only.
- **Benefits:** least bookkeeping.
- **Costs/Risks:** rejected. A single misconfigured run could consume the whole
  program cap, and there would be no recorded per-run bound to audit a run
  against afterwards.

## Decision

**Option B is accepted.**

1. **Standing authorization.** On 2026-09-11 the user granted standing
   authorization to launch provider runs for this program. Individual runs no
   longer require a fresh user approval by name.
2. **Program hard cap: USD 50.** This is the total the program may spend under
   this authorization. It is consistent with the whole-project budget control
   the lifecycle already enforces, and with the USD 43.232088 that remains once
   the settled legacy spend of USD 6.767912 is subtracted from it. Note that
   USD 43.232088 is a *derived* figure (50.00 − 6.767912), not a field stored
   in the ledger; the ledger's own settled total is USD 6.767912 with zero
   pending reservations.
3. **Per-run caps are recorded per run.** Each run carries its own explicitly
   recorded cost and wall-clock cap. For the next run, `remote-eval-20260911-b`,
   Sol set **USD 10.00 and 4 hours**.
4. **Lifecycle rules are unchanged.** The required sequence remains, in order:
   read-only catalogue → cost preflight → dry-run gate → watchdog/backstop →
   salvage (which runs before teardown on every exit path, including failure) →
   exact teardown → post-run read-only verification that the run left nothing
   running. None of these steps is waived, shortened, or made advisory by this
   authorization, and a refusal at any one of them is a refusal of the run.
   **Scope of the last step, stated exactly:** the verification is
   *exact-instance*, not account-wide. By deliberate design
   (`scripts/shadeform_lifecycle.py:6-9`) "no account-wide instance-list
   operation exists in this module. A run may inspect, update, or delete only
   the exact resource ID recorded in its phase ledger. There is no code path
   that can enumerate the account and act on whatever it finds." Post-run
   verification is therefore satisfied by the deletion receipt for the exact
   recorded instance ID (`deletion.success = true`), a settled cost event, and
   a terminal status in the ledger — never by an account-wide sweep, which the
   ownership model intentionally makes impossible. Any statement elsewhere that
   this program performs an account-wide zero-instance enumeration is wrong and
   should be read as this exact-instance confirmation instead.
5. **Scope limit.** This authorizes bounded provider runs for this program and
   nothing else. It is not approval of any phase gate, release gate, capability,
   provider, credential, or artifact.

## Rationale

The binding constraint was never the money. The 2026-09-11 attempt stopped at
USD 0.00 on a correctness property — the run could not have returned its
receipts — and the historical per-run actuals for the four complete evaluation
runs are USD 0.499694, 0.526403, 0.515176 and 0.522114, all under USD 0.55. A
USD 50 program cap is therefore deliberately conservative rather than
expansive: it records the boundary that was already implicit instead of raising
it.

The cap is not only bookkeeping. The lifecycle already treats the whole-project
cost control as a launch precondition: candidate selection "subtracts what the
ledger already records and refuses to launch while any prior row's cost is
unaccounted" (`scripts/shadeform_lifecycle.py:18-21`). Recording USD 50 as the
program cap therefore lands on a control that fails closed on its own, which is
what criterion 1 requires.

Per-run caps are kept separate from the program cap because they answer a
different question. The program cap bounds cumulative exposure; the per-run cap
bounds the damage a single misconfigured run can do, and — because it is written
down before the run — gives the post-run reconciliation something exact to check
against. Criterion 3 is satisfied structurally: at USD 10, no single authorized
run can consume more than a fifth of the program cap.

The lifecycle sequence is left untouched on purpose. It is the actual
enforcement mechanism, and it fails closed at every step. Authorization removes
a human round trip; it does not remove a refusal. In particular the dry-run gate
stays a precondition, because it is what caught two of the three latent refusals
that would otherwise have been discovered by paying for them.

## Consequences

### Positive

- A bounded evaluation run becomes launchable, so the model's score against the
  33-tool/37-case shipping profile can stop being unknown.
- Cost exposure is bounded at two independent levels and is auditable per run.
- The ≥95% quality-retention criterion gains a path to a reachable reference,
  because the comparator arms are now buildable within a budgeted run.

### Negative / accepted trade-offs

- Standing authorization removes the per-run human checkpoint. The accepted
  mitigation is that every lifecycle refusal remains in force and that each
  run's cap must be recorded before launch.
- The program cap is a cap, not a forecast. Recording USD 50 does not
  authorize spending USD 50, and an unspent cap confers nothing.
- This ADR does not resolve the cost-ledger genesis question. The migration
  preflight still reports `"safe_to_migrate_now": False` as a hardcoded literal
  (`scripts/shadeform_ledger_migration_preflight.py:1110`), and it still reports
  `adjudication_required: True` with `evidence_complete` false. Revisiting that
  literal requires its own ADR and is tracked as `LEDGER-GENESIS-001`.

### Required implementation changes

- **Owners/tasks:** none in source. This decision is recorded, not implemented;
  the lifecycle code it relies on is already merged at
  `f89c1684ea051e1c9c92f944cb080d424a086f55`. `LEDGER-GENESIS-001` (S4) carries
  the reviewed-genesis follow-up.
- **Contracts:** none. No contract, schema, or version changes.
- **Tests:** none added by this ADR. The existing offline dry-run gate and the
  lifecycle suites remain the enforcement evidence.
- **Migration/rollback:** rollback is withdrawal of the standing authorization
  by the user, or a Sol decision lowering or zeroing the program cap. Neither
  requires a source change, because no source path reads this ADR.

## Validation

- **Experiment/test:** `python3 scripts/j1m_dry_run.py` must pass before any
  run, offline, at USD 0.00. Post-run, the exact-instance deletion receipt and
  the ledger reconciliation must both be clean. The gate carries an operator
  precondition that git cannot express: the artifact destination and its
  ancestors must be owner-private, so a fresh checkout needs
  `chmod 700 artifacts artifacts/qwen35-9b` before the gate will pass.
- **Acceptance threshold:** cumulative recorded spend stays at or below
  USD 50; each run's recorded actual stays at or below that run's recorded cap;
  pending reservations return to zero after every run.
- **Artifact:** the per-run receipts salvaged into
  `artifacts/qwen35-9b/<run-id>/`, the lifecycle receipt, and the cost-ledger
  rows for the run.
- **Revisit trigger:** any of — cumulative spend reaching USD 40; a run
  exceeding its recorded per-run cap; a run ending with a non-zero pending
  reservation or an unverified instance; a change to the lifecycle sequence in
  §Decision item 4; or the ledger-genesis ADR that `LEDGER-GENESIS-001`
  requires.

## Approval

- **Sol:** accepted 2026-09-11. Decision made by S0 Sol on the user's standing
  authorization and relayed for recording; Sol's merge of this refresh to `main`
  is the ratifying act.
- **Affected Luna acknowledgements:** S2 and S4 pending. Recording this ADR
  confers no capability and advances no gate; release/full access remains
  `BLOCKED` / `NOT_READY`.
