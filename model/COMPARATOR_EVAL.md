# Comparator evaluation design — MODEL-COMPARATOR-EVAL-001 / COMPARATOR-ENGINE-001

Status: source design for two S2 slices, both implemented.
`MODEL-COMPARATOR-EVAL-001` shipped the arithmetic, receipts, budget gate and
flag plumbing with the phase gated off; `COMPARATOR-ENGINE-001` (§2.2a)
supplied the host, the transport and the arm driver and ungated it. Nothing in
this document authorizes spend, a provider call, a model download, or a gate
flip. No cap in `model/conversion/j1m-config.json` is raised by either slice,
and the flag remains default OFF.

## 1. Problem

`execution/ACCEPTANCE_CRITERIA.md` §11 requires that the controlled Q4_K_M
artifact "retains ≥95% of the same-source Q8/higher-precision aggregate" and
that "no critical category drops >8 absolute points".
`model/quality-eval/quality-fixture-spec.json` `comparison` fixes the method:
`quality_comparator: same-source-q8-or-higher`, `bootstrap_resamples: 10000`,
`confidence: 0.95`, `non_inferiority_margin_points: 2`,
`critical_category_max_drop_points: 8`.

`governance/MODEL_DECISION.md` records that this criterion "currently has no
reachable reference artifact": `Qwen3.5-9B-bf16.gguf` and
`Qwen3.5-9B-Q8_0.gguf` survive only as hashes in
`artifacts/qwen35-9b/scan-receipt.json`, and
`artifacts/qwen35-9b/post-cleanup-receipt.json` records the Q4 as the only
remaining GGUF.

That record understates what the lane already does. The remote `--mode eval`
lane **rebuilds both comparators on every run**:
`scripts/j1m_runner.py` `command_plan` emits

```
convert_hf_to_gguf.py … --outfile …/Qwen3.5-9B-bf16.gguf  --outtype bf16 --no-mtp
convert_hf_to_gguf.py … --outfile …/Qwen3.5-9B-Q8_0.gguf  --outtype q8_0 --no-mtp
llama-quantize            …/Qwen3.5-9B-bf16.gguf  …/Qwen3.5-9B-Q4_K_M.gguf Q4_K_M
```

and `scripts/j1m_orchestrator.py` `_eval_remote_commands` invokes exactly that
plan (`j1m_runner.py --run`) as an eval stage. The conversion cost of the
comparators is therefore **already paid on every eval run**. What is missing is
only (a) keeping them alive past the `rm -f` stage, (b) evaluating them, and
(c) salvaging their receipts. This slice adds those three things behind a
default-OFF flag.

## 2. Four constraints that shape the design

### 2.1 The product engine physically cannot load a comparator

`native/model_validation/model_validator.hpp` compiles the product identity in:

```
kProductModelFilename = "Qwen3.5-9B-Q4_K_M.gguf"
kProductModelSizeBytes = 5629109088
kProductModelSha256    = "c654bc40…68873b"
kProductModelId        = "qwen35-9b-q4-k-m"
```

`native/main.cpp` states it in the usage text: "model filename, size, SHA-256,
GGUF metadata, and tensor profile are compiled product identity and cannot be
supplied by callers". Both `verify-model` and `serve` go through
`validate_product_model_file`, and `model_validator.cpp` additionally refuses
any tensor profile that is not F32/Q4_K/Q6_K
(`model_quantization_profile_mismatch`, `model_tensor_profile_mismatch`).

**This is a correct gate and this slice does not touch it.** The consequence is
that a comparator can only be served by a different host process.

### 2.2 The evaluator is bound to the product engine's private API

The obvious alternative host is the pinned upstream `llama-server` — the
project already names it as the reference runtime
(`model/quality-eval/quality-fixture-spec.json`
`comparison.runtime_oracle = "pinned-upstream-same-artifact"`). It cannot be
used without changing the evaluator, and the evaluator must not change.

`scripts/test/evaluate_tool_calls.py` `_post` speaks the product engine's own
HTTP contract, not an OpenAI-compatible one:

- it first POSTs `/v1/sessions` and requires a body of exactly
  `{"id", "object", "state_version"}` with `object == "session"` and
  `state_version == 1` — served by `native/server/http_server.cpp`, and absent
  from `llama-server`;
- it sends `"session_id"` and `"mode": "normal"` in the completion payload;
- it requires `choices[0].message` to have **exactly** the keys
  `{"role", "content"}` — `llama-server` adds `reasoning_content` and
  `tool_calls`;
- it requires `usage` to have **exactly** `{"prompt_tokens",
  "completion_tokens"}` — `llama-server` also returns `total_tokens`;
- `evaluate_case` parses Qwen XML tool calls out of `content`, which is what
  the product engine returns. With `--jinja`, `llama-server` parses that XML
  into structured `tool_calls` and removes it from `content`; without
  `--jinja` the `tools` field never reaches the model at all.

A translating shim in front of `llama-server` would have to re-render the Qwen
chat template itself to get the tool catalog into the prompt, which is exactly
the product engine's prompt-construction path. Reimplementing it would make the
comparator arm a different evaluation, not the same one.

`native/CMakeLists.txt` also pins `LLAMA_BUILD_SERVER OFF … FORCE`, so the
product build tree cannot emit `llama-server`; §2.2a builds it in a separate
tree from the same pinned revision instead, and the product tree is unchanged.

**Conclusion as of `MODEL-COMPARATOR-EVAL-001`.** Evaluating a comparator with
the same fixture, the same evaluator and the same settings needed a host that
can load non-Q4 weights, and the evaluator could not talk to the only such
host available. That slice therefore shipped the comparator phase
**source-present and gated**, with `_COMPARATOR_ENGINE_AVAILABLE = False`,
refusing with the typed reason `comparator_engine_unavailable` before any
provider API call.

**Resolved by `COMPARATOR-ENGINE-001` (§2.2a).** The resolution was not to
give the product engine a relaxed identity but to teach the evaluator a
second transport, so the pinned upstream `llama-server` — the runtime oracle
the fixture spec already names — can host the arms. The two blockers above
are closed as follows:

- *bound to `/v1/sessions`*: the upstream transport does no session
  handshake and checks the response as a superset, so `reasoning_content`,
  `tool_calls` and `total_tokens` are accepted;
- *`--jinja` returns structured `tool_calls`*: both shapes are normalized
  into one canonical call and scored by the same code (§2.2a.2).

`_COMPARATOR_ENGINE_AVAILABLE` is now `True`. The typed pre-spend refusal
remains in the source and is still tested, because a build that ever turns
the constant off must still refuse before spending.

### 2.2a `COMPARATOR-ENGINE-001` — **implemented**

Status: implemented on `luna/comparator-engine-v1`. What §2.2 recorded as the
blocking follow-up now exists, and `_COMPARATOR_ENGINE_AVAILABLE` is `True`.
The flag itself is unchanged and still default OFF
(`--evaluate-comparators ""`).

**The shape actually built is not the one sketched below in the original
draft, and the difference is deliberate.** The draft proposed a second
non-product CMake target (`lae-engine-comparator`) built from the product
sources with a caller-supplied identity. That was rejected on review of §2.2
itself: it would have created a *second engine* whose only distinguishing
property is a relaxed identity check, and the repository already names a
better host. `model/quality-eval/quality-fixture-spec.json`
`comparison.runtime_oracle` is `pinned-upstream-same-artifact`, and `AGENTS.md`
says to "treat the pinned upstream engine as an oracle". So the arms are
hosted on the pinned upstream `llama-server`, and the evaluator learned to
speak to it, rather than the product engine learning to load foreign weights.
`native/model_validation/model_validator.hpp` is untouched, and no second
binary with a weakened identity exists anywhere in the tree.

Three pieces:

1. **Transport** — `scripts/test/evaluate_tool_calls.py` gains
   `--transport {product-engine,upstream-openai}`, default `product-engine`.
   The default path is byte-identical: `_post` is unchanged and the request
   body built for a case is the same literal dict, now produced by
   `_case_payload`. The upstream transport POSTs the OpenAI-compatible
   `/v1/chat/completions` on loopback with `Authorization: Bearer` from the
   same 0600 token-file discipline, and no session handshake (the upstream
   server is stateless per request).

2. **Host** — `j1m_runner.comparator_server_plan()` builds `llama-server`
   from the already-cloned pinned revision
   (`3581ba0cf591b3f772fbb002de0f70e294bc0396`) in its own build tree, with
   the *same* compiler identity as the product CUDA eval build: `Release`,
   the same `CMAKE_CUDA_ARCHITECTURES`, the same `CMAKE_CUDA_COMPILER`. An
   oracle is only an oracle when the runtime differs in the weights and
   nothing else. `LLAMA_BUILD_TESTS`, `LLAMA_BUILD_EXAMPLES`,
   `LLAMA_BUILD_APP`, `LLAMA_BUILD_UI`, `LLAMA_USE_PREBUILT_UI` and
   `LLAMA_OPENSSL` are all `OFF`, so the build downloads nothing and the
   binary carries no HTTPS client. Note that `vendor/llama.cpp` is a pruned
   runtime bundle with no `tools/` directory, so the server cannot be built
   from the vendored tree; the eval lane's own `git clone` of the same pinned
   revision is the source, and the revision is re-verified immediately before
   configure and again by the arm driver.

3. **Driver** — `scripts/test/remote_comparator_eval.py`, mirroring
   `remote_model_eval.py`: re-hash the rebuilt artifact against the anchored
   scan receipt (§4) and refuse on any drift, re-check the checkout revision,
   read the bounded fixture contract, mint a fresh 0600 bearer, launch the
   server on `127.0.0.1` with `--api-key-file`, poll `/health`, run the
   unmodified evaluator with the upstream transport, then `SIGTERM`, bounded
   wait, `SIGKILL`. It emits `comparator-receipt-<arm>.json` in the shape
   fixed by §6.1.

#### 2.2a.1 The template decision, and why the prompts are equivalent

The question §2.2 left open was how the tool catalog reaches the model. Two
candidates: send `tools` and let the server's `--jinja` render it from the
model's own embedded template, or render a tool block into the system prompt
by hand. The product engine's own code decides it.

`native/backend/llama_backend.cpp` reads the template from
`llama_model_chat_template(...)` — that is, from GGUF metadata — and
`native/backend/llama_chat_template.cpp` `PinnedChatTemplate::render` feeds
the template `messages`, `tools` (as OpenAI `{type, function{name,
description, parameters}}` objects), `add_generation_prompt: true` and
`enable_thinking`. `llama-server --jinja` does exactly the same thing with
exactly the same template out of exactly the same GGUF. So the catalog is
sent as `tools` and rendered by the model's own template: hand-rendering it
would make the comparator arm a *different* evaluation, which is the failure
mode §2.2 warned about.

The product engine does one thing more, and that one thing is the whole of
the difference. `apply_schema_abstention_policy` prepends an **app-owned**
system message (`kSchemaAbstentionPolicy`) whenever the request carries
tools — merged into a leading system message if the history already has one,
inserted as a new first message if not. The upstream server knows nothing
about it. The upstream transport therefore reproduces that function in
Python and supplies the result in `messages`, so the two hosts render the
same message list through the same template.

Two further product behaviours are reproduced as request fields:

| Product engine | Upstream request | Source |
|---|---|---|
| `mode: "normal"` → `enable_thinking = false` | `chat_template_kwargs: {"enable_thinking": false}` | `native/server/chat_request.cpp:275` |
| bare greedy sampler chain | `temperature: 0` | `native/backend/llama_backend.cpp:163` |

`cache_prompt: false` keeps every request a cold prefill, matching the per-arm
`cold_process_fresh_server_per_arm` cache state. `session_id` and `mode` are
product-private fields and are not sent.

**How equivalence is shown.** Not by asserting it — by test
(`tests/model/test_comparator_engine.py`):

- the Python policy constant is extracted from the C++ literal in
  `native/backend/llama_chat_template.cpp` at test time and compared, so the
  two cannot drift;
- both branches of the merge (with and without a leading system message) are
  exercised against real production cases, and the 37-case profile is
  asserted to contain both;
- every production case is checked to render the policy exactly once, with
  the case's own messages preserved verbatim after it;
- the upstream request body is pinned key-for-key, and `session_id`/`mode`
  are proved absent;
- the default product body is compared against a literal copy of the
  pre-change construction, for all 37 cases, at both the dict and the
  serialized-bytes level.

What the test cannot show offline is the rendered *token* sequence, because
that needs the GGUF. The claim proved here is message-list and
template-source equivalence; the residual is that `llama-server` applies the
same template to the same messages, which is a property of the pinned
upstream build and is exactly the oracle assumption the project already makes.

#### 2.2a.2 Structured `tool_calls`

§2.2 recorded that `--jinja` parses the pinned template's XML into structured
`tool_calls` and removes it from `content`. Both shapes are handled. When
`content` carries XML it goes through the existing `parse_tool_call`
unchanged; when the server returns structured calls,
`normalize_structured_tool_calls` canonicalizes them into exactly the
`{name, arguments}` value `parse_tool_call` returns, under the same
unknown-tool, bound and schema checks, and `evaluate_call` — the scoring core
both paths share — decides the case. A malformed structured call is a
*quality failure* with the same code a malformed XML call gets, never a
transport error, so the two shapes cannot score differently.

#### 2.2a.3 Arms

| Arm | Weights | Role |
|---|---|---|
| `q4_k_m` | `Qwen3.5-9B-Q4_K_M.gguf` | oracle baseline; retention numerator; and, on its own, §11 MUST 1 |
| `q8_0` | `Qwen3.5-9B-Q8_0.gguf` | retention denominator (`same-source-q8-or-higher`) |
| `bf16` | `Qwen3.5-9B-bf16.gguf` | higher-precision denominator; only when explicitly requested |

Running `q4_k_m` is not optional: retention is only meaningful when the
runtime is held constant. Because the host is now the pinned upstream
same-artifact build, that arm *is* the oracle §11 MUST 1 names, which is why
the selection vocabulary gained `q4-oracle`:

| `--evaluate-comparators` | Arms | Comparisons | Required | Marginal cost |
|---|---|---|---:|---:|
| `""` (default) | none | none | 0 | $0 |
| `q4-oracle` | `q4_k_m` | none | 1500 + 720 = **2220 s** | **$0.83** |
| `q8` | `q4_k_m`, `q8_0` | 1 | 1500 + 2×720 = **2940 s** | **$1.10** |
| `q8,bf16` | `q4_k_m`, `q8_0`, `bf16` | 2 | 1500 + 3×720 = **3660 s** | **$1.37** |

`q4-oracle` produces a comparison receipt with a `baseline`, an empty
`comparisons`, and `runtime_parity_delta_points` — the product engine's Q4
score minus the upstream oracle's Q4 score on the same artifact and the same
fixture. That number, and only that number, is what §11 MUST 1 asks for. The
product-engine Q4 receipt (`eval-receipt.json`) is untouched and enters the
comparison receipt as `product_engine_reference`.

### 2.3 The comparators are deleted before any evaluation

`command_plan` ends with `rm -f …bf16.gguf …Q8_0.gguf`, then `--post-cleanup`,
then `--manifest`. Those three stages run inside the `j1m_runner --run` eval
stage, long before `remote_model_eval.py`.

`command_plan` gains a keyword-only `retain_comparators: bool = False`. When
true, the final three stages are **moved**, not removed, into
`comparator_cleanup_plan()`, which the orchestrator appends as the **last**
eval stages once the phase is ungated. Cleanup, `post-cleanup-receipt.json`
and `manifest.json` still happen on the normal path; they simply happen after
the comparator phase. If the deferred tail does not complete, the run is
marked failed through the existing `receipt_error` path — intermediate
deletion being unproven is a fail-closed condition, and teardown still runs
unconditionally in the same `finally` block.

Invariant, pinned by test:

```
command_plan(c) == command_plan(c, retain_comparators=True) + comparator_cleanup_plan(c)
```

so the shipped plan is byte-identical when the flag is absent.

Disk: `resources.expected_peak_gib = 58` already describes the pre-cleanup
peak, when bf16 (17.92 GB) + Q8_0 (9.53 GB) + Q4 (5.63 GB) + the HF source are
all resident simultaneously. Holding that peak longer does not raise it.
`required_scratch_gib = 70` is unchanged.

### 2.4 The eval clock has 309 seconds of static slack

`_eval_deadline_ceiling` sums worst-case stage budgets:

```
activation 600 + host_key 165 + fixed_setup 540 + bootstrap 420
+ small_uploads 420 + post_upload 4050              = 6195 s work
+ cleanup_reserve 480                               = 6675 s ceiling
run_seconds = 1.94 h                                = 6984 s
static slack                                        =  309 s
```

A comparator phase cannot fit that worst case, and this slice **does not raise
`runtime_hours`, `provider_backstop_hours`, `external_watchdog_seconds`,
`host_shutdown_delay_minutes`, `active_cost_usd`, or
`budget_policy.project_total_usd`.**

The resolution is that the comparator phase is *opportunistic and
runtime-bounded*, not statically budgeted:

- Comparator stages are **excluded** from `_eval_deadline_ceiling`'s sum, so
  that function returns the same numbers it does today and a comparator request
  can never make a run refuse to start.
- Immediately after the Q4 eval stage completes, and **before the first
  comparator stage**, the orchestrator computes
  `available = execution_deadline − now − cleanup_reserve − _DELETION_RESERVE_SECONDS`
  and refuses the whole phase if `available < required`.
- Every comparator stage is additionally wrapped in
  `_eval_timeout(execution_deadline, budget)`, which already refuses to hand
  out a timeout that would cross the provider clock.

Empirically the four complete historical runs (`h`/`i`/`j`/`k`, settled
$0.4997/$0.5264/$0.5152/$0.5221 at $1.35/hr) finished in about 22–24 minutes of
a 116-minute cap, so a real run reaches the comparator gate with roughly 90
minutes of authorized clock left. When it does not, the phase is skipped with a
typed reason and the Q4 result is unaffected.

## 3. Budget and cost

Defaults, expressed in minutes in the CLI and stored in seconds in the receipt:

| Bound | Default | Basis |
|---|---:|---|
| `comparator_setup_budget_seconds` | 1500 | one-time upstream `llama-server` configure (300) + CUDA build (1200) |
| `comparator_budget_seconds` (per arm) | 720 | the existing `stage_budgets_seconds.evaluation` 480 + 240 s engine start/model load |

Required clock and projected marginal metered cost at $1.35/hr:

| Request | Arms | Required | Marginal cost |
|---|---:|---:|---:|
| `q4-oracle` | `q4_k_m` | 1500 + 1×720 = **2220 s** (37 min) | **$0.83** |
| `q8` | `q4_k_m`, `q8_0` | 1500 + 2×720 = **2940 s** (49 min) | **$1.10** |
| `q8,bf16` | `q4_k_m`, `q8_0`, `bf16` | 1500 + 3×720 = **3660 s** (61 min) | **$1.37** |

The marginal cost is **projected metered time, not new authority.** The phase
runs strictly inside `execution_deadline`, which is derived from the already
approved `modes.eval` clocks, so `active_cost_usd` (2.619),
`provider_backstop_cost_usd` (3.2738) and `budget_policy.project_total_usd`
(50.0) are all unchanged. The figure is recorded so a reviewer can see what
share of the authorized window the phase consumes.

Fail-closed reasons, all typed and all recorded in `comparison-receipt.json`:

| Reason | Meaning |
|---|---|
| `comparator_not_requested` | flag absent (the default) |
| `comparator_engine_unavailable` | `_COMPARATOR_ENGINE_AVAILABLE` is false; refused before any provider call (§2.2). Retained and tested although the constant is now `True` |
| `comparator_clock_insufficient` | `available < required` at the pre-phase check |
| `comparator_stage_failed` | a comparator build or arm evaluation stage did not complete |
| `comparator_receipt_missing` | the arm receipt was not salvaged before teardown |
| `comparator_receipt_invalid` | the arm receipt failed schema/identity verification |
| `comparator_artifact_identity_mismatch` | the rebuilt comparator did not re-hash to the anchored identity |
| `comparator_baseline_missing` | the oracle `q4_k_m` arm is absent, so no retention denominatorable comparison exists |

## 4. Artifact identity anchor

There is no approved manifest for the comparators —
`artifacts/qwen35-9b/model-manifest.json` describes only the Q4. The anchor is
the checked-in `artifacts/qwen35-9b/scan-receipt.json`
(SHA-256 `0857899bf86527702743dd12b62ae5740cbb27fae2005da7066d1070cb341066`,
708 bytes), which records the exact 2026-09-04 comparator identities:

| Name | Size | SHA-256 |
|---|---:|---|
| `Qwen3.5-9B-bf16.gguf` | 17,920,697,184 | `3781359159dcec91e8f57820f63ffa6ec32b6f0c94b38bfb16c9fe0d50561316` |
| `Qwen3.5-9B-Q8_0.gguf` | 9,527,501,664 | `516a12b01fda7a9a204b71a9917e3b5b0c4efbbd8f6e6f4f6b78e4ff699cf0db` |
| `Qwen3.5-9B-Q4_K_M.gguf` | 5,629,109,088 | `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b` |

The digest is pinned in source as `_APPROVED_SCAN_RECEIPT_SHA256`, in the same
shape as the existing `_APPROVED_EVAL_MANIFEST_SHA256` trust anchor, so a
caller cannot substitute an arbitrary scan receipt through configuration. The
scan receipt is uploaded **only** when comparators are requested.

`remote_comparator_eval.py` re-hashes the freshly rebuilt comparator and
refuses unless name, size and SHA-256 match the anchor exactly. Byte-exact
reproduction from the pinned source revision is therefore the acceptance
condition; any drift refuses with `comparator_artifact_identity_mismatch`
rather than silently evaluating unknown bytes.

## 5. Evaluation contract held constant

Every arm uses:

- the same fixture file `tests/model/production_tool_call_eval.json`, SHA-256
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`,
  33 tools / 37 cases, categories
  `tool_selection 18, confirmation_sensitive 15, schema_edge 1,
  prompt_injection 1, abstention 1, no_tool 1`;
- the same evaluator `scripts/test/evaluate_tool_calls.py`, unmodified;
- the same limits: `context_tokens 8192`, `max_output_tokens 256`,
  `temperature 0`, `max_cases 64` as a ceiling with 37 actual cases;
- the same `--gpu-layers 99` full offload on the same A100-80G device receipt;
- a fresh server process per arm, on its own loopback port, with
  `cache_prompt: false` on every request — recorded as
  `cache_state: "cold_process_fresh_server_per_arm"`;
- `sample_count = 37`, one pass per case, no repetition.

The Q4 path through `remote_model_eval.py` and `lae-engine` is **not touched**.
Its argv, its evaluator invocation, its scoring and its receipt schema are
byte-identical whether or not comparators are requested.

## 6. Receipts

### 6.1 Per-arm: `comparator-receipt-<arm>.json` (remote, then salvaged)

`schema: local_bmo.j1m.comparator-eval-receipt.v1`

```
status               verified | completed_with_failures | failed
arm                  q4_k_m | q8_0 | bf16
artifact             {name, size_bytes, sha256, quantization}
anchor               {scan_receipt_sha256}
fixture              the full fixture identity block (sha256, limits, tool_names, …)
host                 {kind: "pinned-upstream-llama-server", llama_cpp_revision, backend,
                      gpu_layers, server_build_flags, server_argv, transport}
settings             {context_tokens, output_reserve_tokens, temperature, max_cases}
cache_state          "cold_process_fresh_server_per_arm"
sample_count         37
metrics              {case_count, passed, failed, errors, peak_rss_kib, category_summary,
                      canary, error_diagnostics, quality_diagnostics}
case_indicators      [{id, category, passed}] -- bounded per-case pass vector, no
                     prompt/response/model output; required for a paired interval
timings              {server_ready_ms, evaluation_ms, total_ms}
prompt_response_logging  false
token_logging            false
```

### 6.2 Aggregate: `comparison-receipt.json` (local, stdlib, no network)

`schema: local_bmo.j1m.comparison-receipt.v1`. Computed by
`scripts/test/compare_model_quality.py` during salvage, before teardown, from
the salvaged per-arm receipts. Required keys:

```
schema, status, generated_at_utc, fixture_sha256, case_count,
critical_category_policy, gate, baseline, comparisons, skipped,
product_engine_reference
```

`gate` records the spec constants verbatim:
`{retention_min_percent: 95.0, non_inferiority_margin_points: 2,
critical_category_max_drop_points: 8, bootstrap_resamples: 10000,
confidence: 0.95, seed: 20260911}`.

Each entry of `comparisons` carries:

```
comparator, baseline_score_points, comparator_score_points,
retention_percent, retention_verdict,
aggregate_delta_points, category_deltas, category_drop_max_points,
critical_category_verdict,
bootstrap {resamples, confidence, seed, method, pairing,
           delta_ci_lower_points, delta_ci_upper_points,
           non_inferiority_verdict},
verdict
```

`verdict` is `pass` only when retention, critical-category and
non-inferiority verdicts are all `pass`; anything else, including any
undefined quantity, is `fail`.

## 7. Comparison mathematics

Score of an arm is `passed / case_count × 100` points. An `error` case counts
as not passed, exactly as the evaluator's own totals do.

**Retention** — `retention_percent = 100 × score(q4_k_m) / score(comparator)`.
Gate: `≥ 95.0`. When the comparator score is `0`, retention is undefined; the
receipt records `retention_percent: null` with
`retention_verdict: "fail"` and reason `retention_undefined_zero_reference`
(fail-closed, never a silent pass).

**Category deltas** — per category
`delta_c = score_c(q4_k_m) − score_c(comparator)` in points, where
`score_c = passed_c / case_count_c × 100`. `category_drop_max_points` is the
largest observed drop, `max(0, max_c(−delta_c))`. Gate: `≤ 8`.

**Critical categories** — `governance/MODEL_DECISION.md` names the critical set
in prose ("tool argument integrity, safety boundary following, file-edit
fidelity, instruction following, state isolation"), which does not map
one-to-one onto the fixture's six categories. Rather than invent a mapping,
this slice applies the ≤8-point rule to **every** category and records
`critical_category_policy: "all_categories_conservative"`. This is strictly
stricter than any subset, so it cannot let a genuine critical regression
through. Sol may narrow it later by recording an explicit mapping; the receipt
field exists so that change is visible. Note that `schema_edge`,
`prompt_injection`, `abstention` and `no_tool` each hold a single case, so a
single flip is a 100-point category delta; per-category `case_count` is
recorded alongside every delta so a reviewer can see that.

**Bootstrap non-inferiority** — `random.Random(20260911)`, 10,000 resamples,
95% percentile interval, nearest-rank convention on the sorted deltas.
Verdict: `pass` iff `delta_ci_lower_points ≥ −2`.

*The interval must be paired, and that is why §6.1 requires
`case_indicators`.* Two methods are implemented and the receipt records which
one produced the number:

| `pairing` | Method | When |
|---|---|---|
| `paired_by_case_id` | resample case ids with replacement, average the per-case differences `b_i − c_i ∈ {−1,0,1}` | both arms supplied `case_indicators` over the identical case-id set |
| `unpaired_stratified_by_category` | resample `case_count_c` indicators with replacement inside each category, independently per arm | fallback when either vector is absent |

The fallback is not merely wider — at this corpus size it is **not decidable**.
Measured with the shipped constants, two *identical* arms produce:

| Cases | Unpaired 95% interval | Non-inferiority at margin 2 |
|---:|---|---|
| 37 (the current profile) | ±16.22 points | fail |
| 1,200 (the authored corpus target) | ±3.25 points | fail |

whereas the paired interval on two identical arms is exactly `[0, 0]` and
passes. A 2-point margin is therefore unreachable without pairing at any
realistic corpus size, so the per-arm receipt schema in §6.1 makes
`case_indicators` the expected field and the unpaired path exists only to
refuse honestly rather than to approve on weak evidence.

This costs nothing on the Q4 path. `case_indicators` is a field of the **new**
`comparator-eval-receipt.v1` schema, not of `real-tool-eval-receipt.v1`.
`scripts/test/evaluate_tool_calls.py` already builds per-case `records` inside
`run_local`; only `aggregate_result` strips them for the remote-safe contract.
`remote_comparator_eval.py` reads the bounded `{id, category, passed}` triple
from the evaluator's own `--emit-case-indicators` output — no prompt, no
response, no model output — and cross-checks it against the category summary
before writing it. Without that flag the aggregate contract is unchanged, so
the evaluator's scoring semantics and the Q4 receipt are untouched.

Even paired, the 37-case profile is underpowered for a 2-point margin: a
single flipped case is 2.70 points, so the interval cannot sit inside ±2 unless
the arms agree on every case. That is a real property of the fixture, not of
the method, and it is a further argument for `MODEL-QUALITY-CORPUS-001`.

Determinism is a tested property: the same inputs and seed produce the same
interval on every run, and the seed is recorded in the receipt.

## 8. Salvage and teardown

`config.artifacts.eval_fetch_allowlist` is unchanged on disk. The orchestrator
computes the effective allowlist at runtime: the shipped five names, plus, only
when comparators were requested and only for the arms actually requested,
`comparator-receipt-q4_k_m.json`, `comparator-receipt-q8_0.json`,
`comparator-receipt-bf16.json` and `scan-receipt.json`.

**Only receipts are salvaged. No comparator weights are ever fetched.** The
allowlist mechanism is name-based and the model filenames are not in it;
`_salvage` refuses anything not named in the list.

`comparison-receipt.json` is produced locally in `artifact_destination`
during the existing salvage block, before the teardown call, so it exists on
every exit path including failure. If the per-arm receipts were not salvaged —
which is the current state of the source, because `_salvage` still raises
`external salvage transport is unavailable in this source slice` — the
comparison receipt is written with `status: "skipped"` and the typed reason.
It never fabricates a number it did not read.

Teardown, `SOL_J1M_REVIEWED`, the watchdog, the host shutdown backstop, the
no-orphan backstop, exact deletion and cost settlement are all untouched.

## 9. What is implemented today

Complete, offline-provable, and unit-tested, from
`MODEL-COMPARATOR-EVAL-001`:

- `j1m_runner.command_plan(..., retain_comparators=False)` and
  `j1m_runner.comparator_cleanup_plan()`, with the byte-identity invariant of
  §2.3 pinned by test, plus the `--retain-comparators` CLI switch.
- `scripts/j1m_orchestrator.py`: the `--evaluate-comparators` flag (default
  OFF), selection parsing, the arm set, the static budget arithmetic, the
  runtime clock gate, the effective salvage allowlist, the typed fail-closed
  gate, and the comparison-receipt production point inside the existing
  salvage block.
- `scripts/test/compare_model_quality.py`: the whole of §7 — retention,
  per-category deltas, the seeded stratified bootstrap, every verdict, and the
  `comparison-receipt.json` writer, all stdlib.

Added by `COMPARATOR-ENGINE-001` (§2.2a):

- `scripts/test/evaluate_tool_calls.py`: the transport abstraction, the
  `upstream-openai` transport, the port of the product engine's
  schema-abstention policy, structured-`tool_calls` normalization, and
  `--emit-case-indicators`. The default transport is byte-identical.
- `scripts/j1m_runner.py`: `comparator_server_plan()`,
  `comparator_server_configure_flags()` and `comparator_server_binary()` —
  the pinned upstream `llama-server` build.
- `scripts/test/remote_comparator_eval.py`: artifact identity against the
  §4 anchor, the bounded server launch and `/health` readiness check, the
  evaluator invocation, the §6.1 receipt, and SIGTERM/SIGKILL teardown.
- `scripts/j1m_orchestrator.py`: `_COMPARATOR_ENGINE_AVAILABLE = True`, the
  `q4-oracle` selection, the comparator remote stages, the deferred cleanup
  tail, the live runtime clock gate, and the comparator-only stage budgets
  (kept out of `_eval_deadline_ceiling`, §2.4).

Still not reachable, and not owned by either slice:

- `_salvage` still raises `external salvage transport is unavailable in this
  source slice`, so no receipt of any kind can be fetched yet and the
  comparator phase degrades to a typed skip on a real run. That transport is
  `J1M-SALVAGE-TRANSPORT`, not this work.

## 10. What this work does not do

- It does not run anything. No provider call, no model, no network, no
  credential, no new dependency.
- It does not raise any cap, budget, timeout, or hours figure.
- It does not change the product engine, its compiled identity or its CMake,
  and it does not change the Q4 evaluation path. The evaluator gains a second
  transport; its default transport, its scoring semantics and its Q4 aggregate
  contract are unchanged. With the flag absent, the argv, plan object,
  uploads, allowlist and receipts are byte-for-byte what they are on `main`.
- It does not author the quality corpus (`MODEL-QUALITY-CORPUS-001`); the
  comparator arms score the 33-tool/37-case tool-call profile, which is the
  fixture the ≥95% criterion will be read against first.
- It does not by itself satisfy §11. It makes the reference *reachable*; the
  engine slice plus a reviewed run with Sol's spend authorization produces the
  numbers.

## 11. Open items for review

1. **`COMPARATOR-ENGINE-001` (§2.2a) has landed, in a different shape than
   this document originally proposed.** No second engine target with a
   relaxed identity was created; the arms are hosted on the pinned upstream
   `llama-server`. Sol should confirm that substitution. Two residuals that
   only a real run can close: (a) the rendered *token* sequence is not
   compared offline — only the message list and the template source are
   (§2.2a.1); (b) `--no-webui` and `chat_template_kwargs.enable_thinking` are
   accepted by the pinned upstream server as read from its argument parser
   and API at that revision, and an unknown flag would refuse the launch into
   a typed skip rather than produce a wrong number. The failure is
   fail-closed either way, but it would cost one arm's budget.
2. The static worst case does not fit (§2.4). Sol may either accept the
   opportunistic runtime gate as designed, or authorize a `modes.eval` clock
   increase — `runtime_hours` 1.94 → ~2.8 with the dependent
   `host_shutdown_delay_minutes`, `external_watchdog_seconds` and
   `provider_backstop_hours` raised in step — which is a cap change and is
   deliberately **not** made here.
3. The critical-category mapping (§7) is conservative-by-default and wants an
   explicit Sol-recorded mapping.
4. The non-inferiority margin of 2 points is only decidable with paired
   indicators (§7), and even paired it needs a corpus larger than 37 cases.
   Sol should note that §11 MUST "report effect sizes and bootstrap intervals"
   is satisfiable today, but the 2-point margin verdict on the tool-call
   profile will read `fail` for any real difference until
   `MODEL-QUALITY-CORPUS-001` lands.
5. `_salvage` is inert in current source, so no receipt of any kind can be
   fetched yet. The comparator phase degrades to a typed skip until that
   transport lands. This is now the single remaining blocker between the
   implemented phase and a real number.
6. The comparator server build is budgeted at 1500 s (configure 300 + CUDA
   build 1200) on the assumption that a second CUDA build tree costs about
   what the engine's does. It is the one figure in §3 that is an estimate
   rather than an existing measurement, and a first real run should record
   the actual value.
