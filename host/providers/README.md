# External provider tools (v0.1.0)

The provider layer is host-only and uses Node.js built-ins. `MicrosoftGraphProvider`
accepts an injected delegated credential source and transport; it never discovers
credentials, builds URLs from model input, or contacts Graph unless an operator
constructs it with `enabled: true`. The default `createExternalToolRegistry()`
returns no model-visible tools, so fixture/native launcher runs remain offline.
Registry membership is a frozen startup-configuration decision: disabled or
structurally unconfigured providers are omitted, Graph tools are limited to the
configured delegated scopes, and transient sign-in state is not used to change
the bundle. A configured device-code Graph provider therefore remains visible
while signed out so the separate guarded authentication bootstrap can work.
`createExternalToolDefinitionCatalog()` is schema/evaluation-only and must not
be passed to the production controller.

Graph requests are constrained to the fixed HTTPS Graph origin and `/me` mail,
`/me/chats`, `/chats/{id}/messages`, and
`/teams/{id}/channels/{id}/messages` endpoint families. Outlook search uses a
fixed `$search` template and is classified as T2 `search_query` egress with an
exact-query preview and confirmation; Teams text filtering is explicitly local
to the current bounded Graph page. Mail and Teams body reads request selected
fields only and remove HTML, entities, complete or unclosed script/style
content, post-transformation controls, unsafe UTF-8, and over-bound output.
Strict projections reject unknown/malformed fields, duplicate item identities,
invalid UTC dates, unsupported body types, attachments, and oversized pages.
Results contain no headers, remote URLs, attachments, tokens, or transport
details. Writes
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
bodies, retries, polling, timeouts, cancellation, and response parsing are bounded;
Graph JSON must be strict duplicate-free UTF-8 with the expected JSON content type. A
provider next link is accepted only when its origin, path, and fixed query values match
the initiating request. The model receives a one-use opaque, in-memory page cursor rather
than the provider URL, and that cursor is bound to the current account fingerprint and
tool resource. After
authentication, `status()` queries `/me` and exposes only a stable in-memory account fingerprint.
The registry's bounded `providerAuthStatus()` control surface can be polled by the host/UI to
obtain the current state and device `userCode`/verification URL; it contains no token or account
identifier. Calling the credential's `clear()` aborts an in-flight device flow and removes its
memory-only cache.
The default registry remains disabled and unconfigured until an operator supplies explicit
settings; no Microsoft account is contacted by tests or fixture launches.
The Entra app registration must be an explicitly approved public client with public-client
flows enabled; this path never accepts a client secret. The scope allowlist is limited to
`User.Read`, `Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `Chat.Read`, `Chat.ReadWrite`,
`ChatMessage.Send`, `Channel.ReadBasic.All`, and `ChannelMessage.Read.All`; `User.Read` is
mandatory for opaque account verification, and each Graph
operation is rejected unless its least-privilege delegated scope (or documented higher scope)
was configured and returned by the token response when a scope field is supplied.

Every Graph read result carries an adapter-private attestation over its exact serialized
safe projection. The controller verifies that call, tool, and payload digest after envelope
validation. A generic, cloned, echoed, or modified result with a Graph read-tool name is
replaced by the fixed `provider_read_unverified` summary; it cannot project arbitrary
provider/tool JSON into model history.

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

Copilot is omitted from the model bundle unless its executable/version policy
and at least one workspace root are configured. Browser tools are omitted
unless the provider executable and allowlist are configured; mutation tools
remain absent unless their separate safe-actions gate is configured.
On Windows, Copilot and browser-action tools remain omitted even when configured
until their subprocess launches use the approved native identity-pinned,
minimal-environment broker. Only explicitly test-only provider injections may
retain those tools for isolated mocked tests.

Live Graph/Copilot authorization, tenant scope consent, organization approval, Windows process
evidence, and synthetic release-account evidence are not present in this
fixture-only implementation. Until those gates pass, provider status is
`disabled`, `unconfigured`, `mock`, or `unverified`, never `live`.
