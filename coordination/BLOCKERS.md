# Blockers

## B-001 — Exact target receipt incomplete

- Fact: the user confirmed a Dell Intel Core Ultra 7 vPro Enterprise-class platform, but no redacted `hardware-receipt.json`, exact CPU SKU, GPU device ID, driver, or memory topology is present.
- Impact: Intel backend promotion and Phase 8 target acceptance cannot complete.
- Workaround: implement the read-only probe, use Core Ultra 7 as the CPU-family matching input, keep CPU mandatory, and label all GPU Shadeform machines as directional analogs until the adapter/driver fields are captured.
- Needed from: target operator, after S2/S4 approve the probe.
- State: OPEN; does not block fixture, model-build, CPU, host, tool, or packaging work.

## B-002 — Corporate approval references unavailable

- Fact: repository, model-transfer, signing, scanning, and live-provider approval identifiers have not been supplied.
- Impact: formal enterprise release sign-off and live provider enablement cannot complete.
- Workaround: use explicit `UNASSESSED` states, synthetic data, HF source only on Shadeform, and keep live web/provider behavior disabled by default.
- Needed from: user/organization before release sign-off.
- State: OPEN; does not block implementation or non-sensitive evidence.
