# Inference speed and tool-call quality: literature review and what it changed

Date: 2026-10-02. Scope: Qwen3.5-9B Q4_K_M on the Intel Core Ultra laptop, vendored
llama.cpp `3581ba0c`. Four reviews ran in parallel; claims below carry a confidence
label and every claim I could check against `vendor/llama.cpp` was checked.

Labels: **source** = verified in this repository's vendored tree; **cited** = from a
paper, issue or PR, URL given, not independently reproduced; **estimate** =
arithmetic or judgement, no measurement behind it.

**How far to trust the citations.** The reviewers read the sources they cite only in part: several items (the vLLM/SGLang coercion details, the constrained-decoding and quantization papers, the Vulkan crash reports) came from search snippets or fetch summaries rather than a full read, and one benchmark page could not be fetched. Treat **cited** as 'a lead worth acting on', not as reproduced. Only **source** items were checked here.

## A correction first

Earlier this session I wrote that speculative decoding was impossible on this model
and recorded it as refused with evidence. **That was wrong.** I read the comment and
first return of `llama_memory_recurrent::seq_rm` and missed the branch below it.

- **source:** `src/llama-memory-recurrent.cpp:~181` rolls back 1..`n_rs_seq` tokens
  using per-token state snapshots. `n_rs_seq` is a public context parameter
  (`include/llama.h:355`, marked EXPERIMENTAL, default 0 at `llama-context.cpp:3489`).
- **source:** `llm_arch_supports_rs_rollback` (`llama-arch.cpp:990`) returns true for
  `LLM_ARCH_QWEN35`. The context clamps `n_rs_seq` to 0 for architectures not listed.
- The engine leaves it at 0, which is why the path was never reached.

Claim rows `ENGINE-SPECULATIVE-DECODING-001`, `ENGINE-PROMPT-PREFIX-REUSE-001` and
`ENGINE-TURN-BOUNDARY-SNAPSHOT-001` are corrected accordingly.

## 1. Speculative decoding without a second model

Generation is bandwidth-bound: about 5.6 GB of weights are read per token, so on
DDR5-5600 (~90 GB/s peak) the ceiling is roughly 16 tokens/s and **kernels cannot
raise it**. Verifying k drafted tokens reads the weights once, which is the only
lever that moves the ceiling.

- **Draft-free n-gram / prompt-lookup** needs no new artifact. Tool calls and JSON echo
  prompt text, so acceptance should be high on this workload (**estimate**). The pinned
  tree has no speculative driver (`common/` is trimmed), so the engine needs its own
  draft/verify loop (**source**).
- **Costs to measure, not assume:** snapshots multiply recurrent-state memory (about
  50 MB each by unverified arithmetic, so `n_rs_seq=4` is ~200 MB); the trailing
  `1 + n_rs_seq` tokens of a sequence are forced into one ubatch (**source**,
  `llama-memory-hybrid.cpp:85`); the feature is experimental upstream.
- **Evidence is GPU-only.** Upstream reports ~72-75% acceptance and >2x on a 27B model
  on a GPU (cited). No CPU or Intel-iGPU number exists. One secondhand report on a Qwen
  MoE found no speculative mode faster than baseline. **Worth testing, not worth
  promising.** Expected range, **estimate**: 1.0-1.1x on free text, 1.5-2.5x on
  copy-heavy JSON and tool calls.
- **Native MTP** is a different route. The manifest says `mtp_present: true`, but our
  own conversion used `--no-mtp` and the engine sets `load_mtp = false`, so this GGUF
  has no head (**source**). Using it means a new artifact (reconvert from the HF
  weights, which needs the leaked, unrotated token rotated first, SI-002, operator
  only), a new pinned identity, and a driver on `llama-ext.h`'s NextN hooks. Upstream
  merged MTP on 2026-05-16 (cited, PR 22673); open issues report low acceptance on
  hybrid models (23322) and tokens retained past end of generation (28049).
- **Skip:** Token Recycling, lookahead decoding, tree verification, Medusa/EAGLE heads
  (need new kernels or trained heads that do not exist for this model).

Sources: https://github.com/ggml-org/llama.cpp/pull/22673 ,
https://github.com/ggml-org/llama.cpp/pull/22400 ,
https://github.com/ggml-org/llama.cpp/issues/23322 ,
https://github.com/ggml-org/llama.cpp/issues/28049 ,
https://arxiv.org/pdf/2505.14969 (STree), https://arxiv.org/abs/2509.19873 (SpecMamba)

## 2. Prompt-prefix reuse across turns

- **Measured on the real model:** a tool-call continuation reuses its whole retained
  prompt (39 of 39 tokens). A new user turn does not: the chat template re-renders
  earlier assistant turns without the `<think>` scaffold, so the new prompt agreed with
  30 of 35 retained tokens and then diverged.
- **cited:** Marconi (arXiv 2411.19379) reaches the same conclusion for hybrid models:
  exact-match prefixes only, checkpointing at branch points. vLLM aligns state to block
  boundaries; SGLang copies out the deepest node holding a state. llama.cpp's server
  keeps "context checkpoints" but still fully re-prefills on a divergent Qwen3.5 turn
  (issue 20225).
- **source:** `llama_state_seq_get_data_ext` with `LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY`
  is in the pinned tree, and `llama-memory-hybrid.cpp` writes the attention KV unless
  that flag is set and always writes the recurrent half. So a boundary snapshot is
  implementable without touching vendor code. Size is **estimate**-grade: ~50 MB flat
  for the recurrent half, plus 32 KiB per token of attention KV if included.
- **Design that fits:** snapshot the recurrent state at the end of the last user
  message, restore it next turn, truncate the KV to that boundary, prefill only the
  remainder. Cheaper than re-prefilling a multi-thousand-token history by orders of
  magnitude (a ~50 MB copy is milliseconds; prefill at 20-60 tokens/s is tens of
  seconds). Recorded as `ENGINE-TURN-BOUNDARY-SNAPSHOT-001`, not built.
- **Rejected:** re-rendering the assistant turn *with* the scaffold. It is cheap but
  changes the bytes the model sees relative to the hash-pinned template.

## 3. Tool-call accuracy

- **cited, high confidence:** Qwen3.5 can emit booleans as `True`/`False`
  (PrimeIntellect renderers issue 47; TensorFold PR 135). vLLM's `qwen3_coder` parser
  and SGLang's both coerce by declared schema type. This matches our two boolean
  failures exactly. **Implemented** as schema-directed coercion in both parsers (see
  `TOOL-CALL-REMAINING-FAILURES-001`); **not yet confirmed on our model**.
- **cited, medium:** shrinking the advertised tool list helps small models (RAG-MCP,
  arXiv 2505.03275; "Less is More", arXiv 2411.15399). The gain figures come from far
  larger tool pools than ours. Our app already advertises 12 tools (~1,182 tokens)
  rather than the eval's 33 (~3,956).
- **cited, medium:** constrained decoding removes type/format errors but can hurt: one
  study reports ~24.5% valid-but-wrong-function rates and reasoning-augmented methods
  beating it by 19.5 points on a 7B model (arXiv 2608.13959). Schema-directed coercion
  in the parser is cheaper and has none of that risk. A lazy grammar at `<tool_call>`
  is a fallback only if type errors persist.
- **no evidence found** for schema-description wording or for `fs.apply_patch`'s
  `missing_argument`. That failure remains undiagnosed.
- **Evaluation is too small to see small effects:** with 37 cases one flipped case is
  2.7 points. Before trusting a quantization or prompt change, expand to ~100 variants
  (paraphrases, both boolean values, 5-10 abstention cases) and log failure class.

## 4. Long context and quantization

- **cited, medium-low:** gated-DeltaNet hybrids retrieve well in vendor reports (RULER,
  80B model); the known weakness is exact multi-key recall from limited state. At the
  ~8k context this product uses it is unlikely to bind.
- **cited, low:** Q4_K_M vs Q5_K_M differences are about 1-3 points on coding evals in
  blog measurements on other models. **No tool-calling-specific quantization study for
  Qwen3.5 was found.** Treat Q5_K_M and Dynamic-quant comparisons as unmeasured.
- **cited, high for coding agents:** observation masking (drop old tool results past a
  window) matches LLM summarization at about half the cost (arXiv 2508.21433). Cheap
  and relevant when a tool loop approaches 8k tokens.
- **estimate:** KV-cache quantization is noise here. Only 8 of 32 layers keep a KV
  cache, and quantized V needs flash attention.

## 5. Intel laptop tuning

- **cited, medium:** closest hard data is a Panther Lake Xe3 iGPU on a 35B MoE with ~3B
  active parameters: Vulkan prefill 633 tok/s vs CPU 102 (6x), decode 33 vs 19 (1.7x).
  Meteor Lake's Xe-LPG iGPU is much weaker; expect a smaller prefill gain and little or
  no decode gain, since the iGPU shares the same RAM bandwidth.
- **cited:** a Meteor Lake Vulkan crash on long generations (issue 17389, fixed by PR
  17887) and a garbage-output bug on an MTL iGPU under Mesa (issue 21888). **Vulkan
  correctness is unproven on this laptop: compare its output to CPU token by token.**
  The recorded driver, 32.0.101.8247, is one build older than the one in the crash report.
- **cited, conflicting:** P-core-only threads were ~3x faster than all cores on old
  Alder Lake (discussion 572) but slower on an Arrow Lake 265K (discussion 21112). The
  result is not universal, so measure. `--threads` exists; the engine ties batch threads
  to the same value (`llama_backend.cpp`), so prefill and decode cannot be tuned
  separately yet.
- **cited:** the NPU is not usable from llama.cpp. OpenVINO GenAI reached ~8.8 tok/s on
  an NPU for Qwen3-8B, no better than CPU decode. Switching engines is not justified.
- **estimate (judge measurements against these):** decode ceiling ~10-11 tok/s at
  55-65 GB/s achieved. CPU decode 6-9, prefill 30-60 tok/s. Vulkan iGPU decode 5-9,
  prefill 80-200. Decode above 11 means the estimate is wrong; below 4 means a thread
  or power-plan problem.

Sources: https://grigio.org/benchmarking-llama-cpp-backends-on-intel-panther-lake-vulkan-vs-sycl-vs-openvino-vs-cpu/ ,
https://github.com/ggml-org/llama.cpp/issues/17389 ,
https://github.com/ggml-org/llama.cpp/issues/21888 ,
https://github.com/ggml-org/llama.cpp/discussions/572 ,
https://github.com/ggml-org/llama.cpp/discussions/21112 ,
https://github.com/PrimeIntellect-ai/renderers/issues/47 ,
https://github.com/ashhart/TensorFold/pull/135 ,
https://arxiv.org/abs/2411.19379 , https://arxiv.org/pdf/2508.21433

## First day on the Dell, in order

1. `-Mode cases` on the three failing cases: confirms or refutes the `True`/`False`
   explanation, and shows what `fs.apply_patch` omits.
2. `-Mode bench` on CPU at default threads: the baseline.
3. Thread sweep (`-Threads`): compare prefill and decode seconds separately.
4. Vulkan with all layers offloaded, then diff its output against CPU's. Drop it if it
   crashes, garbles, or decodes slower than CPU.
5. Only then consider speculation, with measured acceptance on the real tool-call corpus.
