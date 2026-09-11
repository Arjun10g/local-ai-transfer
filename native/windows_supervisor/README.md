# Inert Windows supervisor topology

This directory is a deliberately inert `_WIN32` source boundary. Its
`authority.cpp` — and, through it, `launch_authority.hpp` — is *listed in* the
default-OFF isolated Windows compile-check target, and listed is all it is:
`LAE_ENABLE_INERT_WINDOWS_COMPILE_CHECKS` is `OFF` and the target hard-fails
off WIN32/MSVC, so nothing here has ever been compiled by an executed target.
"Listed in a default-OFF, never-executed target" is the exact claim; it is not
"covered", "compiled", or "checked". This source is not in the product CMake
target, launcher, host, package allowlist, or production registry. The public
authority API exposes refusal and redacted loopback metadata only.

Phase 2a establishes one co-located supervisor/helper ownership topology:
`SupervisorState` owns the sole `JournalAuthorityOwner`; the pipe server and
process transaction borrow that owner under an explicit lifetime proof. The
helper receives a move-only `PipeServerBorrow` ticket and the process path a
move-only, one-use launch authority; neither accepts a raw owner. Startup
requires an explicit canonical `StorageRequest` handoff, with no implicit
storage path. Shutdown closes pipe-call admission and joins the helper, stops
and releases the long-lived pipe borrow, then stops process admission, drains
process borrows and children, drains remaining tickets, destroys leases, and
releases the owner last. The bounded shared control word makes
close/acquire linearizable; a same-thread or timed-out drain is a finite
fail-stop/refusal rather than a deadlock. No owner lock is held across waits.

All process-path identities are complete sixteen-byte arrays. Lease binding is
the complete typed binding from the owner helper, including request/call refs,
tool/risk/side-effect labels, argument/preview/operation digests,
authorization kind, sequence-one authorization, and receipt/event proof
digests. The process path owns no journal state or persistence callback.

The accepted lease has no proof issuer and external-proof authority is false.
Therefore phase 2a refuses before lease persistence, external dispatch, process
creation, or any irreversible mutation. A separately audited phase-2b
OS-handle evidence verifier is required; this source adds no issuer, friend,
factory, mint, or fake proof. Trust, production, host, package, and controller
activation remain false.

## Dormant launch-authority cancellation state machine

`launch_authority.hpp` carries the dormant `CancellationState`. Generations are
not caller supplied: `begin()` takes no argument, derives the run generation
from the authority's issued generation anchor, and returns it (`0` means
refused). The generation is strictly increasing within one authority and
bounded to eight runs. Every other transition takes a generation that must
equal the current active generation exactly; anything else is a typed refusal
that changes no state. `kUnknownManual` is terminal for an authority instance:
restart after an orphan, or after any cancellation, requires a freshly issued
authority carrying a new generation anchor from the issuer. Orphan marking is
fenced by the same generation and state guard as every other transition, so it
cannot be used to escape a running transaction into a restart.

| From | Event | Guard | To | Result |
| --- | --- | --- | --- | --- |
| `kIdle` | `begin()` | anchor non-zero, runs remaining, next generation strictly greater | `kRunning` | new generation |
| `kComplete` | `begin()` | anchor non-zero, runs remaining, next generation strictly greater | `kRunning` | new generation |
| `kRunning` | `begin()` | — | `kRunning` | `0` refused |
| `kCancelRequested` | `begin()` | — | `kCancelRequested` | `0` refused |
| `kUnknownManual` | `begin()` | — (terminal) | `kUnknownManual` | `0` refused |
| `kRunning` | `request_cancel(g)` | `g == active` | `kCancelRequested` | `true` |
| any other | `request_cancel(g)` | — | unchanged | `false` |
| `kRunning` | `complete(g)` | `g == active` | `kComplete` | `true` |
| `kCancelRequested` | `complete(g)` | `g == active`; deliberate ambiguity | `kUnknownManual` | `false` |
| `kIdle` | `complete(g)` | — never poisoned | `kIdle` | `false` |
| `kComplete` / `kUnknownManual` | `complete(g)` | — | unchanged | `false` |
| any | `complete(g)` with stale `g` | — | unchanged | `false` |
| `kCancelRequested` | `finish_bounded_cancel_join(g, true)` | `g == active`, zero active processes | `kUnknownManual` | `true` |
| `kCancelRequested` | `finish_bounded_cancel_join(g, false)` | `g == active`, join timed out | `kUnknownManual` | `false` |
| any other | `finish_bounded_cancel_join(g, *)` | — | unchanged | `false` |
| `kRunning` / `kCancelRequested` | `orphan(g)` | `g == active` | `kUnknownManual` | `true` |
| `kIdle` / `kComplete` / `kUnknownManual` | `orphan(g)` | — | unchanged | `false` |

Every transition is `noexcept` and takes a `std::mutex`. That combination is a
deliberate policy, not an accident of annotation: `std::lock_guard`
construction can throw `std::system_error`, and inside a `noexcept` function
that is `std::terminate()`. Fail-stop on a corrupt lock is the accepted
outcome, matching `SupervisorState::~SupervisorState`, because a
half-linearized cancellation state is strictly worse than a dead supervisor.

The header also binds the argument-vector obligation (ordered UTF-16-safe
array, one fixed `CommandLineToArgvW`/MSVCRT escaping algorithm, verified
round-trip parse equality, never a shell or concatenated command string) and
the canonical-path shape test, which refuses UNC, `\\?\UNC\`, device and
volume namespaces, alternate data streams, reserved device names, short-name
components, trailing dots or spaces, empty components, relative components, and
forward-slash separators. Both are recorded obligations only: this revision
builds no command line, opens no path, and launches nothing.
