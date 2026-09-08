#include "protocol_codec.hpp"

#include <bcrypt.h>

#include <algorithm>
#include <cstring>
#include <limits>
#include <map>
#include <string_view>

namespace lae::action_journal_helper {
namespace {

constexpr char kProtocol[] = "lae.action-journal.v0.1.0";
constexpr char kRequestKind[] = "request";
constexpr std::uint64_t kMaximumUnixMs = 4'102'444'800'000ULL;
constexpr std::size_t kMaximumJsonDepth = 16;
constexpr std::size_t kMaximumJsonNodes = 4'096;
constexpr std::size_t kMaximumObjectFields = 24;
constexpr std::size_t kMaximumArrayItems = 128;
constexpr std::size_t kMaximumStringBytes = 2'048;

bool identifier(std::string_view value, std::string_view prefix,
                std::size_t hex_bytes) noexcept {
  if (value.size() != prefix.size() + hex_bytes ||
      value.substr(0, prefix.size()) != prefix) return false;
  for (std::size_t index = prefix.size(); index < value.size(); ++index) {
    const char character = value[index];
    if (!((character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f'))) return false;
  }
  return true;
}

bool digest(std::string_view value) noexcept {
  return identifier(value, "", 64);
}

bool safe_string(std::string_view value, std::size_t maximum,
                 bool allow_empty = false) noexcept {
  if ((!allow_empty && value.empty()) || value.size() > maximum) return false;
  for (const unsigned char character : value)
    if (character < 0x20 || character == 0x7f) return false;
  return true;
}

bool one_of(std::string_view value,
            std::initializer_list<std::string_view> allowed) noexcept {
  return std::find(allowed.begin(), allowed.end(), value) != allowed.end();
}

bool exact_keys(const nlohmann::json& value,
                std::initializer_list<std::string_view> keys) {
  if (!value.is_object() || value.size() != keys.size()) return false;
  for (const auto key : keys)
    if (!value.contains(std::string(key))) return false;
  return true;
}

bool unsigned_value(const nlohmann::json& value, std::uint64_t minimum,
                    std::uint64_t maximum, std::uint64_t& result) noexcept {
  if (!value.is_number_unsigned()) return false;
  result = value.get<std::uint64_t>();
  return result >= minimum && result <= maximum;
}

bool nullable_digest(const nlohmann::json& value) noexcept {
  return value.is_null() || (value.is_string() && digest(value.get_ref<const std::string&>()));
}

bool bounded_tree(const nlohmann::json& value, std::size_t depth,
                  std::size_t& nodes) noexcept {
  if (depth > kMaximumJsonDepth || ++nodes > kMaximumJsonNodes) return false;
  if (value.is_null() || value.is_boolean() || value.is_number_unsigned()) {
    if (value.is_number_unsigned() &&
        value.get<std::uint64_t>() > 9'007'199'254'740'991ULL) return false;
    return true;
  }
  if (value.is_number()) return false;  // no floats, negative numbers, or -0
  if (value.is_string())
    return safe_string(value.get_ref<const std::string&>(), kMaximumStringBytes,
                       true);
  if (value.is_array()) {
    if (value.size() > kMaximumArrayItems) return false;
    for (const auto& item : value)
      if (!bounded_tree(item, depth + 1, nodes)) return false;
    return true;
  }
  if (!value.is_object() || value.size() > kMaximumObjectFields) return false;
  for (const auto& item : value.items()) {
    if (!safe_string(item.key(), 128, true) ||
        !bounded_tree(item.value(), depth + 1, nodes)) return false;
  }
  return true;
}

bool strict_parse(std::string_view input, nlohmann::json& output,
                  CodecStatus& failure) {
  if (input.size() < 2 || input.size() > kMaxPayloadBytes) {
    failure = CodecStatus::kInvalidFrame;
    return false;
  }
  if (input.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, input.data(),
                          static_cast<int>(input.size()), nullptr, 0) <= 0) {
    failure = CodecStatus::kInvalidUtf8;
    return false;
  }
  bool duplicate = false;
  bool excessive_depth = false;
  std::vector<std::set<std::string>> object_keys(kMaximumJsonDepth + 2);
  const auto callback = [&](int depth, nlohmann::json::parse_event_t event,
                            nlohmann::json& parsed) {
    if (depth < 0 || static_cast<std::size_t>(depth) > kMaximumJsonDepth) {
      excessive_depth = true;
      return false;
    }
    if (event == nlohmann::json::parse_event_t::object_start) {
      if (static_cast<std::size_t>(depth + 1) >= object_keys.size()) {
        excessive_depth = true;
        return false;
      }
      object_keys[static_cast<std::size_t>(depth + 1)].clear();
    } else if (event == nlohmann::json::parse_event_t::key) {
      auto& keys = object_keys[static_cast<std::size_t>(depth)];
      if (!keys.insert(parsed.get<std::string>()).second) {
        duplicate = true;
        return false;
      }
    }
    return true;
  };
  try {
    output = nlohmann::json::parse(input.begin(), input.end(), callback, true,
                                   false);
  } catch (...) {
    failure = duplicate ? CodecStatus::kDuplicateKey : CodecStatus::kInvalidJson;
    return false;
  }
  if (duplicate || output.is_discarded() || excessive_depth) {
    failure = duplicate ? CodecStatus::kDuplicateKey : CodecStatus::kInvalidJson;
    return false;
  }
  std::size_t nodes = 0;
  if (!bounded_tree(output, 0, nodes)) {
    failure = CodecStatus::kInvalidJson;
    return false;
  }
  std::string canonical;
  try {
    canonical = output.dump(-1, ' ', false,
                            nlohmann::json::error_handler_t::strict);
  } catch (...) {
    failure = CodecStatus::kInvalidUtf8;
    return false;
  }
  if (canonical != input) {
    failure = CodecStatus::kNoncanonicalJson;
    return false;
  }
  failure = CodecStatus::kOk;
  return true;
}

bool hex_decode(std::string_view value, std::uint8_t* output,
                std::size_t bytes) noexcept {
  if (value.size() != bytes * 2) return false;
  auto nibble = [](char character, std::uint8_t& result) {
    if (character >= '0' && character <= '9') result = character - '0';
    else if (character >= 'a' && character <= 'f') result = character - 'a' + 10;
    else return false;
    return true;
  };
  for (std::size_t index = 0; index < bytes; ++index) {
    std::uint8_t high = 0, low = 0;
    if (!nibble(value[index * 2], high) || !nibble(value[index * 2 + 1], low))
      return false;
    output[index] = static_cast<std::uint8_t>((high << 4) | low);
  }
  return true;
}

std::string hex_encode(const std::uint8_t* value, std::size_t bytes) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string result(bytes * 2, '0');
  for (std::size_t index = 0; index < bytes; ++index) {
    result[index * 2] = kHex[value[index] >> 4];
    result[index * 2 + 1] = kHex[value[index] & 0xf];
  }
  return result;
}

bool constant_time_equal(const std::uint8_t* left, const std::uint8_t* right,
                         std::size_t count) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < count; ++index)
    difference |= static_cast<std::uint8_t>(left[index] ^ right[index]);
  return difference == 0;
}

bool hmac_sha256(const std::array<std::uint8_t, 32>& key,
                 std::string_view canonical_without_mac,
                 std::array<std::uint8_t, 32>& output) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_length = 0, result_length = 0;
  std::vector<std::uint8_t> object;
  bool ok = BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
      &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, BCRYPT_ALG_HANDLE_HMAC_FLAG));
  if (ok) ok = BCRYPT_SUCCESS(BCryptGetProperty(
      algorithm, BCRYPT_OBJECT_LENGTH,
      reinterpret_cast<PUCHAR>(&object_length), sizeof(object_length),
      &result_length, 0));
  if (ok && (object_length == 0 || object_length > 65'536)) ok = false;
  if (ok) object.resize(object_length);
  if (ok) ok = BCRYPT_SUCCESS(BCryptCreateHash(
      algorithm, &hash, object.data(), object_length,
      const_cast<PUCHAR>(key.data()), static_cast<ULONG>(key.size()), 0));
  const std::string prefix = std::string(kProtocol) + '\0';
  if (ok) ok = BCRYPT_SUCCESS(BCryptHashData(
      hash, reinterpret_cast<PUCHAR>(const_cast<char*>(prefix.data())),
      static_cast<ULONG>(prefix.size()), 0));
  if (ok && !canonical_without_mac.empty()) ok = BCRYPT_SUCCESS(BCryptHashData(
      hash, reinterpret_cast<PUCHAR>(const_cast<char*>(canonical_without_mac.data())),
      static_cast<ULONG>(canonical_without_mac.size()), 0));
  if (ok) ok = BCRYPT_SUCCESS(BCryptFinishHash(
      hash, output.data(), static_cast<ULONG>(output.size()), 0));
  if (hash != nullptr) BCryptDestroyHash(hash);
  if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
  if (!object.empty()) SecureZeroMemory(object.data(), object.size());
  return ok;
}

nlohmann::json without_mac(const nlohmann::json& input) {
  nlohmann::json result = input;
  result.erase("mac");
  return result;
}

bool nullable_operation(const nlohmann::json& value) noexcept {
  return value.is_null() || (value.is_string() &&
      identifier(value.get_ref<const std::string&>(), "act_", 32));
}

bool validate_prepare_body(const nlohmann::json& body) {
  if (!exact_keys(body, {"arguments_digest", "call_ref", "operation_digest",
                         "preview_digest", "request_ref", "risk_tier",
                         "side_effect", "tool_name"})) return false;
  for (const char* key : {"arguments_digest", "call_ref", "operation_digest",
                          "preview_digest", "request_ref"})
    if (!body[key].is_string() || !digest(body[key].get_ref<const std::string&>()))
      return false;
  if (!body["tool_name"].is_string()) return false;
  const auto& tool = body["tool_name"].get_ref<const std::string&>();
  if (tool.size() < 2 || tool.size() > 96 || tool[0] < 'a' || tool[0] > 'z')
    return false;
  for (const char character : tool)
    if (!((character >= 'a' && character <= 'z') ||
          (character >= '0' && character <= '9') || character == '_' ||
          character == '.' || character == '-')) return false;
  if (!body["risk_tier"].is_string() ||
      !one_of(body["risk_tier"].get_ref<const std::string&>(),
              {"T0", "T1", "T2", "T3", "T4"})) return false;
  return body["side_effect"].is_string() &&
      one_of(body["side_effect"].get_ref<const std::string&>(),
             {"browser_activation", "browser_close", "browser_input",
              "browser_navigation", "browser_read", "cloud_inference", "create",
              "external_navigation", "launch", "none", "process_execution",
              "read_sensitive", "replace", "write_sensitive"});
}

bool validate_request_body(std::string_view method,
                           const nlohmann::json& body,
                           const nlohmann::json& operation) {
  const bool without_operation = one_of(method, {"health", "prepare", "summary"});
  if (!body.is_object() || without_operation != operation.is_null()) return false;
  if (method == "health") return exact_keys(body, {});
  if (method == "prepare") return validate_prepare_body(body);
  if (method == "authorize")
    return exact_keys(body, {"authorization_kind"}) &&
        body["authorization_kind"].is_string() &&
        one_of(body["authorization_kind"].get_ref<const std::string&>(),
               {"policy", "user_confirmation", "operator_grant"});
  if (method == "dispatch") return exact_keys(body, {});
  if (method == "acknowledge")
    return exact_keys(body, {"provider_receipt_digest"}) &&
        body["provider_receipt_digest"].is_string() &&
        digest(body["provider_receipt_digest"].get_ref<const std::string&>());
  if (method == "begin_reconciliation")
    return exact_keys(body, {"reason"}) && body["reason"].is_string() &&
        one_of(body["reason"].get_ref<const std::string&>(),
               {"lost_ack", "provider_timeout", "transport_closed",
                "startup_recovery", "postcondition_pending"});
  if (method == "complete")
    return exact_keys(body, {"receipt_digest", "resolution"}) &&
        body["receipt_digest"].is_string() &&
        digest(body["receipt_digest"].get_ref<const std::string&>()) &&
        body["resolution"].is_string() &&
        one_of(body["resolution"].get_ref<const std::string&>(),
               {"completed", "manual_completed"});
  if (method == "cancel")
    return exact_keys(body, {"resolution"}) && body["resolution"].is_string() &&
        one_of(body["resolution"].get_ref<const std::string&>(),
               {"user_denied", "request_cancelled"});
  if (method == "fail_definitive")
    return exact_keys(body, {"resolution"}) && body["resolution"].is_string() &&
        one_of(body["resolution"].get_ref<const std::string&>(),
               {"pre_dispatch_failure", "manual_failed_definitive"});
  if (method == "mark_unknown")
    return exact_keys(body, {"resolution"}) &&
        body["resolution"] == "dispatch_ambiguous";
  if (method == "summary") {
    std::uint64_t limit = 0;
    return exact_keys(body, {"cursor", "include_terminal", "limit"}) &&
        nullable_operation(body["cursor"]) && body["include_terminal"].is_boolean() &&
        unsigned_value(body["limit"], 1, 128, limit);
  }
  if (method == "detail") {
    std::uint64_t after = 0, limit = 0;
    const bool sequence_ok = body.contains("after_sequence") &&
        (body["after_sequence"].is_null() ||
         unsigned_value(body["after_sequence"], 0,
                        kMaxEventsPerOperation - 1, after));
    return exact_keys(body, {"after_event_digest", "after_sequence", "limit"}) &&
        sequence_ok && nullable_digest(body["after_event_digest"]) &&
        (body["after_sequence"].is_null() == body["after_event_digest"].is_null()) &&
        unsigned_value(body["limit"], 1, kMaxDetailEvents, limit);
  }
  return false;
}

bool validate_request(const nlohmann::json& value) {
  if (!exact_keys(value, {"body", "deadline_at_ms", "issued_at_ms", "kind",
                          "mac", "method", "nonce", "operation_id",
                          "request_id", "sequence", "version"}) ||
      value["kind"] != kRequestKind || value["version"] != kHelperAbiVersion ||
      !value["request_id"].is_string() ||
      !identifier(value["request_id"].get_ref<const std::string&>(), "req_", 32) ||
      !value["nonce"].is_string() ||
      !identifier(value["nonce"].get_ref<const std::string&>(), "", 32) ||
      !value["method"].is_string() ||
      !one_of(value["method"].get_ref<const std::string&>(),
              {"health", "prepare", "authorize", "dispatch", "acknowledge",
               "begin_reconciliation", "complete", "cancel", "fail_definitive",
               "mark_unknown", "summary", "detail"}) ||
      !nullable_operation(value["operation_id"]) || !value["mac"].is_string() ||
      !digest(value["mac"].get_ref<const std::string&>())) return false;
  std::uint64_t sequence = 0, issued = 0, deadline = 0;
  if (!unsigned_value(value["sequence"], 0, 0x7fffffff, sequence) ||
      !unsigned_value(value["issued_at_ms"], 0, kMaximumUnixMs, issued) ||
      !unsigned_value(value["deadline_at_ms"], 1, kMaximumUnixMs, deadline) ||
      deadline <= issued || deadline - issued > kMaximumDeadlineSpanMs)
    return false;
  return validate_request_body(value["method"].get_ref<const std::string&>(),
                               value["body"], value["operation_id"]);
}

bool error_retryable(std::string_view code) noexcept {
  return one_of(code, {"queue_full"});
}

bool response_error(const std::string& code, nlohmann::json& value) {
  if (code.empty()) {
    value = nullptr;
    return true;
  }
  if (!one_of(code, {"invalid_frame", "invalid_utf8", "invalid_json",
                     "duplicate_key", "noncanonical_json", "unknown_field",
                     "invalid_request", "invalid_mac", "replay",
                     "sequence_out_of_order", "nonce_mismatch",
                     "deadline_expired", "queue_full", "not_found",
                     "invalid_transition", "commit_non_cancellable",
                     "platform_unavailable", "record_limit_exceeded", "internal"}))
    return false;
  value = {{"code", code}, {"retryable", error_retryable(code)}};
  return true;
}

bool nullable_enum(const nlohmann::json& value,
                   std::initializer_list<std::string_view> allowed) noexcept {
  return value.is_null() || (value.is_string() &&
      one_of(value.get_ref<const std::string&>(), allowed));
}

bool safe_receipt(const nlohmann::json& value) {
  if (!exact_keys(value, {"authorization_kind", "operation_id", "receipt_digest",
                          "recovery_required", "redacted", "resolution",
                          "sequence", "state"}) || value["redacted"] != true ||
      !value["recovery_required"].is_boolean() ||
      !value["operation_id"].is_string() ||
      !identifier(value["operation_id"].get_ref<const std::string&>(), "act_", 32) ||
      !value["receipt_digest"].is_string() ||
      !digest(value["receipt_digest"].get_ref<const std::string&>()) ||
      !value["sequence"].is_number_unsigned() ||
      value["sequence"].get<std::uint64_t>() >= kMaxEventsPerOperation ||
      !value["state"].is_string() ||
      !one_of(value["state"].get_ref<const std::string&>(),
              {"prepared", "authorized", "dispatching", "acknowledged",
               "reconciling", "completed", "cancelled", "failed_definitive",
               "unknown_manual"}) ||
      !nullable_enum(value["authorization_kind"],
                     {"policy", "user_confirmation", "operator_grant"}) ||
      !nullable_enum(value["resolution"],
                     {"user_denied", "request_cancelled", "pre_dispatch_failure",
                      "dispatch_ambiguous", "provider_acknowledged", "completed",
                      "startup_recovery", "manual_completed",
                      "manual_failed_definitive"})) return false;
  const auto& state = value["state"].get_ref<const std::string&>();
  const bool recovery = state == "reconciling" || state == "unknown_manual";
  return value["recovery_required"].get<bool>() == recovery;
}

bool safe_event(const nlohmann::json& value) {
  if (!exact_keys(value, {"action", "authorization_kind", "receipt_digest",
                          "resolution", "sequence", "state"}) ||
      !value["action"].is_string() ||
      !one_of(value["action"].get_ref<const std::string&>(),
              {"prepare", "authorize", "dispatch", "acknowledge",
               "begin_reconciliation", "complete", "cancel",
               "fail_definitive", "mark_unknown", "startup_recovery"}))
    return false;
  nlohmann::json as_receipt = {
      {"authorization_kind", value["authorization_kind"]},
      {"operation_id", "act_00000000000000000000000000000000"},
      {"receipt_digest", value["receipt_digest"]},
      {"recovery_required", value["state"] == "reconciling" ||
                                 value["state"] == "unknown_manual"},
      {"redacted", true}, {"resolution", value["resolution"]},
      {"sequence", value["sequence"]}, {"state", value["state"]},
  };
  return safe_receipt(as_receipt);
}

bool safe_success(const DecodedRequest& request,
                  const EncodedResult& result) {
  if (!result.body.is_object()) return false;
  if (!result.error_code.empty())
    return result.body.empty() && result.state.empty() &&
        ((request.method == "prepare" && result.operation_id.empty()) ||
         (request.method != "prepare" &&
          result.operation_id == request.operation_id));
  if (request.method == "health") {
    std::uint64_t recovery = 0;
    return result.operation_id.empty() && result.state.empty() &&
        exact_keys(result.body, {"platform_available", "production_enabled",
                                 "recovery_count", "status"}) &&
        result.body["platform_available"] == false &&
        result.body["production_enabled"] == false &&
        unsigned_value(result.body["recovery_count"], 0, 1024, recovery) &&
        result.body["status"] == "unavailable";
  }
  if (request.method == "summary") {
    if (!result.operation_id.empty() || !result.state.empty() ||
        !exact_keys(result.body, {"next_cursor", "records", "truncated"}) ||
        !nullable_operation(result.body["next_cursor"]) ||
        !result.body["records"].is_array() || result.body["records"].size() > 128 ||
        !result.body["truncated"].is_boolean()) return false;
    for (const auto& item : result.body["records"])
      if (!safe_receipt(item)) return false;
    return true;
  }
  if (request.method == "detail") {
    std::uint64_t next_sequence = 0;
    if (result.operation_id != request.operation_id ||
        !exact_keys(result.body, {"events", "next_sequence", "predecessor",
                                 "receipt", "truncated"}) ||
        !result.body["events"].is_array() ||
        result.body["events"].size() > kMaxDetailEvents ||
        !result.body["truncated"].is_boolean() ||
        !(result.body["next_sequence"].is_null() ||
          unsigned_value(result.body["next_sequence"], 0,
                         kMaxEventsPerOperation - 1, next_sequence)) ||
        !(result.body["predecessor"].is_null() ||
          safe_event(result.body["predecessor"])) ||
        !safe_receipt(result.body["receipt"]) ||
        result.body["receipt"]["operation_id"] != result.operation_id ||
        result.body["receipt"]["state"] != result.state) return false;
    for (const auto& event : result.body["events"])
      if (!safe_event(event)) return false;
    return true;
  }
  return identifier(result.operation_id, "act_", 32) &&
      !result.state.empty() && exact_keys(result.body, {"receipt"}) &&
      safe_receipt(result.body["receipt"]) &&
      result.body["receipt"]["operation_id"] == result.operation_id &&
      result.body["receipt"]["state"] == result.state;
}

}  // namespace

ProtocolSession::ProtocolSession(
    const std::array<std::uint8_t, 32>& key,
    const std::array<std::uint8_t, 16>& nonce) noexcept
    : key_(key), nonce_(nonce) {}

ProtocolSession::~ProtocolSession() { wipe(); }

CodecStatus ProtocolSession::decode_request(
    const std::vector<std::uint8_t>& frame, std::uint64_t now_ms,
    DecodedRequest& request) noexcept {
  try {
    if (frame.size() < 6 || frame.size() > kMaxFrameBytes)
      return CodecStatus::kInvalidFrame;
    const std::uint32_t length =
        (static_cast<std::uint32_t>(frame[0]) << 24) |
        (static_cast<std::uint32_t>(frame[1]) << 16) |
        (static_cast<std::uint32_t>(frame[2]) << 8) |
        static_cast<std::uint32_t>(frame[3]);
    if (length < 2 || length > kMaxPayloadBytes || length + 4 != frame.size())
      return CodecStatus::kInvalidFrame;
    nlohmann::json value;
    CodecStatus parse_status = CodecStatus::kOk;
    const std::string_view input(
        reinterpret_cast<const char*>(frame.data() + 4), length);
    if (!strict_parse(input, value, parse_status)) return parse_status;
    if (!validate_request(value)) return CodecStatus::kInvalidRequest;
    const std::string unsigned_canonical = without_mac(value).dump();
    std::array<std::uint8_t, 32> expected{}, supplied{};
    if (!hmac_sha256(key_, unsigned_canonical, expected))
      return CodecStatus::kCryptoFailed;
    if (!hex_decode(value["mac"].get_ref<const std::string&>(), supplied.data(),
                    supplied.size()) ||
        !constant_time_equal(expected.data(), supplied.data(), expected.size()))
      return CodecStatus::kInvalidMac;
    std::array<std::uint8_t, 16> supplied_nonce{};
    if (!hex_decode(value["nonce"].get_ref<const std::string&>(),
                    supplied_nonce.data(), supplied_nonce.size()) ||
        !constant_time_equal(nonce_.data(), supplied_nonce.data(), nonce_.size()))
      return CodecStatus::kNonceMismatch;
    const auto sequence = value["sequence"].get<std::uint32_t>();
    if (sequence < inbound_sequence_) return CodecStatus::kReplay;
    if (sequence > inbound_sequence_) return CodecStatus::kSequenceOutOfOrder;
    const auto& request_id = value["request_id"].get_ref<const std::string&>();
    if (request_ids_.count(request_id) != 0) return CodecStatus::kReplay;
    if (request_ids_.size() >= kMaxSessionFrames)
      return CodecStatus::kRecordLimitExceeded;
    const auto issued = value["issued_at_ms"].get<std::uint64_t>();
    const auto deadline = value["deadline_at_ms"].get<std::uint64_t>();
    if (issued > now_ms + kMaximumFutureSkewMs) return CodecStatus::kInvalidRequest;
    if (deadline <= now_ms) return CodecStatus::kDeadlineExpired;
    request_ids_.insert(request_id);
    ++inbound_sequence_;
    request = {request_id,
               value["method"].get<std::string>(),
               value["operation_id"].is_null()
                   ? std::string()
                   : value["operation_id"].get<std::string>(),
               value["body"], issued, deadline};
    SecureZeroMemory(expected.data(), expected.size());
    SecureZeroMemory(supplied.data(), supplied.size());
    return CodecStatus::kOk;
  } catch (...) {
    return CodecStatus::kInternal;
  }
}

CodecStatus ProtocolSession::encode_response(
    const DecodedRequest& request, const EncodedResult& result,
    std::vector<std::uint8_t>& frame) noexcept {
  try {
    if (outbound_sequence_ >= kMaxSessionFrames)
      return CodecStatus::kRecordLimitExceeded;
    if (!identifier(request.request_id, "req_", 32) ||
        !safe_success(request, result)) return CodecStatus::kInternal;
    nlohmann::json error;
    if (!response_error(result.error_code, error)) return CodecStatus::kInternal;
    nlohmann::json value = {
        {"body", result.error_code.empty() ? result.body : nlohmann::json::object()},
        {"error", error},
        {"kind", "response"},
        {"mac", ""},
        {"method", request.method},
        {"nonce", hex_encode(nonce_.data(), nonce_.size())},
        {"operation_id", result.operation_id.empty()
             ? nlohmann::json(nullptr) : nlohmann::json(result.operation_id)},
        {"request_id", request.request_id},
        {"sequence", outbound_sequence_},
        {"state", result.state.empty()
             ? nlohmann::json(nullptr) : nlohmann::json(result.state)},
        {"version", kHelperAbiVersion},
    };
    const std::string unsigned_canonical = without_mac(value).dump();
    std::array<std::uint8_t, 32> mac{};
    if (!hmac_sha256(key_, unsigned_canonical, mac))
      return CodecStatus::kCryptoFailed;
    value["mac"] = hex_encode(mac.data(), mac.size());
    const std::string payload = value.dump();
    SecureZeroMemory(mac.data(), mac.size());
    if (payload.size() < 2 || payload.size() > kMaxPayloadBytes)
      return CodecStatus::kInvalidFrame;
    frame.resize(payload.size() + 4);
    const auto size = static_cast<std::uint32_t>(payload.size());
    frame[0] = static_cast<std::uint8_t>(size >> 24);
    frame[1] = static_cast<std::uint8_t>(size >> 16);
    frame[2] = static_cast<std::uint8_t>(size >> 8);
    frame[3] = static_cast<std::uint8_t>(size);
    std::memcpy(frame.data() + 4, payload.data(), payload.size());
    ++outbound_sequence_;
    return CodecStatus::kOk;
  } catch (...) {
    frame.clear();
    return CodecStatus::kInternal;
  }
}

void ProtocolSession::wipe() noexcept {
  SecureZeroMemory(key_.data(), key_.size());
  SecureZeroMemory(nonce_.data(), nonce_.size());
  inbound_sequence_ = outbound_sequence_ = 0;
  request_ids_.clear();
}

const char* codec_status_name(CodecStatus status) noexcept {
  switch (status) {
    case CodecStatus::kOk: return "ok";
    case CodecStatus::kInvalidFrame: return "invalid_frame";
    case CodecStatus::kInvalidUtf8: return "invalid_utf8";
    case CodecStatus::kInvalidJson: return "invalid_json";
    case CodecStatus::kDuplicateKey: return "duplicate_key";
    case CodecStatus::kNoncanonicalJson: return "noncanonical_json";
    case CodecStatus::kUnknownField: return "unknown_field";
    case CodecStatus::kInvalidRequest: return "invalid_request";
    case CodecStatus::kInvalidMac: return "invalid_mac";
    case CodecStatus::kReplay: return "replay";
    case CodecStatus::kSequenceOutOfOrder: return "sequence_out_of_order";
    case CodecStatus::kNonceMismatch: return "nonce_mismatch";
    case CodecStatus::kDeadlineExpired: return "deadline_expired";
    case CodecStatus::kQueueFull: return "queue_full";
    case CodecStatus::kRecordLimitExceeded: return "record_limit_exceeded";
    case CodecStatus::kCryptoFailed: return "internal";
    case CodecStatus::kInternal: return "internal";
  }
  return "internal";
}

}  // namespace lae::action_journal_helper
