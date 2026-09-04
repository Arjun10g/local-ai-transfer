# Intel GPU Backend Decision and Validation Plan

## Purpose

The Dell laptop has an Intel GPU, but the exact device has not yet been captured. This document prevents the implementation from treating all Intel GPUs as equivalent and defines how CPU, Vulkan, SYCL, and partial offload are evaluated.

## Fixed backend policy

- **CPU ships and is always supported.**
- **Vulkan is the primary accelerated candidate.**
- **SYCL is experimental until promoted by a Sol-approved ADR.**
- **OpenVINO is a research comparator, not an MVP dependency.**
- **No registry changes, driver installation, or target-side compiler installation are allowed.**
- **No GPU path is accepted without correctness, stability, memory-recovery, and speed evidence on the exact target profile.**

## Why vendor name is insufficient

“Intel GPU” may refer to:

- an older Intel UHD integrated GPU,
- Iris Xe integrated graphics,
- Arc/Xe integrated graphics in a Core Ultra platform,
- an Intel Arc discrete GPU,
- a device with small dedicated memory,
- a unified-memory device borrowing system RAM.

These classes differ in Vulkan feature support, integer dot-product support, matrix/cooperative operations, driver stability, shared-memory pressure, and whether GPU offload is faster than a well-tuned CPU path.

## Phase 0 hardware receipt

Luna Session 2 prepares a read-only `Collect-HardwareReceipt.ps1`. Luna Session 4 reviews it before target execution. It must collect:

### System identity

```powershell
Get-CimInstance Win32_ComputerSystem |
  Select-Object Manufacturer, Model, SystemType, TotalPhysicalMemory

Get-CimInstance Win32_OperatingSystem |
  Select-Object Caption, Version, BuildNumber, OSArchitecture,
                TotalVisibleMemorySize, FreePhysicalMemory
```

### CPU

```powershell
Get-CimInstance Win32_Processor |
  Select-Object Name, Manufacturer, NumberOfCores,
                NumberOfLogicalProcessors, MaxClockSpeed,
                SecondLevelAddressTranslationExtensions,
                VirtualizationFirmwareEnabled
```

The repository probe also records CPUID/ISA flags relevant to the packaged CPU kernels, including AVX, AVX2, FMA, F16C, BMI2, AVX-VNNI, and AVX-512 where present.

### GPU and driver

```powershell
Get-CimInstance Win32_VideoController |
  Select-Object Name, PNPDeviceID, AdapterRAM,
                DriverVersion, DriverDate,
                VideoProcessor, CurrentHorizontalResolution,
                CurrentVerticalResolution
```

The packaged engine probe must additionally enumerate:

- Vulkan physical-device name and vendor/device IDs.
- UMA versus discrete memory classification.
- Vulkan heap sizes and budgets.
- FP16, integer-dot, subgroup, cooperative-matrix, and buffer-device-address support.
- Maximum storage-buffer and allocation sizes.
- Queue families.
- Driver/API versions.
- Whether the device is the active display adapter.
- A tiny deterministic compute self-test.

### Graphics diagnostics

When allowed, the script runs:

```powershell
$dx = Join-Path $env:TEMP "lae-dxdiag.txt"
dxdiag /dontskip /t $dx
```

The receipt stores only hardware/driver metadata. It must redact usernames, machine names, serial numbers, network identifiers, and unrelated installed-software details.

## Hardware receipt output

`hardware-receipt.json` includes:

```json
{
  "receipt_version": 1,
  "captured_at_utc": "<timestamp>",
  "system": {
    "manufacturer": "Dell Inc.",
    "model": "<captured>",
    "os_build": "<captured>",
    "ram_bytes": 0,
    "available_ram_bytes": 0
  },
  "cpu": {
    "name": "<captured>",
    "physical_cores": 0,
    "logical_processors": 14,
    "isa": []
  },
  "gpus": [
    {
      "name": "<captured>",
      "vendor_id": "0x8086",
      "device_id": "<captured>",
      "driver": "<captured>",
      "uma": true,
      "dedicated_bytes": 0,
      "shared_budget_bytes": 0,
      "vulkan": {
        "available": false,
        "api_version": "<captured>",
        "features": {}
      }
    }
  ],
  "redactions_applied": true,
  "sha256": "<computed over canonical receipt>"
}
```

## Shadeform hardware matching

The exact Dell integrated GPU may not be rentable. Shadeform testing therefore uses a ladder:

1. **CPU correctness profile:** Windows x64 CPU with similar instruction set and 16–32 GiB constrained RAM.
2. **Intel API profile:** an available Intel Arc/Xe machine when Shadeform offers one, matching generation as closely as possible.
3. **Generic Vulkan profile:** validates backend packaging and device selection but does not replace Intel-specific evidence.
4. **Target acceptance profile:** final short test on the actual Dell laptop.

Shadeform results are authoritative for conversion, broad testing, and reproducible builds. The actual target receipt and acceptance run are authoritative for exact Intel backend disposition.

## Build profiles

### `win-x64-cpu`

Mandatory release artifact:

- Static/self-contained dependencies as permitted by upstream licenses.
- Runtime ISA dispatch or a safe minimum plus optional tuned binary.
- No GPU runtime requirement.
- Model mapping and memory guards enabled.
- Used as correctness oracle against upstream CPU.

### `win-x64-vulkan-intel`

Primary accelerated candidate:

- Bundles the required Vulkan backend DLLs that are part of the application build.
- Depends only on the already installed Intel display driver/Vulkan ICD.
- Enumerates and pins the selected device by vendor/device ID.
- Supports explicit full or partial layer/operator offload profiles.
- Reports actual placement and any fallback.
- Supports a conservative profile that disables problematic optional matrix paths when required by evidence.

### `win-x64-sycl-intel-experimental`

Not a default release dependency:

- Built and tested only if all required redistributable components can be packaged lawfully and under workstation policy.
- Must beat the accepted CPU/Vulkan path by a practically meaningful margin.
- Must pass the complete parity, long-prompt, tool-call, cancellation, state-reset, and soak suites.
- Must not rely on a target-side oneAPI installation unless that installation is separately approved.

## Backend selection algorithm

The launcher does not simply prefer GPU. It follows an allowlisted backend receipt:

1. Verify model and application manifests.
2. Load `hardware-receipt.json` or collect a fresh read-only receipt with user approval.
3. Match vendor/device/driver against `backend-profiles.json`.
4. Select the most preferred profile whose exact acceptance tests have passed.
5. Run a fast startup self-test with fixed tokens.
6. If the self-test fails, fall back to CPU and surface a visible diagnostic.
7. Never fall back to cloud or another model.

Example profile order:

```json
{
  "profiles": [
    {
      "id": "intel-vulkan-conservative",
      "requires": {
        "vendor_id": "0x8086",
        "vulkan": true
      },
      "environment": {
        "GGML_VK_DISABLE_COOPMAT": "1"
      },
      "accepted_receipts": ["<exact hardware+driver hash>"]
    },
    {
      "id": "cpu-safe",
      "requires": {
        "x64": true,
        "avx2": true
      }
    }
  ]
}
```

The cooperative-matrix environment flag is an example of a driver-specific conservative profile, not a universal default. It is enabled only when testing supports it.

## Validation matrix

Each backend/offload profile is tested at:

- 512-token prompt, 128-token output.
- 2K prompt, 256-token output.
- 8K prompt, 256-token output.
- 16K prompt, 256-token output when the profile is eligible.
- 20 repeated short turns.
- Five reset/reload cycles.
- Cancellation during prefill.
- Cancellation during decode.
- Prefix-cache hit and miss.
- Session switching.
- Tool selection and structured argument generation.
- A long prompt followed immediately by a trivial prompt to detect persistent state corruption.
- Thirty-minute interactive soak.
- Multi-hour release soak on the final candidate.

## Correctness gates

An accelerated backend is rejected when any of the following occurs:

- Invalid token IDs.
- Garbled/multilingual token explosions absent on CPU.
- Persistent corruption after a long request.
- Incorrect tool syntax or tool-call regression beyond threshold.
- State leakage between sessions.
- Different stop-token behavior.
- Driver reset, TDR, hang, or process crash.
- Silent operator fallback that invalidates benchmark claims.
- Memory that is not released after reset/unload within the accepted tolerance.
- Output/logit divergence beyond the defined numerical tolerance.

The policy is **correctness before speed**. A single unexplained corruption in the release suite blocks the profile.

## Performance experiments

Luna Session 2 evaluates:

- CPU thread count and affinity.
- CPU batch and micro-batch sizes.
- Memory mapping and page-preload strategies.
- Vulkan full offload.
- Vulkan partial offload.
- Conservative versus cooperative-matrix paths where supported.
- Flash-attention on/off where supported and correct.
- F16, Q8, and candidate lower-bit cache types.
- 4K, 8K, and 16K context.
- Prefix-cache size and eviction.
- MTP/speculative path only after base correctness.
- Thermal/power behavior over at least 30 minutes.

Report:

- model load time,
- time to first token,
- prompt processing tokens/s,
- decode tokens/s,
- p50 and p95 turn latency,
- CPU utilization,
- GPU utilization,
- dedicated/shared GPU memory,
- process working set and commit,
- hard page faults,
- power/thermal throttling indicators when available,
- actual operator/device placement.

## Promotion criteria

### Vulkan promotion

Vulkan becomes the default only when:

- full correctness suite passes on the exact Dell receipt,
- no driver reset or persistent corruption occurs,
- default 8K process commit remains at or below the memory guard,
- decode or end-to-end turn latency improves by at least 20% over tuned CPU, or the user-experience improvement is otherwise clearly material,
- p95 stability does not regress materially,
- cancellation and recovery remain reliable.

If Vulkan is correct but only marginally faster, CPU remains the default and Vulkan remains an opt-in profile.

### SYCL promotion

SYCL requires all Vulkan criteria plus:

- a self-contained, policy-approved redistribution story,
- at least 15% end-to-end improvement over the accepted Vulkan profile or a distinct capability benefit,
- no missing/fallback Gated DeltaNet operations,
- no output corruption across the long-prompt and repeated-turn suites.

## Known current risks to reproduce

The test suite explicitly covers issues reported in current upstream Intel/Qwen3.5 use:

- Qwen3.5 SYCL decode substantially slower than Qwen3 on an Intel Core Ultra/Arc iGPU.
- Qwen3.5 SYCL failures or missing fused Gated DeltaNet support on some Arc systems/builds.
- Intel Arc 140V Vulkan driver timeouts with cooperative-matrix paths on some driver/build combinations, with a reported conservative disable flag.
- Backend/build regressions between llama.cpp commits.
- Silent CPU fallback under GPU memory pressure.

These reports guide tests; they do not prove the target will fail. The target’s exact receipt and pinned-build evidence decide.

## Primary-source anchors

- `https://github.com/ggml-org/llama.cpp/issues/22001`
- `https://github.com/ggml-org/llama.cpp/issues/20169`
- `https://github.com/ggml-org/llama.cpp/issues/20423`
- `https://github.com/ggml-org/llama.cpp/issues/20554`
- `https://github.com/ggml-org/llama.cpp/issues/24199`
- `https://github.com/ggml-org/llama.cpp/issues/26581`

Phase 0 records issue status and pins a known-good commit; no worker uses a floating latest binary in release evidence.
