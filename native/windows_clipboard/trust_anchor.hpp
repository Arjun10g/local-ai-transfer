#pragma once

namespace lae::windows_clipboard::trust_anchor {

// These are immutable source constants with no environment, argv, file,
// registry, or test override. Enabling any one without all reviewed production
// implementations would still leave execute() fail closed.
inline constexpr bool kSupervisorIdentityAvailable = false;
inline constexpr bool kAuthenticatedCapabilityIssuerAvailable = false;
inline constexpr bool kCancellableClipboardIoAvailable = false;
inline constexpr bool kDurableJournalAvailable = false;
inline constexpr bool kTargetAcceptancePassed = false;

inline constexpr bool activation_prerequisites_available() noexcept {
  return kSupervisorIdentityAvailable &&
      kAuthenticatedCapabilityIssuerAvailable &&
      kCancellableClipboardIoAvailable &&
      kDurableJournalAvailable &&
      kTargetAcceptancePassed;
}

}  // namespace lae::windows_clipboard::trust_anchor
