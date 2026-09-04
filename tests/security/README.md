# Adversarial tool-calling QA

`tool-calling-adversarial.test.mjs` is a release-facing, provider-free security
suite. It exercises parser limits, tool argument boundaries, controller loops and
confirmation correlation, host authentication/origin/body gates, browser egress
disclosure and provider disablement, shell/argv boundaries, and filesystem path
and platform safety.

Tests marked `KNOWN GAP` are intentionally Node `TODO` tests: they are runnable
regressions for a discovered defect or missing hardening, but do not pretend that
the current implementation passes that invariant. A release review must resolve
or explicitly accept each TODO before treating this suite as a clean security
gate. Current TODO probes cover bracketed private IPv6 browser URLs, full
tool-result validation in the controller, parser prototype preservation, and
strict confirmation-body fields. `permission-mode-adversarial.test.mjs` also
records the future explicit-operator-grant contract. The MVP currently has no
full-access mode; its passing tests prove unknown grant configuration and
model-created grant calls fail closed, while the named TODO matrix prevents a
future mode from shipping without scope, T4, credential, replay, audit,
emergency-stop, downgrade, and mid-turn revocation coverage.

The suite never contacts a provider, opens a real browser, or uses credentials.
