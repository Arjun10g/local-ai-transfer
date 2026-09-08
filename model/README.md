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

## No-model remote canary

The J1M command planner exposes a `canary` mode for source-only review. It
contains only bounded toolchain and CUDA prerequisite probes and has no model,
source checkout, conversion, quantization, compiler/build, or evaluation
command. Its receipt allowlist is limited to the two probe receipts; salvage
and exact teardown remain mandatory lifecycle obligations. External receipt
salvage is deliberately refused in this source-only slice because a pathname
cannot stay bound to the validated destination across an SCP transfer; a
descriptor-safe, platform-specific transport is required before it can be
enabled. The mode is plan-only until the existing remote gates and explicit
approval/ledger path are extended.

Persisted argv and output receipts reject credential-like values before write.
Private token/SSH key file paths remain usable as handles, but token contents,
bearer/auth headers, URL query credentials, assignment-form secrets, and
`HF_TOKEN` values are never recorded.
