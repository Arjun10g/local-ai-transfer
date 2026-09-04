# External provider tools (v0.1.0)

The provider layer is host-only and uses Node.js built-ins. `MicrosoftGraphProvider`
accepts an injected delegated credential source and transport; it never discovers
credentials, builds URLs from model input, or contacts Graph unless an operator
constructs it with `enabled: true`. The default `createExternalToolRegistry()`
returns disabled Graph and Copilot tools, so fixture/native launcher runs remain
offline.

Graph requests are constrained to the fixed HTTPS Graph origin and `/me` mail,
`/me/chats` listing, and `/chats/{id}/messages` existing-chat endpoint families.
Mail body reads request Graph's plain-text projection and still sanitize HTML,
entities, scripts, styles, UTF-8, and byte bounds defensively. Results are
projections with no headers, attachments, tokens, or transport details. Writes
require a preview; send-draft previews fetch and bind the current projected
draft identity/content/ETag, then revalidate it before dispatch. A bounded
pre-dispatch ledger records every write attempt, so ambiguous failures cannot
be resent.

`OperatorGrantStore` is an in-memory, provider/account/scope-bound operator
grant. `full_access` can suppress confirmation only for routine draft creation
and mark-read when a matching, unexpired grant exists. Send actions always keep
confirmation. Revocation or expiry is checked at execution and does not widen a
model call; grants are not persisted and model arguments cannot create or alter
them.

`CopilotCliProvider` is a prompt-only cloud bridge. It requires an explicitly
allowlisted executable and version check, snapshots and hashes selected context
at preview, rechecks it at execute, passes the prompt over stdin to bare
`copilot`, and uses supported silent/no-update/no-custom-instructions/no-remote/
no-MCP/no-tools flags. It passes only a minimal credential-store environment,
uses fixed argv with `shell: false`, and enforces output, timeout, cancellation,
UTF-8, and injected process-tree-kill bounds. It never accepts shell, write,
URL, MCP, plugin, or `--allow-all*` arguments. Its preview discloses the cloud
destination, selected paths, categories, and exact combined byte count. Context
is read only through an injected workspace-bound reader.

Live Graph/Copilot authorization, organization approval, Windows process
evidence, and synthetic release-account evidence are not present in this
fixture-only implementation. Until those gates pass, provider status is
`disabled`, `unconfigured`, `mock`, or `unverified`, never `live`.
