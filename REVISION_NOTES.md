# Revision Notes — Intel GPU and Newer Model Update

## Revision purpose

This revision replaces the original Qwen3-8B model decision with a newer and materially stronger local model while preserving the five-session execution structure and the restricted-laptop deployment requirements.

## Changed decisions

- **New fixed model:** Qwen3.5-9B.
- **MVP weight artifact:** project-produced text-only GGUF at `Q4_K_M`, generated on Shadeform from an approved, pinned copy of the official Qwen3.5-9B weights.
- **Default context:** 8,192 tokens.
- **Maximum MVP context:** 16,384 tokens only after target-memory and correctness gates pass.
- **Default behavior:** non-thinking/fast assistant mode.
- **Deep behavior:** opt-in, bounded thinking mode.
- **Primary Intel GPU candidate:** Vulkan.
- **Mandatory fallback:** CPU.
- **SYCL status:** experimental only until it beats Vulkan or CPU and passes the full correctness/soak suite on the exact Dell device.
- **Vision status:** deferred. The MVP transfers only the language GGUF and does not transfer or load a vision projection artifact.
- **Model supply chain:** no community quant is trusted merely because it is convenient. The agents produce, hash, evaluate, and attest the accepted GGUF themselves on Shadeform.

## Why Qwen3.5-9B rather than a still larger or newer model

Qwen3.5-9B is the strongest model in the current candidate set that still leaves a safe system-memory margin on the observed target. The recorded laptop has approximately 31.46 GiB of physical RAM but only approximately 17.35 GiB available during the probe. Because an Intel integrated GPU may borrow system memory, model fit must be judged against the available-memory window, not nominal installed RAM.

Qwen3.6-35B-A3B activates only about 3B parameters per token, but all 35B expert weights still have to be resident or streamed; a 4-bit representation alone is already near or above the recorded available-memory window before quantization metadata, state, buffers, Windows, the browser, and tools. Qwen3.8-27B has the same practical residency problem. Gemma 4 12B is newer and attractive, but current Intel Arc backend reports include correctness failures on long prompts. The MVP therefore uses Qwen3.5-9B and makes backend correctness a first-class release gate.

## Files materially updated

- `README.md`
- `AGENTS.md`
- `CONTEXT.md`
- `governance/MODEL_DECISION.md`
- `governance/INTEL_GPU_BACKEND.md`
- `governance/ARCHITECTURE.md`
- `governance/RISK_REGISTER.md`
- `governance/SECURITY_AND_TOOL_POLICY.md`
- `execution/PHASES.md`
- `execution/TASK_BOARD.md`
- `execution/TEST_AND_BENCHMARK_PLAN.md`
- `execution/ACCEPTANCE_CRITERIA.md`
- all five session prompts
- `templates/MODEL_MANIFEST.md`

## Required first action after launch

The Sol orchestrator must not immediately start optimization. It first assigns the target hardware receipt task. The exact Dell model, CPU, Intel GPU SKU, driver, dedicated/shared memory, Vulkan feature set, and power profile must be captured before the accelerated binary is selected.
