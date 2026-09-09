# J1M Strict Lifecycle Test Sync v1

## Status

- Role: Luna
- Branch: luna/j1m-strict-lifecycle-test-sync-v1
- Base: 2ad3006d684a8b36bfb4f4827855165d470cb877
- Scope: test fixtures and expectations only; no production or remote-gate changes
- State: candidate complete, pending Sol review

## Bounded changes

- Lifecycle fixtures now use owner-private temporary roots and explicit 0700 directories/0600 receipt files where strict persistence/read contracts require them.
- Tests use finite sanitized launch/refusal codes and assert retired external salvage refuses before _remote.
- Descriptor-path test isolation preserves the capability probe after wrapping os.open; it does not weaken the production descriptor policy.
- The model fixture remains unchanged at 33 tools, 37 cases, and max_cases=64.

## Evidence

- tests.performance.test_j1m_lifecycle: 117 passed.
- tests.performance.test_remote_canary_secret_hardening: 27 passed.
- tests.performance.test_probe_and_preflight: 18 passed.
- tests.model.test_tool_call_eval: 32 passed.
- Related remote lifecycle/teardown suites: 68 passed.
- Node fixture parity: 4 passed.
- scripts/test/run_qa.py: expected safe-mode BLOCKED; execution suites are inventoried as skipped and no live/provider/model process is enabled.

## Uncertainty and gates

No network, provider, SSH, download, model, build, compile, or live-process operation was performed. Remote execution remains disabled and canary execution remains a guarded plan-only seam. Sol must review and integrate; this worktree does not merge.
