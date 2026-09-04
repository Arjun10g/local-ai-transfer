#include "../../native/backend/fixture_backend.hpp"
#include "../../native/engine/engine.hpp"
#include "../../native/model_validation/model_validator.hpp"
#include "../../native/server/http_server.hpp"
#include "../../native/server/chat_request.hpp"
#include "../../native/backend/llama_chat_template.hpp"
#include "../../native/backend/llama_backend.hpp"

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
  ChatRequest parsed;
  std::string parse_error;
  assert(parse_chat_request(R"({"model":"fixture","session_id":"s","messages":[{"role":"system","content":"policy"},{"role":"user","content":"say \"hi\""},{"role":"tool","name":"time.now","tool_call_id":"call-1","content":"noon"}],"stream":true,"max_tokens":4,"mode":"normal"})", parsed, parse_error));
  assert(parsed.generation.messages.size() == 3);
  assert(parsed.generation.messages[0].role == "system");
  assert(parsed.generation.messages[1].content == "say \"hi\"");
  assert(parsed.generation.messages[2].name == "time.now");
  assert(parsed.stream && parsed.generation.max_tokens == 4 && !parsed.generation.enable_thinking);
  ChatRequest deep_request;
  assert(parse_chat_request(R"({"model":"fixture","messages":[{"role":"user","content":"hello"}],"mode":"deep"})", deep_request, parse_error));
  assert(deep_request.generation.enable_thinking);
#if LAE_ENABLE_LLAMA_CPP
  // Test-only template fixture exercises the pinned Jinja input contract; no
  // Qwen template source is embedded in production.
  PinnedChatTemplate template_fixture;
  template_fixture.load(R"jinja({% for message in messages %}{{ message.role }}:{{ message.content }}
{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant
{% if enable_thinking %}<think>
{% else %}<think>

</think>

{% endif %}{% endif %})jinja");
  GenerationRequest::ChatMessage test_message{"user", "hello", "", ""};
  const auto thinking_off = template_fixture.render({test_message}, false);
  const auto thinking_on = template_fixture.render({test_message}, true);
  assert(thinking_off.find("<think>\n\n</think>\n\n") != std::string::npos);
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
  {
    std::ofstream model(model_path, std::ios::binary | std::ios::trunc);
    const unsigned char fixture[] = {'G','G','U','F',3,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0};
    model.write(reinterpret_cast<const char*>(fixture), sizeof(fixture));
  }
  lae::ModelProfile profile; profile.expected_size_bytes = 24; profile.expected_sha256 = "a4e5e156ddec27e286f75328784d7106b60a4eb1d246e950a001a3f944fbda99";
  const auto accepted = lae::validate_model_file(model_path, profile, true);
  assert(accepted.valid && accepted.code == "ok" && accepted.gguf_version == 3);
  const auto rejected_real = lae::validate_model_file(model_path, profile, false);
  assert(!rejected_real.valid && rejected_real.code == "model_profile_missing_metadata");
  profile.expected_sha256 = std::string(64, '0');
  const auto rejected_hash = lae::validate_model_file(model_path, profile, true);
  assert(!rejected_hash.valid && rejected_hash.code == "model_hash_mismatch");
  std::error_code cleanup_error; std::filesystem::remove(model_path, cleanup_error);
  std::cout << "runtime contract/backend tests: PASS\\n";
}
