#pragma once

// Source-only protocol codec. It is deliberately absent from CMake and every
// production import/package path.
#ifndef _WIN32
#error "The ActionJournal helper source is Windows-only"
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <array>
#include <cstdint>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace lae::action_journal_helper {

inline constexpr std::uint32_t kHelperAbiVersion = 1;
inline constexpr std::size_t kMaxFrameBytes = 65'536;
inline constexpr std::size_t kMaxPayloadBytes = 65'532;
inline constexpr std::size_t kMaxBufferedFrames = 8;
inline constexpr std::size_t kMaxSessionFrames = 1'024;
inline constexpr std::size_t kMaxPendingRequests = 8;
inline constexpr std::uint64_t kMaximumDeadlineSpanMs = 600'000;
inline constexpr std::uint64_t kMaximumFutureSkewMs = 30'000;

enum class CodecStatus : std::uint8_t {
  kOk,
  kInvalidFrame,
  kInvalidUtf8,
  kInvalidJson,
  kDuplicateKey,
  kNoncanonicalJson,
  kUnknownField,
  kInvalidRequest,
  kInvalidMac,
  kReplay,
  kSequenceOutOfOrder,
  kNonceMismatch,
  kDeadlineExpired,
  kQueueFull,
  kRecordLimitExceeded,
  kCryptoFailed,
  kInternal,
};

struct DecodedRequest {
  std::string request_id;
  std::string method;
  std::string operation_id;
  nlohmann::json body;
  std::uint64_t issued_at_ms = 0;
  std::uint64_t deadline_at_ms = 0;
};

struct EncodedResult {
  std::string operation_id;
  std::string state;
  nlohmann::json body = nlohmann::json::object();
  std::string error_code;
};

class ProtocolSession final {
 public:
  ProtocolSession(const std::array<std::uint8_t, 32>& key,
                  const std::array<std::uint8_t, 16>& nonce) noexcept;
  ~ProtocolSession();
  ProtocolSession(const ProtocolSession&) = delete;
  ProtocolSession& operator=(const ProtocolSession&) = delete;

  CodecStatus decode_request(const std::vector<std::uint8_t>& frame,
                             std::uint64_t now_ms,
                             DecodedRequest& request) noexcept;
  CodecStatus encode_response(const DecodedRequest& request,
                              const EncodedResult& result,
                              std::vector<std::uint8_t>& frame) noexcept;
  void wipe() noexcept;

 private:
  std::array<std::uint8_t, 32> key_{};
  std::array<std::uint8_t, 16> nonce_{};
  std::uint32_t inbound_sequence_ = 0;
  std::uint32_t outbound_sequence_ = 0;
  std::set<std::string> request_ids_;
};

const char* codec_status_name(CodecStatus status) noexcept;

}  // namespace lae::action_journal_helper
