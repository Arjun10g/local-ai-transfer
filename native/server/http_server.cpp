#include "http_server.hpp"
#include "chat_request.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstring>
#include <iostream>
#include <limits>
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
constexpr size_t kMaxHeaderBytes = 16 * 1024;
constexpr size_t kMaxHeaderCount = 64;
constexpr size_t kMaxHeaderLine = 8 * 1024;
constexpr size_t kMaxSseLine = 256 * 1024;
constexpr size_t kMaxSseBytes = 4 * 1024 * 1024;
constexpr size_t kMaxSseEvents = 4096;
constexpr unsigned kSocketTimeoutMs = 5000;
constexpr size_t kMaxResponseBytes = 4 * 1024 * 1024;
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

void set_socket_timeouts(HttpServer::Socket socket) {
#ifdef _WIN32
  const DWORD timeout = kSocketTimeoutMs;
  setsockopt(native_socket(socket), SOL_SOCKET, SO_RCVTIMEO,
             reinterpret_cast<const char*>(&timeout), sizeof(timeout));
  setsockopt(native_socket(socket), SOL_SOCKET, SO_SNDTIMEO,
             reinterpret_cast<const char*>(&timeout), sizeof(timeout));
#else
  timeval timeout{};
  timeout.tv_sec = static_cast<long>(kSocketTimeoutMs / 1000);
  timeout.tv_usec = static_cast<long>((kSocketTimeoutMs % 1000) * 1000);
  setsockopt(native_socket(socket), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(native_socket(socket), SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
#endif
}

bool socket_timed_out() {
#ifdef _WIN32
  const int error = WSAGetLastError();
  return error == WSAETIMEDOUT || error == WSAEWOULDBLOCK;
#else
  return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
#endif
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

bool header_token(const std::string& value) {
  if (value.empty()) return false;
  for (const unsigned char c : value) {
    if (!(std::isalnum(c) || c == '!' || c == '#' || c == '$' || c == '%' || c == '&' ||
          c == '\'' || c == '*' || c == '+' || c == '-' || c == '.' || c == '^' || c == '_' ||
          c == '`' || c == '|' || c == '~')) return false;
  }
  return true;
}

bool valid_path_id(const std::string& id) {
  if (id.size() < 1 || id.size() > 96) return false;
  return std::all_of(id.begin(), id.end(), [](unsigned char c) {
    return std::isalnum(c) || c == '-' || c == '_';
  });
}

bool constant_time_equal(const std::string& actual, const std::string& expected) {
  const size_t length = std::max(actual.size(), expected.size());
  unsigned int difference = static_cast<unsigned int>(actual.size() ^ expected.size());
  for (size_t i = 0; i < length; ++i) {
    const unsigned char left = i < actual.size() ? static_cast<unsigned char>(actual[i]) : 0;
    const unsigned char right = i < expected.size() ? static_cast<unsigned char>(expected[i]) : 0;
    difference |= static_cast<unsigned int>(left ^ right);
  }
  return difference == 0;
}

bool valid_content_type(const std::string& value) {
  const std::string normalized = lower(trim(value));
  return normalized == "application/json" || normalized == "application/json; charset=utf-8";
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
    case 408: return "Request Timeout"; case 409: return "Conflict"; case 413: return "Payload Too Large";
    case 415: return "Unsupported Media Type"; case 431: return "Request Header Fields Too Large";
    case 499: return "Client Closed Request";
    case 503: return "Service Unavailable"; default: return "Internal Server Error";
  }
}

}  // namespace

std::string json_escape(const std::string& value) {
  std::ostringstream out;
  for (const unsigned char c : value) {
    switch (c) {
      case '"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\b': out << "\\b"; break;
      case '\f': out << "\\f"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default:
        if (c < 0x20) { static constexpr char hex[] = "0123456789abcdef"; out << "\\u00" << hex[c >> 4] << hex[c & 0x0f]; }
        else out << static_cast<char>(c);
    }
  }
  return out.str();
}

HttpServer::HttpServer(Engine& engine, std::string bearer_token)
    : engine_(engine), bearer_token_(std::move(bearer_token)) {}
HttpServer::~HttpServer() { stop(); }

unsigned HttpServer::start(unsigned port) {
  if (listen_socket_ != -1) throw std::logic_error("server already started");
#ifdef _WIN32
  WSADATA data{};
  if (WSAStartup(MAKEWORD(2, 2), &data) != 0) throw std::runtime_error("winsock initialization failed");
  network_initialized_ = true;
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
  if (listen_socket_ != -1) {
    stopping_ = true;
    const Socket socket = listen_socket_;
    close_socket(socket);
  }
  if (accept_thread_.joinable()) accept_thread_.join();
  listen_socket_ = -1;
  std::unique_lock<std::mutex> lock(workers_wait_mutex_);
  workers_wait_cv_.wait(lock, [this] { return active_workers_.load() == 0; });
#ifdef _WIN32
  if (network_initialized_) { WSACleanup(); network_initialized_ = false; }
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
    set_socket_timeouts(client);
    active_workers_.fetch_add(1);
    try {
      std::thread([this, client] {
        try { handle(client); } catch (...) { close_socket(client); }
        active_workers_.fetch_sub(1);
        workers_wait_cv_.notify_all();
      }).detach();
    } catch (...) {
      active_workers_.fetch_sub(1);
      active_connections_.fetch_sub(1);
      close_socket(client);
    }
  }
}

bool HttpServer::authorized(const std::map<std::string, std::string>& headers) const {
  auto it = headers.find("authorization");
  return it != headers.end() && constant_time_equal(it->second, "Bearer " + bearer_token_);
}

void HttpServer::respond(Socket client, int status, const std::string& type, const std::string& body,
                         const std::string& request_id) {
  std::ostringstream out;
  out << "HTTP/1.1 " << status << " " << reason(status) << "\r\nContent-Type: " << type
      << "\r\nContent-Length: " << body.size() << "\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Request-Id: "
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
  while (raw.find("\r\n\r\n") == std::string::npos && raw.size() <= kMaxHeaderBytes) {
#ifdef _WIN32
    const int n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#else
    const ssize_t n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#endif
    if (n == 0) { close_socket(client); return; }
    if (n < 0) { if (socket_timed_out()) respond(client, 408, "application/json", "{\"error\":{\"code\":\"request_timeout\"}}", request_id); close_socket(client); return; }
    raw.append(buffer, static_cast<size_t>(n));
  }
  const size_t split = raw.find("\r\n\r\n");
  if (split == std::string::npos || split > kMaxHeaderBytes) { respond(client, 431, "application/json", "{\"error\":{\"code\":\"headers_too_large\"}}", request_id); close_socket(client); return; }
  std::istringstream headers_stream(raw.substr(0, split));
  std::string request_line; std::getline(headers_stream, request_line);
  if (!request_line.empty() && request_line.back() == '\r') request_line.pop_back();
  std::istringstream request_parts(request_line); std::string method, path, protocol, extra;
  request_parts >> method >> path >> protocol;
  if (request_parts >> extra || method.empty() || path.empty() || protocol.empty() ||
      method.size() > 16 || path.size() > 2048 || protocol != "HTTP/1.1" ||
      !header_token(method) || path.front() != '/' || path.find_first_of("\r\n") != std::string::npos) {
    respond(client, 400, "application/json", "{\"error\":{\"code\":\"invalid_request_line\"}}", request_id);
    close_socket(client); return;
  }
  std::map<std::string, std::string> headers; std::string line; size_t header_count = 0;
  bool malformed_headers = false;
  while (std::getline(headers_stream, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty() || line.size() > kMaxHeaderLine || (!line.empty() &&
        (line.front() == ' ' || line.front() == '\t'))) { malformed_headers = true; break; }
    const size_t colon = line.find(':');
    if (colon == std::string::npos || colon == 0 || !header_token(line.substr(0, colon))) { malformed_headers = true; break; }
    const std::string name = lower(line.substr(0, colon));
    std::string value = line.substr(colon + 1);
    if (value.find_first_of("\r\n") != std::string::npos ||
        std::any_of(value.begin(), value.end(), [](unsigned char c) { return c < 0x20 && c != '\t'; })) { malformed_headers = true; break; }
    value = trim(value);
    if (++header_count > kMaxHeaderCount || headers.find(name) != headers.end()) { malformed_headers = true; break; }
    headers.emplace(name, std::move(value));
  }
  if (malformed_headers) { respond(client, 400, "application/json", "{\"error\":{\"code\":\"invalid_headers\"}}", request_id); close_socket(client); return; }
  size_t content_length = 0;
  if (headers.count("content-length")) {
    const std::string& length_text = headers["content-length"];
    if (length_text.empty() || length_text.size() > 10 ||
        !std::all_of(length_text.begin(), length_text.end(), [](unsigned char c) { return std::isdigit(c); })) {
      respond(client, 400, "application/json", "{\"error\":{\"code\":\"invalid_content_length\"}}", request_id); close_socket(client); return;
    }
    try { content_length = std::stoull(length_text); } catch (...) { content_length = kMaxBody + 1; }
  }
  if (content_length > kMaxBody) { respond(client, 413, "application/json", "{\"error\":{\"code\":\"request_too_large\"}}", request_id); close_socket(client); return; }
  if (headers.count("transfer-encoding")) {
    respond(client, 400, "application/json", "{\"error\":{\"code\":\"unsupported_transfer_encoding\"}}", request_id); close_socket(client); return;
  }
  if (method == "POST" && !headers.count("content-length")) {
    respond(client, 400, "application/json", "{\"error\":{\"code\":\"missing_content_length\"}}", request_id); close_socket(client); return;
  }
  if (method == "POST" && (!headers.count("content-type") || !valid_content_type(headers["content-type"]))) {
    respond(client, 415, "application/json", "{\"error\":{\"code\":\"invalid_content_type\"}}", request_id); close_socket(client); return;
  }
  if (method != "POST" && content_length != 0) {
    respond(client, 400, "application/json", "{\"error\":{\"code\":\"unexpected_body\"}}", request_id); close_socket(client); return;
  }
  std::string body = raw.substr(split + 4);
  while (body.size() < content_length) {
#ifdef _WIN32
    const int n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#else
    const ssize_t n = recv(native_socket(client), buffer, sizeof(buffer), 0);
#endif
    if (n == 0) { close_socket(client); return; }
    if (n < 0) { if (socket_timed_out()) respond(client, 408, "application/json", "{\"error\":{\"code\":\"request_timeout\"}}", request_id); close_socket(client); return; }
    body.append(buffer, static_cast<size_t>(n));
  }
  if (body.size() > content_length) {
    respond(client, 400, "application/json", "{\"error\":{\"code\":\"surplus_body\"}}", request_id); close_socket(client); return;
  }
  auto fail = [&](int status, const std::string& code) {
    respond(client, status, "application/json", "{\"error\":{\"code\":\"" + code + "\",\"request_id\":\"" + request_id + "\"}}", request_id);
  };
  const auto host = headers.find("host");
  if (host == headers.end() || !valid_loopback_authority(host->second)) {
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
    respond(client, 200, "application/json", "{\"engine_version\":\"" LAE_ENGINE_VERSION "\",\"api_version\":\"" LAE_API_VERSION "\",\"backend\":\"" + json_escape(engine_.backend_id()) + "\",\"llama_cpp_revision\":\"" LAE_LLAMA_CPP_REVISION "\",\"model\":\"" + json_escape(engine_.model_id()) + "\"}", request_id);
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
    if (!valid_path_id(id)) fail(400, "invalid_session_id");
    else if (!engine_.delete_session(id)) fail(404, "not_found");
    else respond(client, 200, "application/json", "{\"deleted\":true}", request_id);
  } else if (method == "POST" && path.rfind("/v1/cancel/", 0) == 0) {
    const std::string id = path.substr(std::string("/v1/cancel/").size());
    if (!valid_path_id(id)) fail(400, "invalid_request_id");
    else {
      const bool cancelled = engine_.cancel(id);
      respond(client, 200, "application/json", std::string("{\"cancelled\":") + (cancelled ? "true" : "false") + "}", request_id);
    }
  } else if (method == "POST" && path == "/v1/chat/completions") {
    if (engine_.state() != LifecycleState::READY) { fail(engine_.state() == LifecycleState::BUSY ? 409 : 503, engine_.state() == LifecycleState::BUSY ? "busy" : "not_ready"); close_socket(client); return; }
    ChatRequest chat_request;
    std::string parse_error;
    if (!parse_chat_request(body, chat_request, parse_error)) { fail(400, parse_error); close_socket(client); return; }
    const std::string& session_id = chat_request.session_id;
    const bool stream = chat_request.stream;
    const Cancellation cancellation = std::make_shared<std::atomic<bool>>(false);
    std::string combined;
    if (stream) {
      std::ostringstream head; head << "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache, no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\nX-Request-Id: " << request_id << "\r\n\r\n";
      if (!send_all(client, head.str())) { cancellation->store(true); close_socket(client); return; }
      size_t sse_bytes = head.str().size(); size_t sse_events = 0;
      bool stream_bound_exceeded = false;
      try {
        const auto result = engine_.generate(request_id, session_id, chat_request.generation, cancellation, [&](const std::string& token) {
          const std::string frame = "data: {\"id\":\"" + json_escape(request_id) + "\",\"choices\":[{\"delta\":{\"content\":\"" + json_escape(token) + "\"}}]}\n\n";
          if (frame.size() > kMaxSseLine || sse_events >= kMaxSseEvents || sse_bytes > kMaxSseBytes - frame.size()) {
            stream_bound_exceeded = true; cancellation->store(true); return false;
          }
          if (combined.size() > kMaxResponseBytes - token.size()) {
            stream_bound_exceeded = true; cancellation->store(true); return false;
          }
          combined += token;
          sse_bytes += frame.size(); ++sse_events;
          return send_all(client, frame);
        });
        if (stream_bound_exceeded) {
          send_all(client, "data: {\"error\":{\"code\":\"response_too_large\"}}\n\ndata: [DONE]\n\n");
        } else {
          const std::string tail = "data: {\"id\":\"" + json_escape(request_id) + "\",\"choices\":[{\"delta\":{},\"finish_reason\":\"" + json_escape(result.finish_reason) + "\"}]}\n\ndata: [DONE]\n\n";
          if (tail.size() > kMaxSseLine || sse_events + 2 > kMaxSseEvents || sse_bytes > kMaxSseBytes - tail.size())
            send_all(client, "data: {\"error\":{\"code\":\"response_too_large\"}}\n\ndata: [DONE]\n\n");
          else send_all(client, tail);
        }
      } catch (const std::invalid_argument& error) { const std::string code = std::string(error.what()) == "context limit exceeded" ? "invalid_request" : "not_found"; send_all(client, "data: {\"error\":{\"code\":\"" + code + "\"}}\n\ndata: [DONE]\n\n"); }
      catch (const std::logic_error&) { send_all(client, "data: {\"error\":{\"code\":\"busy\"}}\n\ndata: [DONE]\n\n"); }
      catch (const std::exception& error) { std::cerr << "generation " << request_id << " failed: " << error.what() << "\n"; send_all(client, "data: {\"error\":{\"code\":\"internal_error\"}}\n\ndata: [DONE]\n\n"); }
      catch (...) { std::cerr << "generation " << request_id << " failed: unknown exception\n"; send_all(client, "data: {\"error\":{\"code\":\"internal_error\"}}\n\ndata: [DONE]\n\n"); }
    } else {
      bool response_bound_exceeded = false;
      try {
        const auto result = engine_.generate(request_id, session_id, chat_request.generation, cancellation, [&](const std::string& token) {
          if (token.size() > kMaxResponseBytes || combined.size() > kMaxResponseBytes - token.size()) {
            response_bound_exceeded = true; cancellation->store(true); return false;
          }
          combined += token; return true;
        });
        if (response_bound_exceeded) fail(413, "response_too_large");
        else respond(client, 200, "application/json", "{\"id\":\"" + json_escape(request_id) + "\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"" + json_escape(combined) + "\"},\"finish_reason\":\"" + json_escape(result.finish_reason) + "\"}],\"usage\":{\"completion_tokens\":" + std::to_string(result.generated_tokens) + "}}", request_id);
      } catch (const std::invalid_argument& error) { if (std::string(error.what()) == "context limit exceeded") fail(400, "invalid_request"); else fail(404, "not_found"); } catch (const std::logic_error&) { fail(409, "busy"); } catch (const std::exception& error) { std::cerr << "generation " << request_id << " failed: " << error.what() << "\n"; fail(500, "internal_error"); } catch (...) { std::cerr << "generation " << request_id << " failed: unknown exception\n"; fail(500, "internal_error"); }
    }
  } else {
    fail(404, "not_found");
  }
  close_socket(client);
}

}  // namespace lae
