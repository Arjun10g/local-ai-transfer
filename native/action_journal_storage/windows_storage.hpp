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
inline constexpr wchar_t kDescriptorWalLeafName[] = L"action-journal-v2.wal";
inline constexpr std::uint64_t kDescriptorWalMaxBytes = 32ull * 1024ull * 1024ull;

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

// Dormant bootstrap authority for the JavaScript DescriptorActionJournal WAL.
// A Windows HANDLE is not a CRT descriptor. The inheritable duplicate below
// may only be consumed by a future reviewed launcher bridge which converts it
// in the child before constructing DescriptorActionJournal; it must never be
// serialized as LAE_ACTION_JOURNAL_FD.
struct DescriptorWalIdentity {
  std::uint64_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
};

struct DescriptorWalRequest {
  OpenMode mode = OpenMode::kOpenExisting;
  std::wstring absolute_directory;
  bool has_expected_identity = false;
  DescriptorWalIdentity expected_identity{};
};

struct DescriptorWalReceipt {
  std::uint32_t abi_version = kStorageAbiVersion;
  std::string status;
  std::uint64_t wal_bytes = 0;
  std::string volume_serial_hex;
  std::string file_id_hex;
  std::string filesystem;
  std::string dacl_profile;
  bool identity_reopened = false;
  bool exclusive_writer_lease = false;
  bool descriptor_bridge_available = false;
};

class DescriptorWalHandoff final {
 public:
  DescriptorWalHandoff() noexcept;
  ~DescriptorWalHandoff();
  DescriptorWalHandoff(const DescriptorWalHandoff&) = delete;
  DescriptorWalHandoff& operator=(const DescriptorWalHandoff&) = delete;
  DescriptorWalHandoff(DescriptorWalHandoff&&) noexcept;
  DescriptorWalHandoff& operator=(DescriptorWalHandoff&&) noexcept;

  bool valid() const noexcept;
  HANDLE take_inheritable_handle() noexcept;
  void reset() noexcept;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  friend class DescriptorWalLease;
};

class DescriptorWalLease final {
 public:
  DescriptorWalLease() noexcept;
  ~DescriptorWalLease();
  DescriptorWalLease(const DescriptorWalLease&) = delete;
  DescriptorWalLease& operator=(const DescriptorWalLease&) = delete;
  DescriptorWalLease(DescriptorWalLease&&) noexcept;
  DescriptorWalLease& operator=(DescriptorWalLease&&) noexcept;

  bool valid() const noexcept;
  // Produces at most one inheritable duplicate. The retained source handle is
  // never inheritable and continues to hold the deny-write/delete share lease.
  StorageStatus prepare_inheritable_handoff(DescriptorWalHandoff& output) noexcept;
  void reset() noexcept;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  friend StorageStatus acquire_descriptor_wal(const DescriptorWalRequest&,
                                               DescriptorWalLease&,
                                               DescriptorWalReceipt&) noexcept;
};

// Success transfers all identity-holding handles into `lease`. Failure leaves
// `lease` empty and never removes or repairs a partially created container.
StorageStatus acquire_storage(const StorageRequest& request,
                              JournalStorageLease& lease,
                              StorageReceipt& receipt) noexcept;

// Creation publishes only the exact v2 WAL header. A torn header is preserved
// and accepted on trusted reopen only when it is an exact strict prefix; the
// DescriptorActionJournal consumer owns descriptor-relative truncation and
// frame recovery. Complete corruption is never repaired or discarded here.
StorageStatus acquire_descriptor_wal(const DescriptorWalRequest& request,
                                     DescriptorWalLease& lease,
                                     DescriptorWalReceipt& receipt) noexcept;

const char* status_name(StorageStatus status) noexcept;

}  // namespace lae::action_journal_storage
