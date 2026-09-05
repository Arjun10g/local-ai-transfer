#pragma once

#include "manifest.hpp"
#include "protocol.hpp"
#include "win32_identity.hpp"

#include <atomic>
#include <string>

namespace lae::windows_broker {

// Evaluates one already schema-checked and manifest-bound invocation. Normal
// startup is disabled, launch kinds are refusal-only, and no child can be
// created by this revision. The retained clipboard implementation is likewise
// unreachable through normal startup. No function in this API accepts an
// executable path, argv vector, environment, or cwd from a request.
Response execute_bound_action(const Request& request, const ActionSpec& action,
                              const FileIdentitySpec* executable,
                              const DirectoryIdentitySpec* cwd,
                              const ManifestLease& manifest,
                              std::atomic<bool>& cancelled) noexcept;

}  // namespace lae::windows_broker
