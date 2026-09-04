# Pinned native backend source

The `llama.cpp/` directory is a pruned source snapshot of upstream
`ggml-org/llama.cpp` at immutable commit
`3581ba0cf591b3f772fbb002de0f70e294bc0396` (detached source checkout, not a
Git submodule). It contains the public headers, runtime `src/` model support,
CPU ggml backend, CMake modules, and the official same-pin `common/jinja`
template evaluator with its Unicode/nlohmann dependencies. Tests, examples,
tools, conversion utilities, non-CPU backends, documentation, and nested VCS
metadata are intentionally excluded from this portable runtime bundle.

The product build enables this source only with `LAE_ENABLE_LLAMA_CPP=ON` and
disables upstream examples/tools/server/UI/common/test targets. CPU remains the
mandatory backend. No model weights or runtime download path is included.

Upstream license: MIT, preserved at `llama.cpp/LICENSE`.
The vendored nlohmann/json headers are version 3.12.0 and carry their own MIT
SPDX notice (`Niels Lohmann`) in each header.

The deterministic tar receipt for the exact pruned path set at that revision is
SHA-256 `33e10f87d4bbfff20f9d3f9765717205a284e5850e52cbdee5508fe314e2c891`.

The same-pin upstream source archive used to import the Jinja evaluator has
SHA-256 `707a57b621283079b978cfb39957f1af42cce06a7f9fcb60573bf87a09ec3de7`.
For the checked-in added paths, the sorted per-file SHA-256 manifest digest is
`d56a578e4d9be93e95ba9f358f3a15dd8d3c4cbb949159d999137473cfbdbb01`.
