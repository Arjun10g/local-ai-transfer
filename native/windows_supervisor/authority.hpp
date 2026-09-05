#pragma once

// Deliberately unlinked source boundary. This header is not included by the
// host or CMake until the full Windows identity and containment review passes.
#if !defined(_WIN32)
#error "the supervisor authority is Windows-only and unavailable here"
#endif

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <cstdint>

namespace lae::windows_supervisor {

// These are compile-time false by construction. A manifest hash, linker flag,
// environment value, or caller argument cannot turn this source slice on.
inline constexpr bool kReleaseManifestPinned = false;
inline constexpr bool kSelfAuthenticodePinned = false;
inline constexpr bool kPackageIdentityPinned = false;
inline constexpr bool kCancellableIoProven = false;
inline constexpr bool kDurableJournalAuthority = false;
inline constexpr bool kNestedJobPolicyProven = false;
inline constexpr bool kBrokerIssuedIdentityProven = false;
// A process pathname can be replaced after inspection.  Until the broker can
// retain and revalidate an executing image section handle, bootstrap is
// unavailable; a pathname re-open is never treated as equivalent proof.
inline constexpr bool kRetainedExecutingSectionIdentityProven = false;
inline constexpr bool kProductionAvailable =
    kReleaseManifestPinned && kSelfAuthenticodePinned &&
    kPackageIdentityPinned && kCancellableIoProven &&
    kDurableJournalAuthority &&
    kBrokerIssuedIdentityProven && kRetainedExecutingSectionIdentityProven;

enum class Status : std::uint8_t {
  kUnavailable,
  kInvalidBootstrap,
  kIdentityMismatch,
  kDeadline,
  kCancelled,
  kUnknown,
  kOk,
};

struct RedactedReceipt {
  Status status = Status::kUnavailable;
  std::uint32_t resource_count = 0;
  bool journal_bound = false;
  bool session_bound = false;
};

// No public constructor or factory exists for the issuer or capability. Only
// the future broker-owned implementation can create these private objects.
class Authority final {
 public:
  static Status start() noexcept;
  static Status shutdown() noexcept;
  static RedactedReceipt loopback_metadata() noexcept;

 private:
  Authority() = delete;
};

}  // namespace lae::windows_supervisor
