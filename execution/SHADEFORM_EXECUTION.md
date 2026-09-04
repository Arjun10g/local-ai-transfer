# Shadeform Execution Plan

## 1. Purpose

All formal development tests, benchmarks, model-quality evaluations, security campaigns, and release builds run through Shadeform. The target laptop is not a build machine and is not used for broad experimentation.

Shadeform is used to provide:

- Reproducible remote build environments.
- CPU and GPU profiles selected to approximate the target.
- Approved model/source staging.
- Pinned reference execution.
- Performance and memory evidence.
- Windows cross-build or native Windows tests when available.
- Short-lived, auditable jobs.
- Cost/hour controls and cleanup.

This document does not assume a particular current Shadeform SKU or price. Instance inventory and rates change. Luna B records the available profile and Sol approves the spend at execution time.

## 2. Profile strategy

### Profile A — CPU correctness analog

Selection criteria:

- x86-64.
- 16 vCPU preferred, with a fallback 12–16 vCPU range.
- 32 GiB RAM.
- Local SSD with at least 30 GiB free.
- Modern AVX2/FMA capable CPU if target probe supports it.
- Linux acceptable for core/backend correctness and performance characterization.
- Windows preferred when available for native process/filesystem behavior.

Uses:

- Pinned oracle.
- Native engine correctness.
- Token/logit parity.
- CPU performance.
- Memory and low-memory tests.
- Offline tests.
- Long soak.

### Profile B — Lowest-cost Intel-target acceleration analog

The vendor is known to be Intel, but the exact Dell adapter is not. Select only after the target receipt when possible. Match:

- Intel family/generation and integrated-versus-discrete topology where Shadeform inventory permits.
- Dedicated or shared-memory class.
- Minimum VRAM needed to test full/partial Q4 offload.
- Driver/API family, with Vulkan as the primary lane and SYCL only as an experimental lane.
- 32 GiB host RAM.
- Similar display/shared-memory constraints where possible.

Uses:

- Backend bring-up.
- CPU versus Intel-backend parity and hybrid-state correctness.
- Layer offload.
- Flash Attention/cache type.
- TTFT/decode.
- GPU memory headroom.
- Driver/device failure, device-loss, output-corruption, long-prompt→short-prompt, and silent-fallback tests.

When Shadeform has no sufficiently similar Intel GPU, do not substitute a non-Intel GPU and call it target-like. Use CPU for correctness, a non-Intel accelerator only for backend-independent orchestration tests, and reserve Intel performance/promotion claims for an approved Intel environment plus the final bounded Dell acceptance receipt.

### Profile C — Secondary GPU differential

Use only when needed:

- One adjacent GPU/VRAM class.
- Used to determine whether behavior is device-specific.
- Short bounded runs.
- Not a permanent test profile unless the product supports both.

### Profile W — Windows validation

Preferred order:

1. Shadeform native Windows x64 profile matching target class.
2. Approved Windows CI worker integrated into the same evidence process.
3. Cross-compiled Windows binary plus Wine for broad startup/API behavior.
4. Final target acceptance check for actual Windows/device semantics.

Linux or Wine evidence must never be described as equivalent to native Windows for reparse points, job objects, clipboard, browser launch, or driver behavior.

## 3. Target matching procedure

Luna B produces `hardware-receipt.json`. Sol and Luna D select profiles using a weighted match:

| Dimension | Weight |
|---|---:|
| Intel GPU generation/API/device class | 30% |
| GPU memory class/shared-vs-dedicated | 20% |
| CPU ISA/core topology | 15% |
| Host RAM | 15% |
| OS/driver family | 15% |
| Storage behavior | 5% |

Record each mismatch. Performance claims use:

- “Exact/near match” only when supported.
- “Directional analog” when materially different.
- Relative-to-reference metrics on each profile.
- Final target receipt for actual acceptance.

## 4. Remote repository and artifact flow

### Source

- Checkout the approved repository at an immutable commit.
- Verify clean tree.
- Obtain vendored third-party source through an approved mirror or immutable artifact.
- Verify `third_party/manifest.lock`.
- Do not pull a floating branch during a test.
- Record source and subcomponent hashes.

### Model

- Stage the pinned official Qwen3.5-9B source revision in an approved restricted source cache.
- Run the controlled text-only conversion and Q4_K_M quantization job once per accepted tool revision.
- Generate the exact GGUF SHA-256, byte count, metadata digest, tensor-inventory digest, license/model-card archive, and manifest.
- Stage the accepted Q4_K_M plus evaluation-only high-precision/Q8 comparator in content-addressed storage.
- Verify generated hashes and manifests before every job mount.
- Mount/read it read-only into jobs where possible.
- Never bake the model into a general image or release ZIP.
- Restrict access and lifecycle according to model policy.
- Do not repeatedly download from a public hub for each job.

### Results

Store:

```text
<artifact-root>/<project>/<build-id>/
  source-manifest.json
  machine.json
  source-model-receipt.json
  conversion-receipt.json
  model-receipt.json
  commands.txt
  environment-redacted.json
  tests/
  benchmarks/
  quality/
  security/
  package/
  checksums.sha256
  summary.md
```

Large raw files live in approved artifact storage. Commit only the evidence index and immutable references.

## 5. Job classes

### J0 — Probe and bootstrap

- Machine inventory.
- Backend/device enumeration.
- Compiler/runtime versions.
- Storage and memory.
- No model load.


### J1 — Clean application build

- Fresh checkout.
- Dependency verification.
- CPU build.
- Accelerated build where profile supports it.
- Windows cross-build/native build.
- Unit tests.
- Package skeleton.


### J1M — Controlled model artifact build

- Acquire the pinned official Qwen3.5-9B source revision.
- Archive model card and Apache-2.0 license.
- Convert the text-language component to a high-precision GGUF.
- Produce a Q8_0 comparator when practical.
- Quantize the deployable Q4_K_M artifact.
- Validate Qwen3.5 structural metadata, tokenizer/template, text-only mode, and smoke inference.
- Generate exact manifests, checksums, tensor inventories, and scan receipts.
- Publish only to approved content-addressed model storage.

### J2 — Reference oracle

- Depends on an accepted J1M artifact receipt.
- Pinned upstream build.
- Model verification.
- Deterministic fixtures.
- Baseline memory/performance.
- Output artifact.


### J3 — Product correctness

- Real engine.
- Tokenizer/template parity.
- Deterministic model tests.
- API/session/cache/cancel.
- CPU versus Intel-backend parity and hybrid-state correctness.


### J4 — Tool/security integration

- Host/UI.
- Local tools.
- Prompt injection.
- Path/process/network policy.
- Mock provider.
- Optional live provider with synthetic data.


### J5 — Performance matrix

- Fixed corpus.
- Cold/warm.
- Prefill/decode.
- 4K/8K/optional16K.
- Cache hit/miss.
- CPU thread/batch plus Intel Vulkan partial/full offload, Flash Attention, cooperative-matrix, cache, and batch configurations.
- SYCL only as an explicitly labeled experimental comparison.
- Actual operator placement and silent-fallback detection.
- Repetitions and raw metrics.


### J6 — Fuzz/soak/offline

- Parser/API/schema fuzz.
- Network-disabled integration.
- 30–60 minute soak.
- Fault injection.
- Leak/orphan monitoring.


### J7 — Release

- Two clean builds.
- Windows package.
- Dependency scan.
- SBOM/notices/scans.
- Full gate suite.
- Demo.


## 6. Reproducible job interface

Each job should be invokable through one versioned script, for example:

```text
scripts/shadeform/run-job.ps1 -Job J3 -BuildId <id> -Profile <profile>
```

or a platform-appropriate direct command. The script:

- Fails on unknown arguments.
- Prints no secrets.
- Writes all effective non-secret configuration.
- Verifies source/model before work.
- Applies explicit timeouts.
- Captures exit status.
- Uploads/indexes artifacts.
- Cleans temporary credentials/state.
- Stops the instance on success or failure.

The underlying commands remain documented so a wrapper is not a black box.

## 7. Cost control

Until live instance rates are supplied, manage **hours**, not invented dollar prices.

Initial planning caps:

| Resource | Soft warning | Hard pause |
|---|---:|---:|
| CPU profile hours | 120 | 200 |
| Primary GPU hours | 60 | 120 |
| Secondary GPU hours | 12 | 24 |
| Windows validation hours | 20 | 40 |
| Retained model/storage | One approved artifact | Sol approval for duplicate |

Rules:

- S4 reports cumulative use at 50%, 80%, and 100% of soft cap.
- Sol must approve crossing a hard pause.
- Stop idle instances automatically.
- Use model/build caches but invalidate correctly.
- Run small correctness before large performance matrices.
- Use successive halving for tuning: cheap screen, then confirm finalists.
- Do not leave a GPU active while reviewing results.
- Store raw results and terminate compute.
- Record current rate and estimated dollar impact when provisioning.

A later plan may replace these caps after empirical iteration speed is known.

## 8. Tuning workflow

Avoid combinatorial waste.

### Step 1 — Feasibility screen

Test a small set:

- CPU threads: physical-core estimate, logical-minus-two, logical-all.
- Batch: 128, 256, 512 where memory permits.
- KV: correctness baseline and one candidate quantized cache.
- Backend/offload: CPU, Intel Vulkan conservative partial, Vulkan maximum safe, and experimental SYCL only when available.
- Flash Attention and cooperative-matrix features: introduce one at a time only after baseline correctness.
- Context: 4K and 8K.

Use short fixed prompts and repeated 128-token decode.

### Step 2 — Candidate selection

Select Pareto candidates on:

- TTFT.
- Decode rate.
- Peak commit/VRAM.
- UI responsiveness.
- Correctness.
- Quality.

Discard configurations that improve throughput but violate memory or latency.

### Step 3 — Confirmation

Run finalists on the full corpus with randomized scenario order and enough repetitions for stable intervals.

### Step 4 — Contention

Run selected defaults with ordinary background CPU/memory load and conservative GPU display headroom.

### Step 5 — Freeze

Store profile keyed by hardware/backend/build signature. Any signature change triggers validation.

## 9. Windows build approach

Because the target has no compiler:

- Build remotely.
- Prefer a self-contained C/C++ runtime.
- Produce baseline and accelerated binaries as needed.
- Precompile Vulkan shaders.
- Do not require Vulkan SDK on target.
- Inspect imported DLLs.
- Test paths containing spaces and Unicode.
- Test non-admin user.
- Test read-only application directory with writable app-data directory.
- Test PowerShell launcher and direct executable commands.
- Do not recommend execution-policy bypass.

If using cross-compilation:

- Record toolchain.
- Run under Wine only for supported behaviors.
- Mark Windows-specific tests pending until native Windows/target acceptance.
- Do not rely on Wine for driver/GPU results.

## 10. Offline test setup

Proof requires network isolation external to the process:

- Disable outbound networking in the VM/security group/network namespace.
- Clear DNS/proxy/provider environment.
- Start from fresh app state.
- Observe sockets/DNS attempts.
- Run model load, chat, local tools, UI.
- Invoke web search and verify typed failure.
- Verify no delay caused by update/hub/provider calls.
- Archive network-observation evidence.

The native engine should have no network client code beyond local server sockets.

## 11. Live web-provider test

Only after approval:

- Inject provider config at runtime.
- Use synthetic/non-sensitive queries.
- Confirm provider endpoint and data disclosure in UI.
- Capture tool events and sources.
- Remove credentials after job.
- Redact artifacts.
- Run provider failure/timeouts/oversize.
- Do not use the provider as an inference service.
- Do not persist queries beyond the approved test policy.

## 12. Release-to-benchmark identity

The benchmarked candidate and packaged release must share:

- Source commit.
- Backend source revision.
- Compiler/flags.
- Model hash expectation.
- Default config.
- Tool/prompt/schema versions.
- CPU and exact promoted Intel backend build/profile.
- Build ID.

S4 must reject a package assembled from a different binary than the final benchmark.

## 13. Cleanup checklist

Every job, including failure:

- Stop child processes.
- Remove temporary tokens/credentials.
- Remove unredacted logs.
- Unmount/detach model where appropriate.
- Upload evidence/checksums.
- Terminate instance.
- Confirm no retained public IP/service.
- Record compute duration.
- Update cumulative budget.
- Preserve only approved caches/artifacts.

## 14. What Shadeform cannot prove alone

Shadeform may not prove:

- Actual company policy approval.
- Exact target GPU/driver behavior when profile differs.
- Target endpoint-management restrictions.
- Browser/app/clipboard behavior on the managed laptop.
- Real local available memory during ordinary work.
- Target network/proxy/provider reachability.
- Final operator permissions.

These are resolved by approvals and the bounded target acceptance check, not by overstating remote results.
