# Status Packet

- **Session:** S1
- **Required model:** GPT-5.6 Luna
- **Role:** Runtime/security boundary — B-005 Windows native broker repair
- **Timestamp (UTC):** 2026-09-05T03:53:15Z
- **Branch/worktree:** `luna/windows-native-process-broker-37a134f` / `wt-windows-native-process-broker`
- **Current phase:** quarantined source-only research lane
- **Primary task ID:** B-005-SOURCE
- **Task state:** QUARANTINED
- **Base `main` commit:** `37a134f16c936849d5dd32c96fcd8c0f7d90608e`

## Outcome

The rejected broker skeleton was repaired by removing its process-creation
implementation and making normal startup fail closed. The compiled manifest
trust anchor remains empty. Non-overridable supervisor-containment and launch-
confinement prerequisites remain false. Launch requests are refused before a
worker or child is created.

The earlier claims of safe suspended-create/Job assignment, restricted-token
confinement, full-Job reaping, and Windows-safe bounded behavior are withdrawn.
An in-process Job cannot close the broker-death interval between child creation
and Job assignment, and `DISABLE_MAX_PRIVILEGE` alone is not confinement.

## Repair scope

- No CMake, package, host, registry, launcher, action-journal, or capability
  integration was added.
- Process launch APIs were removed from the source skeleton. Receipts always say
  `process_created: false` and make no Job/token/confinement claim.
- Protocol reads/writes, I/O cancellation, worker start, output lock, and joins
  now have explicit bounds or fail-stop behavior.
- Windows headers establish `NOMINMAX` and `WIN32_LEAN_AND_MEAN` before
  `windows.h`.
- Product manifest parsing now requires exact executable classes, action policy
  IDs, a future AppContainer/low-integrity confinement profile, fixed and fully
  consumed parameter placement, finite process argv values, application fixed
  argv, bounded HTTPS browser URLs, Copilot stdin-only prompts, and bounded
  clipboard actions. Interpreters/common LOLBins and control-bearing literals or
  values are rejected.
- Clipboard source rechecks deadline/cancellation before reads and mutation,
  uses manifest bounds, and treats invalid conversion separately from true empty
  clipboard text.
- A bounded 1,024-entry request-ID replay cache was added. It is not durable; a
  durable host action journal remains mandatory before activation.

## Evidence and limits

Evidence is limited to JSON parsing, static Python contract tests, source
inspection, and `git diff --check`. No CMake configure, compile, package,
launcher, registry, host, model, provider, browser, Windows process, or live
action was run. Static tests do not establish C++17 compilation or Windows
runtime behavior.

- `python3 -m unittest tests.native.test_windows_process_broker_static -v`:
  11/11 passed on the local macOS development host.
- `python3 -m json.tool` parsed all 26 broker schema/fixture JSON files.
- `git diff --check` passed.
- A Draft 2020-12 validator was not available locally; no package was installed
  to obtain one. Schema syntax and the independent semantic fixture harness are
  the only local contract evidence.

## Prior packet and rejection history retained

The prior 2026-09-05T03:18:17Z packet marked commits `d2f29a6` and `8b01290`
`READY_FOR_REVIEW` after 11 local static tests. It claimed suspended child
creation, restricted-token launch, assignment to a kill-on-close Job before
resume, and `ACTIVE_PROCESS_ZERO` proof. The subsequent security audit rejected
that design: it found the pre-Job broker-death orphan interval, unbounded
synchronous I/O/join paths, insufficient token confinement, permissive manifest
semantics, clipboard cancellation/conversion gaps, and absent replay refusal.
This superseding packet records the repair without erasing that evidence trail.

## Activation blockers retained

1. Authenticated supervisor-created containment with no pre-containment child
   interval and exact-target broker-death evidence.
2. Reviewed AppContainer or deny-only/restricted-SID plus low-integrity profile,
   including GUI/browser/Copilot behavior.
3. Full-tree termination and `ACTIVE_PROCESS_ZERO` proof for every post-create
   error before any terminal receipt.
4. Executable dependency-closure, DLL/plugin/config, and script identity review.
5. Durable host action journal/replay/recovery integration.
6. Remote Windows compile, signing, package, hostile runtime, clipboard, and
   enterprise-target acceptance evidence.

## Readiness and review instruction

`NOT_READY`. B-005 remains open. This quarantined source is **not approved to
merge or integrate**. A later review may use it as a non-runnable contract
artifact only; source review never authorizes Windows/provider execution.
