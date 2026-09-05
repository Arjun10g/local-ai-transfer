# Windows process broker contract v1.0.0 (inactive skeleton)

This contract describes a future inherited-pipe boundary between the Node host
and one native Windows broker. It does not activate a product capability. The
broker source is absent from the native build graph and package allowlist, its
compiled manifest trust anchor is empty by default, and all existing Windows
tool-advertisement exclusions remain authoritative.

Each message is encoded as a four-byte unsigned big-endian length followed by
exactly that many UTF-8 JSON bytes. Request frames are limited to 65,536 bytes;
response frames to 1,048,576 bytes. Duplicate JSON keys and trailing JSON data
are invalid even though JSON Schema cannot express those lexical constraints.
The broker reads inherited stdin and writes inherited stdout. TCP, shell text,
and a generic command endpoint are not part of this protocol.

An `invoke` request selects a manifest-pinned logical `action_id`. The request
cannot select an executable, manifest, argv array, cwd, environment, output
path, or shell. Its `arguments` keys must exactly match that action's typed
parameter declarations. Literal argv pieces and parameter placement are fixed
by the authenticated manifest. A parameter may be placed into one argv element,
bounded stdin, or the clipboard, never interpolated into a shell string.

The immediate response to a valid invocation has status `accepted`. A later
response with the same request ID contains the terminal result. One action may
run at a time. A `cancel` frame targets that exact request ID and causes full Job
termination and bounded reaping. Parent EOF has the same cancellation effect.

Receipts intentionally exclude paths, argv values, environment, stdout/stderr,
clipboard text, Copilot prompts, URLs, and other content. Process output is
returned separately as bounded base64 only to the authenticated parent channel.

The checked-in manifest is `mode: fixture`. The product parser rejects it.
Product mode requires a separate reviewed manifest whose exact SHA-256 is
compiled into a remotely built and signed broker and whose executable and
directory identities are captured on the accepted Windows target.
