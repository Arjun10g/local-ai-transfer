#include "../../native/backend/fixture_backend.hpp"
#include "../../native/engine/engine.hpp"

#include <cassert>
#include <iostream>
#include <memory>
#include <string>

int main() {
  using namespace lae;
  Engine engine(std::make_unique<FixtureBackend>());
  assert(engine.state() == LifecycleState::NEW);
  engine.initialize();
  assert(engine.state() == LifecycleState::READY);
  assert(engine.backend_id() == "fixture-cpu/0.1.0");
  const auto session = engine.create_session();
  assert(session.id == "sess-00000001");
  assert(engine.has_session(session.id));

  std::string output;
  auto cancellation = std::make_shared<std::atomic<bool>>(false);
  auto result = engine.generate("req-test", session.id, {"ignored", 64}, cancellation,
                               [&](const std::string& token) { output += token; return true; });
  assert(result.finish_reason == "stop");
  assert(output == "fixture response ready for the local engine");

  auto cancelled = std::make_shared<std::atomic<bool>>(false);
  cancelled->store(true);
  output.clear();
  result = engine.generate("req-cancelled", session.id, {"ignored", 8}, cancelled,
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
  std::cout << "runtime contract/backend tests: PASS\\n";
}
