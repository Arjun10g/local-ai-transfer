#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace lae {

struct BackendConfig {
  std::string model_path;
  std::string backend_profile = "fixture-cpu";
};

struct GenerationRequest {
  std::string prompt;
  unsigned max_tokens = 8;
};

struct GenerationResult {
  std::string finish_reason;
  unsigned generated_tokens = 0;
};

using Cancellation = std::shared_ptr<std::atomic<bool>>;
using TokenSink = std::function<bool(const std::string& token)>;

// Product-owned boundary. No upstream llama.cpp/ggml type may cross this API.
class EngineBackend {
 public:
  virtual ~EngineBackend() = default;
  virtual std::string id() const = 0;
  virtual void initialize(const BackendConfig& config) = 0;
  virtual GenerationResult generate(const GenerationRequest& request,
                                    const Cancellation& cancellation,
                                    const TokenSink& sink) = 0;
  virtual void reset() = 0;
  virtual void shutdown() = 0;
};

}  // namespace lae
