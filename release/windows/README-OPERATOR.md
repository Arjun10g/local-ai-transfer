# Local Assistant Engine — Windows x64 portable package source

This directory is the inspected source template, not a release artifact. Build
the portable CPU tree from the repository root with an already-built Windows
CPU engine:

```text
python scripts/package/build_portable.py --source . --engine D:\build\lae-engine-cpu.exe --node D:\approved\node.exe --node-license D:\approved\node-v24.20.0-LICENSE --output D:\package\LocalBMO
```

`node.exe` must be the official Node.js v24.20.0 Windows x64 executable with
SHA-256 `5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5`;
the builder rejects any other bytes. Python is a packaging-machine tool only.
The matching Node v24.20.0 `LICENSE` file is also mandatory (SHA-256
`5888dbb9a1d2b18f2c3e6c5f6af1b39de658372b402a0577b002777f14c62ace`),
and the generated package carries both Node and llama.cpp license texts.
The generated target package contains the pinned Node runtime and
the exact Node host/controller/tool/provider source closure, UI, CPU engine,
manifest, checksums, notices, and a foreground supervisor. The target requires
Windows x64, but no preinstalled Node or Python. It contains no model
weights, compiler, credentials, or installer. The approved
Qwen3.5-9B Q4_K_M model must remain a separately verified local file. Its
filename, 5,629,109,088-byte size, SHA-256, GGUF metadata and tensor inventory
are compiled into the engine and planner; caller-supplied identity values are
not accepted. `config.example.json` documents the strict native config;
`host-config.example.json` is the separate supervisor/tool-provider config and
cannot substitute engine identity or launch values.

On the target, first run `Verify-Release.ps1`, then
`Start-LocalAssistant.ps1 -ModelPath D:\ApprovedModels\Qwen3.5-9B-Q4_K_M.gguf`.
The supervisor refuses non-Windows/non-x64 hosts, linked or renamed engine/model
files, duplicate/unknown config keys, and a binary whose compiled identity is
not the pinned CPU product. It passes the exact model/backend/context plan to
the engine, generates the engine bearer in memory, sends it only through stdin,
and supervises engine plus host as one foreground lifetime. No credential is
placed in argv or the environment.

The launcher creates current-user-only named pipes before starting the
supervisor. It assigns the supervisor to the kill-on-close Job and only then
opens a launch gate, so the native engine cannot be created in the assignment
window. The supervisor passes the 60-second, one-time bootstrap URL back
through the second pipe, and the launcher passes it to the Windows shell
through the COM API. Default stdout/stderr contain neither the nonce nor either
bearer. The explicit diagnostic pair `-NoBrowser -RevealBootstrapUrl` prints
the URL when shell launch is prohibited. The launcher never constructs a
browser command line containing the nonce. Its
nonce is a URL fragment (never an HTTP query), is removed by
`history.replaceState` before the exchange, and the exchange explicitly uses
`no-referrer`; the bearer returned by the exchange remains in page memory.
Static assets are unauthenticated so normal navigation works, while every API
remains bearer-protected. Hostile origin/referrer, malformed/duplicate bodies,
and replay are rejected. A Windows kill-on-close Job contains the Node
supervisor and its native engine child, including abrupt launcher termination.

## Explicit Windows backend choice

The following build/planner/diagnostic scripts belong to this source template;
they are intentionally not copied into the CPU portable package. They may use
Python and toolchains on an engineering machine without creating a target
Python/npm/install prerequisite. `Start-LocalAssistant.ps1` is the only
generated-package runtime entry point.

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
the planner's exact JSON, checks engine build identity, and passes that plan's
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

Before any release claim, provide the real `lae-engine-cpu.exe`, build and scan
the generated package, then complete native Windows launch, exact model-load,
tool-provider, and offline acceptance. The package builder and static mocked
tests prove dependency closure and launch wiring only. They do not prove the
Node executable, PE/DLL loader, 5.6 GB artifact, Intel target, or providers on
the actual laptop. No Shadeform, Intel, real-model, or native-Windows evidence
is implied by this source tree.
