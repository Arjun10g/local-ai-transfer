# Release Gates

Overall release/full-access state: **BLOCKED / NOT_READY**. The states below
describe formal evidence/approval gates, not whether source work exists.

Current audited integrated source baseline is exact
`main@263f11413d1746044a6cc13062ad2b1f821c4d11`, the last source merge; the
docs descendants `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f` and
`1b22a40e21dce0ebc903bc3c2024ab162adc2d56` and this refresh are documentation
descendants, not self-referential source hashes. The previous baseline
`7239b7e` is historical and superseded. Current reproduced evidence is
`npm test` 432 tests/430 pass/0 fail/0 cancelled/1 skipped/1 todo, native
static `Ran 309 tests` OK (the narrower Windows-only pattern is `Ran 289
tests` OK; 309 is 289 plus the 20 tests of the new descriptor-WAL contract
suite), QA safe-runner `Ran 20 tests` OK, 154 tracked JSON files (153
strict-valid under a duplicate-key-rejecting parser plus one intentional
hostile fixture), and a host import graph of 29 modules/67 unique relative
import edges/0 unresolved/0 cycles. QA remains `BLOCKED` at 64 discovered,
0 missing/unknown, and 70 records (1 PASS/69 expected SKIP). The prior
`7239b7e` numbers (Node 426/424, native static 289, JSON 153/152, QA 62
discovered/68 records) and the older `6e0d12c` focused numbers (journal/Graph
Node 210/209, handoff/release 63/63, env 26/26, QA-runner 27/27, conformance
11/11, JSON 152/151, imports 29/71, QA 59 discovered/65 records) are
historical. Historical broad Node 350/349 and Python 663/663 evidence belongs
only to `d195235`.

No model evidence is recorded against the current shipping profile. The
production evaluation fixture is 33 tools and 37 cases
(`tests/model/production_tool_call_eval.json`, SHA-256
`c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`) and has no
score. Every recorded score — 28/34 on the retired 11-tool/34-case canary and
13/32 on the retired 28-tool/32-case profile — was measured on 2026-09-04 on
CUDA/A100, never on CPU or Intel Vulkan, and neither fixture is the shipping
profile.

| Gate | State | Required evidence / remaining work |
|---|---|---|
| Phase 0 | IN_PROGRESS / UNAPPROVED | Contracts, policy matrix, source pin procedure, Shadeform profile/cost preflight |
| Phase 1 | EVIDENCE_INCOMPLETE / UNAPPROVED | Fixture/source work and an activation-refused metadata handoff exist; real signed artifacts, approved trust anchor, controlled artifact acceptance/custody, and oracle evidence are missing |
| Phase 2 | EVIDENCE_INCOMPLETE / UNAPPROVED | Real accepted-artifact CPU inference, oracle parity, and quality gate. Fixture-identity gap: every recorded score was measured on 2026-09-04 against retired fixtures (11-tool/34-case canary 28/34; 28-tool/32-case profile 13/32) on CUDA/A100, never CPU or Intel Vulkan; the current 33-tool/37-case shipping profile (`tests/model/production_tool_call_eval.json`, sha `c75af520…c8ac6c`) has no recorded score, so the model's score on the profile it must ship against is unknown rather than merely below gate. The ≥95% retention comparator is also unreachable: the Q8_0 and bf16 outputs survive only as hashes in `artifacts/qwen35-9b/scan-receipt.json`, so a quality run must budget a full Shadeform re-conversion. A 2026-09-11 local development attempt aborted before any model load on host memory and a 532-commit-stale prebuilt engine, producing no score |
| Phase 3 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source sessions/local tools, the descriptor WAL, and a dormant uncompiled Win32 descriptor-WAL bootstrap (secure create/trusted reopen, single-writer lease, one-shot non-inheritable handoff) exist; native secure owner publication, an authenticity/anti-rollback anchor, compaction, a reviewed launcher and HANDLE-to-CRT child bridge, the production bridge, MSVC compile, and Windows capability evidence are missing/refused |
| Phase 4 | EVIDENCE_INCOMPLETE / UNAPPROVED | Graph reconciliation/WAL source exists, now including bounded restart completion for durably acknowledged, newly account-bound `mail.create_draft` records proved by a fresh unique exact provider GET; its startup trigger is operationally inert on memory-only tokens, manual `POST .../reconcile` is still HTTP 501, `reconciling` records have no automatic resolution path, the `listSentForDigest` collection-projection defect is confirmed and repaired by the merged sent-mail proof projection slice (`collectMailProof`: one folder ID page plus at most 20 exact GETs per call, typed inconclusive results, a `mail.send_draft` proof deadline), so `mail.send_draft` is no longer structurally unable to complete — but the unchanged pre-send Sent Items `@odata.nextLink` refusal still blocks every send for a mailbox holding more than 50 sent items (follow-up `GRAPH-SENT-PAGINATION-PRECHECK`), a pre-dispatch provider refusal still lands as `unknown_manual`, three pre-proof requests in the send branch remain uncapped, and live Graph accounts/consent, browser/Copilot evidence, and model quality are missing |
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
