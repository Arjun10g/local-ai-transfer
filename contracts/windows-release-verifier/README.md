# Windows release-tree verifier contract v0.1.0

This is a source-only refusal boundary and review fixture. It defines how a
future native Windows verifier would prove the identity of an already-built,
already-extracted portable release tree. It does not build, extract, install,
launch, sign, or activate that tree.

The public native entry refuses before dereferencing its request or touching a
path, handle, directory, or file. Its compiled-manifest, Authenticode-policy,
and cancellable-native-I/O trust anchors are immutable `false`. Changing a
constant is not an activation procedure: a future slice must supply an
authenticated supervisor-issued root capability, reviewed signer identities,
cancellable I/O, packaging integration, compilation evidence, and exact
Windows tests.

The source manifest fixture mirrors the finite file names in the current
`release/windows/RELEASE_MANIFEST.json` source skeleton and pins their source
snapshot sizes and hashes. That correspondence is advisory only. The fixture
is not a final package manifest, carries no trusted signer identity, and cannot
produce `VERIFIED` through the public API.

Model weights are deliberately outside this contract. The verifier accepts no
model path or model handle, and the receipt contains no model identity. The
separate model verifier remains the only authority for the external GGUF.

The receipt is compact metadata only. It never includes a root path, file
name, user/SID, volume serial, file ID, certificate subject, environment value,
or file/model content.
