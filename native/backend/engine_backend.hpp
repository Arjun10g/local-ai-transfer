#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace lae {

struct BackendConfig {
  std::string model_path;
  std::string model_id = "fixture";
  std::string backend_profile = "fixture-cpu";
  unsigned context_tokens = 8192;
  // Bounded Vulkan offload policy. CPU ignores this; intel-vulkan requires
  // an explicit positive value and never silently falls back.
  unsigned gpu_layers = 20;
  std::string vulkan_device_name;
  std::string cuda_device_name;
};

struct GenerationRequest {
  std::string prompt;
  // Ordered host/native chat history. The fixture may ignore the content, but
  // real backends must render this complete history through the model template.
  struct ChatMessage {
    std::string role;
    std::string content;
    std::string name;
    std::string tool_call_id;
  };
  struct ToolDefinition {
    std::string name;
    std::string description;
    // Canonical JSON object for the function parameter schema.
    std::string parameters_json;
  };
  std::vector<ChatMessage> messages;
  std::vector<ToolDefinition> tools;
  unsigned max_tokens = 8;
  bool enable_thinking = false;
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
