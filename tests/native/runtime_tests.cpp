#include "../../native/backend/fixture_backend.hpp"
#include "../../native/engine/engine.hpp"
#include "../../native/model_validation/model_validator.hpp"
#include "../../native/server/http_server.hpp"
#include "../../native/server/chat_request.hpp"
#include "../../native/backend/llama_chat_template.hpp"
#include "../../native/backend/llama_backend.hpp"
#include "../../native/config/runtime_config.hpp"

#include <cassert>
#include <iostream>
#include <memory>
#include <string>
#include <fstream>
#include <filesystem>

int main() {
  using namespace lae;
  assert(json_escape("\"\\\n\t\x01") == "\\\"\\\\\\n\\t\\u0001");
  assert(context_budget_fits(8, 8, 16));
  assert(!context_budget_fits(8, 9, 16));
  assert(!context_budget_fits(17, 1, 16));
  assert(!context_budget_fits(0, 0, 0));
  GenerationRequest::ChatMessage policy_user{"user", "use the declared tool", "", ""};
  GenerationRequest::ToolDefinition policy_tool{"time.now", "Return time", R"({"type":"object","properties":{},"required":[]})"};
  const auto without_tools = apply_schema_abstention_policy({policy_user}, {});
  assert(without_tools.size() == 1 && without_tools[0].role == "user" && without_tools[0].content == policy_user.content);
  const auto prepended = apply_schema_abstention_policy({policy_user}, {policy_tool});
  assert(prepended.size() == 2 && prepended[0].role == "system" && prepended[1].role == "user" && prepended[1].content == policy_user.content);
  assert(prepended[0].content.find("call only a declared tool") != std::string::npos);
  assert(prepended[0].content.find("every required argument is supplied") != std::string::npos);
  assert(prepended[0].content.find("Never invent unsupported arguments or enum values") != std::string::npos);
  assert(prepended[0].content.find("Otherwise emit no tool call and ask for clarification or refuse") != std::string::npos);
  GenerationRequest::ChatMessage caller_policy{"system", "Caller policy", "", ""};
  GenerationRequest::ChatMessage caller_user{"user", "hello", "", ""};
  const auto merged = apply_schema_abstention_policy({caller_policy, caller_user}, {policy_tool});
  assert(merged.size() == 2 && merged[0].role == "system" && merged[1].content == caller_user.content);
  assert(merged[0].content.find("Caller policy") != std::string::npos);
  assert(merged[0].content.find("Caller policy") > merged[0].content.find("Otherwise emit no tool call"));
  const auto default_batches = context_batch_config(0);
  assert(default_batches.n_ctx == 8192 && default_batches.n_batch == 8192 && default_batches.n_ubatch == 512);
  const auto boundary_batches = context_batch_config(513);
  assert(boundary_batches.n_ctx == 513 && boundary_batches.n_batch == 513 && boundary_batches.n_ubatch == 512);
  const auto large_batches = context_batch_config(1200);
  assert(large_batches.n_ctx == 1200 && large_batches.n_batch == 1200 && large_batches.n_ubatch == 512);
  ChatRequest parsed;
  std::string parse_error;
  assert(parse_chat_request(R"({"model":"fixture","session_id":"s","messages":[{"role":"system","content":"policy"},{"role":"user","content":"say \"hi\""},{"role":"tool","name":"time.now","tool_call_id":"call-1","content":"noon"}],"stream":true,"max_tokens":4,"mode":"normal"})", parsed, parse_error));
  assert(parsed.generation.messages.size() == 3);
  assert(parsed.generation.messages[0].role == "system");
  assert(parsed.generation.messages[1].content == "say \"hi\"");
  assert(parsed.generation.messages[2].name == "time.now");
  assert(parsed.stream && parsed.generation.max_tokens == 4 && !parsed.generation.enable_thinking);
  ChatRequest output_boundary;
  assert(parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"ok"}],"max_tokens":256})", output_boundary, parse_error));
  assert(output_boundary.generation.max_tokens == 256);
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"ok"}],"max_tokens":257})", output_boundary, parse_error));
  ChatRequest tools_request;
  assert(parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"use tool"}],"tools":[{"type":"function","function":{"name":"time.now","description":"Return time","parameters":{"type":"object","properties":{"format":{"type":"string"}},"required":["format"]}}}]})", tools_request, parse_error));
  assert(tools_request.generation.tools.size() == 1);
  assert(tools_request.generation.tools[0].name == "time.now");
  assert(tools_request.generation.tools[0].parameters_json.find("properties") != std::string::npos);
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"use tool"}],"tools":[{"type":"function","function":{"name":"time.now","description":"x","parameters":[]}}]})", parsed, parse_error));
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"use tool"}],"tools":[{"type":"function","function":{"name":"Time.Now","description":"x","parameters":{}}}]})", parsed, parse_error));
  ChatRequest deep_request;
  assert(parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"hello"}],"mode":"deep"})", deep_request, parse_error));
  assert(deep_request.generation.enable_thinking);
#if LAE_ENABLE_LLAMA_CPP
  // Test-only template fixture exercises the pinned Jinja input contract; no
  // Qwen template source is embedded in production.
  PinnedChatTemplate template_fixture;
  template_fixture.load(R"jinja({% for message in messages %}{{ message.role }}:{{ message.content }}
{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant
{{ tools[0].function.name }}
{% if enable_thinking %}<think>
{% else %}<think>

</think>

{% endif %}{% endif %})jinja");
  GenerationRequest::ChatMessage test_message{"user", "hello", "", ""};
  GenerationRequest::ToolDefinition test_tool{"time.now", "Return time", R"({"type":"object","properties":{}})"};
  const auto thinking_off = template_fixture.render({test_message}, {test_tool}, false);
  const auto thinking_on = template_fixture.render({test_message}, {test_tool}, true);
  assert(thinking_off.find("<think>\n\n</think>\n\n") != std::string::npos);
  assert(thinking_off.find("time.now") != std::string::npos);
  assert(thinking_on.find("<think>\n") != std::string::npos && thinking_on.find("</think>") == std::string::npos);
#endif
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"developer","content":"no"}]})", parsed, parse_error));
  assert(parse_error == "invalid_request");
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"ok","extra":true}]})", parsed, parse_error));
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"ok"}],"unknown":1})", parsed, parse_error));
  assert(!parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"unterminated}]})", parsed, parse_error));
  assert(parse_error == "invalid_json");
  assert(!parse_chat_request(std::string(R"({"model":"fixture","messages":[{"role":"user","content":" )") + std::string(32769, 'x') + R"("}]})", parsed, parse_error));
  Engine engine(std::make_unique<FixtureBackend>());
  assert(engine.state() == LifecycleState::NEW);
  engine.initialize();
  assert(engine.state() == LifecycleState::READY);
  assert(engine.backend_id() == "fixture-cpu/0.1.0");
  const auto session = engine.create_session();
  assert(session.id == "sess-00000001");
  assert(engine.has_session(session.id));
  GenerationRequest fixture_request;
  fixture_request.prompt = "ignored";
  fixture_request.max_tokens = 64;

  std::string output;
  auto cancellation = std::make_shared<std::atomic<bool>>(false);
  auto result = engine.generate("req-test", session.id, fixture_request, cancellation,
                               [&](const std::string& token) { output += token; return true; });
  assert(result.finish_reason == "stop");
  assert(output == "fixture response ready for the local engine");

  auto cancelled = std::make_shared<std::atomic<bool>>(false);
  cancelled->store(true);
  output.clear();
  fixture_request.max_tokens = 8;
  result = engine.generate("req-cancelled", session.id, fixture_request, cancelled,
                           [&](const std::string& token) { output += token; return true; });
  assert(result.finish_reason == "cancelled");
  assert(output.empty());

  // Session storage is bounded and eviction is deterministic (oldest opaque ID first).
  for (unsigned i = 0; i < 4; ++i) engine.create_session();
  assert(!engine.has_session(session.id));
  assert(!engine.delete_session(session.id));
  assert(!engine.has_session(session.id));
  engine.stop();
  assert(engine.state() == LifecycleState::STOPPED);

  const auto model_path = std::filesystem::temp_directory_path() / "Qwen3.5-9B-Q4_K_M.gguf";
  const std::string model_text = model_path.generic_string();
  const auto config_path = std::filesystem::temp_directory_path() / "lae-runtime-config.json";
  {
    std::ofstream config(config_path, std::ios::trunc);
    config << "{\"model_path\":\"" << model_text << "\",\"backend_profile\":\"cpu\",\"context_tokens\":8192}\n\t";
  }
  RuntimeConfigFile runtime_config;
  std::string config_error;
  std::error_code cleanup_error;
  assert(load_runtime_config(config_path, runtime_config, config_error));
  assert(runtime_config.model_path == model_text && runtime_config.backend_profile == "cpu" && runtime_config.context_tokens == 8192);
  assert(runtime_config.gpu_layers == 0 && runtime_config.vulkan_device_name.empty() && runtime_config.cuda_device_name.empty());
  {
    std::ofstream invalid_config(config_path, std::ios::trunc);
    invalid_config << "{\"model_path\":\"" << model_text << "\",\"model_path\":\"/other.gguf\"}";
  }
  assert(!load_runtime_config(config_path, runtime_config, config_error));
  assert(config_error.find("duplicate") != std::string::npos);
  {
    std::ofstream invalid_config(config_path, std::ios::trunc);
    invalid_config << "{\"model_path\":\"" << model_text << "\",\"model_sha256\":\"caller-controlled\"}";
  }
  assert(!load_runtime_config(config_path, runtime_config, config_error));
  assert(config_error.find("unknown key") != std::string::npos);
  {
    std::ofstream invalid_config(config_path, std::ios::trunc);
    invalid_config << "{\"model_path\":{\"value\":\"" << model_text << "\"}}";
  }
  assert(!load_runtime_config(config_path, runtime_config, config_error));
  {
    std::ofstream invalid_config(config_path, std::ios::trunc);
    invalid_config << "{\"model_path\":\"relative.gguf\"}";
  }
  assert(!load_runtime_config(config_path, runtime_config, config_error));
  {
    std::ofstream invalid_config(config_path, std::ios::trunc);
    invalid_config << "{\"model_path\":\"" << model_text << "\",\"backend_profile\":\"cpu\",\"gpu_layers\":20}";
  }
  assert(!load_runtime_config(config_path, runtime_config, config_error));
  {
    std::ofstream vulkan_config(config_path, std::ios::trunc);
    vulkan_config << "{\"model_path\":\"" << model_text << "\",\"backend_profile\":\"intel-vulkan\",\"gpu_layers\":20,\"vulkan_device_name\":\"Intel Graphics\"}";
  }
  assert(load_runtime_config(config_path, runtime_config, config_error));
  assert(runtime_config.backend_profile == "intel-vulkan" && runtime_config.gpu_layers == 20 && runtime_config.vulkan_device_name == "Intel Graphics");
  {
    std::ofstream cuda_config(config_path, std::ios::trunc);
    cuda_config << "{\"model_path\":\"" << model_text << "\",\"backend_profile\":\"cuda\",\"gpu_layers\":99,\"cuda_device_name\":\"NVIDIA Test GPU\"}";
  }
  assert(load_runtime_config(config_path, runtime_config, config_error));
  assert(runtime_config.backend_profile == "cuda" && runtime_config.gpu_layers == 99 && runtime_config.cuda_device_name == "NVIDIA Test GPU");
  {
    std::ofstream cross_backend_config(config_path, std::ios::trunc);
    cross_backend_config << "{\"model_path\":\"" << model_text << "\",\"backend_profile\":\"cuda\",\"gpu_layers\":99,\"cuda_device_name\":\"NVIDIA Test GPU\",\"vulkan_device_name\":\"Intel Graphics\"}";
  }
  assert(!load_runtime_config(config_path, runtime_config, config_error));
  const auto relative_config = std::filesystem::path("runtime-config.json");
  assert(!load_runtime_config(relative_config, runtime_config, config_error));
  const auto symlink_path = std::filesystem::temp_directory_path() / "lae-runtime-config-link.json";
  std::filesystem::create_symlink(config_path, symlink_path, cleanup_error);
  if (!cleanup_error) {
    assert(!load_runtime_config(symlink_path, runtime_config, config_error));
    std::filesystem::remove(symlink_path, cleanup_error);
  }
  std::filesystem::remove(config_path, cleanup_error);
  std::cout << "runtime contract/backend tests: PASS\\n";
}
