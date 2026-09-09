#pragma once

// Dormant phase-2b authority boundary. It is included transitively by the
// default-OFF inert Windows compile-check target through authority.cpp, but is
// outside the product CMake graph. No constructor below can be reached by a
// caller and the availability gate is permanently false in this revision.
#if !defined(_WIN32)
#error "the launch authority contract is Windows-only and unavailable here"
#endif

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

namespace lae::windows_supervisor {

struct SupervisorState;

// Owning HANDLE wrapper. It is move-only and closes a non-null handle once,
// including on every authority destruction path. No raw handle accessor is
// exposed to callers; the future issuer and verifier are the only friends.
class UniqueHandle final {
 public:
  UniqueHandle() noexcept = default;
  UniqueHandle(const UniqueHandle&) = delete;
  UniqueHandle& operator=(const UniqueHandle&) = delete;
  UniqueHandle(UniqueHandle&& other) noexcept : value_(other.release()) {}
  UniqueHandle& operator=(UniqueHandle&& other) noexcept {
    if (this != &other) reset(other.release());
    return *this;
  }
  ~UniqueHandle() noexcept { reset(); }

 private:
  friend class LaunchAuthority;
  friend class LaunchAuthorityIssuer;
  explicit UniqueHandle(HANDLE value) noexcept : value_(value) {}
  HANDLE release() noexcept {
    HANDLE value = value_;
    value_ = nullptr;
    return value;
  }
  void reset(HANDLE value = nullptr) noexcept {
    if (value_ != nullptr) ::CloseHandle(value_);
    value_ = value;
  }
  HANDLE value_ = nullptr;
};

enum class RunState : std::uint8_t {
  kIdle,
  kRunning,
  kCancelRequested,
  kComplete,
  kUnknownManual,
};

// Actual dormant cancellation state machine. The mutex protects the
// generation/state pair as one linearization point: a completion from an old
// run cannot alter a restarted run. It never waits on a process or owner lock.
// A future native caller must perform a bounded join and observe zero active
// Job-object processes before release.
class CancellationState final {
 public:
  CancellationState() = default;
  CancellationState(const CancellationState&) = delete;
  CancellationState& operator=(const CancellationState&) = delete;

 private:
  friend class LaunchAuthority;
  friend class LaunchAuthorityIssuer;

  bool begin(std::uint64_t generation) noexcept {
    if (generation == 0) return false;
    std::lock_guard<std::mutex> lock(mutex_);
    active_generation_ = generation;
    state_ = RunState::kRunning;
    return true;
  }

  bool request_cancel(std::uint64_t generation) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (active_generation_ != generation || state_ != RunState::kRunning)
      return false;
    state_ = RunState::kCancelRequested;
    return true;
  }

  bool complete(std::uint64_t generation) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (active_generation_ != generation) return false;
    if (state_ != RunState::kRunning) {
      state_ = RunState::kUnknownManual;
      return false;
    }
    state_ = RunState::kComplete;
    return true;
  }

  void orphan() noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    state_ = RunState::kUnknownManual;
  }

  mutable std::mutex mutex_;
  std::uint64_t active_generation_ = 0;
  RunState state_ = RunState::kIdle;
};

// Opaque, one-use authority. Only the private supervisor issuer can construct
// one; callers receive it by move and cannot manufacture, copy, inspect, or
// serialize its handles or identity values.
class LaunchAuthority final {
 public:
  LaunchAuthority(const LaunchAuthority&) = delete;
  LaunchAuthority& operator=(const LaunchAuthority&) = delete;
  LaunchAuthority(LaunchAuthority&&) noexcept = default;
  LaunchAuthority& operator=(LaunchAuthority&&) noexcept = default;
  ~LaunchAuthority() noexcept = default;

  // These operations are harmless on an unavailable authority and are the
  // only state transitions exposed to a future supervisor call site. They do
  // not create, acknowledge, retry, or launch a child.
  bool begin_run(std::uint64_t generation) noexcept {
    return cancellation_state_ != nullptr && cancellation_state_->begin(generation);
  }
  bool request_cancel(std::uint64_t generation) noexcept {
    return cancellation_state_ != nullptr && cancellation_state_->request_cancel(generation);
  }
  bool complete_run(std::uint64_t generation) noexcept {
    return cancellation_state_ != nullptr && cancellation_state_->complete(generation);
  }
  void mark_orphaned() noexcept {
    if (cancellation_state_ != nullptr) cancellation_state_->orphan();
  }

  // Read-only admission check; it cannot create or expose proof material.
  bool validate_for_admission() const noexcept { return valid_for_admission(); }

 private:
  friend class LaunchAuthorityIssuer;

  struct FileIdentity final {
    std::wstring canonical_absolute_path;
    std::uint64_t size_bytes = 0;
    std::uint32_t volume_serial = 0;
    std::array<std::uint8_t, 16> file_id{};
    std::array<std::uint8_t, 32> sha256{};
    bool identity_rechecked_after_open = false;
    bool read_only = false;
  };

  struct HandleIdentity final {
    UniqueHandle handle;
    std::array<std::uint8_t, 16> object_id{};
    bool identity_verified = false;
  };

  // Read-only admission validation runs before ProcessLaunchAuthority is
  // consumed. The source-only false gate prevents any authority admission.
  bool valid_for_admission() const noexcept {
    return kLaunchAuthorityAvailable &&
        nonzero(operation_id_) && nonzero(nonce_) && generation_ != 0 &&
        executable_.handle.value_ != nullptr && executable_.identity_verified &&
        executable_parent_directory_.handle.value_ != nullptr &&
        executable_parent_directory_.identity_verified &&
        executable_file_.identity_rechecked_after_open && executable_file_.read_only &&
        canonical_absolute(executable_file_.canonical_absolute_path) &&
        executable_file_.size_bytes != 0 && executable_file_.volume_serial != 0 &&
        nonzero(executable_file_.file_id) && nonzero(executable_file_.sha256) &&
        working_directory_.handle.value_ != nullptr &&
        working_directory_.identity_verified &&
        working_directory_file_.identity_rechecked_after_open &&
        canonical_absolute(working_directory_file_.canonical_absolute_path) &&
        working_directory_file_.volume_serial != 0 &&
        nonzero(working_directory_file_.file_id) &&
        token_.handle.value_ != nullptr && token_.identity_verified && nonzero(token_.object_id) &&
        job_.handle.value_ != nullptr && job_.identity_verified && nonzero(job_.object_id) &&
        cancellation_event_.handle.value_ != nullptr &&
        cancellation_event_.identity_verified && nonzero(cancellation_event_.object_id) &&
        containment_ready_ &&
        kill_on_job_close_ && active_process_zero_on_terminal_ &&
        no_ambient_handle_inheritance_ && least_privilege_token_ &&
        fixed_environment_allowlist_ && nonzero(environment_digest_) &&
        issuer_bound_to_supervisor_ && cancellation_owner_is_supervisor_;
  }

  // No production source currently calls this private constructor. The
  // issuer would fill every field only after descriptor/handle validation.
  LaunchAuthority() = default;

  template <std::size_t N>
  static bool nonzero(const std::array<std::uint8_t, N>& value) noexcept {
    for (const std::uint8_t byte : value) {
      if (byte != 0) return true;
    }
    return false;
  }

  static bool canonical_absolute(const std::wstring& path) noexcept {
    if (path.size() < 7 || path.rfind(L"\\\\?\\", 0) != 0) return false;
    for (const wchar_t character : path) {
      if (character < 0x20 || character == L'<' || character == L'>' ||
          character == L'|' || character == L'"') return false;
    }
    return path.find(L"..") == std::wstring::npos;
  }

  static constexpr bool kLaunchAuthorityAvailable = false;
  inline static constexpr std::array<const char*, 2>
      kMinimalEnvironmentAllowlist = {"SystemRoot", "WINDIR"};
  HandleIdentity executable_;
  HandleIdentity executable_parent_directory_;
  HandleIdentity working_directory_;
  HandleIdentity token_;
  HandleIdentity job_;
  HandleIdentity cancellation_event_;
  FileIdentity executable_file_;
  FileIdentity working_directory_file_;
  std::array<std::uint8_t, 16> operation_id_{};
  std::array<std::uint8_t, 16> nonce_{};
  std::uint64_t generation_ = 0;
  std::array<std::uint8_t, 32> environment_digest_{};
  bool containment_ready_ = false;
  bool kill_on_job_close_ = false;
  bool active_process_zero_on_terminal_ = false;
  bool no_ambient_handle_inheritance_ = false;
  bool least_privilege_token_ = false;
  bool fixed_environment_allowlist_ = false;
  bool issuer_bound_to_supervisor_ = false;
  bool cancellation_owner_is_supervisor_ = false;
  std::unique_ptr<CancellationState> cancellation_state_;
};

// Private issuer declaration only. It has no public constructor or factory;
// a future SupervisorState implementation may become its friend after the
// native verifier, proof issuer, and target race evidence are accepted.
class LaunchAuthorityIssuer final {
 private:
  friend class LaunchAuthority;
  friend struct SupervisorState;
  LaunchAuthorityIssuer() = delete;
  static LaunchAuthority issue() noexcept { return LaunchAuthority(); }
};

}  // namespace lae::windows_supervisor
