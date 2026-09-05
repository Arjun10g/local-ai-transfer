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

inline constexpr std::uint32_t kAbiVersion = 1;
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

struct Request {
  std::uint32_t abi_version = kAbiVersion;
  Operation operation = Operation::kStat;
  std::string grant_id;
  std::wstring absolute_grant_root;
  ObjectIdentity expected_root_identity{};
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

// On success every root/component/list-child handle remains retained in
// `lease`; the caller must keep the lease alive while consuming `response`.
// Failure leaves both outputs empty/redacted and performs no filesystem write.
Status execute_readonly(const Request& request,
                        ReadLease& lease,
                        Response& response) noexcept;

const char* status_name(Status status) noexcept;
const char* operation_name(Operation operation) noexcept;

}  // namespace lae::windows_readonly_fs
