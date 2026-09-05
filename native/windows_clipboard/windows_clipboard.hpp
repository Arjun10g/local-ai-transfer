#pragma once

// Inert, source-only Win32 clipboard boundary. This header is deliberately
// absent from CMake, the host, package manifests, and production registries.
#ifndef _WIN32
#error "The Windows clipboard boundary is Windows-only"
#endif

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace lae::windows_clipboard {

inline constexpr std::size_t kMaximumUtf8Bytes = 64 * 1024;
inline constexpr std::uint64_t kMaximumDeadlineMs = 5'000;

enum class Operation : std::uint8_t {
  kRead,
  kWrite,
};

enum class Status : std::uint8_t {
  kOk,
  kPlatformUnavailable,
  kInvalidRequest,
  kCapabilityRefused,
  kSessionRefused,
  kCancelled,
  kDeadlineExceeded,
  kClipboardBusy,
  kFormatUnavailable,
  kInvalidText,
  kOutputLimit,
  kJournalUnavailable,
  kAlreadyDispatched,
  kMutationUnknown,
  kPostconditionPresentManual,
  kPostconditionAbsentManual,
  kInternalFailure,
};

enum class JournalDispatch : std::uint8_t {
  kDispatched,
  kAlreadyDispatched,
  kRefused,
  kUnavailable,
};

enum class JournalOutcome : std::uint8_t {
  kApplied,
  kFailedBeforeMutation,
  kMutationAttemptFailed,
  kFailedAfterMutation,
  kUnknownAfterMutation,
};

enum class JournalLookupState : std::uint8_t {
  kNotDispatched,
  kDispatched,
  kMutationPrepared,
  kApplied,
  kFailedBeforeMutation,
  kMutationAttemptFailed,
  kUnknownAfterMutation,
};

// A boolean cannot honestly represent restart state. A durable dispatch alone
// cannot prove whether the previous process crossed the Win32 mutation call
// boundary. kAttempted is used only by the live caller after EmptyClipboard
// returned or by a durable terminal state that necessarily follows that call.
enum class MutationAttemptState : std::uint8_t {
  kNotAttempted,
  kMayHaveBeenAttempted,
  kAttempted,
};

struct JournalLookup final {
  JournalLookupState state = JournalLookupState::kUnknownAfterMutation;
  std::uint32_t sequence_before = 0;
  std::uint32_t sequence_after = 0;
  bool mutation_prepared_durable = false;
};

// A production implementation must durably persist dispatch before returning
// kDispatched, the mutation-prepared marker before returning true, and every
// terminal outcome before returning true. kRefused, kUnavailable, and an
// invalid value are transport observations, not durable proof that dispatch
// did not occur; a lost acknowledgement is therefore reconciled through an
// exact lookup. FailedAfterMutation is reconstructed as
// kUnknownAfterMutation. This source supplies no implementation and its
// journal availability gate is false.
class JournalPort {
 public:
  virtual ~JournalPort() = default;
  virtual JournalDispatch dispatch_write(
      const std::string& operation_id,
      const std::array<std::uint8_t, 32>& request_digest,
      const std::array<std::uint8_t, 32>& content_digest,
      std::uint32_t sequence_before) noexcept = 0;
  // Must durably record the last point before the first EmptyClipboard call.
  // Failure prevents the call. A recovered marker is conservative evidence
  // that mutation may have been attempted, not proof that the call occurred.
  virtual bool record_mutation_prepared(
      const std::string& operation_id,
      std::uint32_t sequence_before) noexcept = 0;
  virtual bool record_write_outcome(
      const std::string& operation_id,
      JournalOutcome outcome,
      std::uint32_t sequence_before,
      std::uint32_t sequence_after) noexcept = 0;
  virtual bool lookup_write(
      const std::string& operation_id,
      const std::array<std::uint8_t, 32>& request_digest,
      JournalLookup& output) noexcept = 0;
};

// Only a future reviewed broker authority may construct this type. There is no
// issuer, deserializer, test constructor, setter, or runtime override in this
// slice. The MAC binds the exact scope/session/issuance/expiry,
// operation, request/content/confirmation digests, sequence preview, and
// authority nonce. The MAC bytes are not recursively part of their own frame.
class BrokerClipboardCapability final {
 public:
  BrokerClipboardCapability(const BrokerClipboardCapability&) = delete;
  BrokerClipboardCapability& operator=(const BrokerClipboardCapability&) = delete;
  BrokerClipboardCapability(BrokerClipboardCapability&&) = delete;
  BrokerClipboardCapability& operator=(BrokerClipboardCapability&&) = delete;

  bool structurally_valid() const noexcept;
  Operation operation() const noexcept;
  const std::string& session_id() const noexcept;
  std::uint32_t interactive_session_id() const noexcept;
  std::uint64_t issued_monotonic_ms() const noexcept;
  std::uint64_t expires_monotonic_ms() const noexcept;
  const std::string& operation_id() const noexcept;
  const std::array<std::uint8_t, 32>& request_digest() const noexcept;
  const std::array<std::uint8_t, 32>& content_digest() const noexcept;
  const std::array<std::uint8_t, 32>& confirmation_digest() const noexcept;
  std::uint32_t confirmed_sequence_number() const noexcept;
  bool replace_all_formats_confirmed() const noexcept;

 private:
  friend class BrokerClipboardAuthority;

  BrokerClipboardCapability() = default;

  Operation operation_ = Operation::kRead;
  std::string session_id_;
  std::uint32_t interactive_session_id_ = 0;
  std::uint64_t issued_monotonic_ms_ = 0;
  std::uint64_t expires_monotonic_ms_ = 0;
  std::string operation_id_;
  std::array<std::uint8_t, 32> request_digest_{};
  std::array<std::uint8_t, 32> content_digest_{};
  std::array<std::uint8_t, 32> confirmation_digest_{};
  std::array<std::uint8_t, 32> authority_nonce_{};
  std::array<std::uint8_t, 32> mac_{};
  std::uint32_t confirmed_sequence_number_ = 0;
  bool replace_all_formats_confirmed_ = false;
};

// Nonconstructible in this inert slice. A future reviewed supervisor issuer
// must own this object and its key; callers receive only a pointer suitable for
// verification. The key and authority nonce are never exposed by an accessor.
class BrokerClipboardAuthority final {
 public:
  BrokerClipboardAuthority(const BrokerClipboardAuthority&) = delete;
  BrokerClipboardAuthority& operator=(const BrokerClipboardAuthority&) = delete;
  BrokerClipboardAuthority(BrokerClipboardAuthority&&) = delete;
  BrokerClipboardAuthority& operator=(BrokerClipboardAuthority&&) = delete;
  ~BrokerClipboardAuthority();

  bool verify_capability(
      const BrokerClipboardCapability& capability) const noexcept;

 private:
  BrokerClipboardAuthority() = default;

  std::array<std::uint8_t, 32> mac_key_{};
  std::array<std::uint8_t, 32> authority_nonce_{};
};

struct ExecutionContext final {
  // Must be a supervisor-owned manual-reset event. Production authentication
  // of this handle is one of the immutable-false activation gates.
  HANDLE cancellation_event = nullptr;
  // Must point to the same supervisor-owned authority that issued capability.
  // The authority is nonconstructible and unprovisioned in this source slice.
  const BrokerClipboardAuthority* authority = nullptr;
};

struct Request final {
  Operation operation = Operation::kRead;
  const BrokerClipboardCapability* capability = nullptr;
  const ExecutionContext* context = nullptr;
  // Used only by kWrite. It is returned nowhere and never copied to receipts.
  const std::string* utf8_text = nullptr;
};

struct Receipt final {
  const char* operation = "unknown";
  const char* status = "internal_failure";
  std::size_t utf8_bytes = 0;
  std::uint32_t sequence_before = 0;
  std::uint32_t sequence_after = 0;
  bool sensitive = true;
  bool content_logged = false;
  // Conservative by construction. A write operation ID can name an earlier
  // invocation whose durable dispatch acknowledgement was lost. Only an exact
  // durable failed-before-mutation readback may downgrade this value.
  MutationAttemptState mutation_attempt_state =
      MutationAttemptState::kMayHaveBeenAttempted;
  bool journal_dispatch_durable = false;
  bool journal_mutation_prepared_durable = false;
  bool journal_outcome_durable = false;
};

struct Result final {
  Status status = Status::kInternalFailure;
  // Populated only for a successful read and never included in Receipt.
  std::string text;
  Receipt receipt;
};

struct ReconciliationResult final {
  Status status = Status::kInternalFailure;
  JournalLookupState journal_state = JournalLookupState::kUnknownAfterMutation;
  std::uint32_t journal_sequence_before = 0;
  std::uint32_t journal_sequence_after = 0;
  std::uint32_t observed_sequence = 0;
  bool exact_content_present = false;
  Receipt receipt;
};

Result execute(const Request& request, JournalPort* journal) noexcept;

// Read-only lost-ACK/status query. It never repeats a write. A dispatched-only
// journal entry can at most report a present/absent postcondition requiring
// manual resolution; only a durable kApplied lookup is typed complete.
ReconciliationResult query_write_status(
    const BrokerClipboardCapability* capability,
    const ExecutionContext* context,
    JournalPort* journal) noexcept;

}  // namespace lae::windows_clipboard
