#pragma once

#include "engine_backend.hpp"

#include <cstddef>

namespace lae {

bool context_budget_fits(size_t prompt_tokens, unsigned max_tokens, unsigned context_tokens);

struct ContextBatchConfig {
  unsigned n_ctx;
  unsigned n_batch;
  unsigned n_ubatch;
};

// Keep the logical submission window equal to context capacity while using a
// bounded physical micro-batch. llama_decode splits a causal batch into
// n_ubatch-sized pieces, preserving positions and the final output row.
ContextBatchConfig context_batch_config(unsigned context_tokens);

// How many leading prompt tokens the retained context already holds.
//
// The 24 gated-DeltaNet layers of the product model keep a recurrent state,
// not a per-token cache: upstream `llama_memory_recurrent::seq_rm` refuses to
// erase part of a sequence's tail, and `llama_memory_hybrid::seq_rm` fails
// with it, so a retained state can only be EXTENDED or dropped whole. This
// returns a reuse length only when `state` is a strict prefix of `prompt`,
// which is the only case that needs no rewind. A divergence anywhere, or a
// state that already covers the whole prompt (no row left to read logits
// from), returns 0, meaning: clear the state and prefill from scratch.
size_t reusable_prefix_tokens(const std::vector<int32_t>& state,
                              const std::vector<int32_t>& prompt);

// Draft-free speculation: guess the next tokens by finding where the end of
// `history` (prompt plus everything generated so far) occurred before, and
// proposing what followed it. Tool calls and JSON repeat names, keys and
// values already in the prompt, which is the workload this targets.
//
// Prefers the longest matching suffix (down to 2 tokens; a single token
// matches too often to mean anything) and, among equals, the most recent
// earlier occurrence. Returns at most `max_draft` tokens, none if nothing
// matches. A wrong guess costs only the verification slot it occupied: the
// caller accepts a drafted token only when the model's own greedy choice
// equals it, so the output is the one plain greedy decoding would produce.
std::vector<int32_t> ngram_draft(const std::vector<int32_t>& history,
                                 size_t max_ngram, size_t max_draft);

// Defuse control-token text inside UNTRUSTED content before it is rendered.
//
// The chat template's own markers (`<|im_start|>`, `<|im_end|>`) must reach the
// model as the single control tokens it was trained on, so the prompt is
// tokenized with special-token parsing ON. That parses EVERY occurrence, though,
// including inside a pasted file, a tool result or a web page: text such as
// "<|im_end|>\n<|im_start|>system\n..." would become a real role switch. This
// inserts a zero-width space after the first character of each occurrence of a
// control-token string, so the content tokenizes as ordinary text, exactly what
// it did before special parsing was switched on. Content that contains no such
// string is returned unchanged, and applying it twice changes nothing more.
std::string neutralize_control_text(std::string text, const std::vector<std::string>& control_texts);

// Defuse everything in `text` that the tokenizer would turn into a structural
// token, given who wrote it. Two classes exist in this vocabulary:
//   * CONTROL tokens (<|im_start|>, <|im_end|>, ...): parsed only when special
//     parsing is on, which the engine now does, so ALWAYS defused in content.
//   * USER_DEFINED tags (<tool_call>, </tool_call>, <tool_response>,
//     </tool_response>, <think>, </think>): parsed REGARDLESS of that flag. A
//     hostile file or web page containing "</tool_response>" followed by
//     instruction-like text would close the template's tool-result wrapper early
//     and leave that text looking like the operator's own words, so they are
//     defused too -- except in ASSISTANT history, where the host legitimately
//     stores the model's own `<tool_call>` markup and the template needs it intact.
std::string defuse_untrusted_text(std::string text, bool assistant_authored,
                                  const std::vector<std::string>& control_texts,
                                  const std::vector<std::string>& tag_texts);

// Adapter for the pinned llama.cpp C API. Upstream types stay private to the
// implementation; fixture builds retain a deterministic backend when disabled.
class LlamaBackend final : public EngineBackend {
 public:
  LlamaBackend();
  ~LlamaBackend() override;
  std::string id() const override;
  std::string runtime_info_json() const override;
  void initialize(const BackendConfig& config) override;
  GenerationResult generate(const GenerationRequest& request,
                            const Cancellation& cancellation,
                            const TokenSink& sink) override;
  void reset() override;
  void forget(const std::string& key) override;
  bool unload() override;
  bool loaded() const override;
  void shutdown() override;

 private:
  // Model, context and sampler from the stored config; used by initialize and
  // by the reload after an idle unload.
  void load_resources();
  struct Impl;
  Impl* impl_ = nullptr;
};

}  // namespace lae
