# Windows native broker source skeleton (B-005)

Status: **NOT_READY**. This directory is an inactive, non-runnable design
artifact for a future Windows action broker. It is absent from CMake, packaging,
the launcher, the host, and the tool registry. Its manifest trust anchor is
empty. Two non-overridable activation prerequisites are also `false`:
supervisor-created containment and launch confinement. Supplying a manifest
hash cannot activate it.

There is no process-creation implementation in this source. Launch actions
(`process`, `application`, `browser`, and `copilot`) are refused before a worker
or child is created. The source must remain quarantined until a separately
reviewed implementation has Windows runtime evidence.

## Why launch is refused

Creating a suspended child and assigning it to an in-process Job Object leaves
an unavoidable interval in which broker death can orphan that child. Error-path
cleanup inside the broker cannot close the broker-death interval. Activation
therefore requires an authenticated supervisor-created containment primitive
that already contains the broker and is proven to contain every child from the
instant it exists. It must terminate the full process tree and prove
`ACTIVE_PROCESS_ZERO` before any terminal success or failure receipt.

`DISABLE_MAX_PRIVILEGE` is not confinement. A future launcher must also prove a
real least-privilege profile—deny-only/restricted SIDs plus low integrity, or a
reviewed AppContainer equivalent—on the accepted Windows target. The current
receipt has `process_created: false`; it makes no token, Job, reaping, or
confinement claim.

## Inactive contract surface

The proposed transport is inherited stdin/stdout with a four-byte big-endian
length and bounded JSON body. Every inbound frame, including its first byte,
has one five-second assembly deadline. Response writes run under a five-second
deadline and use checked synchronous-I/O cancellation; a thread that cannot be
cancelled causes the broker to fail-stop instead of leaving caller-owned memory
live. Worker
joins first prove native-thread termination under a deadline. There are no
unbounded pipe reads, writes, waits, or joins in the protocol/broker sources.

The request contains a logical action ID and typed arguments only. It cannot
name an executable, raw argv, cwd, environment, shell, or manifest. A bounded
1,024-entry in-memory replay cache rejects recently reused request IDs. That
cache is only defense in depth: it is not durable across restart. A durable host
action journal with duplicate-action and unknown-after-dispatch handling is a
mandatory activation dependency.

The authenticated product manifest parser additionally requires:

- an exact executable path, SHA-256, size, NTFS volume/file identity, explicit
  executable class, action policy ID, cwd identity, and fixed argv template;
- rejection of interpreters, shells, generic script hosts, and common LOLBins;
- no NUL, newline, control character, invalid numeric value, optional/ignored
  parameter, or unconsumed argv parameter;
- application actions with only fixed literal argv;
- browser actions with one bounded `https` URL argument (no credentials, port,
  fragment, or localhost);
- Copilot prompts only through bounded stdin, never argv or receipts;
- process parameters only in explicit argv positions and limited to finite
  manifest values; and
- clipboard read/write limits no larger than 64 KiB and five seconds.

The checked-in manifest is deliberately `mode: fixture` and uses
`unproven-disabled`. Product parsing rejects it. Clipboard implementation text
is present for contract review, including manifest bounds, strict UTF-8/UTF-16
conversion, and cancellation/deadline checks before reads and mutation, but it
is unreachable through normal startup because the global activation gate is
false.

## Evidence still required

Static tests check the refusal gates, source surface, schemas, negative
fixtures, replay bound, and timeout patterns. They are not compilation proof,
Windows runtime evidence, containment proof, confinement proof, or permission to
integrate. Before activation, a later review must establish at least:

1. An authenticated, identity-pinned supervisor and containment design that has
   no pre-Job/pre-containment child interval and survives broker death.
2. AppContainer or equivalently reviewed deny-only/restricted-SID and
   low-integrity behavior for console, visible GUI, browser, and Copilot lanes.
3. Bounded cancellation, full-tree termination, and `ACTIVE_PROCESS_ZERO`
   receipts for every post-create exception, timeout, parent death, and output
   failure on the exact enterprise Windows target.
4. DLL/plugin/config dependency closure for every immutable executable and
   script identity; primary-image hashing alone is insufficient.
5. Clipboard desktop/session behavior and conversion tests on the target.
6. A durable host action journal and recovery/reconciliation policy.
7. Remote compile, signing, packaging, and adversarial runtime tests.

Until all of that is reviewed, B-005 and Windows laptop-control readiness remain
**NOT_READY**. This source must not be added to any build or runtime path.
