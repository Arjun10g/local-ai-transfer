# Native runtime configuration

`lae-engine serve --config <absolute-json> (--token-file <protected-file> |
--token-stdin)` accepts a bounded native launcher file. It must be a regular,
non-link file no larger than 64 KiB and contain a single JSON object. Trailing
JSON whitespace is accepted; duplicate or unknown keys, nested/wrongly typed
values and relative, UNC, device or alternate-stream model paths are rejected.

The allowed keys are `model_path`, `backend_profile`, `context_tokens`,
`gpu_layers`, and `vulkan_device_name`. `model_path` is required and absolute.
The backend is `cpu` or `intel-vulkan`; context is 1–16,384. CPU forbids Vulkan
settings. Vulkan requires an exact device name and 1–99 GPU layers. The model
filename, size, SHA-256, GGUF metadata and tensor inventory are compiled into
the product and are not valid config keys.

The launcher token is deliberately not stored here. Windows launchers remove
the token from the child environment and write it through inherited stdin;
POSIX callers may use an owner-only token file. The bearer value is never an
argv argument and must contain 16–512 printable bytes.
