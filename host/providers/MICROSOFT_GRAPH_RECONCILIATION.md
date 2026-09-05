# Microsoft Graph action reconciliation

The Graph adapter does not send a generic `Idempotency-Key`: Microsoft Graph
does not define that header as a provider idempotency contract for these
operations. The controller passes a private `internal.journal_binding` after
the durable action-journal dispatch barrier. It contains only the operation
ID and SHA-256 digests; the model-facing call, schema, events, and receipts do
not expose this authority metadata.

The mail draft creator carries a bounded `x-lae-operation` Internet message
header when a journal binding is present. On a timeout or incomplete response,
the adapter searches only the signed-in account's bounded Drafts collection
and accepts one exact marker match. Multiple or absent matches remain
`reconciling` and require manual resolution. The marker match is additionally
required to have the exact normalized subject, plain-text body, content type,
and recipient projection requested by the action; a marker on different draft
content is never treated as completion.

`mail.mark_read` first reads the requested message state, performs no PATCH
when the requested state is already present, and always verifies with a fresh
GET after a PATCH. A timeout is never retried blindly; a GET can prove the
desired state, otherwise the operation remains reconciling.

`mail.send_draft` binds the complete bounded normalized draft content,
recipients, subject, and non-empty returned ETag/change key at preview. It does
not claim completion from Graph's `202 Accepted`. Completion requires the draft
to be absent and exactly one new matching Sent Items projection carrying the
same bounded `x-lae-operation` marker already present on the draft, plus a
post-snapshot `sentDateTime` inside the bounded deterministic window. Existing
drafts without that marker, missing versions, stale versions, old matches, or
undated matches remain reconciling/manual; no unsupported marker is invented.
Teams sends do not use an idempotency header or automatic retry. They complete
only from a validated `201 Created` resource. Timeout/list-message proof stays
manual in this slice because a trusted signed-in sender identity is not
available; content/time alone is insufficient against a concurrent identical
message.

Official Microsoft Graph references (accessed 2026-09-05):

- [Create message](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0) documents JSON draft creation and custom `x-` Internet message headers.
- [message: send](https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0) documents `202 Accepted` with no response body and Sent Items processing.
- [Update message](https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0) documents `PATCH /me/messages/{id}` and the `Mail.ReadWrite` requirement for `isRead`.
- [Send message in a chat](https://learn.microsoft.com/en-us/graph/api/chat-post-messages?view=graph-rest-1.0) documents `POST /chats/{chat-id}/messages` and `201 Created`.
- [Chat message resource operation](https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0) documents the chat-message body and response shape.

The production action journal remains fail-closed until its handle-relative
durable store is available. These adapters therefore have no claim of live
account readiness in this slice; tests use injected transports and synthetic
credentials only.
