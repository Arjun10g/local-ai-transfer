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
