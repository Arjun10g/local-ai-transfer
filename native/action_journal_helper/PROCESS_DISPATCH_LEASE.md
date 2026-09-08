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

Acknowledgement recovery is an exact read-only reload/lookup after the lease
has crossed `begin_external_dispatch`. The caller supplies the external
receipt/event proof; recovery recomputes the canonical acknowledged receipt
digest from that proof and the on-disk dispatching event, then stores the exact
proof in the lease without appending an acknowledge event. It never retries
dispatch or acknowledge. A missing, divergent, or mismatched proof becomes a
fail-closed readback failure. Post-dispatch completion requires the recovered
matching external proof and the `dispatching -> acknowledged -> reconciling ->
completed` path. A missing or ambiguous proof becomes `unknown_manual` with no
replay. Destruction of a lease after dispatch poisons the owner and blocks
further work. Any `unknown_manual` record globally blocks new prepare, lease,
and dispatch work until explicit resolution.

All public lease methods return a finite status even when bounded string, JSON,
hash, map, or storage work reports an exception. The lease constructor is
intentionally potentially-throwing and is called only inside the exception
boundary of acquisition. Once a lease has passed its exact registration,
reload, tip, and caller-proof checks, `kNotFound`, `kInvalidTransition`,
mutation conflicts, authority divergence, storage failures, and readback
failures poison the owner; they are never a ready retry. Caller binding or
external-proof mismatches detected before those checks remain non-poisoning
rejections. The shared legacy `fail_definitive` parser path is not callable
through this lease API: definitive failure is enforced as pre-dispatch-only.

Lock order is fixed: admission CAS, then owner mutex, then store internals.
The owner mutex is never held across process, pipe, or external-provider
waits. Durable store commits are bounded storage operations; the future
supervisor owns all process/pipe waits outside this boundary.

`ProcessExternalProof` is an opaque, move-only capability. It has no public
constructor, fields, or test factory; only the future co-located
`TrustedProcessExternalProofIssuer` in slice 2 may mint one after independently
validating real Windows process/job/handle/creation/I/O evidence. The proof is
bound to the owner-generated, non-wrapping dispatch generation as well as the
operation and external receipt/event evidence. Slice 1 defines no issuer and
therefore has no genuine external-proof success path until that trusted seam
is implemented. Arbitrary caller digests cannot acknowledge, recover, or
complete an operation.
