#pragma once

// Source-only fixed-container codec. It owns the complete validated storage
// lease so no operation can fall back to a pathname-selected authority.
#ifndef _WIN32
#error "The ActionJournal helper source is Windows-only"
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
#include <map>
#include <string>
#include <vector>

#include "../action_journal_storage/windows_storage.hpp"
#include "protocol_codec.hpp"

namespace lae::action_journal_helper {

inline constexpr std::uint32_t kSlotCount = 1'024;
inline constexpr std::uint32_t kBanksPerSlot = 2;
inline constexpr std::uint32_t kBankBytes = 16'384;
inline constexpr std::uint32_t kBankBodyBytes = 15'360;
inline constexpr std::uint32_t kBankMarkerBytes = 1'024;
inline constexpr std::uint32_t kEventCellBytes = 896;
inline constexpr std::uint32_t kEventCellsPerBank = 16;
inline constexpr std::uint32_t kMaxCanonicalEventBytes = 768;
inline constexpr std::uint32_t kMaxActiveRecords = 256;
inline constexpr std::uint32_t kMaxTerminalRecords = 768;

enum class StoreStatus : std::uint8_t {
  kOk,
  kNotFound,
  kInvalidTransition,
  kRecordLimitExceeded,
  kEventLimitExceeded,
  kGenerationExhausted,
  kCorruptHeader,
  kCorruptBank,
  kConflictingAuthority,
  kDuplicateOperation,
  kReadFailed,
  kWriteFailed,
  kFlushFailed,
  kReadbackFailed,
  kHashFailed,
  kCancelled,
  kIoTimeout,
  kIoCancelFailed,
  kCommitNonCancellable,
  kInternal,
};

struct JournalEvent {
  std::string action;
  std::string authorization_kind;
  std::string receipt_digest;
  std::string resolution;
  std::uint32_t sequence = 0;
  std::string state;
  std::string canonical_json;
};

struct JournalRecord {
  std::string operation_id;
  std::uint32_t slot_index = 0;
  std::uint32_t bank_index = 0;
  std::uint64_t generation = 0;
  std::array<std::uint8_t, 32> commit_digest{};
  std::vector<JournalEvent> events;
};

struct CancellationProbe {
  using Function = bool (*)(void*) noexcept;
  Function check = nullptr;
  void* context = nullptr;
  bool cancelled() const noexcept { return check != nullptr && check(context); }
};

// `hard_deadline_tick_ms` is a monotonic GetTickCount64 deadline for every
// storage read, write, flush, and full-bank scan.  Request cancellation is
// honored before commit; the hard I/O deadline remains binding inside the
// noncancellable commit section so a stalled filesystem poisons the helper
// instead of pinning its foreground process indefinitely.
struct StorageIoControl {
  CancellationProbe cancellation{};
  std::uint64_t hard_deadline_tick_ms = 0;
  bool honor_request_cancellation = true;

  bool deadline_expired() const noexcept {
    return hard_deadline_tick_ms == 0 ||
        GetTickCount64() >= hard_deadline_tick_ms;
  }
  bool cancellation_requested() const noexcept {
    return honor_request_cancellation && cancellation.cancelled();
  }
  bool stop_requested() const noexcept {
    return deadline_expired() || cancellation_requested();
  }
  StorageIoControl commit_control() const noexcept {
    StorageIoControl result = *this;
    result.honor_request_cancellation = false;
    return result;
  }
};

class FixedContainerStore final {
 public:
  FixedContainerStore(action_journal_storage::JournalStorageLease&& lease,
                      const std::array<std::uint8_t, 32>& container_id) noexcept;
  ~FixedContainerStore() = default;
  FixedContainerStore(const FixedContainerStore&) = delete;
  FixedContainerStore& operator=(const FixedContainerStore&) = delete;

  StoreStatus load_and_recover(StorageIoControl io,
                               std::uint32_t& recovery_count);
  StoreStatus apply(const DecodedRequest& request,
                    StorageIoControl io,
                    EncodedResult& result) noexcept;

 private:
  StoreStatus reload(StorageIoControl io);
  StoreStatus append(const std::string& operation_id,
                     const JournalEvent& event,
                     StorageIoControl io,
                     bool& committed);
  StoreStatus mutation(const DecodedRequest& request,
                       StorageIoControl io,
                       EncodedResult& result);
  StoreStatus summary(const DecodedRequest& request,
                      StorageIoControl io,
                      EncodedResult& result);
  StoreStatus detail(const DecodedRequest& request,
                     StorageIoControl io,
                     EncodedResult& result);

  action_journal_storage::JournalStorageLease lease_;
  HANDLE file_ = INVALID_HANDLE_VALUE;
  std::array<std::uint8_t, 32> container_id_{};
  std::map<std::string, JournalRecord> records_;
  std::array<bool, kSlotCount> occupied_{};
  std::array<std::int8_t, kSlotCount> staged_bank_{};
  std::uint32_t active_count_ = 0;
  std::uint32_t terminal_count_ = 0;
  std::uint32_t recovery_count_ = 0;
  bool commit_section_ = false;
  bool poisoned_ = false;
};

const char* store_status_name(StoreStatus status) noexcept;

}  // namespace lae::action_journal_helper
