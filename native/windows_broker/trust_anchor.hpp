#pragma once

#include <array>
#include <cstddef>
#include <string_view>

// A release build must supply the lowercase SHA-256 of the exact reviewed
// windows-broker.manifest.json. The source default intentionally cannot activate.
#ifndef LAE_WINDOWS_BROKER_MANIFEST_SHA256_HEX
#define LAE_WINDOWS_BROKER_MANIFEST_SHA256_HEX ""
#endif

namespace lae::windows_broker {

inline constexpr std::string_view kCompiledManifestSha256 =
    LAE_WINDOWS_BROKER_MANIFEST_SHA256_HEX;

constexpr bool is_lower_hex(char value) {
  return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f');
}

constexpr bool release_trust_anchor_configured() {
  if (kCompiledManifestSha256.size() != 64) return false;
  bool nonzero = false;
  for (const char value : kCompiledManifestSha256) {
    if (!is_lower_hex(value)) return false;
    nonzero = nonzero || value != '0';
  }
  return nonzero;
}

}  // namespace lae::windows_broker
