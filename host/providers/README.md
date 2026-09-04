# External provider tools (v0.1.0)

The provider layer is host-only and uses Node.js built-ins. `MicrosoftGraphProvider`
accepts an injected delegated credential source and transport; it never discovers
credentials, builds URLs from model input, or contacts Graph unless an operator
constructs it with `enabled: true`. The default `createExternalToolRegistry()`
returns disabled Graph and Copilot tools, so fixture/native launcher runs remain
offline.

Graph requests are constrained to the configured HTTPS Graph origin and `/me`
mail or existing-chat endpoint families. Results are projections with bounded
text and no headers, attachments, tokens, or transport details. Writes require a
proposal created by `preview`, are revalidated against that proposal, and use a
call/proposal-derived idempotency key. Replaying the same call returns a typed
`replayed` outcome without a second transport request.

`OperatorGrantStore` is an in-memory, provider/account/scope-bound operator
grant. `full_access` can suppress confirmation only for routine draft creation
and mark-read when a matching, unexpired grant exists. Send actions always keep
confirmation. Revocation or expiry is checked at execution and does not widen a
model call; grants are not persisted and model arguments cannot create or alter
them.

`CopilotCliProvider` is a prompt-only cloud bridge. It requires an explicitly
allowlisted executable and version check, sends the prompt/context over stdin,
passes only a minimal environment, uses fixed argv with `shell: false`, and
enforces output, timeout, and cancellation bounds. It never accepts shell,
write, URL, MCP, plugin, or `--allow-all*` arguments. Its preview discloses the
cloud destination, selected paths, categories, and byte estimate. Context is
read only through an injected workspace-bound reader.

Live Graph/Copilot authorization, organization approval, Windows process
evidence, and synthetic release-account evidence are not present in this
fixture-only implementation. Until those gates pass, provider status is
`disabled`, `unconfigured`, `mock`, or `unverified`, never `live`.
