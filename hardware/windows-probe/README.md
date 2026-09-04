# Windows hardware receipt probe

`Get-HardwareReceipt.ps1` is a no-admin, read-only target probe for `PERF-001`
and `PERF-019`. It collects exact Windows/CPU/DIMM/baseboard/BIOS fields, Dell
product, display adapter/PNP/driver identity, storage, known graphics runtime
DLLs, and optional bounded `vulkaninfo --summary` evidence.
It does not read a full environment, credentials, browser state, model files, or
network settings; it does not install or update a driver/runtime and makes no
network changes.

Run on the Dell target with an explicit output path:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& .\Get-HardwareReceipt.ps1 `
  -OutputPath .\out\hardware-receipt.json `
  -VulkanInfoPath D:\ApprovedTools\vulkaninfo.exe `
  -ExpectedVulkanInfoSha256 '<approved lowercase SHA-256>'
```

The probe writes `hardware-receipt.json`, a SHA-256 sidecar, and a short summary.
Do not commit the target receipt unless identifiers have been redacted and Sol/S4
approve it. Missing Vulkan/SYCL information is recorded as unknown, never inferred
as unsupported. An ambient `vulkaninfo` is recorded as unpinned and cannot satisfy
the target acceptance gate; the gate requires the explicit path and approved hash
form above. The receipt is the input to backend profile selection; Intel vendor
name alone is not sufficient for promotion. Serial numbers, machine/user names,
network data, environment dumps, and credentials are deliberately omitted.
