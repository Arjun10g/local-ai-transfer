#pragma once

// Shared only by the inert supervisor/helper seam.  The control block is
// deliberately independent of SupervisorState so a released ticket can keep
// its accounting storage alive until destruction.

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <memory>
#include <mutex>
#include <new>
#include <system_error>
#include <utility>

namespace lae::action_journal_helper {
class JournalAuthorityOwner;
}

namespace lae::windows_supervisor {

struct SupervisorState;
constexpr std::uint32_t kMaxActiveBorrows = 4096;
constexpr std::uint32_t kShutdownWaitMs = 250;

enum class BorrowKind : std::uint8_t { kPipe, kProcess };

// Closing and both bounded counts are one atomic word.  Admission therefore
// linearizes against shutdown, and a process ticket cannot be observed after
// only one of its counters has been incremented.
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
  action_journal_helper::JournalAuthorityOwner* get() const noexcept {
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
               action_journal_helper::JournalAuthorityOwner* owner,
               BorrowKind kind) noexcept
      : control_(std::move(control)), owner_(owner), kind_(kind) {}

  std::shared_ptr<BorrowControlBlock> control_;
  action_journal_helper::JournalAuthorityOwner* owner_ = nullptr;
  BorrowKind kind_ = BorrowKind::kProcess;
};

// A helper call receives this move-only proof object, never a raw owner.  The
// checked accessor is the only owner access path and returns null for an
// empty/moved ticket.
struct PipeServerBorrow final {
  PipeServerBorrow() noexcept = default;
  ~PipeServerBorrow() noexcept = default;
  PipeServerBorrow(const PipeServerBorrow&) = delete;
  PipeServerBorrow& operator=(const PipeServerBorrow&) = delete;
  PipeServerBorrow(PipeServerBorrow&&) noexcept = default;
  PipeServerBorrow& operator=(PipeServerBorrow&&) noexcept = default;

  action_journal_helper::JournalAuthorityOwner* checked_owner() const noexcept {
    return ticket.get();
  }

 private:
  friend struct SupervisorState;
  void attach(BorrowTicket value) noexcept { ticket = std::move(value); }
  void stop() noexcept { ticket.release(); }

  BorrowTicket ticket;
};

}  // namespace lae::windows_supervisor
