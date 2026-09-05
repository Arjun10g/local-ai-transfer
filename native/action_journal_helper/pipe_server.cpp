#include "pipe_server.hpp"

#include <aclapi.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <limits>
#include <thread>
#include <new>
#include <utility>
#include <vector>

#include "../action_journal_storage/windows_storage.hpp"
#include "protocol_codec.hpp"
#include "journal_authority_owner.hpp"

namespace lae::action_journal_helper {
namespace {

constexpr std::size_t kMaximumBootstrapBytes = 65'536;
constexpr std::size_t kBootstrapFixedBytes = 192;
constexpr DWORD kBootstrapDeadlineMs = 15'000;
constexpr DWORD kIoDeadlineMs = 15'000;
constexpr DWORD kCancellationGraceMs = 2'000;
constexpr std::size_t kMaximumPathCharacters = 1'024;
constexpr wchar_t kPipePrefix[] = L"\\\\.\\pipe\\LocalBMO.ActionJournal.v1.";
constexpr std::array<std::uint8_t, 16> kBootstrapMagic = {
    'L', 'A', 'E', 'J', 'R', 'N', 'H', 'E', 'L', 'P', 'B', 'O', 'O', 'T', 0, 0};

// This slice has no accepted supervisor binary or package trust anchor.  The
// inherited-pipe provenance checks below are implemented, but the helper must
// remain fail-closed until a later reviewed packaging slice authenticates the
// retained supervisor image against a signed release manifest.  This constant
// has no build override by design.
constexpr bool kAuthenticatedSupervisorIssuerAvailable = false;

class UniqueHandle final {
 public:
  UniqueHandle() noexcept = default;
  explicit UniqueHandle(HANDLE value) noexcept : value_(value) {}
  ~UniqueHandle() { reset(); }
  UniqueHandle(const UniqueHandle&) = delete;
  UniqueHandle& operator=(const UniqueHandle&) = delete;
  UniqueHandle(UniqueHandle&& other) noexcept : value_(other.release()) {}
  UniqueHandle& operator=(UniqueHandle&& other) noexcept {
    if (this != &other) reset(other.release());
    return *this;
  }
  HANDLE get() const noexcept { return value_; }
  explicit operator bool() const noexcept {
    return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
  }
  HANDLE release() noexcept {
    HANDLE output = value_;
    value_ = INVALID_HANDLE_VALUE;
    return output;
  }
  void reset(HANDLE value = INVALID_HANDLE_VALUE) noexcept {
    if (*this) CloseHandle(value_);
    value_ = value;
  }

 private:
  HANDLE value_ = INVALID_HANDLE_VALUE;
};

struct UserIdentity {
  UniqueHandle token;
  std::vector<std::uint8_t> token_user;
  PSID sid = nullptr;
};

struct PipeSecurity {
  SECURITY_DESCRIPTOR descriptor{};
  std::vector<std::uint8_t> acl;
  SECURITY_ATTRIBUTES attributes{};
};

struct ClientLease {
  UniqueHandle process;
  UniqueHandle image;
  UniqueHandle token;
  std::vector<std::uint8_t> token_user;
};

struct SupervisorIssuerLease {
  UniqueHandle bootstrap_pipe;
  UniqueHandle process;
  UniqueHandle token;
  std::vector<std::uint8_t> token_user;
  std::uint32_t pid = 0;
};

struct BootstrapScope {
  explicit BootstrapScope(BootstrapRecord& value) noexcept : value(value) {}
  ~BootstrapScope() {
    SecureZeroMemory(value.hmac_key.data(), value.hmac_key.size());
    SecureZeroMemory(value.session_nonce.data(), value.session_nonce.size());
    if (locked) VirtualUnlock(&value, sizeof(value));
  }
  BootstrapRecord& value;
  bool locked = false;
};

class SecureBytes final {
 public:
  explicit SecureBytes(std::size_t count) : value(count) {}
  ~SecureBytes() {
    if (!value.empty()) SecureZeroMemory(value.data(), value.size());
  }
  std::vector<std::uint8_t> value;
};

std::uint32_t read_u32(const std::uint8_t* bytes) noexcept {
  return static_cast<std::uint32_t>(bytes[0]) |
      (static_cast<std::uint32_t>(bytes[1]) << 8) |
      (static_cast<std::uint32_t>(bytes[2]) << 16) |
      (static_cast<std::uint32_t>(bytes[3]) << 24);
}

std::uint64_t read_u64(const std::uint8_t* bytes) noexcept {
  std::uint64_t value = 0;
  for (std::size_t index = 0; index < 8; ++index)
    value |= static_cast<std::uint64_t>(bytes[index]) << (index * 8);
  return value;
}

bool equal_bytes(const std::uint8_t* left, const std::uint8_t* right,
                 std::size_t count) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < count; ++index)
    difference |= static_cast<std::uint8_t>(left[index] ^ right[index]);
  return difference == 0;
}

bool equal_path(const std::wstring& left, const std::wstring& right) noexcept {
  if (left.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()) ||
      right.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
    return false;
  return CompareStringOrdinal(left.data(), static_cast<int>(left.size()),
                              right.data(), static_cast<int>(right.size()),
                              TRUE) == CSTR_EQUAL;
}

std::wstring strip_extended_prefix(std::wstring path) {
  if (path.rfind(L"\\\\?\\UNC\\", 0) == 0) return L"\\\\" + path.substr(8);
  if (path.rfind(L"\\\\?\\", 0) == 0) return path.substr(4);
  return path;
}

bool final_path(HANDLE handle, std::wstring& output) {
  std::vector<wchar_t> buffer(32'768);
  const DWORD count = GetFinalPathNameByHandleW(
      handle, buffer.data(), static_cast<DWORD>(buffer.size()),
      FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
  if (count == 0 || count >= buffer.size()) return false;
  output = strip_extended_prefix(std::wstring(buffer.data(), count));
  return output.find(L'\0') == std::wstring::npos;
}

bool safe_bootstrap_path(const std::wstring& value) noexcept {
  if (value.size() <= 3 || value.size() > kMaximumPathCharacters ||
      value[1] != L':' || value[2] != L'\\' || value.rfind(L"\\\\", 0) == 0 ||
      value.rfind(L"\\\\?\\", 0) == 0 || value.rfind(L"\\\\.\\", 0) == 0)
    return false;
  for (std::size_t index = 0; index < value.size(); ++index) {
    const wchar_t character = value[index];
    if (character == L'\0' || character < 0x20 || character == 0x7f ||
        character == L'/') return false;
    if (character == L':' && index != 1) return false;
  }
  return true;
}

bool current_user(UserIdentity& output) {
  HANDLE token = INVALID_HANDLE_VALUE;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
  output.token.reset(token);
  DWORD size = 0;
  GetTokenInformation(output.token.get(), TokenUser, nullptr, 0, &size);
  if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0 || size > 65'536)
    return false;
  output.token_user.resize(size);
  if (!GetTokenInformation(output.token.get(), TokenUser,
                           output.token_user.data(), size, &size)) return false;
  output.sid = reinterpret_cast<TOKEN_USER*>(output.token_user.data())->User.Sid;
  if (!IsValidSid(output.sid) ||
      GetLengthSid(output.sid) > SECURITY_MAX_SID_SIZE) return false;
  constexpr WELL_KNOWN_SID_TYPE rejected[] = {
      WinNullSid, WinWorldSid, WinAnonymousSid, WinAuthenticatedUserSid,
      WinBuiltinUsersSid, WinBuiltinAdministratorsSid, WinLocalSystemSid,
      WinLocalServiceSid, WinNetworkServiceSid};
  for (const auto type : rejected) {
    std::array<std::uint8_t, SECURITY_MAX_SID_SIZE> buffer{};
    DWORD length = static_cast<DWORD>(buffer.size());
    if (!CreateWellKnownSid(type, nullptr, buffer.data(), &length) ||
        EqualSid(output.sid, buffer.data())) return false;
  }
  return true;
}

bool private_pipe_security(PSID sid, PipeSecurity& output) {
  if (!IsValidSid(sid)) return false;
  const DWORD sid_size = GetLengthSid(sid);
  const std::size_t acl_size = sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) -
      sizeof(DWORD) + sid_size;
  if (acl_size > 65'536) return false;
  output.acl.assign(acl_size, 0);
  auto* acl = reinterpret_cast<PACL>(output.acl.data());
  if (!InitializeAcl(acl, static_cast<DWORD>(output.acl.size()), ACL_REVISION) ||
      !AddAccessAllowedAceEx(acl, ACL_REVISION, 0,
                             GENERIC_READ | GENERIC_WRITE | SYNCHRONIZE, sid) ||
      !InitializeSecurityDescriptor(&output.descriptor,
                                    SECURITY_DESCRIPTOR_REVISION) ||
      !SetSecurityDescriptorDacl(&output.descriptor, TRUE, acl, FALSE) ||
      !SetSecurityDescriptorControl(&output.descriptor, SE_DACL_PROTECTED,
                                    SE_DACL_PROTECTED)) return false;
  BOOL present = FALSE, defaulted = TRUE;
  PACL observed = nullptr;
  SECURITY_DESCRIPTOR_CONTROL control = 0;
  DWORD revision = 0;
  if (!GetSecurityDescriptorDacl(&output.descriptor, &present, &observed,
                                 &defaulted) || !present || defaulted ||
      observed != acl || observed->AceCount != 1 ||
      !GetSecurityDescriptorControl(&output.descriptor, &control, &revision) ||
      (control & SE_DACL_PROTECTED) == 0) return false;
  output.attributes = {sizeof(SECURITY_ATTRIBUTES), &output.descriptor, FALSE};
  return true;
}

bool wait_for_bootstrap_bytes(HANDLE pipe, DWORD required,
                              ULONGLONG deadline) noexcept {
  for (;;) {
    DWORD available = 0;
    if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr)) return false;
    if (available >= required) return true;
    if (GetTickCount64() >= deadline) return false;
    Sleep(1);
  }
}

bool authenticate_bootstrap_issuer(PSID helper_sid,
                                   SupervisorIssuerLease& output) {
  HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
  if (input == nullptr || input == INVALID_HANDLE_VALUE ||
      GetFileType(input) != FILE_TYPE_PIPE) return false;
  HANDLE retained_pipe = INVALID_HANDLE_VALUE;
  if (!DuplicateHandle(GetCurrentProcess(), input, GetCurrentProcess(),
                       &retained_pipe, 0, FALSE, DUPLICATE_SAME_ACCESS))
    return false;
  output.bootstrap_pipe.reset(retained_pipe);
  ULONG server_pid = 0;
  if (!GetNamedPipeServerProcessId(output.bootstrap_pipe.get(), &server_pid) ||
      server_pid == 0 || server_pid == GetCurrentProcessId()) return false;
  HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                               FALSE, server_pid);
  if (process == nullptr) return false;
  output.process.reset(process);
  output.pid = server_pid;
  if (GetProcessId(output.process.get()) != server_pid ||
      WaitForSingleObject(output.process.get(), 0) != WAIT_TIMEOUT) return false;
  HANDLE token = INVALID_HANDLE_VALUE;
  if (!OpenProcessToken(output.process.get(), TOKEN_QUERY, &token)) return false;
  output.token.reset(token);
  DWORD size = 0;
  GetTokenInformation(output.token.get(), TokenUser, nullptr, 0, &size);
  if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || size == 0 || size > 65'536)
    return false;
  output.token_user.resize(size);
  if (!GetTokenInformation(output.token.get(), TokenUser,
                           output.token_user.data(), size, &size)) return false;
  const PSID issuer_sid =
      reinterpret_cast<TOKEN_USER*>(output.token_user.data())->User.Sid;
  DWORD issuer_session = 0, issuer_session_size = sizeof(issuer_session);
  DWORD helper_session = 0;
  if (!IsValidSid(issuer_sid) || !EqualSid(issuer_sid, helper_sid) ||
      !GetTokenInformation(output.token.get(), TokenSessionId, &issuer_session,
                           sizeof(issuer_session), &issuer_session_size) ||
      !ProcessIdToSessionId(GetCurrentProcessId(), &helper_session) ||
      issuer_session != helper_session) return false;
  // Same-user/session and kernel-reported anonymous-pipe server PID prevent a
  // bootstrap swap, but do not authenticate which same-user program is the
  // issuer.  No bootstrap field may fill that gap because it is attacker
  // supplied.  Refuse until a signed package manifest pins and verifies the
  // retained issuer process image in a separate reviewed integration slice.
  return kAuthenticatedSupervisorIssuerAvailable;
}

bool read_bootstrap(HANDLE input, BootstrapRecord& output) {
  if (input == nullptr || input == INVALID_HANDLE_VALUE ||
      GetFileType(input) != FILE_TYPE_PIPE) return false;
  const ULONGLONG deadline = GetTickCount64() + kBootstrapDeadlineMs;
  if (!wait_for_bootstrap_bytes(input, 24, deadline)) return false;
  std::array<std::uint8_t, 24> prefix{};
  DWORD peeked = 0;
  if (!PeekNamedPipe(input, prefix.data(), static_cast<DWORD>(prefix.size()),
                     &peeked, nullptr, nullptr) || peeked != prefix.size() ||
      !equal_bytes(prefix.data(), kBootstrapMagic.data(), kBootstrapMagic.size()) ||
      read_u32(prefix.data() + 16) != kHelperAbiVersion) return false;
  const auto total = read_u32(prefix.data() + 20);
  if (total < kBootstrapFixedBytes || total > kMaximumBootstrapBytes ||
      !wait_for_bootstrap_bytes(input, total, deadline)) return false;
  SecureBytes secure_bytes(total);
  auto& bytes = secure_bytes.value;
  DWORD read = 0;
  if (!ReadFile(input, bytes.data(), total, &read, nullptr) || read != total)
    return false;
  // The bootstrap writer must close. This makes a suffix impossible; merely
  // observing zero currently-available bytes would leave a later-write race.
  for (;;) {
    DWORD available = 0;
    if (!PeekNamedPipe(input, nullptr, 0, nullptr, &available, nullptr)) {
      if (GetLastError() != ERROR_BROKEN_PIPE) return false;
      break;
    }
    if (available != 0 || GetTickCount64() >= deadline) return false;
    Sleep(1);
  }
  const auto storage_chars = read_u32(bytes.data() + 168);
  const auto image_chars = read_u32(bytes.data() + 172);
  if (storage_chars == 0 || image_chars == 0 ||
      storage_chars > kMaximumPathCharacters || image_chars > kMaximumPathCharacters ||
      !std::all_of(bytes.begin() + 176, bytes.begin() + kBootstrapFixedBytes,
                   [](std::uint8_t value) { return value == 0; })) return false;
  const std::uint64_t text_bytes =
      static_cast<std::uint64_t>(storage_chars + image_chars) * sizeof(wchar_t);
  if (text_bytes != total - kBootstrapFixedBytes) return false;
  BootstrapRecord parsed;
  // BootstrapRecord's implicit move copies fixed arrays. This scope wipes the
  // local HMAC key and nonce on every invalid return and after a successful
  // move into the caller-owned, separately guarded record.
  BootstrapScope parsed_scope(parsed);
  parsed.expected_client_pid = read_u32(bytes.data() + 24);
  parsed.expected_client_session_id = read_u32(bytes.data() + 28);
  parsed.expected_client_creation_time = read_u64(bytes.data() + 32);
  parsed.expected_client_image_volume_serial = read_u64(bytes.data() + 40);
  std::copy_n(bytes.data() + 48, 16, parsed.expected_client_image_file_id.begin());
  parsed.expected_storage_volume_serial = read_u64(bytes.data() + 64);
  std::copy_n(bytes.data() + 72, 16, parsed.expected_storage_file_id.begin());
  std::copy_n(bytes.data() + 88, 32, parsed.expected_container_id.begin());
  std::copy_n(bytes.data() + 120, 32, parsed.hmac_key.begin());
  std::copy_n(bytes.data() + 152, 16, parsed.session_nonce.begin());
  const wchar_t* text = reinterpret_cast<const wchar_t*>(
      bytes.data() + kBootstrapFixedBytes);
  parsed.storage_directory.assign(text, storage_chars);
  parsed.expected_client_image_path.assign(text + storage_chars, image_chars);
  const bool valid = parsed.expected_client_pid != 0 &&
      parsed.expected_client_creation_time != 0 &&
      parsed.expected_client_image_volume_serial != 0 &&
      parsed.expected_storage_volume_serial != 0 &&
      !std::all_of(parsed.expected_client_image_file_id.begin(),
                   parsed.expected_client_image_file_id.end(),
                   [](std::uint8_t value) { return value == 0; }) &&
      !std::all_of(parsed.expected_storage_file_id.begin(),
                   parsed.expected_storage_file_id.end(),
                   [](std::uint8_t value) { return value == 0; }) &&
      !std::all_of(parsed.expected_container_id.begin(),
                   parsed.expected_container_id.end(),
                   [](std::uint8_t value) { return value == 0; }) &&
      !std::all_of(parsed.hmac_key.begin(), parsed.hmac_key.end(),
                   [](std::uint8_t value) { return value == 0; }) &&
      !std::all_of(parsed.session_nonce.begin(), parsed.session_nonce.end(),
                   [](std::uint8_t value) { return value == 0; }) &&
      safe_bootstrap_path(parsed.storage_directory) &&
      safe_bootstrap_path(parsed.expected_client_image_path);
  if (!valid) return false;
  output = std::move(parsed);
  return true;
}

std::uint64_t file_time_value(const FILETIME& value) noexcept {
  return (static_cast<std::uint64_t>(value.dwHighDateTime) << 32) |
      value.dwLowDateTime;
}

std::uint64_t unix_time_ms() noexcept {
  FILETIME value{};
  GetSystemTimePreciseAsFileTime(&value);
  constexpr std::uint64_t kWindowsToUnix100ns = 116'444'736'000'000'000ULL;
  const auto ticks = file_time_value(value);
  return ticks < kWindowsToUnix100ns ? 0 :
      (ticks - kWindowsToUnix100ns) / 10'000;
}

bool client_identity(HANDLE pipe, const BootstrapRecord& expected, PSID helper_sid,
                     ClientLease& lease) {
  ULONG pid = 0;
  if (!GetNamedPipeClientProcessId(pipe, &pid) ||
      pid != expected.expected_client_pid) return false;
  HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                               FALSE, pid);
  if (process == nullptr) return false;
  lease.process.reset(process);
  if (GetProcessId(lease.process.get()) != pid ||
      WaitForSingleObject(lease.process.get(), 0) != WAIT_TIMEOUT) return false;
  FILETIME creation{}, exit{}, kernel{}, user{};
  if (!GetProcessTimes(lease.process.get(), &creation, &exit, &kernel, &user) ||
      file_time_value(creation) != expected.expected_client_creation_time)
    return false;
  DWORD process_session = 0;
  if (!ProcessIdToSessionId(pid, &process_session) ||
      process_session != expected.expected_client_session_id) return false;
  HANDLE token = INVALID_HANDLE_VALUE;
  if (!OpenProcessToken(lease.process.get(), TOKEN_QUERY, &token)) return false;
  lease.token.reset(token);
  DWORD token_session = 0, token_session_size = sizeof(token_session);
  if (!GetTokenInformation(lease.token.get(), TokenSessionId, &token_session,
                           sizeof(token_session), &token_session_size) ||
      token_session != expected.expected_client_session_id) return false;
  DWORD user_size = 0;
  GetTokenInformation(lease.token.get(), TokenUser, nullptr, 0, &user_size);
  if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || user_size == 0 ||
      user_size > 65'536) return false;
  lease.token_user.resize(user_size);
  if (!GetTokenInformation(lease.token.get(), TokenUser, lease.token_user.data(),
                           user_size, &user_size)) return false;
  const PSID client_sid =
      reinterpret_cast<TOKEN_USER*>(lease.token_user.data())->User.Sid;
  if (!IsValidSid(client_sid) || !EqualSid(client_sid, helper_sid)) return false;
  std::vector<wchar_t> path(32'768);
  DWORD path_size = static_cast<DWORD>(path.size());
  if (!QueryFullProcessImageNameW(lease.process.get(), 0, path.data(), &path_size) ||
      path_size == 0 || path_size >= path.size()) return false;
  const std::wstring observed(path.data(), path_size);
  if (!equal_path(observed, expected.expected_client_image_path)) return false;
  HANDLE image = CreateFileW(
      expected.expected_client_image_path.c_str(),
      FILE_READ_ATTRIBUTES | READ_CONTROL, FILE_SHARE_READ, nullptr,
      OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
  if (image == INVALID_HANDLE_VALUE) return false;
  lease.image.reset(image);
  FILE_ATTRIBUTE_TAG_INFO tag{};
  BY_HANDLE_FILE_INFORMATION basic{};
  FILE_ID_INFO identity{};
  std::wstring image_final;
  if (!GetFileInformationByHandleEx(lease.image.get(), FileAttributeTagInfo,
                                    &tag, sizeof(tag)) ||
      (tag.FileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT |
                             FILE_ATTRIBUTE_DIRECTORY)) != 0 ||
      !GetFileInformationByHandle(lease.image.get(), &basic) ||
      basic.nNumberOfLinks != 1 ||
      !GetFileInformationByHandleEx(lease.image.get(), FileIdInfo,
                                    &identity, sizeof(identity)) ||
      identity.VolumeSerialNumber != expected.expected_client_image_volume_serial ||
      !equal_bytes(identity.FileId.Identifier,
                   expected.expected_client_image_file_id.data(), 16) ||
      !final_path(lease.image.get(), image_final) ||
      !equal_path(image_final, expected.expected_client_image_path) ||
      WaitForSingleObject(lease.process.get(), 0) != WAIT_TIMEOUT)
    return false;
  FILETIME creation_recheck{};
  if (!GetProcessTimes(lease.process.get(), &creation_recheck, &exit, &kernel, &user) ||
      file_time_value(creation_recheck) != expected.expected_client_creation_time)
    return false;
  return true;
}

enum class IoResult { kOk, kClosed, kTimeout, kCancelFailed, kFailed };

IoResult wait_overlapped(HANDLE pipe, HANDLE client_process, OVERLAPPED& operation,
                         ULONGLONG deadline, DWORD& transferred) noexcept {
  const ULONGLONG now = GetTickCount64();
  const DWORD remaining = now >= deadline ? 0 : static_cast<DWORD>(
      std::min<ULONGLONG>(deadline - now, MAXDWORD - 1));
  HANDLE waits[2] = {operation.hEvent, client_process};
  const DWORD observed = WaitForMultipleObjects(2, waits, FALSE, remaining);
  if (observed == WAIT_OBJECT_0) {
    if (GetOverlappedResult(pipe, &operation, &transferred, FALSE))
      return IoResult::kOk;
    const DWORD error = GetLastError();
    return (error == ERROR_BROKEN_PIPE || error == ERROR_PIPE_NOT_CONNECTED)
        ? IoResult::kClosed : IoResult::kFailed;
  }
  const bool timed_out = observed == WAIT_TIMEOUT;
  const BOOL cancelled = CancelIoEx(pipe, &operation);
  const DWORD cancel_error = cancelled ? ERROR_SUCCESS : GetLastError();
  const DWORD settled = WaitForSingleObject(operation.hEvent, kCancellationGraceMs);
  DWORD ignored = 0;
  const BOOL completed = settled == WAIT_OBJECT_0
      ? GetOverlappedResult(pipe, &operation, &ignored, FALSE) : FALSE;
  const DWORD completion_error = completed ? ERROR_SUCCESS : GetLastError();
  if (settled != WAIT_OBJECT_0 ||
      (!cancelled && cancel_error != ERROR_NOT_FOUND) ||
      (completed && ignored != 0) ||
      (!completed && completion_error != ERROR_OPERATION_ABORTED &&
       completion_error != ERROR_BROKEN_PIPE &&
       completion_error != ERROR_PIPE_NOT_CONNECTED)) return IoResult::kCancelFailed;
  return timed_out ? IoResult::kTimeout : IoResult::kClosed;
}

IoResult exact_io(HANDLE pipe, HANDLE client_process, bool write,
                  std::uint8_t* bytes, std::size_t count,
                  ULONGLONG deadline) noexcept {
  std::size_t completed = 0;
  while (completed < count) {
    UniqueHandle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!event) return IoResult::kFailed;
    OVERLAPPED operation{};
    operation.hEvent = event.get();
    const DWORD requested = static_cast<DWORD>(std::min<std::size_t>(
        count - completed, 65'536));
    DWORD transferred = 0;
    const BOOL started = write
        ? WriteFile(pipe, bytes + completed, requested, &transferred, &operation)
        : ReadFile(pipe, bytes + completed, requested, &transferred, &operation);
    if (!started) {
      const DWORD error = GetLastError();
      if (error == ERROR_IO_PENDING) {
        const auto status = wait_overlapped(pipe, client_process, operation,
                                            deadline, transferred);
        if (status != IoResult::kOk) return status;
      } else if (error == ERROR_BROKEN_PIPE || error == ERROR_PIPE_NOT_CONNECTED) {
        return IoResult::kClosed;
      } else return IoResult::kFailed;
    }
    if (transferred == 0 || transferred > requested) return IoResult::kClosed;
    completed += transferred;
  }
  return IoResult::kOk;
}

IoResult read_frame(HANDLE pipe, HANDLE client_process,
                    std::vector<std::uint8_t>& frame) {
  frame.assign(4, 0);
  const ULONGLONG deadline = GetTickCount64() + kIoDeadlineMs;
  auto status = exact_io(pipe, client_process, false, frame.data(), 4, deadline);
  if (status != IoResult::kOk) { frame.clear(); return status; }
  const auto payload = (static_cast<std::uint32_t>(frame[0]) << 24) |
      (static_cast<std::uint32_t>(frame[1]) << 16) |
      (static_cast<std::uint32_t>(frame[2]) << 8) | frame[3];
  if (payload < 2 || payload > kMaxPayloadBytes) {
    frame.clear();
    return IoResult::kFailed;
  }
  frame.resize(payload + 4);
  status = exact_io(pipe, client_process, false, frame.data() + 4, payload, deadline);
  if (status != IoResult::kOk) frame.clear();
  return status;
}

IoResult write_frame(HANDLE pipe, HANDLE client_process,
                     std::vector<std::uint8_t>& frame) noexcept {
  if (frame.size() < 6 || frame.size() > kMaxFrameBytes) return IoResult::kFailed;
  const auto status = exact_io(pipe, client_process, true, frame.data(),
                               frame.size(), GetTickCount64() + kIoDeadlineMs);
  SecureZeroMemory(frame.data(), frame.size());
  frame.clear();
  return status;
}

HelperStatus io_status(IoResult status) noexcept {
  switch (status) {
    case IoResult::kOk: return HelperStatus::kOk;
    case IoResult::kClosed: return HelperStatus::kTransportClosed;
    case IoResult::kTimeout: return HelperStatus::kIoTimeout;
    case IoResult::kCancelFailed: return HelperStatus::kIoCancelFailed;
    case IoResult::kFailed: return HelperStatus::kInvalidFrame;
  }
  return HelperStatus::kInternal;
}

HelperStatus connect_one(HANDLE pipe) noexcept {
  UniqueHandle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
  if (!event) return HelperStatus::kPipeConnectFailed;
  OVERLAPPED operation{};
  operation.hEvent = event.get();
  if (ConnectNamedPipe(pipe, &operation)) return HelperStatus::kOk;
  const DWORD error = GetLastError();
  if (error == ERROR_PIPE_CONNECTED) {
    SetEvent(event.get());
    return HelperStatus::kOk;
  }
  if (error != ERROR_IO_PENDING) return HelperStatus::kPipeConnectFailed;
  const DWORD wait = WaitForSingleObject(event.get(), kIoDeadlineMs);
  if (wait == WAIT_OBJECT_0) {
    DWORD ignored = 0;
    return GetOverlappedResult(pipe, &operation, &ignored, FALSE)
        ? HelperStatus::kOk : HelperStatus::kPipeConnectFailed;
  }
  const BOOL cancelled = CancelIoEx(pipe, &operation);
  const DWORD cancel_error = cancelled ? ERROR_SUCCESS : GetLastError();
  const DWORD settled = WaitForSingleObject(event.get(), kCancellationGraceMs);
  DWORD ignored = 0;
  const BOOL completed = settled == WAIT_OBJECT_0
      ? GetOverlappedResult(pipe, &operation, &ignored, FALSE) : FALSE;
  const DWORD completion_error = completed ? ERROR_SUCCESS : GetLastError();
  if (settled != WAIT_OBJECT_0 ||
      (!cancelled && cancel_error != ERROR_NOT_FOUND) ||
      (completed && ignored != 0) ||
      (!completed && completion_error != ERROR_OPERATION_ABORTED &&
       completion_error != ERROR_PIPE_NOT_CONNECTED))
    return HelperStatus::kIoCancelFailed;
  return HelperStatus::kPipeConnectFailed;
}

bool recoverable_store_status(StoreStatus status) noexcept {
  return status == StoreStatus::kNotFound ||
      status == StoreStatus::kInvalidTransition ||
      status == StoreStatus::kRecordLimitExceeded ||
      status == StoreStatus::kEventLimitExceeded ||
      status == StoreStatus::kGenerationExhausted ||
      status == StoreStatus::kCancelled ||
      status == StoreStatus::kCommitNonCancellable;
}

HelperStatus authority_status(AuthorityStatus status) noexcept {
  switch (status) {
    case AuthorityStatus::kStorageUnavailable:
      return HelperStatus::kStorageUnavailable;
    case AuthorityStatus::kStorageCorrupt:
      return HelperStatus::kStorageCorrupt;
    case AuthorityStatus::kIoTimeout:
      return HelperStatus::kIoTimeout;
    case AuthorityStatus::kIoCancelFailed:
      return HelperStatus::kIoCancelFailed;
    case AuthorityStatus::kRecoveryFailed:
      return HelperStatus::kRecoveryFailed;
    case AuthorityStatus::kReady:
      return HelperStatus::kOk;
    case AuthorityStatus::kNotReady:
    case AuthorityStatus::kPoisoned:
    case AuthorityStatus::kInternal:
      return HelperStatus::kInternal;
  }
  return HelperStatus::kInternal;
}

struct RequestCancellationContext {
  HANDLE pipe = INVALID_HANDLE_VALUE;
  HANDLE client_process = INVALID_HANDLE_VALUE;
  std::uint64_t deadline_at_ms = 0;
};

bool request_cancelled(void* raw) noexcept {
  const auto* context = static_cast<const RequestCancellationContext*>(raw);
  if (context == nullptr || context->pipe == INVALID_HANDLE_VALUE ||
      context->client_process == INVALID_HANDLE_VALUE ||
      WaitForSingleObject(context->client_process, 0) != WAIT_TIMEOUT ||
      unix_time_ms() >= context->deadline_at_ms) return true;
  DWORD available = 0;
  return !PeekNamedPipe(context->pipe, nullptr, 0, nullptr, &available, nullptr);
}

// Pipe/process probes run on this monitor thread, never from the owner lock.
// The storage codec sees only the immutable event/atomic snapshot below.
class RequestCancellationMonitor final {
 public:
  RequestCancellationMonitor(HANDLE pipe, HANDLE client_process,
                             std::uint64_t deadline_at_ms) noexcept
      : pipe_(pipe), client_process_(client_process),
        deadline_at_ms_(deadline_at_ms) {}
  ~RequestCancellationMonitor() noexcept {
    stopping_.store(true, std::memory_order_release);
    if (worker_.joinable()) worker_.join();
  }
  RequestCancellationMonitor(const RequestCancellationMonitor&) = delete;
  RequestCancellationMonitor& operator=(const RequestCancellationMonitor&) = delete;

  bool start() noexcept {
    event_.reset(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!event_) return false;
    try {
      worker_ = std::thread([this] { monitor(); });
    } catch (...) {
      event_.reset();
      return false;
    }
    return true;
  }

  bool cancellation_signaled() const noexcept {
    if (cancelled_.load(std::memory_order_acquire)) return true;
    return event_ && WaitForSingleObject(event_.get(), 0) == WAIT_OBJECT_0;
  }

 private:
  void signal() noexcept {
    cancelled_.store(true, std::memory_order_release);
    if (event_) SetEvent(event_.get());
  }

  void monitor() noexcept {
    while (!stopping_.load(std::memory_order_acquire)) {
      if (unix_time_ms() >= deadline_at_ms_ ||
          WaitForSingleObject(client_process_, 0) != WAIT_TIMEOUT) {
        signal();
        return;
      }
      DWORD available = 0;
      if (!PeekNamedPipe(pipe_, nullptr, 0, nullptr, &available, nullptr)) {
        signal();
        return;
      }
      Sleep(1);
    }
  }

  HANDLE pipe_ = INVALID_HANDLE_VALUE;
  HANDLE client_process_ = INVALID_HANDLE_VALUE;
  std::uint64_t deadline_at_ms_ = 0;
  UniqueHandle event_;
  std::atomic_bool stopping_{false};
  std::atomic_bool cancelled_{false};
  std::thread worker_;
};

bool monitored_request_cancelled(void* raw) noexcept {
  const auto* monitor = static_cast<const RequestCancellationMonitor*>(raw);
  return monitor == nullptr || monitor->cancellation_signaled();
}

struct StartupCancellationContext {
  HANDLE supervisor_process = INVALID_HANDLE_VALUE;
};

bool startup_cancelled(void* raw) noexcept {
  const auto* context = static_cast<const StartupCancellationContext*>(raw);
  return context == nullptr || context->supervisor_process == INVALID_HANDLE_VALUE ||
      WaitForSingleObject(context->supervisor_process, 0) != WAIT_TIMEOUT;
}

}  // namespace

HelperStatus run_foreground_helper_from_inherited_stdin() noexcept {
  BootstrapRecord bootstrap;
  BootstrapScope bootstrap_scope(bootstrap);
  try {
    UserIdentity user;
    if (!current_user(user)) return HelperStatus::kBootstrapIssuerUntrusted;
    SupervisorIssuerLease issuer;
    if (!authenticate_bootstrap_issuer(user.sid, issuer))
      return HelperStatus::kBootstrapIssuerUntrusted;
    if (!read_bootstrap(issuer.bootstrap_pipe.get(), bootstrap))
      return HelperStatus::kBootstrapInvalid;
    bootstrap_scope.locked = VirtualLock(&bootstrap, sizeof(bootstrap)) != FALSE;
    action_journal_storage::StorageRequest storage_request;
    storage_request.mode = action_journal_storage::OpenMode::kOpenExisting;
    storage_request.absolute_directory = bootstrap.storage_directory;
    storage_request.has_expected_identity = true;
    storage_request.expected_identity.volume_serial =
        bootstrap.expected_storage_volume_serial;
    storage_request.expected_identity.file_id = bootstrap.expected_storage_file_id;
    storage_request.expected_identity.container_id = bootstrap.expected_container_id;
    action_journal_storage::StorageReceipt storage_receipt;
    StartupCancellationContext startup_context{issuer.process.get()};
    const StorageIoControl startup_io{
        CancellationProbe{startup_cancelled, &startup_context},
        GetTickCount64() + kIoDeadlineMs, true};
    AuthorityStatus owner_status = AuthorityStatus::kInternal;
    auto owner = JournalAuthorityOwner::open(
        storage_request, bootstrap.expected_container_id, startup_io,
        owner_status, storage_receipt);
    if (!owner) return authority_status(owner_status);
    PipeSecurity security;
    if (!private_pipe_security(user.sid, security))
      return HelperStatus::kPipeSecurityFailed;
    // Recheck the shared startup authority at the final pipe-publication
    // boundary; recovery success must not outlive its supervisor or deadline.
    if (startup_io.stop_requested())
      return startup_io.cancellation_requested()
          ? HelperStatus::kRecoveryFailed : HelperStatus::kIoTimeout;
    const std::wstring pipe_name = kPipePrefix +
        std::to_wstring(GetCurrentProcessId());
    UniqueHandle pipe(CreateNamedPipeW(
        pipe_name.c_str(), PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
            PIPE_REJECT_REMOTE_CLIENTS,
        1, static_cast<DWORD>(kMaxFrameBytes),
        static_cast<DWORD>(kMaxFrameBytes), kIoDeadlineMs,
        &security.attributes));
    if (!pipe) return HelperStatus::kPipeSecurityFailed;
    const auto connected = connect_one(pipe.get());
    if (connected != HelperStatus::kOk) return connected;
    ClientLease client;
    if (!client_identity(pipe.get(), bootstrap, user.sid, client)) {
      DisconnectNamedPipe(pipe.get());
      return HelperStatus::kClientIdentityMismatch;
    }
    ProtocolSession protocol(bootstrap.hmac_key, bootstrap.session_nonce);
    SecureZeroMemory(bootstrap.hmac_key.data(), bootstrap.hmac_key.size());
    SecureZeroMemory(bootstrap.session_nonce.data(), bootstrap.session_nonce.size());
    if (bootstrap_scope.locked) {
      VirtualUnlock(&bootstrap, sizeof(bootstrap));
      bootstrap_scope.locked = false;
    }
    for (std::size_t count = 0; count < kMaxSessionFrames; ++count) {
      std::vector<std::uint8_t> request_frame;
      auto io = read_frame(pipe.get(), client.process.get(), request_frame);
      if (io != IoResult::kOk) return io_status(io);
      DecodedRequest request;
      const auto decoded = protocol.decode_request(
          request_frame, unix_time_ms(), request);
      SecureZeroMemory(request_frame.data(), request_frame.size());
      request_frame.clear();
      if (decoded != CodecStatus::kOk) {
        DisconnectNamedPipe(pipe.get());
        return decoded == CodecStatus::kInvalidMac ? HelperStatus::kInvalidMac
             : decoded == CodecStatus::kReplay ? HelperStatus::kReplay
             : decoded == CodecStatus::kSequenceOutOfOrder
                   ? HelperStatus::kSequenceOutOfOrder
             : decoded == CodecStatus::kDeadlineExpired
                   ? HelperStatus::kDeadlineExpired
             : HelperStatus::kInvalidFrame;
      }
      if (WaitForSingleObject(client.process.get(), 0) != WAIT_TIMEOUT)
        return HelperStatus::kTransportClosed;
      EncodedResult result;
      RequestCancellationContext cancellation_context{
          pipe.get(), client.process.get(), request.deadline_at_ms};
      // Probe the transport before admission. Once admitted, the monitor is
      // the only cancellation callback visible to the owner/store: it polls
      // pipe/process handles on its own thread and publishes an atomic/event
      // result, so no pipe API executes while the owner mutex is held.
      if (unix_time_ms() >= request.deadline_at_ms)
        return HelperStatus::kDeadlineExpired;
      if (request_cancelled(&cancellation_context))
        return HelperStatus::kTransportClosed;
      RequestCancellationMonitor cancellation_monitor(
          pipe.get(), client.process.get(), request.deadline_at_ms);
      if (!cancellation_monitor.start()) return HelperStatus::kInternal;
      const CancellationProbe cancellation{monitored_request_cancelled,
                                            &cancellation_monitor};
      const StorageIoControl request_io{
          cancellation, GetTickCount64() + kIoDeadlineMs, true};
      const auto applied = owner->apply(request, request_io, result);
      // Do not encode or return a result after the transport stopped during
      // the owner-locked store application.
      if (request_cancelled(&cancellation_context)) {
        return unix_time_ms() >= request.deadline_at_ms
            ? HelperStatus::kDeadlineExpired : HelperStatus::kTransportClosed;
      }
      if (applied != StoreStatus::kOk) {
        if (applied == StoreStatus::kIoTimeout) {
          DisconnectNamedPipe(pipe.get());
          return HelperStatus::kIoTimeout;
        }
        if (applied == StoreStatus::kIoCancelFailed) {
          DisconnectNamedPipe(pipe.get());
          return HelperStatus::kIoCancelFailed;
        }
        if (!recoverable_store_status(applied)) {
          DisconnectNamedPipe(pipe.get());
          return HelperStatus::kStorageCorrupt;
        }
        result = {};
        result.operation_id = request.operation_id;
        result.error_code = store_status_name(applied);
      }
      std::vector<std::uint8_t> response_frame;
      if (protocol.encode_response(request, result, response_frame) != CodecStatus::kOk)
        return HelperStatus::kInternal;
      io = write_frame(pipe.get(), client.process.get(), response_frame);
      if (io != IoResult::kOk) return io_status(io);
    }
    return HelperStatus::kQueueFull;
  } catch (const std::bad_alloc&) {
    return HelperStatus::kInternal;
  } catch (...) {
    return HelperStatus::kInternal;
  }
}

const char* helper_status_name(HelperStatus status) noexcept {
  switch (status) {
    case HelperStatus::kOk: return "ok";
    case HelperStatus::kPlatformUnavailable: return "platform_unavailable";
    case HelperStatus::kBootstrapInvalid: return "bootstrap_invalid";
    case HelperStatus::kBootstrapIssuerUntrusted:
      return "bootstrap_issuer_untrusted";
    case HelperStatus::kBootstrapTimeout: return "bootstrap_timeout";
    case HelperStatus::kStorageUnavailable: return "storage_unavailable";
    case HelperStatus::kStorageCorrupt: return "storage_corrupt";
    case HelperStatus::kRecoveryFailed: return "recovery_failed";
    case HelperStatus::kPipeSecurityFailed: return "pipe_security_failed";
    case HelperStatus::kPipeConnectFailed: return "pipe_connect_failed";
    case HelperStatus::kClientIdentityMismatch: return "client_identity_mismatch";
    case HelperStatus::kInvalidFrame: return "invalid_frame";
    case HelperStatus::kInvalidMac: return "invalid_mac";
    case HelperStatus::kReplay: return "replay";
    case HelperStatus::kSequenceOutOfOrder: return "sequence_out_of_order";
    case HelperStatus::kDeadlineExpired: return "deadline_expired";
    case HelperStatus::kQueueFull: return "queue_full";
    case HelperStatus::kInvalidTransition: return "invalid_transition";
    case HelperStatus::kCommitNonCancellable: return "commit_non_cancellable";
    case HelperStatus::kIoTimeout: return "io_timeout";
    case HelperStatus::kIoCancelFailed: return "io_cancel_failed";
    case HelperStatus::kTransportClosed: return "transport_closed";
    case HelperStatus::kInternal: return "internal";
  }
  return "internal";
}

}  // namespace lae::action_journal_helper
