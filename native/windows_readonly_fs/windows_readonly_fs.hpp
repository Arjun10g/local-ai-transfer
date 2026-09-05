#pragma once

// Inert, source-only Win32 read boundary. This header is intentionally absent
// from CMake, host imports, package manifests, and all production registries.
#ifndef _WIN32
#error "The Windows read-only filesystem boundary is Windows-only"
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
#include <memory>
#include <string>
#include <vector>

namespace lae::windows_readonly_fs {

inline constexpr std::uint32_t kAbiVersion = 2;
inline constexpr std::uint32_t kMaxComponents = 64;
inline constexpr std::uint32_t kMaxListEntries = 256;
inline constexpr std::uint32_t kMaxListNameBytes = 65'536;
inline constexpr std::uint32_t kMaxReadBytes = 65'536;
inline constexpr std::uint64_t kMaxReadableFileBytes = 16'777'216;

enum class Operation : std::uint8_t {
  kStat = 1,
  kList = 2,
  kRead = 3,
};

enum class ObjectKind : std::uint8_t {
  kFile = 1,
  kDirectory = 2,
};

enum class Status : std::uint8_t {
  kOk,
  kPlatformUnavailable,
  kInvalidRequest,
  kUnsafeGrant,
  kUnsafeComponent,
  kUnsafeVolume,
  kUnsupportedFilesystem,
  kNotFound,
  kAccessDenied,
  kSecurityUnavailable,
  kReparseRefused,
  kUnsupportedFileType,
  kLinkCountRefused,
  kDeletePending,
  kIdentityMismatch,
  kBoundsExceeded,
  kUtf8Required,
  kBinaryRefused,
  kCancelled,
  kDeadlineExceeded,
  kIoFailed,
  kInternal,
};

struct ObjectIdentity {
  std::uint64_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
};

struct ExecutionContext {
  std::uint64_t deadline_monotonic_ms = 0;
  std::uint64_t (*monotonic_ms)(void*) noexcept = nullptr;
  bool (*is_cancelled)(void*) noexcept = nullptr;
  void* opaque = nullptr;
};

// Opaque future broker authority. There is deliberately no public issuer,
// mutator, deserializer, or test constructor in this slice. The default value
// is invalid; production must continue refusing until a separately reviewed
// broker binds a MAC-authenticated, operation-scoped capability and the native
// I/O cancellation boundary is available.
class BrokerGrantCapability final {
 public:
  BrokerGrantCapability() noexcept = default;
  BrokerGrantCapability(const BrokerGrantCapability&) = delete;
  BrokerGrantCapability& operator=(const BrokerGrantCapability&) = delete;
  BrokerGrantCapability(BrokerGrantCapability&&) noexcept = default;
  BrokerGrantCapability& operator=(BrokerGrantCapability&&) noexcept = default;

  bool valid() const noexcept;
  const std::string& grant_id() const noexcept;
  const std::wstring& absolute_root() const noexcept;
  const ObjectIdentity& root_identity() const noexcept;

 private:
  bool authenticated_ = false;
  std::uint32_t allowed_operations_ = 0;
  std::uint64_t expires_monotonic_ms_ = 0;
  std::string grant_id_;
  std::wstring absolute_root_;
  ObjectIdentity root_identity_{};
  std::array<std::uint8_t, 16> authority_nonce_{};
  std::array<std::uint8_t, 32> request_binding_digest_{};
  std::array<std::uint8_t, 32> mac_{};
};

struct Request {
  std::uint32_t abi_version = kAbiVersion;
  Operation operation = Operation::kStat;
  const BrokerGrantCapability* grant_capability = nullptr;
  std::vector<std::wstring> relative_components;
  std::uint64_t offset = 0;
  std::uint32_t max_bytes = 0;
  ExecutionContext execution{};
};

struct Entry {
  std::string name;
  ObjectKind kind = ObjectKind::kFile;
  std::uint64_t size = 0;
};

struct Receipt {
  std::uint32_t abi_version = kAbiVersion;
  std::string status;
  std::string operation;
  std::string grant_id;
  std::uint32_t component_count = 0;
  std::uint32_t entries_returned = 0;
  std::uint32_t bytes_returned = 0;
  bool truncated = false;
  bool root_identity_bound = false;
  bool handles_retained = false;
};

struct Response {
  bool has_stat = false;
  Entry stat{};
  std::vector<Entry> entries;
  std::string text;
  Receipt receipt{};
};

class ReadLease final {
 public:
  ReadLease() noexcept;
  ~ReadLease();
  ReadLease(const ReadLease&) = delete;
  ReadLease& operator=(const ReadLease&) = delete;
  ReadLease(ReadLease&&) noexcept;
  ReadLease& operator=(ReadLease&&) noexcept;

  bool valid() const noexcept;
  void reset() noexcept;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  friend Status execute_readonly(const Request&, ReadLease&, Response&) noexcept;
};

// This slice always returns kPlatformUnavailable before filesystem access
// because the authenticated issuer and cancellable-I/O gates are absent. The
// retained latent implementation must not be enabled merely by changing those
// constants: issuer verification and cancellable supervision require a new
// reviewed implementation. Failure leaves outputs empty/redacted and performs
// no filesystem write.
Status execute_readonly(const Request& request,
                        ReadLease& lease,
                        Response& response) noexcept;

const char* status_name(Status status) noexcept;
const char* operation_name(Operation operation) noexcept;

}  // namespace lae::windows_readonly_fs
