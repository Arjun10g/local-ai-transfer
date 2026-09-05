# Windows clean-machine acceptance — NOT_READY

The acceptance producer refuses before reading package, model, receipt, or
account state; creating output; starting a process; opening a browser; issuing
HTTP; or invoking any live provider. No switch or operator attestation enables
it.

The reported target is a Dell Windows x64 laptop with an Intel Core Ultra 7
vPro Enterprise CPU, Intel integrated Graphics driver `32.0.101.8247`, 32 GiB
DDR5-5600 memory, and board `039NNG` revision `A00`. Those values are not an
accepted hardware receipt.

`python -m qa.windows_acceptance.verify <receipt>` remains a bounded diagnostic
for historical receipt shapes. It always includes the current producer/launcher
blocker, sets `core_ready` and `full_access_ready` false, and returns
`NOT_READY`.

Do not use real mail, Teams, files, repositories, credentials, browser targets,
or model data to work around this state. Acceptance can resume only after the
safe package builder/verifier, native identity-pinned launcher, bounded native
hardware collector, and remotely built Windows artifacts pass independent
review.
