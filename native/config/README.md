# Native runtime configuration

`lae-engine serve --config <absolute-json> (--token-file <protected-file> |
--token-stdin)` accepts the
small native launcher file used by the Windows foreground script. The config
must contain an explicit absolute `model_path`; `backend_profile` defaults to
`cpu`, `context_tokens` defaults to 8192, and `model_size_bytes` plus
`model_sha256` must be filled from the approved artifact for a real launch.
The config loader is bounded to 64 KiB and does not contain a download or
provider path. CLI options override config values when supplied.

The launcher token is deliberately not stored in this file. Windows launchers
write the environment secret to the inherited stdin pipe for the foreground
process; POSIX callers may use an owner-only token file. The bearer value is
never an argv argument and must contain 16–512 printable bytes. CPU is the
only accepted runtime profile in this slice; no Intel promotion is implied.
