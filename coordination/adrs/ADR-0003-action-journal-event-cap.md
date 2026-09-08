# ADR-0003 — ActionJournal event-cap alignment

- **Status:** Accepted for source correction; independent review required
- **Date:** 2026-09-05
- **Decision owner:** S0 Sol
- **Implementation owner:** S1 Runtime
- **Related task/request:** TOOL-042; ICR-TOOL-042

## Context

The pathname ActionJournal and the fixed native container each enforce at most
16 events per operation. The container's versioned on-disk layout has exactly
16 event cells in each bank. The source-only wire contract, its Node reference
codec, the inert native protocol codec, and the test-injected client contract
nevertheless advertised or accepted 32 events. A 17th event could not be
represented by the authoritative store.

## Decision

Retain `lae.action-journal.v0.1.0` and correct every event-count declaration and
validator to exactly 16 events, sequence values zero through 15, and a maximum
detail page of 16 events.

This is a coordinated pre-activation erratum, not a new wire or storage format.
No production transport exists, every availability gate remains false, and the
corrected range was already impossible to persist. The protocol identity,
envelope version, HMAC domain, deterministic frame bytes, fixed-container ABI,
event encoding, and transition graph remain unchanged.

## Consequences

- A detail limit of 17 or sequence 16 now fails consistently before store use.
- The 16th event remains valid in both the pathname journal and protocol schema.
- Synthetic fixture metadata declares the cap without modifying any existing
  authenticated vector or canonical frame.
- Protocol, client, helper, pathname, container, and reference-model tests scan
  for future 32/16 drift.
- Production, compile, packaging, Windows-target, and release readiness do not
  advance.

## Validation

- Boundary tests accept the 16th event/detail item and refuse the 17th.
- Machine-contract tests compare the protocol, client, vector, pathname, and
  fixed-container declarations.
- Native static checks bind the helper protocol constants to the 16-cell store
  ABI and reject stale 32-event validator patterns.

## Approval

- Sol: accepted through the explicit TOOL-042 implementation assignment.
- Independent source audit: required before merge.
