# Status Packet

## Dormant Windows process authority boundary

- **Role:** S1 Luna; independent source-only implementation owner.
- **Branch/worktree:** `luna/windows-process-authority-gap-v1` /
  `wt-windows-process-authority-gap-v1`.
- **Base:** exact `main` `d723c43263ee34211abe12c414aefa1c290a3ec1`.
- **First claimed task:** add a bounded, inert authority contract for
  identity-pinned executable/working-directory handles, pre-child containment,
  minimal child environment, cancellation/orphan ownership, and explicit
  deny-closed activation/package gates. Exercise it through the existing
  process-transaction refusal seam.
- **Dependencies:** existing `ProcessLaunchAuthority`, complete dispatch
  binding, dormant Windows supervisor gates, and the broker manifest/request
  contracts. This slice does not alter ActionJournal or Graph behavior.
- **Assumptions:** the authority remains unavailable in this source-only
  phase; no proof issuer, native handle verifier, CMake target, provider,
  browser, Copilot, or product activation may be introduced.
- **Uncertainties:** Windows handle identity, Job-object containment, token
  construction, and orphan cleanup still require a native implementation and
  target evidence; the contract records requirements without claiming them.
- **State:** IN_PROGRESS; no compile, process launch, network, model, or live
  evidence is permitted.

## Outcome

- Added the dormant `windows-process-authority` v1.0.0 contract and the
  Windows-only `LaunchAuthorityProof` shape. It binds supervisor-owned
  executable and working-directory handles to manifest identity, requires
  pre-child containment, least privilege, a fixed child environment, and
  supervisor-owned cancellation/orphan cleanup. It carries no serializable
  path, handle, argv, environment, prompt, or credential receipt fields.
- `process_transaction.inc` now checks the proof before the existing global
  process gates and before any future mutation boundary. The proof authority,
  CMake/package/host/registry gates, and broker containment/confinement gates
  remain false; this slice cannot launch or activate a process.
- Added nine hostile static/model tests and registered them in the bounded QA
  inventory. The test model refuses every missing proof and the complete proof
  while the global gate is false.

## Evidence

- `python3 tests/native/test_windows_process_authority_static.py`: 9/9 PASS.
- `python3 tests/native/test_windows_process_transaction_static.py`: 23/23
  PASS; broker static: 11/11 PASS; supervisor authority: 11/11 PASS; dispatch
  lease: 23/23 PASS; inert compile harness: 13/13 PASS; clipboard: 26/26
  PASS. Combined Windows-static discovery: 236/236 PASS.
- `python3 scripts/test/run_qa.py --output -`: no missing/unknown inventory
  entries and the expected safe-mode `BLOCKED` result. `git diff --check` and
  contract JSON parsing pass. No CMake, compiler, Windows, child-process,
  provider, browser, Copilot, model, network, or live evidence was produced.

## Residual blockers

- A native Windows handle-relative identity verifier, supervisor-owned RAII
  handle lifetime, Job-object/token implementation, cancellation race suite,
  SDK compile/static-analysis evidence, and target receipt remain absent.
- The proof header is intentionally not in CMake and has no issuer or public
  activation path. Production, package, host, registry, live, and target
  readiness remain `NO`/`NOT_READY`.
