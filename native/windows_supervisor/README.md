# Inert Windows supervisor topology

This directory is a deliberately unlinked `_WIN32` source boundary. It is not
in `native/CMakeLists.txt`, the launcher, the host, the package allowlist, or a
production registry. The public header exposes refusal and redacted loopback
metadata only.

Phase 2a establishes one co-located supervisor/helper ownership topology:
`SupervisorState` owns the sole `JournalAuthorityOwner`; the pipe server and
process transaction borrow that owner under an explicit lifetime proof. Startup
requires an explicit canonical `StorageRequest` handoff, with no implicit
storage path. Shutdown stops admission, drains process borrows and children,
stops the pipe, destroys leases, and releases the owner last. No owner lock is
held across waits.

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
