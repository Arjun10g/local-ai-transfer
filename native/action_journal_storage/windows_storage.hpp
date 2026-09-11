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
  kHandoffAlreadyTransferred,
  kSourceHandleInheritable,
  kInheritanceControlFailed,
  kFinalPathMismatch,
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
// A Windows HANDLE is not a CRT descriptor. The duplicate below may only be
// consumed by a future reviewed launcher bridge which converts it in the child
// before constructing DescriptorActionJournal; it must never be serialized as
// LAE_ACTION_JOURNAL_FD.
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

// A prepared duplicate of the retained WAL handle. It is deliberately NOT
// born inheritable: inheritance is a separate, explicit, revocable step.
//
// Hard precondition on any future launcher that consumes this object. Each
// clause is mandatory; a launcher that cannot satisfy all of them must close
// the duplicate instead of launching.
//   1. Call arm_inheritance() only immediately before the single
//      CreateProcess* call that must receive this handle. The duplicate must
//      never be inheritable while any unrelated child is created.
//   2. That CreateProcess* call must pass an explicit handle allowlist through
//      an updated thread attribute list entry
//      PROC_THREAD_ATTRIBUTE_HANDLE_LIST containing exactly this handle, so no
//      other ambient inheritable handle can leak and this handle is scoped to
//      that one child. bInheritHandles = TRUE without that allowlist is
//      forbidden.
//   3. Call revoke_inheritance() (or reset(), which closes the duplicate)
//      immediately after the child is created and on every failure path,
//      including a failed CreateProcess* and a failed acknowledgement.
//   4. After the handoff the child owns the shared file position; the parent
//      must not perform any I/O through its own lease (see DescriptorWalLease
//      below), and the child must use positional reads and writes only.
// This is the same property the supervisor launch contract models as
// no_ambient_handle_inheritance; this type is the storage-side half of it.
class DescriptorWalHandoff final {
 public:
  DescriptorWalHandoff() noexcept;
  ~DescriptorWalHandoff();
  DescriptorWalHandoff(const DescriptorWalHandoff&) = delete;
  DescriptorWalHandoff& operator=(const DescriptorWalHandoff&) = delete;
  DescriptorWalHandoff(DescriptorWalHandoff&&) noexcept;
  DescriptorWalHandoff& operator=(DescriptorWalHandoff&&) noexcept;

  // Ownership of the duplicate, independent of its inheritance flag.
  bool valid() const noexcept;
  // Reads HANDLE_FLAG_INHERIT from the kernel on every call; never cached.
  bool inheritance_armed() const noexcept;
  // Idempotent; both report a typed status and verify the observed flag.
  StorageStatus arm_inheritance() noexcept;
  StorageStatus revoke_inheritance() noexcept;
  // Borrowed value for the PROC_THREAD_ATTRIBUTE_HANDLE_LIST allowlist and the
  // CreateProcess* call. Ownership stays here, so reset() still closes it.
  HANDLE handle() const noexcept;
  // Ownership transfer. Refused while inheritance is armed, so a handle that
  // leaves RAII ownership is never ambiently inheritable.
  HANDLE take_handle() noexcept;
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
  // True once the single duplicate has been transferred out. The duplicate
  // shares one file object, and therefore one file position, with the retained
  // handle. After transfer the child owns that position, so this lease refuses
  // every operation that would perform parent-side I/O on the WAL. Any future
  // parent-side read/write accessor must check this first.
  bool handoff_transferred() const noexcept;
  // Re-runs the complete acquisition check set on the retained handle. This
  // performs I/O, so it is refused with kHandoffAlreadyTransferred once the
  // handoff has been transferred.
  StorageStatus revalidate() const noexcept;
  // Produces at most one duplicate, which is not inheritable when produced;
  // the caller arms inheritance explicitly through DescriptorWalHandoff. The
  // retained source handle is never inheritable, is verified non-inheritable
  // before duplication, and continues to hold the deny-write/delete share
  // lease. A second call is refused with kHandoffAlreadyTransferred and never
  // disturbs the handoff a first call already produced.
  StorageStatus prepare_inheritable_handoff(DescriptorWalHandoff& output) noexcept;
  void reset() noexcept;

 private:
  // Shared by revalidate() and prepare_inheritable_handoff() so the
  // pre-duplication recheck set is exactly the acquisition check set.
  StorageStatus recheck() const noexcept;

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

// Creation publishes only the exact v2 WAL header. Creation is the only path
// that may discard: a leaf this call created with CREATE_NEW and has not yet
// published is deleted through its own already-open handle when any later
// check or publication step fails, so a transient failure cannot wedge the
// fixed leaf name. That is a discard of a never-published private artifact,
// not a repair. Reopen never deletes, never truncates, and never writes. A
// torn header is preserved and accepted on trusted reopen only when it is an
// exact strict prefix; the DescriptorActionJournal consumer owns
// descriptor-relative truncation and frame recovery. Complete corruption is
// never repaired or discarded here.
StorageStatus acquire_descriptor_wal(const DescriptorWalRequest& request,
                                     DescriptorWalLease& lease,
                                     DescriptorWalReceipt& receipt) noexcept;

const char* status_name(StorageStatus status) noexcept;

}  // namespace lae::action_journal_storage
