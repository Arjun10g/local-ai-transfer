# Windows artifact provenance handoff v1

This contract carries a small, signed provenance statement from the approved
HF/Shadeform artifact workflow to a future Windows release verifier. It carries
identities and receipt digests only: no model bytes, paths, URLs, credentials,
or receipt content.

The handoff validator checks exact schema, Qwen3.5-9B text-only Q4_K_M identity,
receipt cross-reference digests, release exclusion of model bytes, and a
canonical detached-payload digest. Signature verification remains an external
approved trust-anchor operation. Without an injected trusted verifier, the
public CLI always returns `REFUSED_NOT_ACTIVATED`; a `verified` field in input
cannot activate it. This source-only contract does not package, copy, hash, or
load model weights and does not activate the Windows broker or launcher.
