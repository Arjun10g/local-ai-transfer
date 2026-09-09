# Windows artifact provenance handoff v1

This contract carries a small, signed provenance statement from the approved
HF/Shadeform artifact workflow to a future Windows release verifier. It carries
identities and receipt digests only: no model bytes, paths, URLs, credentials,
or receipt content.

The JSON Schema checks the exact shape, approved artifact constants, nonzero
digest form, and Ed25519 encoding length. The procedural validator additionally
enforces cross-field relationships: the evidence artifact-manifest digest must
equal the artifact manifest digest, the payload digest must equal the canonical
payload (which binds signature algorithm and key ID), and the private verifier
callback receives that canonical payload plus the detached signature. Signature
verification remains an external approved trust-anchor operation. Without an
injected trusted verifier, the public CLI always returns
`REFUSED_NOT_ACTIVATED`; a `verified` field in input cannot activate it. This
source-only contract does not package, copy, hash, or load model weights and
does not activate the Windows broker or launcher.
