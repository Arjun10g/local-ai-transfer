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
  void shutdown() override;

 private:
  struct Impl;
  Impl* impl_ = nullptr;
};

}  // namespace lae
