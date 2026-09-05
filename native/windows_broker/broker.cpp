#include "broker.hpp"

#include "win32_process.hpp"

#include <algorithm>
#include <utility>

namespace lae::windows_broker {
namespace {

// Allows the worker's two bounded five-second process/Job proof windows plus
// pipe cancellation, while still placing a hard bound on graceful EOF teardown.
constexpr DWORD kBrokerWorkerShutdownMs = 12000;

Response protocol_failure(std::string request_id, std::string code) {
  return Response{std::move(request_id), "failed", std::move(code),
                  nlohmann::json::object(), std::nullopt};
}

}  // namespace

Broker::Broker(ManifestLease manifest) : manifest_(std::move(manifest)) {}

Broker::~Broker() { cancel_and_reap(); }

bool Broker::write(HANDLE output, const Response& response) noexcept {
  std::lock_guard lock(output_mutex_);
  return write_response_frame(output, response);
}

void Broker::reap_completed() {
  std::unique_ptr<ActiveRequest> completed;
  {
    std::lock_guard lock(active_mutex_);
    if (active_ && active_->completed.load(std::memory_order_acquire))
      completed = std::move(active_);
  }
  if (completed && completed->worker.joinable()) completed->worker.join();
}

void Broker::cancel_and_reap() {
  std::unique_ptr<ActiveRequest> active;
  {
    std::lock_guard lock(active_mutex_);
    if (active_) {
      active_->cancelled.store(true, std::memory_order_release);
      active = std::move(active_);
    }
  }
  if (active && active->worker.joinable()) {
    if (active->start_event) SetEvent(active->start_event.get());
    const DWORD waited = active->done_event
                             ? WaitForSingleObject(active->done_event.get(),
                                                   kBrokerWorkerShutdownMs)
                             : WAIT_FAILED;
    if (waited != WAIT_OBJECT_0) {
      // A graceful worker that cannot finish inside its bounded Job-reap window
      // is not allowed to strand descendants. Process death closes the broker's
      // non-inherited Job handle and the kernel applies KILL_ON_JOB_CLOSE.
      TerminateProcess(GetCurrentProcess(), 70);
      return;
    }
    active->worker.join();
  }
}

Response Broker::hello(const Request& request) const {
  nlohmann::json action_ids = nlohmann::json::array();
  for (const auto& [action_id, ignored] : manifest_.manifest.actions) {
    (void)ignored;
    action_ids.push_back(action_id);
  }
  return Response{
      request.request_id,
      "ok",
      "ok",
      {{"protocol", "lae.windows-broker.v1"},
       {"manifest_id", manifest_.manifest.manifest_id},
       {"manifest_sha256", manifest_.manifest_sha256},
       {"action_ids", std::move(action_ids)},
       {"max_concurrency", 1}},
      std::nullopt,
  };
}

Response Broker::cancel(const Request& request) {
  std::lock_guard lock(active_mutex_);
  const bool accepted = active_ && active_->request_id == request.target_request_id &&
                        !active_->completed.load(std::memory_order_acquire);
  if (accepted) active_->cancelled.store(true, std::memory_order_release);
  return Response{request.request_id, "ok", "ok", {{"accepted", accepted}},
                  std::nullopt};
}

bool Broker::start(const Request& request, HANDLE output) {
  reap_completed();
  const auto action = manifest_.manifest.actions.find(request.action_id);
  if (action == manifest_.manifest.actions.end())
    return write(output, protocol_failure(request.request_id, "action_denied"));
  std::string arguments_error;
  if (!action_arguments_match(action->second, request.arguments, arguments_error))
    return write(output, protocol_failure(request.request_id, arguments_error));

  std::lock_guard lock(active_mutex_);
  if (active_)
    return write(output, protocol_failure(request.request_id, "broker_busy"));
  auto active = std::make_unique<ActiveRequest>();
  active->request_id = request.request_id;
  active->start_event = UniqueHandle(CreateEventW(nullptr, TRUE, FALSE, nullptr));
  active->done_event = UniqueHandle(CreateEventW(nullptr, TRUE, FALSE, nullptr));
  if (!active->start_event || !active->done_event)
    return write(output,
                 protocol_failure(request.request_id, "internal_failure"));
  ActiveRequest* state = active.get();
  const ActionSpec action_copy = action->second;
  const FileIdentitySpec* executable = nullptr;
  const DirectoryIdentitySpec* cwd = nullptr;
  if (!action_copy.executable_id.empty())
    executable = &manifest_.manifest.executables.at(action_copy.executable_id);
  if (!action_copy.cwd_id.empty())
    cwd = &manifest_.manifest.directories.at(action_copy.cwd_id);
  const FileIdentitySpec executable_copy = executable ? *executable : FileIdentitySpec{};
  const DirectoryIdentitySpec cwd_copy = cwd ? *cwd : DirectoryIdentitySpec{};
  const bool has_executable = executable != nullptr;
  const bool has_cwd = cwd != nullptr;
  active->worker = std::thread(
      [this, state, request, action_copy, executable_copy, cwd_copy, has_executable,
      has_cwd, output] {
        if (WaitForSingleObject(state->start_event.get(), INFINITE) != WAIT_OBJECT_0) {
          state->completed.store(true, std::memory_order_release);
          SetEvent(state->done_event.get());
          return;
        }
        Response response = execute_bound_action(
            request, action_copy, has_executable ? &executable_copy : nullptr,
            has_cwd ? &cwd_copy : nullptr, manifest_, state->cancelled);
        if (!write(output, response)) state->cancelled.store(true, std::memory_order_release);
        state->completed.store(true, std::memory_order_release);
        SetEvent(state->done_event.get());
      });
  active_ = std::move(active);
  const bool acknowledged = write(
      output, Response{request.request_id, "accepted", "ok", {{"accepted", true}},
                       std::nullopt});
  if (!acknowledged) active_->cancelled.store(true, std::memory_order_release);
  SetEvent(active_->start_event.get());
  return acknowledged;
}

int Broker::serve(HANDLE input, HANDLE output) noexcept {
  try {
    while (true) {
      reap_completed();
      std::string frame;
      std::string frame_error;
      const FrameStatus status = read_request_frame(input, frame, frame_error);
      if (status == FrameStatus::kEndOfStream) {
        cancel_and_reap();  // Parent EOF cancels the full active Job.
        return 0;
      }
      if (status != FrameStatus::kOk) {
        write(output, protocol_failure("broker_protocol_error", frame_error));
        cancel_and_reap();
        return 2;
      }
      Request request;
      std::string request_error;
      if (!parse_request(frame, request, request_error)) {
        if (!write(output,
                   protocol_failure("broker_protocol_error", request_error))) {
          cancel_and_reap();
          return 3;
        }
        continue;
      }
      if (request.kind == RequestKind::kHello) {
        if (!write(output, hello(request))) break;
      } else if (request.kind == RequestKind::kCancel) {
        if (!write(output, cancel(request))) break;
      } else {
        if (!start(request, output)) break;
      }
    }
  } catch (...) {
    cancel_and_reap();
    return 4;
  }
  cancel_and_reap();
  return 3;
}

}  // namespace lae::windows_broker
