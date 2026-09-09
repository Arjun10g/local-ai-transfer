#pragma once

// Dormant phase-2b contract only.  This header deliberately has no CMake
// target and cannot activate a process.  A future Windows implementation must
// retain these handles in supervisor-owned RAII objects; this POD is only the
// non-serializable proof shape consumed by the already-inert transaction.
#if !defined(_WIN32)
#error "the launch authority contract is Windows-only and unavailable here"
#endif

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <array>
#include <cstdint>

namespace lae::windows_supervisor {

struct ExecutableHandleProof final {
  HANDLE executable_handle = nullptr;
  HANDLE containing_directory_handle = nullptr;
  std::uint64_t size_bytes = 0;
  std::uint32_t volume_serial = 0;
  std::array<std::uint8_t, 16> file_id{};
  std::array<std::uint8_t, 32> sha256{};
  bool absolute_path_manifest_bound = false;
  bool identity_rechecked_after_open = false;
  bool read_only = false;
};

struct WorkingDirectoryHandleProof final {
  HANDLE directory_handle = nullptr;
  std::uint32_t volume_serial = 0;
  std::array<std::uint8_t, 16> directory_id{};
  bool absolute_path_manifest_bound = false;
  bool identity_rechecked_after_open = false;
  bool no_reparse_components = false;
};

struct ContainmentProof final {
  HANDLE job_handle = nullptr;
  HANDLE cancellation_event = nullptr;
  bool job_created_before_child = false;
  bool kill_on_job_close = false;
  bool active_process_zero_on_terminal = false;
  bool no_ambient_handle_inheritance = false;
  bool least_privilege_token = false;
};

struct MinimalEnvironmentProof final {
  // The child receives an implementation-owned fixed allowlist, never a
  // caller map or inherited environment.  No credential-bearing categories
  // are represented in this proof or in a receipt.
  bool exact_allowlist = false;
  bool credentials_excluded = false;
  bool proxy_excluded = false;
  bool user_config_excluded = false;
};

struct LaunchAuthorityProof final {
  ExecutableHandleProof executable{};
  WorkingDirectoryHandleProof working_directory{};
  ContainmentProof containment{};
  MinimalEnvironmentProof environment{};
  bool issuer_bound_to_supervisor = false;
  bool cancellation_owner_is_supervisor = false;
};

// No build flag, manifest, caller argument, or environment value may enable
// this source seam.  A native verifier and target race evidence are still
// required before this constant could ever be reviewed for change.
inline constexpr bool kLaunchAuthorityAvailable = false;

inline bool launch_authority_proven(const LaunchAuthorityProof& proof) noexcept {
  return kLaunchAuthorityAvailable &&
      proof.executable.executable_handle != nullptr &&
      proof.executable.containing_directory_handle != nullptr &&
      proof.executable.absolute_path_manifest_bound &&
      proof.executable.identity_rechecked_after_open && proof.executable.read_only &&
      proof.working_directory.directory_handle != nullptr &&
      proof.working_directory.absolute_path_manifest_bound &&
      proof.working_directory.identity_rechecked_after_open &&
      proof.working_directory.no_reparse_components &&
      proof.containment.job_handle != nullptr &&
      proof.containment.cancellation_event != nullptr &&
      proof.containment.job_created_before_child &&
      proof.containment.kill_on_job_close &&
      proof.containment.active_process_zero_on_terminal &&
      proof.containment.no_ambient_handle_inheritance &&
      proof.containment.least_privilege_token &&
      proof.environment.exact_allowlist &&
      proof.environment.credentials_excluded && proof.environment.proxy_excluded &&
      proof.environment.user_config_excluded &&
      proof.issuer_bound_to_supervisor && proof.cancellation_owner_is_supervisor;
}

}  // namespace lae::windows_supervisor
