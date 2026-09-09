// Included only by authority.cpp.  This is the inert phase-2a process seam,
// not a product implementation.  It has no process, pipe, shell, provider,
// journal callback, durable-adapter, or external execution path.

namespace {

using ProcessIdentity = std::array<std::uint8_t, 16>;

struct LaunchLimits final {
  std::uint32_t timeout_ms = 0;
  std::uint32_t output_limit = 0;
};

struct LaunchPlan final {
  // The binding is the complete canonical lease binding: request/call refs,
  // tool/risk/side-effect, argument and preview digests, operation digest,
  // authorization kind, sequence-one receipt and event proof.
  action_journal_helper::ProcessDispatchBinding binding{};
  ProcessIdentity request_identity{};
  ProcessIdentity session_identity{};
  ProcessIdentity stable_identity{};
  LaunchLimits limits{};
  // Non-serializable, supervisor-issued proof shape.  The current source
  // authority is permanently unavailable, so this remains an inert refusal.
  LaunchAuthorityProof authority_proof{};
};

enum class LaunchResultCode : std::uint8_t {
  kUnavailable,
  kInvalidRequest,
  kCapabilityRefused,
};

struct LaunchReceipt final {
  LaunchResultCode status = LaunchResultCode::kUnavailable;
  bool journal_bound = false;
  bool mutation_attempted = false;
};

// The accepted lease has no proof issuer and external-proof authority is
// false.  Refusal is before journal persistence/external dispatch and
// before any process creation or other irreversible mutation.
LaunchReceipt execute_process_transaction(ProcessLaunchAuthority authority,
                                          const LaunchPlan& plan) noexcept {
  // The by-value move-only authority is consumed exactly once and retains its
  // borrow ticket for the entire call, including this inert refusal path. No
  // SupervisorState/raw-owner entry point exists for a future bypass.
  if (!authority.consume()) return LaunchReceipt{};
  if (!launch_authority_proven(plan.authority_proof))
    return LaunchReceipt{};
  if (!kProcessLaunchAvailable || !kSupervisorOwnedProcessTransactionAccepted)
    return LaunchReceipt{};
  return LaunchReceipt{};
}

// A future phase-2b implementation may call only ProcessDispatchLease APIs
// after a separately audited OS-handle evidence verifier exists.  This phase
// adds no issuer, friend, factory, mint, or aggregate bypass.

}  // namespace
