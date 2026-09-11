# Blockers

## Current governance reconciliation — 2026-09-09

- Exact audited integrated source baseline is
  `main@6e0d12c0023068456b97fc9c857a3538ca612421`; this docs descendant is not a
  self-hash. Release/full access remains `BLOCKED` / `NOT_READY`.
- Protected `.secrets/shadeform.env` projection/setup source (`c8c28a9`), the
  metadata-only Windows/HF artifact handoff (`c2801ec`, merged by `e2e5156`),
  and the inherited-descriptor ActionJournal WAL (`2dda060`, merged by
  `6e0d12c`) are source-present. They do not establish credential approval,
  provider execution, signed artifact custody/trust, native Windows authority,
  production activation, or target readiness.
- The WAL has descriptor-bound durable integrity/recovery behavior but lacks a
  native secure FD owner and publication boundary, cross-process single-writer
  exclusion, authenticity/anti-rollback anchoring, compaction, and automatic
  provider reconciliation. The artifact handoff's public validator always
  returns `REFUSED_NOT_ACTIVATED` pending real signed artifacts and an approved
  trust anchor. Graph mutation ambiguity therefore remains fail-closed.
- Current focused evidence is Node 210 discovered/209 pass/0 fail/1 existing
  TODO; handoff/release 63/63; env 26/26; QA-runner 27/27; conformance 11/11.
  Static inventory is 152 tracked JSON files (151 strict-valid plus one
  intentional duplicate-key hostile fixture) and 29 modules/71 relative
  imports/0 cycles. QA has 59 discovered, 0 missing/unknown, and 65 records
  (1 PASS/64 expected SKIP); overall QA remains `BLOCKED`.
- Broader Node 350/349/0/0/1 TODO and Python 663/663 across 30/33 safe files
  are explicitly historical `d195235` evidence, not current `6e0d12c` proof.
  Remote execution remains false, ledger migration remains
  `SAFE_TO_MIGRATE_NOW=NO`, and previously exposed credentials—including the
  leaked HF token—require source-side rotation/revocation before reuse.

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

## B-003 — Production external-tool chain lacks activated durable authority and live proof

- Fact: production model evaluation proves only tool proposal/arguments. The
  hostile external-tools harness uses injected Graph/CDP/Copilot fakes and does
  not execute a real account, browser, Copilot service, or Windows process.
  Disabled/unconfigured provider definitions are now withheld from the model,
  and the reviewed action-journal/controller barrier is source-merged
  (`55f3dfd`, merge `65decba`). The descriptor-backed WAL accepted at
  `2dda060` and integrated by `6e0d12c` supplies canonical append/replay,
  crash-tail recovery, restart tombstones, and provider-proof ordering once an
  already-authoritative descriptor is injected. It does not securely acquire,
  publish, exclusively own, compact, or anti-rollback-anchor that descriptor,
  and it does not automatically reconcile provider ambiguity. Microsoft Graph
  reconciliation source is merged at `b4702a5` after two independent source
  audits but has no live-provider evidence or approval; browser/Copilot
  proposal state remains memory-only, and browser/Copilot executable paths are
  not bound to immutable file identity across preview and spawn.
- Impact: a passing model score or mocked provider run cannot establish safe
  end-to-end mail, Teams, browser-action, or Copilot readiness. After an
  ambiguous provider write, no definitive result or safe automatic retry can be
  established until provider reconciliation completes; retrying outside that
  boundary could duplicate work. A mutable executable can also change after
  preview/version inspection.
- Workaround: keep all external providers disabled by default and all writes
  confirmation-bound. Preserve per-generation active-tool filtering and the
  fail-closed journal barrier. Connect the inert storage/helper sources and
  descriptor WAL to a native secure single owner with durable publication,
  locking, authenticity/anti-rollback, bounded compaction, package wiring, and
  compile/target evidence. Complete provider-owned reconciliation and
  executable identity pinning before any write-capable live test.
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
- Source/evidence correction: lifecycle authority hardening is source-merged
  at `91de464`, but `REMOTE_EXECUTION_ENABLED=False` remains binding and no
  approved/committed cost-ledger genesis exists. The merge neither authorizes
  a live retry nor supplies model-quality evidence.
- Environment correction: mutation/recovery CLIs now default to protected
  `.secrets/shadeform.env` after accepted `c8c28a9`; project-root `.env` remains
  a separate read-only-catalogue input and is refused for mutations. An operator
  must create the protected owner-private directory and project the selected
  keys offline; source support is not credential approval or spend authority.

## B-005 — Windows process/application launch boundary is not identity-pinned

- Fact: `process.run_allowlisted` repeats canonical pathname checks before
  spawn, but Node cannot hold a deny-write/delete Windows image handle through
  `CreateProcess`. `app.open`, `browser.open_url`, and clipboard subprocesses
  currently spawn configured/bare executable paths without equivalent canonical
  identity checks and inherit the host environment. The visible browser opener
  uses bare `rundll32.exe`; the application config does not require an absolute
  path. These boundaries are mocked on non-Windows hosts only. An independently
  reviewed native broker contract and source skeleton is merged, but it contains
  no process-creation implementation, has empty trust/containment/confinement
  activation prerequisites, is excluded from build/package/host integration,
  and is explicitly `QUARANTINED` / `NOT_READY`. Later process implementation
  `6167ef6` remains rejected and unmerged. The rejected supervisor-authority
  predecessor `81cfd79` is superseded by independently accepted inert source
  `8c34cca`, merged by `9f6bbb6` after 24/24 focused source checks. The rejected
  clipboard predecessor `0ba98d5` is superseded by inert repair `04d6860` and
  audited handoff `393189f`, merged by `0622713` after 26/26 focused source plus
  2/2 inventory checks. Dormant supervisor-owned process transaction `64b3947`
  is merged by `ca2d893` after 56/56 source plus 2/2 inventory checks, but its
  durable adapter remains null/unrecovered and it has no public launch API,
  CMake, or package path. All merged boundaries remain unlinked, uncompiled,
  activation-gated false, and explicitly unavailable for production/target use.
- Impact: a path replacement/search-path race can change executed bytes, and a
  launched application can inherit provider credentials or other host secrets.
  Current process/app/browser/clipboard tests do not establish safe laptop
  execution.
- Workaround: keep these launch-capable tools disabled in the Windows product
  profile. Complete the quarantined skeleton with an authenticated supervisor,
  pre-child containment, real least-privilege confinement, identity-pinned
  executable handling, and minimal environment; independently review its host
  integration, then run real Windows cancellation/orphan/path-replacement
  acceptance.
- Needed from: S1/S3 implementation, S4 security review, and target operator.
- State: OPEN; blocks Phase 3/4/6 readiness and full laptop-control claims.

## B-006 — Model/tool quality and artifact acceptance remain below gate

- Fact: the strongest recorded general remote tool evaluation completed at
  27/34, below its gate. A separate production-profile run completed at 13/32
  with 19 failures and therefore failed that profile. Subsequent corrected
  attempts did not reach evaluation because provider activation timed out.
- Fact: the product verifier pins `Qwen3.5-9B-Q4_K_M.gguf` to exactly
  5,629,109,088 bytes and SHA-256
  `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`.
  That technical identity does not approve ignored local bytes or establish
  transfer custody. Local revalidation, an approved transfer receipt, and the
  chain of custody remain unset.
- Fact: accepted source `c2801ec`, integrated by `e2e5156`, adds a bounded
  metadata-only artifact-handoff schema/validator. Its public path has no trust
  anchor and always returns `REFUSED_NOT_ACTIVATED`; the private future seam can
  validate an approved signature but never activates or reads model bytes.
  No real signed handoff, approved key, trust-store integration, or Windows
  release-verifier receipt is present.
- Impact: neither model/tool quality nor the deployable local artifact is
  accepted for release.
- Workaround: keep the known identity and activation-refused handoff as
  fail-closed constraints; do not load or distribute local bytes until real
  signed receipts, an approved trust anchor/transfer, and fresh verification
  evidence exist. Resume quality work only through the separately authorized,
  lifecycle-gated route.
- Needed from: S0/S2 artifact and quality approval, S4 evidence review, and the
  target operator for the approved transfer/acceptance route.
- State: OPEN; blocks Phase 1/2/4/7/8 and every readiness claim.
