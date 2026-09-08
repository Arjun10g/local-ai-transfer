#include "authority.hpp"

#if defined(_WIN32)

#include "../action_journal_helper/journal_authority_owner.hpp"
#include "../action_journal_storage/windows_storage.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <system_error>
#include <utility>
#include <vector>

// Inert phase-2a topology seam.  This source is not in the product graph.
// Construction has an explicit canonical storage handoff; no fallback store
// or caller-selected implicit path exists.
namespace lae::windows_supervisor {
namespace {

using OperationIdentity = std::array<std::uint8_t, 16>;
using JournalAuthorityOwner =
    action_journal_helper::JournalAuthorityOwner;
constexpr std::size_t kMaxChildren = 8;
constexpr std::size_t kMaxLeases = 8;
constexpr std::uint32_t kMaxActiveBorrows = 4096;
constexpr std::uint32_t kShutdownWaitMs = 250;
struct SupervisorState;

struct SupervisorStartupHandoff final {
  SupervisorStartupHandoff(
      action_journal_storage::StorageRequest request,
      std::array<std::uint8_t, 32> container,
      action_journal_helper::StorageIoControl control)
      : storage_request(std::move(request)), container_id(container), io(control) {}

  action_journal_storage::StorageRequest storage_request;
  std::array<std::uint8_t, 32> container_id;
  action_journal_helper::StorageIoControl io;
};

struct ProcessTransactionFence final {
  void stop_admission() noexcept { admission_stopped = true; }
  void drain() noexcept { drained = true; }
  bool admission_stopped = false;
  bool drained = false;
};

enum class BorrowKind : std::uint8_t { kPipe, kProcess };

// The control word contains closing/poison bits plus both bounded borrower
// counts.  A ticket acquisition increments the relevant counts in one CAS,
// so shutdown cannot observe a process ticket between its overall and
// process-specific increments.  The control block is intentionally separate
// from SupervisorState and may outlive it while a released ticket is being
// destroyed.
class BorrowControlBlock final {
 public:
  static constexpr std::uint32_t kClosing = 1u << 30;
  static constexpr std::uint32_t kPoisoned = 1u << 31;
  static constexpr std::uint32_t kCounterBits = 13;
  static constexpr std::uint32_t kCounterMask = (1u << kCounterBits) - 1u;
  static constexpr std::uint32_t kProcessShift = kCounterBits;
  static constexpr std::uint32_t kProcessMask = kCounterMask << kProcessShift;

  bool try_acquire(BorrowKind kind) noexcept {
    std::uint32_t current = state.load(std::memory_order_acquire);
    for (;;) {
      if ((current & (kClosing | kPoisoned)) != 0) return false;
      const std::uint32_t active = current & kCounterMask;
      const std::uint32_t process = (current & kProcessMask) >> kProcessShift;
      if (active >= kMaxActiveBorrows ||
          (kind == BorrowKind::kProcess && process >= kMaxActiveBorrows)) {
        state.fetch_or(kPoisoned, std::memory_order_acq_rel);
        drained.notify_all();
        return false;
      }
      std::uint32_t next = current + 1u;
      if (kind == BorrowKind::kProcess) next += (1u << kProcessShift);
      if (state.compare_exchange_weak(current, next,
                                      std::memory_order_acq_rel,
                                      std::memory_order_acquire)) {
        return true;
      }
    }
  }

  void close_admission() noexcept {
    state.fetch_or(kClosing, std::memory_order_acq_rel);
    drained.notify_all();
  }

  void poison() noexcept {
    state.fetch_or(kPoisoned, std::memory_order_acq_rel);
    drained.notify_all();
  }

  void release(BorrowKind kind) noexcept {
    std::uint32_t current = state.load(std::memory_order_acquire);
    for (;;) {
      const std::uint32_t active = current & kCounterMask;
      const std::uint32_t process = (current & kProcessMask) >> kProcessShift;
      if (active == 0 || (kind == BorrowKind::kProcess && process == 0)) {
        poison();
        return;
      }
      std::uint32_t next = current - 1u;
      if (kind == BorrowKind::kProcess) next -= (1u << kProcessShift);
      if (state.compare_exchange_weak(current, next,
                                      std::memory_order_acq_rel,
                                      std::memory_order_acquire)) {
        drained.notify_all();
        return;
      }
    }
  }

  bool wait_process_drained() noexcept {
    return wait_for([](std::uint32_t value) {
      return ((value & kProcessMask) >> kProcessShift) == 0;
    });
  }

  bool wait_all_drained() noexcept {
    return wait_for([](std::uint32_t value) {
      return (value & kCounterMask) == 0;
    });
  }

 private:
  template <typename Predicate>
  bool wait_for(Predicate predicate) noexcept {
    try {
      std::unique_lock<std::mutex> lock(wait_mutex);
      const bool drained_now = drained.wait_for(
          lock, std::chrono::milliseconds(kShutdownWaitMs), [&] {
            const std::uint32_t value = state.load(std::memory_order_acquire);
            return predicate(value);
          });
      return drained_now && (state.load(std::memory_order_acquire) & kPoisoned) == 0;
    } catch (...) {
      poison();
      return false;
    }
  }

  std::atomic<std::uint32_t> state{0};
  std::mutex wait_mutex;
  std::condition_variable drained;
};

class BorrowTicket final {
 public:
  BorrowTicket() noexcept = default;
  ~BorrowTicket() noexcept { release(); }
  BorrowTicket(const BorrowTicket&) = delete;
  BorrowTicket& operator=(const BorrowTicket&) = delete;
  BorrowTicket(BorrowTicket&& other) noexcept
      : control_(std::move(other.control_)), owner_(other.owner_), kind_(other.kind_) {
    other.owner_ = nullptr;
  }
  BorrowTicket& operator=(BorrowTicket&& other) noexcept {
    if (this != &other) {
      release();
      control_ = std::move(other.control_);
      owner_ = other.owner_;
      kind_ = other.kind_;
      other.owner_ = nullptr;
    }
    return *this;
  }

  bool proven() const noexcept {
    return owner_ != nullptr && control_ != nullptr;
  }
  JournalAuthorityOwner* get() const noexcept {
    return proven() ? owner_ : nullptr;
  }
  void release() noexcept {
    if (control_ != nullptr) control_->release(kind_);
    control_.reset();
    owner_ = nullptr;
  }

 private:
  friend struct SupervisorState;
  BorrowTicket(std::shared_ptr<BorrowControlBlock> control,
               JournalAuthorityOwner* owner, BorrowKind kind) noexcept
      : control_(std::move(control)), owner_(owner), kind_(kind) {}

  std::shared_ptr<BorrowControlBlock> control_;
  JournalAuthorityOwner* owner_ = nullptr;
  BorrowKind kind_ = BorrowKind::kProcess;
};

struct PipeServerBorrow final {
  BorrowTicket ticket;
  bool stopped = false;
  void attach(BorrowTicket value) noexcept { ticket = std::move(value); }
  void stop() noexcept {
    ticket.release();
    stopped = true;
  }
};

struct ChildRegistry final {
  struct ChildSlot {};
  std::map<OperationIdentity, ChildSlot> children;
  void drain() noexcept { children.clear(); }
  bool bounded() const noexcept { return children.size() <= kMaxChildren; }
};

struct LeaseRegistry final {
  std::vector<std::unique_ptr<action_journal_helper::ProcessDispatchLease>> leases;
  void clear() noexcept { leases.clear(); }
  bool bounded() const noexcept { return leases.size() <= kMaxLeases; }
};

// Sole process-lifetime owner.  Shutdown closes admission, fences and drains
// process borrows/children, stops the pipe, destroys leases, and releases the
// owner last.  No owner lock is held across a wait in this seam.
struct SupervisorState final {
  explicit SupervisorState(const SupervisorStartupHandoff& handoff)
      : startup(handoff), borrow_domain(std::make_shared<BorrowControlBlock>()) {}
  SupervisorState(const SupervisorState&) = delete;
  SupervisorState& operator=(const SupervisorState&) = delete;
  ~SupervisorState() noexcept {
    // A same-thread or timed-out drain (including reentrant shutdown) is a
    // finite fail-stop, not
    // an opportunity to destroy the owner while an untracked ticket exists.
    if (!shutdown_ordered()) std::terminate();
  }

  static std::unique_ptr<SupervisorState> open(
      const SupervisorStartupHandoff& handoff) {
    // No OS-handle evidence issuer exists in phase 2a.  Refuse before owner
    // open, pipe publication, lease acquisition, or irreversible mutation.
    if (!kProcessLaunchAvailable || !kSupervisorOwnedProcessTransactionAccepted)
      return nullptr;
    try {
      auto state = std::make_unique<SupervisorState>(handoff);
      if (!state->journal_owner) return nullptr;
      if (!state->attach_pipe_borrow()) return nullptr;
      return state;
    } catch (const std::bad_alloc&) {
      return nullptr;
    } catch (const std::system_error&) {
      return nullptr;
    } catch (...) {
      return nullptr;
    }
  }

  BorrowTicket borrow_for_process() noexcept {
    return borrow(BorrowKind::kProcess);
  }

  bool attach_pipe_borrow() noexcept {
    BorrowTicket candidate = borrow(BorrowKind::kPipe);
    if (!candidate.proven()) return false;
    pipe.attach(std::move(candidate));
    return true;
  }

  bool shutdown_ordered() noexcept {
    if (shutdown_complete.load(std::memory_order_acquire)) return true;
    bool expected = false;
    if (!shutdown_started.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel,
            std::memory_order_acquire))
      return false;
    process_fence.stop_admission();
    borrow_domain->close_admission();
    process_fence.drain();
    children.drain();
    // Process tickets must be gone before stopping the pipe.  The pipe owns
    // its own ticket, so it is released next and the final wait drains every
    // remaining ticket before leases or the owner can be destroyed.
    if (!borrow_domain->wait_process_drained()) {
      borrow_domain->poison();
      return false;
    }
    pipe.stop();
    if (!borrow_domain->wait_all_drained()) {
      borrow_domain->poison();
      return false;
    }
    leases.clear();
    journal_owner.reset();
    shutdown_complete.store(true, std::memory_order_release);
    return true;
  }

 private:
  BorrowTicket borrow(BorrowKind kind) noexcept {
    if (!borrow_domain->try_acquire(kind)) return {};
    if (!journal_owner) {
      borrow_domain->release(kind);
      return {};
    }
    return BorrowTicket(borrow_domain, journal_owner.get(), kind);
  }

  SupervisorStartupHandoff startup;
  // Declared before dependants and explicitly reset last.
  std::unique_ptr<JournalAuthorityOwner> journal_owner;
  std::shared_ptr<BorrowControlBlock> borrow_domain;
  ProcessTransactionFence process_fence;
  ChildRegistry children;
  PipeServerBorrow pipe;
  LeaseRegistry leases;
  std::atomic_bool shutdown_started{false};
  std::atomic_bool shutdown_complete{false};
};

// Process launch borrows the supervisor owner and owns no journal, adapter,
// record, storage handle, or receipt callback.
struct ProcessLaunchAuthority final {
  explicit ProcessLaunchAuthority(SupervisorState& state) noexcept
      : owner(state.borrow_for_process()) {}
  ~ProcessLaunchAuthority() noexcept = default;
  ProcessLaunchAuthority(const ProcessLaunchAuthority&) = delete;
  ProcessLaunchAuthority& operator=(const ProcessLaunchAuthority&) = delete;
  BorrowTicket owner;
};

bool trust_gates_open() noexcept {
  return kReleaseManifestPinned && kSelfAuthenticodePinned &&
      kPackageIdentityPinned && kCancellableIoProven &&
      kDurableJournalAuthority && kNestedJobPolicyProven &&
      kBrokerIssuedIdentityProven &&
      kRetainedExecutingSectionIdentityProven &&
      kRetainedWorkingDirectoryIdentityProven &&
      kSupervisorOwnedProcessTransactionAccepted;
}

}  // namespace

#include "process_transaction.inc"

Status Authority::start() noexcept {
  if (!trust_gates_open()) return Status::kUnavailable;
  return Status::kUnavailable;
}

Status Authority::shutdown() noexcept {
  if (!trust_gates_open()) return Status::kUnavailable;
  return Status::kUnavailable;
}

RedactedReceipt Authority::loopback_metadata() noexcept {
  return RedactedReceipt{Status::kUnavailable, 0, false, false};
}

}  // namespace lae::windows_supervisor

#endif  // defined(_WIN32)
