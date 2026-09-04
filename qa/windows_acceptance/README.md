# Windows clean-machine acceptance

This directory prepares `PERF-019` and implements the `QA-020` target gate for
the reported Dell laptop. It does not contain current target evidence, and the
checked-in target profile is deliberately labeled
`reported-values-awaiting-exact-receipt`.

The target assertions are:

- Windows x64/AMD64 on a Dell computer;
- one exact Intel Core Ultra 7 CPU receipt (the reported platform tier is vPro
  Enterprise; the probe must capture the exact SKU rather than infer one);
- Dell baseboard `039NNG`, revision `A00`;
- exactly 32 GiB across populated memory devices, configured at 5600 MT/s;
- one Intel integrated/UMA Graphics adapter with an exact PCI PNP ID and driver
  `32.0.101.8247`;
- a pinned, pre-approved `vulkaninfo.exe` receipt correlated with the same
  graphics device; and
- at least 12 GiB available memory at launch time.

The acceptance scripts never install, download, elevate, change a driver, dump
the environment, or print an account token, bootstrap nonce, model response,
message, email, file content, or provider diagnostic. Run them only from an
approved local copy. Do not use execution-policy bypass flags.

## Inputs that must already exist

1. The generated CPU portable package, including the pinned Node 24.20.0
   runtime, manifest, checksums, engine, host, UI, launcher, notices, and SBOM.
2. The separately transferred immutable model
   `Qwen3.5-9B-Q4_K_M.gguf` (`5629109088` bytes, SHA-256
   `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`).
3. A reviewed Vulkan candidate named `lae-engine-vulkan.exe` and its approved
   SHA-256. It is a test candidate, not a promoted release binary.
4. A separately approved, pre-supplied `vulkaninfo.exe` and SHA-256. The probe
   will not fetch it or accept an unpinned copy for READY.
5. For optional live checks, a reviewed host config whose capabilities point
   only at dedicated synthetic accounts/targets and a disposable Copilot
   workspace. Provider credentials stay in their approved provider/OS stores;
   they are never command-line arguments or receipt fields.

## Operator sequence

Use a standard, non-administrator Windows PowerShell session. The output folder
must be new and local.

```powershell
& .\hardware\windows-probe\Get-HardwareReceipt.ps1 `
  -OutputPath 'D:\Acceptance\run-001\hardware-receipt.json' `
  -VulkanInfoPath 'D:\ApprovedTools\vulkaninfo.exe' `
  -ExpectedVulkanInfoSha256 '<approved-vulkaninfo-sha256>'

& .\qa\windows_acceptance\Invoke-WindowsAcceptance.ps1 `
  -PackageRoot 'D:\ApprovedRelease\LocalBMO' `
  -ModelPath 'D:\ApprovedModels\Qwen3.5-9B-Q4_K_M.gguf' `
  -HardwareReceiptPath 'D:\Acceptance\run-001\hardware-receipt.json' `
  -VulkanEnginePath 'D:\ApprovedCandidates\lae-engine-vulkan.exe' `
  -ExpectedVulkanEngineSha256 '<approved-vulkan-engine-sha256>' `
  -OutputDirectory 'D:\Acceptance\run-001'
```

The harness verifies the package, runs the CPU portable path, exercises a
bounded synthetic chat and cancellation, tests hostile/replayed browser
bootstrap requests, performs both graceful and abrupt process-tree cleanup,
opens the real browser through `Shell.Application.ShellExecute`, asks the
operator to confirm that the fragment disappeared, and then exercises the
exact Vulkan candidate with no CPU fallback. Vulkan failure is recorded as
`REJECTED_WITH_EVIDENCE`; it is never hidden or promoted.

Before the CPU pass, the harness requires the operator to disconnect external
network access and enter `OFFLINE-CORE-CONFIRMED`; the disabled-provider status
is also checked. The harness never changes a network adapter, firewall, proxy,
or policy. If live checks were selected, it later asks the operator to reconnect
through the normal approved path and enter
`LIVE-SYNTHETIC-NETWORK-CONFIRMED`.

The diagnostic bootstrap URL crosses redirected process memory only. The
harness removes it from the captured stream and never prints or writes it. The
bearer token remains in process memory, is sent only in loopback HTTP headers,
and is never placed in argv, environment variables, output, or receipts.

## Optional live full-access checks

Add both `-HostConfigPath '<reviewed-local-config.json>'` and
`-RunLiveChecks`. The harness opens the visible UI, then requires the exact
typed phrase `I CONSENT TO SYNTHETIC LIVE ACTIONS`. For every check, the
operator must inspect the product's own confirmation card and record
`PASS`, `FAIL`, or `SKIP`:

- read a dedicated synthetic mailbox message;
- read a dedicated synthetic Teams conversation;
- create an Outlook draft addressed only to the test recipient;
- send one uniquely tagged synthetic mail after per-action confirmation;
- send one uniquely tagged synthetic Teams message after confirmation;
- open Outlook through the configured logical application ID;
- open an approved HTTPS test URL;
- fill/activate a control only on the dedicated browser test page; and
- ask GitHub Copilot through the existing prompt-only ACP adapter for a bounded
  code suggestion using an explicitly selected harmless file in a disposable,
  non-repository workspace; confirm that no edit was applied implicitly, then
  delete the workspace after evidence capture.

Do not use real company mail, chats, files, repositories, or destinations. Do
not paste tokens into prompts. A `SKIP` or missing consent keeps full-access
status `NOT_READY`; it is never converted to a pass. The receipt stores only
check IDs, status, timestamps, and consent booleans—not message text,
recipients, account IDs, prompts, replies, URLs, diffs, or credentials.

## Offline gate and interpretation

Transfer only the new receipt directory to the review machine, then run:

```text
python -m qa.windows_acceptance.verify D:\Acceptance\run-001\target-acceptance-receipt.json
```

The verifier requires the separate `hardware-receipt.json` beside the target
receipt and binds its bytes/hash to the embedded hardware object. It rejects
duplicates, malformed/oversized JSON, simulated receipts, non-target runs,
wrong hardware, unpinned Vulkan evidence, wrong runtime/model identities,
missing CPU/bootstrap/job evidence, silent GPU fallback, and incomplete live
consent/results.

`core_ready: true` means the CPU release path and target lifecycle are proven
and the Vulkan candidate has a recorded target disposition. It does not promote
Vulkan. Overall `status: READY` additionally requires every opt-in full-access
check above. Only Sol may turn the receipt into final `ACCEPTED`,
`ACCEPTED_WITH_LIMITATIONS`, or `REJECTED_FOR_TARGET` status.

## Current status

`NOT_READY`. No exact target receipt or Windows execution evidence is checked
in. Static fixture tests only prove that the gate fails closed and the harness
contains the required safety/receipt controls.
