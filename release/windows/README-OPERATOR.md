# Local Assistant Engine — Windows x64 fixture package

This is a REL-001 package skeleton, not a runnable release. It contains no
model weights, compiler, Node modules, credentials, or installer. The approved
Qwen3.5-9B Q4_K_M model must remain a separately verified local file. Its
filename, 5,629,109,088-byte size, SHA-256, GGUF metadata and tensor inventory
are compiled into the engine and planner; caller-supplied identity values are
not accepted. `config.example.json` documents the strict native config only.

`windows_backend_plan.py` is shipped beside the PowerShell scripts, removing a
repository-relative script dependency. Build and run still require an already
installed Python 3 interpreter selected by `-PythonCommand`; its absence is a
clear hard failure. Therefore this skeleton is not yet a self-contained
portable runtime. A future packaged launcher must eliminate that interpreter
dependency before portability can be claimed.

## Explicit Windows backend choice

`backend-profiles.json` and `backend-runtime.example.json` describe a
receipt-gated choice with no fallback. The planner requires Windows x64/AMD64,
at least 24 GiB installed and 12 GiB currently available RAM, and an absolute
pinned model path. A nonexistent path can produce only a `planning-only` plan;
an exact verified artifact advances it to `launch-preconditions-verified`.
Neither state is called execution-ready: binary identity, model-load and startup
self-test evidence remain `UNPROVEN` until actual execution.

Run `Build-WindowsBackend.ps1 -Backend cpu-safe ...` for
the product CPU engine, or select
`-Backend intel-sycl-experimental -AllowExperimentalSycl ...` for an explicit
upstream llama.cpp SYCL CLI diagnostic build (still experimental and blocked
until a separate acceptance review). `Run-WindowsBackend.ps1` requires the
same choice and never changes it to CPU when SYCL is unavailable. It consumes
the packaged plan JSON, checks engine build identity, and passes that plan's
exact model, backend, context, device and offload values to the engine; an
unrelated config cannot substitute launch values. For the product engine,
`LAE_ENGINE_TOKEN` is removed from the child environment and sent only through
stdin, never argv. The SYCL
runtime selects `SYCL0` and emits bounded one-token output through
`llama-cli`; it does not expose an unauthenticated server and is not a
promoted product backend until a separate review approves its compatibility.

The profile order is CPU (mandatory baseline), Vulkan (primary accelerated
candidate, source-locked and implemented but not promoted until target
acceptance), then SYCL (experimental diagnostic, currently blocked). This
package does not claim that Vulkan features or SYCL support are present on the
target until the receipt proves them. Vulkan additionally requires a separate
operator GPU attestation bound by receipt SHA-256, exact PNP ID, adapter name,
driver, and Vulkan enumeration identity; the WMI probe cannot infer integrated
status from an Intel product name. The `GGML_VULKAN` source/shader closure is
vendored under `vendor/llama.cpp` and independently verified by
`ggml-vulkan-source-lock.json`; a modified, missing, or unexpected closure
file fails closed. The profile remains unpromoted until the bound Windows
attestation and acceptance run are complete.

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

Before any release claim, provide the real `lae-engine-cpu.exe` and a packaged
Python-independent launch path, run the
allowlist/dependency/secret/weight scanner, generate checksums and SBOM, and
complete native Windows launch and offline acceptance. No Shadeform, Intel,
real-model, or native-Windows evidence is implied by this tree.
