# Delivery Phases

## Program shape

The work is organized into nine phases and seven visible milestones. Four Luna sessions operate in parallel under Sol. The plan is intentionally vertical-slice driven: every phase should leave the repository in a runnable, testable state.

Execution is **gate-driven rather than date-driven**. Contract, artifact, hardware-receipt, reference-runtime, and security-fixture work starts in parallel. GPU optimization never blocks the CPU correctness path. Sol publishes completed evidence, active blockers, and the next release gate rather than compressing safety or correctness work to match a calendar estimate.

| Phase | Theme | Primary milestone |
|---|---|---|
| 0 | Constraint freeze, hardware/model approvals, contracts | M0 |
| 1 | Reproducible scaffolding and pinned reference | M1 |
| 2 | First real local-model vertical slice | M2 |
| 3 | Stable chat/session host and local tools | M3 |
| 4 | Model-driven tool calling and web-provider path | M4 |
| 5 | Caching, acceleration, memory, and latency | M5 |
| 6 | Security, offline, soak, and failure hardening | M6 candidate |
| 7 | Portable release and full Shadeform demonstration | M6/M7 |
| 8 | Target acceptance and handoff | Final acceptance |

## Cross-phase rules

- No phase begins with unfrozen consumer-facing contracts.
- No optimization merges without correctness and memory comparison.
- No live Internet tool test uses real company data.
- No target-side install/download/build is added to unblock a phase.
- Every phase ends with an explicit Sol gate decision.
- Mock/fixture completion and real-model completion are labeled separately.
- CPU fallback remains green throughout.
- One model remains in scope.
- The target GPU profile is selected only after hardware evidence.
- Large tasks are split into reviewable vertical commits.

---

# Phase 0 — Constraint, Approval, and Contract Freeze

## Objective

Turn the restriction material and project intent into executable constraints, eliminate hidden assumptions, collect exact hardware evidence, and freeze the first interfaces so all four Luna sessions can work in parallel.

## Sol

- Create coordination structure, branches, worktrees, and task claims.
- Record the revision ADR fixing Qwen3.5-9B, the controlled text-only Q4_K_M build, and the product architecture.
- Establish policy status matrix.
- Assign Phase 0 tasks.
- Freeze definitions of MVP, offline, full tool calling, live search, and target acceptance.
- Decide the model/source/backend acquisition review path.
- Select the first Shadeform profiles based on matching criteria.
- Record open questions and owners.

## Luna A — Runtime

- Define backend adapter boundary.
- Define engine API, lifecycle, error, session, and cancellation semantics.
- Inventory Windows runtime DLL/static-link requirements.
- Implement a tiny bounded GGUF metadata fixture reader or specification.
- Propose pinned backend-source integration procedure.
- Identify any upstream surface that should not be exposed.

## Luna B — Model/Performance

- Finalize the Qwen3.5-9B source, conversion, quantization, text-only modality, manifest, and integrity expectations.
- Implement the read-only Dell/CPU/Intel GPU/Vulkan/SYCL hardware receipt defined in `governance/INTEL_GPU_BACKEND.md`.
- Define model oracle pin/run methodology.
- Produce initial memory calculation and resource profiles.
- Define model-quality and tool-call evaluation.
- Propose CPU plus Intel integrated/discrete Vulkan analog criteria and explicitly label gaps where Shadeform cannot match the target.
- Identify exact approval-dependent features.

## Luna C — Agent/Tools

- Define tool envelope, assistant events, risk tiers, confirmation, and config schema.
- Build a fixture engine contract.
- Define first tool set and provider abstraction.
- Define network/offline behavior.
- Inventory Windows file/app/clipboard/process mechanisms that do not require installation.
- Define static UI scope.

## Luna D — QA/Release

- Create machine/build/evidence schemas.
- Create contract-conformance skeleton.
- Create initial threat model and adversarial test inventory.
- Define clean-build and portable-package gates.
- Establish Shadeform run cleanup/budget rules.
- Define model/runtime/weights scan and SBOM expectations.

## Outputs

- `coordination/` live.
- ADR-0001 model/architecture accepted.
- Policy matrix.
- Hardware receipt or explicit pending-target probe state, including the user-reported Intel GPU as an unresolved exact-SKU item.
- Model manifest draft.
- Contracts v0.1.
- Shadeform profile plan.
- Baseline test plan and risk register update.
- Approved source-acquisition process or a recorded blocker.

## Exit criteria

- All fixed decisions are recorded.
- No session is waiting for an undefined interface.
- Exact Dell/Intel GPU details may remain pending only if the target probe task, owner, deadline, and mandatory CPU fallback are recorded.
- Model/source/binary approval status is explicit.
- Model file is not on the target through an unapproved route.
- Contracts are sufficient for S1/S3 mocks.
- S4 can run initial conformance tests.
- Sol records `PASS` or `CONDITIONAL_PASS`.

---

# Phase 1 — Reproducible Scaffolding and Pinned Reference

## Objective

Create a clean repository, reproducible remote build, fixed upstream reference, fixture-based engine/host vertical slice, and portable Windows package skeleton.

## Parallel lanes

### Luna A

- Create CMake/native layout.
- Integrate the approved immutable llama.cpp/ggml source behind the adapter.
- Build `lae-engine` skeleton.
- Implement `version`, `print-build-info`, `probe`, health/readiness.
- Bind loopback with local auth fixture.
- Implement stable error JSON.
- Build Linux/native and Windows cross-target artifacts in Shadeform.

### Luna B

- Acquire the approved official Qwen3.5-9B source revision on Shadeform and generate the controlled text-only high-precision/Q8 and Q4_K_M artifacts.
- Generate and verify exact artifact hashes, byte counts, GGUF metadata, tensor inventory, tokenizer, chat template, license, and provenance receipts.
- Build pinned upstream oracle.
- Capture baseline model-load, CPU output, memory, and performance.
- Freeze tokenizer, chat-template, structured thinking-control, tool-call, recurrent-state, and reset fixtures.
- Run hardware-probe fixture and, if available, target receipt analysis.

### Luna C

- Build `lae-host.mjs` skeleton using built-ins only.
- Implement local auth/bootstrap against fixture engine.
- Implement static UI shell and event stream.
- Implement conversation state machine against mocks.
- Implement strict tool-envelope parser/schema foundation.
- Implement `time.now` fixture tool.

### Luna D

- Build test runner and evidence layout.
- Run clean checkout/build.
- Create contract conformance.
- Create Windows package skeleton and DLL dependency scan.
- Verify target package has no `node_modules`, weights, credentials, or downloads.
- Start HTTP/path/parser negative tests.

## Integration vertical slice

From a fresh Shadeform checkout:

1. Build native fixture engine.
2. Run host without npm install.
3. Open local UI.
4. Send a synthetic fixture prompt.
5. Stream a fixture answer.
6. Execute `time.now` through the canonical tool envelope.
7. Shut down with no orphan process.
8. Produce evidence bundle.

## Outputs

- Source and backend revision pin.
- Reproducible build pipeline.
- Fixture engine and host.
- Pinned real-model oracle.
- Windows portable package skeleton.
- Contracts v0.2.
- Baseline report.
- Controlled model-build receipt and accepted Q4/Q8 artifact identities.
- Initial Intel backend compatibility matrix.

## Exit criteria

- Clean build is reproducible enough to identify every source/dependency.
- Fixture vertical slice passes.
- The pinned upstream oracle runs the exact controlled Qwen3.5-9B Q4_K_M bytes from a local path on Shadeform; the higher-precision/Q8 comparator is separately identified.
- Source revision plus final GGUF size, SHA-256, metadata digest, and tensor-inventory digest are recorded.
- Host runs with Node built-ins only.
- Windows artifact dependency scan identifies no unresolved target requirement.
- No target-side install/download instructions exist.
- All Phase 1 blocker findings have owners.

---

# Phase 2 — First Real Local-Model Vertical Slice

## Objective

Replace the fixture response with real Qwen3.5 inference through the repository-owned engine, establish CPU correctness, and integrate streaming host/UI.

## Luna A

- Implement full model-path verification.
- Load the accepted Q4_K_M artifact through adapter.
- Implement tokenization/chat rendering.
- Implement deterministic prefill/decode and correct Qwen3.5 hybrid attention-KV plus recurrent/Gated DeltaNet state lifecycle.
- Stream real text.
- Implement request limits, EOS/stop/context behavior.
- Implement CPU fallback.
- Add model/load metrics.

## Luna B

- Compare tokenizer, prompt tokens, top tokens/logits, and sequences to the oracle.
- Diagnose backend/config differences.
- Measure 4K/8K model weights, attention KV, recurrent state, optional MTP state, graph/scratch, driver/shared-memory, and host overhead separately.
- Establish CPU performance baseline.
- Confirm template-driven `enable_thinking=false|true` behavior and host-enforced deep-mode budgets.
- Validate published model metadata against actual artifact.

## Luna C

- Integrate host with real engine.
- Implement message rendering and finish/error behavior.
- Implement normal/deep UI control.
- Implement Stop.
- Complete `system.get_info` and `time.now` real end-to-end tools.
- Keep tool selection fixture-driven until Phase 4.

## Luna D

- Independently run real-model correctness.
- Test invalid/tampered model.
- Run HTTP/SSE/auth/cancel basic suite.
- Validate no outbound connection by native engine.
- Check memory guard.
- Run repeated start/stop.

## Required demonstration

- Start from an extracted release tree.
- Provide the model through a local path.
- Verify hash.
- Launch engine/host.
- Ask one general question in normal mode and stream response.
- Ask one bounded deep-mode question.
- Stop a generation.
- Run `system.get_info`.
- Exit cleanly.

## Outputs

- M2 real-model engine.
- CPU correctness and hybrid-state lifecycle report.
- 4K/8K component-level memory table.
- Integrated UI.
- Model validation receipt.
- First package containing real engine but no weights.

## Exit criteria

- Real response flows through product engine and host.
- Deterministic CPU output meets oracle tolerance.
- Model path/hash failure is safe.
- Default 8K profile stays under planning memory guard on required profile.
- Cancellation does not corrupt next turn.
- Core inference succeeds with network disabled.
- S4 has no blocker correctness/security finding.

---

# Phase 3 — Stable Sessions, Caching Baseline, and Local Laptop Tools

## Objective

Make the assistant useful for multi-turn local work before model-autonomous tool selection is enabled.

## Luna A

- Implement session create/delete/reset across attention KV, recurrent/Gated DeltaNet state, and any optional draft state.
- One active decode with bounded queue.
- Implement safe immutable system/tool prefix snapshot/restore only when the complete hybrid state is versioned and restorable.
- Implement request cancellation rollback.
- Expose cache/session metrics.
- Handle engine/host backpressure.
- Stabilize error codes.

## Luna B

- Validate session reuse for both attention KV and recurrent/Gated DeltaNet state, including mismatch invalidation and reset.
- Tune safe CPU threads/batches.
- Measure cache hit/miss and repeated-turn latency.
- Define tool-result context limits.
- Validate 8K default under ordinary contention.
- Recommend low-memory 4K profile.

## Luna C

- Implement:
  - `fs.list`
  - `fs.read_text`
  - `fs.search_text`
  - `fs.write_new`
  - `fs.apply_patch`
  - `clipboard.read`
  - `clipboard.write`
  - `app.open`
  - `browser.open_url`
- Implement confirmation cards and session approval rules.
- Implement atomic patch preview/base-hash flow.
- Complete session/reset/history budgeting.
- Use a deterministic developer/test controller to invoke tools while autonomous model tool selection is still gated.

## Luna D

- Test session/cross-cache canaries.
- Attack paths, junctions, ADS, reserved names, patch races.
- Test confirmations and event ordering.
- Test process/host shutdown.
- Test no prompt/file content in default logs.
- Run local-tool offline suite.

## Required demonstration

- Multi-turn chat in one session.
- New independent session without data leakage.
- Read a synthetic local file.
- Search text within approved workspace.
- Propose and confirm an atomic patch.
- Deny a tool and continue safely.
- Open an allowlisted local app or browser URL.
- Show cache hit and reset.
- Run with network disconnected.

## Outputs

- M3 stable sessions.
- Local deterministic tool suite.
- Confirmation framework.
- Hybrid-state cache baseline.
- Low-memory profile.
- Local-tool security report.

## Exit criteria

- Sessions/reset/eviction pass.
- No cross-session canary leak.
- Local tools stay within configured roots.
- Mutations are confirmed and atomic.
- Tool output/context limits work.
- Offline assistant and local tools pass.
- Warm repeated-turn latency improves measurably without correctness loss.
- No blocker path/process/confirmation finding.

---

# Phase 4 — Model-Driven Tool Calling and Optional Web Tools

## Objective

Enable Qwen3.5 to select and execute the approved tools end to end, then add a live Internet-capable provider without making inference network-dependent.

## Luna A

- Support tool-role messages through chat template.
- Support the pinned Qwen3.5 template’s structured tool-call events through the adapter; never depend on an unversioned marker heuristic.
- Support bounded grammar/schema-constrained call generation when available, with a strict complete-object parser as the invariant fallback.
- Ensure partial JSON/reasoning is separated from user-visible answer.
- Support multiple model continuations in one turn.
- Maintain cancellation and context accounting.

## Luna B

- Build tool-calling evaluation corpus.
- Tune:
  - system/tool prompt,
  - dynamic active-tool bundle,
  - non-thinking mode,
  - tool sampling,
  - grammar behavior,
  - one repair attempt.
- Quantify tool selection, argument exact match, execution success, no-tool restraint, and recovery.
- Compare Q4 to higher-precision reference where approved.
- Establish category confidence intervals.

## Luna C

- Complete autonomous controller:
  - detect call,
  - validate,
  - assign tier,
  - confirm,
  - execute,
  - insert result,
  - continue.
- Implement `process.run_allowlisted`.
- Implement approved search/fetch provider adapters.
- Implement offline/unconfigured typed failures.
- Implement local-data egress confirmation.
- Treat web content as untrusted.
- Add network status and data disclosure UI.

## Luna D

- Independently run tool-call quality.
- Attack malformed calls, schema grammar, prompt injection, shell/path/URL policy.
- Verify live provider uses injected config and synthetic data.
- Verify native engine has no network dependency.
- Verify browser-open is not mislabeled as search.
- Test cancellation across tool states.

## Required demonstrations

### Local autonomous loop

User asks: “Read the synthetic project notes, find the stated deadline, and create a new summary file.” The model must select read/search/write, show the mutation confirmation, execute, and return a final answer.

### Process loop

User asks for an allowlisted read-only command such as repository status. The model proposes the logical command, confirmation occurs under policy, output returns, and final answer explains it.

### Live web loop

With an approved provider injected on Shadeform and synthetic/non-sensitive query:

1. Model selects search.
2. UI displays provider/query.
3. Search returns sources.
4. Model selects a safe fetch if needed.
5. Web content is labeled untrusted.
6. Model answers with source references.

### Offline web behavior

Disable network. The same search attempt returns a typed error; the model explains the limitation and local chat remains functional.

## Outputs

- M4 autonomous tool controller.
- Tool-call quality report.
- Provider adapter and mock/live evidence.
- Prompt-injection report.
- Tool/event contract v1.0.

## Exit criteria

- Tool-call thresholds pass.
- One malformed call cannot execute.
- Model cannot lower risk or bypass confirmation.
- No unrestricted shell.
- No unconfirmed local-data egress.
- Web provider is optional and isolated from engine.
- Offline mode remains complete.
- No blocker injection/path/process/network finding.

---

# Phase 5 — Performance, Heavy Caching, and GPU Acceleration

## Objective

Reach practical interactive latency and throughput while preserving the complete assistant/tool/security path. Intel acceleration is earned per exact device/driver/build; the CPU path remains a valid release profile.

## Luna A

- Implement accepted performance patches.
- Complete the safe hybrid-state cache manager for attention KV, recurrent state, and immutable prefixes.
- Add accepted Q8 KV profile if evidence supports it.
- Build CPU ISA/runtime profiles.
- Build separate Intel Vulkan conservative/tuned profiles and an explicitly experimental SYCL profile, while preserving CPU binaries.
- Add robust fallback/denylist.
- Improve streaming/backpressure and memory reuse.

## Luna B

- Lead designed tuning:
  - CPU threads,
  - batch/ubatch,
  - context/cache type,
  - Flash Attention and cooperative-matrix paths independently,
  - Intel Vulkan full/partial offload and reported operator placement,
  - memory headroom,
  - warmup,
  - prefix blocks,
  - shader/autotune cache,
  - SYCL only as an experimental comparison,
  - MTP/self-speculation only after the non-MTP path is correct.
- Produce paired results against pinned upstream/product baselines.
- Test 4K/8K/optional 16K.
- Test cold/warm/cache hit/miss.
- Test ordinary CPU/GPU contention.
- Freeze default and low-resource profiles.

## Luna C

- Compact prompts/tool schemas.
- Avoid resending unneeded history.
- Token-budget tool outputs.
- Coalesce UI stream updates without perceptible lag.
- Cache only safe provider/tool results.
- Keep host overhead below gate.
- Ensure confirmations/cancellation remain responsive during load.

## Luna D

- Re-run full regression for each release candidate.
- Verify CPU versus each candidate Intel backend on short, 8K/16K, repeated-turn, tool-call, reset, cancellation, and long-prompt-then-short-prompt contamination fixtures.
- Verify memory and cache isolation.
- Run performance independently.
- Run 30–60 minute soak under repeated turns/tool loops.
- Verify default profile stays responsive and below guard.

## Performance gate

Use both:

### Relative

- Product CPU decode at least 80% of pinned upstream oracle on the same profile.
- Product CPU prefill at least 75% of oracle.
- Accelerated product decode at least 80% of the pinned accelerated oracle by final candidate, with 70% acceptable only as a temporary Phase 5 checkpoint.
- Product peak committed memory no more than 115% of comparable oracle unless justified by host/cache functionality.
- Host/tool orchestration overhead within specified bounds.

### Absolute UX

On the accepted target-like accelerated profile:

- Warm decode target ≥8 tokens/s; preferred ≥15 tokens/s.
- Warm p50 TTFT for a 256-token prompt target ≤5 seconds.
- p95 UI event gap during streaming ≤250 ms.
- Cancellation acknowledgment ≤250 ms and completion ≤2 seconds when the backend reaches a cancellation check.
- Ordinary local tool overhead ≤250 ms excluding tool work.
- Search latency excludes provider/network time but host overhead is measured.

CPU fallback floor:

- Decode ≥3 tokens/s on the accepted CPU analog.
- Warm p50 TTFT ≤15 seconds for a 256-token prompt.
- UI remains responsive.

If exact target hardware differs, Sol reports relative evidence and completes a target acceptance measurement rather than claiming equality.

## Outputs

- M5 performance profiles.
- Autotune/cache format.
- CPU/Intel-Vulkan/SYCL parity and corruption-regression report.
- Final memory budget.
- Final performance report.
- Exact device/driver/backend/build allowlist or quarantine matrix.
- Frozen defaults.

## Exit criteria

- Required relative and absolute gates pass or Sol records a narrow conditional exception.
- No model-quality/tool-call category regression beyond threshold.
- No cache leak.
- No sustained paging in default profile.
- Clean GPU initialization failure falls back safely; corruption, device loss, or silent fallback quarantines the profile rather than silently retrying it.
- Full assistant path, not only CLI microbenchmark, meets usable behavior.
- S4 independently reproduces the result.

---

# Phase 6 — Hardening, Offline Proof, Soak, and Failure Recovery

## Objective

Prove the product fails safely and remains stable before packaging the final release candidate.

## Luna A

- Harden model parser, server, resource guard, and lifecycle.
- Address fuzz/sanitizer findings.
- Ensure cancellation/reset after failures.
- Stabilize error taxonomy.
- Verify no engine outbound network code/path.

## Luna B

- Run long-context and memory-pressure matrix.
- Verify cache invalidation across build/model/tool/profile.
- Test Intel driver/device failure, device loss, silent CPU fallback detection, conservative Vulkan flags, and explicit CPU fallback.
- Freeze performance settings.
- Investigate thermal/contention behavior.

## Luna C

- Harden tool/parser/schema/provider/path/process behavior.
- Complete prompt-injection mitigations.
- Freeze prompts and tool schemas.
- Verify transcript/cache/log retention.
- Handle all typed provider/tool failures gracefully.

## Luna D

- Full adversarial suite.
- GGUF/API/schema fuzz.
- Offline network-denied proof.
- Long soak.
- Crash/orphan/restart tests.
- Secret/log scan.
- Tamper/model/release tests.
- Clean Windows package tests.
- Defect triage and retest.

## Failure scenarios

Must include:

- Wrong model or a community quant that does not match the accepted manifest.
- Corrupted model.
- Insufficient memory.
- Intel Vulkan/SYCL initialization failure, device loss, silent CPU fallback, or output corruption.
- Engine crash.
- Host crash.
- UI disconnect.
- User cancellation.
- Context overflow and long-prompt corruption contaminating the next short prompt.
- Tool timeout.
- Child process crash.
- File changes between preview and write.
- Search offline.
- Provider 4xx/5xx/timeout/oversized response.
- Prompt-injection attempt.
- Invalid config.
- Port conflict.
- Read-only application directory.
- Unicode/spaced paths.

## Outputs

- Release candidate.
- Offline proof.
- Security/adversarial report.
- Soak report.
- Defect closure matrix.
- Frozen contracts/config/prompts.
- Known limitations.

## Exit criteria

- No blocker/major unresolved security finding.
- Mandatory offline suite passes.
- Soak has no memory growth/orphan/corrupt session.
- Failure scenarios produce typed recoverable behavior.
- Release candidate package passes dependency and content scans.
- All skipped mandatory tests have an accepted exception.

---

# Phase 7 — Portable Release and Shadeform Demonstration

## Objective

Build the exact portable release, reproduce it, and demonstrate every accepted MVP capability in a scripted Shadeform run.

## Luna A

- Produce final CPU and promoted Intel-backend binaries/profiles plus build information; exclude unpromoted SYCL artifacts from the normal launcher path.
- Verify release model path/engine commands.
- Support final demo and defect fixes.

## Luna B

- Verify release binary matches benchmarked binary.
- Run final performance/quality report.
- Produce recommended target profile based on hardware receipt.

## Luna C

- Package host/UI/config/tool assets.
- Run final demo.
- Produce operator and tool-provider guide.
- Verify no npm install or remote asset.

## Luna D

- Build release twice from clean source.
- Compare hashes/normalized artifacts.
- Produce SBOM, notices, checksums, provenance, scans.
- Run final full gate suite.
- Produce evidence index and release recommendation.

## Required scripted demo

See `execution/DEMO_SCRIPT.md`.

At minimum:

1. Verify package and model.
2. Launch without network.
3. General chat.
4. Deep mode.
5. Multi-turn/cache.
6. Local file read/search.
7. Confirmed file creation/patch.
8. Allowlisted process.
9. Stop/cancel.
10. Session reset/isolation.
11. Offline web failure.
12. Enable approved live provider.
13. Search/fetch and source-based answer.
14. Show CPU/GPU/backend and performance metrics.
15. Exit with no child process.
16. Verify no weights/secrets/log content in package.

## Outputs

- Versioned portable Windows ZIP.
- Checksums.
- SBOM and notices.
- Release manifest/provenance.
- Final benchmark/quality/security/offline reports.
- Demo evidence.
- Operator runbook.
- Target acceptance checklist.

## Exit criteria

- All mandatory acceptance criteria pass.
- Package is reproducible or documented at accepted normalization level.
- Demo uses the exact release artifact.
- No Internet is required for core assistant.
- Live search capability is labeled/configured honestly.
- Model file remains separate.
- Sol signs release candidate.

---

# Phase 8 — Target-Laptop Acceptance and Handoff

## Objective

Verify that the remotely tested release behaves correctly on the actual restricted laptop without turning the laptop into a development environment.

## Allowed target actions

- Copy approved release and model through approved paths.
- Verify checksums/scans.
- Run read-only hardware probe.
- Extract release to approved directory.
- Create local config pointing to model and workspace.
- Run model verification.
- Launch in foreground.
- Run the small acceptance script.
- Collect redacted machine/performance receipt.
- Stop and remove temporary state if needed.

## Prohibited target actions

- Build source.
- Install packages/drivers/runtimes.
- Download from public sources.
- Modify firewall/security/proxy policy.
- Run broad development tests.
- Use real sensitive content in a demo.
- Expose server beyond loopback.

## Acceptance checks

- Correct model hash.
- Clean launch with no missing DLL/runtime.
- Correct CPU or promoted Intel backend profile selected from the exact hardware receipt, with actual operator placement reported.
- Offline chat.
- One local read-only tool.
- One confirmed synthetic write in an approved test workspace.
- Cancellation.
- Memory, actual offload/operator placement, TTFT, prompt-processing, decode, and driver-stability receipt.
- Clean exit/no orphan.
- No unexpected outbound connection.
- Remove synthetic test data if required.

## Final gate

Sol compares target receipt with Shadeform expectations and records:

- `ACCEPTED`
- `ACCEPTED_WITH_LIMITATIONS`
- `REJECTED_FOR_TARGET`

A target mismatch triggers a narrow remediation phase; it does not permit ad hoc installation or model substitution.

---

# Post-MVP backlog

Only after final acceptance:

- Windows UI Automation accessibility-tree tools.
- Local stdio MCP.
- Better persistent encrypted sessions.
- Continuous batching for multiple local clients.
- Self-speculative/prompt-lookup decoding.
- Custom Qwen3.5-aware kernels/backend replacement.
- Multimodal/screenshot support under a new modality, projection-artifact, memory, privacy, and security decision.
- Approved background service.
- Remote clients with TLS/auth.
- Automatic signed updates.
