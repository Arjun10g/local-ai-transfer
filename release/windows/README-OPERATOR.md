# Local Assistant Engine — Windows x64 fixture package

This is a REL-001 package skeleton, not a runnable release. It contains no
model weights, compiler, Node modules, credentials, or installer. The approved
Qwen3.5-9B Q4_K_M model must remain a separately verified local file. Set the
absolute `model_path`, exact `model_size_bytes`, and exact `model_sha256` in
`config.local.json`; `backend_profile` defaults to `cpu` and no model fallback
is permitted. Supply `LAE_ENGINE_TOKEN` through the protected launch
environment before invoking `Start-LocalAssistant.ps1`.

## Explicit Windows backend choice

`backend-profiles.json` and `backend-runtime.example.json` describe a
receipt-gated choice with no fallback. Run `Build-WindowsBackend.ps1 -Backend cpu-safe ...` for
the product CPU engine, or select
`-Backend intel-sycl-experimental -AllowExperimentalSycl ...` for an explicit
upstream llama.cpp SYCL CLI diagnostic build (currently blocked until the
product engine has a reviewed `GGML_VULKAN` profile). `Run-WindowsBackend.ps1` requires the
same choice and never changes it to CPU when SYCL is unavailable. The SYCL
runtime selects `SYCL0` and emits bounded one-token output through
`llama-cli`; it does not expose an unauthenticated server and is not a
promoted product backend until a separate review approves its compatibility.

The profile order is CPU (mandatory baseline), Vulkan (primary accelerated
candidate, implemented but not promoted), then SYCL (experimental diagnostic,
currently blocked). This
package does not claim that Vulkan features or SYCL support are present on the
target until the receipt proves them. Vulkan additionally requires a separate
operator GPU attestation bound by receipt SHA-256, exact PNP ID, adapter name,
driver, and Vulkan enumeration identity; the WMI probe cannot infer integrated
status from an Intel product name.

The SYCL build requires an already-installed Visual Studio C++ toolchain,
Intel oneAPI DPC++/C++ compiler and runtime, CMake, Ninja, and an existing
llama.cpp checkout at revision
`3581ba0cf591b3f772fbb002de0f70e294bc0396`. The scripts do not download or
install any of these. See the [official llama.cpp SYCL backend documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md)
and [Intel oneAPI Windows guide](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/get-started-guide/2025-2/get-started-on-windows.html).
The llama.cpp documentation lists Windows
support and Intel Core iGPU generations; an Intel product name alone is not
capability evidence. The read-only hardware receipt must contain an exact PNP
device identity, explicit integrated/UMA evidence, and a bounded successful
SYCL/Level Zero probe. The current probe intentionally records those fields as
unknown/not checked, so it cannot authorize Vulkan or SYCL by itself.

The planning defaults are conservative for the reported 32 GiB shared-memory
machine: 8 GiB OS/application reserve, approximately 5.25 GiB resident Q4,
2 GiB runtime headroom, and 8192 context tokens. 16384 tokens requires a
fresh target measurement. “Core Ultra 7 vPro” is a family description, not an
exact SKU; no macOS run or family-name inference is target validation.

Before any release claim, provide the real `lae-engine-cpu.exe`, run the
allowlist/dependency/secret/weight scanner, generate checksums and SBOM, and
complete native Windows launch and offline acceptance. No Shadeform, Intel,
real-model, or native-Windows evidence is implied by this tree.
