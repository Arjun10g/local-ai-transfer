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
| `POST /api/operator-grants/{capability}` | required `granted`; `duration_ms` required only when granting (1 minute–8 hours) |
| `POST /api/operator-grants/revoke-all` | `{}` |
| `POST /api/shutdown` | `{}` |
| `GET /api/provider-auth/microsoft_graph` | status only; returns only bounded state, bounded prompt, and `account_verified` boolean |
| `POST /api/provider-auth/microsoft_graph/{start,cancel,clear}` | `{}`; `start` returns `409 provider_unconfigured` unless explicit device auth is configured |

Authenticated `GET /api/operator-grants` lists only host-configured capability,
provider, scope, label, profile, and expiry projections. It never returns account
fingerprints or grant generations. A grant request selects one exact
host-configured binding; callers cannot provide or widen provider, account, root,
application, or scope values. Grants are memory-only, expire automatically, and
are all revoked on host shutdown. The revoke-all route also cancels the active
controller turn.

Failed authentication uses timing-safe token comparison and a bounded per-loopback
rate limit. Header count/size limits use Node's `maxHeadersCount` and
`maxHeaderSize` settings.

The Graph auth status and control routes use the same bearer, loopback Host, and
exact loopback Origin checks as other authenticated routes. `start` is operator
initiated and concurrent starts share one device-code flow; `cancel` aborts it and
`clear` also removes the memory-only token and revokes Graph grants. Public auth
responses are explicitly projected to `state`, bounded `prompt.userCode` and
`prompt.verificationUri`, and `account_verified`; they contain no access token,
device code, tenant, client ID, scopes, account fingerprint, or provider fields.
The boolean is provider-issued only after the device-auth provider has derived
and matched its canonical account identity; arbitrary or malformed fingerprints
cannot make it true.

The native loopback transport accepts only numeric `127.0.0.1` client
endpoints. It requires HTTP/1.1, a loopback `Host`, bounded unique headers,
`Content-Length` on JSON POSTs, and rejects transfer encoding. Native request
headers are limited to 16 KiB/64 fields and bodies to 64 KiB; socket reads and
writes time out after five seconds. Native outbound JSON is bounded to 64 KiB,
non-stream JSON responses to 4 MiB, and SSE responses to 4 MiB, 4096 events,
and 256 KiB per line. The native bearer is supplied through a
protected token file (`--token-file`) and is never accepted on the command
line.
