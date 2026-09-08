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
  bool begin() noexcept {
    if (closing.load(std::memory_order_acquire)) return false;
    bool expected = false;
    return active.compare_exchange_strong(expected, true,
                                          std::memory_order_acq_rel,
                                          std::memory_order_acquire);
  }

  void close_admission() noexcept {
    closing.store(true, std::memory_order_release);
    changed.notify_all();
  }

  void end() noexcept {
    active.store(false, std::memory_order_release);
    changed.notify_all();
  }

  bool wait_drained() noexcept {
    try {
      std::unique_lock<std::mutex> lock(wait_mutex);
      return changed.wait_for(lock, std::chrono::milliseconds(kShutdownWaitMs),
                              [&] { return !active.load(std::memory_order_acquire); });
    } catch (...) {
      return false;
    }
  }

  std::atomic_bool closing{false};
  std::atomic_bool active{false};
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

  action_journal_helper::HelperStatus run_pipe_helper() noexcept {
    // Move the ticket to a call-local handoff.  Shutdown closes this fence
    // and waits for the call to return before releasing the state ticket.
    if (!pipe_call.begin())
      return action_journal_helper::HelperStatus::kStorageUnavailable;
    PipeServerBorrow in_flight = std::move(pipe);
    const auto result =
        action_journal_helper::run_foreground_helper_from_inherited_stdin(
            std::move(in_flight));
    pipe = std::move(in_flight);
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
    process_fence.stop_admission();
    borrow_domain->close_admission();
    pipe_call.close_admission();
    process_fence.drain();
    children.drain();
    // Process tickets must be gone before stopping the pipe.  The pipe owns
    // its own ticket, so it is released next and the final wait drains every
    // remaining ticket before leases or the owner can be destroyed.
    if (!borrow_domain->wait_process_drained()) {
      borrow_domain->poison();
      return false;
    }
    if (!pipe_call.wait_drained()) {
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
