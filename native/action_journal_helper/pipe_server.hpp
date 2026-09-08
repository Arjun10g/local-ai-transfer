#pragma once

#ifndef _WIN32
#error "The ActionJournal helper source is Windows-only"
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

#include "../windows_supervisor/borrow_ticket.hpp"

namespace lae::action_journal_helper {

class JournalAuthorityOwner;

enum class HelperStatus : std::uint8_t {
  kOk,
  kPlatformUnavailable,
  kBootstrapInvalid,
  kBootstrapIssuerUntrusted,
  kBootstrapTimeout,
  kStorageUnavailable,
  kStorageCorrupt,
  kRecoveryFailed,
  kPipeSecurityFailed,
  kPipeConnectFailed,
  kClientIdentityMismatch,
  kInvalidFrame,
  kInvalidMac,
  kReplay,
  kSequenceOutOfOrder,
  kDeadlineExpired,
  kQueueFull,
  kInvalidTransition,
  kCommitNonCancellable,
  kIoTimeout,
  kIoCancelFailed,
  kTransportClosed,
  kInternal,
};

struct BootstrapRecord {
  std::uint32_t expected_client_pid = 0;
  std::uint32_t expected_client_session_id = 0;
  std::uint64_t expected_client_creation_time = 0;
  std::uint64_t expected_client_image_volume_serial = 0;
  std::array<std::uint8_t, 16> expected_client_image_file_id{};
  std::uint64_t expected_storage_volume_serial = 0;
  std::array<std::uint8_t, 16> expected_storage_file_id{};
  std::array<std::uint8_t, 32> expected_container_id{};
  std::array<std::uint8_t, 32> hmac_key{};
  std::array<std::uint8_t, 16> session_nonce{};
  std::wstring storage_directory;
  std::wstring expected_client_image_path;
};

// Reads only from an inherited pipe installed as standard input. The function
// never accepts bootstrap bytes from argv, environment, a file, or the pipe
// client. It creates at most one named-pipe instance and then exits.
// The helper receives a move-only supervisor ticket; it never accepts a raw
// owner or constructs/owns storage authority. The ticket must remain valid
// for the complete call.
HelperStatus run_foreground_helper_from_inherited_stdin(
    ::lae::windows_supervisor::PipeServerBorrow&& borrow) noexcept;
const char* helper_status_name(HelperStatus status) noexcept;

}  // namespace lae::action_journal_helper
