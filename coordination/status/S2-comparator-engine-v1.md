# Status Packet

- **Session/role:** Luna Session 2 (model/artifact) — comparator engine hosting and the upstream tool-call transport.
- **Task:** `COMPARATOR-ENGINE-001`.
- **Branch/worktree:** `luna/comparator-engine-v1` / `wt-comparator-engine-v1`, based on exact `9a8519c` — the tip of `luna/j1m-comparator-eval-v1` (`MODEL-COMPARATOR-EVAL-001`), which was itself based on `main@360519274b2be2f04a61297650a4b0586016201d`. This branch **extends** that slice and does not fork it; it was not rebased.
- **Dependencies:** `MODEL-COMPARATOR-EVAL-001`, MODEL_DECISION quality gate, `B-004`, `B-006`. **Reviewers:** S0, S4.
- **Objective:** make the comparator arms runnable, so the ≥95% retention gate in `execution/ACCEPTANCE_CRITERIA.md` §11 becomes measurable — by hosting them on the pinned upstream `llama-server` and teaching the tool-call evaluator to speak to it, without weakening the product engine's compiled Q4 identity.

## The decision this slice had to make, and what it decided

`model/COMPARATOR_EVAL.md` §2.2a, as written on `9a8519c`, proposed a second CMake target `lae-engine-comparator`: the product sources rebuilt with a compile definition that swaps the compiled Q4 identity for a caller-supplied, receipt-anchored one.

**That is not what was built, and the substitution is the main thing for review to confirm.** Building it would have created a second engine binary whose only distinguishing property is a relaxed identity check — precisely the object `native/main.cpp` says must not exist ("model filename, size, SHA-256, GGUF metadata, and tensor profile are compiled product identity and cannot be supplied by callers"). The repository already names a better host for a comparator: `model/quality-eval/quality-fixture-spec.json` `comparison.runtime_oracle` is `pinned-upstream-same-artifact`, and `AGENTS.md` says to "treat the pinned upstream engine as an oracle".

So the arms are hosted on the pinned upstream `llama-server`, and **the evaluator learned to speak to it** instead of the product engine learning to load foreign weights. `native/model_validation/model_validator.hpp` is byte-unchanged, `native/CMakeLists.txt` still pins `LLAMA_BUILD_SERVER OFF … FORCE`, and no binary anywhere in this tree accepts a caller-supplied model identity.

A consequence worth recording: **`vendor/llama.cpp` cannot build the server.** It is a pruned runtime bundle — `vendor/README.md` states that tools are excluded, and there is no `tools/` directory in it, so `LLAMA_BUILD_SERVER=ON` has nothing to compile. The eval lane's own `git clone` of the same pinned revision (`3581ba0cf591b3f772fbb002de0f70e294bc0396`, already a stage of `command_plan`) is the source, and the revision is re-verified twice: once by `--verify-llama` immediately before configure, and again by the arm driver's own `git rev-parse HEAD` check before it launches anything.

## Transport design, and the equivalence argument

### How the tool catalog reaches the model

The product engine decides this question, so it was read rather than guessed. `native/backend/llama_backend.cpp:144` obtains the template from `llama_model_chat_template(...)` — GGUF metadata — and `native/backend/llama_chat_template.cpp` `PinnedChatTemplate::render` feeds that template `messages`, `tools` serialized as OpenAI `{type, function{name, description, parameters}}` objects, `add_generation_prompt: true`, and `enable_thinking`. `llama-server --jinja` does the same thing, with the same template, out of the same GGUF.

**Decision: send `tools` in the request and let the server's embedded template render the catalog.** Hand-rendering a tool block into the system prompt would have reimplemented the product engine's prompt-construction path and made the comparator arm a different evaluation — the exact failure mode `model/COMPARATOR_EVAL.md` §2.2 warned about.

### The one real difference, and how it is closed

The product engine does one thing the upstream server does not. `apply_schema_abstention_policy` (`native/backend/llama_chat_template.cpp:15-32`) prepends an **app-owned** system message whenever the request carries tools: merged into a leading system message when the history already has one, inserted as a new first message when it does not. The upstream server knows nothing about it.

`scripts/test/evaluate_tool_calls.py` therefore carries `SCHEMA_ABSTENTION_POLICY` and `apply_schema_abstention_policy` as a port of that function, and the upstream transport supplies the result in `messages`. Two further product behaviours are reproduced as request fields:

| Product engine | Upstream request field | Source |
|---|---|---|
| `mode: "normal"` → `enable_thinking = false` | `chat_template_kwargs: {"enable_thinking": false}` | `native/server/chat_request.cpp:272-275` |
| bare greedy sampler chain (`llama_sampler_init_greedy`) | `temperature: 0` | `native/backend/llama_backend.cpp:160-163` |
| a fresh session per request | `cache_prompt: false` | `_post` creates a new `/v1/sessions` per call |

`session_id` and `mode` are product-private fields that `native/server/chat_request.cpp:218` would reject as unknown from any other client; they are not sent upstream.

### How equivalence is shown

Not asserted — tested, in `tests/model/test_comparator_engine.py`:

1. **The policy text cannot drift from the engine.** `test_the_policy_message_is_the_engine_constant_verbatim` extracts the C++ string literal `kSchemaAbstentionPolicy` out of `native/backend/llama_chat_template.cpp` at test time and compares it to the Python constant. Editing either side alone fails the suite.
2. **The merge branches match the engine's branching.** Both paths (leading system message present / absent) are exercised, including that the caller's own list is never mutated.
3. **Every case of both shipped fixtures renders the policy exactly once**, with the case's own messages preserved verbatim after it, and the two fixtures together are asserted to exercise *both* branches. Worth recording precisely: the 37-case production profile is all-user-first, so the merge branch is covered by the default fixture's `injection-001`, not by the production profile.
4. **The upstream request body is pinned key-for-key** — exactly `{model, messages, tools, stream, max_tokens, temperature, cache_prompt, chat_template_kwargs}` — with `session_id` and `mode` proved absent, `temperature == 0`, `stream is False`, `cache_prompt is False`, `enable_thinking is False`, and `tools` equal to the fixture's list.
5. **The default transport is byte-identical.** `_post` is unchanged (`git diff 9a8519c -- scripts/test/evaluate_tool_calls.py` removes no line of it), and the request body built for a case is compared against a *literal copy of the pre-change construction* for every case of both shipped fixtures — the 8-case default and the 37-case production profile — at both the dict level and the `json.dumps` level. The canary body is compared the same way for both. The product transport's session handshake is additionally exercised end-to-end over a loopback `http.server` fixture, asserting the `/v1/sessions` → `/v1/chat/completions` order and that the completion body is the payload plus exactly `session_id`.

**What the tests cannot show offline** is the rendered *token* sequence, because that needs the GGUF. The proved claim is message-list and template-source equivalence; the residual is that `llama-server` applies the same template to the same messages, which is the oracle assumption the project already makes and which `AGENTS.md` codifies.

### Structured `tool_calls`

`--jinja` parses the pinned template's XML into structured `tool_calls` and removes it from `content`. Both shapes are handled and **scored by the same code**: `evaluate_case` was split into `parse_tool_call` + `evaluate_call`, and `normalize_structured_tool_calls` canonicalizes a structured call into exactly the `{name, arguments}` value `parse_tool_call` returns, under the same unknown-tool check, the same 4096-byte argument bound, and the same `_validate_arguments` schema check. A malformed structured call is a **quality failure with the same code** a malformed XML call gets, never a transport error, so the two shapes cannot score differently. `test_structured_and_xml_answers_score_identically` proves it on a real production case.

## Server build and launch

`j1m_runner.comparator_server_plan()` emits three argv arrays, in its own build tree (`/scratch/llama-server-build`), separate from both the CPU conversion build and the product engine build:

```
python3 /scratch/j1m/j1m_runner.py --config /scratch/j1m/j1m-config.json \
    --verify-llama /scratch/llama.cpp 3581ba0cf591b3f772fbb002de0f70e294bc0396
cmake -S /scratch/llama.cpp -B /scratch/llama-server-build \
    -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=80 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
    -DLLAMA_BUILD_COMMON=ON -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_APP=OFF \
    -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_OPENSSL=OFF
cmake --build /scratch/llama-server-build --target llama-server --parallel 8
```

`CMAKE_BUILD_TYPE`, `CMAKE_CUDA_ARCHITECTURES` and `CMAKE_CUDA_COMPILER` are asserted by test to be the **same three values** the product engine's CUDA eval configure uses — an oracle is only an oracle when the runtime differs in the weights and nothing else. `LLAMA_USE_PREBUILT_UI=OFF` matters: it is `ON` by default and downloads a prebuilt UI from an HF bucket. `LLAMA_OPENSSL=OFF` leaves the binary with no HTTPS client. `LLAMA_CURL` is *not* passed: at this revision `CMakeLists.txt:179` marks it deprecated.

The full flag list is recorded verbatim in every arm receipt (`host.server_build_flags`), so a published number is auditable from the receipt alone.

Launch argv, asserted exactly by `test_launch_argv_is_exact_loopback_only_and_carries_no_bearer`:

```
/scratch/llama-server-build/bin/llama-server
  --model /scratch/j1m/artifacts/Qwen3.5-9B-<ARM>.gguf
  --host 127.0.0.1 --port <18081+index>
  --api-key-file /scratch/j1m/comparator-token-<arm>
  --ctx-size 8192 --n-gpu-layers 99 --parallel 1 --threads 1
  --jinja --temp 0 --no-webui
```

**`--api-key-file`, not `--api-key`.** The brief said "`--api-key` from a generated 0600 file"; a bearer passed as a command-line argument is readable by every process on the host, which `AGENTS.md` forbids and which `remote_model_eval.py` already refuses in a comment of its own. `--api-key-file` satisfies the intent (a random per-launch bearer, from a 0600 file) without that exposure. The token file is minted per arm with `O_EXCL | 0o600`, verified `0o600` after creation, and never appears in any argv, log, progress record or receipt.

Readiness is a bounded poll of `GET /health` with the bearer in the header until `{"status": "ok"}` or the stage deadline; a never-ready server, a crashed server and a closed port all become `comparator_server_ready_timeout`. Teardown is SIGTERM → 15 s wait → SIGKILL in a `finally`, so no server is orphaned on any path.

## Vocabulary

| `--evaluate-comparators` | Arms | Comparisons | What it answers |
|---|---|---|---|
| `""` (default) | none | none | nothing; refused with `comparator_not_requested` |
| `q4-oracle` | `q4_k_m` | none | §11 MUST 1 — "within 2 aggregate points of the pinned upstream same-artifact Q4 oracle", as `runtime_parity_delta_points` |
| `q8` | `q4_k_m`, `q8_0` | 1 | §11 MUST 2 retention, plus MUST 1 |
| `q8,bf16` | `q4_k_m`, `q8_0`, `bf16` | 2 | both denominators, plus MUST 1 |

`q4-oracle` is new. Because the comparator host is now the pinned upstream same-artifact build, the always-present `q4_k_m` arm *is* the oracle §11 MUST 1 names, and running it alone is a cheap, complete measurement of a MUST that has no reachable reference today. It is represented as a selection whose only arm is the baseline; `_comparator_comparators()` filters the baseline out of the comparison receipt's `requested`, so the receipt carries a `baseline`, an empty `comparisons`, and the parity delta — and never a retention ratio it cannot compute.

## Cost and time bound per arm

Unchanged per-arm budget: **720 s** (the existing `stage_budgets_seconds.evaluation` 480 plus 240 s of server start and model load). One-time setup budget **1500 s**, now split explicitly as configure **300** + CUDA build **1200**, asserted by test to sum to `_COMPARATOR_SETUP_BUDGET_SECONDS`.

| Request | Arms | Required | Projected marginal cost @ $1.35/hr |
|---|---:|---:|---:|
| `q4-oracle` | 1 | 1500 + 1×720 = **2220 s** (37 min) | **$0.8325** |
| `q8` | 2 | 1500 + 2×720 = **2940 s** (49 min) | **$1.1025** |
| `q8,bf16` | 3 | 1500 + 3×720 = **3660 s** (61 min) | **$1.3725** |

**Projected metered time, not new authority.** No cap is raised: `active_cost_usd` 2.619, `provider_backstop_cost_usd` 3.2738, `budget_policy.project_total_usd` 50.0, `runtime_hours` 1.94, `external_watchdog_seconds` 7700, `host_shutdown_delay_minutes` 110 and `provider_backstop_hours` 2.425 are all byte-unchanged in `model/conversion/j1m-config.json`. `_comparator_budget` still records `raises_authorized_cost: false` and `fits_static_worst_case: false`.

Comparator stage budgets live in their own `_comparator_stage_timeout`, deliberately separate from `_eval_stage_timeout`, so `_eval_deadline_ceiling` returns exactly the numbers it returned before: `ceiling_seconds` 6675.0, `run_seconds` 6984.0, **static slack 309.0 s**, `upload_count` 15. A comparator request therefore still cannot make a run refuse to start.

The one budget figure that is an estimate rather than a measurement is the 1200 s server build; a first real run should record the actual value.

## Review findings closed

The S0/S4 review of `MODEL-COMPARATOR-EVAL-001` returned ACCEPT_FOR_MERGE with four MINORs that become live once `_COMPARATOR_ENGINE_AVAILABLE` flips. All four are fixed here, each with a test.

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | the 8-point critical-drop threshold is not pinned by any test; replacing the gate lookup with any constant up to 100 stays green | boundary test on a synthetic 100-case category, where one case is worth exactly one point | `test_the_critical_drop_threshold_is_exactly_the_fixture_spec_value` — 8.0 → `pass`, 9.0 → `fail`, limit read from the gate, never written into the test. Mutation reproduced: replacing `gate["critical_category_max_drop_points"]` with `20.0` now gives `FAILED (failures=1)`; before this it gave `Ran 31 tests OK` |
| 2 | the runtime clock gate is unreachable — `execution_deadline` is never passed | `_comparator_phase` is called again with the live deadline immediately after `lifecycle["job"]` is fixed and before the first comparator stage | `test_budget_check_refuses_when_the_remaining_clock_is_short` (now unconditional, no `_COMPARATOR_ENGINE_AVAILABLE` patch) — short clock → `refused` / `comparator_clock_insufficient`; ample clock → `approved` |
| 3 | `lifecycle["comparator_cleanup_error"]` has no producer and `--retain-comparators` has no caller | both wired: a selection adds `--retain-comparators` to the runner stage, and `_comparator_cleanup_commands` runs the deferred `rm -f` / `--post-cleanup` / `--manifest` tail in a `finally` on every path out of the phase — refusal, stage failure or success. No clock budget or a non-completing stage sets the typed error the existing fail-closed branch reads | `test_the_flag_adds_exactly_the_retention_switch_and_two_uploads`, `test_the_deferred_cleanup_tail_is_the_runner_tail_with_remote_paths`, and the preserved `command_plan(c) == command_plan(c, retain=True) + tail` invariant |
| 4 | a present-but-invalid arm receipt is coarsened to `comparator_receipt_missing` | `_write_comparison_receipt` checks existence first and records `comparator_receipt_invalid` for a receipt that arrived and failed verification, including when it is the baseline that failed | `test_a_present_but_invalid_arm_receipt_keeps_its_distinct_reason` — `q8_0` present-but-invalid → `comparator_receipt_invalid`; `bf16` absent → `comparator_receipt_missing`, in one receipt |

Both NITs are also closed: the comment at `execute()`'s head now says what is actually refused where (only the vocabulary parse precedes the deletion preflight and `load_config`; both are local and make no provider call), and the comparison-receipt handler catches `ComparisonError | ValueError | OSError` and keeps the typed code instead of collapsing everything to `comparison_failed`.

## Exact evidence

Machine: macOS `Darwin 25.2.0`, arm64, CPython 3.14.6, worktree `wt-comparator-engine-v1`, cold process per command, **no network** beyond a `http.server` fixture bound to `127.0.0.1:0` inside the test process. No provider, model, CUDA, remote, Windows, target or release evidence exists or is claimed.

| Command | Result |
|---|---|
| `python3 -m unittest tests.performance.test_j1m_lifecycle tests.performance.test_model_specs tests.model.test_tool_call_eval tests.performance.test_cost_ledger_genesis` | `Ran 170 tests` `OK` |
| `python3 -m unittest tests.performance.test_comparator_eval` | `Ran 32 tests` `OK` (31 before; +1 is the critical-drop boundary) |
| `python3 -m unittest tests.model.test_comparator_engine` | `Ran 48 tests` `OK` (new suite) |
| all six modules in one invocation | `Ran 250 tests` `OK` |
| `python3 scripts/test/run_qa.py --root . --skip-native --output -` | 66 discovered, **0 missing**, **0 unknown**, 72 records (1 PASS / 71 expected SKIP), overall `BLOCKED` (unchanged; 65/71 before this slice) |
| `git diff --check 9a8519c...HEAD` | exit `0` |
| `git status --short` | empty |

Plan-shape numbers, all asserted by test:

| Quantity | Without the flag | With `q8` |
|---|---:|---:|
| `_eval_remote_commands` stages | 14 | 14 (one stage gains `--retain-comparators`; no stage added or removed) |
| `_eval_uploads` entries | 15 | 17 (`remote_comparator_eval.py`, `comparator-anchor-scan-receipt.json`) |
| `_eval_deadline_ceiling["upload_count"]` | 15.0 | 15.0 (unchanged by design, §2.4) |
| `_comparator_remote_commands` stages | 0 | 5 (verify + configure + build + 2 arms) |
| deferred cleanup tail | 0 | 3 |
| `command_plan` stages | 31 | 28 + 3 moved |

Default-OFF equivalence is proved for **all three** selections, not just one: for all four modes the printed plan with the flag absent equals the plan with `--evaluate-comparators ""`; with `q4-oracle`, `q8` and `q8,bf16` the plan gains exactly `comparator_phase`, `comparator_fetch_allowlist`, `comparator_commands` and `comparator_cleanup_commands`, and no existing key changes value.

Security properties asserted by test: no `.gguf` in any salvage allowlist or upload; no bearer in any argv; every server bind is `127.0.0.1`; a non-loopback, non-`http`, or wrong-path endpoint is refused before the token is used; a full run's aggregate, indicator vector and stdout contain neither the bearer nor any prompt or response text.

## Commits

| Commit | Contents |
|---|---|
| `5aaa440` | `model: add the upstream llama-server transport to the tool-call evaluator` |
| `7152d62` | `model: build and drive the pinned upstream llama-server for one arm` |
| `fa74b88` | `model: run the comparator phase and close the reviewed gate findings` |
| `8653caa` | `qa: test the transport, the arm driver and the critical-drop boundary` |
| `b873ddd` | `docs: record COMPARATOR-ENGINE-001 as implemented and why its shape changed` |

## What this does not do

- It does not run anything. No provider call, no model, no network, no credential, no new dependency, no cap raised, no gate advanced.
- It does not change the product engine, its compiled identity, its CMake, or the Q4 evaluation path. The evaluator gains a second transport; its default transport, its scoring semantics and its Q4 aggregate contract are unchanged.
- It does not produce a quality number. `_salvage` still raises `external salvage transport is unavailable in this source slice`, so no receipt of any kind can be fetched from a real run yet; the comparator phase degrades to a typed skip until that transport lands. **That is now the single remaining blocker between the implemented phase and a measured retention figure.**
- It does not author the quality corpus. The 37-case profile remains underpowered for the 2-point margin (one flipped case is 2.70 points), which is `MODEL-QUALITY-CORPUS-001`.

## For Sol

1. **Confirm the substitution.** §2.2a originally proposed `lae-engine-comparator`; this slice hosts the arms on the pinned upstream `llama-server` instead and leaves `model_validator.hpp` untouched. That is the one design decision that differs from the approved sketch.
2. **Confirm `--api-key-file` in place of `--api-key`.** Same property, no bearer in argv.
3. **Two upstream flags are unverified against a real binary:** `--no-webui`, and `chat_template_kwargs.enable_thinking` as the mechanism for non-thinking mode. Both are read from the pinned revision's argument parser and API. An unknown flag refuses the launch into a typed skip rather than producing a wrong number, so the failure is fail-closed either way — but it would cost one arm's budget on a real run.
4. **The 1200 s server build budget is an estimate**, derived from the engine build's 750 s plus margin for a second CUDA tree. A first run should record the actual figure.
