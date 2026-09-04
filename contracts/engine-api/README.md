# Engine API contract v0.1.0

This is the fixture-era, repository-owned engine contract. It is deliberately a
small OpenAI-compatible subset and is independent of llama.cpp/ggml types.

## Lifecycle

The engine reports exactly one state from `NEW`, `VERIFYING_MODEL`,
`LOADING_MODEL`, `WARMING`, `READY`, `BUSY`, `DEGRADED`, `STOPPING`, `STOPPED`,
or `FAILED`. A fixture engine enters `WARMING` and then `READY` after backend
initialization. `readyz` returns HTTP 200 only in `READY` or `BUSY`.

## HTTP

The server binds only to `127.0.0.1`; `--port 0` chooses an ephemeral port.
`GET /healthz` is unauthenticated and reports process/lifecycle liveness.
Every other route requires `Authorization: Bearer <launch-token>`.
Requests are capped at 64 KiB and one active generation is allowed per engine.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | no | Liveness and lifecycle state |
| GET | `/readyz` | yes | Model/backend readiness |
| GET | `/version` | yes | API and engine versions |
| GET | `/build-info` | yes | Reproducible build/backend identity |
| GET | `/probe` | yes | Read-only host/build probe |
| GET | `/metrics` | yes | Metadata-only counters |
| POST | `/v1/sessions` | yes | Create an opaque session |
| DELETE | `/v1/sessions/{id}` | yes | Delete a session |
| POST | `/v1/chat/completions` | yes | Deterministic fixture stream or JSON |
| POST | `/v1/cancel/{request_id}` | yes | Request cancellation |

Malformed JSON, unknown routes, missing authentication, oversized requests, and
unknown sessions return the typed error envelope in `../error-codes`.

## Chat request

```json
{"model":"fixture","session_id":"optional","messages":[{"role":"user","content":"hello"}],"stream":true,"max_tokens":8}
```

`stream` defaults to false. `max_tokens` is an integer from 1 through 64.
The fixture ignores prompt text but retains the session boundary. A streaming
response is `text/event-stream`, one JSON chunk per line (`data: ...\n\n`), and
terminates with `data: [DONE]\n\n`. Every response includes `X-Request-Id`.

## Cancellation and atomic state

`POST /v1/cancel/{request_id}` is idempotent. Cancellation is observable as a
`cancelled` finish reason and typed `request_cancelled` error where no stream
chunk can be committed after cancellation. Session state is committed only
after a generation completes; cancellation leaves the previous committed
snapshot unchanged. A session has at most one active generation.

## Session response

```json
{"id":"sess-00000001","object":"session","state_version":1}
```

Session IDs are opaque and must not be interpreted by clients.
