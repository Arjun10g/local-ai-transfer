#include "authority.hpp"

#if defined(_WIN32)

#include "../action_journal_helper/journal_authority_owner.hpp"
#include "../action_journal_storage/windows_storage.hpp"

#include <array>
#include <cstdint>
#include <map>
#include <memory>
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
      action_journal_helper::StorageIoControl control) noexcept
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

struct BorrowedOwnerHandle final {
  JournalAuthorityOwner* owner = nullptr;
  const SupervisorState* lifetime = nullptr;
  bool proven() const noexcept { return owner != nullptr && lifetime != nullptr; }
  void release() noexcept { owner = nullptr; lifetime = nullptr; }
};

struct PipeServerBorrow final {
  BorrowedOwnerHandle owner;
  bool stopped = false;
  void stop() noexcept {
    owner.release();
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
  explicit SupervisorState(const SupervisorStartupHandoff& handoff) noexcept
      : startup(handoff) {}
  SupervisorState(const SupervisorState&) = delete;
  SupervisorState& operator=(const SupervisorState&) = delete;
  ~SupervisorState() noexcept { shutdown_ordered(); }

  static std::unique_ptr<SupervisorState> open(
      const SupervisorStartupHandoff& handoff) noexcept {
    (void)handoff;
    // No OS-handle evidence issuer exists in phase 2a.  Refuse before owner
    // open, pipe publication, lease acquisition, or irreversible mutation.
    if (!kProcessLaunchAvailable || !kSupervisorOwnedProcessTransactionAccepted)
      return nullptr;
    return nullptr;
  }

  BorrowedOwnerHandle borrow_for_process() noexcept {
    if (!admission_open || process_fence.admission_stopped || !journal_owner)
      return {};
    return BorrowedOwnerHandle{journal_owner.get(), this};
  }

  void shutdown_ordered() noexcept {
    process_fence.stop_admission();
    process_fence.drain();
    children.drain();
    process_borrow.release();
    pipe.stop();
    leases.clear();
    journal_owner.reset();
    admission_open = false;
  }

  SupervisorStartupHandoff startup;
  // Declared before dependants and explicitly reset last.
  std::unique_ptr<JournalAuthorityOwner> journal_owner;
  ProcessTransactionFence process_fence;
  ChildRegistry children;
  PipeServerBorrow pipe;
  LeaseRegistry leases;
  BorrowedOwnerHandle process_borrow;
  bool admission_open = false;
};

// Process launch borrows the supervisor owner and owns no journal, adapter,
// record, storage handle, or receipt callback.
struct ProcessLaunchAuthority final {
  explicit ProcessLaunchAuthority(SupervisorState& state) noexcept
      : supervisor(&state), owner(state.borrow_for_process()) {}
  ProcessLaunchAuthority(const ProcessLaunchAuthority&) = delete;
  ProcessLaunchAuthority& operator=(const ProcessLaunchAuthority&) = delete;
  SupervisorState* supervisor = nullptr;
  BorrowedOwnerHandle owner;
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
