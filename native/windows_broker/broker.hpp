#pragma once

#include "protocol.hpp"
#include "win32_identity.hpp"

#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

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
    UniqueHandle done_event;
    std::thread worker;
  };

  bool write(HANDLE output, const Response& response) noexcept;
  void reap_completed();
  void cancel_and_reap();
  Response hello(const Request& request) const;
  Response cancel(const Request& request);
  bool start(const Request& request, HANDLE output);

  ManifestLease manifest_;
  std::mutex output_mutex_;
  std::mutex active_mutex_;
  std::unique_ptr<ActiveRequest> active_;
};

}  // namespace lae::windows_broker
