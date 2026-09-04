# Error contract v0.1.0

Errors are JSON objects with `error.code`, stable `error.message`, and a
request-scoped `request_id`. Messages contain no prompts, credentials, or file
contents. Clients must branch on `code`, not message text.

| Code | HTTP | Meaning |
|---|---:|---|
| `unauthorized` | 401 | Missing or invalid bearer token |
| `not_found` | 404 | Unknown route or session |
| `method_not_allowed` | 405 | Route exists with another method |
| `invalid_json` | 400 | Malformed JSON body |
| `invalid_request` | 400 | Valid JSON violates request limits/shape |
| `request_too_large` | 413 | Body exceeds 64 KiB |
| `not_ready` | 503 | Backend is not ready |
| `busy` | 409 | Another generation is active |
| `request_cancelled` | 499 | Generation was cancelled |
| `shutdown` | 503 | Engine is stopping/stopped |
| `internal_error` | 500 | Unexpected fixture-engine failure |
| `model_path_not_absolute` | 400 | Model path is not an explicit absolute local path |
| `model_symlink_forbidden` | 400 | Model path resolves through a symlink |
| `model_size_mismatch` | 400 | Model size differs from manifest profile |
| `model_hash_mismatch` | 400 | Model SHA-256 differs from manifest profile |
| `model_mmproj_forbidden` | 400 | Text-only runtime rejects a vision projection |
| `gguf_magic_invalid` | 400 | GGUF magic is invalid |
| `gguf_version_unsupported` | 400 | GGUF version is unsupported |
| `model_architecture_mismatch` | 400 | GGUF architecture differs from Qwen3.5 profile |

The fixture uses only these codes and never silently falls back to another
backend or model.
