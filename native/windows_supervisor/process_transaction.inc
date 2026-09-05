// Included only from authority.cpp, inside its anonymous namespace. Nothing in
// this file is a public API and this source remains outside every build target.

constexpr std::size_t kLaunchMaxArguments = 16;
constexpr std::size_t kLaunchMaxArgumentCodeUnits = 2048;
constexpr std::size_t kLaunchMaxCommandCodeUnits = 16384;
constexpr std::size_t kLaunchMaxPathCodeUnits = 1024;
constexpr std::uint64_t kLaunchMaxRuntimeMs = 120000;
constexpr std::uint64_t kLaunchMaxOutputBytes =
    2 * kMaxCapturedBytesPerStream;

enum class LaunchClass : std::uint8_t {
  kAllowlistedProcess,
  kApprovedApplication,
  kApprovedBrowser,
  kApprovedCopilot,
};

enum class LaunchResultCode : std::uint8_t {
  kUnavailable,
  kInvalidRequest,
  kCapabilityRefused,
  kJournalUnavailable,
  kPreDispatchFailure,
  kDispatchedUnknown,
  kCancelled,
  kDeadline,
  kOutputLimit,
  kExitFailure,
  kTerminalFailure,
  kOk,
};

struct LaunchLimits final {
  std::uint64_t deadline_at_ms = 0;
  std::uint64_t cleanup_deadline_at_ms = 0;
  std::uint64_t timeout_ms = 0;
  std::uint32_t stdout_limit = 0;
  std::uint32_t stderr_limit = 0;
};

struct LaunchPlan final {
  LaunchClass launch_class = LaunchClass::kAllowlistedProcess;
  std::uint64_t request_id = 0;
  std::uint64_t operation_id = 0;
  std::uint64_t session_id = 0;
  std::wstring executable;
  std::wstring working_directory;
  std::vector<std::wstring> arguments;
  FixedIdentity expected_executable{};
  std::array<std::byte, 32> expected_content_sha256{};
  std::array<std::byte, 32> expected_token_policy_digest{};
  LaunchLimits limits{};
};

struct LaunchDigests final {
  std::array<std::byte, 32> request{};
  std::array<std::byte, 32> executable{};
  std::array<std::byte, 32> token{};
  std::array<std::byte, 32> arguments{};
  std::array<std::byte, 32> limits{};
  std::array<std::byte, 32> environment{};
  std::array<std::byte, 32> operation{};
};

struct LaunchReceipt final {
  LaunchResultCode status = LaunchResultCode::kUnavailable;
  std::uint32_t stdout_bytes = 0;
  std::uint32_t stderr_bytes = 0;
  std::uint32_t exit_code = 0;
  bool journal_bound = false;
  bool job_reaped = false;
  bool mutation_attempted = false;
};

bool concrete_terminal_failure(LaunchResultCode status) noexcept {
  switch (status) {
    case LaunchResultCode::kPreDispatchFailure:
    case LaunchResultCode::kCancelled:
    case LaunchResultCode::kDeadline:
    case LaunchResultCode::kOutputLimit:
    case LaunchResultCode::kExitFailure:
    case LaunchResultCode::kTerminalFailure:
      return true;
    case LaunchResultCode::kUnavailable:
    case LaunchResultCode::kInvalidRequest:
    case LaunchResultCode::kCapabilityRefused:
    case LaunchResultCode::kJournalUnavailable:
    case LaunchResultCode::kDispatchedUnknown:
    case LaunchResultCode::kOk:
      return false;
  }
  return false;
}

struct ProcessLaunchAuthority final {
  std::mutex mutex;
  JournalAuthority journal;
  DurableJournalAdapter* const durable_adapter = nullptr;
  const std::uint64_t root_generation = 0;
  const bool recovered = false;
  bool active = false;
  bool poisoned = false;
  std::atomic<bool> shutting_down{false};
};

ProcessLaunchAuthority& process_launch_authority() noexcept {
  static ProcessLaunchAuthority state;
  return state;
}

bool process_launch_gates_open() noexcept {
  return trust_gates_open() && kProcessLaunchAvailable &&
      kSupervisorOwnedProcessTransactionAccepted;
}

bool zero_digest(const std::array<std::byte, 32>& value) noexcept {
  return std::all_of(value.begin(), value.end(),
                     [](std::byte item) { return item == std::byte{}; });
}

template <std::size_t Size>
bool all_zero(const std::array<std::byte, Size>& value) noexcept {
  return std::all_of(value.begin(), value.end(),
                     [](std::byte item) { return item == std::byte{}; });
}

bool valid_launch_class(LaunchClass value) noexcept {
  switch (value) {
    case LaunchClass::kAllowlistedProcess:
    case LaunchClass::kApprovedApplication:
    case LaunchClass::kApprovedBrowser:
    case LaunchClass::kApprovedCopilot:
      return true;
  }
  return false;
}

wchar_t upper_ascii(wchar_t value) noexcept {
  return value >= L'a' && value <= L'z' ? value - (L'a' - L'A') : value;
}

bool reserved_dos_component(const std::wstring& path, std::size_t begin,
                            std::size_t length) noexcept {
  const std::size_t dot = path.find(L'.', begin);
  const std::size_t stem_end = dot == std::wstring::npos || dot >= begin + length
      ? begin + length : dot;
  const std::size_t stem_size = stem_end - begin;
  auto equal = [&](const wchar_t* literal, std::size_t count) noexcept {
    if (stem_size != count) return false;
    for (std::size_t index = 0; index < count; ++index)
      if (upper_ascii(path[begin + index]) != literal[index]) return false;
    return true;
  };
  if (equal(L"CON", 3) || equal(L"PRN", 3) || equal(L"AUX", 3) ||
      equal(L"NUL", 3)) return true;
  if (stem_size == 4 &&
      ((upper_ascii(path[begin]) == L'C' &&
        upper_ascii(path[begin + 1]) == L'O' &&
        upper_ascii(path[begin + 2]) == L'M') ||
       (upper_ascii(path[begin]) == L'L' &&
        upper_ascii(path[begin + 1]) == L'P' &&
        upper_ascii(path[begin + 2]) == L'T')) &&
      path[begin + 3] >= L'1' && path[begin + 3] <= L'9') return true;
  return false;
}

bool absolute_local_path(const std::wstring& path, bool executable) noexcept {
  if (path.size() < 3 || path.size() > kLaunchMaxPathCodeUnits ||
      path[1] != L':' || path[2] != L'\\' ||
      path.rfind(L"\\\\", 0) == 0 || path.rfind(L"\\\\?\\", 0) == 0 ||
      path.rfind(L"\\??\\", 0) == 0) return false;
  for (std::size_t index = 0; index < path.size(); ++index) {
    const wchar_t value = path[index];
    if (value == L'\0' || value < 0x20 || value == L'/' ||
        value == L'"' || value == L'<' || value == L'>' || value == L'|' ||
        value == L'?' || value == L'*' ||
        (value == L':' && index != 1)) return false;
  }
  std::size_t begin = 3;
  while (begin <= path.size()) {
    const std::size_t end = path.find(L'\\', begin);
    const std::size_t length = (end == std::wstring::npos ? path.size() : end) - begin;
    if (length == 0 || path[begin + length - 1] == L'.' ||
        path[begin + length - 1] == L' ' ||
        (length == 1 && path[begin] == L'.') ||
        (length == 2 && path[begin] == L'.' && path[begin + 1] == L'.') ||
        reserved_dos_component(path, begin, length)) return false;
    if (end == std::wstring::npos) break;
    begin = end + 1;
  }
  if (!executable || path.size() < 4) return !executable;
  const std::wstring suffix = path.substr(path.size() - 4);
  return _wcsicmp(suffix.c_str(), L".exe") == 0;
}

bool argument_shape(const std::wstring& value) noexcept {
  if (value.empty() || value.size() > kLaunchMaxArgumentCodeUnits) return false;
  for (const wchar_t item : value)
    if (item == L'\0' || item < 0x20) return false;
  return true;
}

bool checkpoint(const SupervisorState& supervisor,
                const ProcessLaunchAuthority& authority,
                const LaunchPlan& plan, HANDLE cancellation) noexcept {
  const std::uint64_t now = GetTickCount64();
  return process_launch_gates_open() && !authority.poisoned &&
      !authority.shutting_down.load(std::memory_order_acquire) &&
      authority.root_generation != 0 && supervisor.root_job &&
      plan.limits.deadline_at_ms > now &&
      plan.limits.deadline_at_ms <= now + kLaunchMaxRuntimeMs &&
      (!cancellation || WaitForSingleObject(cancellation, 0) == WAIT_TIMEOUT);
}

bool append_bytes(std::array<std::byte, 32768>& output, std::size_t& used,
                  const void* input, std::size_t count) noexcept {
  if (!input || used > output.size() || count > output.size() - used) return false;
  std::memcpy(output.data() + used, input, count);
  used += count;
  return true;
}

bool append_u64(std::array<std::byte, 32768>& output, std::size_t& used,
                std::uint64_t value) noexcept {
  return append_bytes(output, used, &value, sizeof(value));
}

bool append_u32(std::array<std::byte, 32768>& output, std::size_t& used,
                std::uint32_t value) noexcept {
  return append_bytes(output, used, &value, sizeof(value));
}

bool append_wstring(std::array<std::byte, 32768>& output, std::size_t& used,
                    const std::wstring& value) noexcept {
  const std::uint32_t code_units = static_cast<std::uint32_t>(value.size());
  return append_u32(output, used, code_units) &&
      append_bytes(output, used, value.data(), value.size() * sizeof(wchar_t));
}

bool sha256_bytes(const void* bytes, std::size_t count,
                  std::array<std::byte, 32>& output) noexcept {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_bytes = 0;
  DWORD returned = 0;
  std::array<UCHAR, 4096> object{};
  bool ok = bytes && count <= 32768 &&
      BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM,
                                  nullptr, 0) == 0 &&
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_bytes),
                        sizeof(object_bytes), &returned, 0) == 0 &&
      object_bytes > 0 && object_bytes <= object.size() &&
      BCryptCreateHash(algorithm, &hash, object.data(), object_bytes,
                       nullptr, 0, 0) == 0 &&
      BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<void*>(bytes)),
                     static_cast<ULONG>(count), 0) == 0 &&
      BCryptFinishHash(hash, reinterpret_cast<PUCHAR>(output.data()),
                       static_cast<ULONG>(output.size()), 0) == 0;
  if (hash) BCryptDestroyHash(hash);
  SecureZeroMemory(object.data(), object.size());
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  return ok;
}

bool digest_plan(const LaunchPlan& plan, std::uint64_t root_generation,
                 const std::wstring& system_root,
                 LaunchDigests& digests) noexcept {
  if (!valid_launch_class(plan.launch_class) ||
      !absolute_local_path(plan.executable, true) ||
      !absolute_local_path(plan.working_directory, false) ||
      plan.request_id == 0 || plan.operation_id == 0 || plan.session_id == 0 ||
      plan.arguments.size() > kLaunchMaxArguments ||
      plan.limits.timeout_ms == 0 || plan.limits.timeout_ms > kLaunchMaxRuntimeMs ||
      plan.limits.stdout_limit == 0 ||
      plan.limits.stdout_limit > kMaxCapturedBytesPerStream ||
      plan.limits.stderr_limit == 0 ||
      plan.limits.stderr_limit > kMaxCapturedBytesPerStream ||
      static_cast<std::uint64_t>(plan.limits.stdout_limit) +
              plan.limits.stderr_limit > kLaunchMaxOutputBytes ||
      zero_digest(plan.expected_content_sha256) ||
      zero_digest(plan.expected_token_policy_digest) ||
      !plan.expected_executable.regular ||
      !plan.expected_executable.reparse_free ||
      plan.expected_executable.volume_serial == 0 ||
      all_zero(plan.expected_executable.file_id) ||
      plan.expected_executable.size == 0 ||
      plan.expected_executable.size > kMaxIdentityFileBytes ||
      zero_digest(plan.expected_executable.digest) || system_root.empty()) return false;
  for (const auto& argument : plan.arguments)
    if (!argument_shape(argument)) return false;

  std::array<std::byte, 32768> material{};
  std::size_t used = 0;
  constexpr char kRequestDomain[] = "lae.windows-supervisor.launch-request.v1";
  if (!append_bytes(material, used, kRequestDomain, sizeof(kRequestDomain) - 1) ||
      !append_u64(material, used, plan.request_id) ||
      !append_u64(material, used, plan.operation_id) ||
      !append_u64(material, used, plan.session_id) ||
      !append_u64(material, used, root_generation) ||
      !append_u32(material, used, static_cast<std::uint32_t>(plan.launch_class)) ||
      !append_wstring(material, used, plan.executable) ||
      !append_wstring(material, used, plan.working_directory) ||
      !sha256_bytes(material.data(), used, digests.request)) return false;

  used = 0;
  for (const auto& argument : plan.arguments)
    if (!append_wstring(material, used, argument)) return false;
  if (!sha256_bytes(material.data(), used, digests.arguments)) return false;
  used = 0;
  constexpr char kExecutableDomain[] =
      "lae.windows-supervisor.executable-identity.v1";
  if (!append_bytes(material, used, kExecutableDomain,
                    sizeof(kExecutableDomain) - 1) ||
      !append_bytes(material, used, plan.expected_content_sha256.data(),
                    plan.expected_content_sha256.size()) ||
      !append_bytes(material, used, plan.expected_executable.digest.data(),
                    plan.expected_executable.digest.size()) ||
      !append_bytes(material, used, plan.expected_executable.file_id.data(),
                    plan.expected_executable.file_id.size()) ||
      !append_u64(material, used, plan.expected_executable.volume_serial) ||
      !append_u64(material, used, plan.expected_executable.file_index) ||
      !append_u32(material, used, plan.expected_executable.size) ||
      !sha256_bytes(material.data(), used, digests.executable)) return false;
  digests.token = plan.expected_token_policy_digest;

  used = 0;
  if (!append_u64(material, used, plan.limits.deadline_at_ms) ||
      !append_u64(material, used, plan.limits.cleanup_deadline_at_ms) ||
      !append_u64(material, used, plan.limits.timeout_ms) ||
      !append_u32(material, used, plan.limits.stdout_limit) ||
      !append_u32(material, used, plan.limits.stderr_limit) ||
      !sha256_bytes(material.data(), used, digests.limits)) return false;

  used = 0;
  constexpr wchar_t kSystemRootName[] = L"SystemRoot";
  if (!append_bytes(material, used, kSystemRootName,
                    (std::size(kSystemRootName) - 1) * sizeof(wchar_t)) ||
      !append_wstring(material, used, system_root) ||
      !sha256_bytes(material.data(), used, digests.environment)) return false;

  used = 0;
  constexpr char kOperationDomain[] = "lae.windows-supervisor.launch-binding.v1";
  if (!append_bytes(material, used, kOperationDomain, sizeof(kOperationDomain) - 1) ||
      !append_u64(material, used, root_generation) ||
      !append_bytes(material, used, digests.request.data(), digests.request.size()) ||
      !append_bytes(material, used, digests.executable.data(), digests.executable.size()) ||
      !append_bytes(material, used, digests.token.data(), digests.token.size()) ||
      !append_bytes(material, used, digests.arguments.data(), digests.arguments.size()) ||
      !append_bytes(material, used, digests.limits.data(), digests.limits.size()) ||
      !append_bytes(material, used, digests.environment.data(), digests.environment.size()) ||
      !sha256_bytes(material.data(), used, digests.operation)) return false;
  SecureZeroMemory(material.data(), material.size());
  return true;
}

bool same_identity_handle(HANDLE retained, const FixedIdentity& expected) noexcept {
  FixedIdentity observed{};
  return identity_shape(retained, observed) &&
      executable_identity_bound(expected, observed);
}

[[noreturn]] void launch_cleanup_fail_stop() noexcept;

bool settle_started_overlapped_read(HANDLE file, OVERLAPPED& operation,
                                    DWORD& transferred,
                                    std::uint64_t cleanup_deadline_at_ms,
                                    bool request_cancellation) noexcept {
  if (request_cancellation &&
      !CancelIoEx(file, &operation) && GetLastError() != ERROR_NOT_FOUND) {
    // Cancellation refusal does not prove that the operation stopped. Continue
    // to wait for the exact event; failing to settle is a process fail-stop.
  }
  const std::uint64_t before_wait = GetTickCount64();
  if (before_wait >= cleanup_deadline_at_ms) launch_cleanup_fail_stop();
  DWORD wait_ms = static_cast<DWORD>(std::min<std::uint64_t>(
      kWorkerSettlementMs, cleanup_deadline_at_ms - before_wait));
  DWORD wait = WaitForSingleObject(operation.hEvent, wait_ms);
  if (wait != WAIT_OBJECT_0) {
    // A failed/timeout wait never proves settlement, even if cancellation was
    // already requested once. Reissue cancellation for the exact operation.
    (void)CancelIoEx(file, &operation);
    const std::uint64_t retry_at = GetTickCount64();
    if (retry_at >= cleanup_deadline_at_ms) launch_cleanup_fail_stop();
    wait_ms = static_cast<DWORD>(std::min<std::uint64_t>(
        kWorkerSettlementMs, cleanup_deadline_at_ms - retry_at));
    wait = WaitForSingleObject(operation.hEvent, wait_ms);
    if (wait != WAIT_OBJECT_0) launch_cleanup_fail_stop();
  }
  if (GetOverlappedResult(file, &operation, &transferred, FALSE)) {
    if (GetTickCount64() >= cleanup_deadline_at_ms) {
      transferred = 0;
      return false;
    }
    return true;
  }
  const DWORD error = GetLastError();
  if (error == ERROR_IO_INCOMPLETE) launch_cleanup_fail_stop();
  // ERROR_OPERATION_ABORTED and every other terminal I/O error prove that the
  // exact OVERLAPPED is no longer pending. They are operation failure, never
  // permission to use partial bytes.
  transferred = 0;
  return false;
}

bool overlapped_read(HANDLE file, OVERLAPPED& operation, void* buffer,
                     DWORD requested, DWORD& transferred, HANDLE cancellation,
                     const std::atomic<bool>& shutting_down,
                     std::uint64_t deadline_at_ms,
                     std::uint64_t cleanup_deadline_at_ms) noexcept {
  transferred = 0;
  if (!operation.hEvent || !ResetEvent(operation.hEvent)) return false;
  if (ReadFile(file, buffer, requested, nullptr, &operation)) {
    if (GetOverlappedResult(file, &operation, &transferred, FALSE)) return true;
    if (GetLastError() == ERROR_IO_INCOMPLETE)
      return settle_started_overlapped_read(
          file, operation, transferred, cleanup_deadline_at_ms, true);
    return false;
  }
  if (GetLastError() != ERROR_IO_PENDING) return false;
  for (;;) {
    const std::uint64_t now = GetTickCount64();
    if (now >= deadline_at_ms ||
        shutting_down.load(std::memory_order_acquire) ||
        (cancellation && WaitForSingleObject(cancellation, 0) == WAIT_OBJECT_0)) {
      (void)settle_started_overlapped_read(
          file, operation, transferred, cleanup_deadline_at_ms, true);
      return false;
    }
    const DWORD slice = static_cast<DWORD>(std::min<std::uint64_t>(
        kWaitSliceMs, deadline_at_ms - now));
    HANDLE waits[2] = {operation.hEvent, cancellation};
    const DWORD count = cancellation ? 2u : 1u;
    const DWORD result = WaitForMultipleObjects(count, waits, FALSE, slice);
    if (result == WAIT_OBJECT_0)
      return settle_started_overlapped_read(
          file, operation, transferred, cleanup_deadline_at_ms, false);
    if (cancellation && result == WAIT_OBJECT_0 + 1) {
      (void)settle_started_overlapped_read(
          file, operation, transferred, cleanup_deadline_at_ms, true);
      return false;
    }
    if (result != WAIT_TIMEOUT) {
      (void)settle_started_overlapped_read(
          file, operation, transferred, cleanup_deadline_at_ms, true);
      return false;
    }
  }
}

bool hash_retained_executable(HANDLE file, std::uint32_t exact_size,
                              HANDLE cancellation,
                              const std::atomic<bool>& shutting_down,
                              std::uint64_t deadline_at_ms,
                              std::uint64_t cleanup_deadline_at_ms,
                              std::array<std::byte, 32>& output) noexcept {
  if (!file || exact_size == 0 || exact_size > kMaxIdentityFileBytes) return false;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_bytes = 0;
  DWORD returned = 0;
  std::array<UCHAR, 4096> object{};
  UniqueHandle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
  if (!event || BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM,
                                             nullptr, 0) != 0 ||
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_bytes),
                        sizeof(object_bytes), &returned, 0) != 0 ||
      object_bytes == 0 || object_bytes > object.size() ||
      BCryptCreateHash(algorithm, &hash, object.data(), object_bytes,
                       nullptr, 0, 0) != 0) {
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    return false;
  }
  std::array<std::byte, 65536> chunk{};
  std::uint64_t offset = 0;
  bool ok = true;
  while (offset < exact_size) {
    OVERLAPPED operation{};
    operation.Offset = static_cast<DWORD>(offset & 0xffffffffu);
    operation.OffsetHigh = static_cast<DWORD>(offset >> 32u);
    operation.hEvent = event.get();
    const DWORD request = static_cast<DWORD>(std::min<std::uint64_t>(
        chunk.size(), exact_size - offset));
    DWORD read = 0;
    if (!overlapped_read(file, operation, chunk.data(), request, read,
                         cancellation, shutting_down, deadline_at_ms,
                         cleanup_deadline_at_ms) || read != request ||
        BCryptHashData(hash, reinterpret_cast<PUCHAR>(chunk.data()), read, 0) != 0) {
      ok = false;
      break;
    }
    offset += read;
  }
  if (ok)
    ok = BCryptFinishHash(hash, reinterpret_cast<PUCHAR>(output.data()),
                          static_cast<ULONG>(output.size()), 0) == 0;
  BCryptDestroyHash(hash);
  SecureZeroMemory(object.data(), object.size());
  SecureZeroMemory(chunk.data(), chunk.size());
  BCryptCloseAlgorithmProvider(algorithm, 0);
  return ok && offset == exact_size;
}

bool append_quoted_argument(std::wstring& command,
                            const std::wstring& argument) noexcept {
  if (!argument_shape(argument) || command.size() >= kLaunchMaxCommandCodeUnits)
    return false;
  try {
    if (!command.empty()) command.push_back(L' ');
    command.push_back(L'"');
    std::size_t backslashes = 0;
    for (const wchar_t value : argument) {
      if (value == L'\\') {
        ++backslashes;
      } else {
        if (value == L'"') command.append(backslashes * 2 + 1, L'\\');
        else command.append(backslashes, L'\\');
        backslashes = 0;
        command.push_back(value);
      }
      if (command.size() > kLaunchMaxCommandCodeUnits) return false;
    }
    command.append(backslashes * 2, L'\\');
    command.push_back(L'"');
  } catch (...) {
    return false;
  }
  return command.size() <= kLaunchMaxCommandCodeUnits;
}

bool build_command_line(const LaunchPlan& plan, std::wstring& command) noexcept {
  command.clear();
  if (!append_quoted_argument(command, plan.executable)) return false;
  for (const auto& argument : plan.arguments)
    if (!append_quoted_argument(command, argument)) return false;
  return command.size() < kLaunchMaxCommandCodeUnits;
}

bool build_fixed_environment(const std::wstring& system_root,
                             std::vector<wchar_t>& environment) noexcept {
  if (!absolute_local_path(system_root, false)) return false;
  constexpr wchar_t kName[] = L"SystemRoot=";
  const std::size_t required = std::size(kName) - 1 + system_root.size() + 2;
  if (required > kLaunchMaxPathCodeUnits + 32) return false;
  try {
    environment.assign(kName, kName + std::size(kName) - 1);
    environment.insert(environment.end(), system_root.begin(), system_root.end());
    environment.push_back(L'\0');
    environment.push_back(L'\0');
  } catch (...) {
    return false;
  }
  return true;
}

bool create_restricted_low_token(UniqueHandle& token,
                                 std::array<std::byte, 32>& policy_digest) noexcept {
  HANDLE current_raw = nullptr;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY |
                        TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT,
                        &current_raw)) return false;
  UniqueHandle current(current_raw);
  HANDLE restricted_raw = nullptr;
  if (!CreateRestrictedToken(current.get(), DISABLE_MAX_PRIVILEGE, 0, nullptr,
                             0, nullptr, 0, nullptr, &restricted_raw)) return false;
  UniqueHandle restricted(restricted_raw);
  SID_IDENTIFIER_AUTHORITY mandatory = SECURITY_MANDATORY_LABEL_AUTHORITY;
  PSID low_sid = nullptr;
  if (!AllocateAndInitializeSid(&mandatory, 1, SECURITY_MANDATORY_LOW_RID,
                                0, 0, 0, 0, 0, 0, 0, &low_sid)) return false;
  TOKEN_MANDATORY_LABEL label{};
  label.Label.Attributes = SE_GROUP_INTEGRITY;
  label.Label.Sid = low_sid;
  const DWORD label_size = sizeof(label) + GetLengthSid(low_sid);
  const bool set = SetTokenInformation(restricted.get(), TokenIntegrityLevel,
                                       &label, label_size) != FALSE;
  FreeSid(low_sid);
  constexpr char kPolicy[] =
      "lae.windows-supervisor.token.v1:disable-max-privilege;low-integrity";
  if (!set || !sha256_bytes(kPolicy, sizeof(kPolicy) - 1, policy_digest))
    return false;
  token = std::move(restricted);
  return true;
}

class StartupAttributeOwner final {
 public:
  static std::unique_ptr<StartupAttributeOwner> create(
      HANDLE child_stdin, HANDLE child_stdout, HANDLE child_stderr,
      HANDLE root_job) noexcept {
    if (!child_stdin || !child_stdout || !child_stderr || !root_job)
      return nullptr;
    std::unique_ptr<StartupAttributeOwner> owner(
        new (std::nothrow) StartupAttributeOwner(
            child_stdin, child_stdout, child_stderr, root_job));
    if (!owner || !owner->initialize()) return nullptr;
    return owner;
  }

  ~StartupAttributeOwner() {
    if (attributes_) DeleteProcThreadAttributeList(attributes_);
  }
  StartupAttributeOwner(const StartupAttributeOwner&) = delete;
  StartupAttributeOwner& operator=(const StartupAttributeOwner&) = delete;

  PPROC_THREAD_ATTRIBUTE_LIST get() const noexcept { return attributes_; }

  bool payloads_match(HANDLE child_stdin, HANDLE child_stdout,
                      HANDLE child_stderr, HANDLE root_job) const noexcept {
    return attributes_ && inherited_handles_[0] == child_stdin &&
        inherited_handles_[1] == child_stdout &&
        inherited_handles_[2] == child_stderr &&
        job_handles_[0] == root_job;
  }

 private:
  StartupAttributeOwner(HANDLE child_stdin, HANDLE child_stdout,
                        HANDLE child_stderr, HANDLE root_job) noexcept
      : inherited_handles_{child_stdin, child_stdout, child_stderr},
        job_handles_{root_job} {}

  bool initialize() noexcept {
    SIZE_T bytes = 0;
    InitializeProcThreadAttributeList(nullptr, 2, 0, &bytes);
    if (bytes == 0 || bytes > 65536) return false;
    try { attribute_storage_.resize(bytes); }
    catch (...) { return false; }
    attributes_ = reinterpret_cast<PPROC_THREAD_ATTRIBUTE_LIST>(
        attribute_storage_.data());
    if (!InitializeProcThreadAttributeList(attributes_, 2, 0, &bytes)) {
      attributes_ = nullptr;
      return false;
    }
    return UpdateProcThreadAttribute(
               attributes_, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
               const_cast<HANDLE*>(inherited_handles_.data()),
               sizeof(inherited_handles_), nullptr, nullptr) != FALSE &&
        UpdateProcThreadAttribute(
               attributes_, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
               const_cast<HANDLE*>(job_handles_.data()),
               sizeof(job_handles_), nullptr, nullptr) != FALSE;
  }

  const std::array<HANDLE, 3> inherited_handles_;
  const std::array<HANDLE, 1> job_handles_;
  std::vector<std::byte> attribute_storage_;
  PPROC_THREAD_ATTRIBUTE_LIST attributes_ = nullptr;
};

struct PreparedLaunch final {
  SupervisorState* supervisor = nullptr;
  ProcessLaunchAuthority* authority = nullptr;
  const LaunchPlan* plan = nullptr;
  const IssuedCapability* capability = nullptr;
  UniqueHandle executable;
  UniqueHandle token;
  UniqueHandle child_stdout_write;
  UniqueHandle child_stderr_write;
  UniqueHandle child_stdin_read;
  UniqueHandle parent_stdin_write;
  Child child;
  std::wstring command_line;
  std::vector<wchar_t> environment;
  std::unique_ptr<StartupAttributeOwner> startup_attributes;
  LaunchReceipt receipt{};
};

bool make_pipe_pair(UniqueHandle& parent_read, UniqueHandle& child_write) noexcept {
  SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
  HANDLE read_raw = nullptr;
  HANDLE write_raw = nullptr;
  if (!CreatePipe(&read_raw, &write_raw, &security, 0)) return false;
  UniqueHandle read(read_raw);
  UniqueHandle write(write_raw);
  if (!SetHandleInformation(read.get(), HANDLE_FLAG_INHERIT, 0)) return false;
  parent_read = std::move(read);
  child_write = std::move(write);
  return true;
}

bool make_stdin_pair(UniqueHandle& child_read, UniqueHandle& parent_write) noexcept {
  SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
  HANDLE read_raw = nullptr;
  HANDLE write_raw = nullptr;
  if (!CreatePipe(&read_raw, &write_raw, &security, 0)) return false;
  UniqueHandle read(read_raw);
  UniqueHandle write(write_raw);
  if (!SetHandleInformation(write.get(), HANDLE_FLAG_INHERIT, 0)) return false;
  child_read = std::move(read);
  parent_write = std::move(write);
  return true;
}

bool prepare_startup_attributes(PreparedLaunch& context) noexcept {
  context.startup_attributes = StartupAttributeOwner::create(
      context.child_stdin_read.get(), context.child_stdout_write.get(),
      context.child_stderr_write.get(), context.supervisor->root_job.get());
  return context.startup_attributes != nullptr;
}

bool start_drain_workers(Child& child, const LaunchLimits& limits) noexcept {
  if (!child.stdout_context.allocate() || !child.stderr_context.allocate()) {
    child.stdout_context.discard_before_worker_start();
    child.stderr_context.discard_before_worker_start();
    return false;
  }
  child.stdout_context->source = child.stdout_read.get();
  child.stderr_context->source = child.stderr_read.get();
  child.stdout_context->limit = limits.stdout_limit;
  child.stderr_context->limit = limits.stderr_limit;
  child.stdout_worker.reset(CreateThread(nullptr, 0, drain_child_pipe,
                                         child.stdout_context.get(), 0, nullptr));
  if (!child.stdout_worker) {
    child.stdout_context.discard_before_worker_start();
    child.stderr_context.discard_before_worker_start();
    return false;
  }
  child.stderr_worker.reset(CreateThread(nullptr, 0, drain_child_pipe,
                                         child.stderr_context.get(), 0, nullptr));
  if (!child.stderr_worker) {
    CancelSynchronousIo(child.stdout_worker.get());
    if (!settle_drain_worker(child.stdout_worker, child.stdout_context.get(),
                             limits.cleanup_deadline_at_ms)) std::terminate();
    child.stdout_worker.reset();
    child.stdout_context.release_after_join();
    child.stderr_context.discard_before_worker_start();
    return false;
  }
  return true;
}

struct MonitorContext final {
  const LaunchPlan* plan = nullptr;
  const IssuedCapability* capability = nullptr;
  ProcessLaunchAuthority* authority = nullptr;
  HANDLE cancellation = nullptr;
  LaunchReceipt* receipt = nullptr;
};

bool monitor_registered_child(Child& child, HANDLE root_job,
                              void* opaque) noexcept {
  auto* context = static_cast<MonitorContext*>(opaque);
  if (!context || !context->plan || !context->capability ||
      !context->authority || !context->receipt || !child.primary_thread ||
      !ChildRegistry::verify_membership(child.process.get(), root_job)) return false;
  const LaunchPlan& plan = *context->plan;
  const std::uint64_t before_resume = GetTickCount64();
  if (before_resume >= plan.limits.deadline_at_ms ||
      before_resume >= context->capability->expires_at_ms ||
      context->authority->shutting_down.load(std::memory_order_acquire) ||
      (context->cancellation &&
       WaitForSingleObject(context->cancellation, 0) == WAIT_OBJECT_0) ||
      ResumeThread(child.primary_thread.get()) != 1) return false;
  child.resumed = true;

  bool saw_exit = false;
  while (GetTickCount64() < plan.limits.deadline_at_ms) {
    if (context->authority->shutting_down.load(std::memory_order_acquire)) {
      context->receipt->status = LaunchResultCode::kCancelled;
      return false;
    }
    if (child.stdout_context->overflow.load(std::memory_order_acquire) ||
        child.stderr_context->overflow.load(std::memory_order_acquire)) {
      context->receipt->status = LaunchResultCode::kOutputLimit;
      return false;
    }
    HANDLE waits[2] = {child.process.get(), context->cancellation};
    const DWORD count = context->cancellation ? 2u : 1u;
    const std::uint64_t before_wait = GetTickCount64();
    if (before_wait >= plan.limits.deadline_at_ms) break;
    const DWORD wait_ms = static_cast<DWORD>(std::min<std::uint64_t>(
        kWaitSliceMs, plan.limits.deadline_at_ms - before_wait));
    const DWORD wait = WaitForMultipleObjects(count, waits, FALSE, wait_ms);
    if (wait == WAIT_OBJECT_0) {
      saw_exit = true;
      break;
    }
    if (context->cancellation && wait == WAIT_OBJECT_0 + 1) {
      context->receipt->status = LaunchResultCode::kCancelled;
      return false;
    }
    if (wait != WAIT_TIMEOUT) return false;
  }
  if (!saw_exit) {
    context->receipt->status = LaunchResultCode::kDeadline;
    return false;
  }
  while (!job_empty(root_job)) {
    const std::uint64_t now = GetTickCount64();
    if (now >= plan.limits.deadline_at_ms ||
        context->authority->shutting_down.load(std::memory_order_acquire))
      break;
    Sleep(static_cast<DWORD>(std::min<std::uint64_t>(
        kWaitSliceMs, plan.limits.deadline_at_ms - now)));
  }
  if (!job_empty(root_job)) {
    context->receipt->status = LaunchResultCode::kDeadline;
    return false;
  }
  if (context->authority->shutting_down.load(std::memory_order_acquire)) {
    context->receipt->status = LaunchResultCode::kCancelled;
    return false;
  }
  if (GetTickCount64() >= plan.limits.deadline_at_ms) {
    context->receipt->status = LaunchResultCode::kDeadline;
    return false;
  }
  DWORD exit_code = 0;
  if (!GetExitCodeProcess(child.process.get(), &exit_code) ||
      exit_code == STILL_ACTIVE) return false;
  if (GetTickCount64() >= plan.limits.deadline_at_ms) {
    context->receipt->status = LaunchResultCode::kDeadline;
    return false;
  }
  if (context->authority->shutting_down.load(std::memory_order_acquire)) {
    context->receipt->status = LaunchResultCode::kCancelled;
    return false;
  }
  context->receipt->exit_code = exit_code;
  context->receipt->status = exit_code == 0
      ? LaunchResultCode::kOk : LaunchResultCode::kExitFailure;
  return true;
}

[[noreturn]] void launch_cleanup_fail_stop() noexcept {
  // Losing the last owner of pending I/O, a live worker, or a child is worse
  // than terminating the supervisor. Process termination closes the retained
  // kill-on-close root Job. If a durable start exists, startup recovery keeps
  // it unknown/manual; pre-dispatch I/O never fabricates a journal record.
  std::terminate();
}

bool terminate_unregistered(PreparedLaunch& context) noexcept {
  if (!context.supervisor || !context.authority ||
      !context.supervisor->root_job) launch_cleanup_fail_stop();
  // This is safe even if a failed ownership transfer left no primary handle:
  // creation-time JOB_LIST membership makes the retained root Job the
  // authoritative containment boundary. Never infer "nothing to reap" from a
  // moved-from local Child.
  context.authority->poisoned = true;
  if (!TerminateJobObject(context.supervisor->root_job.get(), 1))
    launch_cleanup_fail_stop();
  const bool primary_reaped = !context.child.process ||
      wait_reaped_until(context.child.process.get(),
                        context.plan->limits.cleanup_deadline_at_ms, nullptr);
  if (!primary_reaped ||
      !wait_job_empty_until(context.supervisor->root_job.get(),
                            context.plan->limits.cleanup_deadline_at_ms))
    launch_cleanup_fail_stop();
  if ((context.child.stdout_worker || context.child.stderr_worker) &&
      !settle_child_drains(context.child,
                           context.plan->limits.cleanup_deadline_at_ms))
    launch_cleanup_fail_stop();
  context.receipt.job_reaped = true;
  return true;
}

DispatchStatus create_monitor_reap_after_durable_start(
    const IssuedCapability& capability, void* opaque) noexcept {
  auto* context = static_cast<PreparedLaunch*>(opaque);
  if (!context || !context->supervisor || !context->authority ||
      !context->plan || context->capability != &capability) {
    return DispatchStatus::kPreDispatchFailure;
  }
  const LaunchPlan& plan = *context->plan;
  // The durable adapter append+readback has just completed. Only bounded
  // rechecks separate it from the first process mutation.
  if (!checkpoint(*context->supervisor, *context->authority, plan,
                  context->supervisor->cancellation.get()) ||
      GetTickCount64() >= capability.expires_at_ms ||
      !same_identity_handle(context->executable.get(),
                            plan.expected_executable)) {
    context->receipt.status = LaunchResultCode::kPreDispatchFailure;
    context->receipt.job_reaped = true;  // no CreateProcess call occurred
    return DispatchStatus::kPreDispatchFailure;
  }

  STARTUPINFOEXW startup{};
  startup.StartupInfo.cb = sizeof(startup);
  startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
  startup.StartupInfo.hStdInput = context->child_stdin_read.get();
  startup.StartupInfo.hStdOutput = context->child_stdout_write.get();
  startup.StartupInfo.hStdError = context->child_stderr_write.get();
  if (!context->startup_attributes ||
      !context->startup_attributes->payloads_match(
          context->child_stdin_read.get(), context->child_stdout_write.get(),
          context->child_stderr_write.get(),
          context->supervisor->root_job.get())) {
    context->receipt.status = LaunchResultCode::kPreDispatchFailure;
    context->receipt.job_reaped = true;
    return DispatchStatus::kPreDispatchFailure;
  }
  startup.lpAttributeList = context->startup_attributes->get();
  PROCESS_INFORMATION created{};
  context->receipt.mutation_attempted = true;
  const DWORD flags = CREATE_SUSPENDED | CREATE_NO_WINDOW |
      CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT;
  const BOOL launched = CreateProcessAsUserW(
      context->token.get(), plan.executable.c_str(),
      context->command_line.data(), nullptr, nullptr, TRUE, flags,
      context->environment.data(), plan.working_directory.c_str(),
      &startup.StartupInfo, &created);
  if (!launched) return DispatchStatus::kDispatchedUnknown;

  context->child.process.reset(created.hProcess);
  context->child.primary_thread.reset(created.hThread);
  context->child_stdout_write.reset();
  context->child_stderr_write.reset();
  context->child_stdin_read.reset();
  context->parent_stdin_write.reset();
  context->child.stable_id = capability.operation_id;
  if (!ChildRegistry::verify_membership(context->child.process.get(),
                                        context->supervisor->root_job.get()) ||
      !same_identity_handle(context->executable.get(), plan.expected_executable) ||
      !start_drain_workers(context->child, plan.limits)) {
    terminate_unregistered(*context);
    return DispatchStatus::kTerminalFailure;
  }
  if (!context->supervisor->children.insert(
          context->child, context->supervisor->root_job.get())) {
    terminate_unregistered(*context);
    return DispatchStatus::kTerminalFailure;
  }

  // Nothing can execute until the registered child is rechecked here.
  if (!checkpoint(*context->supervisor, *context->authority, plan,
                  context->supervisor->cancellation.get()) ||
      GetTickCount64() >= capability.expires_at_ms ||
      !same_identity_handle(context->executable.get(), plan.expected_executable)) {
    const bool reaped = context->supervisor->children.terminate_and_reap(
        capability.operation_id, context->supervisor->root_job.get(),
        plan.limits.cleanup_deadline_at_ms, nullptr);
    context->authority->poisoned = true;
    context->receipt.job_reaped = reaped;
    if (!reaped) launch_cleanup_fail_stop();
    return DispatchStatus::kTerminalFailure;
  }

  MonitorContext monitor{&plan, &capability, context->authority,
                         context->supervisor->cancellation.get(),
                         &context->receipt};
  ChildRegistry::RegisteredOutcome child_outcome{};
  if (!context->supervisor->children.operate_and_unregister(
          capability.operation_id, context->supervisor->root_job.get(),
          monitor_registered_child, &monitor, plan.limits.deadline_at_ms,
          child_outcome)) {
    const bool reaped = context->supervisor->children.terminate_and_reap(
        capability.operation_id, context->supervisor->root_job.get(),
        plan.limits.cleanup_deadline_at_ms, nullptr);
    context->authority->poisoned = true;
    context->receipt.job_reaped = reaped;
    if (!reaped) launch_cleanup_fail_stop();
    return DispatchStatus::kTerminalFailure;
  }
  context->receipt.job_reaped = true;
  context->receipt.stdout_bytes = child_outcome.stdout_bytes;
  context->receipt.stderr_bytes = child_outcome.stderr_bytes;
  if (child_outcome.output_overflow) {
    context->receipt.status = LaunchResultCode::kOutputLimit;
    return DispatchStatus::kTerminalFailure;
  }
  return context->receipt.status == LaunchResultCode::kOk
      ? DispatchStatus::kTerminalSuccess : DispatchStatus::kTerminalFailure;
}

[[maybe_unused]] LaunchReceipt supervisor_owned_launch_transaction(
    const LaunchPlan& plan, IssuedCapability& capability) noexcept {
  LaunchReceipt output{};
  // Immutable false gates and the module-owned durable adapter are checked
  // before path, token, pipe, process, randomness, or registry access.
  if (!process_launch_gates_open()) return output;
  SupervisorState& supervisor = process_state();
  ProcessLaunchAuthority& authority = process_launch_authority();
  std::lock_guard transaction_lock(authority.mutex);
  if (authority.active || authority.poisoned ||
      authority.shutting_down.load(std::memory_order_acquire) ||
      !authority.recovered || !authority.durable_adapter ||
      !authority.durable_adapter->authority_ready() ||
      !supervisor.root_job || !root_job_policy_proven(supervisor.root_job.get()) ||
      !job_empty(supervisor.root_job.get()))
    return output;
  authority.active = true;
  struct ActiveReset final {
    ProcessLaunchAuthority& authority;
    ~ActiveReset() { authority.active = false; }
  } active_reset{authority};

  wchar_t windows_directory[kLaunchMaxPathCodeUnits]{};
  const UINT windows_length = GetWindowsDirectoryW(
      windows_directory, static_cast<UINT>(std::size(windows_directory)));
  if (windows_length == 0 || windows_length >= std::size(windows_directory)) {
    output.status = LaunchResultCode::kInvalidRequest;
    return output;
  }
  const std::wstring system_root(windows_directory, windows_length);
  LaunchDigests digests{};
  if (!digest_plan(plan, authority.root_generation, system_root, digests)) {
    output.status = LaunchResultCode::kInvalidRequest;
    return output;
  }
  const std::uint64_t validated_at_ms = GetTickCount64();
  if (plan.limits.deadline_at_ms <= validated_at_ms ||
      plan.limits.deadline_at_ms - validated_at_ms > plan.limits.timeout_ms ||
      plan.limits.cleanup_deadline_at_ms <= plan.limits.deadline_at_ms ||
      plan.limits.cleanup_deadline_at_ms - plan.limits.deadline_at_ms >
          kMaxWaitMs ||
      plan.limits.cleanup_deadline_at_ms - validated_at_ms >
          kMaxCleanupHorizonMs ||
      capability.expires_at_ms < plan.limits.deadline_at_ms) {
    output.status = LaunchResultCode::kInvalidRequest;
    return output;
  }

  PreparedLaunch prepared{};
  prepared.supervisor = &supervisor;
  prepared.authority = &authority;
  prepared.plan = &plan;
  prepared.capability = &capability;
  prepared.executable.reset(CreateFileW(
      plan.executable.c_str(), GENERIC_READ | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
      FILE_SHARE_READ, nullptr, OPEN_EXISTING,
      FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_OVERLAPPED, nullptr));
  FixedIdentity observed{};
  std::array<std::byte, 32> content_sha{};
  if (!prepared.executable || !identity_shape(prepared.executable.get(), observed) ||
      !executable_identity_bound(plan.expected_executable, observed) ||
      !hash_retained_executable(prepared.executable.get(), observed.size,
                                supervisor.cancellation.get(),
                                authority.shutting_down,
                                plan.limits.deadline_at_ms,
                                plan.limits.cleanup_deadline_at_ms,
                                content_sha) ||
      content_sha != plan.expected_content_sha256 ||
      !create_restricted_low_token(prepared.token, digests.token) ||
      digests.token != plan.expected_token_policy_digest ||
      !build_command_line(plan, prepared.command_line) ||
      !build_fixed_environment(system_root, prepared.environment) ||
      !make_pipe_pair(prepared.child.stdout_read,
                      prepared.child_stdout_write) ||
      !make_pipe_pair(prepared.child.stderr_read,
                      prepared.child_stderr_write) ||
      !make_stdin_pair(prepared.child_stdin_read,
                       prepared.parent_stdin_write) ||
      !prepare_startup_attributes(prepared)) {
    output.status = LaunchResultCode::kPreDispatchFailure;
    return output;
  }

  // Capability verification is over values recomputed by this module. The
  // private nonce replay set consumes authority before durable dispatch.
  if (!consume_capability(supervisor, capability, GetTickCount64(),
                          plan.session_id, kScopeProcessLaunch,
                          plan.operation_id, digests.operation,
                          digests.arguments, digests.executable)) {
    output.status = LaunchResultCode::kCapabilityRefused;
    return output;
  }
  if (!checkpoint(supervisor, authority, plan, supervisor.cancellation.get())) {
    output.status = LaunchResultCode::kPreDispatchFailure;
    return output;
  }

  const JournalOutcome journal = authority.journal.dispatch_owned(
      capability, *authority.durable_adapter,
      create_monitor_reap_after_durable_start, &prepared);
  output = prepared.receipt;
  output.journal_bound = journal.durable_state == JournalState::kTerminalDurable;
  switch (journal.status) {
    case DispatchStatus::kTerminalSuccess:
      if (!output.journal_bound || !output.job_reaped ||
          output.status != LaunchResultCode::kOk) {
        authority.poisoned = true;
        output.status = LaunchResultCode::kDispatchedUnknown;
        return output;
      }
      if (!authority.journal.begin_next_operation()) authority.poisoned = true;
      return output;
    case DispatchStatus::kTerminalFailure:
      if (!output.journal_bound || !output.job_reaped) {
        authority.poisoned = true;
        output.status = LaunchResultCode::kDispatchedUnknown;
        return output;
      }
      // The callback can discover failure after it had observed exit code 0
      // (for example, drain settlement misses the operation deadline). Never
      // leak that stale kOk, nor an untouched default, across a durable failure.
      if (!concrete_terminal_failure(output.status))
        output.status = LaunchResultCode::kTerminalFailure;
      if (!authority.journal.begin_next_operation()) authority.poisoned = true;
      return output;
    case DispatchStatus::kPreDispatchFailure:
      output.status = LaunchResultCode::kPreDispatchFailure;
      return output;
    case DispatchStatus::kDispatchedUnknown:
    case DispatchStatus::kPersistenceFailure:
      // Start/terminal append acknowledgement loss cannot prove whether a
      // durable start or effect exists. Keep actual reap evidence, but require
      // manual reconciliation and never replay.
      output.status = LaunchResultCode::kDispatchedUnknown;
      return output;
  }
  authority.poisoned = true;
  output.status = LaunchResultCode::kDispatchedUnknown;
  return output;
}
