#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace lae {

class ModelValidationLease;

struct BackendConfig {
  std::string model_path;
  std::string model_id = "fixture";
  std::string backend_profile = "fixture-cpu";
  unsigned context_tokens = 8192;
  // Bounded Vulkan offload policy. CPU ignores this; intel-vulkan requires
  // an explicit positive value and never silently falls back.
  unsigned gpu_layers = 20;
  // CPU threads for prefill and generation; 0 means every logical thread.
  unsigned threads = 0;
  // CPU threads for prompt processing only; 0 means "same as `threads`".
  // Prompt processing is compute-bound and can use more cores than generation,
  // which is memory-bandwidth-bound and is often fastest on fewer.
  unsigned threads_batch = 0;
  // Draft-free (n-gram) speculative decoding: how many tokens may be drafted
  // and verified per forward pass. 0 disables it, which is the default and
  // leaves the generation path unchanged. It also sizes the recurrent-state
  // rollback (`n_rs_seq`) the hybrid model needs to discard a rejected draft.
  unsigned speculate_tokens = 0;
  std::string vulkan_device_name;
  std::string cuda_device_name;
  std::shared_ptr<ModelValidationLease> model_lease;
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
  // Scopes reuse of an already-computed prompt prefix to one conversation.
  // Reuse is sound regardless of this value -- it only ever skips work for an
  // exact token-prefix match -- but keying it makes the behaviour auditable
  // and keeps unrelated conversations from extending each other's state.
  std::string cache_key;
};

struct GenerationResult {
  std::string finish_reason;
  unsigned generated_tokens = 0;
  unsigned prompt_tokens = 0;
  // Prompt tokens served from the retained context instead of recomputed.
  unsigned reused_prefix_tokens = 0;
};

using Cancellation = std::shared_ptr<std::atomic<bool>>;
using TokenSink = std::function<bool(const std::string& token)>;

// Product-owned boundary. No upstream llama.cpp/ggml type may cross this API.
class EngineBackend {
 public:
  virtual ~EngineBackend() = default;
  virtual std::string id() const = 0;
  // Bounded runtime identity for diagnostics/metrics; must never contain
  // paths, prompts, model output, or device secrets.
  virtual std::string runtime_info_json() const { return "{}"; }
  virtual void initialize(const BackendConfig& config) = 0;
  virtual GenerationResult generate(const GenerationRequest& request,
                                    const Cancellation& cancellation,
                                    const TokenSink& sink) = 0;
  virtual void reset() = 0;
  // Drop any context retained for `key` (a deleted session's conversation).
  // Backends that retain nothing keep the default.
  virtual void forget(const std::string& /*key*/) {}
  virtual void shutdown() = 0;
};

}  // namespace lae
