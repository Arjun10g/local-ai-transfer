#pragma once

#include "../backend/engine_backend.hpp"

#include <chrono>
#include <condition_variable>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

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
  // A streaming request sends its response head, which carries the request id a
  // client cancels by, BEFORE `generate` registers the request. Announcing it
  // first closes that window: a cancel that arrives in between is remembered
  // and takes effect the moment generation starts, instead of being refused.
  void announce(const std::string& request_id, const Cancellation& cancellation);
  void cancel_all();
  GenerationResult generate(const std::string& request_id, const std::string& session_id,
                            const GenerationRequest& request, const Cancellation& cancellation,
                            const TokenSink& sink);
  std::string metrics_json() const;
  // Free the model after `idle` with no request in flight (zero turns it off);
  // the next request loads it again. `poll` is how often the clock is checked
  // (tests use milliseconds; the server uses a few seconds). Call after
  // initialize; stop() ends the watcher.
  void enable_idle_unload(std::chrono::milliseconds idle, std::chrono::milliseconds poll = std::chrono::seconds(5));

 private:
  void set_state(LifecycleState next);
  void touch();  // guarded by mutex_
  std::unique_ptr<EngineBackend> backend_;
  mutable std::mutex mutex_;
  LifecycleState state_ = LifecycleState::NEW;
  std::string model_id_ = "fixture";
  std::map<std::string, SessionInfo> sessions_;
  // The session whose generation is in flight, if any; guarded by mutex_.
  // Eviction must never pick it (see create_session).
  std::string active_session_;
  // Requests announced but not yet generating; bounded, oldest dropped first.
  std::vector<std::pair<std::string, Cancellation>> announced_;
  std::map<std::string, Cancellation> active_;
  unsigned next_session_ = 1;
  unsigned cancellation_count_ = 0;
  // Idle unload: a watcher thread that frees the model after a quiet period.
  std::chrono::steady_clock::time_point last_activity_ = std::chrono::steady_clock::now();
  std::chrono::milliseconds idle_unload_{0};
  unsigned idle_unloads_ = 0;
  bool idle_stop_ = false;
  std::condition_variable idle_cv_;
  std::thread idle_thread_;
};

}  // namespace lae
