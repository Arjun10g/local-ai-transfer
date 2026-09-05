# Inert Windows compile/static-analysis checks

`LAE_ENABLE_INERT_WINDOWS_COMPILE_CHECKS` is `OFF` by default. When explicitly
enabled on an approved Windows SDK worker with an MSVC-compatible frontend, it
adds six isolated, non-installed static-library checks and the finite
`lae_inert_windows_compile_checks` convenience target.

The checked boundaries are exactly:

- `windows_readonly_fs`;
- `windows_hardware_attestor`;
- `action_journal_helper`, including its required merged
  `action_journal_storage` implementation;
- `windows_release_verifier`;
- `windows_supervisor`, with `process_transaction.inc` retained as an included,
  header-only source dependency rather than an independent translation unit;
- `windows_clipboard`.

The journal codec uses the repository's already-vendored nlohmann 3.12 headers.
No dependency is downloaded and no runtime dependency is added. Explicit
non-default import libraries are a finite set of Windows SDK system libraries
only; Kernel32 and the MSVC runtime remain toolchain platform defaults. The
supervisor declares `advapi32`, `bcrypt`, and `wintrust`; clipboard declares
`advapi32`, `bcrypt`, and `user32`. Static checks cover both directions: each
declared library has an imported API justification, and each observed
non-default API family has its required declaration. This is source-level
closure evidence only and does not turn the static archives into product
binaries or prove a successful final Windows link.

This is compilation evidence, not activation evidence. The compile graph does
not define or replace any trust, issuer, supervisor, cancellation, manifest,
or production-availability value. It is absent when the option is off and is
not referenced by `lae_runtime`, `lae-engine`, install rules, launchers, host
registries, release manifests, or package builders.

An approved remote Windows evidence run may configure a separate build tree
with the option enabled and build only `lae_inert_windows_compile_checks`.
Local or target-laptop configuration/build remains prohibited. Successful
compilation does not authorize packaging or execution; independent source
review, signed-package integration, and exact-target acceptance remain open.

The future approved remote evidence command shape is:

```powershell
cmake -S . -B build-inert-checks -A x64 `
  -DLAE_ENABLE_INERT_WINDOWS_COMPILE_CHECKS=ON
cmake --build build-inert-checks --config RelWithDebInfo `
  --target lae_inert_windows_compile_checks
```

Those commands are not target-laptop operator steps and have not been run in
this source-only slice.
