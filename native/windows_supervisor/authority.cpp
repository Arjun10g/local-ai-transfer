#include "authority.hpp"

#if defined(_WIN32)

#include <bcrypt.h>
#include <ntsecapi.h>
#include <wintrust.h>

#include <array>
#include <algorithm>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <map>
#include <mutex>
#include <limits>
#include <memory>
#include <new>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

// This translation unit is intentionally absent from native/CMakeLists.txt.
// Every entry point still refuses because all release gates in authority.hpp
// are source constants false.
namespace lae::windows_supervisor {
namespace {

constexpr DWORD kWaitSliceMs = 250;
constexpr DWORD kMaxWaitMs = 120000;
constexpr std::uint64_t kMaxCleanupHorizonMs = 2ull * kMaxWaitMs;
// One direct root Job is the only supported containment model.  The registry
// and the Job capacity intentionally share one limit.
constexpr std::size_t kMaxChildren = 8;
constexpr std::size_t kMaxFrameBytes = 65536;
constexpr std::size_t kCapabilityBytes = 32;
constexpr std::size_t kMaxDigestBytes = 32;
constexpr std::size_t kMaxJournalRecords = 64;
constexpr std::size_t kMaxCapturedBytesPerStream = 64 * 1024;
constexpr DWORD kWorkerSettlementMs = 2000;
// Each operation needs one durable start plus one durable terminal/unknown
// record. Reserve both sequence numbers before invoking provider code.
constexpr std::uint64_t kRequiredJournalSequences = 2;
constexpr std::size_t kMaxIdentityFileBytes = 67'108'864;
constexpr std::uint32_t kScopeProcessLaunch = 1u;
constexpr std::uint32_t kScopeBrokerSession = 2u;
constexpr std::uint32_t kScopeExternalAction = 4u;
constexpr std::uint32_t kAllScopes =
    kScopeProcessLaunch | kScopeBrokerSession | kScopeExternalAction;

struct FixedIdentity final {
  std::array<std::byte, 32> digest{};
  std::array<std::byte, 16> file_id{};
  std::uint64_t volume_serial = 0;
  std::uint64_t file_index = 0;  // never trusted without volume/file_id
  std::uint32_t size = 0;
  bool regular = false;
  bool reparse_free = false;
};

struct ProcessBinding final {
  DWORD pid = 0;
  std::uint64_t creation_time = 0;
  DWORD session_id = 0;
  std::array<std::byte, kCapabilityBytes> token_sid_digest{};
  FixedIdentity image{};
};

enum class PipeDirection : std::uint8_t { kReadFromChild, kWriteToChild };

struct PipeBinding final {
  HANDLE handle = INVALID_HANDLE_VALUE;
  PipeDirection direction = PipeDirection::kReadFromChild;
  DWORD server_pid = 0;
  DWORD client_pid = 0;
  std::uint64_t server_creation_time = 0;
  bool direction_proven = false;  // advisory metadata; kernel access proof wins
};

struct BootstrapProof final {
  PipeBinding control_read{};
  PipeBinding control_write{};
  ProcessBinding parent{};
  ProcessBinding pipe_server{};
  bool inherited_only = false;
};

class CapabilityIssuer;

struct IssuedCapability final {
  std::array<std::byte, kCapabilityBytes> id{};
  std::array<std::byte, kCapabilityBytes> nonce{};
  std::array<std::byte, kCapabilityBytes> mac{};
  std::uint64_t expires_at_ms = 0;
  std::uint32_t operation_scope = 0;  // exactly one closed scope bit
  std::uint64_t session_id = 0;
  std::uint64_t operation_id = 0;
  std::array<std::byte, kMaxDigestBytes> operation_digest{};
  std::array<std::byte, kMaxDigestBytes> argument_digest{};
  std::array<std::byte, kMaxDigestBytes> preview_digest{};

  ~IssuedCapability() {
    clear_secrets();
  }

  void clear_secrets() noexcept {
    SecureZeroMemory(id.data(), id.size());
    SecureZeroMemory(nonce.data(), nonce.size());
    SecureZeroMemory(mac.data(), mac.size());
    SecureZeroMemory(operation_digest.data(), operation_digest.size());
    SecureZeroMemory(argument_digest.data(), argument_digest.size());
    SecureZeroMemory(preview_digest.data(), preview_digest.size());
  }
};

bool trust_gates_open() noexcept;
bool fill_random(void* bytes, ULONG size) noexcept;

// The verifier is a capability to ask the issuer, not a copy of its signing
// key.  The key remains private to CapabilityIssuer for the entire lifetime.
class CapabilityVerifier final {
 public:
  bool verify(const IssuedCapability& capability, std::uint64_t now_ms,
              std::uint64_t session_id, std::uint32_t scope,
              std::uint64_t operation_id,
              const std::array<std::byte, kMaxDigestBytes>& operation_digest,
              const std::array<std::byte, kMaxDigestBytes>& argument_digest,
              const std::array<std::byte, kMaxDigestBytes>& preview_digest) const noexcept;

 private:
  friend class CapabilityIssuer;
  explicit CapabilityVerifier(const CapabilityIssuer* issuer) noexcept
      : issuer_(issuer) {}
  const CapabilityIssuer* issuer_ = nullptr;
};

class CapabilityIssuer final {
 public:
  ~CapabilityIssuer() {
    SecureZeroMemory(signing_key_.data(), signing_key_.size());
  }

  CapabilityVerifier verifier() const noexcept { return CapabilityVerifier(this); }
  bool initialize_epoch() noexcept {
    if (!trust_gates_open()) return false;
    if (epoch_initialized_) return true;
    std::array<std::byte, kCapabilityBytes> candidate{};
    if (!fill_random(candidate.data(), static_cast<ULONG>(candidate.size()))) {
      SecureZeroMemory(candidate.data(), candidate.size());
      return false;
    }
    std::memcpy(signing_key_.data(), candidate.data(), candidate.size());
    SecureZeroMemory(candidate.data(), candidate.size());
    epoch_initialized_ = true;
    return true;
  }
  bool issue(IssuedCapability& capability, std::uint64_t now_ms,
             std::uint64_t session_id, std::uint32_t scope,
             std::uint64_t operation_id,
             const std::array<std::byte, kMaxDigestBytes>& operation_digest,
             const std::array<std::byte, kMaxDigestBytes>& argument_digest,
             const std::array<std::byte, kMaxDigestBytes>& preview_digest) noexcept;

 private:
  friend class CapabilityVerifier;
  friend bool issue_capability(
      CapabilityIssuer&, IssuedCapability&, std::uint64_t, std::uint64_t,
      std::uint32_t, std::uint64_t,
      const std::array<std::byte, kMaxDigestBytes>&,
      const std::array<std::byte, kMaxDigestBytes>&,
      const std::array<std::byte, kMaxDigestBytes>&) noexcept;
  friend bool capability_valid(
      const CapabilityIssuer&, const IssuedCapability&, std::uint64_t,
      std::uint64_t, std::uint32_t, std::uint64_t,
      const std::array<std::byte, kMaxDigestBytes>&,
      const std::array<std::byte, kMaxDigestBytes>&,
      const std::array<std::byte, kMaxDigestBytes>&) noexcept;
  std::array<std::byte, kCapabilityBytes> signing_key_{};
  bool epoch_initialized_ = false;
  bool verify(const IssuedCapability& capability, std::uint64_t now_ms,
              std::uint64_t session_id, std::uint32_t scope,
              std::uint64_t operation_id,
              const std::array<std::byte, kMaxDigestBytes>& operation_digest,
              const std::array<std::byte, kMaxDigestBytes>& argument_digest,
              const std::array<std::byte, kMaxDigestBytes>& preview_digest) const noexcept;
};

class UniqueHandle final {
 public:
  UniqueHandle() noexcept = default;
  explicit UniqueHandle(HANDLE value) noexcept : value_(value) {}
  ~UniqueHandle() { reset(); }
  UniqueHandle(const UniqueHandle&) = delete;
  UniqueHandle& operator=(const UniqueHandle&) = delete;
  UniqueHandle(UniqueHandle&& other) noexcept : value_(other.release()) {}
  UniqueHandle& operator=(UniqueHandle&& other) noexcept {
    if (this != &other) {
      reset(other.release());
    }
    return *this;
  }
  HANDLE get() const noexcept { return value_; }
  explicit operator bool() const noexcept {
    return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
  }
  HANDLE release() noexcept {
    HANDLE value = value_;
    value_ = INVALID_HANDLE_VALUE;
    return value;
  }
  void reset(HANDLE value = INVALID_HANDLE_VALUE) noexcept {
    if (*this) CloseHandle(value_);
    value_ = value;
  }
 private:
  HANDLE value_ = INVALID_HANDLE_VALUE;
};

struct DrainContext final {
  HANDLE source = INVALID_HANDLE_VALUE;
  HANDLE cancellation = nullptr;
  const std::atomic<bool>* shutting_down = nullptr;
  std::uint64_t deadline_at_ms = 0;
  std::array<std::byte, kMaxCapturedBytesPerStream> bytes{};
  DWORD limit = 0;
  std::atomic<DWORD> captured{0};
  std::atomic<DWORD> error{ERROR_IO_PENDING};
  std::atomic<bool> overflow{false};

  ~DrainContext() { SecureZeroMemory(bytes.data(), bytes.size()); }
};

bool drain_result_usable(const DrainContext& context) noexcept {
  return context.shutting_down &&
      !context.shutting_down->load(std::memory_order_acquire) &&
      GetTickCount64() < context.deadline_at_ms &&
      (!context.cancellation ||
       WaitForSingleObject(context.cancellation, 0) == WAIT_TIMEOUT);
}

// CreateThread receives a raw context pointer. This owner therefore refuses
// normal destruction while that pointer could still be in use. The context is
// freed only before a worker starts or after its thread was joined and proven
// stopped.
class DrainContextOwner final {
 public:
  DrainContextOwner() = default;
  ~DrainContextOwner() {
    if (context_) std::terminate();
  }
  DrainContextOwner(const DrainContextOwner&) = delete;
  DrainContextOwner& operator=(const DrainContextOwner&) = delete;
  DrainContextOwner(DrainContextOwner&& other) noexcept
      : context_(std::exchange(other.context_, nullptr)) {}
  DrainContextOwner& operator=(DrainContextOwner&& other) noexcept {
    if (context_) std::terminate();
    context_ = std::exchange(other.context_, nullptr);
    return *this;
  }

  bool allocate() noexcept {
    if (context_) return false;
    context_ = new (std::nothrow) DrainContext();
    return context_ != nullptr;
  }
  explicit operator bool() const noexcept { return context_ != nullptr; }
  DrainContext* get() const noexcept { return context_; }
  DrainContext* operator->() const noexcept { return context_; }

  void discard_before_worker_start() noexcept {
    DrainContext* context = std::exchange(context_, nullptr);
    delete context;
  }
  void release_after_join() noexcept {
    DrainContext* context = std::exchange(context_, nullptr);
    delete context;
  }

 private:
  DrainContext* context_ = nullptr;
};

struct Child final {
  UniqueHandle process;
  UniqueHandle primary_thread;
  UniqueHandle stdout_read;
  UniqueHandle stderr_read;
  UniqueHandle stdout_worker;
  UniqueHandle stderr_worker;
  DrainContextOwner stdout_context;
  DrainContextOwner stderr_context;
  FixedIdentity executable{};
  std::uint64_t stable_id = 0;
  bool membership_verified = false;
  bool resumed = false;
  bool output_overflow = false;
  std::uint32_t stdout_bytes = 0;
  std::uint32_t stderr_bytes = 0;
  Child() = default;
  Child(const Child&) = delete;
  Child& operator=(const Child&) = delete;
  Child(Child&&) noexcept = default;
  Child& operator=(Child&&) noexcept = default;
};

// A failed inherited-handle transfer is not recoverable in-process: one
// endpoint may already have been closed while the other remains inheritable.
// Termination closes process-owned handles without reporting adoption.
[[noreturn]] void bootstrap_transfer_fail_stop() noexcept {
  std::terminate();
}

bool release_manifest_is_pinned() noexcept {
  // A future build must compare exact bounded manifest bytes to a reviewed
  // immutable digest. No manifest path or untrusted configuration is read here.
  return kReleaseManifestPinned;
}

bool self_authenticode_is_pinned() noexcept {
  // A future implementation must use WinVerifyTrust plus a retained module
  // handle and compare the exact reviewed signer/subject. Unknown signatures
  // and replacement/reparse identities refuse.
  return kSelfAuthenticodePinned;
}

bool verify_self_authenticode(HANDLE retained_module_file) noexcept {
  if (!retained_module_file || !kSelfAuthenticodePinned) return false;
  WINTRUST_FILE_INFO file_info{};
  file_info.cbStruct = sizeof(file_info);
  file_info.hFile = retained_module_file;
  WINTRUST_DATA trust_data{};
  trust_data.cbStruct = sizeof(trust_data);
  trust_data.dwUIChoice = WTD_UI_NONE;
  trust_data.fdwRevocationChecks = WTD_REVOKE_NONE;
  trust_data.dwUnionChoice = WTD_CHOICE_FILE;
  trust_data.pFile = &file_info;
  GUID policy = WINTRUST_ACTION_GENERIC_VERIFY_V2;
  // The reviewed release must additionally bind the signer/catalog digest;
  // a successful platform trust result alone is not an activation proof.
  const LONG result = WinVerifyTrust(nullptr, &policy, &trust_data);
  return result == ERROR_SUCCESS && self_authenticode_is_pinned();
}

bool package_identity_is_pinned() noexcept { return kPackageIdentityPinned; }

bool trust_gates_open() noexcept {
  return release_manifest_is_pinned() && self_authenticode_is_pinned() &&
         package_identity_is_pinned() && kCancellableIoProven &&
         kDurableJournalAuthority &&
         kBrokerIssuedIdentityProven && kRetainedExecutingSectionIdentityProven;
}

struct BootstrapHandles final {
  UniqueHandle read;
  UniqueHandle write;

  BootstrapHandles() = default;
  BootstrapHandles(const BootstrapHandles&) = delete;
  BootstrapHandles& operator=(const BootstrapHandles&) = delete;
  BootstrapHandles(BootstrapHandles&&) noexcept = default;
  BootstrapHandles& operator=(BootstrapHandles&&) noexcept = default;
};

bool anonymous_pipe_handle(const PipeBinding& pipe,
                           DWORD expected_server_pid,
                           DWORD expected_client_pid,
                           UniqueHandle& qualified) noexcept {
  HANDLE handle = pipe.handle;
  if (!trust_gates_open() || handle == nullptr ||
      handle == INVALID_HANDLE_VALUE ||
      pipe.server_pid != expected_server_pid ||
      pipe.client_pid != expected_client_pid || pipe.server_pid == 0 ||
      pipe.client_pid == 0 || pipe.server_creation_time == 0)
    return false;
  DWORD handle_flags = 0;
  if (!GetHandleInformation(handle, &handle_flags) ||
      (handle_flags & HANDLE_FLAG_INHERIT) == 0 ||
      (handle_flags & HANDLE_FLAG_PROTECT_FROM_CLOSE) != 0) return false;
  DWORD pipe_flags = 0;
  DWORD read_bytes = 0;
  DWORD write_bytes = 0;
  if (!GetNamedPipeInfo(handle, &pipe_flags, &read_bytes, &write_bytes,
                        nullptr) || (pipe_flags & PIPE_SERVER_END) != 0)
    return false;
  DWORD server_pid = 0;
  DWORD client_pid = 0;
  if (GetNamedPipeServerProcessId(handle, &server_pid) == 0 ||
      GetNamedPipeClientProcessId(handle, &client_pid) == 0 ||
      server_pid != expected_server_pid || client_pid != expected_client_pid)
    return false;
  // Buffer sizes describe the pipe, not this endpoint's access rights.  Use
  // documented DuplicateHandle desired-access semantics: the requested side
  // must duplicate, while the opposite access must not.  The same qualified
  // duplicate is returned for adoption; it is never validated and reopened
  // through a second handle.
  const DWORD desired = pipe.direction == PipeDirection::kReadFromChild
      ? GENERIC_READ : GENERIC_WRITE;
  const DWORD opposite = pipe.direction == PipeDirection::kReadFromChild
      ? GENERIC_WRITE : GENERIC_READ;
  HANDLE raw_qualified = nullptr;
  if (!DuplicateHandle(GetCurrentProcess(), handle, GetCurrentProcess(),
                       &raw_qualified, desired, FALSE, 0))
    return false;
  UniqueHandle candidate(raw_qualified);
  HANDLE raw_opposite = nullptr;
  if (DuplicateHandle(GetCurrentProcess(), handle, GetCurrentProcess(),
                      &raw_opposite, opposite, FALSE, 0)) {
    CloseHandle(raw_opposite);
    return false;
  }
  // A failed duplicate proves absence of the opposite access only for the
  // documented access-denied result. Resource exhaustion, invalid handles,
  // and transient failures are unproven and refuse this bootstrap.
  if (GetLastError() != ERROR_ACCESS_DENIED) return false;
  if (!SetHandleInformation(candidate.get(), HANDLE_FLAG_INHERIT, 0))
    return false;
  if (GetFileType(candidate.get()) != FILE_TYPE_PIPE ||
      read_bytes > kMaxFrameBytes || write_bytes > kMaxFrameBytes)
    return false;
  qualified = std::move(candidate);
  return true;
}

bool validate_bootstrap(const BootstrapProof& proof,
                        BootstrapHandles& adopted) noexcept;

bool executable_identity_bound(const FixedIdentity& expected,
                               const FixedIdentity& observed) noexcept;

bool identity_shape(HANDLE handle, FixedIdentity& identity) noexcept {
  if (!trust_gates_open() || !handle || handle == INVALID_HANDLE_VALUE ||
      GetFileType(handle) != FILE_TYPE_DISK)
    return false;
  FILE_ATTRIBUTE_TAG_INFO tag{};
  FILE_STANDARD_INFO standard{};
  FILE_ID_INFO file{};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &tag,
                                    sizeof(tag)) ||
      !GetFileInformationByHandleEx(handle, FileStandardInfo, &standard,
                                    sizeof(standard)) ||
      !GetFileInformationByHandleEx(handle, FileIdInfo, &file, sizeof(file)))
    return false;
  constexpr DWORD rejected = FILE_ATTRIBUTE_DIRECTORY |
      FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE |
      FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_SPARSE_FILE |
      FILE_ATTRIBUTE_TEMPORARY;
  if ((tag.FileAttributes & rejected) != 0 || standard.Directory ||
      standard.DeletePending || standard.NumberOfLinks != 1 ||
      standard.EndOfFile.QuadPart <= 0 ||
      static_cast<std::uint64_t>(standard.EndOfFile.QuadPart) > kMaxIdentityFileBytes)
    return false;
  identity.volume_serial = file.VolumeSerialNumber;
  for (std::size_t i = 0; i < identity.file_id.size(); ++i)
    identity.file_id[i] = static_cast<std::byte>(file.FileId.Identifier[i]);
  std::memcpy(&identity.file_index, identity.file_id.data(),
              sizeof(identity.file_index));
  identity.size = static_cast<std::uint32_t>(standard.EndOfFile.QuadPart);
  identity.regular = identity.volume_serial != 0 &&
      !std::all_of(identity.file_id.begin(), identity.file_id.end(),
                   [](std::byte value) { return value == std::byte{}; });
  identity.reparse_free = (tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) == 0;
  // The broker-issued digest is over this exact retained identity, not a
  // pathname. A future implementation may replace this reference digest with
  // a full file hash, but must retain the same handle and recheck it before use.
  std::array<std::byte, 32> material{};
  std::memcpy(material.data(), identity.file_id.data(), identity.file_id.size());
  std::memcpy(material.data() + 16, &identity.volume_serial,
              sizeof(identity.volume_serial));
  std::memcpy(material.data() + 24, &identity.size, sizeof(identity.size));
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_bytes = 0;
  DWORD returned = 0;
  std::array<UCHAR, 4096> object{};
  const bool hashed =
      BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0 &&
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_bytes),
                        sizeof(object_bytes), &returned, 0) == 0 &&
      object_bytes > 0 && object_bytes <= object.size() &&
      BCryptCreateHash(algorithm, &hash, object.data(), object_bytes, nullptr, 0, 0) == 0 &&
      BCryptHashData(hash, reinterpret_cast<PUCHAR>(material.data()),
                     static_cast<ULONG>(material.size()), 0) == 0 &&
      BCryptFinishHash(hash, reinterpret_cast<PUCHAR>(identity.digest.data()),
                       static_cast<ULONG>(identity.digest.size()), 0) == 0;
  if (hash) BCryptDestroyHash(hash);
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  SecureZeroMemory(object.data(), object.size());
  SecureZeroMemory(material.data(), material.size());
  return hashed && identity.regular && identity.reparse_free;
}

bool observe_process_binding(DWORD pid, ProcessBinding& output) noexcept {
  // A path query followed by a pathname reopen is TOCTOU-racy.  Refuse until
  // a retained executing section/file identity is supplied by the broker.
  if (!trust_gates_open() || !kRetainedExecutingSectionIdentityProven ||
      pid == 0 || pid == GetCurrentProcessId()) return false;
  UniqueHandle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                                   FALSE, pid));
  if (!process) return false;
  FILETIME created{}, exited{}, kernel{}, user{};
  if (!GetProcessTimes(process.get(), &created, &exited, &kernel, &user)) return false;
  output.pid = pid;
  std::memcpy(&output.creation_time, &created, sizeof(output.creation_time));
  if (output.creation_time == 0 || !ProcessIdToSessionId(pid, &output.session_id))
    return false;
  std::array<wchar_t, 32768> path{};
  DWORD path_size = static_cast<DWORD>(path.size());
  if (!QueryFullProcessImageNameW(process.get(), 0, path.data(), &path_size) ||
      path_size == 0 || path_size >= path.size()) return false;
  UniqueHandle image(CreateFileW(path.data(), GENERIC_READ | FILE_READ_ATTRIBUTES,
                                 FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                                 FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
                                 nullptr));
  if (!image || !identity_shape(image.get(), output.image)) return false;
  return true;
}

bool token_sid_matches(HANDLE token,
                       const std::array<std::byte, kCapabilityBytes>& digest) noexcept {
  if (!trust_gates_open()) return false;
  std::array<std::byte, SECURITY_MAX_SID_SIZE + sizeof(TOKEN_USER)> data{};
  DWORD returned = 0;
  if (!GetTokenInformation(token, TokenUser, data.data(),
                           static_cast<DWORD>(data.size()), &returned) ||
      returned < sizeof(TOKEN_USER))
    return false;
  const auto* user = reinterpret_cast<const TOKEN_USER*>(data.data());
  if (!IsValidSid(user->User.Sid)) return false;

  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_bytes = 0;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
                                  0) != 0 ||
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_bytes),
                        sizeof(object_bytes), &returned, 0) != 0 ||
      object_bytes == 0 || object_bytes > 4096) {
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    return false;
  }
  std::array<UCHAR, 4096> object{};
  std::array<std::byte, kCapabilityBytes> actual{};
  const DWORD sid_bytes = GetLengthSid(user->User.Sid);
  const bool ok =
      BCryptCreateHash(algorithm, &hash, object.data(), object_bytes, nullptr,
                       0, 0) == 0 &&
      BCryptHashData(hash, reinterpret_cast<PUCHAR>(user->User.Sid), sid_bytes, 0) ==
          0 &&
      BCryptFinishHash(hash, reinterpret_cast<PUCHAR>(actual.data()),
                       static_cast<ULONG>(actual.size()), 0) == 0 &&
      std::equal(actual.begin(), actual.end(), digest.begin());
  if (hash) BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  SecureZeroMemory(object.data(), object.size());
  SecureZeroMemory(actual.data(), actual.size());
  return ok;
}

bool process_token_sid_matches(
    DWORD pid, const std::array<std::byte, kCapabilityBytes>& digest) noexcept {
  if (!trust_gates_open() || pid == 0) return false;
  UniqueHandle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid));
  if (!process) return false;
  HANDLE raw_token = nullptr;
  if (!OpenProcessToken(process.get(), TOKEN_QUERY, &raw_token)) return false;
  UniqueHandle token(raw_token);
  return token_sid_matches(token.get(), digest);
}

bool validate_bootstrap(const BootstrapProof& proof,
                        BootstrapHandles& adopted) noexcept {
  if (!trust_gates_open()) return false;
  const DWORD current_pid = GetCurrentProcessId();
  if (!proof.inherited_only || proof.parent.pid == 0 ||
      proof.pipe_server.pid != proof.parent.pid ||
      proof.parent.pid == current_pid || proof.parent.creation_time == 0 ||
      proof.pipe_server.creation_time != proof.parent.creation_time ||
      proof.parent.session_id == 0 ||
      proof.pipe_server.session_id != proof.parent.session_id ||
      proof.pipe_server.token_sid_digest != proof.parent.token_sid_digest ||
      proof.control_read.handle == proof.control_write.handle ||
      proof.control_read.direction != PipeDirection::kReadFromChild ||
      proof.control_write.direction != PipeDirection::kWriteToChild ||
      proof.control_read.server_creation_time != proof.parent.creation_time ||
      proof.control_write.server_creation_time != proof.parent.creation_time ||
      !proof.parent.image.regular || !proof.parent.image.reparse_free ||
      !proof.pipe_server.image.regular || !proof.pipe_server.image.reparse_free)
    return false;

  ProcessBinding observed_parent{};
  ProcessBinding observed_pipe_server{};
  if (!observe_process_binding(proof.parent.pid, observed_parent) ||
      !observe_process_binding(proof.pipe_server.pid, observed_pipe_server) ||
      observed_parent.creation_time != proof.parent.creation_time ||
      observed_parent.session_id != proof.parent.session_id ||
      observed_pipe_server.creation_time != proof.pipe_server.creation_time ||
      observed_pipe_server.session_id != proof.pipe_server.session_id ||
      !executable_identity_bound(observed_parent.image, proof.parent.image) ||
      !executable_identity_bound(observed_pipe_server.image, proof.pipe_server.image))
    return false;
  DWORD current_session = 0;
  if (!ProcessIdToSessionId(GetCurrentProcessId(), &current_session) ||
      current_session != proof.parent.session_id)
    return false;

  if (!process_token_sid_matches(proof.parent.pid, proof.parent.token_sid_digest))
    return false;
  if (!process_token_sid_matches(proof.pipe_server.pid,
                                 proof.pipe_server.token_sid_digest))
    return false;
  UniqueHandle parent_process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                           FALSE, proof.parent.pid));
  if (!parent_process) return false;
  DWORD token_session = 0;
  DWORD token_size = sizeof(token_session);
  HANDLE raw_token = nullptr;
  if (!OpenProcessToken(parent_process.get(), TOKEN_QUERY, &raw_token)) return false;
  UniqueHandle parent_token(raw_token);
  if (GetTokenInformation(parent_token.get(), TokenSessionId, &token_session,
                          token_size, &token_size) == 0 ||
      token_session != proof.parent.session_id) return false;
  adopted.read.reset();
  adopted.write.reset();
  if (!anonymous_pipe_handle(proof.control_read, proof.pipe_server.pid,
                             current_pid, adopted.read) ||
      !anonymous_pipe_handle(proof.control_write, proof.pipe_server.pid,
                             current_pid, adopted.write)) {
    adopted.read.reset();
    adopted.write.reset();
    return false;
  }
  return true;
}

bool adopt_bootstrap(BootstrapProof& proof, BootstrapHandles& adopted) noexcept {
  if (!validate_bootstrap(proof, adopted)) return false;
  // The same qualified duplicates validated above become supervisor-owned.
  // Inherited originals are closed only after both transfers are complete.
  const HANDLE original_read = proof.control_read.handle;
  const HANDLE original_write = proof.control_write.handle;
  const bool closed_read =
      !original_read || original_read == INVALID_HANDLE_VALUE ||
      CloseHandle(original_read) != FALSE;
  const bool closed_write =
      !original_write || original_write == INVALID_HANDLE_VALUE ||
      CloseHandle(original_write) != FALSE;
  if (!closed_read || !closed_write) bootstrap_transfer_fail_stop();
  proof.control_read.handle = INVALID_HANDLE_VALUE;
  proof.control_write.handle = INVALID_HANDLE_VALUE;
  proof.inherited_only = false;
  return true;
}

bool fill_random(void* bytes, ULONG size) noexcept {
  return trust_gates_open() && bytes != nullptr && size <= kMaxFrameBytes &&
         BCryptGenRandom(nullptr, reinterpret_cast<PUCHAR>(bytes), size,
                         BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0;
}

bool valid_scope(std::uint32_t scope) noexcept {
  return scope != 0 && (scope & ~kAllScopes) == 0 &&
         (scope & (scope - 1u)) == 0;
}

bool nonzero_digest(const std::array<std::byte, kMaxDigestBytes>& digest) noexcept {
  std::byte aggregate{};
  for (const auto value : digest) aggregate |= value;
  return aggregate != std::byte{};
}

bool constant_time_equal(const std::array<std::byte, kCapabilityBytes>& left,
                         const std::array<std::byte, kCapabilityBytes>& right) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < left.size(); ++index)
    difference |= static_cast<std::uint8_t>(left[index] ^ right[index]);
  return difference == 0;
}

bool append_mac_bytes(std::array<std::byte, 256>& material, std::size_t& offset,
                      const void* bytes, std::size_t count) noexcept {
  if (bytes == nullptr || offset > material.size() || count > material.size() - offset)
    return false;
  std::memcpy(material.data() + offset, bytes, count);
  offset += count;
  return true;
}

bool append_mac_u64(std::array<std::byte, 256>& material, std::size_t& offset,
                    std::uint64_t value) noexcept {
  return append_mac_bytes(material, offset, &value, sizeof(value));
}

bool append_mac_u32(std::array<std::byte, 256>& material, std::size_t& offset,
                    std::uint32_t value) noexcept {
  return append_mac_bytes(material, offset, &value, sizeof(value));
}

bool compute_mac(const std::array<std::byte, kCapabilityBytes>& signing_key,
                 IssuedCapability& capability) noexcept {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_bytes = 0;
  DWORD returned = 0;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
                                  BCRYPT_ALG_HANDLE_HMAC_FLAG) != 0 ||
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_bytes),
                        sizeof(object_bytes), &returned, 0) != 0 ||
      object_bytes == 0 || object_bytes > 4096) {
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    return false;
  }
  std::array<UCHAR, 4096> object{};
  std::array<std::byte, 256> material{};
  std::size_t material_bytes = 0;
  constexpr char kDomain[] = "lae.windows-supervisor.capability.v1";
  const bool layout =
      append_mac_bytes(material, material_bytes, kDomain, sizeof(kDomain) - 1) &&
      append_mac_bytes(material, material_bytes, capability.id.data(), capability.id.size()) &&
      append_mac_bytes(material, material_bytes, capability.nonce.data(), capability.nonce.size()) &&
      append_mac_u64(material, material_bytes, capability.expires_at_ms) &&
      append_mac_u32(material, material_bytes, capability.operation_scope) &&
      append_mac_u64(material, material_bytes, capability.session_id) &&
      append_mac_u64(material, material_bytes, capability.operation_id) &&
      append_mac_bytes(material, material_bytes, capability.operation_digest.data(), capability.operation_digest.size()) &&
      append_mac_bytes(material, material_bytes, capability.argument_digest.data(), capability.argument_digest.size()) &&
      append_mac_bytes(material, material_bytes, capability.preview_digest.data(), capability.preview_digest.size());
  if (!layout) return false;
  const bool ok =
      BCryptCreateHash(algorithm, &hash, object.data(), object_bytes,
                       reinterpret_cast<PUCHAR>(const_cast<std::byte*>(signing_key.data())),
                       static_cast<ULONG>(signing_key.size()), 0) ==
          0 &&
      BCryptHashData(hash, reinterpret_cast<PUCHAR>(material.data()),
                     static_cast<ULONG>(material_bytes), 0) == 0 &&
      BCryptFinishHash(hash, reinterpret_cast<PUCHAR>(capability.mac.data()),
                       static_cast<ULONG>(capability.mac.size()), 0) ==
          0;
  if (hash) BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  SecureZeroMemory(object.data(), object.size());
  SecureZeroMemory(material.data(), material.size());
  return ok;
}

bool issue_capability(
    CapabilityIssuer& issuer, IssuedCapability& capability, std::uint64_t now_ms,
    std::uint64_t session_id, std::uint32_t scope, std::uint64_t operation_id,
    const std::array<std::byte, kMaxDigestBytes>& operation_digest,
    const std::array<std::byte, kMaxDigestBytes>& argument_digest,
    const std::array<std::byte, kMaxDigestBytes>& preview_digest) noexcept {
  capability.clear_secrets();
  capability.expires_at_ms = 0;
  capability.operation_scope = 0;
  capability.session_id = 0;
  capability.operation_id = 0;
  if (!trust_gates_open() || !valid_scope(scope) || session_id == 0 ||
      operation_id == 0 || now_ms > std::numeric_limits<std::uint64_t>::max() - 300000 ||
      !nonzero_digest(operation_digest) || !nonzero_digest(argument_digest) ||
      !nonzero_digest(preview_digest) || !issuer.epoch_initialized_) return false;
  if (!fill_random(capability.id.data(), capability.id.size()) ||
      !fill_random(capability.nonce.data(), capability.nonce.size())) {
    capability.clear_secrets();
    return false;
  }
  capability.session_id = session_id;
  capability.operation_scope = scope;
  capability.operation_id = operation_id;
  capability.operation_digest = operation_digest;
  capability.argument_digest = argument_digest;
  capability.preview_digest = preview_digest;
  capability.expires_at_ms = now_ms + 300000;
  if (!compute_mac(issuer.signing_key_, capability)) {
    capability.clear_secrets();
    capability.expires_at_ms = 0;
    capability.operation_scope = 0;
    capability.session_id = 0;
    capability.operation_id = 0;
    return false;
  }
  return true;
}

bool capability_valid(const CapabilityIssuer& issuer,
                      const IssuedCapability& capability, std::uint64_t now_ms,
                      std::uint64_t session_id, std::uint32_t scope,
                      std::uint64_t operation_id,
                      const std::array<std::byte, kMaxDigestBytes>& operation_digest,
                      const std::array<std::byte, kMaxDigestBytes>& argument_digest,
                      const std::array<std::byte, kMaxDigestBytes>& preview_digest) noexcept {
  if (!trust_gates_open() || session_id == 0 || !valid_scope(scope) ||
      operation_id == 0 || !nonzero_digest(operation_digest) ||
      !nonzero_digest(argument_digest) || !nonzero_digest(preview_digest) ||
      capability.session_id != session_id || capability.operation_scope != scope ||
      capability.operation_id != operation_id ||
      capability.operation_digest != operation_digest ||
      capability.argument_digest != argument_digest ||
      capability.preview_digest != preview_digest ||
      now_ms >= capability.expires_at_ms || !nonzero_digest(capability.id) ||
      !nonzero_digest(capability.nonce)) {
    return false;
  }
  IssuedCapability recomputed = capability;
  recomputed.mac.fill(std::byte{});
  return compute_mac(issuer.signing_key_, recomputed) &&
      constant_time_equal(recomputed.mac, capability.mac);
}

bool CapabilityIssuer::issue(
    IssuedCapability& capability, std::uint64_t now_ms,
    std::uint64_t session_id, std::uint32_t scope, std::uint64_t operation_id,
    const std::array<std::byte, kMaxDigestBytes>& operation_digest,
    const std::array<std::byte, kMaxDigestBytes>& argument_digest,
    const std::array<std::byte, kMaxDigestBytes>& preview_digest) noexcept {
  return issue_capability(*this, capability, now_ms, session_id, scope,
                          operation_id, operation_digest, argument_digest,
                          preview_digest);
}

bool CapabilityIssuer::verify(
    const IssuedCapability& capability, std::uint64_t now_ms,
    std::uint64_t session_id, std::uint32_t scope, std::uint64_t operation_id,
    const std::array<std::byte, kMaxDigestBytes>& operation_digest,
    const std::array<std::byte, kMaxDigestBytes>& argument_digest,
    const std::array<std::byte, kMaxDigestBytes>& preview_digest) const noexcept {
  return capability_valid(*this, capability, now_ms, session_id, scope,
                          operation_id, operation_digest, argument_digest,
                          preview_digest);
}

bool CapabilityVerifier::verify(
    const IssuedCapability& capability, std::uint64_t now_ms,
    std::uint64_t session_id, std::uint32_t scope, std::uint64_t operation_id,
    const std::array<std::byte, kMaxDigestBytes>& operation_digest,
    const std::array<std::byte, kMaxDigestBytes>& argument_digest,
    const std::array<std::byte, kMaxDigestBytes>& preview_digest) const noexcept {
  return issuer_ != nullptr &&
      issuer_->verify(capability, now_ms, session_id, scope, operation_id,
                      operation_digest, argument_digest, preview_digest);
}

class NonceReplaySet final {
 public:
  ~NonceReplaySet() {
    std::lock_guard lock(mutex_);
    for (auto& nonce : used_) SecureZeroMemory(nonce.data(), nonce.size());
  }

  bool consume(const IssuedCapability& capability) noexcept {
    std::lock_guard lock(mutex_);
    for (const auto& nonce : used_)
      if (constant_time_equal(nonce, capability.nonce)) return false;
    if (used_.size() >= kMaxJournalRecords) return false;
    try {
      used_.push_back(capability.nonce);
    } catch (...) {
      return false;
    }
    return true;
  }

 private:
  std::mutex mutex_;
  std::vector<std::array<std::byte, kCapabilityBytes>> used_;
};

HANDLE create_root_job() noexcept {
  if (!trust_gates_open()) return nullptr;
  HANDLE root = CreateJobObjectW(nullptr, nullptr);
  if (!root) return nullptr;
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags =
      JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
  limits.BasicLimitInformation.ActiveProcessLimit = kMaxChildren;
  if (!SetInformationJobObject(root, JobObjectExtendedLimitInformation,
                               &limits, sizeof(limits))) {
    CloseHandle(root);
    return nullptr;
  }
  return root;
}

HANDLE create_child_job(HANDLE root_job) noexcept {
  if (!trust_gates_open() || !root_job || !kNestedJobPolicyProven) return nullptr;
  // The current policy is one direct root Job with a bounded active-process
  // count. Nested jobs are refused until a signed build proves Windows nested
  // job semantics and assigns every child before its first instruction.
  return nullptr;
}

bool assign_and_verify_root_job(HANDLE root_job, HANDLE process) noexcept {
  if (!trust_gates_open() || !root_job || !process) return false;
  // The caller must invoke this while the process is still suspended.  A
  // boolean supplied by a caller is not evidence of containment.
  if (!AssignProcessToJobObject(root_job, process)) return false;
  BOOL in_job = FALSE;
  return IsProcessInJob(process, root_job, &in_job) != FALSE && in_job != FALSE;
}

bool root_job_policy_proven(HANDLE root_job) noexcept {
  if (!trust_gates_open() || !root_job) return false;
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  if (!QueryInformationJobObject(root_job, JobObjectExtendedLimitInformation,
                                 &limits, sizeof(limits), nullptr))
    return false;
  const DWORD required = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
      JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
  return (limits.BasicLimitInformation.LimitFlags & required) == required &&
      limits.BasicLimitInformation.ActiveProcessLimit == kMaxChildren &&
      !kNestedJobPolicyProven;
}

bool cleanup_deadline_valid(std::uint64_t deadline_at_ms) noexcept {
  const std::uint64_t now = GetTickCount64();
  return deadline_at_ms > now &&
      deadline_at_ms - now <= kMaxCleanupHorizonMs;
}

bool wait_reaped_until(HANDLE process, std::uint64_t deadline_at_ms,
                       HANDLE cancellation = nullptr) noexcept {
  if (!process || !cleanup_deadline_valid(deadline_at_ms)) return false;
  while (GetTickCount64() < deadline_at_ms) {
    const ULONGLONG now = GetTickCount64();
    const DWORD remaining = static_cast<DWORD>(deadline_at_ms - now);
    const DWORD wait_ms = (remaining < kWaitSliceMs) ? remaining : kWaitSliceMs;
    HANDLE handles[2] = {process, cancellation};
    const DWORD count = cancellation ? 2u : 1u;
    const DWORD result = WaitForMultipleObjects(count, handles, FALSE, wait_ms);
    if (result == WAIT_OBJECT_0) return GetTickCount64() < deadline_at_ms;
    if (cancellation && result == WAIT_OBJECT_0 + 1) return false;
    if (result != WAIT_TIMEOUT) return false;
  }
  return false;
}

bool job_empty(HANDLE job) noexcept {
  if (!job) return false;
  JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info{};
  if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                 &info, sizeof(info), nullptr))
    return false;
  return info.ActiveProcesses == 0;
}

bool wait_job_empty_until(HANDLE root_job,
                          std::uint64_t deadline_at_ms) noexcept {
  if (!root_job || !cleanup_deadline_valid(deadline_at_ms)) return false;
  do {
    const std::uint64_t now = GetTickCount64();
    if (now >= deadline_at_ms) return false;
    if (job_empty(root_job)) return GetTickCount64() < deadline_at_ms;
    Sleep(static_cast<DWORD>(std::min<std::uint64_t>(kWaitSliceMs,
                                                     deadline_at_ms - now)));
  } while (true);
}

DWORD WINAPI drain_child_pipe(void* opaque) noexcept {
  auto* context = static_cast<DrainContext*>(opaque);
  if (!context || !context->source || context->source == INVALID_HANDLE_VALUE)
    return ERROR_INVALID_HANDLE;
  DWORD total = 0;
  for (;;) {
    if (context->limit == 0 || context->limit > context->bytes.size()) {
      context->error.store(ERROR_INVALID_DATA, std::memory_order_release);
      return ERROR_INVALID_DATA;
    }
    if (total == context->limit) {
      // Probe one byte without appending it. Exact-limit output followed by
      // EOF is valid; only an actual additional byte is overflow.
      std::byte probe{};
      DWORD read = 0;
      if (!ReadFile(context->source, &probe, 1, &read, nullptr)) {
        const DWORD error = GetLastError();
        SecureZeroMemory(&probe, sizeof(probe));
        if (error == ERROR_BROKEN_PIPE || error == ERROR_HANDLE_EOF) {
          context->captured.store(total, std::memory_order_release);
          context->error.store(ERROR_SUCCESS, std::memory_order_release);
          return ERROR_SUCCESS;
        }
        context->error.store(error, std::memory_order_release);
        return error;
      }
      if (!drain_result_usable(*context)) {
        SecureZeroMemory(&probe, sizeof(probe));
        context->error.store(ERROR_OPERATION_ABORTED,
                             std::memory_order_release);
        return ERROR_OPERATION_ABORTED;
      }
      SecureZeroMemory(&probe, sizeof(probe));
      if (read == 0) {
        context->captured.store(total, std::memory_order_release);
        context->error.store(ERROR_SUCCESS, std::memory_order_release);
        return ERROR_SUCCESS;
      }
      context->overflow.store(true, std::memory_order_release);
      context->error.store(ERROR_BUFFER_OVERFLOW, std::memory_order_release);
      return ERROR_BUFFER_OVERFLOW;
    }
    DWORD read = 0;
    const DWORD room = context->limit - total;
    if (!ReadFile(context->source, context->bytes.data() + total, room,
                  &read, nullptr)) {
      const DWORD error = GetLastError();
      if (error == ERROR_BROKEN_PIPE || error == ERROR_HANDLE_EOF) {
        context->captured.store(total, std::memory_order_release);
        context->error.store(ERROR_SUCCESS, std::memory_order_release);
        return ERROR_SUCCESS;
      }
      context->captured.store(total, std::memory_order_release);
      context->error.store(error, std::memory_order_release);
      return error;
    }
    if (!drain_result_usable(*context)) {
      SecureZeroMemory(context->bytes.data() + total, read);
      context->error.store(ERROR_OPERATION_ABORTED,
                           std::memory_order_release);
      return ERROR_OPERATION_ABORTED;
    }
    if (read == 0) {
      context->captured.store(total, std::memory_order_release);
      context->error.store(ERROR_SUCCESS, std::memory_order_release);
      return ERROR_SUCCESS;
    }
    total += read;
    context->captured.store(total, std::memory_order_release);
  }
}

bool settle_drain_worker(UniqueHandle& worker, DrainContext* context,
                         std::uint64_t deadline_at_ms) noexcept {
  const std::uint64_t started = GetTickCount64();
  if (!context || !worker || started >= deadline_at_ms) return false;
  DWORD maximum_ms = static_cast<DWORD>(std::min<std::uint64_t>(
      kWorkerSettlementMs, deadline_at_ms - started));
  DWORD wait = WaitForSingleObject(worker.get(), 0);
  if (wait == WAIT_TIMEOUT) {
    // Cancel the exact worker thread that owns the synchronous ReadFile. Its
    // heap context remains registry-owned until the thread is proven stopped.
    if (!CancelSynchronousIo(worker.get()) && GetLastError() != ERROR_NOT_FOUND)
      return false;
    const std::uint64_t now = GetTickCount64();
    if (now >= deadline_at_ms) return false;
    maximum_ms = static_cast<DWORD>(std::min<std::uint64_t>(
        maximum_ms, deadline_at_ms - now));
    wait = WaitForSingleObject(worker.get(), maximum_ms);
  }
  if (wait != WAIT_OBJECT_0) return false;
  if (GetTickCount64() >= deadline_at_ms) return false;
  DWORD code = ERROR_GEN_FAILURE;
  if (!GetExitCodeThread(worker.get(), &code) || code == STILL_ACTIVE)
    return false;
  if (GetTickCount64() >= deadline_at_ms) return false;
  return code == ERROR_SUCCESS || code == ERROR_OPERATION_ABORTED ||
      code == ERROR_BROKEN_PIPE || code == ERROR_HANDLE_EOF ||
      code == ERROR_BUFFER_OVERFLOW || code == ERROR_INVALID_DATA;
}

bool settle_child_drains(Child& child,
                         std::uint64_t deadline_at_ms) noexcept {
  if (!child.stdout_worker && !child.stderr_worker &&
      !child.stdout_context && !child.stderr_context) return true;
  if (!child.stdout_worker || !child.stderr_worker ||
      !child.stdout_context || !child.stderr_context) return false;
  const bool stdout_settled = settle_drain_worker(
      child.stdout_worker, child.stdout_context.get(), deadline_at_ms);
  const bool stderr_settled = settle_drain_worker(
      child.stderr_worker, child.stderr_context.get(), deadline_at_ms);
  if (!stdout_settled || !stderr_settled) return false;
  child.stdout_bytes = child.stdout_context->captured.load(std::memory_order_acquire);
  child.stderr_bytes = child.stderr_context->captured.load(std::memory_order_acquire);
  child.output_overflow =
      child.stdout_context->overflow.load(std::memory_order_acquire) ||
      child.stderr_context->overflow.load(std::memory_order_acquire);
  child.stdout_worker.reset();
  child.stderr_worker.reset();
  child.stdout_read.reset();
  child.stderr_read.reset();
  child.stdout_context.release_after_join();
  child.stderr_context.release_after_join();
  return true;
}

class ChildRegistry final {
 public:
  using RegisteredOperation = bool (*)(Child&, HANDLE, void*) noexcept;
  struct RegisteredOutcome final {
    std::uint32_t stdout_bytes = 0;
    std::uint32_t stderr_bytes = 0;
    bool output_overflow = false;
  };

  bool insert(Child& child, HANDLE root_job) noexcept {
    std::lock_guard lock(mutex_);
    if (children_.size() >= kMaxChildren || child.stable_id == 0 ||
        !child.process || !child.primary_thread || !child.stdout_read ||
        !child.stderr_read || !child.stdout_context || !child.stderr_context ||
        !root_job ||
        children_.find(child.stable_id) != children_.end())
      return false;
    // Never trust a caller-provided boolean: membership is queried against
    // this exact root handle immediately before registry insertion.
    if (!verify_membership(child.process.get(), root_job)) return false;
    child.membership_verified = true;
    try {
      // Allocate and publish an empty slot before transferring any retained
      // handle. If allocation fails, the caller still owns the complete
      // suspended child and can terminate the root Job. Move assignment is
      // noexcept, so a successful slot reservation cannot strand ownership
      // between the caller and registry.
      auto [slot, inserted] = children_.try_emplace(child.stable_id);
      if (!inserted) return false;
      slot->second = std::move(child);
      return true;
    } catch (...) {
      return false;
    }
  }

  bool operate_and_unregister(std::uint64_t stable_id, HANDLE root_job,
                              RegisteredOperation operation,
                              void* context,
                              std::uint64_t settlement_deadline_at_ms,
                              RegisteredOutcome& outcome) noexcept {
    std::lock_guard lock(mutex_);
    const auto found = children_.find(stable_id);
    if (found == children_.end() || !root_job || !operation ||
        !verify_membership(found->second.process.get(), root_job)) return false;
    Child& child = found->second;
    if (!operation(child, root_job, context) ||
        WaitForSingleObject(child.process.get(), 0) != WAIT_OBJECT_0 ||
        !job_empty(root_job) ||
        !settle_child_drains(child, settlement_deadline_at_ms)) return false;
    outcome.stdout_bytes = child.stdout_bytes;
    outcome.stderr_bytes = child.stderr_bytes;
    outcome.output_overflow = child.output_overflow;
    children_.erase(found);
    return true;
  }

  static bool verify_membership(HANDLE process, HANDLE root_job) noexcept {
    if (!process || !root_job) return false;
    BOOL in_job = FALSE;
    return IsProcessInJob(process, root_job, &in_job) != FALSE && in_job != FALSE;
  }

  bool unregister(std::uint64_t stable_id, HANDLE root_job) noexcept {
    std::lock_guard lock(mutex_);
    const auto found = children_.find(stable_id);
    if (found == children_.end() ||
        WaitForSingleObject(found->second.process.get(), 0) != WAIT_OBJECT_0 ||
        !verify_membership(found->second.process.get(), root_job)) return false;
    children_.erase(found);
    return true;
  }

  bool terminate_and_reap(std::uint64_t stable_id, HANDLE root_job,
                          std::uint64_t cleanup_deadline_at_ms,
                          HANDLE cancellation = nullptr) noexcept {
    std::lock_guard lock(mutex_);
    if (!root_job || !cleanup_deadline_valid(cleanup_deadline_at_ms)) return false;
    (void)cancellation;
    const auto found = children_.find(stable_id);
    if (found == children_.end()) return false;
    Child& child = found->second;
    // Once the root is terminated, cancellation cannot shorten mandatory
    // whole-tree reap. The hard deadline remains the only bound.
    if (!TerminateJobObject(root_job, 1) ||
        !wait_reaped_until(child.process.get(), cleanup_deadline_at_ms, nullptr) ||
        !wait_job_empty_until(root_job, cleanup_deadline_at_ms) ||
        !settle_child_drains(child, cleanup_deadline_at_ms)) return false;
    children_.erase(found);
    return true;
  }

  bool terminate_and_reap_all(HANDLE root_job,
                              std::uint64_t cleanup_deadline_at_ms,
                              HANDLE cancellation = nullptr) noexcept {
    std::lock_guard lock(mutex_);
    if (!root_job || !cleanup_deadline_valid(cleanup_deadline_at_ms)) return false;
    (void)cancellation;
    // Issue exactly one root termination. Repeating it per child can race the
    // accounting query and falsely reject a tree that is still draining.
    bool ok = TerminateJobObject(root_job, 1) != FALSE;
    for (auto& [ignored, child] : children_) {
      (void)ignored;
      const ULONGLONG now = GetTickCount64();
      // Cancellation is intentionally ignored during mandatory reap; returning
      // early here would leave a kill-on-close root live.
      if (now >= cleanup_deadline_at_ms ||
          !wait_reaped_until(child.process.get(), cleanup_deadline_at_ms, nullptr))
        ok = false;
      if (!settle_child_drains(child, cleanup_deadline_at_ms)) ok = false;
    }
    if (!wait_job_empty_until(root_job, cleanup_deadline_at_ms))
      ok = false;
    return ok;
  }

  void close_all() noexcept {
    std::lock_guard lock(mutex_);
    for (auto& [ignored, child] : children_) {
      (void)ignored;
      if (child.stdout_worker || child.stderr_worker || child.stdout_context ||
          child.stderr_context) {
        // Destroying a live worker's heap context would be a UAF. A process
        // fail-stop is safer than inventing a fresh cleanup deadline here.
        std::terminate();
      }
      child.process.reset();
    }
    children_.clear();
  }

 private:
  std::mutex mutex_;
  std::map<std::uint64_t, Child> children_;
};

struct SupervisorState final {
  UniqueHandle root_job;
  BootstrapHandles bootstrap_handles;
  UniqueHandle cancellation;
  ChildRegistry children;
  NonceReplaySet replay;
  CapabilityIssuer issuer;
};

SupervisorState& process_state() noexcept {
  static SupervisorState state;
  return state;
}

enum class JournalState : std::uint8_t {
  kIdle,
  kStartDurable,
  kTerminalDurable,
  kUnknown,
};

enum class DispatchStatus : std::uint8_t {
  kPreDispatchFailure,
  kDispatchedUnknown,
  kTerminalFailure,
  kTerminalSuccess,
  kPersistenceFailure,
};

enum class DurableLoadResult : std::uint8_t {
  kEmpty,
  kRecord,
  kRefused,
};

struct JournalOutcome final {
  DispatchStatus status = DispatchStatus::kPreDispatchFailure;
  JournalState durable_state = JournalState::kUnknown;
  std::uint64_t sequence = 0;
};

struct JournalRecord final {
  std::uint64_t sequence = 0;
  std::uint64_t operation_id = 0;
  std::uint64_t session_id = 0;
  std::uint32_t scope = 0;
  std::array<std::byte, kMaxDigestBytes> operation_digest{};
  std::array<std::byte, kMaxDigestBytes> argument_digest{};
  std::array<std::byte, kMaxDigestBytes> preview_digest{};
  JournalState state = JournalState::kUnknown;
  std::uint32_t error_code = 0;
};

using JournalPersist = bool (*)(const JournalRecord&) noexcept;
// Recovery reads exactly one validated latest record from the private durable
// adapter.  The callback is deliberately not exposed through the public
// header; an untrusted caller cannot inject an in-memory record into the
// supervisor's authority boundary.
using JournalLoad = bool (*)(JournalRecord&) noexcept;
using DispatchOperation = DispatchStatus (*)(const IssuedCapability&) noexcept;
using OwnedDispatchOperation = DispatchStatus (*)(
    const IssuedCapability&, void*) noexcept;

// This is a module-private contract, not a hook accepted from a request. The
// supervisor will own the one implementation that talks to the authenticated
// native journal helper. No implementation is installed in this inert slice.
class DurableJournalAdapter {
 public:
  virtual ~DurableJournalAdapter() = default;
  virtual bool authority_ready() const noexcept = 0;
  virtual bool append_and_readback_exact(
      const JournalRecord& record) noexcept = 0;
  virtual DurableLoadResult load_latest_exact(
      JournalRecord& record) noexcept = 0;
};

using OwnedJournalPersist = bool (*)(DurableJournalAdapter&,
                                     const JournalRecord&, void*) noexcept;

class JournalAuthority final {
 public:
  JournalOutcome dispatch(const IssuedCapability& capability, JournalPersist persist,
                          DispatchOperation operation) noexcept {
    if (!persist || !operation) return {};
    return dispatch_impl(
        capability,
        [persist](const JournalRecord& record) noexcept {
          return persist(record);
        },
        [operation](const IssuedCapability& issued) noexcept {
          return operation(issued);
        });
  }

  JournalOutcome dispatch_owned(const IssuedCapability& capability,
                                DurableJournalAdapter& adapter,
                                OwnedJournalPersist persist,
                                OwnedDispatchOperation operation,
                                void* context) noexcept {
    if (!adapter.authority_ready() || !persist || !operation) return {};
    return dispatch_impl(
        capability,
        [&adapter, persist, context](const JournalRecord& record) noexcept {
          return persist(adapter, record, context);
        },
        [operation, context](const IssuedCapability& issued) noexcept {
          return operation(issued, context);
        });
  }

  bool recover_owned(DurableJournalAdapter& adapter) noexcept {
    if (!trust_gates_open() || !adapter.authority_ready()) return false;
    JournalRecord latest{};
    const DurableLoadResult loaded = adapter.load_latest_exact(latest);
    if (loaded == DurableLoadResult::kEmpty) {
      return sequence_ == 0 && state_ == JournalState::kIdle;
    }
    if (loaded != DurableLoadResult::kRecord) {
      state_ = JournalState::kUnknown;
      return false;
    }
    return recover_record(latest);
  }

 private:
  template <typename Persist, typename Operation>
  JournalOutcome dispatch_impl(const IssuedCapability& capability,
                               Persist persist,
                               Operation operation) noexcept {
    JournalOutcome outcome{};
    if (!trust_gates_open() || state_ != JournalState::kIdle ||
        capability.operation_id == 0 || !valid_scope(capability.operation_scope))
      return outcome;
    // Sequence exhaustion must be decided before the operation callback. At
    // UINT64_MAX-1 (and UINT64_MAX), no provider code may be invoked.
    if (!can_reserve_sequences(kRequiredJournalSequences)) {
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kPreDispatchFailure, state_, 0};
    }
    JournalRecord start = record(capability, JournalState::kStartDurable, 0);
    if (start.sequence == 0) {
      state_ = JournalState::kUnknown;
      return outcome;
    }
    if (!persist(start)) {
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                            start.sequence};
    }
    state_ = JournalState::kStartDurable;
    const DispatchStatus operation_status = operation(capability);
    if (operation_status == DispatchStatus::kPreDispatchFailure) {
      JournalRecord failed = record(capability, JournalState::kTerminalDurable, 1);
      if (failed.sequence == 0) {
        state_ = JournalState::kUnknown;
        return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                              sequence_};
      }
      if (!persist(failed)) {
        state_ = JournalState::kUnknown;
        return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                              failed.sequence};
      }
      state_ = JournalState::kTerminalDurable;
      return JournalOutcome{DispatchStatus::kTerminalFailure, state_, failed.sequence};
    }
    if (operation_status == DispatchStatus::kDispatchedUnknown) {
      JournalRecord unknown = record(capability, JournalState::kUnknown, 2);
      if (unknown.sequence == 0) {
        state_ = JournalState::kUnknown;
        return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                              sequence_};
      }
      if (!persist(unknown)) {
        state_ = JournalState::kUnknown;
        return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                              unknown.sequence};
      }
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kDispatchedUnknown, state_, unknown.sequence};
    }
    if (operation_status != DispatchStatus::kTerminalSuccess &&
        operation_status != DispatchStatus::kTerminalFailure) {
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kDispatchedUnknown, state_,
                            sequence_};
    }
    const std::uint32_t error =
        operation_status == DispatchStatus::kTerminalFailure ? 1u : 0u;
    JournalRecord completed = record(capability, JournalState::kTerminalDurable, error);
    if (completed.sequence == 0) {
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                            sequence_};
    }
    if (!persist(completed)) {
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kPersistenceFailure, state_,
                            completed.sequence};
    }
    state_ = JournalState::kTerminalDurable;
    return JournalOutcome{operation_status, state_, completed.sequence};
  }

 public:
  JournalState query() const noexcept { return state_; }

  // A terminal record closes one operation.  The private adapter may then
  // start a distinct operation with a higher durable sequence; replaying the
  // previous capability is still blocked by NonceReplaySet.
  bool begin_next_operation() noexcept {
    if (!trust_gates_open() || state_ != JournalState::kTerminalDurable)
      return false;
    state_ = JournalState::kIdle;
    return true;
  }

  // Rehydrate authority after restart from the adapter's latest durable
  // record.  A durable start is intentionally treated as dispatched-unknown:
  // a crash can occur after the adapter flushes start and before the provider
  // effect is observed, so it must never be downgraded to pre-dispatch.
  bool recover(JournalLoad load) noexcept {
    if (!trust_gates_open() || !load) return false;
    JournalRecord latest{};
    if (!load(latest)) {
      state_ = JournalState::kUnknown;
      return false;
    }
    return recover_record(latest);
  }

 private:
  bool recover_record(const JournalRecord& latest) noexcept {
    if (latest.sequence == 0 || latest.operation_id == 0 ||
        !valid_scope(latest.scope) || !nonzero_digest(latest.operation_digest) ||
        !nonzero_digest(latest.argument_digest) ||
        !nonzero_digest(latest.preview_digest) ||
        (latest.state != JournalState::kStartDurable &&
         latest.state != JournalState::kTerminalDurable &&
         latest.state != JournalState::kUnknown)) {
      state_ = JournalState::kUnknown;
      return false;
    }
    sequence_ = latest.sequence;
    state_ = latest.state == JournalState::kStartDurable
        ? JournalState::kUnknown : latest.state;
    return true;
  }

 public:

  bool recovery_required() const noexcept {
    return state_ == JournalState::kUnknown;
  }

  bool acknowledge() const noexcept {
    return state_ == JournalState::kTerminalDurable;
  }


 private:

  bool can_reserve_sequences(std::uint64_t count) const noexcept {
    return count != 0 && sequence_ <=
        std::numeric_limits<std::uint64_t>::max() - count;
  }

  JournalRecord record(const IssuedCapability& capability, JournalState state,
                       std::uint32_t error) noexcept {
    JournalRecord value{};
    if (sequence_ == std::numeric_limits<std::uint64_t>::max()) {
      state_ = JournalState::kUnknown;
      return value;
    }
    value.sequence = ++sequence_;
    value.operation_id = capability.operation_id;
    value.session_id = capability.session_id;
    value.scope = capability.operation_scope;
    value.operation_digest = capability.operation_digest;
    value.argument_digest = capability.argument_digest;
    value.preview_digest = capability.preview_digest;
    value.state = state;
    value.error_code = error;
    return value;
  }

  JournalState state_ = JournalState::kIdle;
  std::uint64_t sequence_ = 0;
};

bool consume_capability(
    SupervisorState& state, const IssuedCapability& capability,
    std::uint64_t now_ms, std::uint64_t session_id, std::uint32_t scope,
    std::uint64_t operation_id,
    const std::array<std::byte, kMaxDigestBytes>& operation_digest,
    const std::array<std::byte, kMaxDigestBytes>& argument_digest,
    const std::array<std::byte, kMaxDigestBytes>& preview_digest) noexcept;

#include "process_transaction.inc"

bool initialize_supervisor(SupervisorState& state,
                           BootstrapProof& bootstrap) noexcept {
  if (!trust_gates_open() || state.root_job || state.cancellation ||
      state.bootstrap_handles.read || state.bootstrap_handles.write ||
      !adopt_bootstrap(bootstrap, state.bootstrap_handles))
    return false;
  state.cancellation.reset(CreateEventW(nullptr, TRUE, FALSE, nullptr));
  if (!state.cancellation) {
    state.bootstrap_handles.read.reset();
    state.bootstrap_handles.write.reset();
    return false;
  }
  state.root_job.reset(create_root_job());
  if (!state.root_job || !root_job_policy_proven(state.root_job.get())) {
    state.root_job.reset();
    state.cancellation.reset();
    state.bootstrap_handles.read.reset();
    state.bootstrap_handles.write.reset();
    return false;
  }
  if (!state.issuer.initialize_epoch()) {
    state.root_job.reset();
    state.cancellation.reset();
    state.bootstrap_handles.read.reset();
    state.bootstrap_handles.write.reset();
    return false;
  }
  return true;
}

bool executable_identity_bound(const FixedIdentity& expected,
                               const FixedIdentity& observed) noexcept {
  return expected.regular && expected.reparse_free && observed.regular &&
         observed.reparse_free && expected.volume_serial == observed.volume_serial &&
         expected.file_id == observed.file_id &&
         expected.file_index == observed.file_index && expected.size == observed.size &&
         !std::all_of(expected.digest.begin(), expected.digest.end(),
                      [](std::byte value) { return value == std::byte{}; }) &&
         std::equal(expected.digest.begin(), expected.digest.end(),
                    observed.digest.begin());
}

bool stop_supervisor(SupervisorState& state, DWORD deadline_ms) noexcept {
  if (!trust_gates_open() || deadline_ms == 0 || deadline_ms > kMaxWaitMs ||
      !state.cancellation || !state.root_job) return false;
  const std::uint64_t now = GetTickCount64();
  if (now > std::numeric_limits<std::uint64_t>::max() - deadline_ms)
    return false;
  const std::uint64_t cleanup_deadline_at_ms = now + deadline_ms;
  ProcessLaunchAuthority& launch = process_launch_authority();
  {
    // This is the shutdown linearization point shared with process creation,
    // resume, and journal-transition claims. Publish before signalling, so a
    // failed SetEvent still stops every later fenced mutation/checkpoint.
    std::lock_guard fence(launch.mutation_fence);
    launch.shutting_down.store(true, std::memory_order_release);
    if (!SetEvent(state.cancellation.get())) return false;
  }
  // Signal before waiting for the transaction or registry lock so a monitor
  // holding the registry lock can leave its wait and retain/settle ownership.
  std::unique_lock<std::mutex> transaction_lock(launch.mutex, std::defer_lock);
  while (!transaction_lock.try_lock()) {
    const std::uint64_t observed = GetTickCount64();
    if (observed >= cleanup_deadline_at_ms) return false;
    Sleep(static_cast<DWORD>(std::min<std::uint64_t>(
        kWaitSliceMs, cleanup_deadline_at_ms - observed)));
  }
  if (!state.children.terminate_and_reap_all(
          state.root_job.get(), cleanup_deadline_at_ms,
          state.cancellation.get()) || !job_empty(state.root_job.get())) {
    // Retain the signaled event, root Job, registry, and all worker contexts.
    // A later explicit shutdown attempt may continue cleanup; no settlement is
    // claimed and kill-on-close still protects supervisor death.
    return false;
  }
  state.children.close_all();
  state.root_job.reset();
  state.cancellation.reset();
  state.bootstrap_handles.read.reset();
  state.bootstrap_handles.write.reset();
  return true;
}

JournalOutcome durable_journal_authorize(
    JournalAuthority& journal, const IssuedCapability& capability,
    JournalPersist persist, DispatchOperation operation) noexcept {
  // The journal adapter is private and must fsync/acknowledge each record.
  // Preserve every typed state for recovery; callers must not receive a bool
  // that collapses pre-dispatch failure, dispatched ambiguity, and persistence
  // failure into the same result.
  return journal.dispatch(capability, persist, operation);
}

bool consume_capability(SupervisorState& state, const IssuedCapability& capability,
                        std::uint64_t now_ms, std::uint64_t session_id,
                        std::uint32_t scope, std::uint64_t operation_id,
                        const std::array<std::byte, kMaxDigestBytes>& operation_digest,
                        const std::array<std::byte, kMaxDigestBytes>& argument_digest,
                        const std::array<std::byte, kMaxDigestBytes>& preview_digest) noexcept {
  return state.issuer.verifier().verify(
      capability, now_ms, session_id, scope, operation_id,
      operation_digest, argument_digest, preview_digest) &&
      state.replay.consume(capability);
}

}  // namespace

Status Authority::start() noexcept {
  // This check must remain before every process, token, pipe, job, or module
  // access. With source gates false, this returns without touching Windows.
  if (!trust_gates_open()) return Status::kUnavailable;

  // Latent path: validate the inherited bootstrap before creating the root
  // Job. No child creation is present; any future launch must happen only
  // after a private issuer supplies all retained identities and journal proof.
  return Status::kInvalidBootstrap;
}

Status Authority::shutdown() noexcept {
  if (!trust_gates_open()) return Status::kUnavailable;
  return stop_supervisor(process_state(), kMaxWaitMs)
      ? Status::kOk : Status::kUnknown;
}

RedactedReceipt Authority::loopback_metadata() noexcept {
  return RedactedReceipt{Status::kUnavailable, 0, false, false};
}

}  // namespace lae::windows_supervisor

#endif  // defined(_WIN32)
