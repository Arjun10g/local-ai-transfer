#pragma once

#include "../backend/engine_backend.hpp"

#include <map>
#include <memory>
#include <mutex>
#include <string>

namespace lae {

enum class LifecycleState { NEW, VERIFYING_MODEL, LOADING_MODEL, WARMING, READY, BUSY,
                             DEGRADED, STOPPING, STOPPED, FAILED };

const char* lifecycle_name(LifecycleState state);

struct SessionInfo {
  std::string id;
  unsigned state_version = 1;
  unsigned committed_generations = 0;
};

class Engine final {
 public:
  explicit Engine(std::unique_ptr<EngineBackend> backend);
  ~Engine();

  void initialize(const BackendConfig& config = {});
  void stop();
  LifecycleState state() const;
  std::string backend_id() const;
  std::string model_id() const;
  SessionInfo create_session();
  bool delete_session(const std::string& id);
  bool has_session(const std::string& id) const;
  bool cancel(const std::string& request_id);
  GenerationResult generate(const std::string& request_id, const std::string& session_id,
                            const GenerationRequest& request, const Cancellation& cancellation,
                            const TokenSink& sink);
  std::string metrics_json() const;

 private:
  void set_state(LifecycleState next);
  std::unique_ptr<EngineBackend> backend_;
  mutable std::mutex mutex_;
  LifecycleState state_ = LifecycleState::NEW;
  std::string model_id_ = "fixture";
  std::map<std::string, SessionInfo> sessions_;
  std::map<std::string, Cancellation> active_;
  unsigned next_session_ = 1;
  unsigned cancellation_count_ = 0;
};

}  // namespace lae
