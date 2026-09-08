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
#include <map>
#include <mutex>
#include <memory>
#include <string>
#include <string_view>

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

// Typed, source-only binding for one process side-effect.  The operation id
// is deliberately the complete sixteen-byte value behind `act_` and is never
// represented as a uint64_t.  Digests are fixed-size bytes so callers cannot
// smuggle JSON, credentials, protocol keys, or session nonces into a lease.
struct ProcessDispatchBinding final {
  std::array<std::uint8_t, 16> operation_id{};
  std::array<std::uint8_t, 32> request_ref{};
  std::array<std::uint8_t, 32> call_ref{};
  std::string tool;
  std::string risk;
  std::string side_effect;
  std::array<std::uint8_t, 32> args_digest{};
  std::array<std::uint8_t, 32> preview_digest{};
  std::array<std::uint8_t, 32> operation_digest{};
  std::string authorization_kind;
  std::uint32_t authorized_sequence = 0;
  std::array<std::uint8_t, 32> authorized_receipt_digest{};
  std::array<std::uint8_t, 32> authorized_event_digest{};
};

struct ProcessDispatchReadbackProof final {
  std::array<std::uint8_t, 16> operation_id{};
  std::uint32_t sequence = 0;
  std::array<std::uint8_t, 32> receipt_digest{};
  std::array<std::uint8_t, 32> event_digest{};
};

class ProcessExternalProof final {
 public:
  ~ProcessExternalProof() noexcept = default;
  ProcessExternalProof(const ProcessExternalProof&) = delete;
  ProcessExternalProof& operator=(const ProcessExternalProof&) = delete;
  ProcessExternalProof(ProcessExternalProof&&) noexcept = default;
  ProcessExternalProof& operator=(ProcessExternalProof&&) noexcept = default;

  // The capability exposes only a provenance check; its operation, witness,
  // and external evidence bytes remain inaccessible to callers.
  bool matches_operation(
      const std::array<std::uint8_t, 16>& operation_id,
      std::uint64_t dispatch_generation) const noexcept;

 private:
  friend class JournalAuthorityOwner;
  friend class ProcessDispatchLease;
  friend class TrustedProcessExternalProofIssuer;
  ProcessExternalProof(
      const std::array<std::uint8_t, 16>& operation_id,
      const std::array<std::uint8_t, 32>& external_receipt_digest,
      const std::array<std::uint8_t, 32>& external_event_digest,
      std::uint64_t dispatch_generation) noexcept;

  std::array<std::uint8_t, 16> operation_id_{};
  std::array<std::uint8_t, 32> external_receipt_digest_{};
  std::array<std::uint8_t, 32> external_event_digest_{};
  std::uint64_t dispatch_generation_ = 0;
};

enum class ProcessDispatchLeaseStatus : std::uint8_t {
  kOk,
  kOwnerNotReady,
  kBindingMismatch,
  kUnknownManualBlocked,
  kLeaseLimit,
  kAlreadyLeased,
  kMutationConflict,
  kInvalidState,
  kReadbackMismatch,
  kStorageFailure,
  kOneShotUsed,
  kLostAcknowledgement,
  kAmbiguousNoReplay,
  kAbandonedPoisoned,
};

class JournalAuthorityOwner;
// Declared only as a narrow future slice-2 seam.  Slice 1 deliberately does
// not define this issuer or provide a test/public minting factory.
class TrustedProcessExternalProofIssuer;

// A lease is a one-use, noncopyable capability for the owner-internal
// dispatch barrier.  It has no process handle, protocol key, nonce, or JSON.
// The owner must outlive a lease.  Destruction after dispatch poisons the
// owner because an unobserved external mutation is ambiguous and cannot be
// replayed.
class ProcessDispatchLease final {
 public:
  ~ProcessDispatchLease() noexcept;
  ProcessDispatchLease(const ProcessDispatchLease&) = delete;
  ProcessDispatchLease& operator=(const ProcessDispatchLease&) = delete;
  ProcessDispatchLease(ProcessDispatchLease&&) = delete;
  ProcessDispatchLease& operator=(ProcessDispatchLease&&) = delete;

  ProcessDispatchLeaseStatus persist_dispatching(
      StorageIoControl io, ProcessDispatchReadbackProof& proof) noexcept;
  // Opens the OS-mutation barrier only after persist_dispatching supplied an
  // exact durable readback proof. This source slice still exposes no OS API.
  ProcessDispatchLeaseStatus begin_external_dispatch() noexcept;
  ProcessDispatchLeaseStatus acknowledge_external(
      const ProcessExternalProof& proof, StorageIoControl io) noexcept;
  ProcessDispatchLeaseStatus lookup_lost_ack(
      const ProcessExternalProof& proof, StorageIoControl io,
      ProcessDispatchReadbackProof& readback) noexcept;
  ProcessDispatchLeaseStatus begin_reconciliation(
      std::string_view reason, StorageIoControl io) noexcept;
  ProcessDispatchLeaseStatus complete_external(
      const ProcessExternalProof& proof, StorageIoControl io) noexcept;
  ProcessDispatchLeaseStatus mark_unknown(StorageIoControl io) noexcept;
  bool dispatching_persisted() const noexcept { return dispatching_persisted_; }
  bool external_dispatch_started() const noexcept { return external_started_; }

 private:
  friend class JournalAuthorityOwner;
  friend class TrustedProcessExternalProofIssuer;
  ProcessDispatchLease(JournalAuthorityOwner& owner,
                       const ProcessDispatchBinding& binding,
                       std::string operation_text);

  JournalAuthorityOwner* owner_ = nullptr;
  ProcessDispatchBinding binding_{};
  std::string operation_text_;
  bool dispatching_persisted_ = false;
  bool external_started_ = false;
  bool acknowledged_ = false;
  std::array<std::uint8_t, 32> acknowledged_receipt_digest_{};
  std::array<std::uint8_t, 32> acknowledged_event_digest_{};
  bool terminal_ = false;
  bool one_shot_used_ = false;
  // The owner table owns this attachment bit.  It is set only after the
  // map insertion succeeds, so a throwing insertion cannot destroy a
  // candidate while re-entering the owner mutex.
  bool owner_attached_ = false;
  std::uint64_t dispatch_generation_ = 0;
};

const char* process_dispatch_lease_status_name(
    ProcessDispatchLeaseStatus status) noexcept;

class JournalAuthorityOwner final {
 public:
  friend class ProcessDispatchLease;
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

  // Sole owner/store-internal process-dispatch API. Acquisition performs a
  // read-only reload and demands an exact authorized sequence-one tip.
  ProcessDispatchLeaseStatus acquire_process_dispatch_lease(
      const ProcessDispatchBinding& binding, StorageIoControl io,
      std::unique_ptr<ProcessDispatchLease>& lease) noexcept;
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
                        const std::array<std::uint8_t, 32>& container_id);

  static AuthorityStatus map_open_status(
      action_journal_storage::StorageStatus status) noexcept;
  static AuthorityStatus map_recovery_status(StoreStatus status) noexcept;
  void poison_for(StoreStatus status) noexcept;
  bool try_acquire_borrow() noexcept;
  bool release_borrow() noexcept;
  bool admission_closing() const noexcept;
  bool wait_for_borrowers() noexcept;
  StoreStatus mutation_preflight_locked(const DecodedRequest& request,
                                        StorageIoControl io) noexcept;
  bool unknown_manual_present_locked() const noexcept;
  bool lease_conflict_locked(const DecodedRequest& request) const noexcept;
  void release_process_dispatch_lease(ProcessDispatchLease& lease) noexcept;
  ProcessDispatchLeaseStatus persist_dispatching(
      ProcessDispatchLease& lease, StorageIoControl io,
      ProcessDispatchReadbackProof& proof) noexcept;
  ProcessDispatchLeaseStatus acknowledge_external(
      ProcessDispatchLease& lease, const ProcessExternalProof& proof,
      StorageIoControl io) noexcept;
  ProcessDispatchLeaseStatus lookup_lost_ack(
      ProcessDispatchLease& lease, StorageIoControl io,
      ProcessDispatchReadbackProof& proof) noexcept;
  ProcessDispatchLeaseStatus transition_lease(
      ProcessDispatchLease& lease, const char* method,
      const char* state, const nlohmann::json& body,
      StorageIoControl io, ProcessDispatchReadbackProof* proof) noexcept;
  ProcessDispatchLeaseStatus begin_external_dispatch(
      ProcessDispatchLease& lease) noexcept;

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
  // Monotonic owner-local provenance for the external mutation barrier. It is
  // never caller supplied and exhaustion is a sticky refusal.
  std::uint64_t dispatch_generation_ = 0;
  std::atomic_bool poisoned_{false};
  // Lease-table access is owner-mutex protected and deliberately bounded;
  // lease admission and ordinary mutation share the same lock ordering.
  static constexpr std::size_t kMaxLeasedProcessDispatches = 8;
  std::map<std::string, ProcessDispatchLease*> leased_operations_;
};

const char* authority_status_name(AuthorityStatus status) noexcept;

}  // namespace lae::action_journal_helper
