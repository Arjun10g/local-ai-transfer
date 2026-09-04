# Contract conformance skeleton

`python -m qa.conformance.runner` runs ten independent positive/negative
fixtures covering the Phase 0 API/event/tool/model/config/metrics/error,
cancellation, session, and release contracts. These are fixture checks, not
real-engine or real-model evidence. Once producer contracts land, their
versioned examples can replace the fixture payloads without weakening the
negative checks.
