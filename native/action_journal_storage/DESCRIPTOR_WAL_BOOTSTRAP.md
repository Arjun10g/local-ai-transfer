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
from the candidate path cannot authorize its own reopen. Both `CreateFileW`
calls added by this slice set `SECURITY_SQOS_PRESENT | SECURITY_ANONYMOUS`, so
a path-validation regression that let a named-pipe or UNC target through could
still not be used to impersonate this token. The shared ancestor and volume
helpers reused from the merged v1 container path are deliberately left
unchanged here; altering them would change already-reviewed behaviour that
this slice produces no evidence for.

The read/write leaf handle denies write and delete sharing for its complete
lease lifetime. That share-mode lease is the exclusive writer boundary against
every other process; a second process cannot predate or outlive successful
acquisition. It is not a boundary between the lease holder and a child that
receives the duplicate: `DuplicateHandle` produces a second handle to the same
file object, so parent and child share one file object and therefore one file
position. After a handoff the child owns that position. The parent must not
perform I/O through its lease afterwards, which `DescriptorWalLease` enforces
by refusing every I/O-performing operation with `handoff_already_transferred`
once `handoff_transferred()` is true, and the child must use positional reads
and writes only. `DescriptorActionJournal` already does exactly that.

The retained source handle is non-inheritable, and that is verified with
`GetHandleInformation` at acquisition and again immediately before
duplication; a set flag is refused with `source_handle_inheritable`. At most
one duplicate can be prepared. The file identity, DACL, ancestor identity and
DACL, bounded size, observed size, final path, volume policy, and WAL prefix
are rechecked before duplication, and that recheck set is exactly the
acquisition check set, with each refusal carrying its own status so a DACL
regression, a size change, a torn prefix, a path swap, a volume change, and an
ancestor swap remain distinguishable. A second `prepare_inheritable_handoff`
is refused with `handoff_already_transferred` before anything is reset, so it
cannot destroy the single handoff a first call produced. No handle value is
included in a receipt, log, command line, or environment variable.

## Inheritance is armed explicitly, never ambiently

The duplicate is created with `bInheritHandle = FALSE` and with explicit
`GENERIC_READ | GENERIC_WRITE` access, so it is born non-inheritable and
carries neither `DELETE` nor `READ_CONTROL` across a future process boundary.
`DescriptorWalHandoff::arm_inheritance()` and `revoke_inheritance()` are
separate, idempotent, typed-status steps over
`SetHandleInformation(HANDLE_FLAG_INHERIT, ...)`, and each verifies the
observed kernel flag rather than trusting the API return.

Hard precondition on any future launcher. Every clause is mandatory; a
launcher that cannot satisfy all of them must close the duplicate instead of
launching.

1. Arm inheritance only immediately before the single `CreateProcess*` call
   that must receive this handle. The duplicate must never be inheritable
   while any unrelated child is created.
2. That `CreateProcess*` call must pass an explicit handle allowlist through a
   `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` thread-attribute entry containing
   exactly this handle, so no other ambient inheritable handle can leak and
   this handle is scoped to that one child. `bInheritHandles = TRUE` without
   that allowlist is forbidden.
3. Revoke inheritance, or close the duplicate, immediately after the child is
   created and on every failure path, including a failed `CreateProcess*` and
   a failed acknowledgement.
4. `take_handle()` is refused while inheritance is armed, so a handle that
   leaves RAII ownership is never ambiently inheritable.

This is the storage-side half of the property the supervisor launch contract
models as `no_ambient_handle_inheritance`. Without it, any future
`CreateProcess*` in this process with `bInheritHandles = TRUE` and no handle
list would hand an unrelated child a writable handle to the dispatch-barrier
journal, whose hash chain is integrity-only and not authenticity-bearing.

## Publication, discard, and what is never repaired

Create writes the exact 48-byte
`{"format":"lae-action-journal-wal","version":2}\n` header through the retained
handle, flushes it, reads it back, and asserts the published size is exactly
48 rather than assuming it. Its flush is bounded OS evidence, not a
physical-media or power-loss guarantee.

If any later check or publication step fails on the create path, the leaf this
call just created with `CREATE_NEW` is discarded: `DELETE` access is requested
only when this call creates the leaf, and the disposition is set through the
handle this call already owns, so no path metadata is trusted a second time.
Without that, a transient I/O failure during genesis would wedge the fixed
leaf name permanently, because `kCreateNew` would then always return
`already_exists` while `kOpenExisting` could not be satisfied by a caller that
never learned the trusted identity tuple. Deleting a private artifact that
this call created and never published is not a WAL repair: no frame, no
foreign byte, and no external observer can exist yet.

The reopen path never requests `DELETE`, never deletes, never truncates, and
never writes. Failure on the reopen path preserves the file exactly as found,
for explicit recovery. Trusted reopen accepts an empty file or an exact strict
prefix of that header because the existing descriptor-relative journal
initializer can conservatively truncate that prefix, flush, and republish the
header. Any other header bytes and any file over 32 MiB fail closed. Complete
frame replay, checksum validation, exact committed-frame recovery, and append
flush/readback remain owned by `DescriptorActionJournal`; this native boundary
neither parses nor repairs frames.

## Deliberately missing bridge and gates

A Win32 `HANDLE` numeric value is not a Microsoft C runtime file descriptor,
and Node's `fs` APIs consume the latter. The duplicate therefore must not be
serialized as `LAE_ACTION_JOURNAL_FD`. A future reviewed native child bridge
must inherit only this duplicate through an explicit handle allowlist, convert
it in the child to a readable/writable CRT descriptor, prove ownership transfer
and acknowledgement, and close every failure path. No `CreateProcess*`, helper
or supervisor wiring, package entry, host import, environment publication, or
activation switch is present here.

Known and accepted limits of the reused boundary. `acquire_directories`
verifies the owner-only protected DACL on the deepest directory only;
intermediate ancestors get attribute, final-path, and identity checks while
being held with deny-write/deny-delete sharing and `FILE_FLAG_OPEN_REPARSE_POINT`.
That is pre-existing merged behaviour shared with the v1 container lease and
is out of scope here; changing it would alter a reviewed path this slice
produces no evidence for. Reopen trust also rests on a
`{volume_serial, file_id}` tuple, which detects accidental divergence and
casual swapping but is not an authenticity anchor: volume serials are settable
and NTFS file IDs are reused after deletion.

Remote MSVC compile/static analysis and exact-target Windows tests must still
prove DACL behavior, sharing exclusion, inheritance allowlisting, CRT/Node fd
conversion, short-write/flush/restart behavior, filter-driver behavior, the
create-failure discard, and power-loss recovery before integration or any
readiness claim. A durable external identity and anti-rollback anchor is also
absent: WAL digests detect corruption but cannot authenticate or reject a
self-consistent older image.
