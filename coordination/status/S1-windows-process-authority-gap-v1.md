# Status Packet

## Dormant Windows process authority boundary

- **Role:** S1 Luna; independent source-only implementation owner.
- **Branch/worktree:** `luna/windows-process-authority-gap-v1` /
  `wt-windows-process-authority-gap-v1`.
- **Base:** exact `main` `d723c43263ee34211abe12c414aefa1c290a3ec1`.
- **Task ID:** `RUN-WINDOWS-PROCESS-AUTHORITY-GAP`.
- **First claimed task:** add a bounded, inert authority contract for
  identity-pinned executable/working-directory handles, pre-child containment,
  minimal child environment, argument-vector policy, cancellation/orphan
  ownership, and explicit deny-closed activation/package gates. Exercise it
  through the existing process-transaction refusal seam.
- **Dependencies:** existing `ProcessLaunchAuthority`, complete dispatch
  binding, dormant Windows supervisor gates, and the broker manifest/request
  contracts. This slice does not alter ActionJournal or Graph behavior.
- **Assumptions:** the authority remains unavailable in this source-only
  phase; no proof issuer, native handle verifier, CMake target, provider,
  browser, Copilot, or product activation may be introduced.
- **Uncertainties:** Windows handle identity, Job-object containment, token
  construction, and orphan cleanup still require a native implementation and
  target evidence; the contract records requirements without claiming them.
- **State:** READY_FOR_REVIEW; no compile, process launch, network, model, or
  live evidence is permitted.

## Outcome

- Added the dormant `windows-process-authority` v1.0.0 contract and a private,
  noncopyable `LaunchAuthority` with move-only `UniqueHandle` ownership. It
  binds supervisor-owned executable/working-directory, token, Job, and
  cancellation handles to canonical identity values, operation ID/generation/
  nonce, pre-child containment, least privilege, a fixed environment, an
  ordered argument vector, and supervisor-owned cancellation/orphan cleanup.
- No path, handle, argument, environment, prompt, or credential value is
  exposed by any accessor or is a receipt field, and the slice contains no
  serialization code at all. The authority does hold a `std::wstring`
  canonical path and owning `HANDLE`s inside `UniqueHandle`; what is
  guaranteed is that none of them is reachable from outside the class and that
  `LaunchReceipt` is exactly `{status, journal_bound, mutation_attempted}`,
  matching the contract's receipt field list.
- `process_transaction.inc` validates the private authority before consuming
  the process borrow, then checks the existing global process gates before any
  future mutation boundary. The proof issuer, CMake/package/host/registry
  gates, and broker containment/confinement gates remain false; this slice
  cannot launch or activate a process.
- Added the C++ generation-fenced cancellation state machine with a full,
  enumerated transition table, and forty-one hostile static/model checks
  registered in the bounded QA inventory. The model refuses every missing
  proof and refuses a complete proof while the global gate is false.

## Review resolution (S0/S4 ACCEPT_WITH_REQUIRED_FIXES)

| Finding | Disposition | Where |
|---|---|---|
| F-1 six dropped proof predicates | fixed | `native/windows_supervisor/launch_authority.hpp` `FileIdentity`/`MintedParts`/`valid_for_admission()`; cross-check test `test_every_contract_predicate_is_a_header_field_and_a_validator_conjunct` |
| F-2 `mark_orphaned()` bypasses the fence | fixed | `orphan(generation)` is generation- and state-fenced; `kUnknownManual` is terminal |
| F-3 caller-supplied, unanchored generation | fixed | `begin()` takes no argument, derives from the issued anchor, strictly monotonic, bounded to eight runs |
| F-4 fence untested | fixed | `CancellationModel` implements the same guard; source-anchored mutation checks on `begin()`/`orphan()`/`complete()`/`finish_bounded_cancel_join()` |
| F-5 deleted friend ban | fixed | `tests/native/test_windows_supervisor_authority_static.py` restores the ban for `authority.hpp`/`authority.cpp` and pins the launch header's friend names exactly |
| F-6 friendship names an undefined type | fixed | friendship removed; enforcement invariant restated as the private `UniqueHandle(HANDLE)` constructor; every friend name resolves to a type the header declares |
| F-7 no argv policy | fixed | contract `arguments` section plus `ArgumentVector`, six argument predicates, and a `CommandLineToArgvW` round-trip model |
| F-8 permissive `canonical_absolute()` | fixed | one accepted shape; hostile tables for UNC, device, volume, ADS, reserved name, relative, short name, trailing dot/space, empty component, wildcard, forward slash |
| F-9 packet state | fixed | `READY_FOR_REVIEW` above |
| F-10 `git diff --check` did not reproduce | fixed | trailing whitespace removed; evidence below re-run against `main...HEAD` |
| F-11 `complete_run(0)` poisons a fresh authority | fixed | typed refusal, no state change |
| F-12 join timeout stayed `kCancelRequested` | fixed | both join outcomes are terminal `kUnknownManual` |
| F-13 stale ordering comment | fixed | `native/windows_supervisor/process_transaction.inc` |
| F-14 commit prefixes | fixed | new commits use `docs:`, `contracts:`, `security:` |
| F-15 `noexcept` plus mutex | documented as deliberate | header comment and `native/windows_supervisor/README.md`: fail-stop on a corrupt lock is accepted |
| F-16 "covered by the compile-check" | documented | README and contract `.md` now say *listed in* a default-OFF, never-executed target |
| F-17 packet wording | fixed | Outcome section above |

## Evidence

- `python3 tests/native/test_windows_process_authority_static.py`: **41/41
  PASS** (was 12/12).
- `python3 -m unittest discover -s tests/native -p 'test_windows_*static.py'`:
  **268/268 PASS** (was 239/239). Per suite: process authority 41, process
  transaction 23, dispatch lease 23, action-journal helper 27, hardware
  attestor 27, clipboard 26, action-journal owner 17, action-journal storage
  16, read-only fs 15, inert compile harness 13, broker 11, supervisor
  authority 11, release verifier 18.
- Strict duplicate-key JSON parse of
  `contracts/windows-process-authority/v1.0.0.json`: **PASS**, exit 0, 13
  top-level keys, no duplicate keys. The file is a declarative posture
  document with no JSON Schema keywords, so no `additionalProperties` clause
  exists to preserve.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -`:
  `status: BLOCKED`, `passed: False`, `release_passed: False`,
  `kind: plan-only-fixture-static`, `mode: safe`; **discovered 60, missing 0,
  unknown 0**, `discovery_error: None`; **66 records = 1 PASS / 65 SKIP**;
  process exit code 1. `tests/native/test_windows_process_authority_static.py`
  is present as `native_static` / `SKIP` / `UNPROVEN` /
  `safe_mode_disables_subprocess_model_network_and_lifecycle_execution`.
- `git diff --check main...HEAD`: **exit 0** (was exit 2).
- Ten hand-applied source mutations were each verified to fail the suite:
  dropping `kUnknownManual` from the `begin()` fence, dropping the
  `no_reparse_components` conjunct, dropping the `credentials_excluded`
  conjunct, adding a raw `HANDLE` accessor, re-adding the undefined
  supervisor-state friendship, unfencing `orphan()`, making a join timeout
  fall back to `kCancelRequested`, dropping the alternate-data-stream check,
  dropping the short-name check, and adding a contract predicate with no
  header binding.
- No CMake, compiler, Windows, child-process, provider, browser, Copilot,
  model, network, or live evidence was produced. Nothing was compiled.

## Residual blockers

- A native Windows handle-relative identity verifier, supervisor-owned RAII
  handle lifetime, Job-object/token implementation, cancellation race suite,
  SDK compile/static-analysis evidence, and target receipt remain absent.
- The authority still supplies no *mechanism*. `valid_for_admission()` tests
  only that declarative fields are set and that recorded values are non-zero;
  it re-derives nothing from the live handle at consume time, and the future
  command-line builder and round-trip verifier do not exist.
- The proof header is listed in the default-OFF inert Windows compile-check
  through `authority.cpp` but has never been compiled by any executed target,
  remains outside the product CMake graph, and has no issuer or public
  activation path. Production, package, host, registry, live, and target
  readiness remain `NO`/`NOT_READY`.

## Proposed B-005 wording for Sol

`coordination/BLOCKERS.md` is intentionally untouched by this branch. The
following replaces the reviewer's §5 draft, adjusted for the final code, and
is offered for Sol to fold into the governance refresh. Append to the existing
`B-005` *Fact* paragraph after the `ca2d893` sentence and before "All merged
boundaries remain unlinked...":

> Dormant launch-authority contract `windows-process-authority` v1.0.0 and a
> private, noncopyable, move-only `LaunchAuthority` are merged by
> `<merge-sha>`; they replace the previous public POD proof with
> supervisor-owned `UniqueHandle` ownership, bind executable/working-directory/
> token/Job/cancellation handles to recorded identity values plus operation
> ID/generation/nonce, require the absolute-manifest-path-binding and
> no-reparse-component predicates on both bound file identities, carry an
> ordered argument-vector policy with one fixed `CommandLineToArgvW` escaping
> algorithm and required round-trip parse verification, add a cancellation
> state machine whose generations are issuer-derived and strictly monotonic and
> whose `unknown_manual` state is terminal, and close a latent gap by adding
> `kNestedJobPolicyProven` to `kProductionAvailable`. They supply no mechanism:
> the Windows process-creation API accepts a path rather than a handle, no
> image-section retention, `NtCreateUserProcess`,
> `GetFinalPathNameByHandle` re-derivation, or post-launch image verification
> exists, and `valid_for_admission()` only tests that declarative fields are
> set and that recorded values are non-zero — it never re-derives identity from
> the live handle at consume time, so a mint-to-spawn executable or junction
> swap remains undetected. No command line is built, no argument is escaped,
> and no path is opened. The header is outside the product CMake graph, has no
> proof issuer, and has never been compiled by any executed target.

Recommended state line: leave `State: OPEN; blocks Phase 3/4/6 readiness and
full laptop-control claims.` unchanged. This slice does not move the blocker's
state.
