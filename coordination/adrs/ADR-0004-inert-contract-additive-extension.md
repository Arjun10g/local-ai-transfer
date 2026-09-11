# ADR-0004 — Additive status-code extension of an inert, activation-refused contract

- **Status:** Accepted for source correction; merged at `4de01f7`; independent
  source review is complete and formal gate approval remains required
- **Date:** 2026-09-11
- **Decision owner:** S0 Sol
- **Implementation owner:** S1 Runtime
- **Related task/request:** RUN-WINDOWS-DESCRIPTOR-JOURNAL-BOOTSTRAP;
  ICR-RUN-WDJB-001; generalizes ADR-0003
- **Provenance:** decision made by S0 Sol and relayed for recording; Sol's merge
  of this refresh to `main` is the ratifying act. Formal gate approval remains
  separate.
- **Form:** this ADR deliberately follows ADR-0003's reduced erratum form
  (Context / Decision / Consequences / Validation / Approval) rather than the
  full `templates/ADR.md`, matching the closest precedent for a pre-activation
  source correction.

## Context

`contracts/action-journal-storage/v0.1.0.json` declares the exact set of status
strings the Win32 journal-storage boundary may emit, and the merged suite
`tests/native/test_windows_action_journal_storage_static.py` requires the
contract set and the emitted set to be equal. That translation unit now carries
two boundaries: the v1 container lease and the dormant v2
`DescriptorActionJournal` WAL lease.

The independent review of the descriptor-WAL bootstrap required distinct typed
refusals where a DACL regression, a size change, a torn prefix, a path swap, an
ancestor swap, a handle-inheritance anomaly, and a repeated handoff had been
indistinguishable, and required a specific "already transferred" status for the
one-shot handoff guard. Reusing existing codes would have re-created exactly the
conflation the review asked to remove. The repair therefore added
`handoff_already_transferred`, `source_handle_inheritable`,
`inheritance_control_failed`, and `final_path_mismatch`, and raised the question
of whether that requires a contract version bump.

ADR-0003 already settled the narrower case: a pre-activation erratum that made
every validator agree with an already-authoritative storage limit retained
`lae.action-journal.v0.1.0`. This decision generalizes that precedent rather
than re-deciding it slice by slice.

## Decision

Additive status-code and predicate extensions to inert, activation-refused,
transport-less contracts MAY retain their version when no existing value,
identifier, envelope, on-disk byte, limit, or gate changes.

Both halves are binding. "Additive" means strictly additive: no existing status
string, identifier, receipt field, header constant, or limit may be changed,
narrowed, or removed. "Inert, activation-refused, transport-less" means the
contract has no production transport, host import, package entry, or activation
path, and every availability gate — including `production_available`,
`native_target_registered`, `helper_added`, `transport_added`,
`node_integration_added`, `package_added`, and `activation_permitted` — remains
`false`. If any of those conditions fails, this ADR does not apply and a
version bump or a new contract is required.

Applied here: the four codes stay in
`contracts/action-journal-storage/v0.1.0.json` and version `0.1.0` is retained,
merged at `4de01f7`.

A separate frozen contract — for example
`contracts/action-journal-descriptor-wal/` — is REQUIRED for the v2 WAL
boundary before any transport, import, package, or activation path exists. The
two boundaries share one translation unit today only because neither can be
reached; that is a source-organization fact, not a licence to keep one
interface artifact describing two boundaries once either becomes reachable.

## Consequences

- Code and contract continue to agree exactly, so the merged status-set
  equality assertion keeps its force instead of being relaxed.
- Distinct refusals stay distinguishable, which was the security property the
  review required.
- No consumer can be broken by the addition, because no consumer exists: there
  is no production transport, import, package entry, or activation path for
  either boundary.
- The v2 WAL boundary carries a standing obligation to be split into its own
  frozen contract before it becomes reachable. Deferring that split past the
  first transport, import, package, or activation path is a violation of this
  ADR, not a judgement call.
- Production, compile, packaging, Windows-target, and release readiness do not
  advance. This ADR records a source-correction rule only.

## Validation

- The merged status-set assertion
  `test_machine_status_contract_exactly_matches_header_mapping`
  (`tests/native/test_windows_action_journal_storage_static.py:151-152`)
  compares the contract's declared status strings against the exact set emitted
  by `native/action_journal_storage/windows_storage.cpp`.
- The descriptor bootstrap suite pins each new status to its own refusal
  condition, so no two conditions can collapse back onto one code.
- The contract's availability gates are asserted `false`, which is the
  precondition this ADR depends on.

## Approval

- Sol: approved 2026-09-11 through the ICR-RUN-WDJB-001 decision.
- Source merge: `4de01f7` on `main`, under integrated baseline `7239b7e`.
- Independent source review: complete, with a re-review returning
  `ACCEPT_FOR_MERGE`.
- Formal gate decision: still required before any production or target use.
