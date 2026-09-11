# Status Packet

- **Session/role:** Luna Session 2 (model/artifact) — comparator evaluation and quantization-retention comparison.
- **Task:** `MODEL-COMPARATOR-EVAL-001`.
- **Branch/worktree:** `luna/j1m-comparator-eval-v1` / `wt-j1m-comparator-eval-v1`, based on exact `main@360519274b2be2f04a61297650a4b0586016201d`.
- **Dependencies:** MODEL_DECISION quality gate, `B-004`, `B-006`. **Reviewers:** S0, S4.
- **Objective:** give the ≥95% quality-retention criterion in `execution/ACCEPTANCE_CRITERIA.md` §11 a reachable reference by making the remote `--mode eval` lane able to evaluate and salvage the higher-precision comparators it already rebuilds — bounded, receipted, fail-closed, and with the default path byte-for-byte unchanged.

## Finding that reframes the problem

`governance/MODEL_DECISION.md` records that any future quality run "must budget a full Shadeform re-conversion". That is not so. `scripts/j1m_runner.py` `command_plan` already converts `Qwen3.5-9B-bf16.gguf` and `Qwen3.5-9B-Q8_0.gguf` in the same stage that produces the Q4, and `scripts/j1m_orchestrator.py` `_eval_remote_commands` invokes exactly that plan on every eval run. The comparator conversion is already paid for. What is missing is keeping them past the `rm -f` stage, evaluating them, and salvaging their receipts.

## Three blockers found while designing it, and what was done about each

1. **The product engine cannot load a comparator.** `native/model_validation/model_validator.hpp` compiles the Q4 filename, size, SHA-256 and model id into the binary; `native/main.cpp` states the identity "cannot be supplied by callers"; `model_validator.cpp` additionally refuses any tensor profile that is not F32/Q4_K/Q6_K. Both `serve` and `verify-model` go through it. **This is a correct gate and was not touched.**
2. **The evaluator is bound to the product engine's private API, so the pinned upstream `llama-server` cannot stand in.** `scripts/test/evaluate_tool_calls.py` `_post` first POSTs `/v1/sessions` and requires exactly `{"id","object","state_version"}` with `state_version == 1` (served by `native/server/http_server.cpp`), sends `session_id` and `mode: "normal"`, requires `choices[0].message` to have exactly `{"role","content"}` and `usage` exactly `{"prompt_tokens","completion_tokens"}`, and parses Qwen XML tool calls out of `content`. `llama-server` satisfies none of that, and with `--jinja` it consumes the XML into `tool_calls` while without it the `tools` field never reaches the model. `native/CMakeLists.txt` also pins `LLAMA_BUILD_SERVER OFF … FORCE`. **Consequence:** a comparator arm needs an engine that speaks the product API *and* loads non-Q4 weights. No such binary exists here, and building one is a `native/` slice. The comparator phase therefore ships **source-present and gated** behind `_COMPARATOR_ENGINE_AVAILABLE = False`, in the same idiom as `_EXTERNAL_SALVAGE_TRANSPORT_AVAILABLE` and `REMOTE_EXECUTION_ENABLED`. The follow-up slice is specified in `model/COMPARATOR_EVAL.md` §2.2a as `COMPARATOR-ENGINE-001`.
3. **The 2-point non-inferiority margin is only decidable with a paired interval.** Measured with the shipped constants, two *identical* arms give an unpaired stratified 95% interval of ±16.22 points at 37 cases and ±3.25 at 1,200; the paired interval on identical arms is exactly `[0, 0]`. Both methods are implemented, the receipt records which ran, and the per-arm schema makes the bounded `{id, category, passed}` vector the expected field. That vector belongs to the **new** `comparator-eval-receipt.v1` schema, never to `real-tool-eval-receipt.v1`, so the Q4 receipt and the evaluator's scoring semantics are untouched.

## Design

`model/COMPARATOR_EVAL.md`. Arms in fixed order `q4_k_m`, `q8_0`, `bf16` — the same-runtime Q4 arm is not optional, because retention is only meaningful with the runtime held constant, and it simultaneously gives §11 MUST 1 (runtime parity within 2 points of the pinned oracle) a reference it also lacks today. The product-engine Q4 receipt enters the comparison receipt as `product_engine_reference`, used only for the runtime-parity delta and never as the retention numerator. Identity anchor for the rebuilt comparators is the checked-in `artifacts/qwen35-9b/scan-receipt.json`, SHA-256 `0857899bf86527702743dd12b62ae5740cbb27fae2005da7066d1070cb341066`, pinned in source exactly like `_APPROVED_EVAL_MANIFEST_SHA256`; byte-exact reproduction from the pinned source revision is the acceptance condition. Only receipts are ever salvaged: the allowlist is name-based and refuses any entry ending in `.gguf`.

## What changed

- `scripts/j1m_runner.py` — `command_plan(..., retain_comparators=False)` plus `comparator_cleanup_plan()`. The final three stages (`rm -f` of the intermediates, `--post-cleanup`, `--manifest`) are **moved, never dropped**; a retaining caller must append the tail. `--retain-comparators` forwards it on the `--run` path.
- `scripts/j1m_orchestrator.py` — `--evaluate-comparators` (default `""`), a closed selection vocabulary, the arm set, `_comparator_budget`, `_comparator_clock_available`, `_comparator_phase`, `_eval_fetch_allowlist`, `_product_engine_reference`, `_write_comparison_receipt`, the `_COMPARATOR_ENGINE_AVAILABLE` gate and the `scan-receipt.json` trust anchor. The comparison receipt is produced inside the existing salvage block, before teardown, on every exit path.
- `scripts/test/compare_model_quality.py` (new) — all the mathematics and the `comparison-receipt.json` writer, stdlib only, offline, no network.
- `tests/performance/test_comparator_eval.py` (new, registered in `scripts/test/run_qa.py` `TEST_INVENTORY` as class `lifecycle`).
- `tests/performance/test_j1m_lifecycle.py` — the static `direct_execute_methods` census moves 12 → 13 for the new pre-spend regression, which carries the existing `@isolated_lifecycle_execute` decorator. The property that test guards (every direct `execute()` regression is isolated) is unchanged.

## Cost and time bound rationale

Per-comparator budget 720 s (the existing `stage_budgets_seconds.evaluation` 480 plus 240 s of engine start and model load); one-time setup budget 1500 s (configure 180 plus a CUDA build 1200 plus margin). Required clock and projected marginal metered cost at the pinned $1.35/hr: `q8` → 2 arms → **2940 s (49 min), $1.1025**; `q8,bf16` → 3 arms → **3660 s (61 min), $1.3725**.

That figure is **projected metered time, not new authority.** The phase runs strictly inside `execution_deadline`, which is `min(provider, run, watchdog)` derived from the already-approved `modes.eval` clocks, so `active_cost_usd` 2.619, `provider_backstop_cost_usd` 3.2738 and `budget_policy.project_total_usd` 50.0 are unchanged, and `raises_authorized_cost` is a recorded `false`.

`_eval_deadline_ceiling` leaves only **309 s** of static slack (`run_seconds` 6984 − `ceiling_seconds` 6675), so the comparator worst case does not fit it and **this slice does not raise any clock.** The resolution is that comparator stages are excluded from that static sum — so a comparator request can never make a run refuse to start — and the phase is instead gated at runtime, immediately after the Q4 eval and before the first comparator stage, on `execution_deadline − now − cleanup_reserve − _DELETION_RESERVE_SECONDS ≥ required`. Historically the four complete runs (`h`/`i`/`j`/`k`, $0.4997/$0.5264/$0.5152/$0.5221) finished in about 22–24 minutes of a 116-minute cap, so a real run reaches the gate with roughly 90 minutes left; when it does not, the phase is skipped with a typed reason and the Q4 result is unaffected. `_comparator_budget` records `fits_static_worst_case: false` so the reviewer sees this rather than having to derive it.

Fail-closed reasons are one closed set shared by both modules and asserted equal by test: `comparator_not_requested`, `comparator_engine_unavailable`, `comparator_clock_insufficient`, `comparator_stage_failed`, `comparator_receipt_missing`, `comparator_receipt_invalid`, `comparator_artifact_identity_mismatch`, `comparator_baseline_missing`.

## Exact evidence

Machine: macOS `Darwin 25.2.0`, arm64, CPython 3.13, repository worktree `wt-j1m-comparator-eval-v1`, cold process per command, no network. All commands from the worktree root.

| Command | Result |
|---|---|
| `python3 -m unittest tests.performance.test_j1m_lifecycle tests.performance.test_model_specs tests.model.test_tool_call_eval tests.performance.test_cost_ledger_genesis` | `Ran 170 tests` `OK` |
| `python3 -m unittest tests.performance.test_comparator_eval` | `Ran 31 tests` `OK` |
| `python3 scripts/test/run_qa.py --root . --skip-native --output -` | 65 discovered, **0 missing**, **0 unknown**, 71 records (1 PASS / 70 expected SKIP), overall `BLOCKED` (unchanged; 64/70 before this slice) |
| `git diff --check main...HEAD` | exit `0` |
| `git status --short` | empty |

Default-OFF equivalence is proved, not asserted: for all four modes the printed plan with the flag absent equals the plan with `--evaluate-comparators ""` (`created_at_utc` excluded); with `--evaluate-comparators q8` the plan gains exactly `comparator_phase` and `comparator_fetch_allowlist` and no existing key changes value; `_eval_remote_commands` is still 14 commands containing neither `comparator` nor `retain`; `_eval_uploads` is still 15 and `_eval_deadline_ceiling(config)["upload_count"]` is still 15; `_eval_fetch_allowlist(config, ())` equals `config["artifacts"]["eval_fetch_allowlist"]`; and `command_plan(c) == command_plan(c, retain_comparators=True) + comparator_cleanup_plan()` on the shipped 31-stage plan.

Commits on the branch, in order:

| Commit | Contents |
|---|---|
| `a913740` | `docs: claim comparator evaluation slice` |
| `91867c3` | `docs: design the comparator evaluation and comparison receipts` — `model/COMPARATOR_EVAL.md` |
| `cd3f706` | `model: defer intermediate deletion behind retain_comparators` — `scripts/j1m_runner.py` and the design correction for blocker 2 |
| `9140f80` | `qa: add the stdlib Q4-versus-comparator comparison mathematics` — **this commit also carries the `scripts/j1m_orchestrator.py` wiring**, which its message does not name; recorded here rather than rewritten, since this lane does not amend or rebase |
| `0648245` | `qa: test the comparison mathematics, flag equivalence and budget gate` — new suite plus its `run_qa.py` registration |
| `1a44248` | `qa: isolate the comparator pre-spend regression and update its census` |

## Limitations

- **No number was produced and none can be yet.** This is offline source and unit evidence on macOS. It is not model, provider, remote, CUDA, Windows, target, or release evidence, and it advances no gate. It does not by itself satisfy §11; it makes the reference reachable.
- With `_COMPARATOR_ENGINE_AVAILABLE` false, `--evaluate-comparators` is parsed, planned, budgeted and then **refused before any provider API call** — `execute()` raises before `sf.load_env`, `sf.list_candidates` and `sf.create_ephemeral_ssh_key`, all three asserted not-called. It cannot spend on a phase that cannot produce a number.
- `_salvage` still raises `external salvage transport is unavailable in this source slice`, so no receipt of any kind can be fetched. The comparison receipt degrades to `status: "skipped"` with a typed reason and never fabricates a value it did not read.
- The critical-category rule is applied to **every** category (`critical_category_policy: "all_categories_conservative"`) rather than inventing a mapping from `MODEL_DECISION`'s prose onto the fixture's six categories. Four of those categories hold a single case, so one flip is a 100-point category delta; per-category `case_count` is recorded beside every delta.
- Even paired, the 37-case profile is underpowered for a 2-point margin — one case is 2.70 points — so that verdict will read `fail` for any real difference until `MODEL-QUALITY-CORPUS-001` lands. `REMOTE_EXECUTION_ENABLED`, `SOL_J1M_REVIEWED`, the watchdog, host-shutdown backstop, teardown, cost settlement and every cap are untouched.

## Decisions needed from Sol

1. Fund `COMPARATOR-ENGINE-001` (`model/COMPARATOR_EVAL.md` §2.2a), and confirm that a separate non-product target with a receipt-anchored identity — relaxing nothing else, and leaving `lae-engine` byte-unchanged — is the acceptable shape.
2. Accept the opportunistic runtime clock gate, or authorize a `modes.eval` clock increase (`runtime_hours` 1.94 → ~2.8 with `host_shutdown_delay_minutes`, `external_watchdog_seconds` and `provider_backstop_hours` raised in step). That is a cap change and was deliberately not made here.
3. Record an explicit critical-category mapping, or keep the conservative all-categories policy.
4. Note that `governance/MODEL_DECISION.md` and `coordination/BLOCKERS.md` B-004 both state that a comparator run must budget a full re-conversion. Correcting that is Sol's edit, not mine.

- **State:** `READY_FOR_REVIEW`.
