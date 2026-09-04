#pragma once

#include "../engine/engine.hpp"

#include <atomic>
#include <cstdint>
#include <map>
#include <string>
#include <thread>
#include <condition_variable>

namespace lae {

// Escapes arbitrary UTF-8 bytes for a JSON string without emitting raw
// control characters. Used for model-generated SSE and JSON responses.
std::string json_escape(const std::string& value);

class HttpServer final {
 public:
  using Socket = intptr_t;
  HttpServer(Engine& engine, std::string bearer_token);
  ~HttpServer();
  // port 0 requests an ephemeral OS-assigned port.
  unsigned start(unsigned port);
  void stop();
  unsigned port() const { return port_; }

 private:
  void accept_loop();
  void handle(Socket client);
  void respond(Socket client, int status, const std::string& type, const std::string& body,
               const std::string& request_id = "");
  bool authorized(const std::map<std::string, std::string>& headers) const;
  Engine& engine_;
  std::string bearer_token_;
  std::atomic<bool> stopping_{false};
  bool network_initialized_ = false;
  Socket listen_socket_ = -1;
  unsigned port_ = 0;
  std::thread accept_thread_;
  std::atomic<unsigned> active_connections_{0};
  std::atomic<unsigned> active_workers_{0};
  std::mutex workers_wait_mutex_;
  std::condition_variable workers_wait_cv_;
};

}  // namespace lae
