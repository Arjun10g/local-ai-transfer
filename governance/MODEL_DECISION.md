# Model Decision Record — Qwen3.5-9B Q4_K_M

## Current governance truth — 2026-09-11 (refresh v5)

- Audited integrated source baseline is exact
  `main@263f11413d1746044a6cc13062ad2b1f821c4d11`, the last source merge. The
  docs descendants `335211f5ce7cdb230c75c8ba14f7cf31ce43f69f` and
  `1b22a40e21dce0ebc903bc3c2024ab162adc2d56` and this document are
  documentation descendants, not self-referential source hashes. The previous
  baseline `7239b7e` is historical and superseded.
- **The model has not been tested against the profile it must ship against.**
  The current production evaluation fixture/profile is 33 tools and 37 cases —
  `tests/model/production_tool_call_eval.json`, SHA-256
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c` — and it
  has NO recorded score. `max_cases=64` is a ceiling, not a request to trim.
  Every recorded score dates from 2026-09-04 and was measured on a retired
  fixture on CUDA/A100 hardware, never on CPU and never on Intel Vulkan: 28/34
  on the retired 11-tool/34-case canary and 13/32 on the retired
  28-tool/32-case production profile. The model's score on the shipping
  profile is unknown, not merely below gate; historical 13/32 and
  28/32/11/34 profile/results remain separate and are not current acceptance.
- The quality comparators were deleted. Q8_0 and bf16 survive only as hashes
  in `artifacts/qwen35-9b/scan-receipt.json`, and
  `artifacts/qwen35-9b/post-cleanup-receipt.json` records `Qwen3.5-9B-Q4_K_M`
  as the only remaining GGUF, so the ≥95% quality-retention criterion below
  (`execution/ACCEPTANCE_CRITERIA.md:215`) currently has no reachable
  reference artifact. Any future quality run must budget a full Shadeform
  re-conversion, which changes B-004 candidate cost planning.
- The quality corpus is not authored. `model/quality-eval/quality-fixture-spec.json`
  defines 13 categories whose `minimum_cases` fields total 1,180, against only
  3 `fixture_cases` present. Authoring it requires no model, no spend, and no
  credential; it is tracked as `UNCLAIMED` task `MODEL-QUALITY-CORPUS-001`.
- A local development run on 2026-09-11, authorized by Sol as a single
  development-only exception to the B-006 "do not load local bytes" workaround
  because the local bytes were re-verified byte-exact against the pinned
  identity, ABORTED before any model load on two independent stop conditions:
  an 8 GiB Apple M2 development host with roughly 94% of swap in use and
  memory pressure at WARNING before start (the artifact needs about 5.2 GiB
  resident), and a prebuilt engine 532 commits behind `fdfed07` that rejects
  the current `serve` flags with `unknown argument`. No score, no bytes
  loaded, nothing bound or downloaded; the model identity re-verified exactly
  at 5,629,109,088 bytes and SHA-256 `c654bc40…68873b`. No model evidence can
  be produced on that laptop; a real measurement needs a rebuild from current
  source on a machine of at least 16 GiB (i.e. Shadeform) once the human-only
  blockers clear. The aborted-run receipt is
  `artifacts/dev-evidence/local-macos-20260911-aborted/README.md`, held
  outside `artifacts/evidence-index/` as development evidence only; it
  advances no gate.
- The inert unmintable dispatch lease and inert remote-canary hardening are
  source-merged (`5bef3cd`, `a9d2388`) but have no proof issuer, process
  mutation, product activation, success/runtime wiring, live execution, or
  production authority. Graph auth composition/read paths are source-present,
  while durable Graph writes remain refused without a healthy production
  ActionJournal. Remote execution is false; the canary is limited to two
  no-model probes.
- External salvage is unavailable and legacy lifecycle is non-green. Read-only
  ledger preflight and its canonical-prefix fix are source-merged (`83cffab`,
  `089d05d`) with 31/31 postmerge integration, but `SAFE_TO_MIGRATE_NOW` remains
  `NO`: current legacy evidence is parse-refused/unavailable under unsafe
  permissions, and orphan receipts/unmatched incidents require adjudication.
  No genesis, spend authorization, or provider execution is supplied. Phase-2a
  single-owner topology is merged at `2ad3006` from `03885df`; two independent
  audits and postmerge source integration passed, with legacy parallel journal
  types removed and exact IDs/tracked tickets retained. Phase-3 controller
  arbitration is merged at `71537d0` after two final source accepts, but remains
  inert with no native bridge, proof issuer, process mutation, product
  activation, or live process. Phase-2b is design-only/in progress, not
  source-accepted. Protected `.secrets/shadeform.env` projection source is
  accepted at `c8c28a9`; its operator setup does not authorize credentials or
  provider execution. Metadata-only Windows/HF handoff source is accepted at
  `c2801ec` and merged by `e2e5156`, but its public validator permanently
  returns `REFUSED_NOT_ACTIVATED` without real signed artifacts and an approved
  trust anchor. Descriptor-backed ActionJournal WAL source is accepted at
  `2dda060` and merged by `6e0d12c`, but native secure FD ownership,
  single-writer exclusion, authenticity/anti-rollback, compaction, and provider
  reconciliation remain absent.
- Two slices were integrated this interval, each independently S0/S4
  source-reviewed and returning `ACCEPT_FOR_MERGE` on the first pass with
  NOTE/MINOR findings only, and neither adding compile, live, provider,
  production, Windows, or target evidence: the frozen descriptor WAL contract
  (accepted `9f136a0`, merged `237d59b`), which freezes
  `contracts/action-journal-descriptor-wal/v0.1.0` for the dormant v2
  boundary, is statically bound to the C++ and Node constants (48-byte header,
  32 MiB) and to the derived v2 status set (26 v2 codes against 25 v1, with
  `v1 ∪ v2 ∪ {platform_unavailable}` equal to the 30-code storage set), leaves
  `contracts/action-journal-storage/v0.1.0.json` byte-identical, and closes
  review item R4 as a documented, test-pinned accepted Win32 limitation; and
  the Graph sent-mail proof projection repair (accepted `26beb82`, merged
  `263f114`), which confirms and fixes the `listSentForDigest`
  collection-projection defect that left the sole completing state
  `unique_sent_item` unreachable in code, bounding proof retrieval to one folder
  ID page plus at most 20 exact GETs per call with typed inconclusive results
  and a send-proof deadline, while leaving the pre-send `@odata.nextLink`
  refusal as follow-up `GRAPH-SENT-PAGINATION-PRECHECK`. The Graph projection
  premise the defect rests on is unverified without a live account, and the
  repair is fail-closed under either behavior; a pre-dispatch refusal still
  lands as `unknown_manual`, and three pre-proof requests in the send branch
  remain uncapped, so the seam is bounded while the tool as a whole is not and
  a slow provider can still surface as `tool_timeout` → `unknown_manual`. The three slices of
  the previous interval (`4280e95`/`61c9475`, `e4ca09b`/`4de01f7`,
  `cfe8136`/`7239b7e`) remain on `main`. ICR-RUN-WDJB-001 is Sol-approved as
  an additive source-only extension with contract version `0.1.0` retained,
  generalized by ADR-0004.
- Current reproduced evidence is `npm test` 432 tests/430 pass/0 fail/0
  cancelled/1 skipped/1 todo; native static `Ran 309 tests` OK, of which the
  narrower Windows-only pattern is `Ran 289 tests` OK — 309 is 289 plus the 20
  tests of the new descriptor-WAL contract suite; and QA safe-runner `Ran 20
  tests` OK. Static inventory records 154 tracked JSON files (153 strict-valid
  under a duplicate-key-rejecting parser plus one intentional hostile fixture)
  and a host import graph of 29 modules/67 unique relative import edges/0
  unresolved/0 cycles. QA discovers 64 with 0 missing/unknown and 70 records
  (1 PASS/69 expected SKIP); overall QA remains `BLOCKED` and no full suite is
  green in the release sense. The prior `7239b7e` numbers (Node 426/424,
  native static 289, JSON 153/152, QA 62 discovered/68 records) and the older
  `6e0d12c` focused numbers (journal/Graph Node 210/209, handoff/release
  63/63, env 26/26, QA-runner 27/27, conformance 11/11, JSON 152/151, imports
  29/71, QA 59 discovered/65 records) are historical, as are broader Node
  350/349 and Python 663/663, which belong to `d195235` and are not current
  `263f114` proof. None of this is model evidence. Bookkeeping is not spend
  authorization. Credentials require source-side rotation/revocation,
  including the previously leaked HF token.
- The model-family/quantization decision below remains accepted only as a
  technical choice. No compile, model-load, provider, Windows-target, target,
  or production acceptance is claimed; release/full access remains blocked.

## Decision

The MVP will use exactly one deployable language-model artifact:

- **Family:** Qwen3.5
- **Model:** Qwen3.5-9B
- **Source checkpoint:** approved, pinned revision of `Qwen/Qwen3.5-9B`
- **Deployable quantization:** `Q4_K_M`
- **Deployable format:** GGUF
- **MVP modality:** text only
- **Expected file name:** `Qwen3.5-9B-Q4_K_M.gguf`
- **Expected technical size:** exactly 5,629,109,088 bytes
- **Expected technical SHA-256:** `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`
- **License identifier to verify and archive:** Apache-2.0
- **Default interaction mode:** thinking disabled
- **Opt-in deep mode:** thinking enabled with host-enforced budgets
- **Default context:** 8,192 tokens
- **Maximum MVP context:** 16,384 tokens only after memory, correctness, and latency gates
- **Primary acceleration candidate:** Intel Vulkan
- **Mandatory fallback:** CPU

The exact filename, size, and hash above are the product verifier/runtime
identity. They are technical constraints, not artifact acceptance: ignored
local bytes have not been revalidated in this governance interval and remain
unapproved; transfer custody, the approved transfer route, and the corporate
approval reference are unset. A checksum reference alone is not a transfer or
chain-of-custody receipt.

The v1 Windows artifact-handoff schema and validator can validate bounded
metadata, fixed identities, receipt digests, release-package exclusion, and the
shape/canonical digest of a detached Ed25519 signature. They do not read model
bytes. The public validator has no trust anchor and always emits
`REFUSED_NOT_ACTIVATED`; even the private future trust-verification seam reports
`activated: false`. This is a fail-closed integration contract, not artifact
acceptance, custody, signature verification evidence, or Windows release
activation.

The deployable GGUF is built on Shadeform from the approved official source revision using a pinned conversion/quantization toolchain. A third-party quant may be used for early research only; it cannot become the release artifact without being independently reproduced or explicitly accepted by a Sol-approved supply-chain ADR.

## Decision status

**Accepted for the revised MVP as the model-family/quantization choice only.**
This supersedes the earlier Qwen3-8B decision; it does not accept particular
GGUF bytes, transfer custody, runtime quality, target execution, or release.

A further model change requires a Sol-approved ADR and restarts:

- model provenance and licensing,
- conversion reproducibility,
- runtime parity,
- quantization quality,
- Intel backend correctness,
- latency/throughput/memory testing,
- tool-call evaluation,
- packaging and release evidence.

Workers may not quietly use Qwen3.5-4B, a 2-bit quant, a fine-tune, a distilled derivative, Gemma 4, Qwen3.8, or a cloud endpoint to make a failing gate pass.

### Historical acceptance evidence (pre-profile synchronization)

The strongest recorded general remote tool evaluation is 28/34 on the retired
11-tool/34-case canary fixture (receipt
`artifacts/qwen35-9b/remote-eval-20260904-j/eval-receipt.json`, git-tracked,
`metrics.passed = 28` against `metrics.case_count = 34`), below its gate and
not on the current profile. This corrects the previously recorded 27/34, which
understated that receipt. A separate production-profile run is 13/32 with 19
failures and failed that profile. These results are not interchangeable,
neither accepts the model/tool path, and both were measured on 2026-09-04 on
retired fixtures on CUDA/A100 — so neither speaks to the current
33-tool/37-case shipping profile, which has no recorded score at all. Later
corrected attempts timed out during provider activation before evaluation. The
expected artifact identity is therefore pinned, but quality, local-byte
verification, transfer custody, target execution, and release acceptance
remain open.

## Why this model

Qwen3.5-9B is the best current engineering balance for the supplied laptop constraints:

1. **Recent and materially stronger.** It is a newer generation than Qwen3-8B and is trained/post-trained for stronger reasoning, instruction following, coding, multilingual use, and agents.
2. **General laptop-assistant fit.** It is not restricted to coding and has explicit agent/tool capabilities.
3. **Practical weight class.** The expected Q4_K_M identity is 5,629,109,088 bytes (about 5.24 GiB), leaving substantially more headroom than 12B–27B alternatives inside the observed 17.35 GiB available-memory window.
4. **Hybrid state efficiency.** Only one layer in each four-layer group is full attention, reducing context-growing KV storage relative to an all-attention 9B model.
5. **Fast and deep modes in one artifact.** Normal laptop tasks can avoid unnecessary reasoning latency, while complex tasks can opt into bounded thinking.
6. **MTP training.** Multi-token prediction offers a future acceleration path without requiring a second main model, although it is deferred until base correctness.
7. **Tool-call ceiling.** Published results show strong function/tool and agent performance for its size.
8. **Intel evidence exists.** Qwen3.5 has been exercised in llama.cpp on Intel Arc/iGPU systems. The evidence is mixed by backend and driver, so the plan treats correctness as a gate rather than an assumption.
9. **CPU remains feasible.** The artifact remains small enough for a guaranteed CPU fallback on a 32 GB-class system.
10. **Lower bit is unnecessary for fit.** Moving directly to 2-bit or 1-bit would add quantization and kernel risk before Q4_K_M has failed a hard resource gate.

This is a constrained deployment decision, not a claim that Qwen3.5-9B is the strongest local model on every machine.

## Official model facts relevant to implementation

The official model card describes the language component as:

- 9B parameters.
- 32 layers.
- Hidden dimension 4,096.
- Repeating layout: eight groups, each containing three Gated DeltaNet blocks followed by one gated-attention block.
- Eight gated-attention layers in total.
- Sixteen query heads and four KV heads in the gated-attention blocks.
- Attention head dimension 256.
- Multi-step MTP training.
- Native context length 262,144 tokens.
- Explicit tool/agent usage.
- Apache-2.0 model license metadata.

The product intentionally does **not** expose the model’s maximum advertised context in the MVP. The target’s memory, latency, and ordinary laptop workload make 8K the safe default and 16K the first gated extension.

### Published selection signal, not release evidence

The official Qwen evaluation table reports the 9B model at 82.5 on MMLU-Pro, 81.7 on GPQA Diamond, 91.5 on IFEval, and 66.1 on BFCL-V4. In the same table, the Qwen3.5-4B scores 79.1, 76.2, 89.8, and 50.3 respectively. This supports choosing the 9B model over the 4B variant for a broad assistant and tool-calling workload. These publisher-reported scores are only a selection signal: the release decision still depends on the project’s controlled Q4_K_M quality suite, tool-loop evaluation, and Intel-backend tests.

The official model page also exposes a quantization route for llama.cpp-compatible applications, establishing practical format availability. The release nevertheless produces its own controlled GGUF rather than accepting an arbitrary community quant.

The engine must parse and validate GGUF metadata rather than hardcode tensor dimensions throughout the code. It must nevertheless reject an artifact that does not match the accepted Qwen3.5-9B language-model profile, tokenizer, template, and tensor inventory.

## Text-only modality decision

The source model includes a vision encoder, but the first release is a text-and-tools assistant. Therefore:

- Transfer and load only the language GGUF.
- Do not ship an `mmproj` or equivalent vision projection in the MVP.
- Reject image/audio/video inputs with a typed `modality_not_enabled` response.
- Do not reserve memory for visual tokens or a vision encoder.
- Preserve an interface boundary that can add a separately reviewed vision artifact later.

This reduces memory, supply-chain surface, parser complexity, and security scope while preserving the main assistant and tool-calling objective.

## Controlled artifact build

Luna Session 2 owns the artifact pipeline. The accepted build must:

1. Acquire the approved official checkpoint on Shadeform, never on the target laptop.
2. Pin the exact upstream revision/commit and archive the model card and license.
3. Pin a llama.cpp commit that supports Qwen3.5 conversion and the required target backends.
4. Convert the language model to a high-precision GGUF reference.
5. Produce a Q8_0 comparison artifact when practical.
6. Quantize the deployable artifact to Q4_K_M.
7. Record commands, tool hashes, compiler identity, environment, tensor inventory, and elapsed time.
8. Run structural validation and inference smoke tests.
9. Evaluate Q4_K_M against the high-precision/Q8 reference.
10. Generate a signed or hash-addressed model manifest.
11. Transfer only the accepted GGUF, manifest, license, and verification receipt through the approved path.

No conversion or quantization occurs on the target.

## Quantization position

Q4_K_M is not called lossless. The accepted claim is:

> Q4_K_M is expected to preserve most useful behavior while reducing memory and improving local inference practicality; retention must be demonstrated for this exact artifact and workload.

The evaluation has two distinct comparisons:

1. **Runtime parity:** repository engine versus pinned upstream llama.cpp using the exact same Q4_K_M bytes.
2. **Quantization retention:** Q4_K_M versus an approved Q8_0 or higher-precision reference generated from the same source revision.

The release gate emphasizes practical effects:

- aggregate assistant-task retention,
- instruction-following retention,
- tool-selection exact match,
- tool-argument validity,
- multi-step tool success,
- refusal/policy boundary retention,
- coding/file-edit fidelity,
- multilingual/general-writing retention,
- long-turn and state-reset stability.

A 2-bit or 1-bit variant is a post-MVP research branch unless Q4_K_M fails a hard fit constraint and a new ADR defines both quality and Intel-kernel evidence.

## Hybrid state and memory model

### Weight mapping

The expected mapped weight identity is exactly 5,629,109,088 bytes (about
5.24 GiB). An accepted verification receipt must confirm it before load.
Memory mapping should prevent a second full host copy, although working-set
behavior and shared GPU memory must be measured.

### Attention KV estimate

The full-attention portion has eight attention layers, four KV heads, and a 256-element head dimension. A planning estimate for F16/BF16 attention KV is:

`2 × attention_layers × tokens × kv_heads × head_dimension × bytes`

This yields approximately:

| Context | F16/BF16 attention KV | Q8 attention KV planning value |
|---:|---:|---:|
| 4,096 | 0.125 GiB | 0.0625 GiB |
| 8,192 | 0.25 GiB | 0.125 GiB |
| 16,384 | 0.50 GiB | 0.25 GiB |
| 32,768 | 1.00 GiB | 0.50 GiB |

These numbers exclude:

- fixed recurrent/Gated DeltaNet state,
- MTP state,
- graph and allocator buffers,
- backend staging buffers,
- prefix-cache copies,
- driver allocations,
- host and tool processes.

Luna Session 2 must measure those components separately. No release decision may substitute this planning estimate for telemetry.

### Default 8K working budget

| Item | Planning value |
|---|---:|
| Mapped Q4_K_M weights | 5,629,109,088 bytes (about 5.24 GiB), subject to accepted verification |
| Attention KV | approximately 0.25 GiB F16/BF16, or approximately 0.125 GiB after validated Q8 |
| Recurrent/model sequence state | measured; initial combined allowance up to 0.75 GiB |
| Graph/scratch/staging/allocator | target at or below 2.5 GiB |
| Prefix and inactive-session cache | 0.5 GiB soft cap |
| Host/UI/tools/log metadata | at or below 0.75 GiB typical |
| Driver/shared-memory reserve | at least 2.0 GiB |
| Windows and ordinary-app reserve | at least 4.0 GiB from the observed available window |
| Product hard commit target | at or below 12.0 GiB in the default profile |

The runtime must expose weight, attention-KV, recurrent-state, scratch, cache, GPU/shared-memory, and total-process measurements. A single aggregate “RAM used” number is not sufficient.

## Default generation profiles

### Interactive mode

- `thinking: false`
- Context: 8,192
- UI output default: 1,024 tokens
- Hard answer cap: 2,048 tokens
- Streaming: enabled
- Active generation: one
- Prefix cache: enabled only after parity and privacy validation
- Sampling defaults begin from the official non-thinking guidance and are tuned on the local evaluation set

### Tool-selection mode

- `thinking: false` unless a tool-planning benchmark demonstrates a material gain from bounded reasoning
- Low-variance sampling
- Tool choice enabled explicitly
- Compact dynamically selected schemas
- Grammar/structured-output constraint after the accepted tool-call boundary
- One bounded repair attempt
- No hidden reasoning written to logs

### Deep mode

- `thinking: true`
- User-initiated or policy-approved task routing
- Separate reasoning and answer budgets
- Global wall-clock and cancellation limits
- Same tool/security policy as interactive mode
- Reasoning text not persisted in ordinary logs

Exact sampler values are versioned configuration and must be supported by evidence. They are not part of model identity.

## Model path contract

The target never assumes a download location. Model path precedence is:

1. CLI `--model`
2. `LAE_MODEL_PATH`
3. absolute path in `config.local.json`

Example:

```text
--model "D:\ApprovedModels\qwen35-9b-q4-k-m\Qwen3.5-9B-Q4_K_M.gguf"
```

Recommended model directory:

```text
<approved-model-root>\
  qwen35-9b-q4-k-m\
    Qwen3.5-9B-Q4_K_M.gguf
    model.manifest.json
    LICENSE.txt
    verification-receipt.json
```

Recommended application directory:

```text
<approved-app-root>\
  lae-engine-cpu.exe
  lae-engine-vulkan.exe
  lae-host.mjs
  Start-LocalAssistant.ps1
  config.example.json
  ui\
  licenses\
```

Weights and application may share a physical drive but remain logically, operationally, and release-wise separate.

## Import and verification

`verify-model` and `Verify-Model.ps1` must:

1. Resolve and canonicalize the path.
2. Confirm it lies inside an approved local model root.
3. Reject links/reparse paths that escape the root.
4. Confirm regular-file semantics and expected size envelope.
5. Compute SHA-256 and compare with the manifest.
6. Validate manifest signature/attestation when available.
7. Parse GGUF defensively before large allocations.
8. Confirm `qwen35`/accepted Qwen3.5 architecture metadata and tensor profile.
9. Confirm tokenizer, special-token, stop-token, and chat-template hashes.
10. Confirm that no vision projection is required by the selected text-only profile.
11. Record a local metadata-only verification receipt.
12. Refuse inference on any mismatch.

## Candidate review

| Candidate | MVP disposition | Reason |
|---|---|---|
| **Qwen3.5-9B Q4_K_M** | **Selected** | Recent, strong agent/tool and reasoning profile, practical 5–6 GiB weight class, text-only deployment, workable CPU fallback, Intel Vulkan path available for validation |
| Qwen3.5-4B Q4_K_M | Rejected as primary | Faster and smaller, but gives up useful capability while the 9B model fits the observed memory envelope |
| Qwen3-8B Q4_K_M | Superseded | Safer legacy choice but materially older/weaker than the selected generation |
| Gemma 4 12B QAT Q4 | Deferred | Newer and capable, but current Intel Arc llama.cpp reports include corruption on long prompts and backend-specific failures; unsuitable as the low-risk first Intel target |
| Qwen3.6-35B-A3B Q4-class | Rejected for this target | Only about 3B parameters activate per token, but all 35B expert weights must remain resident or be streamed; the weight footprint alone consumes the practical available-memory window |
| Qwen3.8-27B low-bit | Rejected for this target | Newer/stronger but too close to the observed available-RAM ceiling once shared GPU memory, state, buffers, tools, browser, and Windows are included |
| GPT-OSS-20B MXFP4 | Deferred | Can fit some 16 GB-class accelerator setups but leaves less target headroom and is not clearly superior to Qwen3.5-9B for this general agent workload |
| 1–2-bit 9B artifact | Post-MVP research | Could reduce memory, but quality and Intel kernel support must earn the added risk |
| Coding-only model | Rejected | The product is a broad laptop assistant, not only a code agent |

## Runtime correctness criteria

Before integration is accepted:

- GGUF metadata and tensor inventory match the manifest.
- Tokenizer, special tokens, stop conditions, and chat template match the reference.
- Fixed-seed top-token/logit parity meets the configured threshold.
- Multi-turn outputs remain semantically and structurally consistent with the oracle.
- Thinking and non-thinking modes transition correctly.
- Attention KV and recurrent state are both isolated by sequence/session.
- Reset removes all user-specific state.
- Prefix-cache reuse does not cross users/workspaces/sensitivity domains.
- Tool calls are complete, parseable, and withheld from user-visible text until validated.
- 8K and 16K prompt paths do not corrupt subsequent short prompts.
- Cancellation leaves the model reusable.
- CPU and accepted accelerated paths agree within tolerance.
- Malformed GGUF variants fail before large allocations.
- Missing vision projection does not break text inference.

## Model-quality acceptance

At minimum:

- Product Q4_K_M is no worse than the pinned Q4_K_M oracle by more than 2 absolute aggregate points.
- Q4_K_M retains at least 95% of the approved higher-precision/Q8 aggregate score unless Sol approves a task-weighted exception.
- No critical category—tool argument integrity, safety boundary following, file-edit fidelity, instruction following, or state isolation—drops by more than 8 absolute points.
- Tool-call exact match, schema validity, and end-to-end task success meet the thresholds in `execution/TEST_AND_BENCHMARK_PLAN.md`.
- Report bootstrap confidence intervals and practical effect sizes; do not rely on p-values alone.
- A speed improvement cannot compensate for unexplained answer corruption.

## Primary-source research anchors

Revalidated 2026-09-03:

- Official Qwen3.5-9B model card: `https://huggingface.co/Qwen/Qwen3.5-9B`
- Official Qwen3.6-35B-A3B model card: `https://huggingface.co/Qwen/Qwen3.6-35B-A3B`
- Official Qwen3.8-27B model card: `https://huggingface.co/Qwen/Qwen3.8-27B`
- llama.cpp repository: `https://github.com/ggml-org/llama.cpp`
- Intel/Qwen3.5 SYCL performance report: `https://github.com/ggml-org/llama.cpp/issues/22001`
- Intel Arc 140V Vulkan TDR/workaround report: `https://github.com/ggml-org/llama.cpp/issues/20554`
- Intel Arc A770 Qwen3.5 Vulkan field report: `https://github.com/ggml-org/llama.cpp/issues/24199`
- Gemma 4 Intel long-prompt corruption report used in candidate rejection: `https://github.com/ggml-org/llama.cpp/issues/26206`

Phase 0 must recheck issue status and pin a known-good runtime commit. The release must never track a floating `latest` build.
