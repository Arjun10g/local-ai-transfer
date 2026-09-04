# ADR-0002 — External account and browser-action providers

- **Status:** ACCEPTED_FOR_IMPLEMENTATION; live enablement remains policy-gated
- **Date:** 2026-09-04
- **Owner:** S0
- **Applies to:** TOOL-022 through TOOL-024 and the requested mail, Teams, browser-action, and Copilot workflows

## Decision

External-account and browser-action tools are optional host providers. They are disabled by default, never run inside the native inference engine, never receive credentials from the model, and never become a fallback inference path.

The first provider set is:

1. A Microsoft Graph delegated, read-only adapter for Outlook mail and Microsoft Teams chat.
2. A visible Chromium/Edge CDP adapter using a new temporary profile for bounded HTTPS navigation and link following.
3. A prompt-only GitHub Copilot CLI bridge using an already-installed, explicitly allowlisted `copilot` executable.

All adapters must support deterministic fake transports so unit and integration tests require no company credentials. A mock pass is not a live-provider pass.

## Microsoft Graph provider

- Fixed authority and API origins are configured by the operator and allowlisted; the model never supplies either.
- MVP authentication is delegated user access only. Application-wide mailbox or tenant chat permissions are not accepted.
- Mail listing may use `Mail.ReadBasic`; reading message bodies requires separately approved `Mail.Read`.
- Teams chat reading requires delegated `Chat.Read`.
- Tokens are loaded by a credential source owned by the host and are never included in prompts, tool results, confirmation previews, command lines, or logs.
- The initial provider is read-only. Sending mail, posting Teams messages, deleting, marking read, reacting, and downloading attachments are disabled.
- Only `/me` resources are accepted. Arbitrary user IDs, tenant-wide reads, and Graph URLs returned by the model are rejected.
- Pagination is bounded. Provider-generated next links are accepted only after scheme, origin, API-version, path-family, and query validation.
- HTML bodies are converted to bounded plain text; active content, remote images, attachments, and embedded resources are ignored.

## Browser-action provider

- The browser is visible and uses a newly created temporary user-data directory. The normal browser profile, existing cookies, saved passwords, extensions, and logged-in sessions are not used.
- Initial actions are restricted to starting a session at an approved HTTPS URL, inspecting bounded page metadata/link candidates, following one resolved HTTPS link, and closing the session.
- The provider does not type into forms, upload/download files, invoke custom protocols, execute model-supplied JavaScript, accept permission prompts, or click buttons that may mutate remote state.
- Link following is two phase: inspect resolves the displayed link and destination; confirmation binds the session, page revision, element identifier, and normalized destination; execution rechecks all bindings.
- Redirects and every final destination pass public-host policy. Loopback/private/link-local destinations are denied outside a synthetic test harness bound to an exact random test port.
- Browser/CDP binaries are not downloaded or installed on the target. If an approved compatible browser is absent, the provider is unavailable and ordinary `browser.open_url` remains separate.

## GitHub Copilot CLI bridge

- This is an explicit cloud tool, not the assistant's model backend and not a fallback when local inference fails.
- It is enabled only when an approved, version-checked Copilot CLI binary is already present and the signed-in account/organization permits its use.
- Prompts are provided over stdin to avoid process-list disclosure. The child is spawned directly without a shell, with a minimal environment, bounded output, a timeout, and process-tree cleanup.
- The initial bridge is prompt-only. It grants no Copilot shell, write, URL, MCP, hook, plugin, or broad path permission and never passes `--allow-all`, `--allow-all-tools`, `--allow-all-paths`, or `--allow-all-urls`.
- Workspace or file content may be included only by explicit path selection and a T3 disclosure/confirmation showing destination, byte count, and data categories. The model cannot silently add files.
- Repository mutation remains under the local assistant's separately confirmed filesystem tools.

## Release evidence

Each provider needs all of the following before its capability label becomes `live`:

- strict configuration and unknown-key rejection;
- deterministic fake-transport unit/integration tests;
- timeout, cancellation, size, redirect, malformed-response, auth-failure, and redaction tests;
- an offline test proving no attempted fallback;
- an approved synthetic test-account run on the exact release package;
- permission/consent receipt and organization approval reference;
- no secrets in logs, evidence, package, or Git;
- Windows target evidence for browser/CLI process handling.

Until then, UI/status must report `disabled`, `mock`, or `unverified`; it must not report `live`.

## Rejected alternatives

- Reading Outlook or Teams databases/profiles directly: fragile, overbroad, and likely to expose secrets.
- Application-wide Graph permissions: excessive for a single-user assistant.
- Automating the user's normal browser profile: exposes cookies and authenticated sessions to untrusted page content.
- Playwright/npm download on the target: violates the zero-install portable runtime constraint.
- Unrestricted CDP JavaScript evaluation or coordinate clicking: too broad for the first action adapter.
- Copilot `--allow-all*` flags or unrestricted shell/write tools: duplicates an autonomous agent behind a weaker confirmation boundary.
- Reusing credentials found in `.env`: borrowed operational practices do not authorize borrowed secrets.

