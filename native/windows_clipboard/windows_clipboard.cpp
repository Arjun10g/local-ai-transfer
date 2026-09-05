#include "windows_clipboard.hpp"

#include "trust_anchor.hpp"

#include <bcrypt.h>

#include <algorithm>
#include <array>
#include <cstring>
#include <exception>
#include <limits>
#include <string_view>
#include <utility>
#include <vector>

#pragma comment(lib, "bcrypt.lib")

namespace lae::windows_clipboard {
namespace {

constexpr std::size_t kMaximumUtf16AllocationBytes =
    (kMaximumUtf8Bytes + 1) * sizeof(wchar_t);
constexpr DWORD kInitialOpenRetryMs = 5;
constexpr DWORD kMaximumOpenRetryMs = 80;
constexpr std::size_t kMaximumIdentifierBytes = 128;
constexpr DWORD kMaximumUserObjectTextBytes = 512;
constexpr DWORD kMaximumSecurityDescriptorBytes = 64 * 1024;

class UniqueHandle final {
 public:
  UniqueHandle() = default;
  explicit UniqueHandle(HANDLE value) noexcept : value_(value) {}
  UniqueHandle(const UniqueHandle&) = delete;
  UniqueHandle& operator=(const UniqueHandle&) = delete;
  UniqueHandle(UniqueHandle&& other) noexcept
      : value_(std::exchange(other.value_, nullptr)) {}
  ~UniqueHandle() {
    if (value_ && value_ != INVALID_HANDLE_VALUE) CloseHandle(value_);
  }
  HANDLE get() const noexcept { return value_; }

 private:
  HANDLE value_ = nullptr;
};

class UniqueDesktop final {
 public:
  UniqueDesktop() = default;
  explicit UniqueDesktop(HDESK value) noexcept : value_(value) {}
  UniqueDesktop(const UniqueDesktop&) = delete;
  UniqueDesktop& operator=(const UniqueDesktop&) = delete;
  UniqueDesktop(UniqueDesktop&& other) noexcept
      : value_(std::exchange(other.value_, nullptr)) {}
  UniqueDesktop& operator=(UniqueDesktop&& other) noexcept {
    if (this != &other) {
      if (value_) CloseDesktop(value_);
      value_ = std::exchange(other.value_, nullptr);
    }
    return *this;
  }
  ~UniqueDesktop() { if (value_) CloseDesktop(value_); }
  HDESK get() const noexcept { return value_; }

 private:
  HDESK value_ = nullptr;
};

class UniqueWindowStation final {
 public:
  UniqueWindowStation() = default;
  explicit UniqueWindowStation(HWINSTA value) noexcept : value_(value) {}
  UniqueWindowStation(const UniqueWindowStation&) = delete;
  UniqueWindowStation& operator=(const UniqueWindowStation&) = delete;
  UniqueWindowStation(UniqueWindowStation&& other) noexcept
      : value_(std::exchange(other.value_, nullptr)) {}
  UniqueWindowStation& operator=(UniqueWindowStation&& other) noexcept {
    if (this != &other) {
      if (value_) CloseWindowStation(value_);
      value_ = std::exchange(other.value_, nullptr);
    }
    return *this;
  }
  ~UniqueWindowStation() { if (value_) CloseWindowStation(value_); }
  HWINSTA get() const noexcept { return value_; }

 private:
  HWINSTA value_ = nullptr;
};

// Cleanup ambiguity can retain sensitive clipboard contents or leave the
// process owning the clipboard. Continuing would make later operations
// unsound, so cleanup failures terminate this inert future helper boundary.
[[noreturn]] void cleanup_fail_stop() noexcept {
  std::terminate();
}

class OwnerWindow final {
 public:
  OwnerWindow() noexcept {
    value_ = CreateWindowExW(0, L"STATIC", L"", 0, 0, 0, 0, 0,
                             HWND_MESSAGE, nullptr, GetModuleHandleW(nullptr),
                             nullptr);
  }
  OwnerWindow(const OwnerWindow&) = delete;
  OwnerWindow& operator=(const OwnerWindow&) = delete;
  ~OwnerWindow() { if (value_) DestroyWindow(value_); }
  HWND get() const noexcept { return value_; }

 private:
  HWND value_ = nullptr;
};

class GlobalMemory final {
 public:
  explicit GlobalMemory(SIZE_T bytes) noexcept
      : value_(GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, bytes)),
        bytes_(bytes) {}
  GlobalMemory(const GlobalMemory&) = delete;
  GlobalMemory& operator=(const GlobalMemory&) = delete;
  ~GlobalMemory() {
    if (value_ && !wipe_and_free()) cleanup_fail_stop();
  }
  HGLOBAL get() const noexcept { return value_; }
  HGLOBAL release_to_clipboard() noexcept {
    return std::exchange(value_, nullptr);
  }

  bool wipe_and_free() noexcept {
    if (!value_) return true;
    void* sensitive = GlobalLock(value_);
    if (!sensitive) return false;
    SecureZeroMemory(sensitive, bytes_);
    SetLastError(ERROR_SUCCESS);
    const bool unlocked = GlobalUnlock(value_) != FALSE ||
        GetLastError() == ERROR_SUCCESS;
    if (!unlocked) return false;
    if (GlobalFree(value_) != nullptr) return false;
    value_ = nullptr;
    return true;
  }

 private:
  HGLOBAL value_ = nullptr;
  SIZE_T bytes_ = 0;
};

class GlobalLockLease final {
 public:
  explicit GlobalLockLease(HGLOBAL value) noexcept
      : value_(value), data_(GlobalLock(value)) {}
  GlobalLockLease(const GlobalLockLease&) = delete;
  GlobalLockLease& operator=(const GlobalLockLease&) = delete;
  ~GlobalLockLease() {
    if (data_ && !unlock()) cleanup_fail_stop();
  }
  void* get() const noexcept { return data_; }
  bool unlock() noexcept {
    if (!data_) return true;
    SetLastError(ERROR_SUCCESS);
    const bool ok = GlobalUnlock(value_) != FALSE ||
        GetLastError() == ERROR_SUCCESS;
    if (ok) data_ = nullptr;
    return ok;
  }

 private:
  HGLOBAL value_ = nullptr;
  void* data_ = nullptr;
};

class ClipboardLease final {
 public:
  ClipboardLease() = default;
  ClipboardLease(const ClipboardLease&) = delete;
  ClipboardLease& operator=(const ClipboardLease&) = delete;
  ~ClipboardLease() {
    if (open_ && !close()) cleanup_fail_stop();
  }
  void mark_open() noexcept { open_ = true; }
  bool close() noexcept {
    if (!open_) return true;
    if (!CloseClipboard()) return false;
    open_ = false;
    return true;
  }
  void close_or_fail_stop() noexcept {
    if (!close()) cleanup_fail_stop();
  }

 private:
  bool open_ = false;
};

bool nonzero(const std::array<std::uint8_t, 32>& value) noexcept {
  std::uint8_t combined = 0;
  for (const auto byte : value) combined |= byte;
  return combined != 0;
}

bool identifier(std::string_view value) noexcept {
  if (value.empty() || value.size() > kMaximumIdentifierBytes) return false;
  for (const unsigned char character : value) {
    if (!((character >= 'a' && character <= 'z') ||
          (character >= 'A' && character <= 'Z') ||
          (character >= '0' && character <= '9') ||
          character == '_' || character == '-')) return false;
  }
  return true;
}

bool digest_equal(const std::array<std::uint8_t, 32>& left,
                  const std::array<std::uint8_t, 32>& right) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < left.size(); ++index)
    difference |= left[index] ^ right[index];
  return difference == 0;
}

bool sha256(std::string_view input,
            std::array<std::uint8_t, 32>& output) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD returned = 0;
  std::vector<std::uint8_t> object;
  bool ok = false;
  if (input.size() > static_cast<std::size_t>(
                         std::numeric_limits<ULONG>::max()) ||
      !BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
          &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(
          algorithm, BCRYPT_OBJECT_LENGTH,
          reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
          &returned, 0)) || object_size == 0 || object_size > 1'048'576)
    goto cleanup;
  object.resize(object_size);
  if (!BCRYPT_SUCCESS(BCryptCreateHash(
          algorithm, &hash, object.data(), object_size, nullptr, 0, 0)) ||
      !BCRYPT_SUCCESS(BCryptHashData(
          hash, reinterpret_cast<PUCHAR>(
                    const_cast<char*>(input.data())),
          static_cast<ULONG>(input.size()), 0)) ||
      !BCRYPT_SUCCESS(BCryptFinishHash(
          hash, output.data(), static_cast<ULONG>(output.size()), 0)))
    goto cleanup;
  ok = true;
cleanup:
  // CNG owns and may still access the caller-provided hash-object buffer until
  // BCryptDestroyHash returns. Destroy the handle before wiping/freeing it.
  if (hash) {
    if (!BCRYPT_SUCCESS(BCryptDestroyHash(hash))) cleanup_fail_stop();
    hash = nullptr;
  }
  if (!object.empty()) SecureZeroMemory(object.data(), object.size());
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  return ok;
}

bool hmac_sha256(const std::array<std::uint8_t, 32>& key,
                 std::string_view input,
                 std::array<std::uint8_t, 32>& output) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD returned = 0;
  std::vector<std::uint8_t> object;
  bool ok = false;
  if (input.size() > static_cast<std::size_t>(
                         std::numeric_limits<ULONG>::max()) ||
      !BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
          &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
          BCRYPT_ALG_HANDLE_HMAC_FLAG)) ||
      !BCRYPT_SUCCESS(BCryptGetProperty(
          algorithm, BCRYPT_OBJECT_LENGTH,
          reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
          &returned, 0)) || object_size == 0 || object_size > 1'048'576)
    goto cleanup;
  object.resize(object_size);
  if (!BCRYPT_SUCCESS(BCryptCreateHash(
          algorithm, &hash, object.data(), object_size,
          const_cast<PUCHAR>(key.data()), static_cast<ULONG>(key.size()), 0)) ||
      !BCRYPT_SUCCESS(BCryptHashData(
          hash, reinterpret_cast<PUCHAR>(
                    const_cast<char*>(input.data())),
          static_cast<ULONG>(input.size()), 0)) ||
      !BCRYPT_SUCCESS(BCryptFinishHash(
          hash, output.data(), static_cast<ULONG>(output.size()), 0)))
    goto cleanup;
  ok = true;
cleanup:
  // The HMAC hash-object buffer has the same lifetime requirement as the
  // ordinary SHA-256 buffer: destroy the CNG handle before zeroizing storage.
  if (hash) {
    if (!BCRYPT_SUCCESS(BCryptDestroyHash(hash))) cleanup_fail_stop();
    hash = nullptr;
  }
  if (!object.empty()) SecureZeroMemory(object.data(), object.size());
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  return ok;
}

void append_u32(std::string& value, std::uint32_t number) {
  for (unsigned shift = 0; shift != 32; shift += 8)
    value.push_back(static_cast<char>((number >> shift) & 0xff));
}

void append_u64(std::string& value, std::uint64_t number) {
  for (unsigned shift = 0; shift != 64; shift += 8)
    value.push_back(static_cast<char>((number >> shift) & 0xff));
}

bool append_sized(std::string& value, std::string_view field) {
  if (field.size() > kMaximumIdentifierBytes) return false;
  append_u32(value, static_cast<std::uint32_t>(field.size()));
  value.append(field.data(), field.size());
  return true;
}

bool request_digest(Operation operation,
                    const std::array<std::uint8_t, 32>& content_digest,
                    std::array<std::uint8_t, 32>& output) {
  constexpr char domain[] = "lae.windows-clipboard.request.v1\0";
  std::string frame(domain, sizeof(domain) - 1);
  frame.append(operation == Operation::kRead ? "clipboard.read" :
                                               "clipboard.write");
  frame.push_back('\0');
  frame.append(reinterpret_cast<const char*>(content_digest.data()),
               content_digest.size());
  return sha256(frame, output);
}

bool confirmation_digest(
    const BrokerClipboardCapability& capability,
    std::array<std::uint8_t, 32>& output) {
  constexpr char domain[] = "lae.windows-clipboard.confirmation.v1\0";
  std::string frame(domain, sizeof(domain) - 1);
  frame.append(capability.operation_id());
  frame.push_back('\0');
  frame.append(capability.session_id());
  frame.push_back('\0');
  frame.append(reinterpret_cast<const char*>(capability.request_digest().data()),
               capability.request_digest().size());
  frame.append("replace_all_formats=true\0", 25);
  append_u32(frame, capability.confirmed_sequence_number());
  return sha256(frame, output);
}

const char* operation_name(Operation operation) noexcept {
  switch (operation) {
    case Operation::kRead: return "clipboard.read";
    case Operation::kWrite: return "clipboard.write";
  }
  return "unknown";
}

const char* status_name(Status status) noexcept {
  switch (status) {
    case Status::kOk: return "ok";
    case Status::kPlatformUnavailable: return "platform_unavailable";
    case Status::kInvalidRequest: return "invalid_request";
    case Status::kCapabilityRefused: return "capability_refused";
    case Status::kSessionRefused: return "session_refused";
    case Status::kCancelled: return "cancelled";
    case Status::kDeadlineExceeded: return "deadline_exceeded";
    case Status::kClipboardBusy: return "clipboard_busy";
    case Status::kFormatUnavailable: return "format_unavailable";
    case Status::kInvalidText: return "invalid_text";
    case Status::kOutputLimit: return "output_limit";
    case Status::kJournalUnavailable: return "journal_unavailable";
    case Status::kAlreadyDispatched: return "already_dispatched";
    case Status::kMutationUnknown: return "mutation_unknown";
    case Status::kPostconditionPresentManual:
      return "postcondition_present_manual";
    case Status::kPostconditionAbsentManual:
      return "postcondition_absent_manual";
    case Status::kInternalFailure: return "internal_failure";
  }
  return "internal_failure";
}

MutationAttemptState mutation_state_for_lookup(
    JournalLookupState state) noexcept {
  switch (state) {
    case JournalLookupState::kNotDispatched:
    case JournalLookupState::kDispatched:
    case JournalLookupState::kMutationPrepared:
      return MutationAttemptState::kMayHaveBeenAttempted;
    case JournalLookupState::kFailedBeforeMutation:
      return MutationAttemptState::kNotAttempted;
    case JournalLookupState::kApplied:
    case JournalLookupState::kMutationAttemptFailed:
    case JournalLookupState::kUnknownAfterMutation:
      return MutationAttemptState::kAttempted;
  }
  return MutationAttemptState::kMayHaveBeenAttempted;
}

Result result(Status status, Operation operation) {
  Result output;
  output.status = status;
  output.receipt.operation = operation_name(operation);
  output.receipt.status = status_name(status);
  output.receipt.mutation_attempt_state = operation == Operation::kWrite
      ? MutationAttemptState::kMayHaveBeenAttempted
      : MutationAttemptState::kNotAttempted;
  return output;
}

Result activation_refusal() {
  Result output;
  output.status = Status::kPlatformUnavailable;
  output.receipt.operation = "unknown";
  output.receipt.status = status_name(output.status);
  return output;
}

bool user_object_text(HANDLE object, int index, std::wstring& output) {
  std::array<wchar_t, kMaximumUserObjectTextBytes / sizeof(wchar_t)> buffer{};
  DWORD needed = 0;
  if (!GetUserObjectInformationW(object, index, buffer.data(),
                                  static_cast<DWORD>(buffer.size() *
                                                     sizeof(wchar_t)),
                                  &needed) || needed < sizeof(wchar_t) ||
      needed > buffer.size() * sizeof(wchar_t) ||
      needed % sizeof(wchar_t) != 0)
    return false;
  const std::size_t units = needed / sizeof(wchar_t);
  if (buffer[units - 1] != L'\0') return false;
  output.assign(buffer.data(), units - 1);
  return !output.empty();
}

bool user_object_security_digest(
    HANDLE object, std::array<std::uint8_t, 32>& output) {
  SECURITY_INFORMATION requested = OWNER_SECURITY_INFORMATION |
      GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
  DWORD needed = 0;
  SetLastError(ERROR_SUCCESS);
  if (GetUserObjectSecurity(object, &requested, nullptr, 0, &needed) ||
      GetLastError() != ERROR_INSUFFICIENT_BUFFER ||
      needed < SECURITY_DESCRIPTOR_MIN_LENGTH ||
      needed > kMaximumSecurityDescriptorBytes)
    return false;
  std::vector<std::uint8_t> descriptor(needed);
  DWORD returned = 0;
  if (!GetUserObjectSecurity(
          object, &requested,
          reinterpret_cast<PSECURITY_DESCRIPTOR>(descriptor.data()),
          static_cast<DWORD>(descriptor.size()), &returned) ||
      returned != descriptor.size() ||
      !IsValidSecurityDescriptor(
          reinterpret_cast<PSECURITY_DESCRIPTOR>(descriptor.data())))
    return false;
  SECURITY_DESCRIPTOR_CONTROL control = 0;
  DWORD revision = 0;
  if (!GetSecurityDescriptorControl(
          reinterpret_cast<PSECURITY_DESCRIPTOR>(descriptor.data()),
          &control, &revision) || !(control & SE_SELF_RELATIVE))
    return false;
  const bool ok = sha256(
      std::string_view(reinterpret_cast<const char*>(descriptor.data()),
                       descriptor.size()), output);
  SecureZeroMemory(descriptor.data(), descriptor.size());
  return ok;
}

bool user_object_flags(HANDLE object, std::uint32_t& output) {
  USEROBJECTFLAGS flags{};
  DWORD returned = 0;
  if (!GetUserObjectInformationW(object, UOI_FLAGS, &flags, sizeof(flags),
                                  &returned) || returned != sizeof(flags))
    return false;
  output = flags.dwFlags;
  return flags.fReserved == FALSE;
}

bool desktop_is_input(HANDLE object) {
  BOOL input = FALSE;
  DWORD returned = 0;
  return GetUserObjectInformationW(object, UOI_IO, &input, sizeof(input),
                                   &returned) && returned == sizeof(input) &&
      input == TRUE;
}

bool equal_ordinal_insensitive(std::wstring_view left,
                               std::wstring_view right) noexcept {
  if (left.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      right.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  return CompareStringOrdinal(
             left.data(), static_cast<int>(left.size()),
             right.data(), static_cast<int>(right.size()), TRUE) == CSTR_EQUAL;
}

struct UserObjectIdentity final {
  std::wstring name;
  std::wstring type;
  std::array<std::uint8_t, 32> security_digest{};
  std::uint32_t object_flags = 0;
};

bool identity_equal(const UserObjectIdentity& left,
                    const UserObjectIdentity& right) noexcept {
  return equal_ordinal_insensitive(left.name, right.name) &&
      equal_ordinal_insensitive(left.type, right.type) &&
      left.object_flags == right.object_flags &&
      digest_equal(left.security_digest, right.security_digest);
}

bool capture_user_object_identity(HANDLE object, const wchar_t* expected_type,
                                  UserObjectIdentity& output) {
  return object && user_object_text(object, UOI_NAME, output.name) &&
      user_object_text(object, UOI_TYPE, output.type) &&
      equal_ordinal_insensitive(output.type, expected_type) &&
      user_object_flags(object, output.object_flags) &&
      user_object_security_digest(object, output.security_digest);
}

bool session_identity(std::uint32_t expected_session) {
  if (expected_session == 0 || expected_session == 0xffffffffu) return false;
  DWORD process_session = 0;
  if (!ProcessIdToSessionId(GetCurrentProcessId(), &process_session) ||
      process_session != expected_session ||
      WTSGetActiveConsoleSessionId() != expected_session)
    return false;
  HANDLE raw_token = nullptr;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw_token))
    return false;
  UniqueHandle token(raw_token);
  DWORD token_session = 0;
  DWORD returned = 0;
  if (!GetTokenInformation(token.get(), TokenSessionId, &token_session,
                           sizeof(token_session), &returned) ||
      returned != sizeof(token_session) || token_session != expected_session)
    return false;
  return true;
}

struct InteractiveIdentityLease final {
  std::uint32_t expected_session = 0;
  UniqueWindowStation retained_window_station;
  UniqueDesktop retained_input_desktop;
  UserObjectIdentity window_station_identity;
  UserObjectIdentity input_desktop_identity;
};

bool revalidate_interactive_identity(
    const InteractiveIdentityLease& identity) noexcept {
  try {
    if (!session_identity(identity.expected_session) ||
        !identity.retained_window_station.get() ||
        !identity.retained_input_desktop.get())
      return false;

    HWINSTA process_station = GetProcessWindowStation();
    HDESK thread_desktop = GetThreadDesktop(GetCurrentThreadId());
    UniqueWindowStation current_station(OpenWindowStationW(
        L"WinSta0", FALSE, WINSTA_READATTRIBUTES | READ_CONTROL));
    UniqueDesktop current_input(OpenInputDesktop(
        0, FALSE, DESKTOP_READOBJECTS | READ_CONTROL));
    if (!process_station || !thread_desktop || !current_station.get() ||
        !current_input.get())
      return false;

    UserObjectIdentity process_station_identity;
    UserObjectIdentity retained_station_identity;
    UserObjectIdentity current_station_identity;
    UserObjectIdentity thread_desktop_identity;
    UserObjectIdentity retained_desktop_identity;
    UserObjectIdentity current_desktop_identity;
    return capture_user_object_identity(
               process_station, L"WindowStation", process_station_identity) &&
        capture_user_object_identity(identity.retained_window_station.get(),
                                     L"WindowStation",
                                     retained_station_identity) &&
        capture_user_object_identity(current_station.get(), L"WindowStation",
                                     current_station_identity) &&
        capture_user_object_identity(thread_desktop, L"Desktop",
                                     thread_desktop_identity) &&
        capture_user_object_identity(identity.retained_input_desktop.get(),
                                     L"Desktop", retained_desktop_identity) &&
        capture_user_object_identity(current_input.get(), L"Desktop",
                                     current_desktop_identity) &&
        equal_ordinal_insensitive(process_station_identity.name, L"WinSta0") &&
        equal_ordinal_insensitive(thread_desktop_identity.name, L"Default") &&
        equal_ordinal_insensitive(retained_desktop_identity.name, L"Default") &&
        desktop_is_input(identity.retained_input_desktop.get()) &&
        desktop_is_input(current_input.get()) &&
        identity_equal(identity.window_station_identity,
                       process_station_identity) &&
        identity_equal(identity.window_station_identity,
                       retained_station_identity) &&
        identity_equal(identity.window_station_identity,
                       current_station_identity) &&
        identity_equal(identity.input_desktop_identity,
                       thread_desktop_identity) &&
        identity_equal(identity.input_desktop_identity,
                       retained_desktop_identity) &&
        identity_equal(identity.input_desktop_identity,
                       current_desktop_identity);
  } catch (...) {
    return false;
  }
}

bool acquire_interactive_identity(std::uint32_t expected_session,
                                  InteractiveIdentityLease& output) {
  if (!session_identity(expected_session)) return false;
  HWINSTA process_station = GetProcessWindowStation();
  HDESK thread_desktop = GetThreadDesktop(GetCurrentThreadId());
  UniqueWindowStation station(OpenWindowStationW(
      L"WinSta0", FALSE, WINSTA_READATTRIBUTES | READ_CONTROL));
  UniqueDesktop input(OpenInputDesktop(
      0, FALSE, DESKTOP_READOBJECTS | READ_CONTROL));
  if (!process_station || !thread_desktop || !station.get() || !input.get())
    return false;
  UserObjectIdentity process_station_identity;
  UserObjectIdentity retained_station_identity;
  UserObjectIdentity thread_desktop_identity;
  UserObjectIdentity retained_desktop_identity;
  if (!capture_user_object_identity(
          process_station, L"WindowStation", process_station_identity) ||
      !capture_user_object_identity(station.get(), L"WindowStation",
                                    retained_station_identity) ||
      !capture_user_object_identity(thread_desktop, L"Desktop",
                                    thread_desktop_identity) ||
      !capture_user_object_identity(input.get(), L"Desktop",
                                    retained_desktop_identity) ||
      !equal_ordinal_insensitive(process_station_identity.name, L"WinSta0") ||
      !equal_ordinal_insensitive(thread_desktop_identity.name, L"Default") ||
      !equal_ordinal_insensitive(retained_desktop_identity.name, L"Default") ||
      !desktop_is_input(input.get()) ||
      !identity_equal(process_station_identity, retained_station_identity) ||
      !identity_equal(thread_desktop_identity, retained_desktop_identity))
    return false;
  output.expected_session = expected_session;
  output.window_station_identity = std::move(retained_station_identity);
  output.input_desktop_identity = std::move(retained_desktop_identity);
  output.retained_window_station = std::move(station);
  output.retained_input_desktop = std::move(input);
  return revalidate_interactive_identity(output);
}

Status stop_status(const ExecutionContext& context,
                   std::uint64_t deadline) noexcept {
  if (!context.cancellation_event ||
      context.cancellation_event == INVALID_HANDLE_VALUE)
    return Status::kInvalidRequest;
  const DWORD state = WaitForSingleObject(context.cancellation_event, 0);
  if (state == WAIT_OBJECT_0) return Status::kCancelled;
  if (state != WAIT_TIMEOUT) return Status::kInvalidRequest;
  if (GetTickCount64() >= deadline) return Status::kDeadlineExceeded;
  return Status::kOk;
}

Status open_clipboard_bounded(HWND owner, const ExecutionContext& context,
                              const InteractiveIdentityLease& identity,
                              std::uint64_t deadline,
                              ClipboardLease& lease) noexcept {
  DWORD delay = kInitialOpenRetryMs;
  while (true) {
    const Status stop = stop_status(context, deadline);
    if (stop != Status::kOk) return stop;
    // The retained input-desktop handle spans the whole operation. Revalidate
    // the console/session and the current input desktop immediately around the
    // clipboard acquisition so a desktop switch cannot authorize access.
    if (!revalidate_interactive_identity(identity))
      return Status::kSessionRefused;
    if (OpenClipboard(owner)) {
      lease.mark_open();
      if (!revalidate_interactive_identity(identity)) {
        lease.close_or_fail_stop();
        return Status::kSessionRefused;
      }
      const Status acquired_stop = stop_status(context, deadline);
      if (acquired_stop != Status::kOk) {
        lease.close_or_fail_stop();
        return acquired_stop;
      }
      return Status::kOk;
    }
    const std::uint64_t now = GetTickCount64();
    if (now >= deadline) return Status::kDeadlineExceeded;
    const std::uint64_t remaining = deadline - now;
    const DWORD wait = static_cast<DWORD>(
        std::min<std::uint64_t>(remaining, delay));
    const DWORD waited = WaitForSingleObject(context.cancellation_event, wait);
    if (waited == WAIT_OBJECT_0) return Status::kCancelled;
    if (waited != WAIT_TIMEOUT) return Status::kInvalidRequest;
    delay = std::min<DWORD>(delay * 2, kMaximumOpenRetryMs);
  }
}

bool valid_utf16(const wchar_t* value, std::size_t units) noexcept {
  for (std::size_t index = 0; index < units; ++index) {
    const std::uint16_t unit = static_cast<std::uint16_t>(value[index]);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      if (++index == units) return false;
      const std::uint16_t low = static_cast<std::uint16_t>(value[index]);
      if (low < 0xdc00 || low > 0xdfff) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return false;
    }
  }
  return true;
}

Status validate_utf8_input(std::string_view input) noexcept {
  if (input.size() > kMaximumUtf8Bytes) return Status::kOutputLimit;
  if (input.find('\0') != std::string_view::npos ||
      input.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return Status::kInvalidText;
  if (input.empty()) return Status::kOk;
  const int units = MultiByteToWideChar(
      CP_UTF8, MB_ERR_INVALID_CHARS, input.data(),
      static_cast<int>(input.size()), nullptr, 0);
  return units > 0 && static_cast<std::size_t>(units) <= kMaximumUtf8Bytes
      ? Status::kOk
      : Status::kInvalidText;
}

bool utf8_to_utf16(std::string_view input, std::vector<wchar_t>& output) {
  output.clear();
  if (input.size() > kMaximumUtf8Bytes ||
      input.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      input.find('\0') != std::string_view::npos)
    return false;
  if (input.empty()) {
    output.push_back(L'\0');
    return true;
  }
  const int units = MultiByteToWideChar(
      CP_UTF8, MB_ERR_INVALID_CHARS, input.data(),
      static_cast<int>(input.size()), nullptr, 0);
  if (units <= 0 || static_cast<std::size_t>(units) > kMaximumUtf8Bytes)
    return false;
  output.resize(static_cast<std::size_t>(units) + 1, L'\0');
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, input.data(),
                          static_cast<int>(input.size()), output.data(), units) !=
      units || !valid_utf16(output.data(), static_cast<std::size_t>(units))) {
    SecureZeroMemory(output.data(), output.size() * sizeof(wchar_t));
    output.clear();
    return false;
  }
  return true;
}

bool utf16_to_utf8(const wchar_t* input, std::size_t units,
                   std::string& output) {
  output.clear();
  if (!valid_utf16(input, units) ||
      units > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  if (units == 0) return true;
  const int bytes = WideCharToMultiByte(
      CP_UTF8, WC_ERR_INVALID_CHARS, input, static_cast<int>(units), nullptr, 0,
      nullptr, nullptr);
  if (bytes <= 0 || static_cast<std::size_t>(bytes) > kMaximumUtf8Bytes)
    return false;
  output.resize(static_cast<std::size_t>(bytes));
  return WideCharToMultiByte(
             CP_UTF8, WC_ERR_INVALID_CHARS, input, static_cast<int>(units),
             output.data(), bytes, nullptr, nullptr) == bytes;
}

void zero_string(std::string& value) noexcept {
  if (!value.empty()) SecureZeroMemory(value.data(), value.size());
  value.clear();
}

struct Snapshot final {
  Snapshot() = default;
  Snapshot(const Snapshot&) = delete;
  Snapshot& operator=(const Snapshot&) = delete;
  ~Snapshot() { zero_string(text); }
  std::string text;
  std::uint32_t sequence = 0;
};

Status read_open_clipboard(Snapshot& output) {
  if (!IsClipboardFormatAvailable(CF_UNICODETEXT))
    return Status::kFormatUnavailable;
  HANDLE data = GetClipboardData(CF_UNICODETEXT);
  if (!data) return Status::kFormatUnavailable;
  HGLOBAL global = reinterpret_cast<HGLOBAL>(data);
  const SIZE_T bytes = GlobalSize(global);
  if (bytes < sizeof(wchar_t) || bytes % sizeof(wchar_t) != 0 ||
      bytes > kMaximumUtf16AllocationBytes)
    return Status::kOutputLimit;
  GlobalLockLease locked(global);
  const auto* text = static_cast<const wchar_t*>(locked.get());
  if (!text) return Status::kInvalidText;
  const std::size_t units = bytes / sizeof(wchar_t);
  std::size_t terminator = 0;
  while (terminator < units && text[terminator] != L'\0') ++terminator;
  bool converted = terminator < units && valid_utf16(text, terminator) &&
      utf16_to_utf8(text, terminator, output.text);
  const bool unlocked = locked.unlock();
  if (!unlocked) cleanup_fail_stop();
  if (!converted) {
    zero_string(output.text);
    return Status::kInvalidText;
  }
  output.sequence = GetClipboardSequenceNumber();
  if (output.sequence == 0) {
    zero_string(output.text);
    return Status::kMutationUnknown;
  }
  return Status::kOk;
}

Status validate_common(const BrokerClipboardCapability& capability,
                       const ExecutionContext& context,
                       Operation operation, std::uint64_t& deadline,
                       InteractiveIdentityLease& identity) {
  if (!context.authority || !capability.structurally_valid() ||
      capability.operation() != operation ||
      !context.authority->verify_capability(capability))
    return Status::kCapabilityRefused;
  deadline = capability.expires_monotonic_ms();
  const std::uint64_t now = GetTickCount64();
  if (now < capability.issued_monotonic_ms())
    return Status::kCapabilityRefused;
  const Status stop = stop_status(context, deadline);
  if (stop != Status::kOk) return stop;
  if (!acquire_interactive_identity(capability.interactive_session_id(),
                                    identity))
    return Status::kSessionRefused;
  return Status::kOk;
}

Result execute_read(const BrokerClipboardCapability& capability,
                    const ExecutionContext& context) {
  std::uint64_t deadline = 0;
  InteractiveIdentityLease identity;
  Status status = validate_common(capability, context, Operation::kRead,
                                  deadline, identity);
  if (status != Status::kOk) return result(status, Operation::kRead);
  std::array<std::uint8_t, 32> expected{};
  const std::array<std::uint8_t, 32> empty{};
  if (!request_digest(Operation::kRead, empty, expected) ||
      !digest_equal(expected, capability.request_digest()) ||
      nonzero(capability.content_digest()))
    return result(Status::kCapabilityRefused, Operation::kRead);

  ClipboardLease clipboard;
  status = open_clipboard_bounded(nullptr, context, identity, deadline,
                                  clipboard);
  if (status != Status::kOk) return result(status, Operation::kRead);
  const std::uint32_t sequence_before = GetClipboardSequenceNumber();
  if (sequence_before == 0)
    return result(Status::kMutationUnknown, Operation::kRead);
  Snapshot snapshot;
  status = read_open_clipboard(snapshot);
  if (status != Status::kOk) return result(status, Operation::kRead);
  if (snapshot.sequence != sequence_before) {
    clipboard.close_or_fail_stop();
    zero_string(snapshot.text);
    return result(Status::kMutationUnknown, Operation::kRead);
  }
  if (!revalidate_interactive_identity(identity)) {
    clipboard.close_or_fail_stop();
    zero_string(snapshot.text);
    return result(Status::kSessionRefused, Operation::kRead);
  }
  clipboard.close_or_fail_stop();
  if (!revalidate_interactive_identity(identity)) {
    zero_string(snapshot.text);
    return result(Status::kSessionRefused, Operation::kRead);
  }
  const std::uint32_t sequence_after = GetClipboardSequenceNumber();
  if (sequence_after != sequence_before) {
    zero_string(snapshot.text);
    return result(Status::kMutationUnknown, Operation::kRead);
  }
  status = stop_status(context, deadline);
  if (status != Status::kOk) {
    zero_string(snapshot.text);
    return result(status, Operation::kRead);
  }
  if (!revalidate_interactive_identity(identity)) {
    zero_string(snapshot.text);
    return result(Status::kSessionRefused, Operation::kRead);
  }
  Result output = result(Status::kOk, Operation::kRead);
  output.receipt.utf8_bytes = snapshot.text.size();
  output.receipt.sequence_before = sequence_before;
  output.receipt.sequence_after = sequence_after;
  output.text = std::move(snapshot.text);
  return output;
}

Result journaled_unknown(Operation operation, const Receipt& receipt) {
  Result output = result(Status::kMutationUnknown, operation);
  output.receipt = receipt;
  output.receipt.operation = operation_name(operation);
  output.receipt.status = status_name(output.status);
  return output;
}

Result journaled_result(Status status, Operation operation,
                        const Receipt& receipt) {
  Result output = result(status, operation);
  output.receipt = receipt;
  output.receipt.operation = operation_name(operation);
  output.receipt.status = status_name(status);
  return output;
}

bool confirm_durable_failed_before_mutation(
    JournalPort& journal,
    const BrokerClipboardCapability& capability,
    const InteractiveIdentityLease& identity,
    std::uint32_t sequence_before,
    bool mutation_prepared_durable) {
  JournalLookup verification;
  const bool lookup_ok = journal.lookup_write(
      capability.operation_id(), capability.request_digest(), verification);
  const bool identity_after_lookup =
      revalidate_interactive_identity(identity);
  return lookup_ok && identity_after_lookup &&
      verification.state == JournalLookupState::kFailedBeforeMutation &&
      verification.sequence_before == sequence_before &&
      verification.sequence_after == 0 &&
      verification.mutation_prepared_durable == mutation_prepared_durable;
}

Result execute_write(const BrokerClipboardCapability& capability,
                     const ExecutionContext& context,
                     const std::string& input, JournalPort& journal) {
  std::uint64_t deadline = 0;
  InteractiveIdentityLease identity;
  Status status = validate_common(capability, context, Operation::kWrite,
                                  deadline, identity);
  if (status != Status::kOk) return result(status, Operation::kWrite);
  if (!capability.replace_all_formats_confirmed())
    return result(Status::kCapabilityRefused, Operation::kWrite);

  // Validate the caller-controlled content before any digest is computed. A
  // capability cannot turn an oversized, NUL-containing, or ill-formed UTF-8
  // value into a hashable request.
  const Status input_status = validate_utf8_input(input);
  if (input_status != Status::kOk)
    return result(input_status, Operation::kWrite);

  std::array<std::uint8_t, 32> content_digest{};
  std::array<std::uint8_t, 32> expected_request{};
  std::array<std::uint8_t, 32> expected_confirmation{};
  if (!sha256(input, content_digest) ||
      !request_digest(Operation::kWrite, content_digest, expected_request) ||
      !confirmation_digest(capability, expected_confirmation) ||
      !digest_equal(content_digest, capability.content_digest()) ||
      !digest_equal(expected_request, capability.request_digest()) ||
      !digest_equal(expected_confirmation, capability.confirmation_digest()))
    return result(Status::kCapabilityRefused, Operation::kWrite);

  std::vector<wchar_t> wide;
  if (!utf8_to_utf16(input, wide))
    return result(input.size() > kMaximumUtf8Bytes ? Status::kOutputLimit :
                                                   Status::kInvalidText,
                  Operation::kWrite);
  const SIZE_T allocation_bytes = wide.size() * sizeof(wchar_t);
  if (allocation_bytes == 0 ||
      allocation_bytes > kMaximumUtf16AllocationBytes) {
    SecureZeroMemory(wide.data(), wide.size() * sizeof(wchar_t));
    return result(Status::kOutputLimit, Operation::kWrite);
  }
  GlobalMemory memory(allocation_bytes);
  if (!memory.get()) {
    SecureZeroMemory(wide.data(), wide.size() * sizeof(wchar_t));
    return result(Status::kInternalFailure, Operation::kWrite);
  }
  GlobalLockLease locked(memory.get());
  void* destination = locked.get();
  if (!destination) {
    SecureZeroMemory(wide.data(), wide.size() * sizeof(wchar_t));
    return result(Status::kInternalFailure, Operation::kWrite);
  }
  std::memcpy(destination, wide.data(), allocation_bytes);
  SecureZeroMemory(wide.data(), wide.size() * sizeof(wchar_t));
  wide.clear();
  if (!locked.unlock()) cleanup_fail_stop();

  OwnerWindow owner;
  if (!owner.get()) return result(Status::kSessionRefused, Operation::kWrite);
  DWORD window_process = 0;
  const DWORD window_thread = GetWindowThreadProcessId(
      owner.get(), &window_process);
  if (window_thread != GetCurrentThreadId() ||
      window_process != GetCurrentProcessId())
    return result(Status::kSessionRefused, Operation::kWrite);

  ClipboardLease clipboard;
  status = open_clipboard_bounded(owner.get(), context, identity, deadline,
                                  clipboard);
  if (status != Status::kOk) return result(status, Operation::kWrite);
  Receipt receipt;
  receipt.operation = operation_name(Operation::kWrite);
  receipt.status = status_name(Status::kInternalFailure);
  receipt.utf8_bytes = input.size();
  receipt.sequence_before = GetClipboardSequenceNumber();
  if (receipt.sequence_before == 0 ||
      receipt.sequence_before != capability.confirmed_sequence_number())
    return result(Status::kCapabilityRefused, Operation::kWrite);

  // This is the final cancellable/deadline point. The exact user confirmation
  // binds replacement of every existing format and this current sequence.
  // Once dispatch is durable no cancellation result may imply no mutation.
  status = stop_status(context, deadline);
  if (status != Status::kOk) return result(status, Operation::kWrite);
  // From immediately before the journal call onward, a lost acknowledgement
  // means dispatch may have become durable. Every subsequent return carries
  // this receipt and may downgrade it only after an exact durable negative
  // readback through confirm_durable_failed_before_mutation().
  receipt.mutation_attempt_state =
      MutationAttemptState::kMayHaveBeenAttempted;
  const JournalDispatch dispatch = journal.dispatch_write(
      capability.operation_id(), capability.request_digest(), content_digest,
      receipt.sequence_before);
  const bool identity_after_dispatch =
      revalidate_interactive_identity(identity);
  if (!identity_after_dispatch) {
    clipboard.close_or_fail_stop();
    if (dispatch == JournalDispatch::kDispatched ||
        dispatch == JournalDispatch::kAlreadyDispatched) {
      receipt.journal_dispatch_durable = true;
    }
    return journaled_unknown(Operation::kWrite, receipt);
  }
  if (dispatch == JournalDispatch::kAlreadyDispatched) {
    receipt.journal_dispatch_durable = true;
    return journaled_result(Status::kAlreadyDispatched, Operation::kWrite,
                            receipt);
  }
  if (dispatch != JournalDispatch::kDispatched) {
    clipboard.close_or_fail_stop();
    const bool exact_failed_before = confirm_durable_failed_before_mutation(
        journal, capability, identity, receipt.sequence_before, false);
    if (!exact_failed_before)
      return journaled_unknown(Operation::kWrite, receipt);
    receipt.journal_outcome_durable = true;
    receipt.mutation_attempt_state = MutationAttemptState::kNotAttempted;
    return journaled_result(Status::kJournalUnavailable, Operation::kWrite,
                            receipt);
  }
  receipt.journal_dispatch_durable = true;
  // Dispatch without a durable terminal record is conservatively unknown on
  // restart. Never publish a false "not attempted" assertion for it.
  receipt.mutation_attempt_state =
      MutationAttemptState::kMayHaveBeenAttempted;

  // A desktop switch after OpenClipboard but before mutation is definitive:
  // nothing was changed, but the durable dispatch must be closed before a
  // caller may resolve it. This security revalidation is mandatory even though
  // cancellation no longer applies after dispatch.
  if (!revalidate_interactive_identity(identity)) {
    clipboard.close_or_fail_stop();
    const bool saved = journal.record_write_outcome(
        capability.operation_id(), JournalOutcome::kFailedBeforeMutation,
        receipt.sequence_before, 0);
    const bool identity_after_journal =
        revalidate_interactive_identity(identity);
    receipt.journal_outcome_durable = saved;
    if (!saved || !identity_after_journal)
      return journaled_unknown(Operation::kWrite, receipt);
    const bool exact_failed_before = confirm_durable_failed_before_mutation(
        journal, capability, identity, receipt.sequence_before, false);
    if (!exact_failed_before) {
      receipt.journal_outcome_durable = false;
      return journaled_unknown(Operation::kWrite, receipt);
    }
    receipt.mutation_attempt_state = MutationAttemptState::kNotAttempted;
    return journaled_result(Status::kSessionRefused, Operation::kWrite,
                            receipt);
  }

  // Persist the last pre-mutation boundary. Failure refuses the Win32 call;
  // recovery still reports dispatch-only as may-have-been-attempted rather
  // than inventing a definitive negative.
  const bool mutation_prepared = journal.record_mutation_prepared(
      capability.operation_id(), receipt.sequence_before);
  const bool identity_after_mutation_prepare =
      revalidate_interactive_identity(identity);
  receipt.journal_mutation_prepared_durable = mutation_prepared;
  if (!mutation_prepared) {
    clipboard.close_or_fail_stop();
    return journaled_unknown(Operation::kWrite, receipt);
  }
  if (!identity_after_mutation_prepare) {
    clipboard.close_or_fail_stop();
    const bool saved = journal.record_write_outcome(
        capability.operation_id(), JournalOutcome::kFailedBeforeMutation,
        receipt.sequence_before, 0);
    const bool identity_after_journal =
        revalidate_interactive_identity(identity);
    receipt.journal_outcome_durable = saved;
    if (!saved || !identity_after_journal)
      return journaled_unknown(Operation::kWrite, receipt);
    const bool exact_failed_before = confirm_durable_failed_before_mutation(
        journal, capability, identity, receipt.sequence_before, true);
    if (!exact_failed_before) {
      receipt.journal_outcome_durable = false;
      return journaled_unknown(Operation::kWrite, receipt);
    }
    receipt.mutation_attempt_state = MutationAttemptState::kNotAttempted;
    return journaled_result(Status::kSessionRefused, Operation::kWrite,
                            receipt);
  }

  const BOOL emptied = EmptyClipboard();
  // This live invocation returned, so the result can state "attempted". If
  // the process dies inside the call, recovery sees only kMutationPrepared
  // and reports may-have-been-attempted.
  receipt.mutation_attempt_state = MutationAttemptState::kAttempted;
  if (!emptied) {
    clipboard.close_or_fail_stop();
    const bool saved = journal.record_write_outcome(
        capability.operation_id(),
        JournalOutcome::kMutationAttemptFailed,
        receipt.sequence_before, 0);
    const bool identity_after_journal =
        revalidate_interactive_identity(identity);
    receipt.journal_outcome_durable = saved;
    if (!saved || !identity_after_journal)
      return journaled_unknown(Operation::kWrite, receipt);
    return journaled_result(Status::kClipboardBusy, Operation::kWrite,
                            receipt);
  }
  if (!revalidate_interactive_identity(identity) ||
      GetClipboardOwner() != owner.get()) {
    clipboard.close_or_fail_stop();
    const bool saved = journal.record_write_outcome(
        capability.operation_id(), JournalOutcome::kFailedAfterMutation,
        receipt.sequence_before, 0);
    const bool identity_after_journal =
        revalidate_interactive_identity(identity);
    receipt.journal_outcome_durable = saved;
    (void)identity_after_journal;
    return journaled_unknown(Operation::kWrite, receipt);
  }
  if (!SetClipboardData(CF_UNICODETEXT, memory.get())) {
    clipboard.close_or_fail_stop();
    const bool saved = journal.record_write_outcome(
        capability.operation_id(), JournalOutcome::kFailedAfterMutation,
        receipt.sequence_before, 0);
    const bool identity_after_journal =
        revalidate_interactive_identity(identity);
    receipt.journal_outcome_durable = saved;
    (void)identity_after_journal;
    return journaled_unknown(Operation::kWrite, receipt);
  }
  memory.release_to_clipboard();  // The system owns and frees it after success.
  receipt.sequence_after = GetClipboardSequenceNumber();
  const bool identity_before_close = revalidate_interactive_identity(identity);
  clipboard.close_or_fail_stop();
  const bool identity_after_close = revalidate_interactive_identity(identity);
  const std::uint32_t closed_sequence = GetClipboardSequenceNumber();
  const bool exact_sequence = identity_before_close && identity_after_close &&
      receipt.sequence_after != 0 &&
      receipt.sequence_after != receipt.sequence_before &&
      closed_sequence == receipt.sequence_after;
  const JournalOutcome outcome = exact_sequence ? JournalOutcome::kApplied :
                                                  JournalOutcome::kUnknownAfterMutation;
  const bool saved = journal.record_write_outcome(
      capability.operation_id(), outcome, receipt.sequence_before,
      receipt.sequence_after);
  const bool identity_after_journal =
      revalidate_interactive_identity(identity);
  receipt.journal_outcome_durable = saved;
  if (!exact_sequence || !saved || !identity_after_journal)
    return journaled_unknown(Operation::kWrite, receipt);
  return journaled_result(Status::kOk, Operation::kWrite, receipt);
}

}  // namespace

BrokerClipboardAuthority::~BrokerClipboardAuthority() {
  SecureZeroMemory(mac_key_.data(), mac_key_.size());
  SecureZeroMemory(authority_nonce_.data(), authority_nonce_.size());
}

bool BrokerClipboardAuthority::verify_capability(
    const BrokerClipboardCapability& capability) const noexcept {
  if (!capability.structurally_valid() || !nonzero(mac_key_) ||
      !nonzero(authority_nonce_) ||
      !digest_equal(authority_nonce_, capability.authority_nonce_))
    return false;
  try {
    constexpr char domain[] = "lae.windows-clipboard.capability-mac.v1\0";
    std::string frame(domain, sizeof(domain) - 1);
    frame.append(reinterpret_cast<const char*>(
                     capability.authority_nonce_.data()),
                 capability.authority_nonce_.size());
    frame.append(reinterpret_cast<const char*>(
                     capability.confirmation_digest_.data()),
                 capability.confirmation_digest_.size());
    append_u32(frame, capability.confirmed_sequence_number_);
    frame.append(reinterpret_cast<const char*>(capability.content_digest_.data()),
                 capability.content_digest_.size());
    append_u64(frame, capability.expires_monotonic_ms_);
    append_u32(frame, capability.interactive_session_id_);
    append_u64(frame, capability.issued_monotonic_ms_);
    frame.push_back(static_cast<char>(capability.operation_));
    if (!append_sized(frame, capability.operation_id_)) return false;
    frame.push_back(capability.replace_all_formats_confirmed_ ? '\x01' : '\x00');
    frame.append(reinterpret_cast<const char*>(capability.request_digest_.data()),
                 capability.request_digest_.size());
    if (!append_sized(frame, capability.session_id_)) return false;

    std::array<std::uint8_t, 32> expected{};
    const bool verified = hmac_sha256(mac_key_, frame, expected) &&
        digest_equal(expected, capability.mac_);
    SecureZeroMemory(expected.data(), expected.size());
    return verified;
  } catch (...) {
    return false;
  }
}

bool BrokerClipboardCapability::structurally_valid() const noexcept {
  if (!identifier(session_id_) || !identifier(operation_id_) ||
      interactive_session_id_ == 0 || interactive_session_id_ == 0xffffffffu ||
      issued_monotonic_ms_ == 0 || expires_monotonic_ms_ <= issued_monotonic_ms_ ||
      expires_monotonic_ms_ - issued_monotonic_ms_ > kMaximumDeadlineMs ||
      !nonzero(request_digest_) || !nonzero(authority_nonce_) || !nonzero(mac_))
    return false;
  if (operation_ == Operation::kRead)
    return !replace_all_formats_confirmed_ &&
        confirmed_sequence_number_ == 0 && !nonzero(content_digest_) &&
        !nonzero(confirmation_digest_);
  return operation_ == Operation::kWrite && replace_all_formats_confirmed_ &&
      confirmed_sequence_number_ != 0 && nonzero(content_digest_) &&
      nonzero(confirmation_digest_);
}

Operation BrokerClipboardCapability::operation() const noexcept {
  return operation_;
}
const std::string& BrokerClipboardCapability::session_id() const noexcept {
  return session_id_;
}
std::uint32_t BrokerClipboardCapability::interactive_session_id() const noexcept {
  return interactive_session_id_;
}
std::uint64_t BrokerClipboardCapability::issued_monotonic_ms() const noexcept {
  return issued_monotonic_ms_;
}
std::uint64_t BrokerClipboardCapability::expires_monotonic_ms() const noexcept {
  return expires_monotonic_ms_;
}
const std::string& BrokerClipboardCapability::operation_id() const noexcept {
  return operation_id_;
}
const std::array<std::uint8_t, 32>&
BrokerClipboardCapability::request_digest() const noexcept {
  return request_digest_;
}
const std::array<std::uint8_t, 32>&
BrokerClipboardCapability::content_digest() const noexcept {
  return content_digest_;
}
const std::array<std::uint8_t, 32>&
BrokerClipboardCapability::confirmation_digest() const noexcept {
  return confirmation_digest_;
}
std::uint32_t
BrokerClipboardCapability::confirmed_sequence_number() const noexcept {
  return confirmed_sequence_number_;
}
bool BrokerClipboardCapability::replace_all_formats_confirmed() const noexcept {
  return replace_all_formats_confirmed_;
}

Result execute(const Request& request, JournalPort* journal) noexcept {
  // This check must remain the first executable statement. It occurs before
  // request/capability/context dereference, time, session, window, clipboard,
  // allocation, hashing, or journal access.
  if (!trust_anchor::activation_prerequisites_available())
    return activation_refusal();
  try {
    if (!request.capability || !request.context)
      return result(Status::kInvalidRequest, request.operation);
    if (request.operation == Operation::kRead) {
      if (request.utf8_text || journal)
        return result(Status::kInvalidRequest, request.operation);
      return execute_read(*request.capability, *request.context);
    }
    if (request.operation == Operation::kWrite) {
      if (!request.utf8_text || !journal)
        return result(Status::kInvalidRequest, request.operation);
      return execute_write(*request.capability, *request.context,
                           *request.utf8_text, *journal);
    }
    return result(Status::kInvalidRequest, request.operation);
  } catch (...) {
    return result(Status::kInternalFailure, request.operation);
  }
}

ReconciliationResult query_write_status(
    const BrokerClipboardCapability* capability,
    const ExecutionContext* context,
    JournalPort* journal) noexcept {
  ReconciliationResult output;
  output.receipt.operation = "clipboard.write.status";
  output.receipt.status = status_name(Status::kPlatformUnavailable);
  // This API always refers to write status. Initialize conservatively before
  // the immutable gate and before inspecting any caller pointer. Missing
  // context/journal/authentication cannot disprove a prior durable dispatch.
  output.receipt.mutation_attempt_state =
      MutationAttemptState::kMayHaveBeenAttempted;
  // Same immutable first boundary as execute(): no pointer, clock, session,
  // clipboard, or journal access when production authority is absent.
  if (!trust_anchor::activation_prerequisites_available()) {
    output.status = Status::kPlatformUnavailable;
    return output;
  }
  try {
    if (!capability || !context || !journal ||
        capability->operation() != Operation::kWrite) {
      output.status = Status::kInvalidRequest;
      output.receipt.status = status_name(output.status);
      return output;
    }
    std::uint64_t deadline = 0;
    InteractiveIdentityLease identity;
    Status status = validate_common(*capability, *context, Operation::kWrite,
                                    deadline, identity);
    std::array<std::uint8_t, 32> expected_request{};
    std::array<std::uint8_t, 32> expected_confirmation{};
    if (status != Status::kOk ||
        !request_digest(Operation::kWrite, capability->content_digest(),
                        expected_request) ||
        !confirmation_digest(*capability, expected_confirmation) ||
        !digest_equal(expected_request, capability->request_digest()) ||
        !digest_equal(expected_confirmation, capability->confirmation_digest())) {
      output.status = status == Status::kOk ? Status::kCapabilityRefused : status;
      output.receipt.status = status_name(output.status);
      return output;
    }
    // Until an exact journal lookup succeeds no negative mutation fact is
    // available. Lookup failure, unavailable storage, malformed state, and
    // unknown enum values all retain the conservative default above.
    JournalLookup lookup;
    const bool lookup_ok = journal->lookup_write(
        capability->operation_id(), capability->request_digest(), lookup);
    // Journal lookup may block. Revalidate the retained session/desktop
    // immediately after it returns, before trusting or publishing its state.
    const bool identity_after_lookup =
        revalidate_interactive_identity(identity);
    if (!lookup_ok) {
      output.status = identity_after_lookup ? Status::kJournalUnavailable :
                                              Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    const bool lookup_shape =
        (lookup.state == JournalLookupState::kNotDispatched &&
         lookup.sequence_before == 0 && lookup.sequence_after == 0 &&
         !lookup.mutation_prepared_durable) ||
        (lookup.state == JournalLookupState::kDispatched &&
         lookup.sequence_before == capability->confirmed_sequence_number() &&
         lookup.sequence_after == 0 &&
         !lookup.mutation_prepared_durable) ||
        (lookup.state == JournalLookupState::kMutationPrepared &&
         lookup.sequence_before == capability->confirmed_sequence_number() &&
         lookup.sequence_after == 0 &&
         lookup.mutation_prepared_durable) ||
        (lookup.state == JournalLookupState::kApplied &&
         lookup.sequence_before == capability->confirmed_sequence_number() &&
         lookup.sequence_after != 0 &&
         lookup.sequence_after != lookup.sequence_before &&
         lookup.mutation_prepared_durable) ||
        (lookup.state == JournalLookupState::kFailedBeforeMutation &&
         lookup.sequence_before == capability->confirmed_sequence_number() &&
         lookup.sequence_after == 0) ||
        (lookup.state == JournalLookupState::kMutationAttemptFailed &&
         lookup.sequence_before == capability->confirmed_sequence_number() &&
         lookup.sequence_after == 0 &&
         lookup.mutation_prepared_durable) ||
        (lookup.state == JournalLookupState::kUnknownAfterMutation &&
         lookup.sequence_before == capability->confirmed_sequence_number() &&
         lookup.mutation_prepared_durable);
    if (!lookup_shape) {
      output.status = Status::kJournalUnavailable;
      output.receipt.status = status_name(output.status);
      return output;
    }
    output.journal_state = lookup.state;
    output.journal_sequence_before = lookup.sequence_before;
    output.journal_sequence_after = lookup.sequence_after;
    output.receipt.journal_dispatch_durable =
        lookup.state != JournalLookupState::kNotDispatched;
    output.receipt.journal_mutation_prepared_durable =
        lookup.mutation_prepared_durable;
    output.receipt.mutation_attempt_state =
        mutation_state_for_lookup(lookup.state);
    output.receipt.journal_outcome_durable =
        lookup.state == JournalLookupState::kApplied ||
        lookup.state == JournalLookupState::kFailedBeforeMutation ||
        lookup.state == JournalLookupState::kMutationAttemptFailed ||
        lookup.state == JournalLookupState::kUnknownAfterMutation;
    if (!identity_after_lookup) {
      output.status = Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    if (lookup.state == JournalLookupState::kApplied) {
      const Status final_stop = stop_status(*context, deadline);
      const bool identity_before_publication =
          revalidate_interactive_identity(identity);
      if (final_stop != Status::kOk || !identity_before_publication) {
        output.status = final_stop != Status::kOk
            ? final_stop : Status::kMutationUnknown;
        output.receipt.status = status_name(output.status);
        return output;
      }
      output.status = Status::kOk;
      output.receipt.sequence_before = lookup.sequence_before;
      output.receipt.sequence_after = lookup.sequence_after;
      output.receipt.status = status_name(output.status);
      return output;
    }
    if (lookup.state == JournalLookupState::kNotDispatched ||
        lookup.state == JournalLookupState::kFailedBeforeMutation ||
        lookup.state == JournalLookupState::kMutationAttemptFailed) {
      const Status final_stop = stop_status(*context, deadline);
      const bool identity_before_publication =
          revalidate_interactive_identity(identity);
      if (final_stop != Status::kOk || !identity_before_publication) {
        output.status = final_stop != Status::kOk
            ? final_stop : Status::kMutationUnknown;
        output.receipt.status = status_name(output.status);
        return output;
      }
      output.status = Status::kPostconditionAbsentManual;
      output.receipt.status = status_name(output.status);
      return output;
    }

    ClipboardLease clipboard;
    status = open_clipboard_bounded(nullptr, *context, identity, deadline,
                                    clipboard);
    if (status != Status::kOk) {
      output.status = Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    Snapshot snapshot;
    status = read_open_clipboard(snapshot);
    if (status != Status::kOk) {
      clipboard.close_or_fail_stop();
      zero_string(snapshot.text);
      output.status = Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    if (!revalidate_interactive_identity(identity)) {
      clipboard.close_or_fail_stop();
      zero_string(snapshot.text);
      output.status = Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    clipboard.close_or_fail_stop();
    if (!revalidate_interactive_identity(identity)) {
      zero_string(snapshot.text);
      output.status = Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    output.observed_sequence = GetClipboardSequenceNumber();
    std::array<std::uint8_t, 32> observed_digest{};
    const bool digest_ok = output.observed_sequence == snapshot.sequence &&
        sha256(snapshot.text, observed_digest);
    zero_string(snapshot.text);
    output.exact_content_present = digest_ok &&
        digest_equal(observed_digest, capability->content_digest());
    SecureZeroMemory(observed_digest.data(), observed_digest.size());
    // Final cancellation/deadline probe: never publish a stale postcondition
    // after the bounded status query has expired.
    const Status final_stop = stop_status(*context, deadline);
    if (final_stop != Status::kOk ||
        !revalidate_interactive_identity(identity)) {
      output.status = final_stop != Status::kOk
          ? final_stop : Status::kMutationUnknown;
      output.receipt.status = status_name(output.status);
      return output;
    }
    output.status = output.exact_content_present
        ? Status::kPostconditionPresentManual
        : Status::kPostconditionAbsentManual;
    output.receipt.sequence_before = lookup.sequence_before;
    output.receipt.sequence_after = output.observed_sequence;
    output.receipt.status = status_name(output.status);
    return output;
  } catch (...) {
    output.status = Status::kInternalFailure;
    output.receipt.status = status_name(output.status);
    return output;
  }
}

}  // namespace lae::windows_clipboard
