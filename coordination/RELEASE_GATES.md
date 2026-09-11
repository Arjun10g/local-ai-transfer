# Release Gates

Overall release/full-access state: **BLOCKED / NOT_READY**. The states below
describe formal evidence/approval gates, not whether source work exists.

Current audited integrated source baseline is exact
`main@7239b7e1a6a512cabf9e5ab18ba463a7fac351fc`, the last source merge; the
docs descendant `6c3a1257` and this refresh are documentation descendants, not
self-referential source hashes. Current reproduced evidence is `npm test` 426
tests/424 pass/0 fail/0 cancelled/1 skipped/1 todo, Windows native static
`Ran 289 tests` OK, QA safe-runner `Ran 20 tests` OK, 153 tracked JSON files
(152 strict-valid under a duplicate-key-rejecting parser plus one intentional
hostile fixture), and a host import graph of 29 modules/67 unique relative
import edges/0 unresolved/0 cycles. QA remains `BLOCKED` at 62 discovered,
0 missing/unknown, and 68 records (1 PASS/67 expected SKIP). The prior
`6e0d12c` focused numbers (journal/Graph Node 210/209, handoff/release 63/63,
env 26/26, QA-runner 27/27, conformance 11/11, JSON 152/151, imports 29/71,
QA 59 discovered/65 records) are historical. Historical broad Node 350/349 and
Python 663/663 evidence belongs only to `d195235`.

| Gate | State | Required evidence / remaining work |
|---|---|---|
| Phase 0 | IN_PROGRESS / UNAPPROVED | Contracts, policy matrix, source pin procedure, Shadeform profile/cost preflight |
| Phase 1 | EVIDENCE_INCOMPLETE / UNAPPROVED | Fixture/source work and an activation-refused metadata handoff exist; real signed artifacts, approved trust anchor, controlled artifact acceptance/custody, and oracle evidence are missing |
| Phase 2 | EVIDENCE_INCOMPLETE / UNAPPROVED | Real accepted-artifact CPU inference, oracle parity, and quality gate |
| Phase 3 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source sessions/local tools, the descriptor WAL, and a dormant uncompiled Win32 descriptor-WAL bootstrap (secure create/trusted reopen, single-writer lease, one-shot non-inheritable handoff) exist; native secure owner publication, an authenticity/anti-rollback anchor, compaction, a reviewed launcher and HANDLE-to-CRT child bridge, the production bridge, MSVC compile, and Windows capability evidence are missing/refused |
| Phase 4 | EVIDENCE_INCOMPLETE / UNAPPROVED | Graph reconciliation/WAL source exists, now including bounded restart completion for durably acknowledged, newly account-bound `mail.create_draft` records proved by a fresh unique exact provider GET; its startup trigger is operationally inert on memory-only tokens, manual `POST .../reconcile` is still HTTP 501, `reconciling` records have no automatic resolution path, `listSentForDigest` projection is an open follow-up, and live Graph accounts/consent, browser/Copilot evidence, and model quality are missing |
| Phase 5 | EVIDENCE_INCOMPLETE / UNAPPROVED | Performance, caches, and exact-device acceleration evidence |
| Phase 6 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source hardening is active and now includes a dormant `windows-process-authority` v1.0.0 contract with a private move-only `LaunchAuthority`; that authority supplies no launch mechanism, no consume-time identity re-derivation, no command-line builder, no proof issuer, and no CMake/package/host path, and security, offline proof, fuzz/soak, compile, and target evidence remain incomplete |
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
