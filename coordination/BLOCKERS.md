# Blockers

## B-001 — Exact target receipt incomplete

- Fact: operator-supplied partial facts identify a Dell Intel Core Ultra 7 vPro Enterprise-class platform, one integrated `Intel Graphics` adapter with no discrete GPU, display driver `32.0.101.8247`, 32 GB DDR5-class memory reported at 5600 MT/s, and motherboard `039NNG` revision `A00`. No redacted `hardware-receipt.json`, exact CPU SKU, GPU PNP/device ID, usable/shared GPU memory, Windows build, Vulkan capability, or measured available-memory topology is present.
- Impact: Intel backend promotion and Phase 8 target acceptance cannot complete.
- Workaround: run the implemented read-only probe, use Core Ultra 7 as the CPU-family matching input, keep CPU mandatory, and label all GPU Shadeform machines as directional analogs until the remaining adapter/runtime fields are captured.
- Needed from: target operator, after S2/S4 approve the probe.
- State: OPEN; does not block fixture, model-build, CPU, host, tool, or packaging work.

## B-002 — Corporate approval references unavailable

- Fact: repository, model-transfer, signing, scanning, and live-provider approval identifiers have not been supplied.
- Impact: formal enterprise release sign-off and live provider enablement cannot complete.
- Workaround: use explicit `UNASSESSED` states, synthetic data, HF source only on Shadeform, and keep live web/provider behavior disabled by default.
- Needed from: user/organization before release sign-off.
- State: OPEN; does not block implementation or non-sensitive evidence.

## B-003 — Production external-tool chain is not yet restart-safe or identity-bound

- Fact: production model evaluation proves only tool proposal/arguments. The
  hostile external-tools harness uses injected Graph/CDP/Copilot fakes and does
  not execute a real account, browser, Copilot service, or Windows process.
  Disabled/unconfigured provider definitions are still advertised to the model;
  Graph/browser/Copilot proposal and replay state is memory-only; no durable
  secret-free user-action receipt exists; and browser/Copilot executable paths
  are not bound to immutable file identity across preview and spawn.
- Impact: a passing model score or mocked provider run cannot establish safe
  end-to-end mail, Teams, browser-action, or Copilot readiness. A host restart
  after an ambiguous provider write can permit duplicate work, and a mutable
  executable can change after preview/version inspection.
- Workaround: keep all external providers disabled by default and all writes
  confirmation-bound. Add deterministic HostServer/controller vertical tests,
  active-tool filtering, durable ambiguous-write/idempotency state and action
  receipts, and executable identity pinning before any write-capable live test.
- Needed from: S3 implementation, S4 independent review, and S0 gate decision.
- State: OPEN; blocks Phase 4/6 readiness and all full-access claims.

## B-004 — Current A100 provider profile is activation-unreliable

- Fact: two consecutive bounded corrected-evaluator attempts (`remote-l` and
  `remote-m`) timed out before the instance became active. Neither reached HF
  acquisition, conversion, or evaluation. Exact cleanup succeeded, the cost
  ledger is settled with zero pending reservations, and cumulative remote spend
  is `$6.767912`.
- Impact: the corrected production tool-quality result is unavailable. Further
  blind retries would spend budget without testing the model or product.
- Workaround: stop retrying this profile. Use a fresh read-only catalogue, then
  a cheaper non-A100 activation/SSH/CUDA canary with no model download. Only a
  candidate that passes that canary is eligible for the HF-backed evaluator.
- Needed from: S2 candidate plan, S4 lifecycle review, and S0 authorization.
- State: OPEN; does not block local mocked/source hardening.
