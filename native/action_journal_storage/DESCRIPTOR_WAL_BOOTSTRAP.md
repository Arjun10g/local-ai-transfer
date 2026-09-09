# Dormant Windows DescriptorActionJournal bootstrap

This source-only slice adds secure Win32 acquisition and handoff primitives
for the append-only v2 `DescriptorActionJournal` WAL. It is not linked into the
host, supervisor, helper, product, installer, or package. All production and
activation gates remain false.

The fixed leaf is `action-journal-v2.wal` beneath an already-existing private
directory. Acquisition reuses the storage boundary's canonical local fixed
NTFS, retained no-follow ancestor, owner-only protected DACL, single-link,
non-delete-pending, stable volume/file identity, and final-path checks. Create
uses `CREATE_NEW` with the private security descriptor in the create call.
Open requires a separately trusted volume-serial/file-ID tuple; identity read
from the candidate path cannot authorize its own reopen.

The read/write leaf handle denies write and delete sharing for its complete
lease lifetime. That share-mode lease is the exclusive writer boundary; a
second writer cannot predate or outlive successful acquisition. The retained
source handle is non-inheritable. At most one inheritable same-access duplicate
can be prepared, and the file identity, DACL, ancestor identity, bounded size,
and WAL prefix are rechecked before duplication. No handle value is included
in a receipt, log, command line, or environment variable.

Create writes the exact 48-byte
`{"format":"lae-action-journal-wal","version":2}\n` header through the retained
handle, flushes it, and reads it back. Failure preserves the file for explicit
recovery. Trusted reopen accepts an empty file or an exact strict prefix of
that header because the existing descriptor-relative journal initializer can
conservatively truncate that prefix, flush, and republish the header. Any
other header bytes and any file over 32 MiB fail closed. Complete frame replay,
checksum validation, exact committed-frame recovery, and append flush/readback
remain owned by `DescriptorActionJournal`; this native boundary neither parses
nor repairs frames. Its flush is bounded OS evidence, not a physical-media or
power-loss guarantee.

## Deliberately missing bridge and gates

A Win32 `HANDLE` numeric value is not a Microsoft C runtime file descriptor,
and Node's `fs` APIs consume the latter. The duplicate therefore must not be
serialized as `LAE_ACTION_JOURNAL_FD`. A future reviewed native child bridge
must inherit only this duplicate through an explicit handle allowlist, convert
it in the child to a readable/writable CRT descriptor, prove ownership transfer
and acknowledgement, and close every failure path. No `CreateProcess*`, helper
or supervisor wiring, package entry, host import, environment publication, or
activation switch is present here.

Remote MSVC compile/static analysis and exact-target Windows tests must still
prove DACL behavior, sharing exclusion, inheritance allowlisting, CRT/Node fd
conversion, short-write/flush/restart behavior, filter-driver behavior, and
power-loss recovery before integration or any readiness claim. A durable
external identity and anti-rollback anchor is also absent: WAL digests detect
corruption but cannot authenticate or reject a self-consistent older image.
