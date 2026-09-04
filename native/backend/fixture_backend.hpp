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

 private:
  bool initialized_ = false;
};

}  // namespace lae
