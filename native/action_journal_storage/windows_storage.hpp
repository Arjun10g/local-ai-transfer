#pragma once

// Source-only Windows storage boundary. It is absent from the default/product
// CMake graph and every production import/registration path. The optional
// helper compile-check target does not register or activate storage.
#ifndef _WIN32
#error "The ActionJournal storage boundary is Windows-only"
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

namespace lae::action_journal_storage {

inline constexpr std::uint32_t kStorageAbiVersion = 1;
inline constexpr std::uint64_t kContainerBytes = 33'558'528;
inline constexpr std::uint32_t kHeaderBytes = 4'096;
inline constexpr wchar_t kFixedLeafName[] = L"action-journal-v1.container";

enum class OpenMode : std::uint8_t {
  kCreateNew = 1,
  kOpenExisting = 2,
};

enum class StorageStatus : std::uint8_t {
  kOkCreated,
  kOkOpened,
  kPlatformUnavailable,
  kInvalidRequest,
  kUnsafePath,
  kUnsafeVolume,
  kUnsupportedFilesystem,
  kPrivateDirectoryRequired,
  kAlreadyExists,
  kNotFound,
  kAccessDenied,
  kSecurityUnavailable,
  kReparseRefused,
  kUnsupportedFileType,
  kLinkCountRefused,
  kDeletePending,
  kSizeMismatch,
  kIdentityMismatch,
  kReopenIdentityMismatch,
  kRngFailed,
  kHashFailed,
  kIoFailed,
  kGenesisIncomplete,
  kContainerUnformatted,
  kContainerCorruptHeader,
  kInternal,
};

struct StorageIdentity {
  std::uint64_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
  std::array<std::uint8_t, 32> container_id{};
};

struct StorageRequest {
  OpenMode mode = OpenMode::kOpenExisting;
  std::wstring absolute_directory;
  bool has_expected_identity = false;
  StorageIdentity expected_identity{};
};

struct StorageReceipt {
  std::uint32_t abi_version = kStorageAbiVersion;
  std::string status;
  std::uint64_t container_bytes = 0;
  std::string container_id_hex;
  std::string volume_serial_hex;
  std::string file_id_hex;
  std::string filesystem;
  std::string dacl_profile;
  bool identity_reopened = false;
  bool handle_retained = false;
};

class JournalStorageLease final {
 public:
  JournalStorageLease() noexcept;
  ~JournalStorageLease();
  JournalStorageLease(const JournalStorageLease&) = delete;
  JournalStorageLease& operator=(const JournalStorageLease&) = delete;
  JournalStorageLease(JournalStorageLease&&) noexcept;
  JournalStorageLease& operator=(JournalStorageLease&&) noexcept;

  bool valid() const noexcept;
  HANDLE retained_file_handle() const noexcept;
  void reset() noexcept;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  friend StorageStatus acquire_storage(const StorageRequest&,
                                       JournalStorageLease&,
                                       StorageReceipt&) noexcept;
};

// Success transfers all identity-holding handles into `lease`. Failure leaves
// `lease` empty and never removes or repairs a partially created container.
StorageStatus acquire_storage(const StorageRequest& request,
                              JournalStorageLease& lease,
                              StorageReceipt& receipt) noexcept;

const char* status_name(StorageStatus status) noexcept;

}  // namespace lae::action_journal_storage
