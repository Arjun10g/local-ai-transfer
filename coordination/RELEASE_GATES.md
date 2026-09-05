# Release Gates

| Gate | State | Required evidence / remaining work |
|---|---|---|
| Phase 0 | IN_PROGRESS | Contracts, policy matrix, source pin procedure, Shadeform profile/cost preflight |
| Phase 1 | NOT_STARTED | Fixture vertical slice, pinned source/backend, controlled model artifact |
| Phase 2 | NOT_STARTED | Real CPU inference and oracle parity |
| Phase 3 | NOT_STARTED | Sessions, safe local tools, offline loop |
| Phase 4 | NOT_STARTED | Autonomous tool path and optional provider path |
| Phase 5 | NOT_STARTED | Performance, caches, exact-device acceleration evidence |
| Phase 6 | NOT_STARTED | Security, offline proof, fuzz/soak |
| Phase 7 | NOT_STARTED | Reproducible Windows release and Shadeform demo |
| Phase 8 | BLOCKED | Exact Dell hardware/driver receipt and bounded target execution required |

## Governance interpretation

Source implementation, evidence, and gate approval are separate records. A
source commit or merge can establish only what is present in the source tree;
it does not establish runtime/target evidence, independent review, or release
approval. `READY_FOR_REVIEW` means review is pending, not that the product is
ready. No gate state above is advanced by a source-only merge or status update.
