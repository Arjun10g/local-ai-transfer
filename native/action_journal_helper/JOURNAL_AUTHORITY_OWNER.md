# Private JournalAuthorityOwner — phase 1 / supervisor phase 2a

This source-only slice gives the co-located supervisor/helper one
process-lifetime authority owner. `SupervisorState` is the sole process
owner, and `JournalAuthorityOwner::open` is the only construction path:
it acquires one validated `JournalStorageLease`, constructs one
`FixedContainerStore` that borrows that lease, and performs the complete
recovery scan before returning a ready owner.

The pipe server receives a move-only `PipeServerBorrow` ticket. It does not acquire storage,
construct a store, or select a pathname authority. The owner mutex serializes
decoded store applications only; protocol HMAC/nonce handling and all named
pipe reads/writes remain outside that lock. The owner and store are destroyed
after the protocol/session and pipe have settled, so the retained lease remains
valid for every store operation.

Shutdown first CAS-publishes a closing bit in the single admission word, then
takes the owner mutex;
this waits for the one active store application and prevents a new one. The
owner destructor repeats that close-admission operation before destroying the
borrowed store and lease. Lock failures return finite fail-closed statuses;
poison is latched while the application lock is still held. Pipe/process
cancellation is monitored by a separate thread that owns duplicated pipe and
client-process handles and reduces cancellation to a latched atomic/event
probe before it reaches storage. The post-application decision reads that
latched probe rather than reopening the original pipe, so the owner lock never
invokes a pipe API and an original-handle close cannot erase cancellation.

Each application increments the same admission word with CAS before waiting for
the owner mutex. Its lower 32 bits are a bounded count (maximum 4096); the
closing bit and count therefore linearize together, so shutdown cannot observe
a false zero or admit a late caller. Shutdown waits for that count to reach
zero before taking the mutex. Fatal statuses and lock failures latch the
sticky poison state, and all later applications/getters fail closed. If the
destructor cannot prove shutdown, including an unexpected WaitOnAddress
failure, it terminates rather than destroying a live store/lease. WaitOnAddress
is the Windows 8+ aligned 64-bit-address wait primitive: timeouts retry, other
errors are unproven.

Corrupt, conflicting, unknown, poisoned, cancelled, or deadline-expired
startup state returns a finite status and publishes no pipe. Severe storage or
I/O failures poison the owner and prevent subsequent mutation. The existing
bounded canonical protocol and redacted response surface are unchanged.

The private process-dispatch lease uses the same admission CAS and owner mutex
ordering (`admission_cas -> owner_mutex -> store_internals`).
`begin_external_dispatch` is itself an admitted, mutex-protected owner
operation: it checks closing, shutdown, poison, and the global
`unknown_manual` fence before publishing the external-mutation capability. No
owner mutex is held across a process, pipe, or provider wait. A lease is
noncopyable and one-use; terminal entries remain in the bounded owner registry
until the lease destructor detaches them. Thus an owner destructor cannot
destroy an owner while any lease object can still call back; failure to prove
an empty registry fail-stops before store/lease teardown.

Every dispatch transition reloads and then verifies the exact resulting event
state, action, authorization, receipt digest, and event digest. Completion
also requires the exact external receipt/event proof previously acknowledged;
an arbitrary nonzero proof is insufficient. Lost acknowledgement is a
read-only lookup only after the lease crossed `begin_external_dispatch`: the
caller supplies the external proof, which is bound to the canonical
acknowledged receipt digest and retained in the lease without appending an
acknowledge event. Recovery never retries dispatch or acknowledge; a
mismatched proof or divergent readback poisons the owner. `mark_unknown` is
valid from dispatching, acknowledged, or reconciling and is terminal no-replay. The
lease surface has no definitive-failure operation: the shared parser's
reconciling recovery branch cannot be reached through this API; definitive
failure remains pre-dispatch-only. Reload, storage, and readback failures latch
sticky poison, so the owner never returns to ready after an unproved authority
failure.

`ProcessExternalProof` is intentionally opaque and move-only: callers cannot
construct, copy, inspect, or fabricate its operation, external evidence, or
dispatch-generation fields. Its constructor is private and slice 1 provides
no factory or mint path. Slice 2 requires a separately reviewed interface that
independently validates process/job/handle/creation/I/O evidence before
minting this capability. The owner allocates a bounded non-wrapping dispatch
generation at the external-dispatch admission point and requires that
provenance in every acknowledge, lost-ACK recovery, and completion proof.
Until that interface exists, all genuine external-proof success paths remain
unavailable; no arbitrary receipt/event digests can authorize a transition.

The phase-2a source topology provides only a borrowed supervisor bridge; it
does not expose a public storage factory, second client, package path, host
import, registry entry, or activation path. The helper and owner remain
unavailable in production until authenticated bootstrap, compile, transport,
crash, and target evidence are separately accepted.
