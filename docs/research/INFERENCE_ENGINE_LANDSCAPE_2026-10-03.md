# Popular inference engines vs ours: what are we missing?

Date: 2026-10-03. Sources: the vendors' own docs and release notes (links at the end),
read via search snippets and page summaries, not the code. Labels as in the
2026-10-02 review: **source** = checked in this repository, **cited** = a document
said so, not reproduced here. Our own column is **source**.

## What we are (so the gaps are fair)

A purpose-built, single-model, single-user engine: C++ over a pinned llama.cpp,
bound to loopback with a bearer token, one live context plus one conversation
snapshot slot, Qwen3.5-9B Q4_K_M only (hash-pinned), special-token-safe prompt
rendering, opt-in n-gram speculation, tool calling done in the **host** (Qwen XML
parsed by `host/agent`, not in the engine). It is not trying to be Ollama; the
question is which missing features matter to a personal assistant on one laptop.

## Gap table

Priority = for this product (one operator, one Windows laptop, 32 GB).

| Capability | llama.cpp server / Ollama / LM Studio | Us | Priority |
|---|---|---|---|
| Model pull/list/delete, many models | Ollama: registry + Modelfile. llama.cpp: router mode, multi-model (cited). LM Studio: GUI browser | one pinned file | **Low.** Pinning is a feature (hash-verified). Revisit only if a second model is adopted |
| Idle unload / keep-alive | Ollama `keep_alive`; llama.cpp router auto-unload on idle (cited) | model mapped while engine runs; engine stopped on exit | **Medium.** A laptop assistant should free 6 GB when idle and reload on demand. Reload costs >1 min here (re-hash), so needs a lighter check than a full hash per start |
| Parallel requests / continuous batching | llama.cpp slots, LM Studio 0.4 (default 4 slots, cited); Ollama near-serial | ONE context; a delegated job re-prefills the user's chat | **Medium.** Multi-slot would remove the delegation re-prefill cost. Costs RAM (KV per slot; only 8 of 32 layers have KV so it is cheap, plus ~50 MB recurrent state per slot) |
| Multiple conversation snapshots | llama.cpp server keeps context checkpoints, even on hybrid models (cited) | ONE snapshot slot | **Medium.** Same cause as above; a small LRU of snapshots (~53 MB each) fixes it without parallelism |
| Speculative decoding | draft model, EAGLE-3, DFlash, ngram-simple / map / **mod** / cache, and speculative **checkpointing merged 2026-04-19** so recurrent Qwen3.5 works (cited, PR 19493) | opt-in n-gram, verified identical to greedy, rollback via `n_rs_seq` | **Medium.** Our pin (2026-08-02) is *later* than that merge and already has the rollback mechanism (`n_rs_seq`) that we drive ourselves. What we lack is upstream's driver (`common/speculative.cpp`, pruned from our tree) and its `ngram-mod` variant. Decide after the Dell shows whether speculation helps a CPU at all |
| Structured outputs / grammar / JSON schema | GBNF + `json_schema`, Ollama `format` | none; the host validates after the fact and coerces booleans | **Medium-high.** Tool-call failures were format-class (`True`/`False`, missing argument). A lazy grammar that activates at `<tool_call>` is the documented fallback if type errors persist; research warns constrained decoding can raise wrong-function rates |
| Native tool-call API (`tool_calls` field, parsers per model) | OpenAI-compatible tool calling in all three | host parses Qwen XML; engine returns text | **Low** for us. Matters only if other clients call the engine directly; the Copilot bridge goes through the host |
| OpenAI-compatible API surface | `/v1/chat/completions`, embeddings, responses, Anthropic `/v1/messages`, rerank, infill, tokenize (cited) | `/v1/chat/completions` only, minimal fields | **Low-medium.** A `/v1/models` + `tokenize` pair is cheap and makes third-party clients (Continue, Open WebUI) work |
| Reasoning control | reasoning budget / format, `think=true/false` (cited) | `<think>` handled by template; no per-request switch | **Medium.** A "no-think" fast path for chit-chat would cut CPU latency a lot. Needs measuring against tool-call accuracy |
| Embeddings / rerank | yes | no | **Medium** for memory: embedding recall would fix the paraphrase blind spot of keyword recall, at the cost of a second model load (~100-600 MB). Not needed until the Dell shows keyword recall failing |
| Vision / audio | llama.cpp multimodal (mmproj), Ollama vision | text only | **Unverified need.** Whether this GGUF has a usable projector was not checked |
| KV cache quantization, flash attention flags | yes (cited) | engine defaults | **Low.** Only 8 of 32 layers keep KV |
| GPU backends | CUDA, Metal, Vulkan, SYCL, ROCm; Ollama 0.30 enables Vulkan by default for AMD/Intel (cited) | CPU on Windows, Vulkan path exists but untested | **High on the Dell:** checklist B3 |
| Observability | Prometheus `/metrics`, `/props`, `/slots` | `/metrics` (own JSON), `/build-info`, counters | **Low.** Adequate for one user |
| Auth / TLS / CORS | API keys, TLS, CORS options | loopback + bearer, no TLS (loopback), strict Origin/Sec-Fetch checks in the host | **Fine.** Stricter than the others by design |
| MCP | LM Studio is an MCP **host**; llama.cpp server has MCP integration and built-in file/shell tools (cited) | MCP **server** (bridge for Copilot); the agent has its own tool layer | **Different roles, not a gap.** Making our agent an MCP *client* (use other servers' tools) is a possible later feature, with the same per-action confirmation |
| Installer / tray / auto-start / updater | Ollama and LM Studio ship one | PowerShell scripts | **High for "Ollama-like" (gate L4)** but needs the Dell first |
| Web UI | llama.cpp built-in; LM Studio; Open WebUI for Ollama | own minimal UI with confirmation cards | **Fine** |

## What I would build next, in order (status 2026-10-03)

1. **Dell measurements** (checklist A-D). Several rows above turn on a CPU number nobody has.
2. **Several conversation snapshots: DONE.** `--snapshots N` (1-8, default 4, ~53 MB each, LRU, 1 GiB total cap; `native/backend/snapshot_store.hpp`). Real model, two conversations interleaved A1 B1 A2 B2 A3: A2, B2 and A3 each restored their own snapshot (15, 15, 34 tokens) and A3's reply and prompt equal a cold run's. Before, B1 would have evicted A's snapshot.
3. **Idle unload: DONE.** `--idle-unload <30..86400 s>` (launchers: `-IdleUnloadMinutes`). The watcher frees model, context and sampler after a quiet period, never during a request; the next request reloads under the held file lease (identity re-check, no 5.6 GB re-hash) and the conversation snapshots survive. Real model: unloaded 65 s after the last reply, engine stayed READY, the next message reloaded and restored its snapshot. Off by default.
4. **No-think fast path: already the default, and measured.** `mode: "normal"` renders the template with `enable_thinking=false`; the 37-case evaluation (36/37 on the A100, three times) always ran in that mode. Only `deep` thinks. Not measured: `deep` on the 37 cases, because the evaluation's 256-token output cap would truncate the thinking and make the number meaningless.
5. **Lazy tool-call grammar** only if the Dell shows type/format failures that the host coercion misses.
6. **No upstream bump now (decided 2026-10-03).** An earlier draft of this review said our pin predates upstream's speculative checkpointing; that was wrong, the pin is from 2026-08-02 and the merge was 2026-04-19. Measured against upstream HEAD (2026-10-03): 141 files changed in `include/` and `src/` (+18,657/-2,587 lines), and the batch API we call (`llama_batch_init`, `llama_decode`) is being replaced by `llama_batch_ext_*`, so a bump means rewriting our decode paths, re-locking the 175-file Vulkan and 278-file CUDA source closures, touching 30+ pinned references, and re-running every evaluation, for a capability we already have. Revisit when the Dell shows speculation is worth having AND a specific upstream fix is needed.

Not recommended: matching Ollama's registry, parallel multi-user serving, or vLLM-class
batching. Those serve fleets; this is one person's laptop.

## Where the others are weaker than us (keep)

- Prompt-injection-safe rendering of control tokens, tested on the real tokenizer.
- Loopback-only with a token that never appears on a command line (Windows refuses a token file).
- Hash-pinned model and template, receipts without prompt text.
- Per-action confirmation and delegated jobs approved on the laptop.

## Sources

- llama.cpp server README: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- llama.cpp speculative docs: https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md
- Speculative checkpointing merged (summary): https://note.com/hacklog_stealth/n/n1e1231b4869f?hl=en-US
- Hybrid-model prompt re-processing and cache fixes: https://particula.tech/blog/prompt-reprocessing-swa-hybrid-models-kv-cache
- llama.cpp issue 28049 (speculation past EOG on hybrid models): https://github.com/ggml-org/llama.cpp/issues/28049
- Ollama 0.30 (Vulkan default, GGUF): https://webscraft.org/blog/ollama-030-scho-novogo-gguf-vulkan-llamacpp-i-tool-calling?lang=en
- llama.cpp vs Ollama 2026: https://dev.to/rosgluk/llamacpp-vs-ollama-in-2026-which-runtime-should-you-run-4k7f
- LM Studio 0.4.0: https://lmstudio.ai/blog/0.4.0
- Engine comparison (continuous batching, MCP): https://codersera.com/blog/ollama-vs-lm-studio-vs-vllm-vs-llama-cpp-vs-mlx-2026/
