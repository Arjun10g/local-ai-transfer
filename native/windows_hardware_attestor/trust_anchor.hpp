#pragma once

#include <array>
#include <cstdint>

namespace lae::windows_hardware_attestor::trust_anchor {

// Empty by design. There is no runtime override and no caller-supplied
// expected identity. A release may replace these only through a separately
// reviewed generated source file bound into the signed executable.
inline constexpr bool kConfigured = false;
inline constexpr bool kOfflineAuthenticodePolicyReviewed = false;
inline constexpr bool kSupervisedGlobalDeadlineAvailable = false;
inline constexpr std::uint64_t kExpectedExecutableSize = 0;
inline constexpr std::array<std::uint8_t, 32> kExpectedExecutableSha256{};
inline constexpr std::array<std::uint8_t, 32> kExpectedManifestSha256{};
inline constexpr std::array<std::uint8_t, 32> kExpectedDiagnosticScriptSha256{};
inline constexpr std::array<std::uint8_t, 32> kExpectedSignerCertificateSha256{};

}  // namespace lae::windows_hardware_attestor::trust_anchor
