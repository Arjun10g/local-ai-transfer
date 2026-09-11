# Interface Change Requests

## ICR-TOOL-042 — ActionJournal event-cap alignment

- **Status:** source-corrected and merged at `12e722a`; independent review and
  formal gate approval remain required for promotion.
- **Affected interface:** source-only `lae.action-journal.v0.1.0` protocol limit
  declarations and validators, its synthetic fixture metadata, the inert client
  contract, and the inert native helper codec.
- **Decision:** retain version `0.1.0` and correct the stale advertised 32-event
  range to exactly 16 events (sequences 0–15 and detail page limit 16), recorded by
  [ADR-0003](adrs/ADR-0003-action-journal-event-cap.md).
- **Compatibility:** the fixed-container ABI has always contained exactly 16
  event cells and both authoritative stores already refuse a 17th event. No
  production transport exists and availability remains false. The correction
  changes no protocol identifier, envelope, HMAC domain, deterministic frame,
  on-disk byte, transition, production gate, or target gate; it only makes all
  pre-activation validators reject the storage-impossible range consistently.
- **Evidence required:** explicit 16th-accepted/17th-refused boundaries and a
  cross-layer scan tying protocol, client, helper, pathname journal, container
  contract, reference model, and native constants to 16.

## ICR-RUN-WDJB-001 — descriptor-WAL refusal statuses in the storage boundary contract

- **Status:** Sol-approved additive source-only extension, merged `4de01f7`;
  independent source review is complete; formal gate approval is still
  required. Sol's decision retains contract version `0.1.0` and keeps the four
  codes in `contracts/action-journal-storage/v0.1.0.json`; the generalizing
  rule is recorded as
  [ADR-0004](adrs/ADR-0004-inert-contract-additive-extension.md). Approval of
  this additive source correction is not activation, production availability,
  compile, Windows, or target approval, and advances no phase or release gate.
- **Affected interface:** `contracts/action-journal-storage/v0.1.0.json`
  `status_codes`, which the merged suite
  `tests/native/test_windows_action_journal_storage_static.py:126` requires to
  equal the exact set of status strings emitted by
  `native/action_journal_storage/windows_storage.cpp`. That translation unit now
  holds two boundaries: the v1 container lease and the dormant v2
  `DescriptorActionJournal` WAL lease.
- **Change:** add `handoff_already_transferred`, `source_handle_inheritable`,
  `inheritance_control_failed`, and `final_path_mismatch`. Version `0.1.0` is
  retained, matching the ICR-TOOL-042 precedent for a source-only correction.
- **Why:** the independent review required distinct typed refusals where a DACL
  regression, a size change, a torn prefix, a path swap, an ancestor swap, a
  handle-inheritance anomaly, and a repeated handoff were previously
  indistinguishable, and required a specific "already transferred" status for
  the one-shot handoff guard. Reusing existing codes would have re-created the
  conflation the review asked to remove.
- **Compatibility:** no production transport, host import, package entry, or
  activation exists for either boundary; `production_available`,
  `native_target_registered`, `helper_added`, `transport_added`,
  `node_integration_added`, `package_added`, and `activation_permitted` all
  remain `false`. No existing status string, identifier, receipt field, on-disk
  byte, header constant, limit, or gate is changed or removed. The additions are
  reachable only from the dormant v2 WAL entry points, which have no callers.
- **Alternative considered:** a separate
  `contracts/action-journal-descriptor-wal/` contract for the v2 boundary. That
  is the cleaner long-term shape, but creating a new frozen interface artifact
  is a larger decision than this repair slice should take unilaterally. Sol may
  redirect these four codes into such a contract; the source and tests would
  follow with no behaviour change.
- **Sol decision (2026-09-11):** approved as written — the four additive status
  codes stay in `contracts/action-journal-storage/v0.1.0.json` and version
  `0.1.0` is retained, because no existing value, identifier, envelope,
  on-disk byte, limit, or gate changes. Per ADR-0004, a separate frozen
  contract (for example `contracts/action-journal-descriptor-wal/`) is
  REQUIRED for the v2 WAL boundary before any transport, import, package, or
  activation path exists.
- **Evidence required:** exact code/contract agreement
  (`tests/native/test_windows_action_journal_storage_static.py` status-set
  assertion) plus the descriptor bootstrap suite's per-condition status
  assertions.
