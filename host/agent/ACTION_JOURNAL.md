# Durable action-journal core

This core is a dispatch barrier and recovery ledger, not provider reconciliation. A tool action cannot cross into `execute` until its `dispatching` event has been appended and fsynced. The per-operation chain is:

`prepared -> authorized -> dispatching -> acknowledged -> reconciling -> completed`

Terminal alternatives are `cancelled` and `failed_definitive`; an action whose post-dispatch outcome cannot be proved is `unknown_manual`. Restart changes pre-dispatch `prepared`/`authorized` records to `cancelled` and `dispatching` to `unknown_manual`. It never promotes `acknowledged` or `reconciling` to `completed`.

## Storage contract

- `LAE_ACTION_JOURNAL_DIR` is optional, but every durable action is hidden from model advertisement and fails closed if the journal is absent or unhealthy.
- Production durable dispatch is currently unavailable on POSIX: Node's built-in promises API does not provide the handle-relative `openat`/`unlinkat` primitives needed to survive an ancestor swap. The pathname implementation is retained only behind an explicit `testOnly` seam. That seam requires a precreated canonical current-user `0700` directory, owner-only `0600` records, `O_NOFOLLOW` where exposed, single-link regular files, and device/inode/size checks around bounded I/O; it never authorizes a model action.
- Windows is deliberately unavailable. Node mode bits cannot establish Windows ACL or reparse-point safety; enabling Windows requires a native handle-relative, identity-pinned protected store.
- Each JSONL event is at most 64 KiB, each operation has at most 16 events, and at most 256 operations may remain active. Terminal records are durably pruned oldest-first to keep the store bounded; pruning never removes active or unresolved records.
- Torn lines, malformed/extra fields, bad transitions, invalid modes, identity changes, hash-chain mismatches, and limit violations block the journal and therefore block further actions.

Receipts store only tool/risk/side-effect metadata, timestamps, state, authorization kind, resolution, and SHA-256 digests. Request IDs, call IDs, arguments, previews, results, prompts, message bodies, credentials, and tokens are never stored or exported. `arguments_digest` plus `preview_digest` are bound with hashed request/call references and the tool name into `operation_digest`. Exact active binding replay is refused, and an unresolved operation with the same tool and argument digest is refused even across request/call IDs or nondeterministic preview revisions. This is not provider-semantic deduplication: changed arguments still require provider idempotency/reconciliation.

## Classification and completion

Risk/effect combinations are an explicit fail-closed contract: T4 is prohibited; T0 is limited to recognized non-action effects; T1 permits recognized reads/launch/navigation; T2 permits bounded local mutations/draft and navigation effects; T3 permits sends, process/cloud egress, and browser input/activation. Unknown effects, missing fields, and tier mismatches fail classification. Durable file/clipboard writes, launches, external/browser navigation and input, Graph mutations, and confirmed Copilot `cloud_inference` egress use the barrier. Pure local/provider/browser reads and browser-session cleanup do not. Host status reports whether the journal is bound to the controller; a split journal injection is never write-ready.

Local synchronous actions may reach `completed` after a valid successful tool result. Graph/browser provider effects remain `reconciling` unless a provider-owned adapter returns bounded digest-only completion evidence; the controller exposes `action_completion_unverified` otherwise. Graph's provider-specific marker/read-proof adapter is documented in `host/providers/MICROSOFT_GRAPH_RECONCILIATION.md`; it does not use a generic idempotency header or in-memory successful replay. The Copilot egress lane is journaled and crash-bound here, but provider-specific egress auditing remains follow-on work.

## Operator API

The loopback HostServer bearer token protects bounded summary and detail reads:

- `GET /api/action-journal?limit=1..100&state=<state>`
- `GET /api/action-journal/<operation_id>`

`POST /api/action-journal/<operation_id>/resolve` accepts only an explicit operator assertion of `completed` or `failed_definitive`. It appends an audited terminal state and never invokes or replays a provider. Graph mutation records are refused there and in the journal itself with typed `action_journal_provider_proof_required`, so an ambiguous Graph action has no operator-assertable outcome in this slice and remains an active unresolved record. The manual `POST .../reconcile` route remains unavailable and returns HTTP 501 with typed `action_reconciliation_unavailable`; the bounded Graph draft recovery path is internal, automatic after canonical device authentication, and does not make a provider callback from this operator endpoint. Journal request errors are 400, missing records are 404, and unavailable/invalid-transition journal states are 409; response bodies expose only the typed code.

This core alone does not make Graph, browser automation, Copilot, or Windows execution release-ready.
