# Status Packet

## Current governance snapshot — 2026-09-05

- The durable journal/controller barrier is source-merged at `65decba`, and the
  bounded wire-protocol contract is source-merged at `3746421`.
- Production journal availability remains false. The protocol merge adds no
  native helper, handle-relative store, transport, key trust anchor, package
  integration, process launch, recovery deployment, or activation.
- Consequently Graph/browser/Copilot/process mutations remain hidden/refused
  when production durability is required. Graph reconciliation source being
  merged is not live-provider readiness; browser candidate `5dc2ad2` is
  rejected and repair `fc317fce023f9364e7f19b69a700124d1936f8ca` remains
  unmerged/unreviewed.
- Overall action-journal/production-action state: `NOT_READY`; no formal gate is
  approved.

The interval packets below are retained as historical evidence and are
superseded where they describe current merge or availability state.

- **Session:** S3
- **Required model:** GPT-5.6 Luna
- **Role:** Agent/Tools — durable generic action-journal core and controller dispatch barrier
- **Timestamp (UTC):** 2026-09-05T03:21:19Z
- **Branch/worktree:** `luna/durable-action-journal-core` / `wt-action-journal-core`
- **Current phase:** Phase 4/6 autonomous action recovery hardening
- **Primary task ID:** TOOL-032
- **Task state:** READY_FOR_REVIEW
- **Base:** exact `main` `37a134f`

## Outcome

Implemented a provider-independent, bounded per-operation JSONL/hash-chain journal and made its fsynced `dispatching` transition a hard controller barrier for side-effecting tools and confirmed Copilot egress. Missing or unhealthy journals remove those tools from every model generation and still reject hostile direct proposals. Reads remain unjournaled.

The test-only POSIX pathname seam requires a precreated canonical current-user `0700` directory, creates `0600` records, uses bounded/O_NOFOLLOW/identity/link-count checks, and fails closed on malformed/torn/over-limit state. Production dispatch is unavailable until a native ACL/reparse-safe handle-relative store exists. Receipts export digests, never request/call IDs, arguments, previews, content, results, prompts, or credentials.

Startup cancels pre-dispatch records, changes `dispatching` to `unknown_manual`, and never manufactures completion from `acknowledged`/`reconciling`. Exact unresolved tool+argument retries are refused across request/call IDs and nondeterministic preview revisions. Terminal records are durably pruned oldest-first; active records are never pruned.

Authenticated loopback summary/detail endpoints and assertion-only terminal resolution are bounded. Reconciliation returns typed HTTP 501 and never invokes/replays a provider. Unverified Graph/browser acknowledgements remain `reconciling` and are surfaced to the model as `action_completion_unverified`.

## Evidence

- Product commit: `2cf1f8bd4f5499e0091d663047b6aa6495cc070e`
- Focused syntax checks: `node --check` for journal, controller, HostServer, and launcher — PASS
- `node --test tests/host/action-journal.test.mjs tests/host/tool-chain-vertical.test.mjs` — 21 pass, 0 fail
- Targeted provider/controller compatibility: `node --test --test-name-pattern='controller advertises|controller stages|process schema' tests/host/external-tools.test.mjs tests/host/process-run.test.mjs` — 3 pass, 0 fail
- A prior mixed mocked/static run was not a valid completion receipt: its Copilot fixture was outside this journal repair and was not rerun as evidence here. No Copilot behavior is changed or claimed by this slice.
- No native build, model/provider/account/browser access, broad/heavy suite, or external network was used.

## Residual blockers

- Windows journal execution is NOT READY and intentionally fails `action_journal_platform_unavailable`; a native ACL/reparse-safe, handle-relative protected store is required.
- POSIX production journal dispatch is also NOT READY: Node's built-in promises API does not provide the required handle-relative `openat`/`unlinkat` primitives. The pathname implementation is retained only behind an explicit `testOnly` seam with regular-file/link-count and identity/size checks; it must not authorize model actions.
- `/api/status` now reports journal health and `durable_action_dispatch`; blocked/unavailable health is not write readiness. Unresolved provider actions remain reconciling and provider completion is not claimed.
- Graph/browser provider idempotency and reconciliation adapters are not implemented. Their mutations cannot be represented as completed by this core.
- Changing action arguments can evade core argument-digest duplicate refusal; provider-semantic idempotency/reconciliation remains mandatory.
- Deployment must provision the canonical private POSIX journal directory and set `LAE_ACTION_JOURNAL_DIR`; without it all durable actions/egress are truthfully hidden and fail closed.
- This slice does not establish provider, browser, model, target-laptop, or release readiness.

## Handoff

S0/S4 should independently review `host/agent/action-journal.mjs`, controller transition ordering, operator endpoint authorization/status mapping, and the crash-injection suite. Follow-on owners must add provider completion proof/reconciliation and the native Windows store before enabling those target paths.
## Action-journal audit repair packet (2026-09-05)

- **Role:** S3 Agent/Tools; repair the final audit findings on the isolated
  durable action-journal branch.
- **Branch/worktree:** `luna/durable-action-journal-core` /
  `wt-action-journal-core`; starting tip `86f767c`.
- **First claimed task:** fail closed on the unresolved POSIX ancestor-swap/
  handle-relative-store gap; add hardlink/link-count and same-size mutation
  defenses; expose truthful journal health/action availability; render failed
  completion/unverified states safely in the UI; and make completion evidence
  digest-only/provider-agnostic.
- **Dependencies:** existing journal/controller dispatch barrier, HostServer
  status API, static UI event renderer, and current provider status contracts.
- **Assumptions:** Node built-ins only; no native handle-relative store can be
  claimed from the current API surface. Production durable action dispatch will
  remain unavailable unless the POSIX core proves its directory identity cannot
  be displaced; test-only seams may remain for deterministic tests.
- **Uncertainties:** Windows readiness, provider completion/reconciliation, and
  real provider/browser/model behavior remain explicitly unproven.
- **State:** READY_FOR_REVIEW.

## Action-journal audit repair evidence (2026-09-05)

- Production `ActionJournal.open` now blocks with the typed
  `action_journal_handle_relative_unavailable` health error on POSIX (Windows
  remains `action_journal_platform_unavailable`); only explicit `testOnly`
  callers may exercise the pathname seam. This is a deliberate NOT READY
  result, not a provider or Windows readiness claim.
- Record init/append/prune paths require regular single-link files and retain
  descriptor identity/size checks. Focused probes cover hardlinks and a
  same-size replacement before append. The unresolved ancestor-swap risk is
  closed by refusing production dispatch, not by claiming pathname safety.
- Host status exposes `{ action_journal: { state, error,
  durable_action_dispatch } }`; tests cover ready, blocked, and absent states.
  The UI reports unavailable durable actions, and safely renders failed
  `tool.completed`/`action_completion_unverified` operation and state fields.
- Reconciliation results are explicitly controller-acknowledged/provider-
  unverified and carry only bounded digest evidence (operation, preview,
  arguments, resource-null, and response digests); no provider completion is
  claimed.
- Risk/effect classification now uses an explicit allowed-tier map: T4 is
  prohibited, T0 is non-action-only, and T2/T3 cannot downgrade to `none` or
  another read effect. HostServer derives its effective journal from the
  controller and reports split/missing bindings as not write-ready. Operator
  grants retain `operator_grant` authorization in durable receipts; a dispatch
  fsync fault is tested to execute zero provider calls.
- Focused checks: `node --check` for journal/controller/HostServer; `node
  --test tests/host/action-journal.test.mjs` (20 pass); `node --test
  tests/host/local-tools.test.mjs` (11 pass); selected external/tool-chain
  mocked tests (51 pass); `git diff --check` (pass). No heavy suite, model,
  provider, browser, native, account, or external-network execution.
- Remaining: native handle-relative journal implementation, Windows readiness,
  provider reconciliation/completion proof, and all real external behavior.

## Action-journal follow-up repair evidence (2026-09-05)

- Risk/effect combinations are enforced by an explicit allowed-tier map before
  dispatch: T4 is prohibited, T0 is non-action-only, T2/T3 cannot claim
  `none`/read effects, and missing/unknown fields fail classification.
- HostServer rejects distinct controller/host journal objects and derives the
  effective journal from the controller when supplied there. Status includes
  `bound_to_controller`; durable dispatch is false for missing or split state.
- Operator-grant authorization is preserved as `operator_grant` in the
  durable `authorized` receipt. A dispatch fsync fault produces zero provider
  executions; successful operator-grant dispatch is covered separately.
- Unverified evidence uses `preview_digest` (not `precondition_digest`) and
  remains digest-only/provider-agnostic.
- Additional focused checks: `node --test tests/security/permission-mode-
  adversarial.test.mjs` (16 pass); `node --test tests/security/tool-calling-
  adversarial.test.mjs tests/host/fixture-host.test.mjs` (43 pass, 1 existing
  TODO); action-journal targeted checks and syntax/diff checks remain green.

## Graph action reconciliation startup packet (2026-09-05)

- **Role:** S3 Agent/Tools; provider-specific Microsoft Graph action
  reconciliation.
- **Branch/worktree:** `luna/graph-action-reconciliation` /
  `wt-graph-action-reconciliation`; starting from `main` `65decba`.
- **Claimed scope:** remove generic `Idempotency-Key` and in-memory
  idempotent-success claims; bind provider calls to the controller's durable
  journal operation/digest metadata without exposing authority to the model;
  add conservative mocked reconciliation for Graph mail drafts, mark-read,
  draft sends, and Teams sends.
- **Dependencies:** current Graph provider, controller action-journal barrier,
  strict external-tool schemas, and test-only journal seams. Production
  durable action execution remains fail-closed until the handle-relative store
  is available.
- **Safety assumptions:** no credentials, live accounts, provider calls,
  browser/model/native builds, or broad/heavy suites. Provider evidence must be
  bounded and digest-only; ambiguous effects remain `reconciling`/manual.
- **Official references to record in implementation docs:**
  Microsoft Graph create message, send mail, update message, chat messages,
  and list messages documentation (all under `learn.microsoft.com/graph`).
- **State:** IN_PROGRESS.

## Graph action reconciliation evidence (2026-09-05)

- **Implementation commits:** `daeea60` (Graph provider/controller,
  transport header policy, contract/journal documentation, mocked tests) and
  `549904f` (ambiguous Teams timeout regression).
- The controller now passes a private `internal.journal_binding` containing
  only operation/digest metadata after the journal dispatch barrier. The model
  schema, model call, events, and provider result contain no authority token or
  message content beyond the existing bounded preview.
- Generic `Idempotency-Key` is removed from the Graph adapter and HTTPS header
  allowlist. In-memory success replay is removed; attempted duplicate writes
  are refused, while `mail.mark_read` is safely repeatable through GET desired-
  state verification.
- Draft creation sends a bounded `x-lae-operation` Internet header and can
  verify one exact `/me/mailFolders/drafts/messages` marker match after an
  ambiguous response. Draft send binds the complete bounded normalized draft
  digest plus ETag/change key; `202`/timeout requires draft disappearance and
  one new matching Sent Items proof. Teams send has no idempotency claim or
  automatic retry and accepts only a validated `201` resource or bounded
  unique chat proof. Unproven effects remain `reconciling`.
- Official references are recorded in
  `host/providers/MICROSOFT_GRAPH_RECONCILIATION.md`.
- **Focused evidence:**
  `node --check host/providers/microsoft-graph.mjs
  host/providers/microsoft-graph-auth.mjs host/agent/controller.mjs` — PASS;
  `node --test tests/host/external-tools.test.mjs` — 57 pass, 0 fail;
  `node --test tests/host/external-tools.test.mjs tests/host/action-journal.test.mjs`
  — 78 pass, 0 fail; `git diff --check` — PASS.
- No real credential/account/provider call, live network, browser/model/native
  build, or broad/heavy suite was used. Production journal/provider readiness
  remains gated by the fail-closed durable journal and live-account review.
- **State:** READY_FOR_REVIEW.
