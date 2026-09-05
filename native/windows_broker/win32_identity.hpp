#pragma once

#ifdef _WIN32
#include <windows.h>
#else
#error "The Windows identity lease is Windows-only"
#endif

#include "manifest.hpp"

#include <cstdint>
#include <atomic>
#include <string>
#include <vector>

namespace lae::windows_broker {

class UniqueHandle {
 public:
  UniqueHandle() = default;
  explicit UniqueHandle(HANDLE value) : value_(value) {}
  ~UniqueHandle();
  UniqueHandle(const UniqueHandle&) = delete;
  UniqueHandle& operator=(const UniqueHandle&) = delete;
  UniqueHandle(UniqueHandle&& other) noexcept;
  UniqueHandle& operator=(UniqueHandle&& other) noexcept;

  HANDLE get() const { return value_; }
  HANDLE release();
  explicit operator bool() const {
    return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
  }

 private:
  HANDLE value_ = INVALID_HANDLE_VALUE;
};

struct IdentityLease {
  UniqueHandle object;
  std::vector<UniqueHandle> ancestors;
  std::wstring normalized_path;
  std::uint64_t volume_serial = 0;
  std::string file_id_128;
  bool directory = false;
};

struct ManifestLease {
  BrokerManifest manifest;
  IdentityLease document;
  IdentityLease runtime_directory;
  std::string manifest_sha256;
};

// Every returned handle remains open to preserve the pathname/identity
// invariant. Closing a lease before CreateProcess and child reaping is unsafe.
bool acquire_executable_lease(const FileIdentitySpec& expected,
                              IdentityLease& lease,
                              std::string& error_code,
                              const std::atomic<bool>* cancelled = nullptr,
                              std::uint64_t deadline_tick_ms = 0) noexcept;
bool acquire_directory_lease(const DirectoryIdentitySpec& expected,
                             IdentityLease& lease,
                             std::string& error_code) noexcept;

// Locates a fixed manifest next to the running broker, authenticates its exact
// bytes against the compiled digest, strictly parses it, and pins its own and
// runtime-directory identities for the full broker lifetime.
bool load_compiled_manifest(ManifestLease& lease,
                            std::string& error_code) noexcept;

bool same_identity(const IdentityLease& lease) noexcept;

}  // namespace lae::windows_broker
