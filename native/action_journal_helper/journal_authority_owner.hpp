#pragma once

// Private helper-lifetime ownership boundary.  This header is intentionally
// not part of the product graph or a public host/supervisor API.  The owner is
// the only place that may acquire the retained storage lease and recover the
// fixed container before the pipe server is published.
#ifndef _WIN32
#error "The ActionJournal owner is Windows-only"
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <memory>

#include "store_codec.hpp"

namespace lae::action_journal_helper {

enum class AuthorityStatus : std::uint8_t {
  kReady,
  kStorageUnavailable,
  kStorageCorrupt,
  kRecoveryFailed,
  kIoTimeout,
  kIoCancelFailed,
  kNotReady,
  kPoisoned,
  kInternal,
};

class JournalAuthorityOwner final {
 public:
  // This is the sole construction path.  It acquires one storage lease,
  // constructs one store borrowing that lease, and completes recovery before
  // returning a non-null owner.  A failed open returns no owner and no caller
  // can publish a pipe from the failed state.
  static std::unique_ptr<JournalAuthorityOwner> open(
      const action_journal_storage::StorageRequest& request,
      const std::array<std::uint8_t, 32>& container_id,
      StorageIoControl io,
      AuthorityStatus& status,
      action_journal_storage::StorageReceipt& receipt) noexcept;

  ~JournalAuthorityOwner() noexcept;
  JournalAuthorityOwner(const JournalAuthorityOwner&) = delete;
  JournalAuthorityOwner& operator=(const JournalAuthorityOwner&) = delete;
  JournalAuthorityOwner(JournalAuthorityOwner&&) = delete;
  JournalAuthorityOwner& operator=(JournalAuthorityOwner&&) = delete;

  // Applies one already-authenticated, decoded protocol request.  The mutex
  // covers only the store application; pipe reads/writes and ProtocolSession
  // I/O remain outside this owner lock.
  StoreStatus apply(const DecodedRequest& request,
                    StorageIoControl io,
                    EncodedResult& result) noexcept;
  // Closes admission and waits for the current owner-locked application to
  // settle.  Closing and the bounded borrower count are one CAS-managed word:
  // no caller can be admitted after shutdown's linearization point, and
  // shutdown cannot observe a false zero count.
  bool begin_shutdown() noexcept;
  bool ready() const;
  bool poisoned() const;
  std::uint32_t recovery_count() const;

 private:
  class ActiveBorrow final {
   public:
    explicit ActiveBorrow(JournalAuthorityOwner& owner) noexcept
        : owner_(owner), acquired_(owner.try_acquire_borrow()) {}
    ~ActiveBorrow() noexcept { if (acquired_ && !owner_.release_borrow())
      owner_.poisoned_.store(true, std::memory_order_release); }
    ActiveBorrow(const ActiveBorrow&) = delete;
    ActiveBorrow& operator=(const ActiveBorrow&) = delete;
    bool acquired() const noexcept { return acquired_; }

   private:
    JournalAuthorityOwner& owner_;
    bool acquired_ = false;
  };

  JournalAuthorityOwner(action_journal_storage::JournalStorageLease&& lease,
                        const std::array<std::uint8_t, 32>& container_id) noexcept;

  static AuthorityStatus map_open_status(
      action_journal_storage::StorageStatus status) noexcept;
  static AuthorityStatus map_recovery_status(StoreStatus status) noexcept;
  void poison_for(StoreStatus status) noexcept;
  bool try_acquire_borrow() noexcept;
  bool release_borrow() noexcept;
  bool admission_closing() const noexcept;
  bool wait_for_borrowers() noexcept;

  // Bit 63 closes admission.  The lower 32 bits are the borrower count;
  // bits 32..62 are reserved and must stay zero.  A single aligned word is
  // the linearization primitive for both admission and shutdown.  The
  // Windows wait API is available on Windows 8+ and is used only on this
  // exact address; unexpected wait failure is an unproved shutdown.
  static constexpr std::uint64_t kAdmissionClosing = UINT64_C(1) << 63;
  static constexpr std::uint64_t kAdmissionCountMask = UINT64_C(0xffffffff);
  static constexpr std::uint32_t kMaxActiveBorrows = 4096;
  static_assert(sizeof(std::atomic<std::uint64_t>) == sizeof(std::uint64_t),
                "admission word must be waitable as one 64-bit value");
  static_assert(alignof(std::atomic<std::uint64_t>) >= alignof(std::uint64_t),
                "admission word must be naturally aligned for WaitOnAddress");

  // Declaration order is part of the ownership invariant: the lease outlives
  // the store that borrows its retained file handle.
  action_journal_storage::JournalStorageLease lease_;
  FixedContainerStore store_;
  mutable std::mutex mutex_;
  alignas(8) std::atomic<std::uint64_t> admission_{0};
  bool shutting_down_ = false;
  std::uint32_t recovery_count_ = 0;
  bool recovered_ = false;
  std::atomic_bool poisoned_{false};
};

const char* authority_status_name(AuthorityStatus status) noexcept;

}  // namespace lae::action_journal_helper
