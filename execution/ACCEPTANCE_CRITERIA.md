# MVP Acceptance Criteria

Sol may sign the release only when every **MUST** criterion passes or has a narrowly documented, user-visible conditional exception that does not violate safety, offline inference, model identity, or target constraints.

## 1. Product identity

### MUST

- Product is built from the approved repository commit.
- Native backend source revision and patch set are immutable and recorded.
- Deployable model is exactly the manifest-verified, project-produced, text-only Qwen3.5-9B Q4_K_M GGUF derived from the approved official source revision.
- Official source revision, conversion/quantization tool revision, exact size, SHA-256, GGUF metadata digest, and tensor-inventory digest are recorded and verified.
- Model weights are not included in Git or release package.
- Runtime prints build, backend, model receipt, and config profile IDs.
- No hidden cloud inference or alternate model fallback.

### SHOULD

- Release is byte-for-byte reproducible.
- Artifact is signed/attested through approved mechanism.

## 2. Target deployment

### MUST

- Portable Windows x64 archive.
- No installer or administrator rights.
- No target-side compiler.
- No Docker/Podman/WSL/Ollama/LM Studio.
- No target-side `pip install` or `npm install`.
- No public registry/model-hub access.
- PowerShell launcher plus documented direct executable path.
- Handles paths with spaces and Unicode.
- Runs in foreground.
- Clean exit leaves no engine/host/tool child process.
- Model path is explicit and external.
- Release contains no weights, secrets, caches, logs, or debug dump.
- Exact Dell model, CPU, Intel GPU device/driver/memory, and usable backend receipt exists before accelerated profile selection.

## 3. Offline core

### MUST

With external network disabled:

- Release verifies and loads model.
- UI and API start from local assets.
- General chat works.
- Multi-turn session works.
- Local tools work.
- Cancellation works.
- No DNS/outbound network attempt from inference engine.
- No cloud fallback.
- Web tools return typed unavailable/unconfigured result without breaking session.
- Startup does not hang on update/provider/model calls.

## 4. Inference functionality

### MUST

- Streaming chat-completions subset.
- System/user/assistant/tool roles.
- Template-driven `enable_thinking=false` normal mode.
- Template-driven `enable_thinking=true` deep mode with host-enforced reasoning, output, time, and tool budgets.
- Correct stop/EOS/max-token finish reasons.
- Default 8K context.
- Over-limit prompt rejected before unsafe allocation.
- One active generation and bounded queue.
- Session create/reset/delete across attention KV, recurrent/Gated DeltaNet state, and any enabled draft state.
- CPU fallback.
- Model verification before load, including text-only modality and no-required-vision-projection checks.
- MTP/self-speculative execution disabled unless separately promoted by correctness, quality, memory, and stability evidence.
- Typed, stable error responses.
- Clean cancellation during prefill and decode.

### SHOULD

- Optional validated 16K profile.
- Accelerated backend selected automatically from an approved hardware receipt.

## 5. Model/runtime correctness

### MUST

- Exact tokenizer, special-token, chat-template, thinking-control, and structured tool-call golden fixtures pass.
- CPU deterministic output meets oracle tolerance.
- Every promoted Intel backend profile meets parity and stability tolerance before it can be selected automatically.
- Wrong/corrupt/unsupported model fails safely.
- Session reset removes all user-derived attention KV, recurrent/Gated DeltaNet state, optional draft state, prefix snapshots, and host history.
- Cache key/invalidation tests pass.
- No cross-session canary leak.
- Cancellation does not corrupt or partially commit hybrid sequence state; the next short and long turn remain correct.
- Long-prompt→short-prompt, reset/reuse, and tool-after-long-context regressions show no garbling, invalid UTF-8, truncation, stale state, or parser/allocation failure.

## 6. Tool calling

### MUST

- Complete model → tool call → strict validation → policy → confirmation → execution → tool result → final model response loop.
- Tool call uses canonical JSON envelope.
- Unknown/malformed tool cannot execute.
- One bounded repair at most.
- Model cannot set risk tier.
- Maximum tool calls enforced.
- Partial tool JSON is not shown as an answer.
- Tool output is size/token bounded.
- Model tool-call quality meets test thresholds.

### Included tools

At minimum:

- `system.get_info`
- `time.now`
- `fs.list`
- `fs.read_text`
- `fs.search_text`
- `fs.write_new`
- `fs.apply_patch`
- `clipboard.read`
- `clipboard.write`
- `app.open`
- `browser.open_url`
- `process.run_allowlisted`
- Provider-backed `web.search` and `web.fetch_public` when configured

A provider-dependent tool can be disabled pending approval, but the adapter, policy, mock tests, and typed offline behavior must be complete. The final release note must say whether live result-returning search is enabled.

## 7. Tool safety

### MUST

- Workspace allowlist and canonical path checks.
- Junction/reparse/ADS/device/network-path protections.
- Bounded reads/list/search.
- Atomic create/patch with base hash and preview.
- Mutations require confirmation.
- Logical app/executable IDs.
- Direct process spawn with argument array; no arbitrary shell.
- Timeout/output cap/process-tree kill.
- HTTPS-only browser default and unsafe-scheme rejection.
- External transmission of local content requires data-egress confirmation.
- Web/file content treated as untrusted.
- No confirmation replay/double execution.
- No unrestricted computer control.

## 8. Local API/UI security

### MUST

- Bind only to `127.0.0.1`.
- Random per-launch bearer token.
- Auth on non-health endpoints.
- Host/origin checks.
- No wildcard CORS.
- Request/header/concurrency/time limits.
- CSP and no remote UI assets.
- No directory listing.
- Default logs contain no prompt, response, file content, secret, or reasoning text.
- Diagnostic content mode requires explicit synthetic-data flag.
- Security blocker/major findings closed.

## 9. Caching and memory

### MUST

- Read-only model mapping.
- No duplicate full model copy.
- Bounded attention-KV, recurrent-state, session, prefix, optional draft-state, and tool-result caches.
- Immutable public system/tool prefix may be shared; user state is session-scoped.
- Cache invalidation covers model/build/template/tool/profile changes.
- Default 8K profile stays below 12 GiB commit guard on accepted profile.
- At least 4 GiB system reserve policy or typed refusal/adaptive profile.
- No sustained paging in default test.
- Reset/eviction stabilizes memory and returns every user-derived state class to the accepted baseline.
- No unbounded growth in soak.

### SHOULD

- Validated Q8 KV if it materially improves memory without practical quality loss.
- Versioned hardware autotune cache containing no user content.

## 10. Performance

### MUST

On same Shadeform profile:

- CPU decode ≥80% of pinned oracle.
- CPU prefill ≥75% of pinned oracle.
- Host no-tool overhead ≤20% over engine-only TTFT.
- Tool-control overhead p95 ≤250 ms excluding tool execution.
- CPU fallback decode ≥3 tokens/s on accepted analog.
- CPU fallback warm p50 TTFT ≤15 s for 256-token prompt.
- UI remains responsive and cancellation works.

For a promoted exact-device Intel accelerated profile:

- Accelerated decode ≥80% of accepted accelerated oracle.
- Warm decode ≥8 tokens/s, preferred ≥15.
- Warm p50 TTFT ≤5 s for 256-token prompt.
- Warm p95 TTFT ≤8 s.
- UI stream p95 gap ≤250 ms.
- Short/long/multi-turn/tool/reset/cancel correctness parity passes.
- Actual tensor/operator placement is reported and no silent CPU fallback occurs.
- Clean backend initialization failure falls back safely; corruption or device loss quarantines the profile instead of silently retrying it.

When Shadeform hardware is only a directional analog, absolute target claims must wait for the target receipt. A release that lacks an approved accelerated path may be marked functionally complete but not “performance complete” unless the CPU path independently meets the agreed UX target.

## 11. Model quality

### MUST

- Product engine on the controlled Q4_K_M artifact is within 2 aggregate points of the pinned upstream same-artifact Q4 oracle.
- The controlled Q4_K_M artifact retains ≥95% of the same-source Q8/higher-precision aggregate, or Sol records a task-weighted non-inferiority rationale that does not waive critical tool/safety categories.
- No critical category drops >8 absolute points.
- Valid tool call first attempt ≥90%.
- Valid after one repair ≥98%.
- Correct tool selection ≥90%.
- Single-tool end-to-end ≥90%.
- Multi-tool end-to-end ≥80%.
- No-tool false positive ≤5%.
- Unauthorized execution and confirmation bypass = 0.
- Report effect sizes and bootstrap intervals.

## 12. Reliability

### MUST

- Repeated start/stop.
- Engine and host crash handling.
- Intel backend initialization failure, device loss/TDR, invalid output, and fallback handling.
- No orphan child.
- Cancellation in model/tool states.
- Port conflict/invalid config/low memory typed behavior.
- File race/disk/read-only failures safe.
- Provider timeout/error/oversize safe.
- 30–60 minute mixed soak with no crash, driver reset, silent fallback, cross-session leak, corrupt hybrid state, garbled output, or unbounded memory.
- Release can recover by restarting without cache corruption.

## 13. Shadeform evidence

### MUST

- Exact machine manifests.
- Exact commands.
- Raw test/benchmark/quality artifacts.
- Model/source/build hashes.
- Pass/fail/skip counts.
- p50/p95 and sample counts.
- Offline network proof.
- CPU versus exact Intel Vulkan/SYCL candidate parity, long-context corruption regression, actual placement, and profile-promotion evidence.
- Security report.
- Package and dependency scan.
- SBOM/notices/checksums.
- Full scripted demo using exact release candidate.
- Compute cleaned up and usage recorded.

## 14. Documentation

### MUST

- Operator quick start.
- Model transfer/import/verification.
- Config schema.
- Tool/risk/confirmation behavior.
- Offline vs network-tool behavior.
- Troubleshooting.
- Performance profile selection.
- Privacy/log/cache behavior.
- Uninstall/remove procedure.
- Third-party notices.
- Known limitations.
- Target acceptance checklist.
- No instruction to bypass corporate controls.

## 15. Permitted known limitations at MVP

These may remain when documented:

- One active generation.
- Text-only model.
- No pixel-based computer use.
- No background service.
- No remote clients.
- No automatic updates.
- Web result retrieval disabled when no approved provider exists.
- Optional 16K context disabled on low-memory profile.
- Exact Intel device/driver/backend/build allowlist, conservative profile, or quarantine/denylist.
- CPU performance lower than GPU target while still above fallback floor.
- No persistent transcript by default.
- No destructive delete tool.

## 16. Non-waivable blockers

No conditional exception can waive:

- Wrong/unverified model.
- Hidden cloud inference.
- Target-side public download/install requirement.
- Non-loopback serving.
- Authentication bypass.
- Path/shell injection.
- Unconfirmed external transmission of local content.
- Model/runtime integrity bypass.
- Weights/secrets in Git or release.
- Accelerated incorrect output, unexplained device loss, or silent CPU fallback enabled by default.
- Default sensitive-content logging.
- Core offline failure.
- Missing CPU fallback.
- Orphan process/persistence.
- Unresolved security blocker.
