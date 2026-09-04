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

The current adapter receives the API's extracted message content as a raw
prompt. It does not yet apply the approved Qwen3.5 GGUF chat template or
`enable_thinking` control, so this raw-prompt mode is non-accepted for Phase 2
quality/parity and is retained only as an adapter/lifecycle bring-up path.
