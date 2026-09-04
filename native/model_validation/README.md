# Model validation (runtime slice)

`validate_model_file` accepts only an explicit absolute regular file, rejects
symlinks and `mmproj` names, reads at most the first 1 MiB for GGUF magic/version
and bounded counts, checks the Qwen3.5 architecture metadata, then computes the
full-file SHA-256 and exact size before backend allocation. The release profile
must provide the expected filename, size, and digest; fixture tests may opt into
the bounded zero-tensor GGUF fixture mode only.

The CLI path is:

```text
lae-engine verify-model --model <absolute-Qwen3.5-9B-Q4_K_M.gguf> --size <bytes> --sha256 <64-hex>
```

No model path is accepted by the fixture backend. `serve --backend cpu` runs
validation first and then loads through the pinned llama.cpp adapter. Context is
bounded to 8,192 by default and 16,384 maximum; CPU offload is mandatory and no
Intel promotion is implied by this slice.

Validation keeps the opened stream for bounded header checks and the full hash,
then rechecks pathname equivalence, size, and modification time. Portable
C++17 cannot turn a pathname into an immutable, share-deny handle on every
target, so a concurrent replacement that evades those checks remains an
OS-specific release hardening concern and is reported as
`model_changed_during_validation` when detected.

The real adapter receives the complete ordered API message history and renders
the GGUF-embedded template through the official same-pin llama.cpp Jinja
evaluator (the selectively vendored `common/jinja` sources). It passes
`enable_thinking=false` by default. Revision
`3581ba0cf591b3f772fbb002de0f70e294bc0396` has no public template-kwargs API;
the product therefore cannot override an embedded template's behavior beyond
the official Jinja input and fails closed if that template is absent, cannot be
parsed, or cannot render. No handwritten Qwen template or raw-prompt fallback
is accepted.
