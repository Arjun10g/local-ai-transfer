# Inert Windows supervisor authority

This directory is a deliberately unlinked `_WIN32` reference boundary. It is
not in `native/CMakeLists.txt`, the launcher, the Node host, the package
allowlist, or the production registry. `authority.hpp` exposes only refusal
and redacted loopback metadata; it has no public issuer, capability factory,
child-launch API, or caller-supplied path/identity API.

The source records the required future control flow so it can be independently
reviewed before integration. Trust gates are compile-time false and are
checked before any Windows process, token, pipe, job, module, or filesystem
operation. The latent supervisor creates one foreground kill-on-close root
Job and keeps direct root membership in a bounded stable registry; nested jobs
are refused until policy is proven, and
requires bounded terminate/wait/reap plus an empty-job proof before closure.
There is no leader-only fallback, `INFINITE` wait, shell/search/PATH lookup,
or detached process path.

Bootstrap accepts only inherited anonymous pipe handles and binds them to a
parent/pipe-server PID, creation time, token session/SID digest, image
identity, and retained regular non-reparse file identity. Both endpoints have
an explicit private direction and must report the expected server/client
relationship; a caller-supplied pathname or identity is not authority. The
future private issuer would generate per-launch/session opaque IDs, nonces,
and MACs using CNG while retaining its signing key privately; consumers receive
only a verifier capability. The MAC covers the complete scope, session,
expiry, operation ID, operation/argument/preview digests, and nonce. A bounded
replay set rejects nonce reuse, partial random issuance zeroizes immediately,
and these values never enter argv, environment, disk, logs, or receipts. Only
bounded redacted metadata can cross a loopback boundary. A durable
ActionJournal authority is a hard prerequisite: its start record must be
durable before dispatch and its terminal record durable before acknowledgement.

The retained executing-image section identity gate is false, so the pathname
reopen code cannot authorize a bootstrap. The reference implementation is not a proof of Windows API behavior. It has
no compile, target, Authenticode, package, process-tree, cancellable-I/O, or
live evidence. Production and target availability remain **false**.
