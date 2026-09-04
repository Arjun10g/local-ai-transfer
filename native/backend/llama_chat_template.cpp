#include "llama_chat_template.hpp"

#include <stdexcept>

#ifdef LAE_ENABLE_LLAMA_CPP
#include "jinja/lexer.h"
#include "jinja/parser.h"
#include "jinja/runtime.h"
#include <nlohmann/json.hpp>

namespace lae {
struct PinnedChatTemplate::Impl {
  std::string source;
  std::unique_ptr<jinja::program> program;
};

PinnedChatTemplate::PinnedChatTemplate() : impl_(std::make_unique<Impl>()) {}
PinnedChatTemplate::~PinnedChatTemplate() = default;
PinnedChatTemplate::PinnedChatTemplate(PinnedChatTemplate&&) noexcept = default;
PinnedChatTemplate& PinnedChatTemplate::operator=(PinnedChatTemplate&&) noexcept = default;

void PinnedChatTemplate::load(const std::string& source) {
  if (source.empty()) throw std::runtime_error("llama chat template unavailable");
  try {
    jinja::lexer lexer;
    auto tokens = lexer.tokenize(source);
    auto program = jinja::parse_from_tokens(tokens);
    impl_->source = tokens.source;
    impl_->program = std::make_unique<jinja::program>(std::move(program));
  } catch (...) {
    throw std::runtime_error("llama chat template parse failed");
  }
}

std::string PinnedChatTemplate::render(const std::vector<GenerationRequest::ChatMessage>& messages,
                                      const std::vector<GenerationRequest::ToolDefinition>& tools,
                                      bool enable_thinking) const {
  if (!impl_->program) throw std::runtime_error("llama chat template is not loaded");
  nlohmann::ordered_json serialized_messages = nlohmann::ordered_json::array();
  for (const auto& message : messages) {
    nlohmann::ordered_json serialized = {
        {"role", message.role},
        {"content", message.content},
    };
    if (!message.name.empty()) serialized["name"] = message.name;
    if (!message.tool_call_id.empty()) serialized["tool_call_id"] = message.tool_call_id;
    serialized_messages.push_back(std::move(serialized));
  }
  nlohmann::ordered_json serialized_tools = nlohmann::ordered_json::array();
  for (const auto& tool : tools) {
    nlohmann::ordered_json parameters;
    try { parameters = nlohmann::ordered_json::parse(tool.parameters_json); }
    catch (...) { throw std::runtime_error("invalid tool parameter schema"); }
    if (!parameters.is_object()) throw std::runtime_error("tool parameter schema must be an object");
    serialized_tools.push_back({{"type", "function"}, {"function", {{"name", tool.name}, {"description", tool.description}, {"parameters", std::move(parameters)}}}});
  }
  nlohmann::ordered_json inputs = {
      {"messages", std::move(serialized_messages)},
      {"bos_token", ""},
      {"eos_token", ""},
      {"enable_thinking", enable_thinking},
      {"add_generation_prompt", true},
  };
  if (!serialized_tools.empty()) inputs["tools"] = std::move(serialized_tools);
  jinja::context context(impl_->source);
  jinja::global_from_json(context, inputs, false);
  jinja::runtime runtime(context);
  const auto rendered = jinja::runtime::gather_string_parts(runtime.execute(*impl_->program));
  if (rendered->val_str.str().empty()) throw std::runtime_error("llama chat template rendered an empty prompt");
  return rendered->val_str.str();
}
}  // namespace lae
#else
namespace lae {
struct PinnedChatTemplate::Impl {};
PinnedChatTemplate::PinnedChatTemplate() : impl_(std::make_unique<Impl>()) {}
PinnedChatTemplate::~PinnedChatTemplate() = default;
PinnedChatTemplate::PinnedChatTemplate(PinnedChatTemplate&&) noexcept = default;
PinnedChatTemplate& PinnedChatTemplate::operator=(PinnedChatTemplate&&) noexcept = default;
void PinnedChatTemplate::load(const std::string&) { throw std::runtime_error("chat templates require llama.cpp"); }
std::string PinnedChatTemplate::render(const std::vector<GenerationRequest::ChatMessage>&, const std::vector<GenerationRequest::ToolDefinition>&, bool) const { throw std::runtime_error("chat templates require llama.cpp"); }
}  // namespace lae
#endif
