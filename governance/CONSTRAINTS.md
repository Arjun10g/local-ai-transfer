# Constraints and Compliance Gates

## 1. Constraint hierarchy

The implementation must distinguish four categories:

1. **Observed fact:** directly demonstrated by the restriction probe.
2. **Planning assumption:** useful for design but not yet verified.
3. **Technical capability:** something the software or hardware could do.
4. **Policy approval:** something the company permits.

A technical capability is not permission. An installed component is not necessarily approved. An absent command is not proof that hardware is absent.

## 2. Observed target facts

| Area | Observed state | Engineering interpretation |
|---|---|---|
| OS | Windows x64 / AMD64 | Release is a portable Windows x64 package |
| CPU | 14 logical processors | Tune, but do not assume physical-core count or ISA |
| Physical memory | ~31.46 GiB | Not the deployable memory budget |
| Available memory | ~17.35 GiB in the probe | Use as the initial practical ceiling |
| Free workspace storage | ~111.44 GiB | Sufficient for the controlled 5–6 GiB Q4 artifact, comparison metadata, and application artifacts, subject to policy |
| Python | CPython 3.12.10 | May be used for optional diagnostics, not required by runtime |
| Node | 24.18.0 | Zero-dependency ESM host is viable |
| npm | 11.16.0 | Must not be required on target |
| CMake | 4.4.2 | Present, but no compiler was found |
| Git / GitHub CLI | Present | Repository use still requires an approved path |
| AWS CLI | Present | Not a model-inference fallback and not a credential source |
| `mmap` probe | Passed | Read-only model mapping is viable |
| Child-process probe | Passed | Foreground host can supervise engine/tool children |
| Temp-file probe | Passed | Atomic temp-file workflow is viable |
| Loopback socket probe | Passed | Local HTTP serving is viable |
| NumPy | Import passed | Not required by release |
| ONNX Runtime | Import passed | Not selected for the model runtime |
| ONNX providers | CPU and Azure | Does not establish local GPU or permission for cloud inference |
| PyTorch ecosystem | Not available | Do not make it a target dependency |
| Docker / Podman | Not found | MVP cannot require containers locally |
| Native toolchains | Not found | Build elsewhere and ship self-contained artifacts |
| Graphics | User reports an Intel GPU in a Dell laptop; NVIDIA CLI/toolkit was not found | Intel vendor is known, but exact adapter, generation, memory model, driver, and usable backend remain unverified |
| Public registries/hubs | Not dependable | No target-side dependency acquisition |

## 3. Binding deployment restrictions

### Target-side prohibited actions

The release and its normal operator steps must not:

- Download model weights.
- Contact a model hub.
- Invoke public PyPI or npm.
- Install packages.
- Install a compiler, SDK, driver, container runtime, service, or system component.
- Require Docker, Podman, WSL, Ollama, LM Studio, or a Python virtual environment.
- Require administrator rights.
- Bind to `0.0.0.0`, a LAN interface, or a public interface.
- Create a Windows service, scheduled task, startup entry, or persistent daemon.
- Store credentials in the repository or release folder.
- Place model weights inside Git.
- Send prompts or files to cloud inference.
- Continue after a model hash mismatch.
- silently reduce security controls for performance.

### Target-side permitted design assumptions

Subject to the final policy gate, the product may:

- Be copied as a portable ZIP or approved release artifact.
- Read a model from a user-specified local path.
- Create bounded application data beneath a configured local directory.
- Launch a foreground child process.
- Bind to loopback.
- Use existing Windows APIs and an already-present approved GPU driver.
- Use installed Node.js 24 without installing packages.
- Open an approved application or browser through an explicit tool action.
- Use a network search/fetch provider when separately configured and enabled.

## 4. Mandatory pre-transfer approvals

Sol must record evidence for each item before any target release is transferred:

| Gate | Required evidence | Owner |
|---|---|---|
| Repository | Approved organization/repository and data classification | Sol |
| Third-party code | License review, fixed revisions, SBOM, notices | Luna D |
| Model | Approved source, license, intended use, checksum | Luna B + Sol |
| Model transfer | Approved route, malware/DLP scan, storage location | Sol |
| Native binary | Security scan and provenance/attestation | Luna D |
| Network tools | Approved destinations, credentials method, data policy | Luna C + Sol |
| GPU backend | Exact device/driver, runtime availability, policy approval | Luna B |
| Local serving | Loopback-only architecture and local auth review | Luna D |
| Logging | Retention, redaction, and path accepted | Luna D |
| Workspace tools | Allowed roots and mutation policy accepted | Luna C |

A missing approval disables the relevant optional feature. It must not trigger an improvised workaround.

## 5. Model and file integrity constraints

The engine must:

- Require an explicit model path through config or CLI.
- Refuse directories, network paths by default, symbolic links/junctions that escape the approved model root, and alternate data streams.
- Open the model read-only.
- Validate GGUF magic, version, metadata lengths, tensor counts, dimensions, element counts, alignments, offsets, file bounds, and supported tensor types before model initialization.
- Reject overflow, truncation, overlapping ranges, impossible shapes, unsupported architectures, and allocation requests over policy.
- Verify the model manifest and SHA-256 on import. A cached verification record may accelerate later starts only when path, file identity, size, modification time, and manifest version match.
- Keep model data separate from writable sessions, caches, logs, and tool state.
- Never mutate, convert, split, or requantize the model on the target.

## 6. Resource bounds

Initial defaults; Luna B replaces planning values with measured values after Phase 1.

| Resource | Default | Hard behavior |
|---|---:|---|
| Context | 8,192 tokens | Reject over-limit prompt before prefill |
| Maximum generated tokens | 2,048 | Stop with typed finish reason |
| Active generations | 1 | Queue or reject additional work |
| Queue depth | 4 | Return busy error beyond limit |
| Tool calls per turn | 8 | Stop tool loop and ask user |
| Tool repair attempts | 1 | Fail closed after one repair |
| Tool stdout/stderr | 1 MiB each | Truncate and mark |
| Tool runtime | 30 seconds default | Kill process tree on timeout |
| HTTP request body | 2 MiB | Return 413 |
| Prefix cache | 512 MiB | LRU eviction |
| Tool-result cache | 128 MiB | Sensitivity-aware TTL |
| Metadata logs | 128 MiB rolling | Rotate; no content by default |
| Host + engine commit | 12 GiB | Cancel/deny allocation before breach |
| Minimum OS reserve | 4 GiB | Lower context/cache or refuse start |
| Session count | 4 stored, 1 active | LRU and explicit reset |
| Request deadline | 10 minutes | Cancel and clean state |

### Adaptive low-memory profile

When startup detects insufficient available memory:

1. Disable nonessential caches.
2. Reduce context to 4,096.
3. Reduce batch/ubatch.
4. Keep one session only.
5. Use CPU or partial GPU offload conservatively.
6. Refuse start if the model and minimum runtime budget still cannot fit.

The process must never rely on swap thrashing as a normal operating mode.

## 7. Intel GPU constraints

The user has identified the laptop GPU vendor as Intel, but **Intel GPU** is not a sufficient deployment specification. Until the Phase 0 hardware receipt is accepted:

- Do not assume Intel UHD, Iris Xe, Arc Xe-LPG/Xe2/Xe3, a discrete Arc adapter, or an NPU.
- Do not assume dedicated VRAM; many Dell Intel laptops use unified/shared system memory.
- Do not infer usable Vulkan or SYCL support from the adapter name alone.
- Do not install or update a driver on the restricted target.
- Do not make GPU acceleration a prerequisite for correctness or offline operation.
- Do not advertise an expected token rate before the exact adapter and driver are measured.

The read-only Phase 0 receipt must collect:

- Dell system product/model and BIOS version.
- CPU SKU, physical/logical core topology, ISA, and power profile.
- Every graphics adapter name, vendor/device/subsystem IDs, and active-adapter state.
- Dedicated, shared, and currently available graphics memory where Windows exposes it.
- UMA versus discrete-memory classification.
- Driver version/date, WDDM version, and DirectX feature level.
- Vulkan loader presence, physical-device enumeration, queue families, memory heaps, cooperative-matrix capabilities, and relevant extensions.
- SYCL/Level Zero device visibility only when the required runtime is already approved and present.
- Current available system memory, commit limit, page-file state, and storage.

The release always ships a CPU backend. Vulkan is the primary acceleration candidate because it can cover Intel integrated and discrete graphics through a portable llama.cpp backend, but it is promoted only after exact-output, long-prompt, repeated-turn, cancellation, and stability tests. SYCL remains experimental unless it materially beats the accepted Vulkan profile and has a self-contained approved redistribution story.

The implementation and promotion matrix in `governance/INTEL_GPU_BACKEND.md` is binding. A model/backend combination that is fast but produces one unexplained corruption, driver reset, silent CPU fallback, or cross-turn state contamination is rejected.

## 8. Build constraints

- Build on Shadeform or an approved remote CI worker.
- Produce reproducible source revision, compiler identity, flags, dependency revisions, and checksums.
- Build self-contained Windows x64 artifacts.
- Prefer static runtime linkage where license and toolchain permit.
- Ship separate CPU capability builds or runtime dispatch rather than requiring local compilation.
- Precompile GPU shaders. Do not ship a target-side SDK or shader compiler unless approved.
- Never require the target to fetch a Git submodule.
- Keep debug symbols in a separate restricted artifact.
- Sign or attest release artifacts when the available corporate mechanism permits.

## 9. Script constraints

- PowerShell is the canonical operator script language.
- Every critical operation must also have a direct executable command documented.
- Do not require `.bat` or `.cmd`.
- Scripts use `Set-StrictMode`, stop on error, quote paths safely, and avoid execution-policy bypass instructions.
- Do not modify machine-wide environment variables.
- Do not change firewall, registry, proxy, certificate store, browser policy, or antivirus settings.
- Do not print environment variables wholesale because they may contain secrets.

## 10. Network constraints

- Core inference performs no DNS lookup or outbound connection.
- Network tools live in the assistant host, not the native engine.
- Network tools are disabled unless a provider is explicitly configured.
- Providers are configured through environment variables or a protected local config, never source control.
- The host enforces destination policy, TLS validation, redirect limits, response-size limits, content-type handling, and timeouts.
- The host displays a confirmation when outgoing data includes local file content, clipboard content, or command output.
- Web content is untrusted and may not alter system/tool policy.
- Offline mode must be testable by disabling network access rather than by using a mock flag alone.

## 11. Logging constraints

Default logs may contain:

- Timestamp.
- Build/model identifier.
- Request ID.
- Token counts.
- Durations.
- Finish reason.
- Cache hit/miss.
- Tool name and status.
- Redacted error class.
- Memory and GPU metrics.

Default logs may not contain:

- Prompt or response text.
- Reasoning text.
- Tool arguments with content.
- File contents.
- URLs containing query strings or credentials.
- Headers, tokens, cookies, passwords, keys, or environment dumps.
- Model tensors or embeddings.
- Company account identifiers.

Content logging is a development-only feature on synthetic data and requires an explicit startup flag.

## 12. Compliance status values

Every policy-dependent feature uses exactly one status:

- `UNASSESSED`
- `REQUESTED`
- `APPROVED`
- `APPROVED_WITH_CONDITIONS`
- `REJECTED`
- `NOT_REQUIRED`

“Probably allowed” is not a status. Sol maintains the status matrix and blocks release on required `UNASSESSED` or `REQUESTED` items.
