#pragma once

// Inert Windows-only source. Intentionally absent from CMake, package
// manifests, host imports, and production capability registries.
#ifndef _WIN32
#error "The Windows hardware attestor source is Windows-only"
#endif

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <string>

namespace lae::windows_hardware_attestor {

inline constexpr std::uint32_t kAttestorAbiVersion = 1;
inline constexpr std::uint64_t kGlobalDeadlineMs = 30'000;
inline constexpr std::size_t kMaximumReceiptBytes = 1'048'576;
inline constexpr std::size_t kMaximumExecutableBytes = 67'108'864;
inline constexpr std::size_t kMaximumManifestBytes = 65'536;
inline constexpr std::size_t kMaximumRuntimeDllBytes = 16'777'216;
inline constexpr std::size_t kMaximumFirmwareBytes = 1'048'576;
inline constexpr std::size_t kMaximumTopologyBytes = 1'048'576;
inline constexpr std::uint32_t kMaximumAdapters = 8;
inline constexpr std::uint32_t kMaximumDisplayDevices = 16;
inline constexpr std::uint32_t kMaximumMemoryDevices = 16;

enum class AttestorStatus : std::uint8_t {
  kNotReady,
  kOk,
  kInvalidRequest,
  kTrustAnchorUnavailable,
  kSupervisionUnavailable,
  kSelfIdentityMismatch,
  kManifestMismatch,
  kSignatureUnproven,
  kDeadlineExceeded,
  kCancelled,
  kFirmwareMalformed,
  kBoundsExceeded,
  kAdapterAmbiguous,
  kDriverAmbiguous,
  kOutputFailed,
  kInternal,
};

struct CollectionContext {
  std::uint64_t started_tick_ms = 0;
  std::uint64_t deadline_tick_ms = 0;
  const std::atomic<bool>* cancelled = nullptr;
};

struct AttestationResult {
  AttestorStatus status = AttestorStatus::kNotReady;
  std::string canonical_json;
  bool hardware_match = false;
  bool target_evidence_accepted = false;
};

// With the checked-in empty trust anchor this emits a canonical NOT_READY
// receipt without hardware access. Enabling real collection requires a new,
// independently reviewed trust anchor and supervised global-deadline boundary.
AttestorStatus collect_attestation(const CollectionContext& context,
                                   AttestationResult& result) noexcept;

// Writes exactly canonical_json plus LF to STD_OUTPUT_HANDLE with an explicit
// byte cap. No filename or alternate output handle is accepted.
AttestorStatus write_receipt_stdout(const AttestationResult& result) noexcept;

const char* status_name(AttestorStatus status) noexcept;

}  // namespace lae::windows_hardware_attestor
