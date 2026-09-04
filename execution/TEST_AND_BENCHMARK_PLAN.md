# Test and Benchmark Plan

## 1. Test philosophy

The release must be evaluated as a complete assistant, not merely as a matrix-multiplication benchmark. The plan separates:

- Runtime correctness.
- Quantization quality.
- API/session correctness.
- Tool-call quality.
- Tool execution security.
- Memory stability.
- Latency/throughput.
- Offline operation.
- Packaging and reproducibility.

A large sample and a small p-value do not make a practically trivial effect meaningful. Report effect sizes, paired differences, confidence intervals, failure rates, and category-level results.

## 2. Build identity required for every result

Every result row references:

- Product source commit.
- Backend source revision and patch set.
- Build ID.
- Compiler and flags.
- OS/kernel.
- CPU.
- GPU/driver/backend.
- Host RAM.
- Official source revision, controlled conversion/quantization receipt, model file SHA-256, byte count, metadata digest, and tensor-inventory digest.
- Context/KV type.
- Threads/batch/ubatch/GPU layers/FA.
- Prompt corpus version.
- Host/tool/prompt/schema versions.
- Cache state.
- Run timestamp.
- Random seed where applicable.

A result without this identity is exploratory only.

## 3. Test layers

### L0 — Static and supply-chain

- Formatting/lint/static analysis.
- Dependency/revision lock verification.
- License and notice.
- Secret and large-file scan.
- Binary imported-DLL scan.
- SBOM.
- Release allowlist.
- Source-tree cleanliness.

### L1 — Unit

- Checked arithmetic.
- GGUF header fields.
- Manifest parser.
- Error mapping.
- Auth token comparison.
- SSE framing.
- Session state.
- Cache keys.
- Tool schemas.
- Grammar generation.
- Path and URL normalization.
- Confirmation state.
- Context budgeting.
- Redaction.

### L2 — Contract conformance

- Engine API.
- Assistant event stream.
- Tool envelope.
- Model manifest.
- Config.
- Metrics.
- Error codes.
- Cancellation.
- Release manifest.

Both producer and consumer use the same fixtures.

### L3 — Component integration

- Backend + model.
- Engine + HTTP.
- Host + fixture engine.
- Host + real engine.
- Individual tools.
- Provider mock.
- UI + host.
- Launcher + process tree.

### L4 — End-to-end

- Local chat.
- Multi-turn/session.
- Local autonomous tool loop.
- Confirmed mutation.
- Allowlisted process.
- Live configured web loop.
- Offline web failure.
- Cancellation and recovery.
- Package launch/exit.

### L5 — Adversarial/fuzz

- Model file.
- HTTP/JSON/SSE.
- Tool parser/schema/grammar.
- Paths/reparse/ADS.
- URLs/redirect/DNS.
- Process arguments.
- Prompt injection.
- Cache isolation.
- Resource exhaustion.
- Tamper/downgrade.

### L6 — Performance/quality/soak

- Cold/warm latency.
- Prefill/decode.
- Cache.
- CPU and exact Intel Vulkan/SYCL candidate profiles.
- Model/tool quality.
- Low memory.
- Contention.
- Long-running stability.

## 4. Runtime correctness oracle

### Same-artifact oracle

Use pinned upstream llama.cpp on the **exact controlled Qwen3.5-9B Q4_K_M bytes**. A community quant, a different converter revision, or a different chat template is not the same-artifact oracle.

Compare:

- Tokenizer IDs.
- Rendered chat prompt.
- Stop tokens.
- First-step logits/top tokens.
- Greedy sequence.
- Template-driven `enable_thinking=false|true` and host-enforced reasoning/output budgets.
- Structured tool-call event normalization, complete-object parsing, tool-result continuation, and no-tool behavior.
- Context boundary.
- Attention-KV plus recurrent/Gated DeltaNet state commit, clone, restore, reset, cancellation rollback, and long-prompt→short-prompt contamination.

Proposed acceptance, adjustable only through Sol-approved evidence:

- Tokenization: exact.
- Rendered prompt: exact normalized bytes/tokens.
- Top-1 next-token agreement: ≥99.5% across fixed states.
- Top-5 set overlap: ≥99.9% averaged across states.
- Final-logit cosine similarity: ≥0.9999 for CPU path, with documented tolerances for backend summation.
- Greedy first-64-token exact sequence: ≥95% of prompts.
- Any divergence must remain semantically equivalent and not affect tool/safety fixtures; otherwise investigate.
- CPU and accelerated product paths meet the same functional task outputs, with numerical tolerance documented.

These thresholds are engineering starting points. S2/S4 may recommend a better per-operation tolerance after observing stable reference variance.

### Parser correctness

- Zero crashes on accepted malformed corpus.
- No allocation over configured maximum.
- No out-of-bounds read.
- No unsupported tensor silently coerced.
- Failure before model execution.
- Fuzz campaign reaches agreed coverage/time without blocker finding.

## 5. Quantization/model-quality evaluation

### Comparators

1. Product engine versus pinned upstream oracle on the exact controlled Q4_K_M artifact: runtime/backend parity.
2. Controlled Q4_K_M versus a Q8_0 or higher-precision GGUF generated from the same official source revision and conversion family: quantization retention.
3. CPU versus each Intel Vulkan/SYCL candidate on the exact Q4_K_M artifact: backend correctness and stability.

Only the first Q4 artifact is deployable. Comparator artifacts remain Shadeform evaluation assets.

The higher-precision comparator runs on Shadeform only and is not a second deployable model.

### Curated task groups

Build a versioned synthetic/permissively usable corpus, for example:

| Group | Suggested count | Primary metric |
|---|---:|---|
| General instruction following | 100 | rubric/pass |
| Multi-turn continuity and hybrid-state reset | 80 | rubric/pass + state canaries |
| Summarization | 60 | key-fact coverage + hallucination |
| Extraction/structured output | 80 | exact/F1 |
| Writing/rewrite | 40 | blinded rubric |
| Quantitative reasoning | 60 | exact |
| Code/command reasoning | 60 | tests/exact |
| Local file task planning | 80 | action-plan correctness |
| Tool selection/no-tool | 200 | exact tool/no-tool |
| Tool argument extraction | 160 | exact/field F1 |
| Multi-step tool recovery | 80 | final task success |
| Policy/safety boundary | 100 | violation rate |
| Long-prompt then short-prompt integrity | 60 pairs | exact/semantic + corruption flag |
| Thinking-control compliance | 60 pairs | mode/budget compliance |

Use a held-out test set. Prompt-tuning occurs only on train/dev.

### Quality statistics

Report:

- Aggregate and category score.
- Absolute difference.
- Relative retention.
- Paired bootstrap 95% interval.
- Failure taxonomy.
- Critical-case count.
- Inter-rater agreement for human rubric items.
- Practical non-inferiority margin defined before test.

Minimum release intent:

- Product Q4 score within 2 points of Q4 oracle.
- Q4 retains ≥95% of approved higher-precision aggregate.
- No critical category falls >8 absolute points.
- No new policy/tool-integrity critical failure.
- Tool success thresholds below.

Do not average away a severe safety/tool category failure.

## 6. Tool-call quality

Metrics:

- Tool selection exact match.
- No-tool precision and recall.
- Argument exact match.
- Required-field accuracy.
- Invalid-call rate.
- Repair success.
- Execution success.
- Final task success.
- Extra/unnecessary tool calls.
- Confirmation correctness.
- Recovery after tool error.
- Loop-limit rate.

Initial release thresholds:

| Metric | Threshold |
|---|---:|
| Valid canonical call on first attempt | ≥90% |
| Valid after one repair | ≥98% |
| Correct tool selection | ≥90% |
| Exact arguments, simple single-tool cases | ≥85% |
| End-to-end success, single-tool | ≥90% |
| End-to-end success, multi-tool | ≥80% |
| No-tool false-positive rate | ≤5% |
| Unauthorized execution | 0 |
| Confirmation bypass | 0 |
| Calls beyond turn limit | 0 executed |

If the model falls short, first narrow/dynamically select tools, improve prompt/grammar, and clarify schemas. Do not weaken validation.

## 7. Performance metrics

### Definitions

- **Cold start:** process start to engine ready with no warm OS/model cache.
- **Warm start:** process start with OS file cache warm.
- **TTFT:** accepted request to first user-visible answer token, excluding confirmation wait.
- **Prefill rate:** prompt tokens divided by model prefill time.
- **Decode rate:** generated tokens divided by decode time after first token.
- **End-to-end turn latency:** request to final answer including model/tool work, with tool/network components reported separately.
- **Tool overhead:** host policy/parse/dispatch/result wrapping, excluding actual tool and network time.
- **Cache hit gain:** paired warm turn difference with exact prefix.
- **Working set:** resident process memory.
- **Commit:** committed virtual memory.
- **VRAM:** dedicated/shared backend allocation where observable.
- **Event gap:** time between UI stream updates.
- **Cancellation latency:** request to generation/tool stop and state cleanup.

### Scenarios

| ID | Prompt | Context | Output | Cache |
|---|---|---:|---:|---|
| P1 | Short chat | 128–256 | 128 | cold/warm |
| P2 | Medium instruction | 512 | 256 | miss |
| P3 | Long document | 4,096 | 256 | miss |
| P4 | Default near-limit | 7,000–8,000 | 256 | miss |
| P5 | Repeated system/tool prefix | 512 new | 256 | hit |
| P6 | Multi-turn 10 turns | growing | 128/turn | session |
| P7 | Single tool loop | 512 | 128 + tool | prefix |
| P8 | Multi-tool loop | 1,024 | 256 + tools | prefix |
| P9 | Deep mode | 512 | bounded | miss |
| P10 | Cancellation | 4,096 | ongoing | miss |
| P11 | Long prompt followed by trivial short prompt in same process | 8K or optional 16K, then 32 | 128 each | reset/reuse variants |
| P12 | Tool call after long context | near-limit | 128 + tool | session |

Use fixed text and token counts from the accepted tokenizer.

### Intel backend matrix

For each exact device/driver/build combination test these profiles independently:

1. Tuned CPU reference.
2. Vulkan conservative: modest batch/ubatch, MTP off, cooperative-matrix path disabled when the profile requires it, conservative partial offload.
3. Vulkan full-offload candidate.
4. Vulkan tuned combinations: Flash Attention, cooperative matrix, cache type, batch/ubatch, and MTP each introduced behind a separate experiment.
5. SYCL experimental only when the runtime can be packaged and the device is visible.

A candidate is immediately blocked on corrupted text, invalid UTF-8, unexplained truncation, device loss/TDR, stale state after reset, tool-call structural corruption, or silent CPU fallback. Speed results from a blocked profile are not reported as a recommendation.

### Repetitions

- Cold load: at least 5 independent processes.
- Warm latency: at least 20 runs per key scenario.
- Throughput: at least 10 runs per fixed output length.
- Tool overhead: at least 30 mock-tool runs.
- Soak: 30–60 minutes minimum, longer final run when budget permits.
- Quality: full fixed corpus once per frozen candidate; bootstrap over items.

### Reporting

- p50/p95.
- Mean and robust dispersion.
- Paired differences.
- 95% bootstrap interval.
- Failure/OOM count.
- Thermal/order notes.
- Raw CSV/JSON.

## 8. Performance acceptance

### Relative-to-pinned oracle

On the same machine/config:

| Metric | Release threshold |
|---|---:|
| CPU decode | ≥80% of oracle |
| CPU prefill | ≥75% of oracle |
| Accelerated decode | ≥80% of accepted accelerated oracle |
| Accelerated prefill | ≥75% of oracle |
| Engine-only peak commit | ≤115% of comparable oracle unless justified |
| Product full-path TTFT overhead | ≤20% over engine-only for no-tool turn |
| Host tool-control overhead | p50 ≤100 ms, p95 ≤250 ms excluding tool work |

### Absolute user-experience target

Promoted exact-device Intel accelerated profile:

- Warm decode ≥8 tokens/s; preferred ≥15.
- Warm p50 TTFT ≤5 seconds for a 256-token prompt.
- Warm p95 TTFT ≤8 seconds.
- UI stream p95 event gap ≤250 ms.
- Stop acknowledgment ≤250 ms.
- Backend cleanup after cancel ≤2 seconds at a cancellation checkpoint.
- Cold ready time target ≤30 seconds from local SSD.
- Peak default-profile process commit ≤12 GiB, with dedicated/shared GPU allocations and total system commit reported separately.
- No sustained paging under default scenario.

CPU fallback:

- Decode ≥3 tokens/s.
- Warm p50 TTFT ≤15 seconds at 256 prompt.
- UI remains interactive.
- Memory stays under guard.

Absolute target-laptop claims require the target receipt; otherwise label results by Shadeform profile.

## 9. Cache tests

### Key correctness

Change each key input and verify miss:

- Model hash.
- Engine/backend build.
- Chat-template hash.
- System prompt.
- Tool bundle/schema version.
- Normal/deep mode.
- KV type.
- Context size.
- Rope/backend state affecting output.
- Session identity for user state.

### Privacy

- Canary in Session A never appears in B.
- Reset invalidates derived user state.
- Eviction frees/reuses safely.
- Global cache contains only approved immutable prefix.
- Tool-result cache respects sensitivity and session.
- No persistent user KV by default.

### Performance

- Measure prefix hit speedup.
- Measure lookup overhead.
- Measure memory cost.
- Measure hit rate in demo workload.
- Reject cache complexity with negligible practical gain.

## 10. Memory tests

Run at:

- Fresh model.
- 4K.
- 8K.
- Optional 16K.
- Maximum output.
- Four stored sessions.
- Repeated reset/evict.
- Cache full.
- Tool output at limit.
- Low available memory.
- Intel Vulkan partial/full offload and any experimental SYCL profile.
- Background contention.
- Long-prompt→short-prompt state-contamination sequence.
- Backend initialization failure, device loss, conservative-profile fallback, and silent-CPU-fallback detection.

Pass:

- No hard-guard breach.
- No unbounded growth.
- No allocation after preflight denial.
- Memory returns near stable baseline after reset/eviction.
- No repeated-load leak.
- No sustained paging in default profile.
- Attention KV, recurrent state, MTP/draft state, scratch, mapped weights, host memory, and GPU shared/dedicated allocations reconcile with the reported total within documented telemetry limits.
- Repeated reset/eviction returns every user-derived state class to the accepted baseline.

## 11. Security test groups

### GGUF/model

- Wrong magic/version.
- Negative/huge counts.
- Integer overflow.
- Truncation.
- Misalignment.
- Overlap.
- Unsupported type/shape/architecture, missing hybrid-state tensor family, unexpected vision-projection requirement, or mismatched MTP profile.
- Hash mismatch.
- Path/junction/network path.

### HTTP/UI

- Non-loopback bind attempt.
- Missing/wrong token.
- Origin/Host spoof.
- Oversized body/header.
- Slow client.
- Invalid JSON/UTF-8.
- SSE disconnect.
- Static path traversal.
- CSP/remote asset scan.

### Tool parser

- Tool-call event/framing splitting across streamed chunks.
- Multiple objects.
- Duplicate keys.
- Deep nesting.
- Huge strings/arrays.
- Unknown tool/field.
- Type confusion.
- Repair injection.
- Grammar edge case.

### Filesystem

- `..`, absolute, UNC, device, ADS.
- Symlink/junction/reparse.
- Case/Unicode confusion.
- Reserved names.
- TOCTOU.
- Large/binary file.
- Base hash mismatch.
- Disk full/read-only.

### Process

- Shell metacharacters.
- Flag injection.
- Executable path swap.
- Environment leak.
- Timeout.
- Output flood.
- Child/grandchild orphan.
- Working-directory escape.

### Network/browser

- Unsafe schemes.
- Userinfo/credential URL.
- Redirect to private IP.
- DNS rebinding.
- Oversize/decompression.
- Invalid TLS.
- Provider credential leak.
- Outbound local content without confirmation.
- Prompt injection.

## 12. Offline proof

Test with network externally disabled:

- Engine starts.
- Host/UI starts.
- Model verifies/loads.
- Chat and local tools work.
- No remote asset.
- No DNS/outbound attempt.
- Web search/fetch returns typed error.
- No cloud inference.
- No long retry blocking.
- Exit cleanly.

Archive socket/network observations.

## 13. Soak

Workload mix:

- Short/medium chat.
- Session reset/create.
- Cache hit/miss across complete attention-KV and recurrent-state snapshots.
- File read/search on synthetic corpus.
- Denied/approved mutation.
- Tool timeout.
- Cancellation.
- Offline provider error.
- Periodic component-level memory and actual operator-placement metrics.
- Periodic long-prompt→short-prompt and reset canaries.

Pass:

- No crash.
- No orphan.
- No monotonic memory growth beyond accepted cache fill.
- No cross-session canary.
- Latency does not degrade materially.
- No coherent-to-garbled transition, stale recurrent state, driver reset, or silent backend fallback.
- Error rate within expected injected failures.
- Final clean shutdown.

## 14. Regression gate

Every merge to a release branch requires:

- L0–L2.
- A smoke subset of L3/L4.
- CPU deterministic fixtures.
- Security smoke.
- Package-content scan.

Performance- or backend-related changes additionally require paired benchmark, quality smoke, hybrid-state reset, long-prompt→short-prompt, and actual-placement verification. Security/tool changes require the relevant adversarial suite.

## 15. Evidence summary template

Every final report answers:

- What exact build/model/machine was tested?
- What passed, failed, or skipped?
- How many observations?
- What is the effect size and uncertainty?
- Is the effect practically meaningful?
- What limitation remains?
- Can another session reproduce it from one command?
