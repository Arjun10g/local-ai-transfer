# Model evidence (no weights)

This tree contains only provenance, controlled-build specifications, manifests,
and compact evaluation fixtures. It must never contain GGUF, Safetensors shards,
model caches, or transfer credentials.

`source-lock/qwen35-9b.source-lock.json` pins the official Hugging Face repository
to an immutable commit and records the API/tree evidence, license/card/template
hashes, tokenizer hashes, and shard metadata observed during Phase 0. The lock is
not an artifact receipt: the controlled Shadeform J1M job must still produce and
hash the text-only reference/Q8/Q4_K_M outputs before an artifact is accepted.

The production manifest schema is strict and rejects placeholders, unknown fields,
wrong architecture/profile, missing hashes, and missing approval receipts. A
generated manifest is an output of J1M and is not checked in with model bytes.
