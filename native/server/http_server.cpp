#include "http_server.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstring>
#include <sstream>
#include <stdexcept>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace lae {
namespace {

constexpr size_t kMaxBody = 64 * 1024;
std::atomic<unsigned> request_counter{1};

#ifdef _WIN32
using NativeSocket = SOCKET;
NativeSocket native_socket(HttpServer::Socket socket) { return static_cast<SOCKET>(socket); }
#else
using NativeSocket = int;
NativeSocket native_socket(HttpServer::Socket socket) { return static_cast<int>(socket); }
#endif

void close_socket(HttpServer::Socket socket) {
#ifdef _WIN32
  closesocket(native_socket(socket));
#else
  ::close(native_socket(socket));
#endif
}

bool send_all(HttpServer::Socket socket, const std::string& text) {
  size_t sent = 0;
  while (sent < text.size()) {
#ifdef _WIN32
    const int n = ::send(native_socket(socket), text.data() + sent,
                         static_cast<int>(text.size() - sent), 0);
#else
    const ssize_t n = ::send(static_cast<int>(socket), text.data() + sent, text.size() - sent, 0);
#endif
    if (n <= 0) return false;
    sent += static_cast<size_t>(n);
  }
  return true;
}

std::string lower(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) { return std::tolower(c); });
  return value;
}

std::string trim(std::string value) {
  while (!value.empty() && std::isspace(static_cast<unsigned char>(value.front()))) value.erase(value.begin());
  while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back()))) value.pop_back();
  return value;
}

bool valid_loopback_authority(const std::string& authority) {
  if (authority.empty() || authority.find_first_of("/\\?#@") != std::string::npos) return false;
  for (const unsigned char c : authority) if (std::isspace(c)) return false;
  const size_t colon = authority.find(':');
  if (colon != std::string::npos && authority.find(':', colon + 1) != std::string::npos) return false;
  const std::string host = lower(colon == std::string::npos ? authority : authority.substr(0, colon));
  if (host != "127.0.0.1" && host != "localhost") return false;
  if (colon == std::string::npos) return true;
  const std::string port = authority.substr(colon + 1);
  if (port.empty() || port.size() > 5 || !std::all_of(port.begin(), port.end(), [](unsigned char c) { return std::isdigit(c); })) return false;
  try { const unsigned value = std::stoul(port); return value >= 1 && value <= 65535; }
  catch (...) { return false; }
}

bool valid_loopback_origin(const std::string& origin) {
  constexpr const char* scheme = "http://";
  if (origin.rfind(scheme, 0) != 0) return false;
  const std::string authority = origin.substr(std::strlen(scheme));
  return valid_loopback_authority(authority);
}

std::string json_string(const std::string& body, const std::string& key) {
  const std::string marker = "\"" + key + "\"";
  const size_t start = body.find(marker);
  if (start == std::string::npos) return {};
  size_t colon = body.find(':', start + marker.size());
  if (colon == std::string::npos) return {};
  size_t quote = body.find('"', colon + 1);
  if (quote == std::string::npos) return {};
  size_t end = quote + 1;
  while (end < body.size()) {
    if (body[end] == '"' && body[end - 1] != '\\') break;
    ++end;
  }
  return end < body.size() ? body.substr(quote + 1, end - quote - 1) : std::string{};
}

unsigned json_unsigned(const std::string& body, const std::string& key, unsigned fallback) {
  const std::string marker = "\"" + key + "\"";
  size_t pos = body.find(marker);
  if (pos == std::string::npos) return fallback;
  pos = body.find(':', pos + marker.size());
  if (pos == std::string::npos) return fallback;
  ++pos;
  while (pos < body.size() && std::isspace(static_cast<unsigned char>(body[pos]))) ++pos;
  if (pos >= body.size() || !std::isdigit(static_cast<unsigned char>(body[pos]))) return fallback;
  unsigned value = 0;
  while (pos < body.size() && std::isdigit(static_cast<unsigned char>(body[pos]))) {
    if (value > 1000000) return fallback + 1;
    value = value * 10 + static_cast<unsigned>(body[pos++] - '0');
  }
  return value;
}

std::string next_request_id() {
  std::ostringstream out;
  out << "req-" << request_counter.fetch_add(1);
  return out.str();
}

const char* reason(int status) {
  switch (status) {
    case 200: return "OK"; case 201: return "Created"; case 400: return "Bad Request";
    case 401: return "Unauthorized"; case 404: return "Not Found"; case 405: return "Method Not Allowed";
    case 409: return "Conflict"; case 413: return "Payload Too Large"; case 499: return "Client Closed Request";
    case 503: return "Service Unavailable"; default: return "Internal Server Error";
  }
}

}  // namespace

HttpServer::HttpServer(Engine& engine, std::string bearer_token)
    : engine_(engine), bearer_token_(std::move(bearer_token)) {}
HttpServer::~HttpServer() { stop(); }

unsigned HttpServer::start(unsigned port) {
  if (listen_socket_ != -1) throw std::logic_error("server already started");
#ifdef _WIN32
  WSADATA data{};
  if (WSAStartup(MAKEWORD(2, 2), &data) != 0) throw std::runtime_error("winsock initialization failed");
#endif
  listen_socket_ = static_cast<Socket>(::socket(AF_INET, SOCK_STREAM, 0));
#ifdef _WIN32
  if (native_socket(listen_socket_) == INVALID_SOCKET) throw std::runtime_error("socket creation failed");
#else
  if (listen_socket_ < 0) throw std::runtime_error("socket creation failed");
#endif
  int yes = 1;
  setsockopt(native_socket(listen_socket_), SOL_SOCKET, SO_REUSEADDR,
             reinterpret_cast<const char*>(&yes), sizeof(yes));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  address.sin_port = htons(static_cast<unsigned short>(port));
  if (::bind(native_socket(listen_socket_), reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 ||
      ::listen(native_socket(listen_socket_), 16) < 0) {
    close_socket(listen_socket_); listen_socket_ = -1; throw std::runtime_error("loopback bind/listen failed");
  }
  sockaddr_in actual{}; socklen_t length = sizeof(actual);
  getsockname(native_socket(listen_socket_), reinterpret_cast<sockaddr*>(&actual), &length);
  port_ = ntohs(actual.sin_port);
  stopping_ = false;
  accept_thread_ = std::thread(&HttpServer::accept_loop, this);
  return port_;
}

void HttpServer::stop() {
  if (listen_socket_ == -1) return;
  stopping_ = true;
  const Socket socket = listen_socket_;
  close_socket(socket);
  if (accept_thread_.joinable()) accept_thread_.join();
  listen_socket_ = -1;
  std::lock_guard<std::mutex> lock(workers_mutex_);
  for (auto& worker : workers_) if (worker.joinable()) worker.join();
  workers_.clear();
#ifdef _WIN32
  WSACleanup();
#endif
}

void HttpServer::accept_loop() {
  while (!stopping_) {
    sockaddr_in peer{}; socklen_t length = sizeof(peer);
    const Socket client = static_cast<Socket>(::accept(native_socket(listen_socket_), reinterpret_cast<sockaddr*>(&peer), &length));
#ifdef _WIN32
    if (native_socket(client) == INVALID_SOCKET) { if (!stopping_) continue; break; }
#else
    if (client < 0) { if (!stopping_) continue; break; }
#endif
    if (active_connections_.fetch_add(1) >= 16) {
      active_connections_.fetch_sub(1);
      close_socket(client);
      continue;
    }
    std::lock_guard<std::mutex> lock(workers_mutex_);
    workers_.emplace_back(&HttpServer::handle, this, client);
  }
}

bool HttpServer::authorized(const std::map<std::string, std::string>& headers) const {
  auto it = headers.find("authorization");
  return it != headers.end() && it->second == "Bearer " + bearer_token_;
}

void HttpServer::respond(Socket client, int status, const std::string& type, const std::string& body,
                         const std::string& request_id) {
  std::ostringstream out;
  out << "HTTP/1.1 " << status << " " << reason(status) << "\r\nContent-Type: " << type
      << "\r\nContent-Length: " << body.size() << "\r\nConnection: close\r\nX-Request-Id: "
      << (request_id.empty() ? next_request_id() : request_id) << "\r\n\r\n" << body;
  send_all(client, out.str());
}

void HttpServer::handle(Socket client) {
  struct ConnectionGuard {
    std::atomic<unsigned>& count;
    ~ConnectionGuard() { count.fetch_sub(1); }
  } connection_guard{active_connections_};
  const std::string request_id = next_request_id();
  std::string raw; char buffer[4096];
  while (raw.find("\r\n\r\n") == std::string::npos && raw.size() <= kMaxBody + 8192) {
#ifdef _WIN32
    const int n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#else
    const ssize_t n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#endif
    if (n <= 0) { close_socket(client); return; }
    raw.append(buffer, static_cast<size_t>(n));
  }
  const size_t split = raw.find("\r\n\r\n");
  if (split == std::string::npos) { respond(client, 413, "application/json", "{\"error\":{\"code\":\"request_too_large\"}}", request_id); close_socket(client); return; }
  std::istringstream headers_stream(raw.substr(0, split));
  std::string request_line; std::getline(headers_stream, request_line);
  if (!request_line.empty() && request_line.back() == '\r') request_line.pop_back();
  std::istringstream request_parts(request_line); std::string method, path, protocol;
  request_parts >> method >> path >> protocol;
  std::map<std::string, std::string> headers; std::string line;
  while (std::getline(headers_stream, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const size_t colon = line.find(':'); if (colon == std::string::npos) continue;
    headers[lower(trim(line.substr(0, colon)))] = trim(line.substr(colon + 1));
  }
  size_t content_length = 0;
  if (headers.count("content-length")) {
    try { content_length = std::stoul(headers["content-length"]); } catch (...) { content_length = kMaxBody + 1; }
  }
  if (content_length > kMaxBody) { respond(client, 413, "application/json", "{\"error\":{\"code\":\"request_too_large\"}}", request_id); close_socket(client); return; }
  std::string body = raw.substr(split + 4);
  while (body.size() < content_length) {
#ifdef _WIN32
    const int n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#else
    const ssize_t n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#endif
    if (n <= 0) { close_socket(client); return; }
    body.append(buffer, static_cast<size_t>(n));
  }
  if (body.size() > content_length) body.resize(content_length);
  auto fail = [&](int status, const std::string& code) {
    respond(client, status, "application/json", "{\"error\":{\"code\":\"" + code + "\",\"request_id\":\"" + request_id + "\"}}", request_id);
  };
  const auto host = headers.find("host");
  if (protocol != "HTTP/1.1" || host == headers.end() || !valid_loopback_authority(host->second)) {
    fail(400, "invalid_request"); close_socket(client); return;
  }
  const auto origin = headers.find("origin");
  if (origin != headers.end() && !valid_loopback_origin(origin->second)) { fail(400, "invalid_request"); close_socket(client); return; }
  const bool public_health = method == "GET" && path == "/healthz";
  if (!public_health && !authorized(headers)) { fail(401, "unauthorized"); close_socket(client); return; }
  if (method == "GET" && path == "/healthz") {
    respond(client, 200, "application/json", "{\"status\":\"ok\",\"lifecycle\":\"" + std::string(lifecycle_name(engine_.state())) + "\"}", request_id);
  } else if (method == "GET" && path == "/readyz") {
    if (engine_.state() != LifecycleState::READY && engine_.state() != LifecycleState::BUSY) fail(503, "not_ready");
    else respond(client, 200, "application/json", "{\"ready\":true,\"lifecycle\":\"" + std::string(lifecycle_name(engine_.state())) + "\"}", request_id);
  } else if (method == "GET" && path == "/version") {
    respond(client, 200, "application/json", "{\"api_version\":\"" LAE_API_VERSION "\",\"engine_version\":\"" LAE_ENGINE_VERSION "\"}", request_id);
  } else if (method == "GET" && path == "/build-info") {
    respond(client, 200, "application/json", "{\"engine_version\":\"" LAE_ENGINE_VERSION "\",\"api_version\":\"" LAE_API_VERSION "\",\"backend\":\"" + engine_.backend_id() + "\",\"llama_cpp_revision\":\"" LAE_LLAMA_CPP_REVISION "\",\"model\":\"external-manifest\"}", request_id);
  } else if (method == "GET" && path == "/probe") {
    respond(client, 200, "application/json", "{\"bind\":\"127.0.0.1\",\"backend\":\"" + engine_.backend_id() + "\",\"platform\":\"" +
#ifdef _WIN32
             "windows"
#else
             "posix"
#endif
             "\"}", request_id);
  } else if (method == "GET" && path == "/metrics") {
    respond(client, 200, "application/json", engine_.metrics_json(), request_id);
  } else if (method == "POST" && path == "/v1/sessions") {
    try { const auto session = engine_.create_session(); respond(client, 201, "application/json", "{\"id\":\"" + session.id + "\",\"object\":\"session\",\"state_version\":1}", request_id); }
    catch (...) { fail(503, "not_ready"); }
  } else if (method == "DELETE" && path.rfind("/v1/sessions/", 0) == 0) {
    const std::string id = path.substr(std::string("/v1/sessions/").size());
    if (id.empty() || !engine_.delete_session(id)) fail(404, "not_found"); else respond(client, 200, "application/json", "{\"deleted\":true}", request_id);
  } else if (method == "POST" && path.rfind("/v1/cancel/", 0) == 0) {
    const bool cancelled = engine_.cancel(path.substr(std::string("/v1/cancel/").size()));
    respond(client, 200, "application/json", std::string("{\"cancelled\":") + (cancelled ? "true" : "false") + "}", request_id);
  } else if (method == "POST" && path == "/v1/chat/completions") {
    if (engine_.state() != LifecycleState::READY) { fail(engine_.state() == LifecycleState::BUSY ? 409 : 503, engine_.state() == LifecycleState::BUSY ? "busy" : "not_ready"); close_socket(client); return; }
    if (body.size() < 2 || body.front() != '{' || body.back() != '}') { fail(400, "invalid_json"); close_socket(client); return; }
    const unsigned max_tokens = json_unsigned(body, "max_tokens", 8);
    if (max_tokens < 1 || max_tokens > 64) { fail(400, "invalid_request"); close_socket(client); return; }
    const std::string session_id = json_string(body, "session_id");
    const std::string prompt = json_string(body, "content");
    if (prompt.empty()) { fail(400, "invalid_request"); close_socket(client); return; }
    const size_t stream_key = body.find("\"stream\"");
    const size_t stream_colon = stream_key == std::string::npos ? std::string::npos : body.find(':', stream_key + 8);
    size_t stream_value = stream_colon == std::string::npos ? std::string::npos : stream_colon + 1;
    while (stream_value < body.size() && std::isspace(static_cast<unsigned char>(body[stream_value]))) ++stream_value;
    const bool stream = stream_value != std::string::npos && body.compare(stream_value, 4, "true") == 0;
    const Cancellation cancellation = std::make_shared<std::atomic<bool>>(false);
    std::string combined;
    if (stream) {
      std::ostringstream head; head << "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\nX-Request-Id: " << request_id << "\r\n\r\n";
      if (!send_all(client, head.str())) { cancellation->store(true); close_socket(client); return; }
      try {
        const auto result = engine_.generate(request_id, session_id, {prompt, max_tokens}, cancellation, [&](const std::string& token) {
          combined += token;
          return send_all(client, "data: {\"id\":\"" + request_id + "\",\"choices\":[{\"delta\":{\"content\":\"" + token + "\"}}]}\n\n");
        });
        send_all(client, "data: {\"id\":\"" + request_id + "\",\"choices\":[{\"delta\":{},\"finish_reason\":\"" + result.finish_reason + "\"}]}\n\ndata: [DONE]\n\n");
      } catch (const std::invalid_argument&) { send_all(client, "data: {\"error\":{\"code\":\"not_found\"}}\n\n"); }
    } else {
      try {
        const auto result = engine_.generate(request_id, session_id, {prompt, max_tokens}, cancellation, [&](const std::string& token) { combined += token; return true; });
        respond(client, 200, "application/json", "{\"id\":\"" + request_id + "\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"" + combined + "\"},\"finish_reason\":\"" + result.finish_reason + "\"}],\"usage\":{\"completion_tokens\":" + std::to_string(result.generated_tokens) + "}}", request_id);
      } catch (const std::invalid_argument&) { fail(404, "not_found"); } catch (const std::logic_error&) { fail(409, "busy"); } catch (...) { fail(500, "internal_error"); }
    }
  } else {
    fail(404, "not_found");
  }
  close_socket(client);
}

}  // namespace lae
