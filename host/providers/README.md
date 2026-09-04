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

An explicitly configured Graph provider may use the Microsoft public-client device-code
flow with `tenant`, `client_id`, and a bounded delegated `scopes` list. The implementation
allowlists only the Microsoft login device-code/token endpoints and the Graph routes above,
uses injected HTTPS transport/clock/sleep hooks for tests, and keeps access tokens in memory
only. Device-code UI callbacks receive only the bounded user code and verification URL; tokens
are never written to configuration, logs, model messages, or UI callback payloads. Transport
bodies, retries, polling, timeouts, cancellation, and response parsing are bounded. After
authentication, `status()` queries `/me` and exposes only a stable in-memory account fingerprint.
The registry's bounded `providerAuthStatus()` control surface can be polled by the host/UI to
obtain the current state and device `userCode`/verification URL; it contains no token or account
identifier. Calling the credential's `clear()` aborts an in-flight device flow and removes its
memory-only cache.
The default registry remains disabled and unconfigured until an operator supplies explicit
settings; no Microsoft account is contacted by tests or fixture launches.
The Entra app registration must be an explicitly approved public client with public-client
flows enabled; this path never accepts a client secret. The scope allowlist is limited to
`User.Read`, `Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `Chat.Read`, `Chat.ReadWrite`, and
`ChatMessage.Send`; `User.Read` is mandatory for opaque account verification, and each Graph
operation is rejected unless its least-privilege delegated scope (or documented higher scope)
was configured and returned by the token response when a scope field is supplied.

`OperatorGrantStore` is an in-memory, provider/account/scope-bound operator
grant. `full_access` can suppress confirmation only for routine draft creation
and mark-read when a matching, unexpired grant exists. Send actions always keep
confirmation. Revocation or expiry is checked at execution and does not widen a
model call; grants are not persisted and model arguments cannot create or alter
them.

`CopilotCliProvider` is a prompt-only cloud bridge. It requires an explicitly
allowlisted executable and version check, snapshots and hashes selected context
at preview, rechecks it at execute, passes the prompt over stdin to bare
`copilot --acp --stdio`, and uses supported no-update/no-custom-instructions/
no-remote/no-MCP/no-tools flags. It passes only a minimal credential-store environment,
uses fixed argv with `shell: false`, and enforces output, timeout, cancellation,
UTF-8, and injected process-tree-kill bounds. It never accepts shell, write,
URL, MCP, plugin, or `--allow-all*` arguments. Its preview discloses the cloud
destination, selected paths, categories, and exact combined byte count. Context
is read only through a `WorkspacePolicy`-backed descriptor-held reader supplied
by `lae-host` from configured workspace roots. Arbitrary context-reader
injection is rejected unless explicitly marked test-only. The bridge remains
disabled until its stdin protocol is independently verified against the pinned
Copilot CLI release.

`BrowserActionProvider` starts an allowlisted browser with a fresh temporary
profile behind the validating public-HTTPS proxy. Page inspection exposes only
bounded visible text and opaque IDs for visible controls. Field entry and
activation require a page-revision-bound preview and user confirmation; static
provider-authored CDP expressions are used, never model selectors or scripts.
Password/file/hidden and sensitive controls are rejected, with bounded cleanup
of the owned process, profile, and proxy.

Live Graph/Copilot authorization, organization approval, Windows process
evidence, and synthetic release-account evidence are not present in this
fixture-only implementation. Until those gates pass, provider status is
`disabled`, `unconfigured`, `mock`, or `unverified`, never `live`.
