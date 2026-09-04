# Pinned native backend source

The `llama.cpp/` directory is a pruned source snapshot of upstream
`ggml-org/llama.cpp` at immutable commit
`3581ba0cf591b3f772fbb002de0f70e294bc0396` (detached source checkout, not a
Git submodule). It contains the public headers, runtime `src/` model support,
CPU ggml backend, the complete official `GGML_VULKAN` source/shader generator
closure, CMake modules, and the official same-pin `common/jinja` template
evaluator with its Unicode/nlohmann dependencies. Tests, examples, tools,
conversion utilities, documentation, and nested VCS metadata remain excluded
from this portable runtime bundle. The Vulkan closure is separately locked by
`ggml-vulkan-source-lock.json` and contains 175 files (including the upstream
license), each with an exact SHA-256 at the pinned revision.

The product build enables this source only with `LAE_ENABLE_LLAMA_CPP=ON` and
disables upstream examples/tools/server/UI/common/test targets. CPU remains the
mandatory backend. No model weights or runtime download path is included.

Upstream license: MIT, preserved at `llama.cpp/LICENSE`.
The vendored nlohmann/json headers are version 3.12.0 and carry their own MIT
SPDX notice (`Niels Lohmann`) in each header.

The same-pin upstream source archive used to import the Jinja evaluator has
SHA-256 `707a57b621283079b978cfb39957f1af42cce06a7f9fcb60573bf87a09ec3de7`.
For the checked-in added paths, the sorted per-file SHA-256 manifest digest is
`d56a578e4d9be93e95ba9f358f3a15dd8d3c4cbb949159d999137473cfbdbb01`.
