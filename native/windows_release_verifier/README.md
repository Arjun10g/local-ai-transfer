# Inert Windows release-tree verifier source

This directory contains an unlinked `_WIN32` design implementation for exact,
read-only release-tree verification. It is absent from the default/product
CMake graph, the host, package manifests, registries, and launchers. An
OFF-by-default static compile-check target may compile it only for approved
remote Windows evidence and does not register or activate the verifier.

The public entry returns `not_activated` before reading its request. The
compiled-manifest identity, handle-bound Authenticode policy, and cancellable
native-I/O authority gates are hard-coded false. There is no public issuer for
the opaque retained-root capability.

The latent code documents the required Windows handle and identity operations
for review. It has not been compiled or run on Windows. Synchronous native I/O
is not treated as deadline-safe, which is one reason the public gate remains
closed. Production activation requires a separately audited supervisor issuer,
cancellable I/O boundary, exact signed package manifest, build/package wiring,
and Windows fault/race evidence.

This verifier never accepts or opens the external model artifact. It produces
only a fixed-schema metadata receipt with no paths, names, users, SIDs, volume
serials, file IDs, signer subjects, environment values, or file content.
