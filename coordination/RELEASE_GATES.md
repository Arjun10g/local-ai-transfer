# Release Gates

Overall release/full-access state: **BLOCKED / NOT_READY**. The states below
describe formal evidence/approval gates, not whether source work exists.

Current source baseline is exact
`main@6e0d12c0023068456b97fc9c857a3538ca612421`. Current focused evidence is
journal/Graph Node 210 discovered/209 pass/1 existing TODO, handoff/release
63/63, env 26/26, QA-runner 27/27, conformance 11/11, 152 tracked JSON files
(151 strict-valid plus one intentional duplicate-key hostile fixture), and 29
modules/71 relative imports/0 cycles. QA remains `BLOCKED` at 59 discovered,
0 missing/unknown, and 65 records (1 PASS/64 expected SKIP). Historical broad
Node 350/349 and Python 663/663 evidence belongs only to `d195235`.

| Gate | State | Required evidence / remaining work |
|---|---|---|
| Phase 0 | IN_PROGRESS / UNAPPROVED | Contracts, policy matrix, source pin procedure, Shadeform profile/cost preflight |
| Phase 1 | EVIDENCE_INCOMPLETE / UNAPPROVED | Fixture/source work and an activation-refused metadata handoff exist; real signed artifacts, approved trust anchor, controlled artifact acceptance/custody, and oracle evidence are missing |
| Phase 2 | EVIDENCE_INCOMPLETE / UNAPPROVED | Real accepted-artifact CPU inference, oracle parity, and quality gate |
| Phase 3 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source sessions/local tools and descriptor WAL exist; native secure FD ownership/publication, single-writer/anti-rollback/compaction, production bridge, and Windows capability evidence are missing/refused |
| Phase 4 | EVIDENCE_INCOMPLETE / UNAPPROVED | Graph reconciliation/WAL source exists; provider reconciliation, live Graph accounts/consent, browser/Copilot evidence, and model quality are missing |
| Phase 5 | EVIDENCE_INCOMPLETE / UNAPPROVED | Performance, caches, and exact-device acceleration evidence |
| Phase 6 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source hardening is active; security, offline proof, fuzz/soak, and target evidence are incomplete |
| Phase 7 | EVIDENCE_INCOMPLETE / UNAPPROVED | Metadata handoff validation exists but remains activation-refused; real signed artifacts/trust anchor, reproducible executable Windows release, and approved Shadeform evidence are absent |
| Phase 8 | BLOCKED | Exact Dell hardware/driver receipt and bounded target execution required |

## Governance interpretation

Source implementation, evidence, and gate approval are separate records. A
source commit or merge can establish only what is present in the source tree;
it does not establish runtime/target evidence, independent review, or release
approval. `READY_FOR_REVIEW` means review is pending, not that the product is
ready. The working Phase 6 hardening stream does not advance the formal Phase 0
gate or make earlier/later gates complete. No gate state above is advanced by a
source-only merge or status update.
