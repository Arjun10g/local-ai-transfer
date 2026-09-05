#pragma once

// Inert, source-only Windows release-tree identity verifier. This header and
// its implementation are intentionally absent from CMake, host imports,
// package manifests, launchers, and registries.
#ifndef _WIN32
#error "The Windows release-tree verifier is Windows-only"
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
#include <string>

namespace lae::windows_release_verifier {

class VerifierCore;

inline constexpr std::uint32_t kAbiVersion = 1;
inline constexpr std::uint32_t kMaximumManifestEntries = 64;
inline constexpr std::uint32_t kMaximumPathComponents = 16;
inline constexpr std::uint64_t kMaximumFileBytes = 268'435'456;
inline constexpr std::uint64_t kMaximumTreeBytes = 1'073'741'824;

enum class Status : std::uint8_t {
  kVerified = 0,
  kNotActivated,
  kInvalidRequest,
  kCancelled,
  kDeadlineExceeded,
  kManifestIdentityMismatch,
  kRootAuthorityInvalid,
  kUnsafeVolume,
  kUnsafeSecurity,
  kUnsafeName,
  kInventoryMismatch,
  kIdentityMismatch,
  kUnsafeObject,
  kSizeMismatch,
  kHashMismatch,
  kSignatureUntrusted,
  kIoFailed,
  kInternal,
};

struct ObjectIdentity {
  std::uint64_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
};

struct ExecutionContext {
  std::uint64_t deadline_monotonic_ms = 0;
  std::uint64_t (*monotonic_ms)(void*) noexcept = nullptr;
  bool (*is_cancelled)(void*) noexcept = nullptr;
  void* opaque = nullptr;
};

class HandleBoundAuthenticodeVerifier {
 public:
  virtual ~HandleBoundAuthenticodeVerifier() = default;

  // A future implementation must verify the exact retained file identity,
  // use an offline/revocation-unavailable policy explicitly, compare only a
  // SHA-256 signer-subject identity, and return no certificate subject text.
  virtual Status verify_retained_handle(
      HANDLE retained_file,
      const ObjectIdentity& retained_identity,
      const std::array<std::uint8_t, 32>& expected_subject_sha256,
      const ExecutionContext& execution) noexcept = 0;
};

// Opaque future supervisor authority. There is no public issuer, mutator,
// deserializer, or test constructor in this slice. A default capability is
// invalid and owns no handle. Even a future valid capability cannot be used
// while the three immutable activation gates in the implementation are false.
class ReleaseRootCapability final {
 public:
  ReleaseRootCapability() noexcept = default;
  ~ReleaseRootCapability();
  ReleaseRootCapability(const ReleaseRootCapability&) = delete;
  ReleaseRootCapability& operator=(const ReleaseRootCapability&) = delete;
  ReleaseRootCapability(ReleaseRootCapability&&) noexcept;
  ReleaseRootCapability& operator=(ReleaseRootCapability&&) noexcept;

  bool valid() const noexcept;

 private:
  HANDLE retained_root_ = INVALID_HANDLE_VALUE;
  ObjectIdentity root_identity_{};
  std::array<std::uint8_t, 32> manifest_sha256_{};
  std::array<std::uint8_t, 32> issuer_binding_{};
  bool authenticated_supervisor_issued_ = false;
  bool root_opened_without_write_or_delete_share_ = false;
  HandleBoundAuthenticodeVerifier* authenticode_verifier_ = nullptr;

  friend class VerifierCore;
  friend Status verify_release_tree(const struct VerificationRequest*,
                                    struct VerificationResponse*) noexcept;
};

struct VerificationRequest {
  std::uint32_t abi_version = kAbiVersion;
  const ReleaseRootCapability* root_capability = nullptr;
  ExecutionContext execution{};
};

struct Receipt {
  std::string status = "REFUSED_NOT_ACTIVATED";
  std::string reason = "trust_anchor_unavailable";
  std::string manifest_sha256;
  std::uint32_t verified_files = 0;
  std::uint32_t verified_directories = 0;
  std::uint64_t verified_bytes = 0;
  bool root_identity_retained = false;
  bool object_identities_retained = false;
  bool signatures_verified = false;
  bool model_external = true;
  bool activated = false;
};

struct VerificationResponse {
  Receipt receipt{};
  std::string canonical_receipt_json;
};

// Always returns kNotActivated before dereferencing request or touching any
// path, handle, directory, or file. A future activation must replace the false
// trust anchors through a separately reviewed implementation; flipping a
// constant is not sufficient or permitted.
Status verify_release_tree(const VerificationRequest* request,
                           VerificationResponse* response) noexcept;

const char* status_name(Status status) noexcept;

}  // namespace lae::windows_release_verifier
