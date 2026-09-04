#include "engine.hpp"

#include <sstream>
#include <stdexcept>
#include <algorithm>

namespace lae {

const char* lifecycle_name(LifecycleState state) {
  switch (state) {
    case LifecycleState::NEW: return "NEW";
    case LifecycleState::VERIFYING_MODEL: return "VERIFYING_MODEL";
    case LifecycleState::LOADING_MODEL: return "LOADING_MODEL";
    case LifecycleState::WARMING: return "WARMING";
    case LifecycleState::READY: return "READY";
    case LifecycleState::BUSY: return "BUSY";
    case LifecycleState::DEGRADED: return "DEGRADED";
    case LifecycleState::STOPPING: return "STOPPING";
    case LifecycleState::STOPPED: return "STOPPED";
    case LifecycleState::FAILED: return "FAILED";
  }
  return "FAILED";
}

Engine::Engine(std::unique_ptr<EngineBackend> backend) : backend_(std::move(backend)) {}
Engine::~Engine() { stop(); }

void Engine::set_state(LifecycleState next) {
  std::lock_guard<std::mutex> lock(mutex_);
  state_ = next;
}

void Engine::initialize(const BackendConfig& config) {
  set_state(LifecycleState::VERIFYING_MODEL);
  set_state(LifecycleState::LOADING_MODEL);
  try {
    backend_->initialize(config);
    set_state(LifecycleState::WARMING);
    backend_->reset();
    set_state(LifecycleState::READY);
  } catch (...) {
    set_state(LifecycleState::FAILED);
    throw;
  }
}

void Engine::stop() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (state_ == LifecycleState::STOPPED || state_ == LifecycleState::STOPPING) return;
    state_ = LifecycleState::STOPPING;
    for (auto& item : active_) item.second->store(true);
  }
  if (backend_) backend_->shutdown();
  set_state(LifecycleState::STOPPED);
}

LifecycleState Engine::state() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return state_;
}

std::string Engine::backend_id() const { return backend_ ? backend_->id() : "none"; }

SessionInfo Engine::create_session() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (state_ != LifecycleState::READY) throw std::runtime_error("engine is not ready");
  constexpr size_t kMaxSessions = 64;
  if (sessions_.size() >= kMaxSessions) sessions_.erase(sessions_.begin());
  std::ostringstream id;
  id << "sess-" << std::string(8 - std::min<size_t>(8, std::to_string(next_session_).size()), '0')
     << next_session_++;
  SessionInfo info{id.str(), 1, 0};
  sessions_.emplace(info.id, info);
  return info;
}

bool Engine::delete_session(const std::string& id) {
  std::lock_guard<std::mutex> lock(mutex_);
  return sessions_.erase(id) != 0;
}

bool Engine::has_session(const std::string& id) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return sessions_.find(id) != sessions_.end();
}

bool Engine::cancel(const std::string& request_id) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto it = active_.find(request_id);
  if (it == active_.end()) return false;
  it->second->store(true);
  ++cancellation_count_;
  return true;
}

GenerationResult Engine::generate(const std::string& request_id, const std::string& session_id,
                                   const GenerationRequest& request, const Cancellation& cancellation,
                                   const TokenSink& sink) {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (state_ != LifecycleState::READY) throw std::runtime_error("engine is not ready");
    if (!session_id.empty() && sessions_.find(session_id) == sessions_.end())
      throw std::invalid_argument("unknown session");
    if (!active_.empty()) throw std::logic_error("another generation is active");
    active_[request_id] = cancellation;
    state_ = LifecycleState::BUSY;
  }
  GenerationResult result;
  try {
    result = backend_->generate(request, cancellation, sink);
  } catch (...) {
    std::lock_guard<std::mutex> lock(mutex_);
    active_.erase(request_id);
    state_ = LifecycleState::READY;
    throw;
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    active_.erase(request_id);
    if (!session_id.empty() && result.finish_reason != "cancelled") ++sessions_[session_id].committed_generations;
    state_ = LifecycleState::READY;
  }
  return result;
}

std::string Engine::metrics_json() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::ostringstream out;
  out << "{\"lifecycle\":\"" << lifecycle_name(state_) << "\",\"backend\":\"" << backend_id()
      << "\",\"active_sessions\":" << sessions_.size() << ",\"active_generations\":" << active_.size()
      << ",\"cancellations\":" << cancellation_count_ << "}";
  return out.str();
}

}  // namespace lae
