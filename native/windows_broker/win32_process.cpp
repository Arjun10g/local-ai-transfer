#include "win32_process.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <memory>
#include <thread>
#include <utility>
#include <vector>

namespace lae::windows_broker {
namespace {

constexpr DWORD kReapDeadlineMs = 5000;
constexpr std::size_t kMaxCommandLineCharacters = 32767;

struct PipePair {
  UniqueHandle parent;
  UniqueHandle child;
};

struct JobContext {
  UniqueHandle job;
  UniqueHandle completion_port;
};

class BoundedCollector {
 public:
  BoundedCollector() = default;
  BoundedCollector(UniqueHandle input, std::uint32_t limit,
                   std::atomic<bool>& overflow)
      : input_(std::move(input)), limit_(limit), overflow_(&overflow) {}
  BoundedCollector(const BoundedCollector&) = delete;
  BoundedCollector& operator=(const BoundedCollector&) = delete;

  void start() {
    worker_ = std::thread([this] {
      std::array<unsigned char, 4096> buffer{};
      while (true) {
        DWORD count = 0;
        if (!ReadFile(input_.get(), buffer.data(), static_cast<DWORD>(buffer.size()),
                      &count, nullptr) || count == 0) break;
        bytes_seen_ += count;
        const std::size_t remaining = data_.size() < limit_ ? limit_ - data_.size() : 0;
        const std::size_t append = std::min<std::size_t>(remaining, count);
        data_.insert(data_.end(), buffer.begin(), buffer.begin() + append);
        if (append != count && overflow_)
          overflow_->store(true, std::memory_order_release);
      }
    });
  }

  void cancel_and_join() {
    if (!worker_.joinable()) return;
    CancelSynchronousIo(static_cast<HANDLE>(worker_.native_handle()));
    worker_.join();
  }

  ~BoundedCollector() { cancel_and_join(); }

  const std::vector<unsigned char>& data() const { return data_; }
  std::uint64_t bytes_seen() const { return bytes_seen_; }

 private:
  UniqueHandle input_;
  std::uint32_t limit_ = 0;
  std::atomic<bool>* overflow_ = nullptr;
  std::thread worker_;
  std::vector<unsigned char> data_;
  std::uint64_t bytes_seen_ = 0;
};

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
  return Response{request.request_id, receipt.status, std::move(error_code),
                  nlohmann::json::object(), std::move(receipt)};
}

std::wstring utf8_to_wide(const std::string& value) {
  if (value.empty()) return {};
  const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                        value.data(), static_cast<int>(value.size()),
                                        nullptr, 0);
  if (count <= 0) return {};
  std::wstring result(static_cast<std::size_t>(count), L'\0');
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                          static_cast<int>(value.size()), result.data(), count) != count)
    return {};
  return result;
}

std::string base64(const std::vector<unsigned char>& input) {
  static constexpr char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string output;
  output.reserve(((input.size() + 2) / 3) * 4);
  for (std::size_t index = 0; index < input.size(); index += 3) {
    const std::uint32_t a = input[index];
    const std::uint32_t b = index + 1 < input.size() ? input[index + 1] : 0;
    const std::uint32_t c = index + 2 < input.size() ? input[index + 2] : 0;
    const std::uint32_t value = (a << 16) | (b << 8) | c;
    output.push_back(alphabet[(value >> 18) & 63]);
    output.push_back(alphabet[(value >> 12) & 63]);
    output.push_back(index + 1 < input.size() ? alphabet[(value >> 6) & 63] : '=');
    output.push_back(index + 2 < input.size() ? alphabet[value & 63] : '=');
  }
  return output;
}

std::wstring quote_argument(const std::wstring& argument) {
  // Implements the CommandLineToArgvW inverse. lpApplicationName is still set
  // explicitly, so this string is never used for executable discovery.
  if (!argument.empty() && argument.find_first_of(L" \t\"") == std::wstring::npos)
    return argument;
  std::wstring output = L"\"";
  std::size_t slashes = 0;
  for (const wchar_t character : argument) {
    if (character == L'\\') {
      ++slashes;
    } else if (character == L'\"') {
      output.append(slashes * 2 + 1, L'\\');
      output.push_back(L'\"');
      slashes = 0;
    } else {
      output.append(slashes, L'\\');
      slashes = 0;
      output.push_back(character);
    }
  }
  output.append(slashes * 2, L'\\');
  output.push_back(L'\"');
  return output;
}

bool parameter_text(const ParameterSpec& parameter, const nlohmann::json& arguments,
                    std::string& result) {
  if (!arguments.contains(parameter.name)) return !parameter.required;
  const auto& value = arguments.at(parameter.name);
  if (value.is_string()) {
    result = value.get<std::string>();
    return true;
  }
  if (value.is_number_unsigned()) {
    result = std::to_string(value.get<std::uint64_t>());
    return true;
  }
  return false;
}

bool render_command_line(const FileIdentitySpec& executable,
                         const ActionSpec& action,
                         const nlohmann::json& arguments,
                         std::wstring& command_line, std::string& stdin_text) {
  command_line = quote_argument(executable.absolute_path);
  for (const auto& part : action.argv_template) {
    std::string text;
    if (part.literal) {
      text = part.value;
    } else {
      const auto parameter = std::find_if(
          action.parameters.begin(), action.parameters.end(),
          [&](const auto& item) { return item.name == part.value; });
      if (parameter == action.parameters.end() ||
          !parameter_text(*parameter, arguments, text)) return false;
    }
    const std::wstring wide = utf8_to_wide(text);
    if (wide.empty() && !text.empty()) return false;
    command_line.push_back(L' ');
    command_line += quote_argument(wide);
    if (command_line.size() > kMaxCommandLineCharacters) return false;
  }
  for (const auto& parameter : action.parameters) {
    if (parameter.placement != ParameterPlacement::kStdin) continue;
    if (!parameter_text(parameter, arguments, stdin_text)) return false;
  }
  return true;
}

std::vector<wchar_t> minimal_environment(const ManifestLease& manifest) {
  std::array<wchar_t, MAX_PATH + 1> windows{};
  const UINT count = GetWindowsDirectoryW(windows.data(),
                                          static_cast<UINT>(windows.size()));
  if (count == 0 || count >= windows.size()) return {};
  const std::wstring temp = manifest.runtime_directory.normalized_path;
  std::vector<std::wstring> entries = {
      std::wstring(L"SystemRoot=") + windows.data(),
      std::wstring(L"TEMP=") + temp,
      std::wstring(L"TMP=") + temp,
  };
  std::sort(entries.begin(), entries.end(), [](const auto& left, const auto& right) {
    return CompareStringOrdinal(left.c_str(), -1, right.c_str(), -1, TRUE) ==
           CSTR_LESS_THAN;
  });
  std::vector<wchar_t> block;
  for (const auto& entry : entries) {
    block.insert(block.end(), entry.begin(), entry.end());
    block.push_back(L'\0');
  }
  block.push_back(L'\0');
  return block;
}

bool make_pipe(PipePair& pair, bool parent_reads) {
  SECURITY_ATTRIBUTES attributes{sizeof(attributes), nullptr, TRUE};
  HANDLE read_handle = INVALID_HANDLE_VALUE;
  HANDLE write_handle = INVALID_HANDLE_VALUE;
  if (!CreatePipe(&read_handle, &write_handle, &attributes, 0)) return false;
  UniqueHandle read(read_handle);
  UniqueHandle write(write_handle);
  HANDLE parent = parent_reads ? read.get() : write.get();
  if (!SetHandleInformation(parent, HANDLE_FLAG_INHERIT, 0)) return false;
  if (parent_reads) {
    pair.parent = std::move(read);
    pair.child = std::move(write);
  } else {
    pair.parent = std::move(write);
    pair.child = std::move(read);
  }
  return true;
}

UniqueHandle restricted_primary_token() {
  HANDLE process_token_raw = INVALID_HANDLE_VALUE;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY |
                                                 TOKEN_ASSIGN_PRIMARY,
                        &process_token_raw)) return {};
  UniqueHandle process_token(process_token_raw);
  HANDLE restricted_raw = INVALID_HANDLE_VALUE;
  if (!CreateRestrictedToken(process_token.get(), DISABLE_MAX_PRIVILEGE, 0, nullptr,
                             0, nullptr, 0, nullptr, &restricted_raw)) return {};
  UniqueHandle restricted(restricted_raw);
  HANDLE primary_raw = INVALID_HANDLE_VALUE;
  if (!DuplicateTokenEx(restricted.get(), TOKEN_ALL_ACCESS, nullptr,
                        SecurityImpersonation, TokenPrimary, &primary_raw)) return {};
  return UniqueHandle(primary_raw);
}

JobContext configured_job(std::uint32_t max_processes) {
  JobContext context;
  context.job = UniqueHandle(CreateJobObjectW(nullptr, nullptr));
  context.completion_port = UniqueHandle(
      CreateIoCompletionPort(INVALID_HANDLE_VALUE, nullptr, 0, 1));
  if (!context.job || !context.completion_port) return {};
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags =
      JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
  limits.BasicLimitInformation.ActiveProcessLimit = max_processes;
  if (!SetInformationJobObject(context.job.get(),
                               JobObjectExtendedLimitInformation, &limits,
                               sizeof(limits))) return {};
  JOBOBJECT_ASSOCIATE_COMPLETION_PORT association{};
  association.CompletionKey = context.job.get();
  association.CompletionPort = context.completion_port.get();
  if (!SetInformationJobObject(context.job.get(),
                               JobObjectAssociateCompletionPortInformation,
                               &association, sizeof(association))) return {};
  return context;
}

bool job_has_zero_active_processes(HANDLE job) {
  JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting{};
  return QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                   &accounting, sizeof(accounting), nullptr) &&
         accounting.ActiveProcesses == 0;
}

bool wait_for_job_zero(const JobContext& context, DWORD timeout_ms,
                       const std::atomic<bool>* cancelled = nullptr,
                       const std::atomic<bool>* output_overflow = nullptr) {
  const ULONGLONG deadline = GetTickCount64() + timeout_ms;
  while (true) {
    if (job_has_zero_active_processes(context.job.get())) return true;
    if (cancelled && cancelled->load(std::memory_order_acquire)) return false;
    if (output_overflow && output_overflow->load(std::memory_order_acquire))
      return false;
    const ULONGLONG now = GetTickCount64();
    if (now >= deadline) return false;
    DWORD message = 0;
    ULONG_PTR key = 0;
    LPOVERLAPPED detail = nullptr;
    const DWORD slice = static_cast<DWORD>(
        std::min<ULONGLONG>(100, deadline - now));
    const BOOL received = GetQueuedCompletionStatus(
        context.completion_port.get(), &message, &key, &detail, slice);
    if (!received && GetLastError() != WAIT_TIMEOUT) return false;
    if (received && key == reinterpret_cast<ULONG_PTR>(context.job.get()) &&
        message == JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO &&
        job_has_zero_active_processes(context.job.get())) return true;
  }
}

bool process_image_matches(HANDLE process, const IdentityLease& executable_lease) {
  std::vector<wchar_t> path(kMaxCommandLineCharacters + 1);
  DWORD size = static_cast<DWORD>(path.size());
  if (!QueryFullProcessImageNameW(process, 0, path.data(), &size) || size == 0 ||
      size >= path.size()) return false;
  const std::wstring observed(path.data(), size);
  return CompareStringOrdinal(observed.data(), static_cast<int>(observed.size()),
                              executable_lease.normalized_path.data(),
                              static_cast<int>(executable_lease.normalized_path.size()),
                              TRUE) == CSTR_EQUAL &&
         same_identity(executable_lease);
}

Response execute_clipboard(const Request& request, const ActionSpec& action,
                           const ManifestLease& manifest,
                           std::atomic<bool>& cancelled) {
  const ULONGLONG started = GetTickCount64();
  const ULONGLONG deadline = started +
      std::min<std::uint32_t>(request.deadline_ms, action.timeout_ms);
  while (!OpenClipboard(nullptr)) {
    if (cancelled.load(std::memory_order_acquire))
      return failure(request, action, manifest, "cancelled", GetTickCount64() - started);
    if (GetTickCount64() >= deadline)
      return failure(request, action, manifest, "deadline_expired",
                     GetTickCount64() - started);
    Sleep(10);
  }
  struct ClipboardGuard { ~ClipboardGuard() { CloseClipboard(); } } guard;
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
  if (action.kind == ActionKind::kClipboardRead) {
    if (!IsClipboardFormatAvailable(CF_UNICODETEXT))
      return failure(request, action, manifest, "clipboard_unavailable",
                     GetTickCount64() - started);
    HANDLE data = GetClipboardData(CF_UNICODETEXT);
    if (!data) return failure(request, action, manifest, "clipboard_unavailable");
    const SIZE_T bytes = GlobalSize(data);
    if (bytes == 0 || bytes > 128 * 1024)
      return failure(request, action, manifest, "output_limit");
    const auto* text = static_cast<const wchar_t*>(GlobalLock(data));
    if (!text) return failure(request, action, manifest, "clipboard_unavailable");
    const std::size_t units = bytes / sizeof(wchar_t);
    const wchar_t* terminator = std::find(text, text + units, L'\0');
    if (bytes % sizeof(wchar_t) != 0 || terminator == text + units) {
      GlobalUnlock(data);
      return failure(request, action, manifest, "clipboard_unavailable");
    }
    const int utf8_bytes = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, text, static_cast<int>(terminator - text),
        nullptr, 0, nullptr, nullptr);
    std::string utf8(utf8_bytes > 0 ? static_cast<std::size_t>(utf8_bytes) : 0,
                     '\0');
    const bool converted = utf8_bytes >= 0 &&
        WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text,
                            static_cast<int>(terminator - text), utf8.data(),
                            utf8_bytes, nullptr, nullptr) == utf8_bytes;
    GlobalUnlock(data);
    if (!converted || utf8.size() > 64 * 1024)
      return failure(request, action, manifest, "output_limit");
    response.result = {{"clipboard_text", utf8}, {"sensitive", true}};
    receipt.stdout_bytes = utf8.size();
  } else {
    const auto parameter = std::find_if(
        action.parameters.begin(), action.parameters.end(), [](const auto& item) {
          return item.placement == ParameterPlacement::kClipboard;
        });
    if (parameter == action.parameters.end())
      return failure(request, action, manifest, "manifest_invalid");
    const std::string input = request.arguments.at(parameter->name).get<std::string>();
    const std::wstring wide = utf8_to_wide(input);
    if (wide.empty() && !input.empty())
      return failure(request, action, manifest, "arguments_rejected");
    const SIZE_T bytes = (wide.size() + 1) * sizeof(wchar_t);
    HGLOBAL memory = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, bytes);
    if (!memory) return failure(request, action, manifest, "clipboard_unavailable");
    void* destination = GlobalLock(memory);
    if (!destination) { GlobalFree(memory); return failure(request, action, manifest, "clipboard_unavailable"); }
    memcpy(destination, wide.c_str(), bytes);
    GlobalUnlock(memory);
    if (!EmptyClipboard() || !SetClipboardData(CF_UNICODETEXT, memory)) {
      GlobalFree(memory);
      return failure(request, action, manifest, "clipboard_unavailable");
    }
    response.result = {{"written", true}, {"bytes", input.size()}, {"sensitive", true}};
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
    const ULONGLONG effective_deadline = started +
        std::min<std::uint32_t>(request.deadline_ms, action.timeout_ms);
    if (cancelled.load(std::memory_order_acquire))
      return failure(request, action, manifest, "cancelled");
    std::string argument_error;
    if (!action_arguments_match(action, request.arguments, argument_error))
      return failure(request, action, manifest, argument_error);
    if (action.kind == ActionKind::kClipboardRead ||
        action.kind == ActionKind::kClipboardWrite)
      return execute_clipboard(request, action, manifest, cancelled);
    if (!executable || !cwd || !action.restricted_token_required)
      return failure(request, action, manifest, "manifest_invalid");

    IdentityLease executable_lease;
    IdentityLease cwd_lease;
    std::string identity_error;
    if (!acquire_executable_lease(*executable, executable_lease, identity_error,
                                  &cancelled, effective_deadline)) {
      if (cancelled.load(std::memory_order_acquire)) identity_error = "cancelled";
      else if (GetTickCount64() >= effective_deadline)
        identity_error = "deadline_expired";
      return failure(request, action, manifest, identity_error,
                     GetTickCount64() - started);
    }
    if (!acquire_directory_lease(*cwd, cwd_lease, identity_error))
      return failure(request, action, manifest, identity_error,
                     GetTickCount64() - started);
    if (cancelled.load(std::memory_order_acquire))
      return failure(request, action, manifest, "cancelled",
                     GetTickCount64() - started);
    if (GetTickCount64() >= effective_deadline)
      return failure(request, action, manifest, "deadline_expired",
                     GetTickCount64() - started);

    std::wstring command_line;
    std::string stdin_text;
    if (!render_command_line(*executable, action, request.arguments, command_line,
                             stdin_text))
      return failure(request, action, manifest, "arguments_rejected");
    std::vector<wchar_t> environment = minimal_environment(manifest);
    if (environment.empty())
      return failure(request, action, manifest, "environment_unavailable");

    PipePair stdin_pipe;
    PipePair stdout_pipe;
    PipePair stderr_pipe;
    if (!make_pipe(stdin_pipe, false) || !make_pipe(stdout_pipe, true) ||
        !make_pipe(stderr_pipe, true))
      return failure(request, action, manifest, "pipe_setup_failed");

    UniqueHandle token = restricted_primary_token();
    if (!token)
      return failure(request, action, manifest, "restricted_token_unavailable");
    JobContext job = configured_job(action.max_processes);
    if (!job.job || !job.completion_port)
      return failure(request, action, manifest, "job_setup_failed");

    std::array<HANDLE, 3> inherited = {
        stdin_pipe.child.get(), stdout_pipe.child.get(), stderr_pipe.child.get()};
    SIZE_T attribute_bytes = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &attribute_bytes);
    if (attribute_bytes == 0 || attribute_bytes > 1024 * 1024)
      return failure(request, action, manifest, "process_create_failed");
    std::vector<unsigned char> attribute_storage(attribute_bytes);
    auto* attributes = reinterpret_cast<PPROC_THREAD_ATTRIBUTE_LIST>(
        attribute_storage.data());
    if (!InitializeProcThreadAttributeList(attributes, 1, 0, &attribute_bytes))
      return failure(request, action, manifest, "process_create_failed");
    struct AttributeGuard {
      PPROC_THREAD_ATTRIBUTE_LIST value;
      ~AttributeGuard() { DeleteProcThreadAttributeList(value); }
    } attribute_guard{attributes};
    if (!UpdateProcThreadAttribute(
            attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited.data(),
            inherited.size() * sizeof(HANDLE), nullptr, nullptr))
      return failure(request, action, manifest, "process_create_failed");

    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = stdin_pipe.child.get();
    startup.StartupInfo.hStdOutput = stdout_pipe.child.get();
    startup.StartupInfo.hStdError = stderr_pipe.child.get();
    startup.lpAttributeList = attributes;
    PROCESS_INFORMATION process_info{};
    DWORD flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT |
                  EXTENDED_STARTUPINFO_PRESENT;
    flags |= action.visible ? CREATE_NEW_PROCESS_GROUP : CREATE_NO_WINDOW;
    if (!CreateProcessAsUserW(
            token.get(), executable_lease.normalized_path.c_str(), command_line.data(),
            nullptr, nullptr, TRUE, flags, environment.data(),
            cwd_lease.normalized_path.c_str(), &startup.StartupInfo, &process_info))
      return failure(request, action, manifest, "process_create_failed",
                     GetTickCount64() - started);
    UniqueHandle process(process_info.hProcess);
    UniqueHandle primary_thread(process_info.hThread);
    stdin_pipe.child = UniqueHandle();
    stdout_pipe.child = UniqueHandle();
    stderr_pipe.child = UniqueHandle();

    auto terminate_and_reap = [&](const char* code) {
      TerminateJobObject(job.job.get(), 0xE0000001u);
      const DWORD reaped = WaitForSingleObject(process.get(), kReapDeadlineMs);
      const bool job_empty = wait_for_job_zero(job, kReapDeadlineMs);
      return failure(request, action, manifest,
                     reaped == WAIT_OBJECT_0 && job_empty ? code
                                                          : "job_reap_failed",
                     GetTickCount64() - started);
    };

    // No user-mode child code has run before every check and Job assignment.
    if (!process_image_matches(process.get(), executable_lease)) {
      TerminateProcess(process.get(), 0xE0000002u);
      WaitForSingleObject(process.get(), kReapDeadlineMs);
      return failure(request, action, manifest, "process_image_mismatch",
                     GetTickCount64() - started);
    }
    if (!AssignProcessToJobObject(job.job.get(), process.get())) {
      TerminateProcess(process.get(), 0xE0000003u);
      WaitForSingleObject(process.get(), kReapDeadlineMs);
      return failure(request, action, manifest, "job_assignment_failed",
                     GetTickCount64() - started);
    }
    if (ResumeThread(primary_thread.get()) != 1)
      return terminate_and_reap("resume_failed");

    std::atomic<bool> output_overflow{false};
    BoundedCollector stdout_collector(std::move(stdout_pipe.parent),
                                      action.stdout_limit_bytes, output_overflow);
    BoundedCollector stderr_collector(std::move(stderr_pipe.parent),
                                      action.stderr_limit_bytes, output_overflow);
    stdout_collector.start();
    stderr_collector.start();
    UniqueHandle stdin_parent = std::move(stdin_pipe.parent);
    std::thread stdin_writer([input = std::move(stdin_parent), &stdin_text]() mutable {
      DWORD written = 0;
      std::size_t offset = 0;
      while (offset < stdin_text.size()) {
        const DWORD amount = static_cast<DWORD>(std::min<std::size_t>(
            stdin_text.size() - offset, 4096));
        if (!WriteFile(input.get(), stdin_text.data() + offset, amount,
                       &written, nullptr) || written == 0) break;
        offset += written;
      }
      input = UniqueHandle();
    });

    DWORD wait = WAIT_TIMEOUT;
    const char* forced_code = nullptr;
    while (true) {
      if (cancelled.load(std::memory_order_acquire)) {
        forced_code = "cancelled";
        break;
      }
      if (output_overflow.load(std::memory_order_acquire)) {
        forced_code = "output_limit";
        break;
      }
      const ULONGLONG now = GetTickCount64();
      if (now >= effective_deadline) {
        forced_code = "deadline_expired";
        break;
      }
      const DWORD slice = static_cast<DWORD>(
          std::min<ULONGLONG>(100, effective_deadline - now));
      wait = WaitForSingleObject(process.get(), slice);
      if (wait == WAIT_OBJECT_0 || wait == WAIT_FAILED) break;
    }
    if (forced_code) TerminateJobObject(job.job.get(), 0xE0000004u);
    const DWORD reaped = WaitForSingleObject(process.get(), kReapDeadlineMs);
    bool entire_job_reaped = false;
    if (reaped == WAIT_OBJECT_0) {
      const ULONGLONG now = GetTickCount64();
      const DWORD remaining = now < effective_deadline
                                  ? static_cast<DWORD>(effective_deadline - now)
                                  : 0;
      entire_job_reaped = wait_for_job_zero(
          job, forced_code ? kReapDeadlineMs : remaining,
          forced_code ? nullptr : &cancelled,
          forced_code ? nullptr : &output_overflow);
    }
    if (!entire_job_reaped && !forced_code) {
      forced_code = cancelled.load(std::memory_order_acquire)
                        ? "cancelled"
                        : output_overflow.load(std::memory_order_acquire)
                              ? "output_limit"
                              : "deadline_expired";
      TerminateJobObject(job.job.get(), 0xE0000005u);
      entire_job_reaped = wait_for_job_zero(job, kReapDeadlineMs);
    }
    // Keep capturing within fixed buffers while descendants remain. Only after
    // accounting proves the entire Job empty (or the proof fails) are pipe I/O
    // tasks cancelled and joined.
    if (stdin_writer.joinable()) {
      CancelSynchronousIo(static_cast<HANDLE>(stdin_writer.native_handle()));
      stdin_writer.join();
    }
    stdout_collector.cancel_and_join();
    stderr_collector.cancel_and_join();
    if (reaped != WAIT_OBJECT_0 || !entire_job_reaped)
      return failure(request, action, manifest, "job_reap_failed",
                     GetTickCount64() - started);
    if (wait == WAIT_FAILED && !forced_code)
      return failure(request, action, manifest, "process_wait_failed",
                     GetTickCount64() - started);

    DWORD exit_code = 0;
    if (!GetExitCodeProcess(process.get(), &exit_code))
      return failure(request, action, manifest, "process_wait_failed");
    Receipt receipt;
    receipt.action_id = action.action_id;
    receipt.action_kind = action_kind_name(action.kind);
    receipt.manifest_sha256 = manifest.manifest_sha256;
    receipt.executable_sha256 = executable->sha256;
    receipt.argv_template_id = action.argv_template_id;
    receipt.cwd_id = action.cwd_id;
    receipt.status = forced_code ? (std::string(forced_code) == "cancelled"
                                        ? "cancelled"
                                        : std::string(forced_code) == "deadline_expired"
                                              ? "timeout"
                                              : "failed")
                                 : (exit_code == 0 ? "ok" : "failed");
    receipt.error_code = forced_code ? forced_code
                                     : (exit_code == 0 ? "ok" : "child_exit_nonzero");
    receipt.duration_ms = GetTickCount64() - started;
    receipt.stdout_bytes = stdout_collector.bytes_seen();
    receipt.stderr_bytes = stderr_collector.bytes_seen();
    receipt.stdin_bytes = stdin_text.size();
    receipt.output_truncated = output_overflow.load(std::memory_order_acquire);
    receipt.restricted_token = true;
    receipt.job_assigned_before_resume = true;
    nlohmann::json result = {
        {"exit_code", exit_code},
        {"stdout_base64", base64(stdout_collector.data())},
        {"stderr_base64", base64(stderr_collector.data())},
        {"encoding", "base64"},
    };
    return Response{request.request_id, receipt.status, receipt.error_code,
                    std::move(result), std::move(receipt)};
  } catch (...) {
    return failure(request, action, manifest, "internal_failure",
                   GetTickCount64() - started);
  }
}

}  // namespace lae::windows_broker
