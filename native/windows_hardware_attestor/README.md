# Windows native hardware attestor (inert source)

This directory is a source/protocol slice, not an executable feature. It is
absent from CMake, packages, the host, and capability registries. The checked-in
trust anchor is empty, global blocking-call supervision is unavailable, and
vPro/integrated-GPU classification is unproven. Consequently, the public entry
point stops before hardware access and produces only a `NOT_READY` diagnostic
receipt in memory. The stdout boundary also refuses while supervision is off.

The latent collector is deliberately narrow:

- The executable, sibling manifest, and sibling diagnostic script are opened
  without write/delete sharing, constrained to regular single-link files, and
  hashed through retained handles. Expected hashes, executable size, and signer
  certificate thumbprint are compile-time source constants with no caller or
  environment override. Authenticode uses cache-only retrieval and explicitly
  disables revocation checks; that offline policy is **not approved** in this
  source and is a separate activation gate.
- CPUID and `GetLogicalProcessorInformationEx` provide bounded CPU feature and
  topology evidence. `GetSystemFirmwareTable('RSMB')` provides bounded SMBIOS
  BIOS/baseboard/RAM facts; serial-number fields are never read or emitted.
- SetupAPI reads only the display hardware-ID and fixed driver registry values.
  It normalizes the non-instance PCI vendor/device/subsystem/revision tuple and
  emits only its domain-separated SHA-256. Full device-instance IDs, INF paths,
  and locations are never emitted. DXGI adapters must correlate one-to-one with
  that normalized tuple. DXGI alone is not treated as authoritative evidence of
  integrated/UMA status.
- Vulkan evidence is limited to opening and hashing the fixed
  `System32\\vulkan-1.dll` file. The collector never loads the DLL, enumerates a
  runtime, launches a process, or loads a model.

Every variable-size source has a fixed cap. Checkpoints are present between
operations, but SetupAPI, DXGI, firmware, registry, and stdout APIs may block.
The checked-in gate therefore refuses rather than claiming the 30-second bound;
activation requires an independently accepted process-level supervisor.

## API assumptions

The source relies on the documented contracts for
[CreateFileW](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew),
[GetFileInformationByHandleEx](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandleex),
[WinVerifyTrust](https://learn.microsoft.com/windows/win32/api/wintrust/nf-wintrust-winverifytrust),
[GetSystemFirmwareTable](https://learn.microsoft.com/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemfirmwaretable),
[SetupDiGetDeviceRegistryPropertyW](https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupdigetdeviceregistrypropertyw),
[DXGI_ADAPTER_DESC1](https://learn.microsoft.com/windows/win32/api/dxgi/ns-dxgi-dxgi_adapter_desc1),
and [GetSystemDirectoryW](https://learn.microsoft.com/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemdirectoryw).
Those citations record design assumptions; no Windows probe or build was run for
this slice.

The receipt schema and activation gates are under
`contracts/windows-hardware-attestor/v1/`. Fixtures are synthetic and must never
be accepted as target evidence.
