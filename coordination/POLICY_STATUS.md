# Policy and Approval Status

| Feature/gate | Status | Owner | Evidence needed |
|---|---|---|---|
| Local fixture development | APPROVED | S0 | User requested end-to-end execution |
| Shadeform use within `.env` caps | APPROVED_WITH_CONDITIONS | S0/S4 | Read-only pricing first; ownership ledger; teardown proof; cost receipt |
| Official Hugging Face model source on Shadeform | APPROVED_WITH_CONDITIONS | S2 | Immutable source revision, license/model-card hashes, no target download |
| Q4_K_M artifact transfer | UNASSESSED | S0/S2 | Approved artifact path and transfer receipt |
| Third-party llama.cpp source | UNASSESSED | S0/S4 | Pin, license, SBOM, approval reference |
| CPU backend | APPROVED | S0 | Correctness and package gates |
| Intel Vulkan backend | UNASSESSED | S2 | Exact target receipt, parity, stability, actual placement |
| Intel SYCL backend | UNASSESSED | S2 | Separate experimental evidence and redistribution approval |
| Local loopback serving | APPROVED_WITH_CONDITIONS | S1/S3/S4 | Auth, origin, limits, security tests |
| Local filesystem/process tools | APPROVED_WITH_CONDITIONS | S3/S4 | Workspace bounds, confirmation, no shell, adversarial tests |
| Live web/search provider | UNASSESSED | S0/S3 | Provider approval and synthetic live-run evidence |
| Release signing/attestation | UNASSESSED | S0/S4 | Corporate mechanism and credential handling |
| Target transfer and acceptance | UNASSESSED | S0 | Operator approval, target receipt, approved transfer route |

