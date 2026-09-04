# Native runtime configuration

`lae-engine serve --config <absolute-json> --token <launch-token>` accepts the
small native launcher file used by the Windows foreground script. The config
must contain an explicit absolute `model_path`; `backend_profile` defaults to
`cpu`, `context_tokens` defaults to 8192, and `model_size_bytes` plus
`model_sha256` must be filled from the approved artifact for a real launch.
The config loader is bounded to 64 KiB and does not contain a download or
provider path. CLI options override config values when supplied.

The launcher token is deliberately not stored in this file. Windows launchers
must provide `LAE_ENGINE_TOKEN` through their protected environment. CPU is
the only accepted runtime profile in this slice; no Intel promotion is implied.
