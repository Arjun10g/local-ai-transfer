#include "win32_process.hpp"

#include "trust_anchor.hpp"

#include <algorithm>
#include <cstring>
#include <limits>
#include <utility>

namespace lae::windows_broker {
namespace {

std::string action_kind_name(ActionKind kind) {
  switch (kind) {
    case ActionKind::kProcess: return "process";
    case ActionKind::kApplication: return "application";
    case ActionKind::kBrowser: return "browser";
    case ActionKind::kCopilot: return "copilot";
    case ActionKind::kClipboardRead: return "clipboard_read";
    case ActionKind::kClipboardWrite: return "clipboard_write";
  }
  return "unknown";
}

bool launch_kind(ActionKind kind) {
  return kind == ActionKind::kProcess || kind == ActionKind::kApplication ||
         kind == ActionKind::kBrowser || kind == ActionKind::kCopilot;
}

Response failure(const Request& request, const ActionSpec& action,
                 const ManifestLease& manifest, std::string error_code,
                 std::uint64_t duration_ms = 0) {
  Receipt receipt;
  receipt.action_id = action.action_id;
  receipt.action_kind = action_kind_name(action.kind);
  receipt.manifest_sha256 = manifest.manifest_sha256;
  receipt.argv_template_id = action.argv_template_id;
  receipt.cwd_id = action.cwd_id;
  receipt.status = error_code == "cancelled" ? "cancelled"
                   : error_code == "deadline_expired" ? "timeout"
                                                       : "failed";
  receipt.error_code = error_code;
  receipt.duration_ms = duration_ms;
  receipt.process_created = false;
  return Response{request.request_id, receipt.status, std::move(error_code),
                  nlohmann::json::object(), std::move(receipt)};
}

const char* stop_code(const std::atomic<bool>& cancelled,
                      ULONGLONG deadline) {
  if (cancelled.load(std::memory_order_acquire)) return "cancelled";
  if (GetTickCount64() >= deadline) return "deadline_expired";
  return nullptr;
}

bool utf8_to_wide(const std::string& input, std::wstring& output) {
  output.clear();
  if (input.empty() || input.size() >
                           static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                        input.data(), static_cast<int>(input.size()),
                                        nullptr, 0);
  if (count <= 0) return false;
  output.resize(static_cast<std::size_t>(count));
  return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, input.data(),
                             static_cast<int>(input.size()), output.data(), count) ==
         count;
}

bool wide_to_utf8(const wchar_t* input, std::size_t units, std::string& output) {
  output.clear();
  if (units == 0) return true;  // A real empty clipboard value, not an API error.
  if (!input || units > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  const int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, input,
                                        static_cast<int>(units), nullptr, 0,
                                        nullptr, nullptr);
  if (count <= 0) return false;
  output.resize(static_cast<std::size_t>(count));
  return WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, input,
                             static_cast<int>(units), output.data(), count,
                             nullptr, nullptr) == count;
}

Response execute_clipboard(const Request& request, const ActionSpec& action,
                           const ManifestLease& manifest,
                           std::atomic<bool>& cancelled) {
  const ULONGLONG started = GetTickCount64();
  const ULONGLONG deadline =
      started + std::min<std::uint32_t>(request.deadline_ms, action.timeout_ms);
  while (!OpenClipboard(nullptr)) {
    if (const char* code = stop_code(cancelled, deadline))
      return failure(request, action, manifest, code, GetTickCount64() - started);
    Sleep(10);
  }
  struct ClipboardGuard {
    ~ClipboardGuard() { CloseClipboard(); }
  } guard;
  if (const char* code = stop_code(cancelled, deadline))
    return failure(request, action, manifest, code, GetTickCount64() - started);

  Response response;
  response.request_id = request.request_id;
  response.status = "ok";
  response.error_code = "ok";
  Receipt receipt;
  receipt.action_id = action.action_id;
  receipt.action_kind = action_kind_name(action.kind);
  receipt.manifest_sha256 = manifest.manifest_sha256;
  receipt.status = "ok";
  receipt.error_code = "ok";
  receipt.process_created = false;

  if (action.kind == ActionKind::kClipboardRead) {
    if (action.stdout_limit_bytes == 0 ||
        action.stdout_limit_bytes > 64 * 1024 ||
        !IsClipboardFormatAvailable(CF_UNICODETEXT))
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    if (const char* code = stop_code(cancelled, deadline))
      return failure(request, action, manifest, code, GetTickCount64() - started);
    HANDLE data = GetClipboardData(CF_UNICODETEXT);
    if (!data)
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    const SIZE_T bytes = GlobalSize(data);
    const SIZE_T maximum_utf16_bytes =
        (static_cast<SIZE_T>(action.stdout_limit_bytes) + 1) * sizeof(wchar_t);
    if (bytes == 0 || bytes % sizeof(wchar_t) != 0 ||
        bytes > maximum_utf16_bytes)
      return failure(request, action, manifest, "output_limit",
                     GetTickCount64() - started);
    const auto* text = static_cast<const wchar_t*>(GlobalLock(data));
    if (!text)
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    const std::size_t units = bytes / sizeof(wchar_t);
    const wchar_t* terminator = std::find(text, text + units, L'\0');
    if (terminator == text + units || stop_code(cancelled, deadline)) {
      GlobalUnlock(data);
      const char* code = stop_code(cancelled, deadline);
      return failure(request, action, manifest,
                     code ? code : "clipboard_invalid_text",
                     GetTickCount64() - started);
    }
    std::string utf8;
    const bool converted =
        wide_to_utf8(text, static_cast<std::size_t>(terminator - text), utf8);
    GlobalUnlock(data);
    if (!converted)
      return failure(request, action, manifest, "clipboard_invalid_text",
                     GetTickCount64() - started);
    if (utf8.size() > action.stdout_limit_bytes)
      return failure(request, action, manifest, "output_limit",
                     GetTickCount64() - started);
    if (const char* code = stop_code(cancelled, deadline))
      return failure(request, action, manifest, code, GetTickCount64() - started);
    response.result = {{"clipboard_text", utf8}, {"sensitive", true}};
    receipt.stdout_bytes = utf8.size();
  } else {
    const auto parameter = std::find_if(
        action.parameters.begin(), action.parameters.end(), [](const auto& item) {
          return item.placement == ParameterPlacement::kClipboard;
        });
    if (parameter == action.parameters.end() ||
        !request.arguments.contains(parameter->name) ||
        !request.arguments.at(parameter->name).is_string())
      return failure(request, action, manifest, "manifest_invalid",
                     GetTickCount64() - started);
    const std::string input =
        request.arguments.at(parameter->name).get<std::string>();
    std::wstring wide;
    if (input.size() > parameter->max_bytes || !utf8_to_wide(input, wide))
      return failure(request, action, manifest, "clipboard_invalid_text",
                     GetTickCount64() - started);
    if (const char* code = stop_code(cancelled, deadline))
      return failure(request, action, manifest, code, GetTickCount64() - started);
    if (wide.size() >
        (std::numeric_limits<SIZE_T>::max() / sizeof(wchar_t)) - 1)
      return failure(request, action, manifest, "output_limit",
                     GetTickCount64() - started);
    const SIZE_T bytes = (wide.size() + 1) * sizeof(wchar_t);
    HGLOBAL memory = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, bytes);
    if (!memory)
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    void* destination = GlobalLock(memory);
    if (!destination) {
      GlobalFree(memory);
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    }
    std::memcpy(destination, wide.c_str(), bytes);
    GlobalUnlock(memory);
    // This is the last cancellation/deadline check before the mutation. Once
    // EmptyClipboard succeeds, SetClipboardData must be attempted atomically
    // without manufacturing a cancellation receipt for an altered clipboard.
    if (const char* code = stop_code(cancelled, deadline)) {
      GlobalFree(memory);
      return failure(request, action, manifest, code, GetTickCount64() - started);
    }
    if (!EmptyClipboard() || !SetClipboardData(CF_UNICODETEXT, memory)) {
      GlobalFree(memory);
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    }
    response.result =
        {{"written", true}, {"bytes", input.size()}, {"sensitive", true}};
    receipt.stdin_bytes = input.size();
  }
  receipt.duration_ms = GetTickCount64() - started;
  response.receipt = std::move(receipt);
  return response;
}

}  // namespace

Response execute_bound_action(const Request& request, const ActionSpec& action,
                              const FileIdentitySpec* executable,
                              const DirectoryIdentitySpec* cwd,
                              const ManifestLease& manifest,
                              std::atomic<bool>& cancelled) noexcept {
  const ULONGLONG started = GetTickCount64();
  try {
    std::string argument_error;
    if (!action_arguments_match(action, request.arguments, argument_error))
      return failure(request, action, manifest, argument_error);
    if (action.kind == ActionKind::kClipboardRead ||
        action.kind == ActionKind::kClipboardWrite)
      return execute_clipboard(request, action, manifest, cancelled);
    (void)executable;
    (void)cwd;
    if (launch_kind(action.kind)) {
      // There is no safe in-process repair for broker death after child create
      // and before Job assignment. Process creation remains absent from this
      // translation unit until a supervisor-created containment primitive is
      // authenticated and a real deny-only/AppContainer confinement profile is
      // proven on Windows.
      if (!kSupervisorContainmentProven)
        return failure(request, action, manifest,
                       "launch_containment_unproven",
                       GetTickCount64() - started);
      if (!kLaunchConfinementProven)
        return failure(request, action, manifest,
                       "launch_confinement_unproven",
                       GetTickCount64() - started);
    }
    return failure(request, action, manifest, "broker_not_activated",
                   GetTickCount64() - started);
  } catch (...) {
    return failure(request, action, manifest, "internal_failure",
                   GetTickCount64() - started);
  }
}

}  // namespace lae::windows_broker
