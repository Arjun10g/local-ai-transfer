# Microsoft Graph action reconciliation

The Graph adapter does not send a generic `Idempotency-Key`: Microsoft Graph
does not define that header as a provider idempotency contract for these
operations. The controller passes a private `internal.journal_binding` after
the durable action-journal dispatch barrier. It contains only the operation
ID and SHA-256 digests; the model-facing call, schema, events, and receipts do
not expose this authority metadata.

The mail draft creator carries a bounded `x-lae-operation` Internet message
header when a journal binding is present. Once device authentication has
provider-attested the canonical account identity, new markers also bind its
SHA-256 fingerprint. On a timeout or incomplete response, the adapter lists a
bounded page of Draft IDs in the signed-in account and performs an explicit GET
with `$select=internetMessageHeaders,...` for every returned ID. It accepts one
exact marker match. Pagination, malformed projections, duplicate IDs, multiple
matches, or absent matches remain
`reconciling` and require manual resolution. The marker match is additionally
required to have the exact normalized subject, plain-text body, content type,
and recipient projection requested by the action; a marker on different draft
content is never treated as completion.

The per-ID GET is not an optimization. Microsoft Graph returns
`internetMessageHeaders` only on a single-message projection, so a
`/messages` collection query cannot carry the custom operation marker no matter
what `$select` asks for; exact proof therefore costs one bounded ID page plus
one GET per candidate. Every seam that needs the marker uses one shared
retrieval — the in-flight draft path, the restart draft path, and the Sent
Items path — so their request budget, projection strictness, and uniqueness
rules cannot diverge. The bound is a single shared constant, and it is a
per-call bound of exactly one list page plus at most 20 exact GETs. The shared
retrieval refuses an absent or malformed marker before spending a request, so
an unmarked message can never satisfy an unmarked expectation.

The in-flight seams additionally derive a deadline from the invoked tool's own
published 10 s `timeout_ms`, minus a safety margin, and refuse to start a proof
request that cannot finish inside the remainder; each request they do issue is
capped to the remaining budget. When the budget runs out the adapter returns a
typed inconclusive result (`draft_proof_budget_exhausted` for
`mail.create_draft`, `sent_proof_budget_exhausted` for `mail.send_draft`)
rather than letting the tool overrun its deadline. This matters because an
overrun surfaces as `tool_timeout`, and the controller records a timed-out
dispatched action as `unknown_manual` — a state no automatic path may touch.
The in-flight seams therefore never throw for an ambiguous outcome: transport
faults, malformed collections, truncation, and budget exhaustion are all
reported as `reconciling` data. Operator cancellation still propagates, because
that is a decision rather than an ambiguity.

After a process restart, only a durably `acknowledged` `mail.create_draft`
record is eligible for automatic reconciliation. After explicit device auth
again verifies the current canonical account, the controller requests a fresh
bounded provider proof. Completion requires exactly one draft whose immutable
custom marker binds the journal operation ID, operation digest, and that
account fingerprint, and whose complete provider projection reconstructs the
persisted arguments digest. The provider returns this proof through a
module-private capability; serialized or cloned proof-shaped objects cannot
complete a record. Concurrent reconciliation calls coalesce. A `dispatching`
record still becomes `unknown_manual` on journal open, while `reconciling`,
`unknown_manual`, all other Graph mutations, legacy two-part markers, and
collections beyond the bounded first page stay manual. At most eight
acknowledged records and 20 draft IDs per record are examined within a
30-second deadline per authentication trigger.

The pass samples the auth epoch and the verified account fingerprint read-only,
before any provider call, and refuses to issue a request unless a live
delegated token is already held with more life (90 s) than the 30 s pass plus
the credential's own 60 s refresh threshold can consume. It
deliberately does not call the provider's `status()`: that call mutates
operator-visible authorization state — a failing credential check clears the
session and revokes the live `microsoft.graph.*` capability grants — and it
reinstalls the session fingerprint on the way out, which would leave the
post-proof epoch guard comparing against a value sampled after the very clear
it exists to detect. A recovery sweep reads identity; it never establishes it.
A `complete()` rejection is counted and returned as a typed metadata-only
`blocked` count with a bounded code; the record keeps its durable state and
stays eligible for a later pass.

Two operational limits are deliberate and are not claimed otherwise. First, the
composition-startup trigger cannot complete anything in production: the
device-code credential holds tokens only in memory, so a freshly restarted host
has no verified account and the pass returns without issuing a single request.
Only the post-authentication trigger can complete a record. Second, the pass is
bounded rather than cancellable: both production call sites invoke it without a
signal, so it is limited by eight candidate records, 20 candidate GETs per
record, a per-request timeout, and a 30-second pass deadline, and host shutdown
invalidates the credential rather than interrupting it.

A record that has reached `reconciling` is not eligible for restart completion
and this slice gives it no resolution path at all: the manual
`POST /api/action-journal/<id>/reconcile` route is HTTP 501, and the operator
`resolve` route and the journal itself refuse Graph mutation records with
`action_journal_provider_proof_required`. Such a record stays active and
visible through the bounded summary/detail reads until a future provider-owned
reconciliation adapter can return it to `acknowledged` or prove a terminal
state. That is the intended posture — ambiguity stays ambiguous — but it does
mean an ambiguous Graph draft accumulates as an unresolved active record rather
than being cleared by an operator.

`mail.mark_read` first reads the requested message state, performs no PATCH
when the requested state is already present, and always verifies with a fresh
GET after a PATCH. A timeout is never retried blindly; a GET can prove the
desired state, otherwise the operation remains reconciling.

Proof timestamps are accepted only as strict, calendar-valid Microsoft Graph
UTC values with a terminal `Z` and 1–12 fractional digits. Non-UTC offsets,
impossible dates, and over-precision remain unproven and fail closed before a
Sent Items baseline or Teams timeout proof is used.

`mail.send_draft` binds the complete bounded normalized draft content,
recipients, subject, and non-empty returned ETag/change key at preview. It does
not claim completion from Graph's `202 Accepted`, which carries no body, so the
Sent Items proof is the only completion evidence this operation has.

Before the send, the adapter records a bounded Sent Items baseline
(`$top=50`, `$select=id,sentDateTime`), which a collection query can answer;
a paginated or malformed baseline refuses the send outright. After the send,
proof uses the shared marker retrieval above: one bounded Sent Items ID page
(`$top=20`, `$orderby=sentDateTime desc`, `$select=id`), then one explicit
`GET /me/messages/{id}` per returned ID with
`$select=id,subject,body,toRecipients,ccRecipients,internetMessageHeaders,changeKey,sentDateTime`.
Completion requires the draft to be absent and exactly one such per-message
projection carrying the same bounded `x-lae-operation` marker already present
on the draft, reconstructing the bound content digest, absent from the
pre-send baseline, and dated inside the bounded deterministic window. Existing
drafts without that marker, missing versions, stale versions, old matches, or
undated matches remain reconciling/manual; no unsupported marker is invented.

That per-message GET replaces a single Sent Items collection query that asked
for `internetMessageHeaders` in `$select` and then required it on every item.
Because Graph populates that property only on a single-message projection, the
old query's own guard rejected every item against a real account:
`unique_sent_item` was unreachable, every send stayed `reconciling`/manual, and
a transport fault on that query escaped as a failed tool result, which the
controller records as `unknown_manual`. Both are repaired. The claim that Graph
withholds the property from collection listings remains unverified against a
live account; it is the same documented rationale the draft path already
depends on, and the repair is the fail-closed direction either way — a message
that does carry the marker on a collection listing still carries it on the
per-message GET.

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
