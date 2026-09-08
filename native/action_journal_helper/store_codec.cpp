#include "store_codec.hpp"
#include "journal_authority_owner.hpp"

#include <bcrypt.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <exception>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <set>
#include <string_view>
#include <thread>
#include <utility>

namespace lae::action_journal_helper {
namespace {

constexpr std::uint32_t kHeaderBytes = 4'096;
constexpr std::uint32_t kDescriptorBytes = 1'024;
constexpr std::uint32_t kMarkerStaging = 1;
constexpr std::uint32_t kMarkerCommitted = 2;
constexpr std::uint64_t kGenerationNone = UINT64_MAX;
constexpr std::uint32_t kMarkerDigestOffset = 112;
constexpr std::uint32_t kMarkerReservedOffset = 144;
constexpr std::uint32_t kDescriptorReservedOffset = 144;
constexpr std::uint32_t kEventDataOffset = 36;
constexpr DWORD kStorageCancellationGraceMs = 2'000;
constexpr char kContainerDomain[] = "lae.action-journal.container.v0.1.0";
constexpr char kProtocolDomain[] = "lae.action-journal.v0.1.0";
constexpr char kHelperDomain[] = "lae.action-journal.helper.v0.1.0";
constexpr std::array<std::uint8_t, 16> kBodyMagic = {
    'L', 'A', 'E', 'J', 'R', 'N', 'L', 'B', 'O', 'D', 'Y', 0, 0, 0, 0, 0};
constexpr std::array<std::uint8_t, 16> kMarkerMagic = {
    'L', 'A', 'E', 'J', 'R', 'N', 'L', 'M', 'A', 'R', 'K', 'E', 'R', 0, 0, 0};

struct Marker {
  enum class State { kUnused, kStaging, kCommitted } state = State::kUnused;
  std::uint64_t generation = 0;
  std::uint64_t previous_generation = 0;
  std::array<std::uint8_t, 32> body_digest{};
  std::array<std::uint8_t, 32> previous_commit_digest{};
  std::array<std::uint8_t, 32> commit_digest{};
};

struct Bank {
  std::uint32_t index = 0;
  Marker marker;
  JournalRecord record;
  bool has_record = false;
};

bool all_zero(const std::uint8_t* data, std::size_t count) noexcept {
  std::uint8_t total = 0;
  for (std::size_t index = 0; index < count; ++index) total |= data[index];
  return total == 0;
}

bool equal_bytes(const std::uint8_t* left, const std::uint8_t* right,
                 std::size_t count) noexcept {
  std::uint8_t difference = 0;
  for (std::size_t index = 0; index < count; ++index)
    difference |= static_cast<std::uint8_t>(left[index] ^ right[index]);
  return difference == 0;
}

std::uint32_t read_u32(const std::uint8_t* bytes) noexcept {
  return static_cast<std::uint32_t>(bytes[0]) |
      (static_cast<std::uint32_t>(bytes[1]) << 8) |
      (static_cast<std::uint32_t>(bytes[2]) << 16) |
      (static_cast<std::uint32_t>(bytes[3]) << 24);
}

std::uint64_t read_u64(const std::uint8_t* bytes) noexcept {
  std::uint64_t value = 0;
  for (std::size_t index = 0; index < 8; ++index)
    value |= static_cast<std::uint64_t>(bytes[index]) << (index * 8);
  return value;
}

void write_u32(std::uint8_t* bytes, std::uint32_t value) noexcept {
  for (std::size_t index = 0; index < 4; ++index)
    bytes[index] = static_cast<std::uint8_t>(value >> (index * 8));
}

void write_u64(std::uint8_t* bytes, std::uint64_t value) noexcept {
  for (std::size_t index = 0; index < 8; ++index)
    bytes[index] = static_cast<std::uint8_t>(value >> (index * 8));
}

std::uint64_t slot_offset(std::uint32_t slot) noexcept {
  return kHeaderBytes + static_cast<std::uint64_t>(slot) *
      kBanksPerSlot * kBankBytes;
}

std::uint64_t bank_offset(std::uint32_t slot, std::uint32_t bank) noexcept {
  return slot_offset(slot) + static_cast<std::uint64_t>(bank) * kBankBytes;
}

std::uint64_t marker_offset(std::uint32_t slot, std::uint32_t bank) noexcept {
  return bank_offset(slot, bank) + kBankBodyBytes;
}

bool seek(HANDLE file, std::uint64_t offset) noexcept {
  LARGE_INTEGER value{};
  value.QuadPart = static_cast<LONGLONG>(offset);
  return SetFilePointerEx(file, value, nullptr, FILE_BEGIN) != FALSE;
}

enum class BoundedIoStatus : std::uint8_t {
  kOk,
  kFailed,
  kCancelled,
  kTimeout,
  kCancellationUnproven,
};

enum class SyncIoKind : std::uint8_t { kRead, kWrite, kFlush };

// The merged storage lease deliberately owns a synchronous, write-through
// handle.  Therefore this slice cannot pretend CancelIoEx makes its I/O
// bounded.  Each operation runs on one dedicated thread so
// CancelSynchronousIo can target that exact thread.  All buffers and a
// duplicated file handle are heap-owned by the operation: if cancellation
// cannot be proved within the finite grace period, the store is poisoned, the
// worker is detached safely, and the foreground helper immediately exits.
struct SyncIoState final {
  ~SyncIoState() {
    if (file != nullptr && file != INVALID_HANDLE_VALUE) CloseHandle(file);
    if (!bytes.empty()) SecureZeroMemory(bytes.data(), bytes.size());
  }
  HANDLE file = INVALID_HANDLE_VALUE;
  SyncIoKind kind = SyncIoKind::kRead;
  std::uint64_t offset = 0;
  std::vector<std::uint8_t> bytes;
  std::mutex mutex;
  std::condition_variable changed;
  bool complete = false;
  bool ok = false;
};

void run_sync_io(const std::shared_ptr<SyncIoState>& state) noexcept {
  bool ok = false;
  if (state->kind == SyncIoKind::kFlush) {
    ok = FlushFileBuffers(state->file) != FALSE;
  } else if (seek(state->file, state->offset)) {
    std::size_t completed = 0;
    ok = true;
    while (completed < state->bytes.size()) {
      const DWORD requested = static_cast<DWORD>(std::min<std::size_t>(
          state->bytes.size() - completed, 65'536));
      DWORD observed = 0;
      const BOOL result = state->kind == SyncIoKind::kWrite
          ? WriteFile(state->file, state->bytes.data() + completed, requested,
                      &observed, nullptr)
          : ReadFile(state->file, state->bytes.data() + completed, requested,
                     &observed, nullptr);
      if (!result || observed != requested) {
        ok = false;
        break;
      }
      completed += observed;
    }
  }
  {
    const std::lock_guard<std::mutex> lock(state->mutex);
    state->ok = ok;
    state->complete = true;
  }
  state->changed.notify_all();
}

BoundedIoStatus bounded_sync_io(HANDLE file, SyncIoKind kind,
                                std::uint64_t offset,
                                std::uint8_t* bytes, std::size_t count,
                                StorageIoControl control) {
  const bool request_cancelled = control.cancellation_requested();
  if (request_cancelled) return BoundedIoStatus::kCancelled;
  if (control.deadline_expired()) return BoundedIoStatus::kTimeout;
  if (file == nullptr || file == INVALID_HANDLE_VALUE ||
      (kind != SyncIoKind::kFlush && (bytes == nullptr || count == 0)))
    return BoundedIoStatus::kFailed;
  auto state = std::make_shared<SyncIoState>();
  HANDLE duplicate = INVALID_HANDLE_VALUE;
  if (!DuplicateHandle(GetCurrentProcess(), file, GetCurrentProcess(),
                       &duplicate, 0, FALSE, DUPLICATE_SAME_ACCESS))
    return BoundedIoStatus::kFailed;
  state->file = duplicate;
  state->kind = kind;
  state->offset = offset;
  if (kind != SyncIoKind::kFlush) {
    state->bytes.resize(count);
    if (kind == SyncIoKind::kWrite)
      std::copy_n(bytes, count, state->bytes.data());
  }
  std::thread worker(run_sync_io, state);
  bool stopped_for_request = false;
  bool stopped_for_deadline = false;
  {
    std::unique_lock<std::mutex> lock(state->mutex);
    while (!state->complete) {
      state->changed.wait_for(lock, std::chrono::milliseconds(1));
      if (state->complete) break;
      stopped_for_request = control.cancellation_requested();
      stopped_for_deadline = control.deadline_expired();
      if (stopped_for_request || stopped_for_deadline) break;
    }
  }
  if (stopped_for_request || stopped_for_deadline) {
    const BOOL cancellation_started =
        CancelSynchronousIo(static_cast<HANDLE>(worker.native_handle()));
    const DWORD cancellation_error = cancellation_started
        ? ERROR_SUCCESS : GetLastError();
    bool settled = false;
    {
      std::unique_lock<std::mutex> lock(state->mutex);
      settled = state->changed.wait_for(
          lock, std::chrono::milliseconds(kStorageCancellationGraceMs),
          [&state] { return state->complete; });
    }
    if (!settled || (!cancellation_started &&
                     cancellation_error != ERROR_NOT_FOUND)) {
      worker.detach();
      return BoundedIoStatus::kCancellationUnproven;
    }
    worker.join();
    return stopped_for_request ? BoundedIoStatus::kCancelled
                               : BoundedIoStatus::kTimeout;
  }
  worker.join();
  if (!state->ok) return BoundedIoStatus::kFailed;
  // A successful blocking call and the waiter's last probe are distinct
  // events. Do not accept bytes if cancellation or the absolute deadline
  // became observable as the call completed.
  if (control.cancellation_requested()) return BoundedIoStatus::kCancelled;
  if (control.deadline_expired()) return BoundedIoStatus::kTimeout;
  if (kind == SyncIoKind::kRead)
    std::copy_n(state->bytes.data(), count, bytes);
  return BoundedIoStatus::kOk;
}

BoundedIoStatus read_exact(HANDLE file, std::uint64_t offset,
                           std::uint8_t* output, std::size_t count,
                           StorageIoControl control) {
  return bounded_sync_io(file, SyncIoKind::kRead, offset, output, count,
                         control);
}

BoundedIoStatus write_exact(HANDLE file, std::uint64_t offset,
                            const std::uint8_t* input, std::size_t count,
                            StorageIoControl control) {
  return bounded_sync_io(file, SyncIoKind::kWrite, offset,
                         const_cast<std::uint8_t*>(input), count, control);
}

BoundedIoStatus flush_exact(HANDLE file, StorageIoControl control) {
  return bounded_sync_io(file, SyncIoKind::kFlush, 0, nullptr, 0, control);
}

StoreStatus read_status(BoundedIoStatus status) noexcept {
  switch (status) {
    case BoundedIoStatus::kOk: return StoreStatus::kOk;
    case BoundedIoStatus::kCancelled: return StoreStatus::kCancelled;
    case BoundedIoStatus::kTimeout: return StoreStatus::kIoTimeout;
    case BoundedIoStatus::kCancellationUnproven:
      return StoreStatus::kIoCancelFailed;
    case BoundedIoStatus::kFailed: return StoreStatus::kReadFailed;
  }
  return StoreStatus::kInternal;
}

StoreStatus write_status(BoundedIoStatus status) noexcept {
  const auto common = read_status(status);
  return common == StoreStatus::kReadFailed ? StoreStatus::kWriteFailed : common;
}

bool sha256(const std::vector<std::pair<const std::uint8_t*, std::size_t>>& parts,
            std::array<std::uint8_t, 32>& output,
            const std::uint8_t* hmac_key = nullptr,
            std::size_t hmac_bytes = 0) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_length = 0, returned = 0;
  std::vector<std::uint8_t> object;
  const ULONG flags = hmac_key == nullptr ? 0 : BCRYPT_ALG_HANDLE_HMAC_FLAG;
  bool ok = BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(
      &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, flags));
  if (ok) ok = BCRYPT_SUCCESS(BCryptGetProperty(
      algorithm, BCRYPT_OBJECT_LENGTH,
      reinterpret_cast<PUCHAR>(&object_length), sizeof(object_length),
      &returned, 0));
  if (ok && (object_length == 0 || object_length > 65'536)) ok = false;
  if (ok) object.resize(object_length);
  if (ok) ok = BCRYPT_SUCCESS(BCryptCreateHash(
      algorithm, &hash, object.data(), object_length,
      const_cast<PUCHAR>(hmac_key), static_cast<ULONG>(hmac_bytes), 0));
  for (const auto& part : parts) {
    if (!ok || part.second > std::numeric_limits<ULONG>::max()) {
      ok = false;
      break;
    }
    if (part.second != 0) ok = BCRYPT_SUCCESS(BCryptHashData(
        hash, const_cast<PUCHAR>(part.first), static_cast<ULONG>(part.second), 0));
  }
  if (ok) ok = BCRYPT_SUCCESS(BCryptFinishHash(
      hash, output.data(), static_cast<ULONG>(output.size()), 0));
  if (hash != nullptr) BCryptDestroyHash(hash);
  if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
  if (!object.empty()) SecureZeroMemory(object.data(), object.size());
  return ok;
}

bool domain_digest(std::string_view label,
                   const std::array<std::uint8_t, 32>& container_id,
                   const std::uint8_t* bytes, std::size_t count,
                   std::array<std::uint8_t, 32>& output) {
  const std::string domain = std::string(kContainerDomain) + '\0';
  const std::string labelled = std::string(label) + '\0';
  return sha256({{reinterpret_cast<const std::uint8_t*>(domain.data()), domain.size()},
                 {reinterpret_cast<const std::uint8_t*>(labelled.data()), labelled.size()},
                 {container_id.data(), container_id.size()}, {bytes, count}}, output);
}

bool protocol_event_digest(std::string_view canonical,
                           std::array<std::uint8_t, 32>& output) {
  const std::string prefix = std::string(kProtocolDomain) + "\0event\0";
  return sha256({{reinterpret_cast<const std::uint8_t*>(prefix.data()), prefix.size()},
                 {reinterpret_cast<const std::uint8_t*>(canonical.data()), canonical.size()}},
                output);
}

std::string hex(const std::uint8_t* value, std::size_t count) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string result(count * 2, '0');
  for (std::size_t index = 0; index < count; ++index) {
    result[index * 2] = kHex[value[index] >> 4];
    result[index * 2 + 1] = kHex[value[index] & 0xf];
  }
  return result;
}

bool identifier(std::string_view value, std::string_view prefix,
                std::size_t digits) noexcept {
  if (value.size() != prefix.size() + digits ||
      value.substr(0, prefix.size()) != prefix) return false;
  for (std::size_t index = prefix.size(); index < value.size(); ++index) {
    const char character = value[index];
    if (!((character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f'))) return false;
  }
  return true;
}

bool exact_keys(const nlohmann::json& value,
                std::initializer_list<std::string_view> keys) {
  if (!value.is_object() || value.size() != keys.size()) return false;
  for (const auto key : keys)
    if (!value.contains(std::string(key))) return false;
  return true;
}

bool one_of(std::string_view value,
            std::initializer_list<std::string_view> allowed) noexcept {
  return std::find(allowed.begin(), allowed.end(), value) != allowed.end();
}

bool event_json(const nlohmann::json& value, JournalEvent& result) {
  if (!exact_keys(value, {"action", "authorization_kind", "receipt_digest",
                          "resolution", "sequence", "state"}) ||
      !value["action"].is_string() || !value["receipt_digest"].is_string() ||
      !identifier(value["receipt_digest"].get_ref<const std::string&>(), "", 64) ||
      !value["sequence"].is_number_unsigned() ||
      value["sequence"].get<std::uint64_t>() >= kEventCellsPerBank ||
      !value["state"].is_string())
    return false;
  const bool authorization_valid = value["authorization_kind"].is_null() ||
      (value["authorization_kind"].is_string() &&
       one_of(value["authorization_kind"].get_ref<const std::string&>(),
              {"policy", "user_confirmation", "operator_grant"}));
  const bool resolution_valid = value["resolution"].is_null() ||
      (value["resolution"].is_string() &&
       one_of(value["resolution"].get_ref<const std::string&>(),
              {"provider_acknowledged", "completed", "manual_completed",
               "user_denied", "request_cancelled", "pre_dispatch_failure",
               "manual_failed_definitive", "dispatch_ambiguous",
               "startup_recovery"}));
  if (!authorization_valid || !resolution_valid)
    return false;
  result.action = value["action"].get<std::string>();
  result.authorization_kind = value["authorization_kind"].is_null()
      ? "" : value["authorization_kind"].get<std::string>();
  result.receipt_digest = value["receipt_digest"].get<std::string>();
  result.resolution = value["resolution"].is_null()
      ? "" : value["resolution"].get<std::string>();
  result.sequence = value["sequence"].get<std::uint32_t>();
  result.state = value["state"].get<std::string>();
  result.canonical_json = value.dump();
  return true;
}

bool terminal(std::string_view state) noexcept {
  return state == "completed" || state == "cancelled" ||
      state == "failed_definitive";
}

bool expected_transition(const JournalEvent* previous,
                         const JournalEvent& current) noexcept {
  if (previous == nullptr)
    return current.sequence == 0 && current.action == "prepare" &&
        current.state == "prepared" && current.authorization_kind.empty() &&
        current.resolution.empty();
  if (current.sequence != previous->sequence + 1) return false;
  if (current.action == "authorize")
    return previous->state == "prepared" && current.state == "authorized" &&
        previous->authorization_kind.empty() && !current.authorization_kind.empty() &&
        current.resolution.empty();
  if (current.authorization_kind != previous->authorization_kind) return false;
  if (current.action == "dispatch")
    return previous->state == "authorized" && current.state == "dispatching" &&
        current.resolution.empty();
  if (current.action == "acknowledge")
    return previous->state == "dispatching" && current.state == "acknowledged" &&
        current.resolution == "provider_acknowledged";
  if (current.action == "begin_reconciliation")
    return (previous->state == "dispatching" || previous->state == "acknowledged") &&
        current.state == "reconciling" && current.resolution.empty();
  if (current.action == "complete") {
    const bool manual = previous->state == "unknown_manual";
    return (previous->state == "acknowledged" || previous->state == "reconciling" || manual) &&
        current.state == "completed" &&
        current.resolution == (manual ? "manual_completed" : "completed");
  }
  if (current.action == "cancel")
    return (previous->state == "prepared" || previous->state == "authorized") &&
        current.state == "cancelled" &&
        (current.resolution == "user_denied" ||
         current.resolution == "request_cancelled");
  if (current.action == "fail_definitive") {
    const bool manual = previous->state == "unknown_manual";
    return (previous->state == "prepared" || previous->state == "authorized" ||
            previous->state == "reconciling" || manual) &&
        current.state == "failed_definitive" &&
        current.resolution == (manual ? "manual_failed_definitive"
                                      : "pre_dispatch_failure");
  }
  if (current.action == "mark_unknown")
    return (previous->state == "dispatching" || previous->state == "acknowledged" ||
            previous->state == "reconciling") && current.state == "unknown_manual" &&
        current.resolution == "dispatch_ambiguous";
  if (current.action == "startup_recovery") {
    if (previous->state == "prepared" || previous->state == "authorized")
      return current.state == "cancelled" && current.resolution == "startup_recovery";
    return (previous->state == "dispatching" || previous->state == "acknowledged" ||
            previous->state == "reconciling") && current.state == "unknown_manual" &&
        current.resolution == "dispatch_ambiguous";
  }
  return false;
}

bool validate_history(const std::vector<JournalEvent>& events) noexcept {
  if (events.empty() || events.size() > kEventCellsPerBank) return false;
  for (std::size_t index = 0; index < events.size(); ++index)
    if (!expected_transition(index == 0 ? nullptr : &events[index - 1], events[index]))
      return false;
  return true;
}

bool decode_marker(const std::array<std::uint8_t, kBankMarkerBytes>& bytes,
                   std::uint32_t slot, std::uint32_t bank,
                   const std::array<std::uint8_t, 32>& container_id,
                   Marker& marker) {
  marker = {};
  if (all_zero(bytes.data(), bytes.size())) return true;
  if (!equal_bytes(bytes.data(), kMarkerMagic.data(), kMarkerMagic.size()) ||
      read_u32(bytes.data() + 16) != 1 || read_u32(bytes.data() + 24) != slot ||
      read_u32(bytes.data() + 28) != bank ||
      !all_zero(bytes.data() + kMarkerReservedOffset,
                bytes.size() - kMarkerReservedOffset)) return false;
  const auto state = read_u32(bytes.data() + 20);
  if (state != kMarkerStaging && state != kMarkerCommitted) return false;
  auto digest_input = bytes;
  std::fill(digest_input.begin() + kMarkerDigestOffset,
            digest_input.begin() + kMarkerDigestOffset + 32, 0);
  std::array<std::uint8_t, 32> expected{};
  if (!domain_digest("bank-marker", container_id, digest_input.data(),
                     digest_input.size(), expected) ||
      !equal_bytes(expected.data(), bytes.data() + kMarkerDigestOffset, 32))
    return false;
  marker.state = state == kMarkerStaging ? Marker::State::kStaging
                                         : Marker::State::kCommitted;
  marker.generation = read_u64(bytes.data() + 32);
  marker.previous_generation = read_u64(bytes.data() + 40);
  std::copy_n(bytes.data() + 48, 32, marker.body_digest.begin());
  std::copy_n(bytes.data() + 80, 32, marker.previous_commit_digest.begin());
  std::copy_n(bytes.data() + 112, 32, marker.commit_digest.begin());
  if ((marker.state == Marker::State::kStaging &&
       !all_zero(marker.body_digest.data(), marker.body_digest.size())) ||
      (marker.state == Marker::State::kCommitted &&
       all_zero(marker.body_digest.data(), marker.body_digest.size()))) return false;
  if (marker.generation == 0)
    return marker.previous_generation == kGenerationNone &&
        all_zero(marker.previous_commit_digest.data(),
                 marker.previous_commit_digest.size());
  return marker.previous_generation == marker.generation - 1 &&
      !all_zero(marker.previous_commit_digest.data(),
                marker.previous_commit_digest.size());
}

bool decode_body(const std::array<std::uint8_t, kBankBodyBytes>& body,
                 std::uint32_t slot, std::uint32_t bank, const Marker& marker,
                 const std::array<std::uint8_t, 32>& container_id,
                 JournalRecord& record) {
  std::array<std::uint8_t, 32> body_digest{};
  if (!equal_bytes(body.data(), kBodyMagic.data(), kBodyMagic.size()) ||
      read_u32(body.data() + 16) != 1 || read_u32(body.data() + 20) != slot ||
      read_u32(body.data() + 24) != bank ||
      read_u64(body.data() + 32) != marker.generation ||
      !all_zero(body.data() + kDescriptorReservedOffset,
                kDescriptorBytes - kDescriptorReservedOffset) ||
      !domain_digest("bank-body", container_id, body.data(), body.size(), body_digest) ||
      !equal_bytes(body_digest.data(), marker.body_digest.data(), body_digest.size()))
    return false;
  const auto count = read_u32(body.data() + 28);
  if (count == 0 || count > kEventCellsPerBank) return false;
  const std::string operation(reinterpret_cast<const char*>(body.data() + 40), 36);
  if (!identifier(operation, "act_", 32)) return false;
  std::vector<JournalEvent> events;
  std::vector<std::uint8_t> framed;
  std::uint32_t used = 0;
  for (std::uint32_t index = 0; index < kEventCellsPerBank; ++index) {
    const std::uint8_t* cell = body.data() + kDescriptorBytes + index * kEventCellBytes;
    if (index >= count) {
      if (!all_zero(cell, kEventCellBytes)) return false;
      continue;
    }
    const auto length = read_u32(cell);
    if (length < 2 || length > kMaxCanonicalEventBytes ||
        !all_zero(cell + kEventDataOffset + length,
                  kEventCellBytes - kEventDataOffset - length)) return false;
    const std::string text(reinterpret_cast<const char*>(cell + kEventDataOffset),
                           length);
    nlohmann::json parsed;
    try {
      parsed = nlohmann::json::parse(text, nullptr, true, false);
    } catch (...) { return false; }
    if (parsed.is_discarded() || parsed.dump() != text) return false;
    JournalEvent event;
    if (!event_json(parsed, event)) return false;
    std::array<std::uint8_t, 32> event_digest{};
    if (!protocol_event_digest(text, event_digest) ||
        !equal_bytes(event_digest.data(), cell + 4, event_digest.size())) return false;
    const auto old_size = framed.size();
    framed.resize(old_size + 4 + event_digest.size() + length);
    write_u32(framed.data() + old_size, length);
    std::copy(event_digest.begin(), event_digest.end(), framed.begin() + old_size + 4);
    std::memcpy(framed.data() + old_size + 36, text.data(), length);
    used += length;
    events.push_back(std::move(event));
  }
  std::array<std::uint8_t, 32> events_digest{};
  if (!validate_history(events) || read_u32(body.data() + 76) != used ||
      !domain_digest("events", container_id, framed.data(), framed.size(), events_digest) ||
      !equal_bytes(events_digest.data(), body.data() + 80, events_digest.size()))
    return false;
  std::array<std::uint8_t, 32> final_digest{};
  if (!protocol_event_digest(events.back().canonical_json, final_digest) ||
      !equal_bytes(final_digest.data(), body.data() + 112, final_digest.size()))
    return false;
  record.operation_id = operation;
  record.slot_index = slot;
  record.bank_index = bank;
  record.generation = marker.generation;
  record.commit_digest = marker.commit_digest;
  record.events = std::move(events);
  return true;
}

bool same_history_prefix(const JournalRecord& low,
                         const JournalRecord& high) noexcept {
  if (low.operation_id != high.operation_id ||
      high.events.size() != low.events.size() + 1) return false;
  for (std::size_t index = 0; index < low.events.size(); ++index)
    if (low.events[index].canonical_json != high.events[index].canonical_json)
      return false;
  return true;
}

bool encode_marker(Marker::State state, std::uint32_t slot, std::uint32_t bank,
                   std::uint64_t generation, std::uint64_t previous_generation,
                   const std::array<std::uint8_t, 32>& body_digest,
                   const std::array<std::uint8_t, 32>& previous_commit,
                   const std::array<std::uint8_t, 32>& container_id,
                   std::array<std::uint8_t, kBankMarkerBytes>& output) {
  output.fill(0);
  std::copy(kMarkerMagic.begin(), kMarkerMagic.end(), output.begin());
  write_u32(output.data() + 16, 1);
  write_u32(output.data() + 20,
            state == Marker::State::kStaging ? kMarkerStaging : kMarkerCommitted);
  write_u32(output.data() + 24, slot);
  write_u32(output.data() + 28, bank);
  write_u64(output.data() + 32, generation);
  write_u64(output.data() + 40, previous_generation);
  std::copy(body_digest.begin(), body_digest.end(), output.begin() + 48);
  std::copy(previous_commit.begin(), previous_commit.end(), output.begin() + 80);
  std::array<std::uint8_t, 32> digest{};
  if (!domain_digest("bank-marker", container_id, output.data(), output.size(),
                     digest)) return false;
  std::copy(digest.begin(), digest.end(), output.begin() + kMarkerDigestOffset);
  return true;
}

bool encode_body(std::uint32_t slot, std::uint32_t bank,
                 std::uint64_t generation, const std::string& operation,
                 const std::vector<JournalEvent>& events,
                 const std::array<std::uint8_t, 32>& container_id,
                 std::array<std::uint8_t, kBankBodyBytes>& output,
                 std::array<std::uint8_t, 32>& body_digest) {
  if (!identifier(operation, "act_", 32) || !validate_history(events)) return false;
  output.fill(0);
  std::copy(kBodyMagic.begin(), kBodyMagic.end(), output.begin());
  write_u32(output.data() + 16, 1);
  write_u32(output.data() + 20, slot);
  write_u32(output.data() + 24, bank);
  write_u32(output.data() + 28, static_cast<std::uint32_t>(events.size()));
  write_u64(output.data() + 32, generation);
  std::memcpy(output.data() + 40, operation.data(), operation.size());
  std::vector<std::uint8_t> framed;
  std::uint32_t used = 0;
  std::array<std::uint8_t, 32> final_digest{};
  for (std::size_t index = 0; index < events.size(); ++index) {
    const auto& text = events[index].canonical_json;
    if (text.size() < 2 || text.size() > kMaxCanonicalEventBytes) return false;
    std::array<std::uint8_t, 32> event_digest{};
    if (!protocol_event_digest(text, event_digest)) return false;
    std::uint8_t* cell = output.data() + kDescriptorBytes + index * kEventCellBytes;
    write_u32(cell, static_cast<std::uint32_t>(text.size()));
    std::copy(event_digest.begin(), event_digest.end(), cell + 4);
    std::memcpy(cell + kEventDataOffset, text.data(), text.size());
    const auto old_size = framed.size();
    framed.resize(old_size + 4 + event_digest.size() + text.size());
    write_u32(framed.data() + old_size, static_cast<std::uint32_t>(text.size()));
    std::copy(event_digest.begin(), event_digest.end(), framed.begin() + old_size + 4);
    std::memcpy(framed.data() + old_size + 36, text.data(), text.size());
    used += static_cast<std::uint32_t>(text.size());
    final_digest = event_digest;
  }
  write_u32(output.data() + 76, used);
  std::array<std::uint8_t, 32> events_digest{};
  if (!domain_digest("events", container_id, framed.data(), framed.size(), events_digest))
    return false;
  std::copy(events_digest.begin(), events_digest.end(), output.begin() + 80);
  std::copy(final_digest.begin(), final_digest.end(), output.begin() + 112);
  return domain_digest("bank-body", container_id, output.data(), output.size(),
                       body_digest);
}

nlohmann::json receipt(const std::string& operation,
                       const JournalEvent& event) {
  return {
      {"authorization_kind", event.authorization_kind.empty()
           ? nlohmann::json(nullptr) : nlohmann::json(event.authorization_kind)},
      {"operation_id", operation},
      {"receipt_digest", event.receipt_digest},
      {"recovery_required", event.state == "reconciling" ||
                                 event.state == "unknown_manual"},
      {"redacted", true},
      {"resolution", event.resolution.empty()
           ? nlohmann::json(nullptr) : nlohmann::json(event.resolution)},
      {"sequence", event.sequence},
      {"state", event.state},
  };
}

std::string helper_digest(std::string_view label,
                          const std::vector<std::string_view>& parts) {
  const std::string prefix = std::string(kHelperDomain) + '\0' +
      std::string(label) + '\0';
  std::vector<std::pair<const std::uint8_t*, std::size_t>> input = {
      {reinterpret_cast<const std::uint8_t*>(prefix.data()), prefix.size()}};
  for (const auto part : parts)
    input.push_back({reinterpret_cast<const std::uint8_t*>(part.data()), part.size()});
  std::array<std::uint8_t, 32> output{};
  if (!sha256(input, output)) return {};
  return hex(output.data(), output.size());
}

std::string operation_id(const std::array<std::uint8_t, 32>& container_id,
                         const std::string& operation_digest) {
  const std::string prefix("operation\0", 10);
  std::array<std::uint8_t, 32> output{};
  if (!sha256({{reinterpret_cast<const std::uint8_t*>(prefix.data()), prefix.size()},
               {reinterpret_cast<const std::uint8_t*>(operation_digest.data()),
                operation_digest.size()}}, output,
              container_id.data(), container_id.size())) return {};
  return "act_" + hex(output.data(), 16);
}

JournalEvent make_event(const std::string& operation,
                        const JournalEvent* previous, std::string action,
                        const nlohmann::json& body, std::string state,
                        std::string authorization, std::string resolution) {
  JournalEvent event;
  event.action = std::move(action);
  event.authorization_kind = std::move(authorization);
  event.resolution = std::move(resolution);
  event.sequence = previous == nullptr ? 0 : previous->sequence + 1;
  event.state = std::move(state);
  if (event.action == "prepare") {
    const auto canonical = body.dump();
    event.receipt_digest = helper_digest("prepare-binding", {canonical});
  } else {
    const std::string canonical = body.dump();
    const std::string previous_digest = previous == nullptr ? "" : previous->receipt_digest;
    event.receipt_digest = helper_digest(
        "receipt", {operation, std::string_view("\0", 1), previous_digest,
                    std::string_view("\0", 1), event.action,
                    std::string_view("\0", 1), canonical,
                    std::string_view("\0", 1), event.state,
                    std::string_view("\0", 1), event.authorization_kind,
                    std::string_view("\0", 1), event.resolution});
  }
  nlohmann::json value = {
      {"action", event.action},
      {"authorization_kind", event.authorization_kind.empty()
          ? nlohmann::json(nullptr) : nlohmann::json(event.authorization_kind)},
      {"receipt_digest", event.receipt_digest},
      {"resolution", event.resolution.empty()
          ? nlohmann::json(nullptr) : nlohmann::json(event.resolution)},
      {"sequence", event.sequence}, {"state", event.state},
  };
  event.canonical_json = value.dump();
  return event;
}

bool response_state(const JournalEvent& previous, const DecodedRequest& request,
                    std::string& state, std::string& authorization,
                    std::string& resolution) {
  authorization = previous.authorization_kind;
  resolution.clear();
  if (request.method == "authorize") {
    if (previous.state != "prepared") return false;
    state = "authorized";
    authorization = request.body["authorization_kind"].get<std::string>();
  } else if (request.method == "dispatch" && previous.state == "authorized")
    state = "dispatching";
  else if (request.method == "acknowledge" && previous.state == "dispatching") {
    state = "acknowledged"; resolution = "provider_acknowledged";
  } else if (request.method == "begin_reconciliation" &&
             (previous.state == "dispatching" || previous.state == "acknowledged"))
    state = "reconciling";
  else if (request.method == "complete" &&
           (previous.state == "acknowledged" || previous.state == "reconciling" ||
            previous.state == "unknown_manual")) {
    state = "completed"; resolution = request.body["resolution"].get<std::string>();
    const std::string expected = previous.state == "unknown_manual"
        ? "manual_completed" : "completed";
    if (resolution != expected) return false;
  } else if (request.method == "cancel" &&
             (previous.state == "prepared" || previous.state == "authorized")) {
    state = "cancelled"; resolution = request.body["resolution"].get<std::string>();
  } else if (request.method == "fail_definitive" &&
             (previous.state == "prepared" || previous.state == "authorized" ||
              previous.state == "reconciling" || previous.state == "unknown_manual")) {
    state = "failed_definitive";
    resolution = request.body["resolution"].get<std::string>();
    const std::string expected = previous.state == "unknown_manual"
        ? "manual_failed_definitive" : "pre_dispatch_failure";
    if (resolution != expected) return false;
  } else if (request.method == "mark_unknown" &&
             (previous.state == "dispatching" || previous.state == "acknowledged" ||
              previous.state == "reconciling")) {
    state = "unknown_manual"; resolution = "dispatch_ambiguous";
  } else return false;
  return true;
}

}  // namespace

FixedContainerStore::FixedContainerStore(
    action_journal_storage::JournalStorageLease& lease,
    const std::array<std::uint8_t, 32>& container_id) noexcept
    : lease_(&lease), file_(lease.retained_file_handle()),
      container_id_(container_id) {}

StoreStatus FixedContainerStore::reload(StorageIoControl io) {
  try {
    if (lease_ == nullptr || !lease_->valid() || file_ == INVALID_HANDLE_VALUE)
      return StoreStatus::kReadFailed;
    if (io.stop_requested())
      return io.cancellation_requested() ? StoreStatus::kCancelled
                                         : StoreStatus::kIoTimeout;
    std::map<std::string, JournalRecord> next;
    std::array<bool, kSlotCount> occupied{};
    std::array<std::int8_t, kSlotCount> staged_bank{};
    staged_bank.fill(-1);
    std::uint32_t active = 0, terminal_count = 0;
    constexpr std::size_t kScanBytes = static_cast<std::size_t>(kSlotCount) *
        kBanksPerSlot * kBankBytes;
    std::vector<std::uint8_t> snapshot(kScanBytes);
    auto bounded = read_exact(file_, kHeaderBytes, snapshot.data(),
                              snapshot.size(), io);
    auto bounded_status = read_status(bounded);
    if (bounded_status != StoreStatus::kOk) {
      return bounded_status;
    }
    for (std::uint32_t slot = 0; slot < kSlotCount; ++slot) {
      if (io.stop_requested())
        return io.cancellation_requested() ? StoreStatus::kCancelled
                                           : StoreStatus::kIoTimeout;
      std::array<Bank, kBanksPerSlot> banks{};
      for (std::uint32_t bank = 0; bank < kBanksPerSlot; ++bank) {
        if (io.stop_requested())
          return io.cancellation_requested() ? StoreStatus::kCancelled
                                             : StoreStatus::kIoTimeout;
        banks[bank].index = bank;
        const std::size_t bank_start =
            (static_cast<std::size_t>(slot) * kBanksPerSlot + bank) * kBankBytes;
        std::array<std::uint8_t, kBankMarkerBytes> marker_bytes{};
        std::copy_n(snapshot.data() + bank_start + kBankBodyBytes,
                    marker_bytes.size(), marker_bytes.data());
        if (!decode_marker(marker_bytes, slot, bank, container_id_, banks[bank].marker))
          return StoreStatus::kCorruptBank;
        std::array<std::uint8_t, kBankBodyBytes> body{};
        std::copy_n(snapshot.data() + bank_start, body.size(), body.data());
        if (banks[bank].marker.state == Marker::State::kUnused) {
          if (!all_zero(body.data(), body.size())) return StoreStatus::kCorruptBank;
        } else if (banks[bank].marker.state == Marker::State::kCommitted) {
          if (!decode_body(body, slot, bank, banks[bank].marker, container_id_,
                           banks[bank].record)) return StoreStatus::kCorruptBank;
          banks[bank].has_record = true;
        }
        if (io.stop_requested())
          return io.cancellation_requested() ? StoreStatus::kCancelled
                                             : StoreStatus::kIoTimeout;
      }
      Bank* authority = nullptr;
      std::vector<Bank*> committed;
      std::vector<Bank*> staging;
      for (auto& bank : banks) {
        if (bank.marker.state == Marker::State::kCommitted) committed.push_back(&bank);
        else if (bank.marker.state == Marker::State::kStaging) staging.push_back(&bank);
      }
      if (committed.size() == 2) {
        if (committed[0]->marker.generation == committed[1]->marker.generation)
          return StoreStatus::kConflictingAuthority;
        Bank* high = committed[0]->marker.generation > committed[1]->marker.generation
            ? committed[0] : committed[1];
        Bank* low = high == committed[0] ? committed[1] : committed[0];
        if (high->marker.generation != low->marker.generation + 1 ||
            high->marker.previous_generation != low->marker.generation ||
            !equal_bytes(high->marker.previous_commit_digest.data(),
                         low->marker.commit_digest.data(), 32) ||
            !same_history_prefix(low->record, high->record))
          return StoreStatus::kConflictingAuthority;
        authority = high;
      } else if (committed.size() == 1) {
        authority = committed[0];
        if (staging.size() == 1) {
          if (staging[0]->marker.generation != authority->marker.generation + 1 ||
              staging[0]->marker.previous_generation != authority->marker.generation ||
              !equal_bytes(staging[0]->marker.previous_commit_digest.data(),
                           authority->marker.commit_digest.data(), 32))
            return StoreStatus::kConflictingAuthority;
        } else if (authority->marker.generation != 0) {
          return StoreStatus::kConflictingAuthority;
        }
      } else if (staging.size() > 1 ||
                 (staging.size() == 1 &&
                  (staging[0]->marker.generation != 0 ||
                   staging[0]->marker.previous_generation != kGenerationNone ||
                   !all_zero(staging[0]->marker.previous_commit_digest.data(), 32)))) {
        return StoreStatus::kConflictingAuthority;
      }
      if (staging.size() == 1)
        staged_bank[slot] = static_cast<std::int8_t>(staging[0]->index);
      if (authority != nullptr) {
        const auto inserted = next.emplace(authority->record.operation_id,
                                           authority->record).second;
        if (!inserted) return StoreStatus::kDuplicateOperation;
        occupied[slot] = true;
        if (terminal(authority->record.events.back().state)) ++terminal_count;
        else ++active;
      }
      if (io.stop_requested())
        return io.cancellation_requested() ? StoreStatus::kCancelled
                                           : StoreStatus::kIoTimeout;
    }
    if (active > kMaxActiveRecords || terminal_count > kMaxTerminalRecords)
      return StoreStatus::kRecordLimitExceeded;
    // Final cooperative boundary for the entire 1024 x 2 decode, hash, and
    // authority scan. No recovered snapshot becomes observable before this.
    if (io.stop_requested())
      return io.cancellation_requested() ? StoreStatus::kCancelled
                                         : StoreStatus::kIoTimeout;
    records_ = std::move(next);
    occupied_ = occupied;
    staged_bank_ = staged_bank;
    active_count_ = active;
    terminal_count_ = terminal_count;
    return StoreStatus::kOk;
  } catch (...) {
    return StoreStatus::kInternal;
  }
}

StoreStatus FixedContainerStore::load_and_recover(
    StorageIoControl io, std::uint32_t& recovery_count) {
  recovery_count = 0;
  // This is the explicit pre-availability recovery probe. A missing startup
  // deadline is itself a refusal; no named pipe may be created afterwards.
  if (io.stop_requested())
    return io.cancellation_requested() ? StoreStatus::kCancelled
                                       : StoreStatus::kIoTimeout;
  StoreStatus status = reload(io);
  if (status != StoreStatus::kOk) return status;
  std::vector<std::string> recover;
  for (const auto& [operation, record] : records_) {
    if (io.stop_requested())
      return io.cancellation_requested() ? StoreStatus::kCancelled
                                         : StoreStatus::kIoTimeout;
    const auto& state = record.events.back().state;
    if (state == "prepared" || state == "authorized" || state == "dispatching" ||
        state == "acknowledged" || state == "reconciling") recover.push_back(operation);
  }
  if (io.stop_requested())
    return io.cancellation_requested() ? StoreStatus::kCancelled
                                       : StoreStatus::kIoTimeout;
  for (const auto& operation : recover) {
    if (io.stop_requested())
      return io.cancellation_requested() ? StoreStatus::kCancelled
                                         : StoreStatus::kIoTimeout;
    const JournalEvent previous = records_.at(operation).events.back();
    const bool pre_dispatch = previous.state == "prepared" || previous.state == "authorized";
    const auto event = make_event(operation, &previous, "startup_recovery",
        nlohmann::json::object(), pre_dispatch ? "cancelled" : "unknown_manual",
        previous.authorization_kind,
        pre_dispatch ? "startup_recovery" : "dispatch_ambiguous");
    bool committed = false;
    status = append(operation, event, io, committed);
    if (status != StoreStatus::kOk || !committed) return status;
    if (io.stop_requested())
      return io.cancellation_requested() ? StoreStatus::kCancelled
                                         : StoreStatus::kIoTimeout;
    ++recovery_count;
  }
  if (io.stop_requested())
    return io.cancellation_requested() ? StoreStatus::kCancelled
                                       : StoreStatus::kIoTimeout;
  return StoreStatus::kOk;
}

StoreStatus FixedContainerStore::append(const std::string& operation,
                                        const JournalEvent& event,
                                        StorageIoControl io,
                                        bool& committed) {
  committed = false;
  StoreStatus status = reload(io);
  if (status != StoreStatus::kOk) return status;
  const auto current_it = records_.find(operation);
  const JournalRecord* current = current_it == records_.end() ? nullptr : &current_it->second;
  std::vector<JournalEvent> events = current == nullptr
      ? std::vector<JournalEvent>{event} : current->events;
  if (current != nullptr) events.push_back(event);
  if (!validate_history(events)) return StoreStatus::kInvalidTransition;
  if (events.size() > kEventCellsPerBank) return StoreStatus::kEventLimitExceeded;
  std::uint32_t next_active = active_count_, next_terminal = terminal_count_;
  if (current == nullptr) {
    if (terminal(event.state)) ++next_terminal; else ++next_active;
  } else if (!terminal(current->events.back().state) && terminal(event.state)) {
    --next_active; ++next_terminal;
  }
  if (next_active > kMaxActiveRecords || next_terminal > kMaxTerminalRecords)
    return StoreStatus::kRecordLimitExceeded;
  std::uint32_t slot = kSlotCount;
  if (current != nullptr) slot = current->slot_index;
  else for (std::uint32_t index = 0; index < kSlotCount; ++index)
    if (!occupied_[index]) { slot = index; break; }
  if (slot == kSlotCount) return StoreStatus::kRecordLimitExceeded;
  if (current != nullptr && current->generation == UINT64_MAX)
    return StoreStatus::kGenerationExhausted;
  const std::uint64_t generation = current == nullptr ? 0 : current->generation + 1;
  const std::uint32_t bank = current == nullptr
      ? (staged_bank_[slot] >= 0 ? static_cast<std::uint32_t>(staged_bank_[slot]) : 0)
      : 1 - current->bank_index;
  const std::uint64_t previous_generation = current == nullptr
      ? kGenerationNone : current->generation;
  const std::array<std::uint8_t, 32> zero{};
  const auto previous_commit = current == nullptr ? zero : current->commit_digest;
  std::array<std::uint8_t, kBankBodyBytes> body{};
  std::array<std::uint8_t, 32> body_digest{};
  if (!encode_body(slot, bank, generation, operation, events, container_id_, body,
                   body_digest)) return StoreStatus::kHashFailed;
  std::array<std::uint8_t, kBankMarkerBytes> staging{}, committed_marker{};
  if (!encode_marker(Marker::State::kStaging, slot, bank, generation,
                     previous_generation, zero, previous_commit, container_id_, staging) ||
      !encode_marker(Marker::State::kCommitted, slot, bank, generation,
                     previous_generation, body_digest, previous_commit,
                     container_id_, committed_marker)) return StoreStatus::kHashFailed;
  // This probe is the final pre-mutation boundary. The caller binds it to the
  // authenticated pipe/process lifetime and the request's absolute deadline.
  if (io.stop_requested())
    return io.cancellation_requested() ? StoreStatus::kCancelled
                                       : StoreStatus::kIoTimeout;
  std::array<std::uint8_t, kBankMarkerBytes> marker_readback{};
  std::array<std::uint8_t, kBankBodyBytes> body_readback{};
  commit_section_ = true;  // no cancellation or response is observed below.
  const StorageIoControl commit_io = io.commit_control();
  auto bounded = write_exact(file_, marker_offset(slot, bank), staging.data(),
                             staging.size(), commit_io);
  status = write_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  bounded = flush_exact(file_, commit_io);
  status = bounded == BoundedIoStatus::kFailed
      ? StoreStatus::kFlushFailed : read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  bounded = read_exact(file_, marker_offset(slot, bank), marker_readback.data(),
                       marker_readback.size(), commit_io);
  status = read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  if (!equal_bytes(staging.data(), marker_readback.data(), staging.size()))
    { return StoreStatus::kReadbackFailed; }
  bounded = write_exact(file_, bank_offset(slot, bank), body.data(), body.size(),
                        commit_io);
  status = write_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  bounded = flush_exact(file_, commit_io);
  status = bounded == BoundedIoStatus::kFailed
      ? StoreStatus::kFlushFailed : read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  bounded = read_exact(file_, bank_offset(slot, bank), body_readback.data(),
                       body_readback.size(), commit_io);
  status = read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  if (!equal_bytes(body.data(), body_readback.data(), body.size()))
    { return StoreStatus::kReadbackFailed; }
  bounded = write_exact(file_, marker_offset(slot, bank), committed_marker.data(),
                        committed_marker.size(), commit_io);
  status = write_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  bounded = flush_exact(file_, commit_io);
  status = bounded == BoundedIoStatus::kFailed
      ? StoreStatus::kFlushFailed : read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  bounded = read_exact(file_, marker_offset(slot, bank), marker_readback.data(),
                       marker_readback.size(), commit_io);
  status = read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  if (!equal_bytes(committed_marker.data(), marker_readback.data(),
                   committed_marker.size()))
    { return StoreStatus::kReadbackFailed; }
  bounded = read_exact(file_, bank_offset(slot, bank), body_readback.data(),
                       body_readback.size(), commit_io);
  status = read_status(bounded);
  if (status != StoreStatus::kOk) { return status; }
  if (!equal_bytes(body.data(), body_readback.data(), body.size()))
    { return StoreStatus::kReadbackFailed; }
  status = reload(commit_io);
  if (status != StoreStatus::kOk) { return status; }
  const auto verified = records_.find(operation);
  if (verified == records_.end() || verified->second.generation != generation ||
      verified->second.events.back().canonical_json != event.canonical_json)
    { return StoreStatus::kReadbackFailed; }
  committed = true;
  commit_section_ = false;
  return StoreStatus::kOk;
}

StoreStatus FixedContainerStore::summary(const DecodedRequest& request,
                                         StorageIoControl io,
                                         EncodedResult& result) {
  StoreStatus status = reload(io);
  if (status != StoreStatus::kOk) return status;
  const bool include_terminal = request.body["include_terminal"].get<bool>();
  const auto limit = request.body["limit"].get<std::uint32_t>();
  const std::string cursor = request.body["cursor"].is_null()
      ? "" : request.body["cursor"].get<std::string>();
  nlohmann::json rows = nlohmann::json::array();
  bool truncated = false;
  for (const auto& [operation, record] : records_) {
    if ((!cursor.empty() && operation <= cursor) ||
        (!include_terminal && terminal(record.events.back().state))) continue;
    if (rows.size() == limit) { truncated = true; break; }
    rows.push_back(receipt(operation, record.events.back()));
  }
  result = {};
  result.body = {{"next_cursor", truncated ? rows.back()["operation_id"]
                                            : nlohmann::json(nullptr)},
                 {"records", rows}, {"truncated", truncated}};
  return StoreStatus::kOk;
}

StoreStatus FixedContainerStore::detail(const DecodedRequest& request,
                                        StorageIoControl io,
                                        EncodedResult& result) {
  StoreStatus status = reload(io);
  if (status != StoreStatus::kOk) return status;
  const auto found = records_.find(request.operation_id);
  if (found == records_.end()) return StoreStatus::kNotFound;
  const auto& events = found->second.events;
  std::size_t first = 0;
  nlohmann::json predecessor = nullptr;
  if (!request.body["after_sequence"].is_null()) {
    const auto sequence = request.body["after_sequence"].get<std::size_t>();
    if (sequence >= events.size()) return StoreStatus::kInvalidTransition;
    std::array<std::uint8_t, 32> event_digest{};
    if (!protocol_event_digest(events[sequence].canonical_json, event_digest) ||
        hex(event_digest.data(), event_digest.size()) !=
            request.body["after_event_digest"].get<std::string>())
      return StoreStatus::kInvalidTransition;
    predecessor = nlohmann::json::parse(events[sequence].canonical_json);
    first = sequence + 1;
  }
  const auto limit = request.body["limit"].get<std::size_t>();
  nlohmann::json page = nlohmann::json::array();
  for (std::size_t index = first; index < events.size() && page.size() < limit; ++index)
    page.push_back(nlohmann::json::parse(events[index].canonical_json));
  const bool truncated = !page.empty() &&
      page.back()["sequence"].get<std::size_t>() < events.back().sequence;
  result.operation_id = request.operation_id;
  result.state = events.back().state;
  result.body = {{"events", page},
                 {"next_sequence", truncated ? page.back()["sequence"]
                                              : nlohmann::json(nullptr)},
                 {"predecessor", predecessor},
                 {"receipt", receipt(request.operation_id, events.back())},
                 {"truncated", truncated}};
  return StoreStatus::kOk;
}

StoreStatus FixedContainerStore::mutation(const DecodedRequest& request,
                                          StorageIoControl io,
                                          EncodedResult& result) {
  if (io.stop_requested()) {
    result = {};
    result.operation_id = request.operation_id;
    result.error_code = "deadline_expired";
    return StoreStatus::kOk;
  }
  StoreStatus status = reload(io);
  if (status != StoreStatus::kOk) return status;
  std::string operation = request.operation_id;
  const JournalRecord* current = nullptr;
  if (request.method == "prepare") {
    operation = operation_id(container_id_,
        request.body["operation_digest"].get<std::string>());
    if (operation.empty()) return StoreStatus::kHashFailed;
    const auto found = records_.find(operation);
    if (found != records_.end()) {
      if (found->second.events.size() == 1 &&
          found->second.events[0].state == "prepared" &&
          found->second.events[0].receipt_digest ==
              helper_digest("prepare-binding", {request.body.dump()})) {
        result.operation_id = operation;
        result.state = "prepared";
        result.body = {{"receipt", receipt(operation, found->second.events[0])}};
        return StoreStatus::kOk;
      }
      return StoreStatus::kInvalidTransition;
    }
  } else {
    const auto found = records_.find(operation);
    if (found == records_.end()) return StoreStatus::kNotFound;
    current = &found->second;
  }
  JournalEvent event;
  if (request.method == "prepare") {
    event = make_event(operation, nullptr, "prepare", request.body, "prepared", "", "");
  } else {
    std::string state, authorization, resolution;
    if (!response_state(current->events.back(), request, state, authorization,
                        resolution)) return StoreStatus::kInvalidTransition;
    event = make_event(operation, &current->events.back(), request.method,
                       request.body, state, authorization, resolution);
  }
  bool committed = false;
  if (io.stop_requested()) {
    result = {};
    result.operation_id = request.operation_id;
    result.error_code = "deadline_expired";
    return StoreStatus::kOk;
  }
  status = append(operation, event, io, committed);
  if (status == StoreStatus::kCancelled) {
    result = {};
    result.operation_id = request.operation_id;
    result.error_code = "deadline_expired";
    return StoreStatus::kOk;
  }
  if (status != StoreStatus::kOk) return status;
  result.operation_id = operation;
  result.state = event.state;
  result.body = {{"receipt", receipt(operation, event)}};
  if (io.cancellation.cancelled() && committed)
    return StoreStatus::kCommitNonCancellable;
  return StoreStatus::kOk;
}

StoreStatus FixedContainerStore::apply(const DecodedRequest& request,
                                       StorageIoControl io,
                                       EncodedResult& result) noexcept {
  try {
    if (request.method == "health") {
      result = {};
      result.body = {{"platform_available", false}, {"production_enabled", false},
                     {"status", "unavailable"}};
      return StoreStatus::kOk;
    }
    if (request.method == "summary") return summary(request, io, result);
    if (request.method == "detail") return detail(request, io, result);
    return mutation(request, io, result);
  } catch (const std::bad_alloc&) {
    return StoreStatus::kInternal;
  } catch (...) {
    return StoreStatus::kInternal;
  }
}

const char* store_status_name(StoreStatus status) noexcept {
  switch (status) {
    case StoreStatus::kOk: return "ok";
    case StoreStatus::kNotFound: return "not_found";
    case StoreStatus::kInvalidTransition: return "invalid_transition";
    case StoreStatus::kRecordLimitExceeded: return "record_limit_exceeded";
    case StoreStatus::kEventLimitExceeded: return "record_limit_exceeded";
    case StoreStatus::kGenerationExhausted: return "record_limit_exceeded";
    case StoreStatus::kCorruptHeader: return "internal";
    case StoreStatus::kCorruptBank: return "internal";
    case StoreStatus::kConflictingAuthority: return "internal";
    case StoreStatus::kDuplicateOperation: return "internal";
    case StoreStatus::kReadFailed: return "internal";
    case StoreStatus::kWriteFailed: return "internal";
    case StoreStatus::kFlushFailed: return "internal";
    case StoreStatus::kReadbackFailed: return "internal";
    case StoreStatus::kHashFailed: return "internal";
    case StoreStatus::kCancelled: return "deadline_expired";
    case StoreStatus::kIoTimeout: return "internal";
    case StoreStatus::kIoCancelFailed: return "internal";
    case StoreStatus::kCommitNonCancellable: return "commit_non_cancellable";
    case StoreStatus::kInternal: return "internal";
  }
  return "internal";
}

JournalAuthorityOwner::JournalAuthorityOwner(
    action_journal_storage::JournalStorageLease&& lease,
    const std::array<std::uint8_t, 32>& container_id) noexcept
    : lease_(std::move(lease)), store_(lease_, container_id) {}

JournalAuthorityOwner::~JournalAuthorityOwner() noexcept {
  // Normal helper scope destroys ProtocolSession/client/pipe locals before
  // this owner.  Close admission first anyway, so direct test-only owners and
  // exceptional paths cannot race a store call with lease destruction.
  if (!begin_shutdown()) std::terminate();
}

AuthorityStatus JournalAuthorityOwner::map_open_status(
    action_journal_storage::StorageStatus status) noexcept {
  switch (status) {
    case action_journal_storage::StorageStatus::kOkOpened:
      return AuthorityStatus::kReady;
    case action_journal_storage::StorageStatus::kContainerCorruptHeader:
    case action_journal_storage::StorageStatus::kContainerUnformatted:
    case action_journal_storage::StorageStatus::kIdentityMismatch:
    case action_journal_storage::StorageStatus::kReopenIdentityMismatch:
      return AuthorityStatus::kStorageCorrupt;
    case action_journal_storage::StorageStatus::kIoFailed:
    case action_journal_storage::StorageStatus::kGenesisIncomplete:
      return AuthorityStatus::kRecoveryFailed;
    default:
      return AuthorityStatus::kStorageUnavailable;
  }
}

AuthorityStatus JournalAuthorityOwner::map_recovery_status(
    StoreStatus status) noexcept {
  switch (status) {
    case StoreStatus::kIoTimeout:
      return AuthorityStatus::kIoTimeout;
    case StoreStatus::kIoCancelFailed:
      return AuthorityStatus::kIoCancelFailed;
    case StoreStatus::kCorruptHeader:
    case StoreStatus::kCorruptBank:
    case StoreStatus::kConflictingAuthority:
    case StoreStatus::kDuplicateOperation:
      return AuthorityStatus::kStorageCorrupt;
    default:
      return AuthorityStatus::kRecoveryFailed;
  }
}

void JournalAuthorityOwner::poison_for(StoreStatus status) noexcept {
  switch (status) {
    case StoreStatus::kReadFailed:
    case StoreStatus::kWriteFailed:
    case StoreStatus::kFlushFailed:
    case StoreStatus::kReadbackFailed:
    case StoreStatus::kHashFailed:
    case StoreStatus::kIoTimeout:
    case StoreStatus::kIoCancelFailed:
    case StoreStatus::kCorruptHeader:
    case StoreStatus::kCorruptBank:
    case StoreStatus::kConflictingAuthority:
    case StoreStatus::kDuplicateOperation:
    case StoreStatus::kInternal:
      poisoned_.store(true, std::memory_order_release);
      return;
    default:
      return;
  }
}

std::unique_ptr<JournalAuthorityOwner> JournalAuthorityOwner::open(
    const action_journal_storage::StorageRequest& request,
    const std::array<std::uint8_t, 32>& container_id,
    StorageIoControl io,
    AuthorityStatus& status,
    action_journal_storage::StorageReceipt& receipt) noexcept {
  status = AuthorityStatus::kInternal;
  receipt = {};
  if (io.stop_requested()) {
    status = io.cancellation_requested() ? AuthorityStatus::kRecoveryFailed
                                         : AuthorityStatus::kIoTimeout;
    return nullptr;
  }
  action_journal_storage::JournalStorageLease lease;
  const auto storage_status = action_journal_storage::acquire_storage(
      request, lease, receipt);
  if (storage_status != action_journal_storage::StorageStatus::kOkOpened) {
    status = map_open_status(storage_status);
    return nullptr;
  }
  try {
    std::unique_ptr<JournalAuthorityOwner> owner(
        new JournalAuthorityOwner(std::move(lease), container_id));
    {
      // Recovery is a store call too: serialize it under the same owner lock
      // used after publication, while keeping all pipe/session I/O outside.
      ActiveBorrow borrow(*owner);
      if (!borrow.acquired()) {
        status = AuthorityStatus::kInternal;
        return nullptr;
      }
      const std::lock_guard<std::mutex> lock(owner->mutex_);
      const auto recovery = owner->store_.load_and_recover(
          io, owner->recovery_count_);
      if (recovery != StoreStatus::kOk) {
        owner->poison_for(recovery);
        status = map_recovery_status(recovery);
        return nullptr;
      }
      if (io.stop_requested()) {
        status = io.cancellation_requested() ? AuthorityStatus::kRecoveryFailed
                                             : AuthorityStatus::kIoTimeout;
        return nullptr;
      }
      owner->recovered_ = true;
    }
    status = AuthorityStatus::kReady;
    return owner;
  } catch (const std::bad_alloc&) {
    status = AuthorityStatus::kInternal;
    return nullptr;
  } catch (...) {
    status = AuthorityStatus::kInternal;
    return nullptr;
  }
}

StoreStatus JournalAuthorityOwner::apply(const DecodedRequest& request,
                                         StorageIoControl io,
                                         EncodedResult& result) noexcept {
  if (admission_closing() || poisoned_.load(std::memory_order_acquire))
    return StoreStatus::kInternal;
  // The CAS admission increment is the linearization point and occurs before
  // any owner-mutex wait. Shutdown uses the same word and therefore cannot
  // race a late increment after it has begun waiting for zero.
  ActiveBorrow borrow(*this);
  if (!borrow.acquired()) return StoreStatus::kInternal;
  std::unique_lock<std::mutex> lock;
  try {
    if (admission_closing() || poisoned_.load(std::memory_order_acquire))
      return StoreStatus::kInternal;
    lock = std::unique_lock<std::mutex>(mutex_);
    if (admission_closing() ||
        shutting_down_ || !recovered_ ||
        poisoned_.load(std::memory_order_acquire))
      return StoreStatus::kInternal;
    try {
      const auto status = store_.apply(request, io, result);
      poison_for(status);
      if (status == StoreStatus::kOk && request.method == "health" &&
          result.body.is_object()) {
        result.body["recovery_count"] = recovery_count_;
      }
      // Shutdown may publish its admission bit while store I/O is in flight;
      // never return a successful result after that linearization point.
      if (admission_closing())
        return StoreStatus::kInternal;
      return status;
    } catch (const std::bad_alloc&) {
      poisoned_.store(true, std::memory_order_release);
      return StoreStatus::kInternal;
    } catch (...) {
      // Keep the lock alive while latching poison.  The owner cannot be
      // observed as healthy between the exception and this state transition.
      poisoned_.store(true, std::memory_order_release);
      return StoreStatus::kInternal;
    }
  } catch (...) {
    // A lock acquisition failure cannot safely claim ownership of the store;
    // atomically poison admission and return a finite fail-closed status
    // rather than terminating noexcept.
    poisoned_.store(true, std::memory_order_release);
    return StoreStatus::kInternal;
  }
}

bool JournalAuthorityOwner::try_acquire_borrow() noexcept {
  std::uint64_t observed = admission_.load(std::memory_order_acquire);
  for (;;) {
    if ((observed & kAdmissionClosing) != 0) return false;
    const auto count = observed & kAdmissionCountMask;
    if ((observed & ~(kAdmissionClosing | kAdmissionCountMask)) != 0 ||
        count >= kMaxActiveBorrows)
      return false;
    const auto desired = observed + UINT64_C(1);
    if (admission_.compare_exchange_weak(
            observed, desired, std::memory_order_acq_rel,
            std::memory_order_acquire))
      return true;
  }
}

bool JournalAuthorityOwner::release_borrow() noexcept {
  std::uint64_t observed = admission_.load(std::memory_order_acquire);
  for (;;) {
    const auto count = observed & kAdmissionCountMask;
    if (count == 0 ||
        (observed & ~(kAdmissionClosing | kAdmissionCountMask)) != 0)
      return false;
    const auto desired = (observed & ~kAdmissionCountMask) | (count - 1);
    if (admission_.compare_exchange_weak(
            observed, desired, std::memory_order_acq_rel,
            std::memory_order_acquire)) {
      if (count == 1)
        WakeByAddressAll(reinterpret_cast<PVOID>(&admission_));
      return true;
    }
  }
}

bool JournalAuthorityOwner::admission_closing() const noexcept {
  return (admission_.load(std::memory_order_acquire) & kAdmissionClosing) != 0;
}

bool JournalAuthorityOwner::wait_for_borrowers() noexcept {
  for (;;) {
    const auto observed = admission_.load(std::memory_order_acquire);
    if ((observed & kAdmissionCountMask) == 0) return true;
    if ((observed & ~(kAdmissionClosing | kAdmissionCountMask)) != 0)
      return false;
    std::uint64_t expected = observed;
    if (WaitOnAddress(reinterpret_cast<volatile VOID*>(&admission_), &expected,
                      sizeof(expected), 1000))
      continue;
    const DWORD error = GetLastError();
    if (error != ERROR_TIMEOUT) return false;
  }
}

bool JournalAuthorityOwner::begin_shutdown() noexcept {
  std::uint64_t observed = admission_.load(std::memory_order_acquire);
  for (;;) {
    if ((observed & ~(kAdmissionClosing | kAdmissionCountMask)) != 0)
      return false;
    if ((observed & kAdmissionClosing) != 0) break;
    const auto desired = observed | kAdmissionClosing;
    if (admission_.compare_exchange_weak(
            observed, desired, std::memory_order_acq_rel,
            std::memory_order_acquire))
      break;
  }
  if (!wait_for_borrowers()) return false;
  try {
    const std::lock_guard<std::mutex> lock(mutex_);
    shutting_down_ = true;
    return true;
  } catch (...) {
    // Admission is already closed atomically.  The caller must treat false as
    // an unproven shutdown and retain the owner for fail-stop handling.
    return false;
  }
}

bool JournalAuthorityOwner::ready() const {
  if (admission_closing() ||
      poisoned_.load(std::memory_order_acquire))
    return false;
  try {
    const std::lock_guard<std::mutex> lock(mutex_);
    return !admission_closing() &&
        !shutting_down_ && recovered_ &&
        !poisoned_.load(std::memory_order_acquire);
  } catch (...) {
    return false;
  }
}

bool JournalAuthorityOwner::poisoned() const {
  return admission_closing() ||
      poisoned_.load(std::memory_order_acquire);
}

std::uint32_t JournalAuthorityOwner::recovery_count() const {
  if (admission_closing() ||
      poisoned_.load(std::memory_order_acquire))
    return UINT32_MAX;
  try {
    const std::lock_guard<std::mutex> lock(mutex_);
    if (admission_closing() ||
        shutting_down_ || poisoned_.load(std::memory_order_acquire))
      return UINT32_MAX;
    return recovery_count_;
  } catch (...) {
    return UINT32_MAX;
  }
}

const char* authority_status_name(AuthorityStatus status) noexcept {
  switch (status) {
    case AuthorityStatus::kReady: return "ready";
    case AuthorityStatus::kStorageUnavailable: return "storage_unavailable";
    case AuthorityStatus::kStorageCorrupt: return "storage_corrupt";
    case AuthorityStatus::kRecoveryFailed: return "recovery_failed";
    case AuthorityStatus::kIoTimeout: return "io_timeout";
    case AuthorityStatus::kIoCancelFailed: return "io_cancel_failed";
    case AuthorityStatus::kNotReady: return "not_ready";
    case AuthorityStatus::kPoisoned: return "poisoned";
    case AuthorityStatus::kInternal: return "internal";
  }
  return "internal";
}

}  // namespace lae::action_journal_helper
