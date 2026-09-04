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

The fixture uses only these codes and never silently falls back to another
backend or model.
