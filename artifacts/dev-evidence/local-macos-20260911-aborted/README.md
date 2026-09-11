> **DEVELOPMENT EVIDENCE ONLY.** Committed at Sol's direction for traceability, held outside
> `artifacts/evidence-index/`; it is not indexed evidence, not Shadeform evidence, not target
> evidence, and it advances no gate. The run aborted before any model load and produced no score.
> Only this `README.md` is committed; the sibling raw logs listed in section 11 stay outside the
> repository. Secret scan before committing: no credential value, token, secret, or external URL
> is present, so nothing required redaction. Every long hexadecimal string here is a content hash
> (model artifact, fixture, binaries) or a commit SHA.

# DEVELOPMENT EVIDENCE ONLY — local macOS arm64, not Shadeform, not target, advances no gate; Sol-authorized exception to B-006 workaround for a single local run

**Status: ABORTED BEFORE SCORING. No tool-call evaluation score was produced.**

Date: 2026-09-11 (UTC). Repo `local_assistant_engine_plan_qwen35`, `main` @ `fdfed07692ae35713432e02a2c1d7f729267acb4`.

This receipt records a development-only local evaluation attempt. It must **not** be written into
`artifacts/evidence-index/`. It is not target evidence, not Shadeform evidence, and not gate
evidence. (Originally written as "must not be committed"; Sol subsequently directed that this
README be committed here, outside the evidence index, so the aborted run stays on the record.)

---

## 1. Outcome summary

| Item | Result |
|---|---|
| **Score** | **NOT PRODUCED — run aborted before any scoring request** |
| Fixture | `tests/model/production_tool_call_eval.json` sha256 `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c` (33 tools / 37 cases) |
| Model artifact identity | **VERIFIED — exact match** |
| `real-native-engine.test.mjs` | **FAIL** (exit 1) — engine rejected its arguments |
| `real_model_smoke.py` | **FAIL** (exit 1) — engine rejected its arguments |
| `real_init_guard.py` | **FAIL** (exit 1) — guard could not be exercised; engine rejected its arguments |
| Stop conditions triggered | **TWO** (see §3) |
| `git status --short` before/after | identical (both empty) — worktree untouched |
| Engine process at exit | none running, no listener |

Two independent declared stop conditions triggered. Either alone forbids the scoring run.

---

## 2. Artifact identity (STEP 1) — PASS

| Field | Value |
|---|---|
| Path | `artifacts/qwen35-9b/Qwen3.5-9B-Q4_K_M.gguf` |
| Size | `5629109088` bytes — **matches** required `5,629,109,088` |
| SHA-256 | `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b` — **matches** required value |
| Command | `shasum -a 256`, 22 s wall (13:46:27Z → 13:46:49Z) |

The pinned artifact is present and bit-exact on this host. **This is the one thing this run
positively establishes.** It did not trigger the identity stop condition.

---

## 3. Stop conditions triggered

### 3.1 STOP — host memory cannot hold the artifact (declared abort condition)

Baseline, measured **before** any engine start (`memory-before.txt`, 13:46:50Z):

| Metric | Value |
|---|---|
| Total physical RAM | **8.00 GiB** (`hw.memsize` = 8589934592) |
| PhysMem used / unused | 7472 MB used / **144 MB unused** |
| Compressor | 3344 MB already held compressed |
| Swap | 6144 MB total, **5791.75 MB used, 352.25 MB free (94.2 % consumed)** |
| `kern.memorystatus_vm_pressure_level` | **2 = WARNING** (1 normal, 2 warning, 4 critical) |
| Lifetime swapouts / swapins | 33,239,505 / 26,086,624 — the host is already thrashing |
| macOS "free percentage" | 31 % (≈ 2.5 GiB, optimistic; includes reclaimable) |

The artifact alone is 5.243 GiB resident. Inference touches every weight each token, so the working
set is effectively the whole file, plus KV/recurrent state for an 8192-token context, plus engine
overhead — call it ≈ 6 GiB. Against ≈ 2.5 GiB optimistically available and 352 MB of remaining swap,
the deficit is ≈ 3.5 GiB. Starting the engine would have driven pressure level to 4 (critical) and
risked the macOS jetsam killer terminating the user's running applications (VS Code, Chrome).

The task's declared condition — *"Free memory drops such that the system starts heavy swapping …
abort if memory pressure becomes critical"* — was **already satisfied at baseline**. Re-measured at
13:48:23Z after the test phase: 92 MB unused, pressure still 2, swap 5500.75 MB used.

This also breaches AGENTS.md → Stop conditions: *"Memory exceeds the hard guard or causes severe
system paging."*

**Host vs. governance target:** target is a Dell Windows x64 laptop with ~31.46 GiB RAM and ~17.35 GiB
observed available. This host has 8.00 GiB total — roughly a quarter of target RAM, different OS and ISA.

### 3.2 STOP — the prebuilt engine is 532 commits stale and does not correspond to `fdfed07`

`out/build-real/native/lae-engine` (sha256 `4a0470ca…14be52`, built 2026-09-04T02:42:38Z) does not
match `native/main.cpp` at HEAD.

Binary `--help` (verbatim):
```
serve options: --config <absolute-json> --backend cpu --model <absolute-gguf> --size <bytes> --sha256 <hex> --context <tokens> --token <bearer>
```
Source at HEAD (`native/main.cpp:32`):
```
serve options: --config <absolute-json> --backend cpu|intel-vulkan|cuda --model <absolute-gguf> --context <tokens> --gpu-layers <0..99> --vulkan-device-name <exact-name> --cuda-device-name <exact-name> (--token-file <protected-file> | --token-stdin)
model filename, size, SHA-256, GGUF metadata, and tensor profile are compiled product identity and cannot be supplied by callers
```

The binary still takes caller-supplied `--size`/`--sha256` and a token **on the command line** — the
exact pre-hardening contract that HEAD replaced with compiled product identity and a protected token
source. Confirmed empirically:

```
$ out/build-real/native/lae-engine serve --backend cpu --token-stdin </dev/null
unknown argument                                    (exit 2)
```

Drift measured against the commit whose `main.cpp` matches the binary (`681a43c8`, 2026-09-04 03:03):
**532 commits behind HEAD; 79 touching `native/`; 64 files changed, +17,971 / −282 lines.**

Consequence: every prescribed test (STEP 3/4/5) passes `--token-stdin` or `--token-file` and is
rejected during argument parsing. A score obtained from this binary would describe Sep-4 code, not
`fdfed07`, and would be misleading evidence.

**No rebuild was attempted.** A rebuild was out of the authorized scope of this run, and it could not
have rescued it: §3.1 independently forbids loading the artifact on this host.

---

## 4. Engine identity and backend flags (as inspected, not as exercised)

| Field | Value |
|---|---|
| Path / sha256 | `out/build-real/native/lae-engine` / `4a0470cadc5caf1c680e851c6e74e97b4229ac88fb32d75681e2bb7ba014be52` |
| `version` | `0.1.0` |
| `print-build-info` | `compiled_backend: llama.cpp/3581ba0c/cpu`, `llama_cpp_revision: 3581ba0cf591b3f772fbb002de0f70e294bc0396`, `selected_backend: runtime-config`, `model: external-manifest` |
| Links `libllama`? | **Yes, transitively.** `lae-engine` → `@rpath/liblae_runtime.dylib` → `@rpath/libllama.0.dylib`, `libggml.0`, `libggml-cpu.0`, `libggml-base.0`. 13 undefined `llama_*` symbols (`llama_backend_init`, `llama_decode`, `llama_init_from_model`, …). |
| Resolved libllama | `out/build-real/bin/libllama.0.0.60.dylib` sha256 `6f9c34b8…67ac12` |

Note: `model: external-manifest` in `print-build-info` is itself the stale contract; HEAD compiles the
model identity in.

`out/build-real/CMakeCache.txt`:

| Flag | Value |
|---|---|
| `LAE_ENABLE_LLAMA_CPP` | **ON** |
| `CMAKE_BUILD_TYPE` | Release |
| `GGML_CPU` | ON |
| `GGML_METAL` | **OFF** |
| `GGML_ACCELERATE` | **ON** |
| `GGML_BLAS` | OFF (vendor Apple) |
| `GGML_NATIVE` | OFF |
| `GGML_CPU_REPACK` | ON |
| `GGML_CPU_KLEIDIAI` | OFF |
| `GGML_OPENMP` | ON requested, but `GGML_OPENMP_ENABLED:INTERNAL=OFF` — **OpenMP not actually enabled** |

Backend that *would* have been used: **CPU only** (Metal off, BLAS off, Accelerate on, no OpenMP).
These flags were **read from the cache, never exercised** — no inference ran.

**Context size / thread count / cache state: not applicable.** No engine was started, so there is no
cold/warm cache observation, no load time, no first-token latency. Note also that `native/main.cpp` at
HEAD exposes **no `--threads` flag** at all; thread count is not caller-settable via the serve CLI, so
the "threads = physical cores" instruction could not have been honoured as written. Bind address is
likewise not caller-settable — the serve path hardcodes `127.0.0.1` and emits
`{"event":"ready","port":…,"bind":"127.0.0.1","token_required":true}`.

---

## 5. Machine profile

| Field | Value |
|---|---|
| CPU | Apple M2 (`Mac14,2`), 8 logical / 8 physical cores |
| RAM | 8.00 GiB |
| OS | macOS 26.2 (build 25C56), Darwin 25.2.0, arm64 |
| node | v25.9.0 |
| python3 | 3.14.6 |
| cmake | 4.1.2 |
| HEAD | `fdfed07692ae35713432e02a2c1d7f729267acb4` ("Merge governance refresh v4", 2026-09-11) |

---

## 6. Smoke / guard results (STEPS 3–5)

All three ran against the stale binary and failed **in argument parsing, before any model load** —
which is why running them was memory-safe.

| Test | Exit | Wall | Observed |
|---|---|---|---|
| `tests/host/real-native-engine.test.mjs` | 1 | 1 s | 1 test, 0 pass, 1 fail. `Error: real engine exited (2): unknown argument` after 16.47 ms. |
| `tests/native/real_model_smoke.py` | 1 | 1 s | `AssertionError: real model server did not become ready: 'unknown argument\n'` |
| `tests/native/real_init_guard.py` | 1 | 0 s | `AssertionError: shallow GGUF was not rejected by compiled identity: rc=2 stderr='unknown argument\n'` |

**What `real_init_guard.py` asserts** (negative identity guard, recorded as instructed): it writes a
superficially GGUF-shaped **70-byte** file named `Qwen3.5-9B-Q4_K_M.gguf` (magic `GGUF` + version 3 +
62 zero bytes) and requires the engine to (a) exit **rc=2** with **`model_size_mismatch`** in stderr
*before* llama.cpp initialization, and (b) refuse a `config.local.json` that tries to override
`model_path`/`model_size_bytes`/`model_sha256`, exiting rc=2 with `unknown key`. Together these prove a
shallow GGUF cannot impersonate the compiled product artifact and that runtime config cannot override
compiled identity. **On this run the guard was not exercised**: it got rc=2 for the wrong reason
(`unknown argument`, i.e. the stale binary does not support `--token-file`), and the guard correctly
refused to record that as a pass. The stale binary in fact still accepts caller-supplied
`--size`/`--sha256`, which is precisely the weakness this guard exists to catch.

---

## 7. Per-case table (STEP 7) — NOT RUN

No scoring request was sent to any engine. The evaluator was executed **only** in `--dry-run` mode,
which validates the fixture and prints case IDs without contacting an endpoint (exit 0).

All 37 cases are therefore **NOT RUN** — no pass, no fail, no failure category. Recording them as
anything else would be fabrication.

| # | Case ID | Category | Status | Failure category |
|---|---|---|---|---|
| 1 | prod-time-001 | tool_selection | NOT RUN | — |
| 2 | prod-system-001 | tool_selection | NOT RUN | — |
| 3 | prod-clipboard-read-001 | tool_selection | NOT RUN | — |
| 4 | prod-clipboard-write-001 | confirmation_sensitive | NOT RUN | — |
| 5 | prod-app-open-001 | confirmation_sensitive | NOT RUN | — |
| 6 | prod-browser-url-001 | confirmation_sensitive | NOT RUN | — |
| 7 | prod-fs-list-001 | tool_selection | NOT RUN | — |
| 8 | prod-fs-read-001 | tool_selection | NOT RUN | — |
| 9 | prod-fs-search-001 | tool_selection | NOT RUN | — |
| 10 | prod-fs-write-001 | confirmation_sensitive | NOT RUN | — |
| 11 | prod-fs-patch-001 | confirmation_sensitive | NOT RUN | — |
| 12 | prod-process-001 | confirmation_sensitive | NOT RUN | — |
| 13 | prod-mail-list-001 | tool_selection | NOT RUN | — |
| 14 | prod-mail-search-001 | tool_selection | NOT RUN | — |
| 15 | prod-mail-read-001 | tool_selection | NOT RUN | — |
| 16 | prod-mail-draft-001 | confirmation_sensitive | NOT RUN | — |
| 17 | prod-mail-send-001 | confirmation_sensitive | NOT RUN | — |
| 18 | prod-mail-read-state-001 | confirmation_sensitive | NOT RUN | — |
| 19 | prod-teams-chats-001 | tool_selection | NOT RUN | — |
| 20 | prod-teams-messages-001 | tool_selection | NOT RUN | — |
| 21 | prod-teams-read-message-001 | tool_selection | NOT RUN | — |
| 22 | prod-teams-channels-001 | tool_selection | NOT RUN | — |
| 23 | prod-teams-channel-messages-001 | tool_selection | NOT RUN | — |
| 24 | prod-teams-read-channel-message-001 | tool_selection | NOT RUN | — |
| 25 | prod-teams-send-001 | confirmation_sensitive | NOT RUN | — |
| 26 | prod-copilot-001 | confirmation_sensitive | NOT RUN | — |
| 27 | prod-browser-start-001 | confirmation_sensitive | NOT RUN | — |
| 28 | prod-browser-links-001 | tool_selection | NOT RUN | — |
| 29 | prod-browser-page-001 | tool_selection | NOT RUN | — |
| 30 | prod-browser-follow-001 | confirmation_sensitive | NOT RUN | — |
| 31 | prod-browser-fill-001 | confirmation_sensitive | NOT RUN | — |
| 32 | prod-browser-activate-001 | confirmation_sensitive | NOT RUN | — |
| 33 | prod-browser-close-001 | tool_selection | NOT RUN | — |
| 34 | prod-schema-invalid-001 | schema_edge | NOT RUN | — |
| 35 | prod-injection-001 | prompt_injection | NOT RUN | — |
| 36 | prod-abstention-001 | abstention | NOT RUN | — |
| 37 | prod-no-tool-001 | no_tool | NOT RUN | — |

Category totals: tool_selection 18, confirmation_sensitive 15, schema_edge 1, prompt_injection 1,
abstention 1, no_tool 1 = **37**.

**Evaluator note for whoever runs this for real:** `--fixture` must be passed explicitly (its default
`FIXTURE` points at the retired `tests/model/tool_call_eval.json`). `run_local()` also fires a
synthetic ~2400-char **canary** request first and requires `513 ≤ prompt_tokens ≤ 8192−256`; if the
canary fails, every case is recorded as `error` with the canary's code and **no case is ever scored**.
`main()` prints only `aggregate_result(...)` — `case_count/passed/failed/errors/peak_rss_kib/
category_summary/canary/error_diagnostics/quality_diagnostics`. Per-case records exist in
`run_local()`'s return value but are **deliberately withheld from stdout** ("Return only the
remote-safe metrics contract, excluding case details"), so a genuine per-case table cannot be obtained
from the CLI as written. `--engine-pid <pid>` enables bounded RSS sampling and is the only way to get
`peak_rss_kib`. There are no output/receipt file flags.

---

## 8. Security posture of this run

- No engine process was started; no socket was opened; nothing bound to any interface.
- No bearer token was generated. A credential was deliberately **not** created, since none was needed
  once the run aborted — no `0600` token file exists in this directory.
- No network host was contacted and nothing was downloaded.
- No prompt or response bodies appear in this receipt (none exist — no inference ran).
- The repository worktree was not modified: `git status --short` is byte-identical (empty) before and
  after; nothing was written inside the repo; nothing was committed.

---

## 9. Timings

| Phase | Wall |
|---|---|
| Model SHA-256 (5.629 GB) | 22 s |
| STEP 3 host engine test | 1 s |
| STEP 4 smoke | 1 s |
| STEP 5 init guard | 0 s |
| STEP 7 evaluator `--dry-run` | < 1 s |
| Model load | **n/a — never loaded** |
| First token | **n/a — no inference** |
| Total session wall (13:45Z → 13:48Z) | ≈ 3 min |

No case approached the 6-minute cap; the 100-minute budget was not remotely consumed. The run ended
on blockers, not on time.

---

## 10. What this does and does not prove

**Proves:**
- The pinned artifact `Qwen3.5-9B-Q4_K_M.gguf` is present on this host at exactly 5,629,109,088 bytes
  with SHA-256 `c654bc40…68873b` — bit-exact identity match.
- The production evaluation fixture on `main@fdfed07` is the 33-tool / 37-case profile
  (sha256 `c75af520…c8ac6c`) with limits {8192 ctx, 256 out, temp 0}, and it passes the evaluator's
  own schema validation.
- `out/build-real` is configured with `LAE_ENABLE_LLAMA_CPP=ON` and genuinely links `libllama`/`ggml`
  (CPU-only: Metal OFF, BLAS OFF, Accelerate ON, OpenMP not enabled).
- The checked-in `out/build-real` engine binary is **532 commits stale** and still exposes the
  pre-hardening caller-supplied `--size`/`--sha256`/`--token` contract. Anyone treating that binary as
  current would be testing Sep-4 code. This is a real, actionable defect in the local build tree.
- `tests/native/real_init_guard.py` correctly refuses to report a pass when the engine exits rc=2 for
  the wrong reason.

**Does NOT prove (nothing here advances any of these):**
- **No score.** Nothing whatsoever is established about tool-call quality on the 33/37 profile. This is
  still the *zeroth* measurement — the first-ever measurement on that profile has **not** happened.
- Does not prove the engine loads the exact bytes on CPU on this host — the model was **never loaded**.
- Does not prove the template / tool-call rendering path works — it was **never exercised**.
- No custody chain, no oracle parity, no quality retention, no 8K or 16K context validation, no soak,
  no cancellation, no memory-recovery evidence.
- No backend disposition (CPU, Vulkan, or SYCL), no Windows target evidence, no Shadeform evidence.
- No gate is advanced, satisfied, or informed. Not B-006 closure. Not M1, M2, M5, or M6 evidence.
- This host (8 GiB macOS arm64) is **not** the governance target (~31.46 GiB Windows x64) and cannot
  stand in for it even if the two blockers were fixed.

**To actually obtain this measurement**, both blockers must clear: (1) rebuild `out/build-real` from
`fdfed07` so the engine matches the pinned source, and (2) run it on a host with enough free RAM —
realistically ≥ 16 GiB, on Shadeform per governance, not on this 8 GiB laptop.

---

## 10b. Correction to the scoping report (`model-testing-status.md` section D(i))

This run falsifies two premises of the report that scoped it. Recording them so the next planner does
not inherit the same assumptions:

1. **"In principle nothing technical is missing for a smoke + tool-call run."** - **False.** The
   report identified `out/build-real/native/lae-engine` as "a real llama.cpp-linked engine" from its
   CMake cache and library linkage, which is correct as far as it goes, but it never invoked the
   binary with the flags the tests actually use. The binary is 532 commits stale and rejects
   `--token-stdin` / `--token-file` with `unknown argument`. All four steps the report lists under
   "What a run would look like" fail on this binary. A rebuild from `fdfed07` is a hard prerequisite
   that the report does not mention.

2. **Host memory was never assessed.** The report does not state this host's RAM. It is **8.00 GiB**,
   against a 5.243 GiB artifact, with swap already 94% consumed and pressure at WARNING before
   anything starts. The local run described in D(i) is not merely "in tension with B-006" - it is
   **not physically runnable on this machine**, independent of authorization.

Section B.3 of the report is confirmed accurate: the three real-model tests are self-gating on
`LAE_QWEN35_MODEL`, and setting it does enable them (they ran here, and failed for the reason above
rather than skipping).

The report's closing claim - that the run's "single genuine value would be establishing, for the first
time, what this model actually scores on the 33/37 profile" - remains **unrealized**. That measurement
still does not exist.

---

## 11. Files in this directory

| File | Contents |
|---|---|
| `README.md` | this receipt |
| `commands.txt` | every command, verbatim, with UTC timestamps and exit codes |
| `machine.json` | machine profile, engine identity, backend flags |
| `model-identity.json` | artifact path / size / SHA-256 verification |
| `fixture-identity.json` | fixture sha256, tool/case counts, limits, categories |
| `machine-raw.txt` | raw `sysctl`/`sw_vers`/version output |
| `memory-before.txt` | baseline `vm_stat`, `memory_pressure`, swap (13:46:50Z) |
| `memory-after.txt` | post-phase `vm_stat`, pressure level, swap (13:48:23Z) |
| `model-sha256.raw` | raw `shasum -a 256` output with start/end timestamps |
| `artifact-hashes.txt` | sha256 of engine, runtime dylib, libllama, fixture, evaluator |
| `real-native-engine.log` | STEP 3 raw output |
| `real_model_smoke.log` | STEP 4 raw output |
| `real_init_guard.log` | STEP 5 raw output |
| `evaluate_tool_calls.log` | STEP 7 `--dry-run` output (fixture validation only; no score) |
| `git-status-before.txt` / `git-status-after.txt` | both empty; proof the worktree was untouched |

**`evaluation-result.json` is deliberately absent — no evaluation was run, so there is no result to record.**
