#pragma once

#include "engine_backend.hpp"

#include <memory>
#include <string>

namespace lae {

// Product wrapper around the pinned upstream Jinja/Minja evaluator. The
// template source always comes from llama_model_chat_template (GGUF metadata).
class PinnedChatTemplate final {
 public:
  PinnedChatTemplate();
  ~PinnedChatTemplate();
  PinnedChatTemplate(PinnedChatTemplate&&) noexcept;
  PinnedChatTemplate& operator=(PinnedChatTemplate&&) noexcept;
  PinnedChatTemplate(const PinnedChatTemplate&) = delete;
  PinnedChatTemplate& operator=(const PinnedChatTemplate&) = delete;

  void load(const std::string& source);
  std::string render(const std::vector<GenerationRequest::ChatMessage>& messages,
                     const std::vector<GenerationRequest::ToolDefinition>& tools,
                     bool enable_thinking) const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace lae
