# Phase 3 local tools (fixture-driven)

These modules run in the Node.js 24 host with built-in modules only. The model
does not receive executable paths, risk tiers, or policy configuration.

## Filesystem

`WorkspacePolicy` canonicalizes each configured root and every existing target
with `realpath`, then checks containment using `path.relative`. Tool paths are
relative to a configured workspace ID. Absolute paths, `..` segments, UNC and
drive paths, alternate data streams (`:`), NUL bytes, and Windows reserved
names are rejected. Symlinks/junctions escaping a root are rejected; search
does not follow symlinks.

`fs.list` is non-recursive by default. `fs.read_text` is UTF-8 and bounded to
64 KiB per call. `fs.search_text` performs literal search only, with bounded
files, depth, matches, and file size. `fs.write_new` uses create-new semantics.
`fs.apply_patch` currently accepts a bounded full-text `replacement` (or
`patch` alias), returns a bounded preview/diff, requires the caller's SHA-256
base hash, rechecks the hash immediately before replacement, writes a same-
directory temporary file, flushes it, and replaces atomically where the host
filesystem supports it. A Windows fallback preserves/restores a backup if
replacement fails.

Final `realpath`/metadata checks narrow TOCTOU and reparse-point races, but
Node cannot provide a kernel-level no-swap guarantee between authorization and
open/rename on every Windows filesystem; that residual is documented for S4
acceptance.

Mutating tools are T2 and `requires_confirmation: true`; the controller, not a
tool or model, binds confirmation to request/call IDs.

## System/clipboard/app/browser

`system.get_info` returns bounded OS/runtime/memory metadata only. Clipboard
integration is explicitly Windows-only (`powershell.exe Get-Clipboard -Raw`
for read and `clip.exe` for write), with no arbitrary command arguments.
`app.open` accepts only a logical ID resolved through a trusted executable and
argument allowlist. `browser.open_url` is an external network action: it
requires the explicit `browser_open` network provider, exposes the canonical
destination in its confirmation preview/result, accepts HTTPS only, rejects
credentials, literal IP/private/local names, and launches through a fixed
executable plus argv with `shell: false`. Neither action retrieves web
content.

On non-Windows hosts clipboard calls return a typed `platform_unsupported`
result; no provider or shell fallback is attempted.
