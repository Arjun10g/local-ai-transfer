# Release Gates

Overall release/full-access state: **BLOCKED / NOT_READY**. The states below
describe formal evidence/approval gates, not whether source work exists.

Current audited integrated source baseline is exact
`main@f89c1684ea051e1c9c92f944cb080d424a086f55`, the last source merge; the
docs descendants `1e341e9`, `aa1087a`, `25a0d95`, `74a43d0`, `335211f`,
`1b22a40` and this refresh are documentation descendants, not self-referential
source hashes. The previous baseline `263f114` is historical and superseded.

Current reproduced evidence, measured in the refresh v6 worktree on
2026-09-11: Python `python3 -m unittest discover -s tests -t . -p 'test_*.py'`
**`Ran 743 tests` OK**; `npm test` **432 tests / 430 pass / 0 fail / 0
cancelled / 1 skipped / 1 todo** (the single skip and single todo are the
pre-existing filesystem `KNOWN LIMITATION` pair); safe QA **`BLOCKED`** with
**70 discovered, 0 missing, 0 unknown, and 76 records (1 PASS / 75 expected
SKIP)**; the offline J1M dry-run gate **PASS** with **142 argv recorded, 0
refused, USD 0.00, 0 WARN**; the quality-corpus validator **PASS in both
default and `--require-complete` modes over 1,336 cases**; strict tracked-JSON
inventory **168 tracked / 167 strict-valid / 1 intentional hostile fixture**
(`tests/native/fixtures/windows_broker/duplicate-key.json`, duplicate key
`deadline_ms`); and a host import graph of **29 modules / 67 unique relative
import edges / 0 unresolved specifiers / 0 cycles** from 72 relative import
occurrences.

Two invocation facts belong with those numbers. First, the `-t .` root argument
is load-bearing: without it, `tests/qa/` shadows the root `qa/` package and
discovery collapses to `Ran 646 tests` with **8 module-level `ERROR`s**
(`model.test_windows_artifact_handoff`, `performance.test_j1m_dry_run`
registration, `qa.test_evidence`, `qa.test_safe_runner`,
`release.test_package_scanner`, `release.test_windows_acceptance`,
`release.test_windows_hardware_receipt_diagnostic`,
`security.test_adversarial`). Second, the dry-run gate has an operator
precondition git cannot carry: in a fresh worktree it **FAILS**
`operator_artifact_destination_is_salvage_ready` until
`chmod 700 artifacts artifacts/qwen35-9b` is applied, after which it passes.
Both were reproduced here.

The prior `263f114` numbers (Node 432/430, native static 309, JSON 154/153, QA
64 discovered/70 records), the `7239b7e` numbers (Node 426/424, native static
289, JSON 153/152, QA 62 discovered/68 records), and the older `6e0d12c`
focused numbers (journal/Graph Node 210/209, handoff/release 63/63, env 26/26,
QA-runner 27/27, conformance 11/11, JSON 152/151, imports 29/71, QA 59
discovered/65 records) are historical. Historical broad Node 350/349 and Python
663/663 evidence belongs only to `d195235`.

No model evidence is recorded against the current shipping profile. The
production evaluation fixture is 33 tools and 37 cases
(`tests/model/production_tool_call_eval.json`, SHA-256
`c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`) and has no
score. Every recorded score — 28/34 on the retired 11-tool/34-case canary and
13/32 on the retired 28-tool/32-case profile — was measured on 2026-09-04 on
CUDA/A100, never on CPU or Intel Vulkan, and neither fixture is the shipping
profile. A remote evaluation run authorized under ADR-0005
(`remote-eval-20260911-b`, caps USD 10.00 / 4 h) was executing when this
refresh was written; **its result is not recorded here and no gate anticipates
it**. A first attempt (`remote-eval-20260911-a`) stopped pre-spend at USD 0.00
with no instance created.

Spend authorization is not gate progress. ADR-0005 records standing user
authorization, a program hard cap of USD 50, and per-run caps; none of that
advances any gate below, and a run result that passes a threshold is evidence
to be recorded, not approval. Gate states remain a separate Sol decision.

| Gate | State | Required evidence / remaining work |
|---|---|---|
| Phase 0 | IN_PROGRESS / UNAPPROVED | Contracts, policy matrix, source pin procedure, Shadeform profile/cost preflight |
| Phase 1 | EVIDENCE_INCOMPLETE / UNAPPROVED | Fixture/source work and an activation-refused metadata handoff exist; real signed artifacts, approved trust anchor, controlled artifact acceptance/custody, and oracle evidence are missing |
| Phase 2 | EVIDENCE_INCOMPLETE / UNAPPROVED | Real accepted-artifact CPU inference, oracle parity, and quality gate. Fixture-identity gap: every recorded score was measured on 2026-09-04 against retired fixtures (11-tool/34-case canary 28/34; 28-tool/32-case profile 13/32) on CUDA/A100, never CPU or Intel Vulkan; the current 33-tool/37-case shipping profile (`tests/model/production_tool_call_eval.json`, sha `c75af520…c8ac6c`) has no recorded score, so the model's score on the profile it must ship against is unknown rather than merely below gate. The ≥95% retention comparator is now **reachable but unmeasured**, correcting the earlier "unreachable" wording: the Q8_0 and bf16 outputs survive only as hashes in `artifacts/qwen35-9b/scan-receipt.json` because the cleanup tail deletes them at the end of each run, but both are rebuilt in every eval run (`scripts/j1m_runner.py:1420-1422`) and the merged comparator slices make that tail deferrable (`--retain-comparators` / default-OFF `--evaluate-comparators`), so a retention number costs a marginal USD 0.8325–1.3725 inside one budgeted run rather than a separate full re-conversion. No retention number exists yet. The quality corpus is merged and complete (1,336 cases, 13 categories, splits 271/250/815, deterministic `match` on all 555 scoring propositions, validator PASS in both modes) but a fixture is not a score and it advances nothing here; two audit residuals must close before it is used to produce a retention or parity number. A 2026-09-11 local development attempt aborted before any model load on host memory and a 532-commit-stale prebuilt engine, producing no score. A run authorized under ADR-0005 was executing when this refresh was written and its result is not recorded here |
| Phase 3 | EVIDENCE_INCOMPLETE / UNAPPROVED | Source sessions/local tools, the descriptor WAL, and a dormant uncompiled Win32 descriptor-WAL bootstrap (secure create/trusted reopen, single-writer lease, one-shot non-inheritable handoff) exist; native secure owner publication, an authenticity/anti-rollback anchor, compaction, a reviewed launcher and HANDLE-to-CRT child bridge, the production bridge, MSVC compile, and Windows capability evidence are missing/refused |
| Phase 4 | EVIDENCE_INCOMPLETE / UNAPPROVED | Graph reconciliation/WAL source exists, now including bounded restart completion for durably acknowledged, newly account-bound `mail.create_draft` records proved by a fresh unique exact provider GET; its startup trigger is operationally inert on memory-only tokens, manual `POST .../reconcile` is still HTTP 501, `reconciling` records have no automatic resolution path, the `listSentForDigest` collection-projection defect is confirmed at code level (the Graph API premise it rests on — that `internetMessageHeaders` is returned only on single-message projections — is unverified without a live account, and the repair is fail-closed under either behavior) and repaired by the merged sent-mail proof projection slice (`collectMailProof`: one folder ID page plus at most 20 exact GETs per call, typed inconclusive results, a `mail.send_draft` proof deadline), so the sent-mail proof path now requests per-message projections; whether `mail.send_draft` can complete against a real account remains unverified (no live-account evidence; all mock probes end without completion by design) — and the unchanged pre-send Sent Items `@odata.nextLink` refusal still blocks every send for a mailbox holding more than 50 sent items (follow-up `GRAPH-SENT-PAGINATION-PRECHECK`), a pre-dispatch provider refusal still lands as `unknown_manual`, three pre-proof requests in the send branch remain uncapped so the seam is bounded while the tool as a whole is not and a slow provider can still surface as `tool_timeout` → `unknown_manual`, and live Graph accounts/consent, browser/Copilot evidence, and model quality are missing. Documentation correction only (2026-09-11): `SECURITY_AND_TOOL_POLICY.md` §10 described `process.run_allowlisted` with an `executable_id`/`arguments`/`workspace_id`/`timeout_ms` schema that no shipping component accepts; all three shipping definitions use `action_id` plus optional `parameters`, and §10 is corrected to the shipping form. No capability, schema, or gate changed |
| Phase 5 | EVIDENCE_INCOMPLETE / UNAPPROVED | Performance, caches, and exact-device acceleration evidence. Still entirely absent: no cold/warm separation, no first-token/prefill/decode/cache/state-restore breakdown, no median/p95/dispersion with sample counts, and no exact-device placement receipt. The only hardware any recorded figure was measured on is CUDA/A100, which is neither the mandatory CPU backend nor the candidate Intel Vulkan backend, so nothing recorded is admissible here. A comparator arm can now produce a same-artifact upstream-oracle parity delta inside a budgeted eval run, but that is a quality/parity measurement, not performance evidence, and none has been produced. No `duration_ms` observation from a development host is carried into this record: single-run wall clocks on one machine are not reproducible evidence |
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
