# Policy and Approval Status

| Feature/gate | Status | Owner | Evidence needed |
|---|---|---|---|
| Local fixture development | APPROVED | S0 | User requested end-to-end execution |
| Shadeform use within `.env` caps | APPROVED_WITH_CONDITIONS | S0/S4 | Read-only pricing first; ownership ledger; teardown proof; cost receipt |
| Official Hugging Face model source on Shadeform | APPROVED_WITH_CONDITIONS | S2 | Immutable source revision, license/model-card hashes, no target download |
| Q4_K_M artifact technical identity | PINNED_NOT_ACCEPTED | S0/S2 | Expected `Qwen3.5-9B-Q4_K_M.gguf`, 5,629,109,088 bytes, SHA-256 `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`; ignored local bytes not revalidated/approved |
| Q4_K_M artifact transfer/custody | UNASSESSED | S0/S2 | Approved artifact path, independent local verification, custody chain, and corporate transfer receipt |
| Third-party llama.cpp source | UNASSESSED | S0/S4 | Pin, license, SBOM, approval reference |
| CPU backend | APPROVED | S0 | Correctness and package gates |
| Intel Vulkan backend | UNASSESSED | S2 | Exact target receipt, parity, stability, actual placement |
| Intel SYCL backend | UNASSESSED | S2 | Separate experimental evidence and redistribution approval |
| Local loopback serving | APPROVED_WITH_CONDITIONS | S1/S3/S4 | Auth, origin, limits, security tests |
| Local filesystem/process tools (non-Windows source/fixture scope) | APPROVED_WITH_CONDITIONS | S3/S4 | Workspace bounds, confirmation, no shell, adversarial tests; not target approval |
| Windows filesystem tools | NOT_READY_REFUSED | S1/S3/S4 | Source-merged refusal boundary; identity-pinned native helper and real target evidence required before reads/writes can be exposed |
| Windows process/app/browser/clipboard execution | NOT_READY_REFUSED | S1/S3/S4 | Quarantined broker prerequisites, exact executable identity, containment, environment, and target acceptance |
| Production durable ActionJournal | UNAVAILABLE | S3/S4 | Native handle-relative protected store, authenticated transport, packaging, recovery, and target evidence |
| Remote external-tools lifecycle | DISABLED | S2/S3/S4 | Source hardening merged; `REMOTE_EXECUTION_ENABLED=False`; explicit approved cost-ledger genesis and separate live authorization required |
| Live web/search provider | UNASSESSED | S0/S3 | Provider approval and synthetic live-run evidence |
| Microsoft Graph mail/Teams provider | REQUESTED_LIVE_UNASSESSED | S0/S3/S4 | Reconciliation source is merged, but production journal, delegated scope/admin consent, synthetic account, redaction, and live package evidence remain absent |
| Isolated browser action provider | REQUESTED | S0/S3/S4 | Repaired reconciliation source `fc317fce023f9364e7f19b69a700124d1936f8ca` is merged by `976aeff` after independent source/mock review; rejected predecessor `5dc2ad2` remains historical; live/browser/Windows evidence is absent, and an approved browser, production journal, and synthetic Windows action evidence remain required |
| GitHub Copilot CLI bridge | REQUESTED | S0/S3/S4 | Existing approved CLI/license, explicit cloud-egress approval, synthetic prompt-only evidence |
| Operator `full_access` capability grants | REQUESTED | S0/S3/S4 | Local grant/revoke UX, immutable safety floor, adversarial tests, corporate approval |
| Release signing/attestation | UNASSESSED | S0/S4 | Corporate mechanism and credential handling |
| Target transfer and acceptance | UNASSESSED | S0 | Operator approval, target receipt, approved transfer route |
