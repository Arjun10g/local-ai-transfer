#include "protocol.hpp"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <initializer_list>
#include <limits>
#include <set>
#include <utility>
#include <vector>

namespace lae::windows_broker {
namespace {

enum class ExactReadStatus { kOk, kCleanEof, kPartialEof, kTimeout, kIoFailure };

struct ReadContext {
  HANDLE input = INVALID_HANDLE_VALUE;
  unsigned char* destination = nullptr;
  DWORD bytes = 0;
  DWORD count = 0;
  DWORD error = ERROR_SUCCESS;
  bool success = false;
};

DWORD WINAPI read_worker(void* opaque) {
  auto& context = *static_cast<ReadContext*>(opaque);
  if (!ReadFile(context.input, context.destination, context.bytes,
                &context.count, nullptr)) {
    context.error = GetLastError();
    return 1;
  }
  context.success = true;
  return 0;
}

[[noreturn]] void fail_stop_on_stuck_io() {
  // Continuing would either strand a thread that references caller-owned
  // memory or leave the authenticated parent unable to observe terminal state.
  TerminateProcess(GetCurrentProcess(), 72);
  std::abort();
}

ExactReadStatus read_some_bounded(HANDLE input, unsigned char* destination,
                                  DWORD bytes, DWORD timeout_ms,
                                  DWORD& count) {
  ReadContext context{input, destination, bytes, 0, ERROR_SUCCESS, false};
  HANDLE raw_thread = CreateThread(nullptr, 0, read_worker, &context, 0, nullptr);
  if (!raw_thread) return ExactReadStatus::kIoFailure;
  const DWORD first_wait = WaitForSingleObject(raw_thread, timeout_ms);
  if (first_wait != WAIT_OBJECT_0) {
    SetLastError(ERROR_SUCCESS);
    const BOOL cancelled = CancelSynchronousIo(raw_thread);
    const DWORD cancellation_error = cancelled ? ERROR_SUCCESS : GetLastError();
    const DWORD cancellation_wait =
        WaitForSingleObject(raw_thread, kIoCancellationGraceMs);
    if (cancellation_wait != WAIT_OBJECT_0) {
      CloseHandle(raw_thread);
      fail_stop_on_stuck_io();
    }
    CloseHandle(raw_thread);
    if (!cancelled && cancellation_error != ERROR_NOT_FOUND)
      return ExactReadStatus::kIoFailure;
    return ExactReadStatus::kTimeout;
  }
  CloseHandle(raw_thread);
  count = context.count;
  if (context.success) return ExactReadStatus::kOk;
  if (context.error == ERROR_BROKEN_PIPE || context.error == ERROR_HANDLE_EOF)
    return ExactReadStatus::kCleanEof;
  return ExactReadStatus::kIoFailure;
}

ExactReadStatus read_exact(HANDLE input, void* destination, DWORD bytes,
                           ULONGLONG& assembly_deadline) {
  if (GetFileType(input) != FILE_TYPE_PIPE) return ExactReadStatus::kIoFailure;
  auto* cursor = static_cast<unsigned char*>(destination);
  DWORD consumed = 0;
  if (assembly_deadline == 0)
    assembly_deadline = GetTickCount64() + kFrameAssemblyDeadlineMs;
  while (consumed < bytes) {
    const ULONGLONG now = GetTickCount64();
    if (now >= assembly_deadline) return ExactReadStatus::kTimeout;
    DWORD count = 0;
    const auto remaining = static_cast<DWORD>(
        std::min<ULONGLONG>(assembly_deadline - now,
                            std::numeric_limits<DWORD>::max()));
    const ExactReadStatus status = read_some_bounded(
        input, cursor + consumed, bytes - consumed, remaining, count);
    if (status == ExactReadStatus::kTimeout) return status;
    if (status == ExactReadStatus::kCleanEof)
      return consumed == 0 ? ExactReadStatus::kCleanEof
                           : ExactReadStatus::kPartialEof;
    if (status != ExactReadStatus::kOk) return status;
    if (count == 0) return ExactReadStatus::kPartialEof;
    consumed += count;
  }
  return ExactReadStatus::kOk;
}

struct WriteContext {
  HANDLE output = INVALID_HANDLE_VALUE;
  const unsigned char* source = nullptr;
  DWORD bytes = 0;
  bool success = false;
};

DWORD WINAPI write_worker(void* opaque) {
  auto& context = *static_cast<WriteContext*>(opaque);
  DWORD consumed = 0;
  while (consumed < context.bytes) {
    DWORD count = 0;
    if (!WriteFile(context.output, context.source + consumed,
                   context.bytes - consumed, &count, nullptr) || count == 0)
      return 1;
    consumed += count;
  }
  context.success = true;
  return 0;
}

bool write_exact_bounded(HANDLE output, const void* source, DWORD bytes) {
  if (GetFileType(output) != FILE_TYPE_PIPE) return false;
  WriteContext context{output, static_cast<const unsigned char*>(source), bytes,
                       false};
  HANDLE raw_thread = CreateThread(nullptr, 0, write_worker, &context, 0, nullptr);
  if (!raw_thread) return false;
  const DWORD first_wait =
      WaitForSingleObject(raw_thread, kResponseWriteDeadlineMs);
  if (first_wait != WAIT_OBJECT_0) {
    SetLastError(ERROR_SUCCESS);
    const BOOL cancelled = CancelSynchronousIo(raw_thread);
    const DWORD cancellation_error = cancelled ? ERROR_SUCCESS : GetLastError();
    const DWORD cancellation_wait =
        WaitForSingleObject(raw_thread, kIoCancellationGraceMs);
    if (cancellation_wait != WAIT_OBJECT_0) {
      CloseHandle(raw_thread);
      fail_stop_on_stuck_io();
    }
    CloseHandle(raw_thread);
    if (!cancelled && cancellation_error != ERROR_NOT_FOUND) return false;
    return false;
  }
  CloseHandle(raw_thread);
  return context.success;
}

bool identifier(std::string_view value, std::size_t minimum,
                std::size_t maximum, bool dotted) {
  if (value.size() < minimum || value.size() > maximum) return false;
  for (const unsigned char character : value) {
    const bool accepted = (character >= 'a' && character <= 'z') ||
                          (character >= 'A' && character <= 'Z') ||
                          (character >= '0' && character <= '9') ||
                          character == '_' || character == '-' ||
                          (dotted && character == '.');
    if (!accepted) return false;
  }
  return true;
}

bool exact_keys(const nlohmann::json& value,
                std::initializer_list<std::string_view> allowed,
                std::initializer_list<std::string_view> required) {
  if (!value.is_object()) return false;
  for (const auto& item : value.items()) {
    if (std::find(allowed.begin(), allowed.end(), item.key()) == allowed.end())
      return false;
  }
  for (const auto key : required) {
    if (!value.contains(std::string(key))) return false;
  }
  return true;
}

bool bounded_argument_value(const nlohmann::json& value, std::size_t depth) {
  if (depth > 2) return false;
  if (value.is_number_unsigned()) return value.get<std::uint64_t>() <= 0x7fffffff;
  if (value.is_string()) {
    const auto& text = value.get_ref<const std::string&>();
    return !text.empty() && text.size() <= kMaxArgumentStringBytes &&
           std::none_of(text.begin(), text.end(), [](unsigned char c) {
             return c < 0x20 || c == 0x7f;
           });
  }
  return false;
}

nlohmann::json receipt_json(const Receipt& receipt) {
  // Deliberately metadata-only. Never add paths, argument values, environment,
  // output, clipboard text, or Copilot prompt content to this object.
  return {
      {"action_id", receipt.action_id},
      {"action_kind", receipt.action_kind},
      {"manifest_sha256", receipt.manifest_sha256},
      {"executable_sha256", receipt.executable_sha256},
      {"argv_template_id", receipt.argv_template_id},
      {"cwd_id", receipt.cwd_id},
      {"status", receipt.status},
      {"error_code", receipt.error_code},
      {"duration_ms", receipt.duration_ms},
      {"stdout_bytes", receipt.stdout_bytes},
      {"stderr_bytes", receipt.stderr_bytes},
      {"stdin_bytes", receipt.stdin_bytes},
      {"output_truncated", receipt.output_truncated},
      {"process_created", receipt.process_created},
  };
}

}  // namespace

bool parse_strict_document(std::string_view input, std::size_t max_bytes,
                           nlohmann::json& output) noexcept {
  if (input.empty() || input.size() > max_bytes) return false;
  bool duplicate = false;
  bool excessive_depth = false;
  std::vector<std::set<std::string>> object_keys(kMaxJsonDepth + 2);
  const auto callback = [&](int depth, nlohmann::json::parse_event_t event,
                            nlohmann::json& parsed) {
    if (depth < 0 || static_cast<std::size_t>(depth) > kMaxJsonDepth) {
      excessive_depth = true;
      return false;
    }
    // nlohmann reports keys one level below the matching object_start event.
    if (event == nlohmann::json::parse_event_t::object_start) {
      if (static_cast<std::size_t>(depth + 1) >= object_keys.size()) {
        excessive_depth = true;
        return false;
      }
      object_keys[static_cast<std::size_t>(depth + 1)].clear();
    } else if (event == nlohmann::json::parse_event_t::key) {
      auto& keys = object_keys[static_cast<std::size_t>(depth)];
      const std::string key = parsed.get<std::string>();
      if (!keys.insert(key).second) {
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
    return false;
  }
  return !output.is_discarded() && !duplicate && !excessive_depth;
}

bool parse_request(std::string_view input, Request& request,
                   std::string& error_code) noexcept {
  error_code = "invalid_request";
  try {
    nlohmann::json value;
    if (!parse_strict_document(input, kMaxRequestFrameBytes, value)) {
      error_code = "invalid_json";
      return false;
    }
    if (!exact_keys(value,
                    {"schema", "request_id", "kind", "action_id",
                     "target_request_id", "deadline_ms", "arguments"},
                    {"schema", "request_id", "kind"})) {
      return false;
    }
    if (!value["schema"].is_string() ||
        value["schema"] != "lae.windows-broker.request.v1") {
      return false;
    }
    if (!value["request_id"].is_string() ||
        !identifier(value["request_id"].get_ref<const std::string&>(), 16, 64,
                    false)) {
      return false;
    }
    if (!value["kind"].is_string()) return false;

    Request parsed;
    parsed.request_id = value["request_id"].get<std::string>();
    const auto& kind = value["kind"].get_ref<const std::string&>();
    if (kind == "hello") {
      if (!exact_keys(value, {"schema", "request_id", "kind"},
                      {"schema", "request_id", "kind"})) return false;
      parsed.kind = RequestKind::kHello;
    } else if (kind == "cancel") {
      if (!exact_keys(value,
                      {"schema", "request_id", "kind", "target_request_id"},
                      {"schema", "request_id", "kind", "target_request_id"}))
        return false;
      if (!value["target_request_id"].is_string() ||
          !identifier(value["target_request_id"].get_ref<const std::string&>(),
                      16, 64, false)) return false;
      parsed.kind = RequestKind::kCancel;
      parsed.target_request_id = value["target_request_id"].get<std::string>();
    } else if (kind == "invoke") {
      if (!exact_keys(value,
                      {"schema", "request_id", "kind", "action_id",
                       "deadline_ms", "arguments"},
                      {"schema", "request_id", "kind", "action_id",
                       "deadline_ms", "arguments"})) return false;
      if (!value["action_id"].is_string() ||
          !identifier(value["action_id"].get_ref<const std::string&>(), 2, 96,
                      true)) return false;
      if (!value["deadline_ms"].is_number_unsigned()) return false;
      const auto deadline = value["deadline_ms"].get<std::uint64_t>();
      if (deadline < kMinDeadlineMs || deadline > kMaxDeadlineMs) return false;
      if (!value["arguments"].is_object() ||
          value["arguments"].size() > kMaxArguments) return false;
      for (const auto& item : value["arguments"].items()) {
        if (!identifier(item.key(), 1, 64, true) ||
            !bounded_argument_value(item.value(), 0)) return false;
      }
      parsed.kind = RequestKind::kInvoke;
      parsed.action_id = value["action_id"].get<std::string>();
      parsed.deadline_ms = static_cast<std::uint32_t>(deadline);
      parsed.arguments = value["arguments"];
    } else {
      return false;
    }
    request = std::move(parsed);
    error_code.clear();
    return true;
  } catch (...) {
    error_code = "invalid_request";
    return false;
  }
}

nlohmann::json response_json(const Response& response) {
  nlohmann::json value = {
      {"schema", "lae.windows-broker.response.v1"},
      {"request_id", response.request_id},
      {"status", response.status},
      {"error_code", response.error_code},
      {"result", response.result},
  };
  value["receipt"] = response.receipt ? receipt_json(*response.receipt)
                                       : nlohmann::json(nullptr);
  return value;
}

FrameStatus read_request_frame(HANDLE input, std::string& json,
                               std::string& error_code) noexcept {
  error_code.clear();
  json.clear();
  std::array<unsigned char, 4> prefix{};
  ULONGLONG assembly_deadline = 0;
  const ExactReadStatus prefix_status = read_exact(
      input, prefix.data(), static_cast<DWORD>(prefix.size()), assembly_deadline);
  if (prefix_status != ExactReadStatus::kOk) {
    if (prefix_status == ExactReadStatus::kCleanEof)
      return FrameStatus::kEndOfStream;
    error_code = prefix_status == ExactReadStatus::kTimeout
                     ? "protocol_io_timeout"
                     : prefix_status == ExactReadStatus::kIoFailure
                           ? "protocol_io_failed"
                           : "invalid_frame";
    return prefix_status == ExactReadStatus::kPartialEof
               ? FrameStatus::kRejected
               : FrameStatus::kIoFailure;
  }
  const std::uint32_t length = (static_cast<std::uint32_t>(prefix[0]) << 24) |
                               (static_cast<std::uint32_t>(prefix[1]) << 16) |
                               (static_cast<std::uint32_t>(prefix[2]) << 8) |
                               static_cast<std::uint32_t>(prefix[3]);
  if (length == 0 || length > kMaxRequestFrameBytes) {
    error_code = "invalid_frame";
    return FrameStatus::kRejected;
  }
  json.resize(length);
  const ExactReadStatus body_status =
      read_exact(input, json.data(), length, assembly_deadline);
  if (body_status != ExactReadStatus::kOk) {
    json.clear();
    error_code = body_status == ExactReadStatus::kTimeout
                     ? "protocol_io_timeout"
                     : body_status == ExactReadStatus::kIoFailure
                           ? "protocol_io_failed"
                           : "invalid_frame";
    return body_status == ExactReadStatus::kPartialEof ||
                   body_status == ExactReadStatus::kCleanEof
               ? FrameStatus::kRejected
               : FrameStatus::kIoFailure;
  }
  return FrameStatus::kOk;
}

bool write_response_frame(HANDLE output, const Response& response) noexcept {
  try {
    const std::string body = response_json(response).dump();
    if (body.empty() || body.size() > kMaxResponseFrameBytes ||
        body.size() > std::numeric_limits<std::uint32_t>::max()) return false;
    const auto length = static_cast<std::uint32_t>(body.size());
    std::vector<unsigned char> frame(4 + body.size());
    frame[0] = static_cast<unsigned char>((length >> 24) & 0xff);
    frame[1] = static_cast<unsigned char>((length >> 16) & 0xff);
    frame[2] = static_cast<unsigned char>((length >> 8) & 0xff);
    frame[3] = static_cast<unsigned char>(length & 0xff);
    std::copy(body.begin(), body.end(), frame.begin() + 4);
    return write_exact_bounded(output, frame.data(),
                               static_cast<DWORD>(frame.size()));
  } catch (...) {
    return false;
  }
}

}  // namespace lae::windows_broker
