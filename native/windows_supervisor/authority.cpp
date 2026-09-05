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
#include <map>
#include <mutex>
#include <limits>
#include <set>
#include <string_view>
#include <vector>

// This translation unit is intentionally absent from native/CMakeLists.txt.
// Every entry point still refuses because all release gates in authority.hpp
// are source constants false.
namespace lae::windows_supervisor {
namespace {

constexpr DWORD kWaitSliceMs = 250;
constexpr DWORD kMaxWaitMs = 120000;
// One direct root Job is the only supported containment model.  The registry
// and the Job capacity intentionally share one limit.
constexpr std::size_t kMaxChildren = 8;
constexpr std::size_t kMaxFrameBytes = 65536;
constexpr std::size_t kCapabilityBytes = 32;
constexpr std::size_t kMaxDigestBytes = 32;
constexpr std::size_t kMaxJournalRecords = 64;
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

struct Child final {
  UniqueHandle process;
  FixedIdentity executable{};
  std::uint64_t stable_id = 0;
  bool membership_verified = false;
  Child() = default;
  Child(const Child&) = delete;
  Child& operator=(const Child&) = delete;
  Child(Child&&) noexcept = default;
  Child& operator=(Child&&) noexcept = default;
};

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
      (handle_flags & HANDLE_FLAG_INHERIT) == 0) return false;
  DWORD pipe_flags = 0;
  DWORD read_bytes = 0;
  DWORD write_bytes = 0;
  if (!GetNamedPipeInfo(handle, &pipe_flags, &read_bytes, &write_bytes,
                        nullptr) || (pipe_flags & PIPE_CLIENT_END) == 0)
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
  proof.control_read.handle = INVALID_HANDLE_VALUE;
  proof.control_write.handle = INVALID_HANDLE_VALUE;
  proof.inherited_only = false;
  if (original_read && original_read != INVALID_HANDLE_VALUE)
    CloseHandle(original_read);
  if (original_write && original_write != INVALID_HANDLE_VALUE)
    CloseHandle(original_write);
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

bool wait_reaped(HANDLE process, DWORD deadline_ms,
                 HANDLE cancellation = nullptr) noexcept {
  if (!process || deadline_ms > kMaxWaitMs) return false;
  const ULONGLONG deadline = GetTickCount64() + deadline_ms;
  while (GetTickCount64() < deadline) {
    const ULONGLONG now = GetTickCount64();
    const DWORD remaining = static_cast<DWORD>(deadline - now);
    const DWORD wait_ms = (remaining < kWaitSliceMs) ? remaining : kWaitSliceMs;
    HANDLE handles[2] = {process, cancellation};
    const DWORD count = cancellation ? 2u : 1u;
    const DWORD result = WaitForMultipleObjects(count, handles, FALSE, wait_ms);
    if (result == WAIT_OBJECT_0) return true;
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

class ChildRegistry final {
 public:
  bool insert(Child child, HANDLE root_job) noexcept {
    std::lock_guard lock(mutex_);
    if (children_.size() >= kMaxChildren || child.stable_id == 0 ||
        !child.process || !root_job ||
        children_.find(child.stable_id) != children_.end())
      return false;
    // Never trust a caller-provided boolean: membership is queried against
    // this exact root handle immediately before registry insertion.
    if (!verify_membership(child.process.get(), root_job)) return false;
    child.membership_verified = true;
    try {
      auto [ignored, inserted] = children_.emplace(child.stable_id, std::move(child));
      (void)ignored;
      return inserted;
    } catch (...) {
      return false;
    }
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
                          DWORD deadline_ms, HANDLE cancellation = nullptr) noexcept {
    std::lock_guard lock(mutex_);
    if (deadline_ms > kMaxWaitMs || !root_job) return false;
    const auto found = children_.find(stable_id);
    if (found == children_.end()) return false;
    Child& child = found->second;
    if (!TerminateJobObject(root_job, 1) ||
        !wait_reaped(child.process.get(), deadline_ms, cancellation) ||
        !job_empty(root_job)) return false;
    children_.erase(found);
    return true;
  }

  bool terminate_and_reap_all(HANDLE root_job, DWORD deadline_ms,
                              HANDLE cancellation = nullptr) noexcept {
    std::lock_guard lock(mutex_);
    if (deadline_ms > kMaxWaitMs || !root_job) return false;
    const ULONGLONG end = GetTickCount64() + deadline_ms;
    bool ok = true;
    for (auto& [ignored, child] : children_) {
      (void)ignored;
      const ULONGLONG now = GetTickCount64();
      const DWORD remaining = now >= end
                                  ? 0
                                  : static_cast<DWORD>(end - now);
      if (remaining == 0 || !TerminateJobObject(root_job, 1) ||
          !wait_reaped(child.process.get(), remaining, cancellation) ||
          !job_empty(root_job))
        ok = false;
    }
    return ok;
  }

  void close_all() noexcept {
    std::lock_guard lock(mutex_);
    for (auto& [ignored, child] : children_) {
      (void)ignored;
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

class JournalAuthority final {
 public:
  JournalOutcome dispatch(const IssuedCapability& capability, JournalPersist persist,
                          DispatchOperation operation) noexcept {
    JournalOutcome outcome{};
    if (!trust_gates_open() || !persist || !operation || state_ != JournalState::kIdle ||
        capability.operation_id == 0 || !valid_scope(capability.operation_scope))
      return outcome;
    JournalRecord start = record(capability, JournalState::kStartDurable, 0);
    if (start.sequence == 0 || !persist(start)) {
      state_ = JournalState::kUnknown;
      return outcome;
    }
    state_ = JournalState::kStartDurable;
    const DispatchStatus operation_status = operation(capability);
    if (operation_status == DispatchStatus::kPreDispatchFailure) {
      JournalRecord failed = record(capability, JournalState::kTerminalDurable, 1);
      if (failed.sequence == 0 || !persist(failed)) {
        state_ = JournalState::kUnknown;
        return outcome;
      }
      state_ = JournalState::kTerminalDurable;
      return JournalOutcome{DispatchStatus::kTerminalFailure, state_, failed.sequence};
    }
    if (operation_status == DispatchStatus::kDispatchedUnknown) {
      JournalRecord unknown = record(capability, JournalState::kUnknown, 2);
      if (unknown.sequence == 0 || !persist(unknown)) {
        state_ = JournalState::kUnknown;
        return outcome;
      }
      state_ = JournalState::kUnknown;
      return JournalOutcome{DispatchStatus::kDispatchedUnknown, state_, unknown.sequence};
    }
    if (operation_status != DispatchStatus::kTerminalSuccess &&
        operation_status != DispatchStatus::kTerminalFailure) {
      state_ = JournalState::kUnknown;
      return outcome;
    }
    const std::uint32_t error =
        operation_status == DispatchStatus::kTerminalFailure ? 1u : 0u;
    JournalRecord completed = record(capability, JournalState::kTerminalDurable, error);
    if (completed.sequence == 0 || !persist(completed)) {
      state_ = JournalState::kUnknown;
      return outcome;
    }
    state_ = JournalState::kTerminalDurable;
    return JournalOutcome{operation_status, state_, completed.sequence};
  }

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
    if (!load(latest) || latest.sequence == 0 || latest.operation_id == 0 ||
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

  bool recovery_required() const noexcept {
    return state_ == JournalState::kUnknown;
  }

  bool acknowledge() const noexcept {
    return state_ == JournalState::kTerminalDurable;
  }

 private:
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

bool initialize_supervisor(SupervisorState& state,
                           BootstrapProof& bootstrap) noexcept {
  if (!trust_gates_open() || !adopt_bootstrap(bootstrap, state.bootstrap_handles))
    return false;
  state.root_job.reset(create_root_job());
  if (!state.root_job || !root_job_policy_proven(state.root_job.get())) {
    state.root_job.reset();
    state.bootstrap_handles.read.reset();
    state.bootstrap_handles.write.reset();
    return false;
  }
  if (!state.issuer.initialize_epoch()) {
    state.root_job.reset();
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
  if (!trust_gates_open() || deadline_ms > kMaxWaitMs) return false;
  bool ok = state.children.terminate_and_reap_all(state.root_job.get(), deadline_ms,
                                                   state.cancellation.get());
  if (state.root_job) {
    // Closing a kill-on-close root is the final containment action, but only
    // after all registered child jobs have been boundedly reaped.
    if (!ok) return false;
    state.root_job.reset();
  }
  state.children.close_all();
  return ok;
}

bool durable_journal_authorize(
    JournalAuthority& journal, const IssuedCapability& capability,
    JournalPersist persist, DispatchOperation operation) noexcept {
  // The journal adapter is private and must fsync/acknowledge each record.
  // Dispatch is impossible until the exact start record is durable; callers
  // receive success only after a terminal record is durable as well.
  const JournalOutcome outcome = journal.dispatch(capability, persist, operation);
  return outcome.status == DispatchStatus::kTerminalSuccess &&
      outcome.durable_state == JournalState::kTerminalDurable && journal.acknowledge();
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
