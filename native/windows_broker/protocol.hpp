#pragma once

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#error "The Windows process broker is Windows-only and is not a portable fallback"
#endif

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

namespace lae::windows_broker {

inline constexpr std::uint32_t kMaxRequestFrameBytes = 64 * 1024;
inline constexpr std::uint32_t kMaxResponseFrameBytes = 1024 * 1024;
inline constexpr std::size_t kMaxJsonDepth = 12;
inline constexpr std::size_t kMaxArguments = 16;
inline constexpr std::size_t kMaxArgumentStringBytes = 64 * 1024;
inline constexpr std::uint32_t kMinDeadlineMs = 100;
inline constexpr std::uint32_t kMaxDeadlineMs = 120 * 1000;
inline constexpr std::uint32_t kFrameAssemblyDeadlineMs = 5000;
inline constexpr std::uint32_t kResponseWriteDeadlineMs = 5000;
inline constexpr std::uint32_t kIoCancellationGraceMs = 1000;

enum class RequestKind { kHello, kInvoke, kCancel };
enum class FrameStatus { kOk, kEndOfStream, kRejected, kIoFailure };

struct Request {
  RequestKind kind = RequestKind::kHello;
  std::string request_id;
  std::string action_id;
  std::string target_request_id;
  std::uint32_t deadline_ms = 0;
  nlohmann::json arguments = nlohmann::json::object();
};

struct Receipt {
  std::string action_id;
  std::string action_kind;
  std::string manifest_sha256;
  std::string executable_sha256;
  std::string argv_template_id;
  std::string cwd_id;
  std::string status;
  std::string error_code;
  std::uint64_t duration_ms = 0;
  std::uint64_t stdout_bytes = 0;
  std::uint64_t stderr_bytes = 0;
  std::uint64_t stdin_bytes = 0;
  bool output_truncated = false;
  bool process_created = false;
};

struct Response {
  std::string request_id;
  std::string status;
  std::string error_code;
  nlohmann::json result = nlohmann::json::object();
  std::optional<Receipt> receipt;
};

// Parses one already-bounded UTF-8 JSON frame. Duplicate and unknown keys are
// rejected. error_code is finite and contains no input data.
bool parse_strict_document(std::string_view input, std::size_t max_bytes,
                           nlohmann::json& output) noexcept;
bool parse_request(std::string_view input, Request& request,
                   std::string& error_code) noexcept;

nlohmann::json response_json(const Response& response);

// The stream framing layer never allocates from an untrusted length. Every
// frame, including its first byte, has one assembly deadline; a clean EOF before
// any prefix byte is distinct from a partial prefix/body.
FrameStatus read_request_frame(HANDLE input, std::string& json,
                               std::string& error_code) noexcept;
bool write_response_frame(HANDLE output, const Response& response) noexcept;

}  // namespace lae::windows_broker
