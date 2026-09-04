# Project Context

## Mission

Build a practical, fully local laptop assistant that runs one recent, highly capable quantized language model from weights already stored on a restricted Dell Windows laptop. The repository owns the runtime, local server, assistant loop, state and cache lifecycle, tool execution, safety controls, packaging, and test evidence. The product is a general laptop assistant rather than only a coding assistant.

The intended user experience is:

- Copy an approved application ZIP and a separately approved model artifact onto the laptop.
- Point the launcher to the verified local GGUF.
- Start a foreground local process with no installation.
- Use a local UI or API with streaming output.
- Reuse model prefix and conversation state aggressively but within explicit memory limits.
- Invoke local tools through validated structured calls.
- Invoke web/browser tools only when network access and an approved provider are enabled.
- Stop cleanly without leaving a service, daemon, scheduled task, or resident cloud dependency.

## Target environment extracted from the supplied restriction material

### Confirmed operating environment

- Dell Windows x64 / AMD64 laptop.
- Intel GPU vendor confirmed by the user; exact SKU/generation remains unknown.
- CPython 3.12.10 installed.
- Node.js 24.18.0 and npm 11.16.0 installed.
- AWS CLI, Git, GitHub CLI, and CMake present.
- Fourteen logical processors observed.
- Total physical memory approximately 31.46 GiB.
- Available physical memory approximately 17.35 GiB during the recorded probe.
- Free workspace storage approximately 111.44 GiB.
- Small probes for memory mapping, disposable child processes, temporary-file lifecycle, loopback sockets, NumPy import, and ONNX Runtime import passed.
- ONNX Runtime exposed CPU and Azure execution providers. This is not evidence of a usable local AI accelerator and is not permission for cloud inference.

### Confirmed missing or unavailable tooling

- PyTorch, Transformers, Safetensors, and Tokenizers were not available on the target.
- Docker and Podman were not found.
- GCC, Clang, MSVC `cl`, Cargo, and other local native build toolchains were not found.
- `nvidia-smi`, CUDA, and `nvcc` were not found.
- Public package registries and public model hubs cannot be assumed reachable or permitted.

### Policy implications

- Presence of software does not establish approval.
- New workstation software may require formal approval.
- Docker Desktop may be blocked; the MVP cannot depend on it.
- Some managed endpoints may block batch scripts, so PowerShell and direct executables are required.
- A Windows service, scheduled task, startup item, or daemon is outside the MVP.
- Network serving beyond loopback requires separate security approval and is not in scope.
- Model weights may be sensitive intellectual property. Source, license, transfer route, encryption, DLP, malware scanning, storage, and backup behavior require explicit acceptance.
- Credentials, tokens, model secrets, and credential files must never be committed, printed, or logged.
- A company-approved repository and transfer path are required.

## Fixed product decisions

- **One deployable model:** Qwen3.5-9B.
- **Quantization:** Q4_K_M GGUF generated in a controlled Shadeform build from approved official source weights.
- **MVP modality:** text only; do not ship a vision projection.
- **No automatic model download.**
- **No target runtime dependency on Hugging Face, Python ML frameworks, Ollama, LM Studio, Docker, public npm, or public PyPI.**
- **Repository-owned engine API and assistant host.**
- **Pinned, vendored llama.cpp/ggml substrate for the first release.**
- **Model file lives outside Git and outside the application ZIP.**
- **Default context: 8,192 tokens.**
- **Maximum MVP context: 16,384 tokens after evidence.**
- **Default mode: thinking disabled.**
- **Deep mode: thinking enabled with a separate bounded reasoning/output budget.**
- **CPU backend always ships.**
- **Vulkan is the primary Intel GPU candidate.**
- **SYCL is experimental until promoted by evidence.**
- **Formal conversion, build, and test execution occurs on Shadeform.**
- **Target activity is limited to hardware probe, transfer, checksum/model verification, and final acceptance.**

## Why the model changed

The initial Qwen3-8B choice was conservative. Qwen3.5-9B offers a more recent training/post-training stack, stronger general reasoning, instruction following, coding, multilingual, and agent/tool performance, while remaining in a weight-size class that leaves useful headroom inside the observed 17.35 GiB available-memory window.

A still larger 20B–27B model is not the default because nominal fit is not enough. The model must coexist with Windows, an Intel GPU that may use shared system memory, graph buffers, recurrent state, KV cache, the browser, the tool host, file operations, and normal laptop workloads. A model that barely loads but causes paging or leaves no tool/browser margin is a failed laptop-assistant design.

## Qwen3.5-9B implementation implications

Qwen3.5-9B is a hybrid model:

- 9B language-model parameters.
- 32 layers.
- A repeating pattern of three Gated DeltaNet blocks followed by one gated-attention block.
- Eight full-attention layers in the 32-layer pattern.
- Multi-token prediction training.
- Native tool/agent behavior and configurable thinking behavior.

This affects engineering:

1. Conversation state includes fixed-size recurrent/DeltaNet state plus attention KV state.
2. Reset, cloning, cancellation, and session switching must include both state classes.
3. Dense-transformer cache formulas are not valid for the entire model.
4. Backend correctness must be tested over short prompts, long prompts, repeated turns, cache reuse, and state resets.
5. MTP-assisted speculative decoding is optional after the base path is correct.

## Hardware interpretation

The exact Intel GPU is still unknown. Therefore:

1. CPU inference establishes correctness and always remains available.
2. Phase 0 captures Dell model, CPU, exact Intel adapter, device/PCI ID, driver, dedicated/shared memory, UMA status, DirectX/Vulkan capability, and power profile.
3. The build matrix includes CPU and Vulkan. SYCL is a research/experimental lane.
4. The launcher chooses only a profile explicitly approved for the hardware receipt.
5. The launcher never downloads a driver, guesses an offload amount, or treats vendor name as sufficient evidence.
6. Partial offload is allowed when full offload is unsafe or unhelpful.
7. A GPU backend is rejected when it is faster but produces any unexplained corruption, hangs, state leakage, or instability.

## Functional scope for the MVP

### Core assistant

- Multi-turn text chat.
- Streaming output.
- Session creation, reset, cancellation, and bounded history.
- Explicit fast and deep modes.
- Local system prompt and compact, dynamically selected tool descriptions.
- Clear errors for model, memory, context, backend, tool, and network failures.
- OpenAI-compatible chat-completions subset for local integrations.
- A repository-specific event stream for tool selection, confirmation, execution, and results.

### Local tools

- Read-only system information.
- Time and local environment information.
- List/read files inside configured workspace roots.
- Search text within permitted files.
- Write a new file or apply an atomic patch inside permitted roots, with confirmation as required.
- Open an allowlisted application.
- Open a policy-checked URL in the default browser.
- Read/write clipboard under explicit policy.
- Run an allowlisted executable using executable-plus-argument-array, timeout, output cap, and confirmation.
- Optional web search and page fetch through a configured approved provider.

“Full tool calling” means the complete model → schema → policy → confirmation → execution → result → model loop is implemented. It does not mean unrestricted shell, unrestricted filesystem, hidden network egress, or arbitrary desktop control.

### Deferred from the first MVP

- Vision projection and screenshot understanding.
- Audio/speech.
- Pixel-based autonomous computer use.
- Multi-user or LAN serving.
- Background services.
- Remote MCP servers.
- Automatic software/model updates.
- Training or fine-tuning on the target.
- Multiple model selection.
- High-concurrency serving.
- Unrestricted shell access.
- Unattended destructive actions.

A Windows UI Automation adapter may be added after deterministic tools are stable. It should use accessibility trees and explicit confirmation, not unbounded coordinate clicking.

## Performance intent

This is a single-user, latency-oriented product. Optimize in this order:

1. Correctness and stable state transitions.
2. Useful warm decode speed.
3. Reasonable first-token latency.
4. Prompt-prefix and system/tool-prefix reuse.
5. Stable memory under repeated turns.
6. Predictable cancellation and recovery.
7. Low-overhead tool loops.
8. CPU fallback.
9. GPU acceleration only after correctness.

No worker may call performance “good” from a single microbenchmark. Performance must be demonstrated in end-to-end assistant flows and under realistic memory pressure.

## Memory intent

The practical planning limit is the observed approximately 17.35 GiB available-memory window, not the approximately 31.46 GiB physical maximum.

### Initial 8K budget

| Component | Planning allowance |
|---|---:|
| Qwen3.5-9B Q4_K_M mapped weights | expected approximately 5–6 GiB; exact value from build manifest |
| Attention KV at 8K, F16 planning estimate | approximately 0.25 GiB |
| Fixed recurrent/DeltaNet state | measured, not guessed; budget initially up to 0.75 GiB including backend state |
| Graph, scratch, allocator, and staging buffers | target at or below 2.5 GiB |
| Prefix/session cache beyond active state | 0.5 GiB soft cap |
| Host, UI, tool process, metadata/logs | at or below 0.75 GiB typical |
| Driver/shared-memory uncertainty reserve | at least 2.0 GiB |
| System/application headroom | at least 4.0 GiB from the observed available window |
| Product hard commit target | at or below 12.0 GiB for default 8K profile |

The attention-KV estimate counts only the eight attention layers implied by the published block pattern and excludes fixed recurrent state and allocator overhead. All release decisions use measurements rather than this estimate.

At 16K, the attention KV estimate approximately doubles. The 16K profile is accepted only when repeated-turn tests leave adequate Windows/application headroom and avoid sustained paging.

## Success definition

The MVP is successful when an approved portable release can be copied to the Dell laptop, pointed to the verified Qwen3.5-9B Q4_K_M file, launched without installation or Internet, and used for local multi-turn chat and safe local tools. When an approved network provider is configured, the same assistant must complete a demonstrated web-assisted tool loop without moving model inference to the cloud. The release must carry reproducible Shadeform evidence for model provenance, quantization quality, runtime parity, Intel backend correctness, latency, throughput, memory, security, offline operation, and soak stability.
