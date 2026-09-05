# Windows native broker source skeleton (B-005)

This directory is a source and protocol design for the single Windows boundary
that will eventually serve allowlisted process, application, browser,
clipboard, and Copilot actions. It is deliberately **not** referenced by the
top-level CMake project, package allowlist, launcher, host, or tool registry.
The compiled trust anchor defaults to empty, so even an ad-hoc build refuses
activation. Windows launch-capable tools therefore remain hidden.

This is not a target-ready binary. It has not been compiled, signed, packaged,
or exercised on Windows. Activation requires all of the following in a later,
separately reviewed change:

1. Generate a product-mode action manifest on the accepted target profile.
2. Pin its exact SHA-256 into an authenticated remote Windows build through
   `LAE_WINDOWS_BROKER_MANIFEST_SHA256_HEX`.
3. Build and sign the broker, record its own immutable package identity, and
   integrate an identity-pinned supervisor path. Node path-based spawning is
   not sufficient to authenticate the broker itself.
4. Run real Windows tests for NTFS share-mode leases, reparse/junction races,
   restricted tokens, nested Job Objects, cancellation, parent death, output
   overflow, GUI launch behavior, descendant-after-parent-exit behavior,
   `ACTIVE_PROCESS_ZERO`/accounting ordering, and orphan cleanup.
5. Review every executable's DLL/plugin/config loading surface. Pinning the
   primary image alone does not authenticate mutable DLLs or extensions.
6. Only then replace the current `unsafe_subprocess_boundary` capability
   exclusions. No file in this directory performs that replacement.

## Trust and launch invariant

The broker derives `windows-broker.manifest.json` from its own module directory;
the protocol cannot name a manifest. The manifest is accepted only when its
streamed SHA-256 equals the non-empty digest compiled into the broker. Duplicate
keys, unknown keys, test-mode manifests, and unbounded values are rejected.

Before `CreateProcessAsUserW`, the implementation opens the executable and every
ancestor with `FILE_FLAG_OPEN_REPARSE_POINT`. The image handle denies WRITE and
DELETE sharing; ancestor directory handles deny DELETE sharing. It checks an
exact normalized DOS path, NTFS volume, size, SHA-256, volume serial, and
`FILE_ID_128`, then retains every handle until the child has terminated and its
Job has been reaped. The exact working directory receives the same no-reparse,
identity-pinned lease. This closes the path replacement window without assuming
that `CreateProcessW` can consume an already-open image handle.

The child is created suspended with an explicit application path, fixed
manifest-rendered argument template, explicit handle list, and a freshly built
environment containing only `SystemRoot`, `TEMP`, and `TMP`. A restricted token
is mandatory for launch actions in this skeleton. The suspended process is
assigned to a non-inheritable Job Object with `KILL_ON_JOB_CLOSE` before its
primary thread is resumed. Any validation, assignment, image-path recheck, or
resume failure terminates and reaps the Job. A primary-process exit is not
success: the completion port plus Job accounting must prove zero active
processes. If descendants remain until the action deadline, the broker
terminates the Job and requires the same bounded zero-process proof; otherwise
it returns `job_reap_failed`, never `ok`.

## Protocol

The only transport is inherited stdin/stdout. There is no listener, socket,
named-pipe server, shell mode, or generic executable operation.

- Four-byte unsigned big-endian byte length followed by one UTF-8 JSON object.
- Inbound frame maximum: 65,536 bytes.
- Outbound frame maximum: 1,048,576 bytes.
- Strict JSON: duplicate keys, unknown fields, trailing data, invalid UTF-8,
  excessive depth, and wrong types fail closed.
- One active invocation. `cancel` frames remain processable while it runs.
- Requests contain a logical `action_id` and typed parameters only. They never
  contain an executable, raw argv, cwd, environment, shell text, or manifest.
- Copilot prompt material and process stdin use a bounded inherited pipe, never
  argv or environment. Receipt metadata records byte counts, never content.
- EOF from the parent cancels the active Job. Killing the broker closes the only
  Job handle and invokes the kernel's kill-on-close behavior.

The normative schemas and non-secret fixtures live under
`contracts/windows-process-broker/v1.0.0/`.

## Explicitly unresolved before activation

- Whether the enterprise image permits restricted-token interactive GUI launch
  without elevation, and which token restrictions are compatible with the
  visible browser/application lane.
- How the signed portable supervisor will authenticate and launch the broker
  itself while holding its image/package identity through process creation.
- The accepted immutable identities and dependency closures for Edge/Chrome,
  Teams/Outlook launch helpers, Git/GitHub Copilot, and any approved scripts.
- Whether Copilot can operate with no inherited `PATH`, `USERPROFILE`, credential
  environment, or ambient handle. Any exception needs its own narrow design.
- Whether target security software or an enclosing enterprise Job prevents
  nested Job assignment or restricted-token launch.
- Clipboard ownership/retry behavior and visible-desktop access on the exact
  interactive target session.
- Exact cwd selection does not sandbox a trusted child from other paths that its
  restricted token can access. Every process action still needs a least-privilege
  executable/argument review and target DACL evidence.

Until these are resolved by a remote-built binary and target receipts, B-005
remains open and Windows laptop-control readiness remains `NOT_READY`.
