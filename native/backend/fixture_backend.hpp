#pragma once

#include "engine_backend.hpp"

namespace lae {

class FixtureBackend final : public EngineBackend {
 public:
  std::string id() const override { return "fixture-cpu/0.1.0"; }
  void initialize(const BackendConfig& config) override;
  GenerationResult generate(const GenerationRequest& request,
                            const Cancellation& cancellation,
                            const TokenSink& sink) override;
  void reset() override {}
  void shutdown() override {}
  // Models the load/unload cycle without a model, so the Engine's idle logic is testable.
  bool unload() override { if (!loaded_) return false; loaded_ = false; return true; }
  bool loaded() const override { return loaded_; }
  std::string runtime_info_json() const override { return std::string("{\"model_loaded\":") + (loaded_ ? "true" : "false") + ",\"reloads\":" + std::to_string(reloads_) + "}"; }

 private:
  bool initialized_ = false;
  bool loaded_ = true;
  unsigned reloads_ = 0;
};

}  // namespace lae
