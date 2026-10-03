# Local tools

These modules run in the Node.js 24 host with built-in modules only. The model
does not receive executable paths, risk tiers, or policy configuration.
`createLocalToolRegistry()` includes only capabilities supported by immutable
startup configuration and records a frozen `capabilitySnapshot`. `time.now`
and `system.get_info` are always present. Filesystem tools require an applicable
configured workspace; app/browser/process tools require their allowlists or
provider/action configuration. `createLocalToolDefinitionCatalog()` is for
schema evaluation only and is never a production execution registry.

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

Final `realpath`/metadata checks narrow TOCTOU and reparse-point races on the
supported POSIX path.

On Windows the POSIX implementation always refuses (`platform_path_safety_unavailable`;
Node has no `O_NOFOLLOW` there). The registry instead serves only the
read-only trio `fs.list`, `fs.read_text` and `fs.search_text` from
`windows-filesystem.mjs`, and only for workspaces the operator explicitly
configured with a local drive path (never a default workspace, never UNC or a
drive root). It is a check-then-verify protocol (strict Win32 path strings,
per-component `lstat` refusing reparse points and other volumes, `realpath`
equality, open-handle identity re-check); the capability snapshot marks it
`profile: windows_read_only`. `fs.write_new` and `fs.apply_patch` stay
`NOT_READY` on Windows: a create or replace through a swapped ancestor cannot
be undone by a post-hoc identity check.

Mutating tools are T2 and `requires_confirmation: true`; the controller, not a
tool or model, binds confirmation to request/call IDs.

## System/clipboard/app/browser

`system.get_info` returns bounded OS/runtime/memory metadata only. Clipboard
integration is Windows-only and runs Windows PowerShell by its absolute
System32 path (derived from a validated `SystemRoot`, never a bare name,
because CreateProcess searches the current directory first) with fixed
`-EncodedCommand` scripts, module-qualified `Get-/Set-Clipboard`, a pinned
`PSModulePath`, a minimal environment, and text only on stdin/stdout as raw
UTF-8 (`clip.exe` is no longer used: it mangles non-ASCII). stderr is never
returned. `app.open` accepts only a logical ID resolved through a trusted
executable and argument allowlist; the executable must be an absolute local
path (config validation and the tool both refuse bare names and UNC paths),
and launched programs do not inherit the host's `LAE_*` settings. `browser.open_url` is an external network action: it
requires the explicit `browser_open` network provider, exposes the canonical
destination in its confirmation preview/result, accepts HTTPS only, rejects
credentials, literal IP/private/local names, and launches through a fixed
executable plus argv with `shell: false`. Neither action retrieves web
content.

On non-Windows hosts direct clipboard calls return a typed
`platform_unsupported` result. On Windows, clipboard, `app.open` and
`browser.open_url` are hardened but still NOT registered (snapshot reason
`unsafe_subprocess_boundary`): the executables are resolved by path, not by a
pinned file identity, so a swapped binary at that path would still run, and
none of it has been exercised on a real Windows machine. They stay unregistered
until a native identity-pinned, minimal-environment broker exists. Direct
modules remain available to focused test seams; no provider or shell fallback
is attempted.

## Allowlisted process actions

`process.run_allowlisted` is disabled unless an operator configures a named
action. Each action uses an absolute executable path and fixed arguments;
shells, interpreters, script extensions, common LOLBins, PATH lookup, and
prototype-pollution IDs are rejected. Substitutions are constrained to safe
identifiers, workspace-relative paths, enums, or bounded scalar values.

The configured executable is an unsandboxed operator trust boundary: it may
access the network or local data, so previews disclose `operator_configured`
egress and T3 confirmation is required unless the exact action has a current
operator grant. Cwd is canonicalized inside a writable workspace immediately
before dispatch. Children receive only injected `SystemRoot`/`WINDIR`, stdin
is closed unless a bounded literal input parameter is declared, and stdout/
stderr are byte-bounded with UTF-8-safe decoding. Cancellation, timeout, and
overflow await bounded process-tree cleanup. Dispatches are at-most-once and
the bounded ledger rejects new actions after its safety limit; stderr content
is never returned.

Executable identity is checked as a regular, non-symlink file at preview and
again immediately before spawn. Node path APIs cannot provide a kernel-level
no-swap guarantee if a trusted parent directory is replaced between that final
check and process creation; that residual remains a platform acceptance item.

This is allowlisting and lifecycle control, not an OS sandbox. Windows
process-tree cleanup spawns `taskkill.exe` by its System32 path, but Windows
process-tree and executable-identity behavior requires platform-specific
acceptance evidence, so the tool is omitted from the Windows production
registry even when configured. The fake process tests exercise the injectable
termination boundary. A process action is also omitted unless its configured
cwd workspace is writable because the current execution contract resolves cwd
with write authorization; relaxing that condition requires a separate policy
decision.

## Launching URLs and stopping processes

- **macOS/Linux URL opener:** `/usr/bin/open` on macOS. On Linux only
  `/usr/bin/xdg-open` or `/bin/xdg-open`, and only a root-owned, executable
  regular file that group and others cannot write; otherwise the tool refuses
  with `browser_open_unavailable`. A bare `open`/`xdg-open` is never used: it
  would be found through `PATH`, so a planted binary could run.
- **Windows `taskkill.exe`:** started by its System32 path from a validated
  `%SystemRoot%` with only `SystemRoot` and `windir` in its environment.
- `launchEnvironment` (used by `app.open` and `browser.open_url`) removes only
  `LAE_*`; other variables still reach an application you configured. That is
  a deliberate design choice, recorded here so it is not mistaken for an oversight.

## Not verified on a real Windows machine

Every Windows behavior above is tested on POSIX with `path.win32` semantics,
simulated `stat` results and fake child processes. None of the following has
run on Windows: NTFS reparse/junction/8.3/volume behavior of the read-only
trio, PowerShell 5.1 start-up and clipboard round-trips under the minimal
environment, `taskkill.exe` tree cleanup, and app/browser launch.
