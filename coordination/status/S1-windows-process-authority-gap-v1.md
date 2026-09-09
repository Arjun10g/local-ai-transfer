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
