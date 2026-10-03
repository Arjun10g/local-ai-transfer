#include "fixture_backend.hpp"

#include <chrono>
#include <stdexcept>
#include <thread>

namespace lae {

void FixtureBackend::initialize(const BackendConfig& config) {
  if (config.backend_profile != "fixture-cpu") {
    throw std::invalid_argument("unsupported fixture backend profile");
  }
  // The fixture intentionally does not open, map, download, or infer from a model.
  initialized_ = true;
  loaded_ = true;
}

GenerationResult FixtureBackend::generate(const GenerationRequest& request,
                                          const Cancellation& cancellation,
                                          const TokenSink& sink) {
  if (!initialized_) throw std::runtime_error("backend is not initialized");
  if (!loaded_) { loaded_ = true; ++reloads_; }  // a request after an idle unload loads again
  static const std::vector<std::string> tokens = {
      "fixture", " ", "response", " ", "ready", " ", "for", " ", "the", " ", "local", " ", "engine"};
  const unsigned limit = request.max_tokens == 0 ? 1 : request.max_tokens;
  GenerationResult result;
  for (unsigned i = 0; i < limit && i < tokens.size(); ++i) {
    if (cancellation->load()) {
      result.finish_reason = "cancelled";
      return result;
    }
    // Make cancellation deterministic and observable during a fixture stream.
    std::this_thread::sleep_for(std::chrono::milliseconds(8));
    if (!sink(tokens[i])) {
      result.finish_reason = "cancelled";
      return result;
    }
    ++result.generated_tokens;
  }
  result.finish_reason = (limit < tokens.size()) ? "length" : "stop";
  return result;
}

}  // namespace lae
