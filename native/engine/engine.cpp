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
  {
    std::lock_guard<std::mutex> lock(mutex_);
    model_id_ = config.model_id;
  }
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
std::string Engine::model_id() const { std::lock_guard<std::mutex> lock(mutex_); return model_id_; }

SessionInfo Engine::create_session() {
  std::lock_guard<std::mutex> lock(mutex_);
  // A session is bookkeeping and touches no model state, so BUSY is allowed.
  // Refusing it while a generation ran made the server answer 503 `not_ready`
  // to a caller whose only problem was that the engine was busy, which is how
  // one abandoned 600 s request turned into a run of misleading refusals
  // (`ENGINE-ABANDONED-REQUEST-STATE-001`).
  if (state_ != LifecycleState::READY && state_ != LifecycleState::BUSY)
    throw std::runtime_error("engine is not ready");
  constexpr size_t kMaxSessions = 4;
  if (sessions_.size() >= kMaxSessions) {
    // Evict the oldest session that is NOT generating. Creating a session is
    // allowed while BUSY, so the oldest one can be the very session running;
    // erasing it would resurrect it as a default entry when it finishes and
    // leave the map one over its cap for good.
    auto victim = sessions_.begin();
    while (victim != sessions_.end() && victim->first == active_session_) ++victim;
    if (victim != sessions_.end()) sessions_.erase(victim);
  }
  std::ostringstream id;
  id << "sess-" << std::string(8 - std::min<size_t>(8, std::to_string(next_session_).size()), '0')
     << next_session_++;
  SessionInfo info{id.str(), 1, 0};
  sessions_.emplace(info.id, info);
  return info;
}

bool Engine::delete_session(const std::string& id) {
  std::lock_guard<std::mutex> lock(mutex_);
  const bool erased = sessions_.erase(id) != 0;
  // Drop the deleted conversation's retained prompt state too, or its tokens
  // would sit in memory until some other request displaced them. Only when no
  // generation is running: the backend is single-flight and `generate` does
  // not hold this mutex, so touching its context now would race it. The
  // mutex does stop a new generation from starting while we do this.
  if (erased && active_.empty() && backend_) backend_->forget(id);
  return erased;
}

bool Engine::has_session(const std::string& id) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return sessions_.find(id) != sessions_.end();
}

bool Engine::cancel(const std::string& request_id) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto it = active_.find(request_id);
  if (it != active_.end()) {
    it->second->store(true);
    ++cancellation_count_;
    return true;
  }
  // Announced but not generating yet: set the flag now so generation aborts as
  // soon as it starts, rather than answering "unknown request".
  for (auto& announced : announced_) {
    if (announced.first != request_id) continue;
    announced.second->store(true);
    ++cancellation_count_;
    return true;
  }
  return false;
}

void Engine::announce(const std::string& request_id, const Cancellation& cancellation) {
  std::lock_guard<std::mutex> lock(mutex_);
  constexpr size_t kMaxAnnounced = 32;
  if (announced_.size() >= kMaxAnnounced) announced_.erase(announced_.begin());
  announced_.emplace_back(request_id, cancellation);
}

void Engine::cancel_all() {
  std::lock_guard<std::mutex> lock(mutex_);
  for (auto& item : active_) {
    if (!item.second->exchange(true)) ++cancellation_count_;
  }
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
    announced_.erase(std::remove_if(announced_.begin(), announced_.end(),
                                    [&](const auto& entry) { return entry.first == request_id; }),
                     announced_.end());
    active_session_ = session_id;
    state_ = LifecycleState::BUSY;
  }
  GenerationResult result;
  try {
    // The session scopes prompt-prefix reuse in the backend, so one
    // conversation's retained context never extends another's.
    GenerationRequest scoped = request;
    scoped.cache_key = session_id;
    result = backend_->generate(scoped, cancellation, sink);
  } catch (...) {
    std::lock_guard<std::mutex> lock(mutex_);
    active_.erase(request_id);
    active_session_.clear();
    state_ = LifecycleState::READY;
    throw;
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    active_.erase(request_id);
    active_session_.clear();
    // find, not operator[]: the session may have been deleted mid-generation,
    // and operator[] would silently recreate it as an empty default entry.
    const auto session = sessions_.find(session_id);
    if (!session_id.empty() && result.finish_reason != "cancelled" && session != sessions_.end())
      ++session->second.committed_generations;
    state_ = LifecycleState::READY;
  }
  return result;
}

std::string Engine::metrics_json() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::ostringstream out;
  out << "{\"lifecycle\":\"" << lifecycle_name(state_) << "\",\"backend\":\"" << backend_id()
      << "\",\"active_sessions\":" << sessions_.size() << ",\"active_generations\":" << active_.size()
      << ",\"cancellations\":" << cancellation_count_ << ",\"runtime\":" << backend_->runtime_info_json() << "}";
  return out.str();
}

}  // namespace lae
