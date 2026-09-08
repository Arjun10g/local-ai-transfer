# ProcessDispatchLease — phase 2 slice 1

This is a private, source-only API on `JournalAuthorityOwner`. It is not
included by the product CMake graph, supervisor, controller, launcher, host,
or package. All production gates remain literal `false`.

`acquire_process_dispatch_lease` first reloads the fixed container under the
owner lock and accepts only a complete exact binding whose record tip is the
`authorized` event at sequence `1`. The binding includes the sixteen-byte
`act_` operation identifier, request/call references, tool/risk/side-effect,
argument/preview/operation digests, authorization kind, and both authorized
receipt and event proofs. The API uses typed fixed-size bytes and never gives a
lease JSON, an HMAC key, or a session nonce.

The owner holds at most eight live leases. The registry is protected by the
owner mutex; ordinary mutations check it under the same lock and fail closed
when they target a leased operation. A lease is noncopyable, nonmovable, and
one-use. `persist_dispatching` is the linearization barrier: it durably appends
`authorized -> dispatching`, then reloads and verifies the exact event and
receipt proof. Only after that proof may `begin_external_dispatch` be called.
This source slice has no OS mutation method.

Acknowledgement recovery is an exact read-only reload/lookup. It never retries
dispatch. Post-dispatch completion requires the matching external proof and
the `dispatching -> reconciling -> completed` path. A missing or ambiguous
proof becomes `unknown_manual` with no replay. Destruction of a lease after
dispatch poisons the owner and blocks further work. Any `unknown_manual`
record globally blocks new prepare, lease, and dispatch work until explicit
resolution.

Lock order is fixed: admission CAS, then owner mutex, then store internals.
The owner mutex is never held across process, pipe, or external-provider
waits. Durable store commits are bounded storage operations; the future
supervisor owns all process/pipe waits outside this boundary.
