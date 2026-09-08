#include "authority.hpp"

#if defined(_WIN32)

#include "../action_journal_helper/journal_authority_owner.hpp"
#include "../action_journal_storage/windows_storage.hpp"
#include "borrow_ticket.hpp"
#include "../action_journal_helper/pipe_server.hpp"

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

struct PipeCallFence final {
  static constexpr std::uint32_t kClosing = 1u << 31;
  static constexpr std::uint32_t kActiveMask = 1u;

  bool begin() noexcept {
    std::uint32_t current = state.load(std::memory_order_acquire);
    for (;;) {
      if ((current & kClosing) != 0 || (current & kActiveMask) != 0)
        return false;
      if (state.compare_exchange_weak(current, current + 1u,
                                      std::memory_order_acq_rel,
                                      std::memory_order_acquire)) return true;
    }
  }

  void close_admission() noexcept {
    state.fetch_or(kClosing, std::memory_order_acq_rel);
    changed.notify_all();
  }

  void end() noexcept {
    std::uint32_t current = state.load(std::memory_order_acquire);
    for (;;) {
      if ((current & kActiveMask) == 0) {
        state.fetch_or(kClosing, std::memory_order_acq_rel);
        changed.notify_all();
        return;
      }
      if (state.compare_exchange_weak(current, current - 1u,
                                      std::memory_order_acq_rel,
                                      std::memory_order_acquire)) break;
    }
    changed.notify_all();
  }

  bool wait_drained() noexcept {
    try {
      std::unique_lock<std::mutex> lock(wait_mutex);
      return changed.wait_for(lock, std::chrono::milliseconds(kShutdownWaitMs),
                              [&] { return (state.load(std::memory_order_acquire) &
                                             kActiveMask) == 0; });
    } catch (...) {
      return false;
    }
  }

  std::atomic<std::uint32_t> state{0};
  std::mutex wait_mutex;
  std::condition_variable changed;
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

// Sole process-lifetime owner. Shutdown joins the helper before releasing the
// long-lived pipe ticket, then closes owner admission, drains borrows/children,
// destroys leases, and releases the owner last. No owner lock is held across a
// wait in this seam.
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

  action_journal_helper::HelperStatus run_pipe_helper() noexcept {
    // First admit the helper call in one atomic fence word. Then acquire a
    // fresh call-local ticket; the long-lived state ticket is never moved or
    // released while the helper can use its owner.
    if (!pipe_call.begin())
      return action_journal_helper::HelperStatus::kStorageUnavailable;
    action_journal_helper::HelperStatus result =
        action_journal_helper::HelperStatus::kStorageUnavailable;
    {
      BorrowTicket ticket = borrow(BorrowKind::kPipe);
      if (ticket.proven()) {
        PipeServerBorrow call_borrow;
        call_borrow.attach(std::move(ticket));
        result = action_journal_helper::run_foreground_helper_from_inherited_stdin(
            std::move(call_borrow));
      }
    }
    pipe_call.end();
    return result;
  }

  bool shutdown_ordered() noexcept {
    if (shutdown_complete.load(std::memory_order_acquire)) return true;
    bool expected = false;
    if (!shutdown_started.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel,
            std::memory_order_acquire))
      return false;
    pipe_call.close_admission();
    if (!pipe_call.wait_drained()) {
      borrow_domain->poison();
      return false;
    }
    pipe.stop();
    process_fence.stop_admission();
    borrow_domain->close_admission();
    process_fence.drain();
    children.drain();
    // The helper call is joined before releasing the long-lived pipe ticket;
    // only then can owner admission close and process tickets drain.
    if (!borrow_domain->wait_process_drained()) {
      borrow_domain->poison();
      return false;
    }
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
  PipeCallFence pipe_call;
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
  ProcessLaunchAuthority(ProcessLaunchAuthority&& other) noexcept
      : owner(std::move(other.owner)) {
    consumed.store(other.consumed.exchange(true, std::memory_order_acq_rel),
                   std::memory_order_release);
  }
  ProcessLaunchAuthority& operator=(ProcessLaunchAuthority&& other) noexcept {
    if (this != &other) {
      owner = std::move(other.owner);
      consumed.store(other.consumed.exchange(true, std::memory_order_acq_rel),
                     std::memory_order_release);
    }
    return *this;
  }
  bool proven() const noexcept {
    return !consumed.load(std::memory_order_acquire) && owner.proven();
  }
  bool consume() noexcept {
    bool expected = false;
    if (!consumed.compare_exchange_strong(expected, true,
                                          std::memory_order_acq_rel,
                                          std::memory_order_acquire)) return false;
    return owner.proven();
  }
  BorrowTicket owner;
  std::atomic_bool consumed{false};
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
