Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# The checked-in tree is a source skeleton and the safe package builder is
# unavailable. Use the bounded repository-side advisory scanner for source
# diagnostics; this target script cannot authorize any package.
throw 'NOT_READY: package verification is unavailable because no handle-relative package builder/verifier has been accepted; no path was accessed'
