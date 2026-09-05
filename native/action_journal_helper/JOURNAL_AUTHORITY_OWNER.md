# Private JournalAuthorityOwner — phase 1

This source-only slice gives the foreground helper one process-lifetime
authority owner. `JournalAuthorityOwner::open` is the only construction path:
it acquires one validated `JournalStorageLease`, constructs one
`FixedContainerStore` that borrows that lease, and performs the complete
recovery scan before returning a ready owner.

The pipe server receives a borrowed owner. It does not acquire storage,
construct a store, or select a pathname authority. The owner mutex serializes
decoded store applications only; protocol HMAC/nonce handling and all named
pipe reads/writes remain outside that lock. The owner and store are destroyed
after the protocol/session and pipe have settled, so the retained lease remains
valid for every store operation.

Corrupt, conflicting, unknown, poisoned, cancelled, or deadline-expired
startup state returns a finite status and publishes no pipe. Severe storage or
I/O failures poison the owner and prevent subsequent mutation. The existing
bounded canonical protocol and redacted response surface are unchanged.

This phase deliberately does not expose a supervisor bridge, public storage
factory, second client, CMake target, package path, host import, registry entry,
or activation path. The helper and owner remain unavailable in production
until authenticated bootstrap, compile, transport, crash, and target evidence
are separately accepted.
