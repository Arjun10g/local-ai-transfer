#pragma once

// Dormant phase-2b authority boundary. It is listed in the default-OFF inert
// Windows compile-check target through authority.cpp -- listed, never
// compiled, because that target is gated OFF and hard-fails off WIN32/MSVC --
// and it is outside the product CMake graph. No constructor below can be
// reached by a caller and the availability gate is permanently false in this
// revision.
#if !defined(_WIN32)
#error "the launch authority contract is Windows-only and unavailable here"
#endif

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace lae::windows_supervisor {

// Owning HANDLE wrapper. It is move-only and closes a non-null handle once,
// including on every authority destruction path. No raw handle accessor is
// exposed to callers; the future issuer and verifier are the only friends.
// There is exactly one close call site in this header (reset), so every close
// path -- destructor, move assignment, reassignment -- funnels through one
// branch that is a no-op on a null handle. Self-move-assignment is explicitly
// guarded, so `h = std::move(h)` neither closes nor nulls, and a moved-from
// wrapper is left null so it can never close a handle a second time.
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
//
// Generations are NOT caller-supplied. begin() takes no argument: it derives
// the run generation from the authority's issued generation_ anchor and
// returns it, strictly increasing within this authority and bounded by
// kMaxRunsPerAuthority. Every other transition takes a generation that must
// equal the current active generation exactly, and is otherwise refused.
// Refusal is always typed (false / 0) and never mutates a state it did not
// legally own. kUnknownManual is TERMINAL for an authority instance: restart
// after an orphan or after any cancellation requires a freshly issued
// authority carrying a new generation anchor from the issuer.
//
// Full state/transition table (the only legal transitions; everything not
// listed is refused with no state change):
//
//   From             Event                              Guard                                   To               Result
//   ---------------  ---------------------------------  --------------------------------------  ---------------  ------------
//   kIdle            begin()                            anchor != 0, runs left, next > active   kRunning         new generation
//   kComplete        begin()                            anchor != 0, runs left, next > active   kRunning         new generation
//   kRunning         begin()                            --                                      kRunning         0 (refused)
//   kCancelRequested begin()                            --                                      kCancelRequested 0 (refused)
//   kUnknownManual   begin()                            -- (TERMINAL)                           kUnknownManual   0 (refused)
//   kRunning         request_cancel(g)                  g == active                             kCancelRequested true
//   any other        request_cancel(g)                  --                                      unchanged        false
//   kRunning         complete(g)                        g == active                             kComplete        true
//   kCancelRequested complete(g)                        g == active (deliberate: ambiguous)     kUnknownManual   false
//   kIdle            complete(g)                        -- (never poisoned; F-11)               kIdle            false
//   kComplete        complete(g)                        --                                      kComplete        false
//   kUnknownManual   complete(g)                        --                                      kUnknownManual   false
//   any              complete(g) with stale g           --                                      unchanged        false
//   kCancelRequested finish_bounded_cancel_join(g,true)  g == active, zero active processes     kUnknownManual   true
//   kCancelRequested finish_bounded_cancel_join(g,false) g == active, join TIMED OUT             kUnknownManual   false
//   any other        finish_bounded_cancel_join(g,*)    --                                      unchanged        false
//   kRunning         orphan(g)                          g == active                             kUnknownManual   true
//   kCancelRequested orphan(g)                          g == active                             kUnknownManual   true
//   kIdle            orphan(g)                          -- (nothing ran)                        kIdle            false
//   kComplete        orphan(g)                          --                                      kComplete        false
//   kUnknownManual   orphan(g)                          -- (TERMINAL)                           kUnknownManual   false
//
// Deliberate policy (not an accident of annotation): every transition is
// noexcept and takes a std::mutex. std::lock_guard construction can throw
// std::system_error; inside a noexcept function that is std::terminate().
// Fail-stop on a corrupt lock is the accepted outcome here, matching
// SupervisorState::~SupervisorState, because a half-linearized cancellation
// state is strictly worse than a dead supervisor.
class CancellationState final {
 public:
  CancellationState(const CancellationState&) = delete;
  CancellationState& operator=(const CancellationState&) = delete;

 private:
  friend class LaunchAuthority;
  friend class LaunchAuthorityIssuer;

  // Bounded restart budget, matching the transaction contract's max_children.
  static constexpr std::uint64_t kMaxRunsPerAuthority = 8;

  // Only the issuer can build one, and only with the authority's generation
  // anchor. There is no default constructor, so no caller can create an
  // unanchored state machine.
  explicit CancellationState(std::uint64_t authority_generation) noexcept
      : authority_generation_(authority_generation) {}

  // Returns the new run generation, or 0 for a typed refusal. No caller ever
  // supplies a generation (F-3). kUnknownManual is terminal and a live run is
  // never restarted in place (F-2).
  std::uint64_t begin() noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (authority_generation_ == 0) return 0;
    if (state_ == RunState::kRunning || state_ == RunState::kCancelRequested ||
        state_ == RunState::kUnknownManual)
      return 0;
    if (run_index_ >= kMaxRunsPerAuthority) return 0;
    if (authority_generation_ >
        (std::numeric_limits<std::uint64_t>::max)() - kMaxRunsPerAuthority)
      return 0;
    const std::uint64_t next = authority_generation_ + run_index_;
    if (next <= active_generation_) return 0;
    ++run_index_;
    active_generation_ = next;
    state_ = RunState::kRunning;
    return active_generation_;
  }

  bool request_cancel(std::uint64_t generation) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (generation == 0 || generation != active_generation_) return false;
    if (state_ != RunState::kRunning) return false;
    state_ = RunState::kCancelRequested;
    return true;
  }

  // A stale generation, or a completion for an authority that never started,
  // is refused without touching the state: a never-started authority is never
  // poisoned to kUnknownManual (F-11). A completion that races an in-flight
  // cancel is deliberately recorded as the contract's ambiguous outcome and
  // is never reported as success.
  bool complete(std::uint64_t generation) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (generation == 0 || generation != active_generation_) return false;
    if (state_ == RunState::kCancelRequested) {
      state_ = RunState::kUnknownManual;
      return false;
    }
    if (state_ != RunState::kRunning) return false;
    state_ = RunState::kComplete;
    return true;
  }

  // Both outcomes of the bounded join are terminal. A join that observed zero
  // active contained processes returns true; a join that TIMED OUT with
  // children still live returns false and still lands in kUnknownManual
  // rather than leaving the authority in kCancelRequested (F-12). A cancelled
  // run is never restarted in place and never reported as a completion.
  bool finish_bounded_cancel_join(std::uint64_t generation,
                                  bool active_process_zero) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (generation == 0 || generation != active_generation_) return false;
    if (state_ != RunState::kCancelRequested) return false;
    state_ = RunState::kUnknownManual;
    return active_process_zero;
  }

  // Orphan marking is fenced by exactly the same generation and state guard as
  // every other transition (F-2). It is legal only for the live generation of
  // a run that actually started, it cannot be used to escape kRunning into a
  // restart, and kUnknownManual stays terminal.
  bool orphan(std::uint64_t generation) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (generation == 0 || generation != active_generation_) return false;
    if (state_ != RunState::kRunning && state_ != RunState::kCancelRequested)
      return false;
    state_ = RunState::kUnknownManual;
    return true;
  }

  mutable std::mutex mutex_;
  std::uint64_t authority_generation_ = 0;
  std::uint64_t active_generation_ = 0;
  std::uint64_t run_index_ = 0;
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
  // not create, acknowledge, retry, or launch a child. begin_run() returns the
  // issuer-anchored run generation, or 0 when the transition is refused; the
  // caller cannot choose a generation.
  std::uint64_t begin_run() noexcept {
    return cancellation_state_ != nullptr ? cancellation_state_->begin() : 0;
  }
  bool request_cancel(std::uint64_t generation) noexcept {
    return cancellation_state_ != nullptr && cancellation_state_->request_cancel(generation);
  }
  bool complete_run(std::uint64_t generation) noexcept {
    return cancellation_state_ != nullptr && cancellation_state_->complete(generation);
  }
  bool finish_bounded_cancel_join(std::uint64_t generation,
                                  bool active_process_zero) noexcept {
    return cancellation_state_ != nullptr &&
        cancellation_state_->finish_bounded_cancel_join(generation,
                                                        active_process_zero);
  }
  bool mark_orphaned(std::uint64_t generation) noexcept {
    return cancellation_state_ != nullptr && cancellation_state_->orphan(generation);
  }

  // Read-only admission check; it cannot create or expose proof material.
  bool validate_for_admission() const noexcept { return valid_for_admission(); }

 private:
  friend class LaunchAuthorityIssuer;

  // Every boolean below is named exactly as the contract predicate it
  // discharges, so contracts/windows-process-authority/v1.0.0.json can be
  // cross-checked against this header mechanically. A conjunct deleted from
  // valid_for_admission() fails the static suite.
  struct FileIdentity final {
    std::wstring canonical_absolute_path;
    std::uint64_t size_bytes = 0;
    std::uint32_t volume_serial = 0;
    // Also carries the working directory's directory_id_128.
    std::array<std::uint8_t, 16> file_id_128{};
    std::array<std::uint8_t, 32> sha256{};
    bool absolute_manifest_path_binding = false;
    bool no_reparse_components = false;
    bool identity_rechecked_after_open = false;
    bool read_only_open = false;
  };

  struct HandleIdentity final {
    UniqueHandle handle;
    std::array<std::uint8_t, 16> object_id{};
    bool identity_verified = false;
  };

  // Ordered argument vector. The Windows process-creation API has no argv
  // array: it takes a single lpCommandLine string, so argv-to-command-line
  // quoting is the injection surface and the reason policy requires an
  // executable plus an argument array rather than a concatenated shell
  // command. The future native layer must keep the ordered array as
  // the only source of truth, build lpCommandLine with the one fixed
  // documented escaping algorithm (the CommandLineToArgvW / MSVCRT parsing
  // rules: backslashes are literal except before a quote, where they double,
  // and each argument is wrapped in quotes), then verify round-trip parse
  // equality against the array before launch. No shell, no concatenated
  // command string, no caller-supplied command line ever reaches a launcher.
  // This revision binds and validates the obligation only; it launches nothing.
  struct ArgumentVector final {
    // argv[0] is not a member: the image is bound by handle and canonical
    // path, never by a caller-chosen program-name token.
    std::vector<std::wstring> ordered_arguments;
    std::array<std::uint8_t, 32> argument_digest{};
    std::uint32_t total_utf16_units = 0;
  };

  // Only a private issuer can assemble this fully typed set of already
  // verified values. It contains owning handles, never borrowed raw handles.
  struct MintedParts final {
    HandleIdentity executable;
    HandleIdentity executable_parent_directory;
    HandleIdentity working_directory;
    HandleIdentity token;
    HandleIdentity job;
    HandleIdentity cancellation_event;
    FileIdentity executable_file;
    FileIdentity working_directory_file;
    ArgumentVector arguments;
    std::array<std::uint8_t, 16> operation_id{};
    std::array<std::uint8_t, 16> nonce{};
    std::uint64_t generation = 0;
    std::array<std::uint8_t, 32> environment_digest{};
    // contract identity_pinned_handles.*.required
    bool supervisor_owned_executable_handle = false;
    bool supervisor_owned_containing_directory_handle = false;
    bool supervisor_owned_directory_handle = false;
    // contract pre_child_containment.required
    bool job_created_before_child = false;
    bool kill_on_job_close = false;
    bool active_process_zero_on_terminal = false;
    bool no_ambient_handle_inheritance = false;
    bool least_privilege_token = false;
    // contract environment.proof_fields
    bool exact_allowlist = false;
    bool credentials_excluded = false;
    bool proxy_excluded = false;
    bool user_config_excluded = false;
    // contract cancellation_and_orphan.required
    bool supervisor_owned_cancellation_event = false;
    bool bounded_cancel_join = false;
    bool kill_entire_contained_tree = false;
    bool active_process_zero_before_release = false;
    bool supervisor_death_closes_containment = false;
    // contract arguments.required / arguments.proof_fields
    bool argv_array_only = false;
    bool command_line_escaping_fixed = false;
    bool command_line_round_trip_verified = false;
    bool no_shell_or_concatenated_command_line = false;
    bool utf16_safe_arguments = false;
    bool argument_count_and_length_bounded = false;
    bool issuer_bound_to_supervisor = false;
    bool cancellation_owner_is_supervisor = false;
    std::unique_ptr<CancellationState> cancellation_state;
  };

  // Read-only admission validation runs before ProcessLaunchAuthority is
  // consumed. The source-only false gate prevents any authority admission.
  // Every conjunct below discharges a named contract predicate; the static
  // suite asserts each contract required/proof_fields name appears here.
  bool valid_for_admission() const noexcept {
    return kLaunchAuthorityAvailable &&
        nonzero(operation_id_) && nonzero(nonce_) && generation_ != 0 &&
        cancellation_state_ != nullptr &&
        supervisor_owned_executable_handle_ &&
        executable_.handle.value_ != nullptr && executable_.identity_verified &&
        supervisor_owned_containing_directory_handle_ &&
        executable_parent_directory_.handle.value_ != nullptr &&
        executable_parent_directory_.identity_verified &&
        executable_file_.identity_rechecked_after_open &&
        executable_file_.read_only_open &&
        executable_file_.absolute_manifest_path_binding &&
        executable_file_.no_reparse_components &&
        canonical_absolute(executable_file_.canonical_absolute_path) &&
        executable_file_.size_bytes != 0 && executable_file_.volume_serial != 0 &&
        nonzero(executable_file_.file_id_128) && nonzero(executable_file_.sha256) &&
        supervisor_owned_directory_handle_ &&
        working_directory_.handle.value_ != nullptr &&
        working_directory_.identity_verified &&
        working_directory_file_.identity_rechecked_after_open &&
        working_directory_file_.absolute_manifest_path_binding &&
        working_directory_file_.no_reparse_components &&
        canonical_absolute(working_directory_file_.canonical_absolute_path) &&
        working_directory_file_.volume_serial != 0 &&
        nonzero(working_directory_file_.file_id_128) &&
        argv_array_only_ && command_line_escaping_fixed_ &&
        command_line_round_trip_verified_ &&
        no_shell_or_concatenated_command_line_ && utf16_safe_arguments_ &&
        argument_count_and_length_bounded_ && bounded_arguments(arguments_) &&
        nonzero(arguments_.argument_digest) &&
        token_.handle.value_ != nullptr && token_.identity_verified && nonzero(token_.object_id) &&
        job_.handle.value_ != nullptr && job_.identity_verified && nonzero(job_.object_id) &&
        supervisor_owned_cancellation_event_ &&
        cancellation_event_.handle.value_ != nullptr &&
        cancellation_event_.identity_verified && nonzero(cancellation_event_.object_id) &&
        job_created_before_child_ &&
        kill_on_job_close_ && active_process_zero_on_terminal_ &&
        no_ambient_handle_inheritance_ && least_privilege_token_ &&
        exact_allowlist_ && nonzero(environment_digest_) &&
        credentials_excluded_ && proxy_excluded_ && user_config_excluded_ &&
        bounded_cancel_join_ && kill_entire_contained_tree_ &&
        active_process_zero_before_release_ &&
        supervisor_death_closes_containment_ &&
        issuer_bound_to_supervisor_ && cancellation_owner_is_supervisor_;
  }

  explicit LaunchAuthority(MintedParts&& parts) noexcept
      : executable_(std::move(parts.executable)),
        executable_parent_directory_(std::move(parts.executable_parent_directory)),
        working_directory_(std::move(parts.working_directory)),
        token_(std::move(parts.token)), job_(std::move(parts.job)),
        cancellation_event_(std::move(parts.cancellation_event)),
        executable_file_(std::move(parts.executable_file)),
        working_directory_file_(std::move(parts.working_directory_file)),
        arguments_(std::move(parts.arguments)),
        operation_id_(parts.operation_id), nonce_(parts.nonce),
        generation_(parts.generation), environment_digest_(parts.environment_digest),
        supervisor_owned_executable_handle_(parts.supervisor_owned_executable_handle),
        supervisor_owned_containing_directory_handle_(
            parts.supervisor_owned_containing_directory_handle),
        supervisor_owned_directory_handle_(parts.supervisor_owned_directory_handle),
        job_created_before_child_(parts.job_created_before_child),
        kill_on_job_close_(parts.kill_on_job_close),
        active_process_zero_on_terminal_(parts.active_process_zero_on_terminal),
        no_ambient_handle_inheritance_(parts.no_ambient_handle_inheritance),
        least_privilege_token_(parts.least_privilege_token),
        exact_allowlist_(parts.exact_allowlist),
        credentials_excluded_(parts.credentials_excluded),
        proxy_excluded_(parts.proxy_excluded),
        user_config_excluded_(parts.user_config_excluded),
        supervisor_owned_cancellation_event_(parts.supervisor_owned_cancellation_event),
        bounded_cancel_join_(parts.bounded_cancel_join),
        kill_entire_contained_tree_(parts.kill_entire_contained_tree),
        active_process_zero_before_release_(parts.active_process_zero_before_release),
        supervisor_death_closes_containment_(parts.supervisor_death_closes_containment),
        argv_array_only_(parts.argv_array_only),
        command_line_escaping_fixed_(parts.command_line_escaping_fixed),
        command_line_round_trip_verified_(parts.command_line_round_trip_verified),
        no_shell_or_concatenated_command_line_(
            parts.no_shell_or_concatenated_command_line),
        utf16_safe_arguments_(parts.utf16_safe_arguments),
        argument_count_and_length_bounded_(parts.argument_count_and_length_bounded),
        issuer_bound_to_supervisor_(parts.issuer_bound_to_supervisor),
        cancellation_owner_is_supervisor_(parts.cancellation_owner_is_supervisor),
        cancellation_state_(std::move(parts.cancellation_state)) {}

  template <std::size_t N>
  static bool nonzero(const std::array<std::uint8_t, N>& value) noexcept {
    for (const std::uint8_t byte : value) {
      if (byte != 0) return true;
    }
    return false;
  }

  // Per-argument and total command-line length bounds. The total bound is the
  // documented Windows lpCommandLine ceiling minus the room a future builder
  // needs for the quoted image path.
  static bool bounded_arguments(const ArgumentVector& arguments) noexcept {
    if (arguments.ordered_arguments.size() > kMaxArguments) return false;
    std::uint64_t total = 0;
    for (const std::wstring& argument : arguments.ordered_arguments) {
      if (argument.size() > kMaxArgumentUtf16Units) return false;
      for (const wchar_t character : argument) {
        // Embedded NULs, control characters, and unpaired-surrogate-prone
        // shapes are refused: the array must be UTF-16-safe before any
        // command line is built from it.
        if (character < 0x20 || character == 0x7f) return false;
      }
      total += static_cast<std::uint64_t>(argument.size()) + 3u;
    }
    if (total > kMaxCommandLineUtf16Units) return false;
    return arguments.total_utf16_units == static_cast<std::uint32_t>(total);
  }

  // Necessary shape test for a canonical absolute Windows image path. It is a
  // necessary, never a sufficient, condition: the future verifier must still
  // resolve reparse points and re-derive identity from the live handle.
  // Exactly one shape is accepted: \\?\<drive letter>:\<component>[\...].
  // Refused: \\?\UNC\ and every other non-drive first component (device and
  // volume namespaces such as \\?\GLOBALROOT\ and \\?\Volume{...}), \\.\
  // device paths, bare UNC \\server\share, any colon after the drive-letter
  // colon (alternate data streams and device/stream qualifiers), reserved
  // device names with or without an extension, relative paths and "."/".."
  // components, 8.3 short-name components containing '~', components with a
  // trailing dot or space, empty components including a trailing separator,
  // and forward-slash separators.
  static bool canonical_absolute(const std::wstring& path) noexcept {
    if (path.size() < 8 || path.size() > 32767) return false;
    if (path.compare(0, 4, L"\\\\?\\") != 0) return false;
    const wchar_t drive = path[4];
    const bool drive_letter = (drive >= L'A' && drive <= L'Z') ||
                              (drive >= L'a' && drive <= L'z');
    if (!drive_letter || path[5] != L':' || path[6] != L'\\') return false;
    for (std::size_t index = 4; index < path.size(); ++index) {
      const wchar_t character = path[index];
      if (character < 0x20 || character == 0x7f || character == L'<' ||
          character == L'>' || character == L'|' || character == L'"' ||
          character == L'*' || character == L'?' || character == L'/')
        return false;
      if (character == L':' && index != 5) return false;
    }
    std::size_t start = 7;
    while (start < path.size()) {
      std::size_t stop = path.find(L'\\', start);
      if (stop == std::wstring::npos) stop = path.size();
      if (stop == start) return false;
      const std::size_t length = stop - start;
      const wchar_t* begin = path.data() + start;
      if (length == 1 && begin[0] == L'.') return false;
      if (length == 2 && begin[0] == L'.' && begin[1] == L'.') return false;
      if (begin[length - 1] == L'.' || begin[length - 1] == L' ') return false;
      if (begin[0] == L' ') return false;
      for (std::size_t scan = 0; scan < length; ++scan) {
        if (begin[scan] == L'~') return false;
      }
      if (reserved_device_name(begin, length)) return false;
      if (stop == path.size()) return true;
      start = stop + 1;
    }
    return false;
  }

  // \\?\ suppresses Win32 name rewriting, so a reserved device name reaches
  // the object manager instead of being rejected. Compare the component stem
  // (text before the first dot) case-insensitively.
  static bool reserved_device_name(const wchar_t* begin,
                                   std::size_t length) noexcept {
    std::size_t stem = 0;
    while (stem < length && begin[stem] != L'.') ++stem;
    if (stem != 3 && stem != 4) return false;
    wchar_t upper[4] = {0, 0, 0, 0};
    for (std::size_t index = 0; index < stem; ++index) {
      wchar_t character = begin[index];
      if (character >= L'a' && character <= L'z')
        character = static_cast<wchar_t>(character - L'a' + L'A');
      upper[index] = character;
    }
    const bool con = upper[0] == L'C' && upper[1] == L'O' && upper[2] == L'N';
    const bool prn = upper[0] == L'P' && upper[1] == L'R' && upper[2] == L'N';
    const bool aux = upper[0] == L'A' && upper[1] == L'U' && upper[2] == L'X';
    const bool nul = upper[0] == L'N' && upper[1] == L'U' && upper[2] == L'L';
    if (stem == 3) return con || prn || aux || nul;
    const bool com = upper[0] == L'C' && upper[1] == L'O' && upper[2] == L'M';
    const bool lpt = upper[0] == L'L' && upper[1] == L'P' && upper[2] == L'T';
    return (com || lpt) && upper[3] >= L'1' && upper[3] <= L'9';
  }

  static constexpr bool kLaunchAuthorityAvailable = false;
  static constexpr std::size_t kMaxArguments = 64;
  static constexpr std::size_t kMaxArgumentUtf16Units = 8192;
  static constexpr std::uint64_t kMaxCommandLineUtf16Units = 30000;
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
  ArgumentVector arguments_;
  std::array<std::uint8_t, 16> operation_id_{};
  std::array<std::uint8_t, 16> nonce_{};
  std::uint64_t generation_ = 0;
  std::array<std::uint8_t, 32> environment_digest_{};
  bool supervisor_owned_executable_handle_ = false;
  bool supervisor_owned_containing_directory_handle_ = false;
  bool supervisor_owned_directory_handle_ = false;
  bool job_created_before_child_ = false;
  bool kill_on_job_close_ = false;
  bool active_process_zero_on_terminal_ = false;
  bool no_ambient_handle_inheritance_ = false;
  bool least_privilege_token_ = false;
  bool exact_allowlist_ = false;
  bool credentials_excluded_ = false;
  bool proxy_excluded_ = false;
  bool user_config_excluded_ = false;
  bool supervisor_owned_cancellation_event_ = false;
  bool bounded_cancel_join_ = false;
  bool kill_entire_contained_tree_ = false;
  bool active_process_zero_before_release_ = false;
  bool supervisor_death_closes_containment_ = false;
  bool argv_array_only_ = false;
  bool command_line_escaping_fixed_ = false;
  bool command_line_round_trip_verified_ = false;
  bool no_shell_or_concatenated_command_line_ = false;
  bool utf16_safe_arguments_ = false;
  bool argument_count_and_length_bounded_ = false;
  bool issuer_bound_to_supervisor_ = false;
  bool cancellation_owner_is_supervisor_ = false;
  std::unique_ptr<CancellationState> cancellation_state_;
};

// Private issuer declaration only. It has no public constructor or factory and
// no friend outside this header. The previous friendship named an undefined
// namespace-scope supervisor-state type that no translation unit in this module
// defines -- the real one is a distinct type declared inside an unnamed
// namespace in authority.cpp, and an elaborated-type-specifier declaration
// there introduces a new class in that unnamed namespace -- so the friendship
// was simultaneously vacuous and an open claim on an unowned namespace-scope
// name that any translation unit could have defined (F-6). It is removed. The enforcement
// invariant is therefore stated explicitly and does not rest on friendship:
// the only constructor of a non-null UniqueHandle is private to this header,
// so no type outside it can assemble an authority that passes
// valid_for_admission(), and every friend named in this header is a type that
// this header itself declares. A future issuer must be declared in this
// header, in this namespace, and befriended by exact name after the native
// verifier, proof issuer, and target race evidence are accepted.
class LaunchAuthorityIssuer final {
 private:
  friend class LaunchAuthority;
  LaunchAuthorityIssuer() = delete;
  static std::optional<LaunchAuthority> issue(
      LaunchAuthority::MintedParts&& parts) noexcept {
    try {
      LaunchAuthority authority(std::move(parts));
      return std::optional<LaunchAuthority>(std::move(authority));
    } catch (...) {
      return std::nullopt;
    }
  }
};

}  // namespace lae::windows_supervisor
