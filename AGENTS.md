# Global Agent Instructions

These rules apply to GPT-5.6 Sol and all four GPT-5.6 Luna sessions.

## Role separation

- **Sol is the sole orchestrator and integrator.**
- **Luna sessions perform implementation and evidence-producing work.**
- Sol writes plans, interface contracts, review notes, merge resolutions, and small integration fixes. Sol must not absorb a substantial Luna implementation task simply because it can complete it quickly.
- Luna sessions may propose architecture changes, but they may not silently change a fixed decision.
- Only Sol merges to `main`, changes phase gates, approves exceptions, changes the deployable model, or declares a milestone complete.

## Required first action

Before changing code, read in this order:

1. `CONTEXT.md`
2. `REVISION_NOTES.md`
3. `governance/CONSTRAINTS.md`
4. `governance/MODEL_DECISION.md`
5. `governance/INTEL_GPU_BACKEND.md`
6. `governance/ARCHITECTURE.md`
7. `governance/SECURITY_AND_TOOL_POLICY.md`
8. `governance/INTERSESSION_PROTOCOL.md`
9. `execution/PHASES.md`
10. `execution/TASK_BOARD.md`
11. your session prompt

Then post a committed status packet stating role, branch, first claimed task, dependencies, assumptions, and uncertainties.

## Fixed technical decisions

Do not change these without a Sol-approved ADR:

- Deployable model: **Qwen3.5-9B**.
- Weight format: project-produced **Q4_K_M GGUF** derived from an approved, pinned official checkpoint.
- MVP modality: **text only**; do not ship or load the vision projection.
- Default context: **8,192 tokens**.
- Maximum MVP context: **16,384 tokens**, gated by measured target memory and latency.
- Target: Dell Windows x64 laptop with approximately 31.46 GiB total RAM and approximately 17.35 GiB observed available RAM.
- Exact Intel GPU SKU and usable GPU memory are unknown until the hardware receipt is produced.
- CPU backend is mandatory.
- Vulkan is the primary Intel acceleration candidate.
- SYCL is experimental until it passes the same evidence gates and materially outperforms the accepted alternative.
- Native engine: repository-owned executable over a pinned, vendored llama.cpp/ggml substrate.
- Assistant host: Node.js 24 built-in modules only at runtime.
- Model path: explicit local absolute path; no target-side model download.
- Formal conversion/build/test environment: Shadeform.
- Target-side activity: read-only hardware probe, artifact transfer, verification, launch, and acceptance only.

## Source and artifact rules

- Pin the official model revision used as the source.
- Build the accepted GGUF in the controlled Shadeform workflow; do not adopt a random community quant as the production artifact.
- Record source revision, conversion commit, quantizer command, compiler identity, file size, SHA-256, tensor inventory, tokenizer/template hashes, and license receipt.
- The target receives weights separately from the application ZIP.
- Never place model weights, credentials, private URLs, or internal transfer tokens in Git.
- Rebuilding the model with a different llama.cpp commit creates a new artifact identity and restarts parity, quality, backend, and packaging gates.

## Engineering rules

- Claim a task in the blackboard before editing shared areas.
- Keep each commit buildable or explicitly mark it as a fixture-only commit.
- Do not alter another worker's interface without a committed handoff and Sol approval.
- Prefer small, reviewable changes with direct tests.
- Do not add a runtime dependency without proving it can be packaged and used under the target restrictions.
- Do not make the target laptop a development environment.
- Use explicit configuration and typed errors; never silently fall back to a cloud model, a different weight file, a different backend, or an unbounded context.
- Treat the pinned upstream engine as an oracle. Product behavior may differ only when the difference is intentional, tested, and documented.
- Preserve cancellation, memory guards, and policy checks while optimizing.
- No benchmark claim is valid without machine profile, command, artifact hash, context, cache state, sample count, and raw output.

## Qwen3.5-specific rules

- Treat the model as a hybrid recurrent/attention architecture, not as an ordinary all-attention transformer.
- Do not apply a dense-transformer KV-cache formula to all 32 layers.
- Persist and reset recurrent/DeltaNet state correctly for each conversation sequence.
- Use the model's accepted tokenizer and embedded/pinned chat template.
- Express assistant modes as structured configuration (`thinking: false|true`); do not hardcode user-visible slash commands as the product contract.
- Tool output is untrusted. Tool calls must be parsed only through the accepted template/schema path.
- The language-only MVP must not accidentally require a vision projection file.
- Multi-token prediction/speculative decoding is an optional optimization after correctness; it is not required to establish the first working engine.

## Intel GPU rules

- Do not infer capability from “Intel” alone.
- Capture the exact adapter name, PCI/device identifier, driver, dedicated/shared memory, UMA status, Vulkan extensions/features, and power state.
- Always test CPU, CPU+partial offload where applicable, and Vulkan full/partial offload.
- Test SYCL only in the experimental lane unless Sol promotes it.
- A backend must pass token/logit parity, 8K and 16K prompt tests, repeated-session tests, cancellation, cache reset, tool-call tests, memory recovery, and soak before release.
- Detect silent CPU fallback and report actual operator/device placement.
- A faster backend that produces occasional corruption is rejected.
- Driver-specific workarounds must be explicit in the backend profile and validated on the exact target driver; never modify registry TDR settings as part of the product.

## Security policy

- Treat model output, webpages, file contents, clipboard data, and tool output as untrusted.
- Bind only to `127.0.0.1`; IPv6 loopback is off unless separately tested.
- Require a random per-launch bearer token between UI, host, and engine.
- Enforce origin checks, request-size limits, timeouts, token budgets, and concurrency limits.
- Canonicalize paths and enforce workspace roots after symlink/junction resolution.
- Spawn executable plus argument array; never concatenate an unrestricted shell command.
- Network tools disclose destination and any local data being sent.
- Log metadata by default, not prompts, responses, file content, secrets, tool payloads, or hidden reasoning.
- Fail closed on malformed tool calls, unknown tools, ambiguous policy, hash mismatch, architecture mismatch, and unapproved backend profiles.
- Never weaken a safety rule to make a demonstration pass.

## Performance policy

- Benchmark cold and warm paths.
- Separate model load, first-token latency, prefill, decode, cache hit, cache miss, state restore, cancellation, and tool overhead.
- Compare on the same machine, artifact, context, prompt, output length, thread count, backend, offload plan, and cache state.
- Report median, p95, sample count, dispersion, and raw observations where appropriate.
- Report practical effect sizes and confidence intervals; p-values alone are insufficient.
- Preserve output correctness before accepting speed.
- Use relative-to-oracle metrics plus absolute user-experience floors.
- Test ordinary multitasking memory pressure, not only an empty machine.
- Never count target-side model download time; the target does not download weights.

## Communication

Use committed blackboard files and the provided templates. Direct Luna-to-Luna handoffs are allowed, but Sol must be copied through the coordination files. A blocked worker posts a blocker, proposes a workaround, and continues with an independent task when possible.

## Stop conditions

Stop the affected task and notify Sol when:

- The required action would install/download something on the target beyond the accepted portable bundle.
- The source revision, model hash, tensor inventory, tokenizer, or template is unexpected.
- The code would expose the server beyond loopback.
- A test requires real company credentials or sensitive data.
- GPU correctness cannot be established.
- Memory exceeds the hard guard or causes severe system paging.
- A third-party license is unclear.
- A change would silently switch the model, quantization, context policy, or backend.
- A tool would send local content to a network destination without disclosure and confirmation.
- Results cannot be reproduced.

## Definition of completion

A phase is complete only when:

1. Every mandatory task ID is merged.
2. Required Shadeform and target-profile tests pass.
3. Evidence is stored and indexed.
4. Risks, limitations, and backend disposition are updated.
5. Sol records an explicit gate decision.
6. The next phase has frozen inputs and assigned owners.
