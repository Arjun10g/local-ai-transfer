# Durable action-journal core

This core is a dispatch barrier and recovery ledger, not provider reconciliation. A tool action cannot cross into `execute` until its `dispatching` event has been appended and fsynced. The per-operation chain is:

`prepared -> authorized -> dispatching -> acknowledged -> reconciling -> completed`

Terminal alternatives are `cancelled` and `failed_definitive`; an action whose post-dispatch outcome cannot be proved is `unknown_manual`. Restart changes pre-dispatch `prepared`/`authorized` records to `cancelled` and `dispatching` to `unknown_manual`. It never promotes `acknowledged` or `reconciling` to `completed`.

## Storage contract

- `LAE_ACTION_JOURNAL_DIR` is optional, but every durable action is hidden from model advertisement and fails closed if the journal is absent or unhealthy.
- On POSIX, the directory must already exist at its canonical absolute path, be owned by the current user, and have mode `0700`. The core will not create or chmod an unverified path. Record files are owner-only `0600`, opened with `O_NOFOLLOW` where the platform exposes it, and checked by device/inode/size around bounded I/O. Directory identity is rechecked around mutations.
- Windows is deliberately unavailable. Node mode bits cannot establish Windows ACL or reparse-point safety; enabling Windows requires a native handle-relative, identity-pinned protected store.
- Each JSONL event is at most 64 KiB, each operation has at most 16 events, and at most 256 operations may remain active. Terminal records are durably pruned oldest-first to keep the store bounded; pruning never removes active or unresolved records.
- Torn lines, malformed/extra fields, bad transitions, invalid modes, identity changes, hash-chain mismatches, and limit violations block the journal and therefore block further actions.

Receipts store only tool/risk/side-effect metadata, timestamps, state, authorization kind, resolution, and SHA-256 digests. Request IDs, call IDs, arguments, previews, results, prompts, message bodies, credentials, and tokens are never stored or exported. `arguments_digest` plus `preview_digest` are bound with hashed request/call references and the tool name into `operation_digest`. Exact active binding replay is refused, and an unresolved operation with the same tool and argument digest is refused even across request/call IDs or nondeterministic preview revisions. This is not provider-semantic deduplication: changed arguments still require provider idempotency/reconciliation.

## Classification and completion

Durability follows side effects, not risk tier. File writes, clipboard writes, application/process launches, external/browser navigation and input, Graph mutations, and confirmed Copilot `cloud_inference` egress use the barrier. Pure local/provider/browser reads and browser-session cleanup do not. Unknown T1–T4 side effects fail classification.

Local synchronous actions may reach `completed` after a valid successful tool result. Graph/browser provider acknowledgements remain `reconciling`; the controller exposes `action_completion_unverified` instead of representing them as complete. Provider-specific idempotency keys, completion proof, and reconciliation are required follow-on work. The Copilot egress lane is journaled and crash-bound here, but provider-specific egress auditing remains follow-on work.

## Operator API

The loopback HostServer bearer token protects bounded summary and detail reads:

- `GET /api/action-journal?limit=1..100&state=<state>`
- `GET /api/action-journal/<operation_id>`

`POST /api/action-journal/<operation_id>/resolve` accepts only an explicit operator assertion of `completed` or `failed_definitive`. It appends an audited terminal state and never invokes or replays a provider. `POST .../reconcile` returns HTTP 501 with typed `action_reconciliation_unavailable` until a provider-owned reconciliation adapter exists. Journal request errors are 400, missing records are 404, and unavailable/invalid-transition journal states are 409; response bodies expose only the typed code.

This core alone does not make Graph, browser automation, Copilot, or Windows execution release-ready.
