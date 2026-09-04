# Model validation

The product binary accepts exactly one compiled artifact identity:
`Qwen3.5-9B-Q4_K_M.gguf`, 5,629,109,088 bytes, SHA-256
`c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`.
Callers cannot provide or override those values. The verification command is:

```text
lae-engine verify-model --model <absolute-Qwen3.5-9B-Q4_K_M.gguf>
```

Before hashing or backend allocation, validation reads the bounded GGUF v3
metadata and all 427 tensor descriptors. It checks the fixed Qwen3.5 model,
quantization, tokenizer and embedded-template profile; accepted tensor types
and counts; required sentinel tensor shapes; dimensions; quantization block
sizes; alignment and zero padding; integer overflow; offset bounds; overlaps;
and exact tensor-payload coverage. Symlinks, device/UNC/alternate-stream paths,
vision/mmproj tensors, shallow headers, truncation and trailing undeclared
payload all fail closed. The 5.6 GB payload is streamed for SHA-256 and is not
allocated as one buffer.

Small synthetic identities exist only in the separately compiled validator
test executable behind `LAE_ENABLE_TEST_MODEL_IDENTITY`. That API is absent
from `lae-engine`, including fixture builds, and cannot be selected through a
product config or command-line option.

Validation keeps the opened stream for structure checks and the full hash,
then rechecks pathname equivalence, size, and modification time. Portable
C++17 cannot turn a pathname into an immutable, share-deny handle on every
target, so a concurrent replacement that evades those checks remains an
OS-specific acceptance concern and is reported as
`model_changed_during_validation` when detected.

The real adapter receives the complete ordered API message history and renders
the fixed artifact's embedded template through the official same-pin llama.cpp
Jinja evaluator. It passes `enable_thinking=false` by default. Revision
`3581ba0cf591b3f772fbb002de0f70e294bc0396` has no public template-kwargs API;
the product cannot override an embedded template's behavior beyond the
official Jinja input and fails closed if the template is absent or invalid.
