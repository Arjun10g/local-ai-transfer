# Host API contract v0.1.0

The host adapter is loopback-only and bearer-authenticated (except liveness
`GET /healthz`). JSON POST routes require `Content-Type: application/json` (an
optional UTF-8 charset is accepted) and exact object bodies; unknown, missing,
or wrong-type fields return a typed `400` response. Bodies and request streams
are bounded by the host configuration. A disconnected chat response aborts its
controller generation.

| Route | Exact body |
|---|---|
| `POST /api/sessions` | optional `session_id` and `reset` |
| `POST /api/chat` | required `session_id`, `request_id`, `message`; optional `mode` |
| `POST /api/cancel` | required `request_id` |
| `POST /api/tool-confirmations/{id}` | required `approved`, `request_id`, `call_id` |
| `POST /api/shutdown` | `{}` |

Failed authentication uses timing-safe token comparison and a bounded per-loopback
rate limit. Header count/size limits use Node's `maxHeadersCount` and
`maxHeaderSize` settings.
