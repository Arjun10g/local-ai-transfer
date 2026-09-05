#pragma once

#include "protocol.hpp"
#include "win32_identity.hpp"

#include <atomic>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_set>

namespace lae::windows_broker {

class Broker final {
 public:
  explicit Broker(ManifestLease manifest);
  Broker(const Broker&) = delete;
  Broker& operator=(const Broker&) = delete;
  ~Broker();

  // Owns inherited stdin/stdout until EOF or fatal framing/I/O failure. At most
  // one action runs, while cancel frames remain readable on this thread.
  int serve(HANDLE input, HANDLE output) noexcept;

 private:
  struct ActiveRequest {
    std::string request_id;
    std::atomic<bool> cancelled{false};
    std::atomic<bool> completed{false};
    UniqueHandle start_event;
    std::thread worker;
  };

  bool write(HANDLE output, const Response& response) noexcept;
  bool remember_request_id(const std::string& request_id);
  void reap_completed();
  void cancel_and_reap();
  Response hello(const Request& request) const;
  Response cancel(const Request& request);
  bool start(const Request& request, HANDLE output);

  ManifestLease manifest_;
  std::timed_mutex output_mutex_;
  std::mutex active_mutex_;
  std::mutex replay_mutex_;
  std::unique_ptr<ActiveRequest> active_;
  std::deque<std::string> recent_request_ids_;
  std::unordered_set<std::string> recent_request_id_set_;
};

}  // namespace lae::windows_broker
