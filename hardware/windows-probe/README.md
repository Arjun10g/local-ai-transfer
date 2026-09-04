# Windows hardware receipt probe

`Get-HardwareReceipt.ps1` is a no-admin, read-only target probe for `PERF-001`.
It collects Windows/CPU/memory, Dell product, display adapter and driver, storage,
known graphics runtime DLL, and optional bounded `vulkaninfo --summary` evidence.
It does not read a full environment, credentials, browser state, model files, or
network settings; it does not install or update a driver/runtime and makes no
network changes.

Run on the Dell target with an explicit output path:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& .\Get-HardwareReceipt.ps1 -OutputPath .\out\hardware-receipt.json
```

The probe writes `hardware-receipt.json`, a SHA-256 sidecar, and a short summary.
Do not commit the target receipt unless identifiers have been redacted and Sol/S4
approve it. Missing Vulkan/SYCL information is recorded as unknown, never inferred
as unsupported. The receipt is the input to backend profile selection; Intel vendor
name alone is not sufficient for promotion.
