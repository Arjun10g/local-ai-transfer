# Initial Task Board

This is the seed backlog. Sol copies active entries into `coordination/TASK_CLAIMS.md`, refines scope, and assigns no more than two concurrent tasks per Luna. A task is complete only with the listed evidence and Sol merge.

## Task prefixes

- `SOL` — orchestration/decision
- `RUN` — native runtime
- `MODEL` — selected model behavior/integrity
- `PERF` — hardware, cache, acceleration, benchmarks
- `TOOL` — host, UI, tool calling
- `SEC` — security/adversarial
- `QA` — test/conformance
- `REL` — packaging/release
- `DOC` — operator/design documentation

## Critical path

```text
SOL-001
  ├── SOL-002 contracts
  │    ├── RUN-001 → RUN-004 → RUN-006 → RUN-009
  │    ├── TOOL-001 → TOOL-004 → TOOL-009 → TOOL-014
  │    └── QA-001 → QA-003 → QA-006
  ├── MODEL-001 → MODEL-003
  └── PERF-001 → PERF-004 → PERF-008
                               └── REL-006 → REL-009
```

Intel GPU acceleration is parallel to the CPU functional critical path until Phase 5. The release remains functionally valid on CPU; Vulkan or SYCL becomes default only after exact-device promotion.

---

## Phase 0 — Freeze and evidence

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| SOL-001 | Sol | Initialize branches, worktrees, coordination files, task claims | None | All five sessions have branch/status/task; no overlapping claim |
| SOL-002 | Sol | Revision ADR: Qwen3.5-9B Q4_K_M, text-only modality, Intel backend ladder, host, offline definition | SOL-001 | ADR accepted by all lane owners; no worker substitutes another model/quant/backend |
| SOL-003 | Sol | Policy/approval status matrix | SOL-001 | Every approval-dependent feature has owner/status/evidence field |
| SOL-004 | Sol | Freeze MVP/non-goals and milestone definitions | SOL-002 | Acceptance criteria linked to phases/tasks |
| SOL-005 | Sol | Select initial Shadeform profile criteria and budget ceiling | SOL-003 | Profile criteria, hour caps, cleanup owner recorded |
| RUN-001 | Luna A | Define `EngineBackend` interface and engine state model | SOL-002 | Contract examples, lifecycle errors, peer review by S3/S4 |
| RUN-002 | Luna A | Define engine HTTP/session/cancel contracts | RUN-001 | Versioned schema/fixtures; S3 can mock; S4 tests parse |
| RUN-003 | Luna A | Bounded GGUF/model-validation specification | MODEL-001 | Supported profile and all rejection classes defined |
| MODEL-001 | Luna B | Qwen3.5-9B source, text-only modality, conversion, quantization, and manifest specification | SOL-002 | Official source/revision and license pinned; placeholders explicit; no weight in Git |
| MODEL-002 | Luna B | Model quality/tool-call evaluation design | MODEL-001 | Corpus/scoring/effect-size/interval plan reviewed by S4 |
| PERF-001 | Luna B | Safe Dell/CPU/Intel GPU/Vulkan/SYCL hardware probe | SOL-003 | Exact device/driver/memory/backend schema; read-only; no secrets; sample receipt |
| PERF-002 | Luna B | Oracle pin and benchmark methodology | MODEL-001 | Immutable-source procedure and command manifest |
| TOOL-001 | Luna C | Tool envelope and result contract | SOL-002 | Strict schema, examples, max sizes, S4 negative fixtures |
| TOOL-002 | Luna C | Assistant event/state contract | TOOL-001, RUN-002 | Streaming/confirmation/cancel order frozen |
| TOOL-003 | Luna C | Config schema and tool-risk mapping | TOOL-001, SOL-003 | Unknown keys fail; no secrets in example |
| QA-001 | Luna D | Evidence directory/schema and common runner | SOL-001 | One command creates machine/build/test manifest |
| QA-002 | Luna D | Contract-conformance framework | QA-001 | Consumes at least one positive and negative fixture per contract |
| SEC-001 | Luna D | Threat model test inventory | QA-001, SOL-003 | Maps threats to tests/gates/owners |
| REL-001 | Luna D | Windows portable-package skeleton | QA-001 | Allowlist package contains no model/node_modules/secrets |

### Phase 0 gate tasks

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G0 | Sol | Phase 0 gate review | Fixed decisions, contracts, risks, approvals, first task claims explicit |
| DOC-001 | Sol | Publish kickoff summary | A new session can start without private chat context |

---

## Phase 1 — Scaffolding and oracle

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| RUN-004 | Luna A | Native CMake project and pinned backend integration | RUN-001, PERF-002 | Clean remote CPU build; immutable revision printed |
| RUN-005 | Luna A | `version`, `print-build-info`, `probe` | RUN-004 | Machine-readable output; no secrets |
| RUN-006 | Luna A | Loopback health/readiness server and auth fixture | RUN-002, RUN-004 | Binds 127.0.0.1 only; auth/request limit tests pass |
| RUN-007 | Luna A | Fixture backend implementation | RUN-001 | Streams deterministic synthetic tokens and supports cancel |
| MODEL-003 | Luna B | Acquire pinned official Qwen3.5-9B source and build controlled text-only high-precision/Q8 plus Q4_K_M GGUFs on Shadeform | MODEL-001, SOL-003 | Reproducible commands; source/build/artifact hashes; tensor inventory; license/scan receipts; no target download |
| MODEL-004 | Luna B | Tokenizer/chat/stop/`enable_thinking`/tool-call/hybrid-state golden fixtures | MODEL-003, PERF-002 | Reproducible fixture bundle, complete attention/recurrent state reset cases, source/build IDs |
| PERF-003 | Luna B | Build pinned upstream CPU oracle and optional Intel backend oracle profiles | PERF-002, MODEL-003 | Exact Q4 bytes load; deterministic CPU results archived; backend/profile identity explicit |
| PERF-004 | Luna B | Baseline load/memory/prefill/decode report | PERF-003 | Cold/warm metrics and raw data on defined profile |
| TOOL-004 | Luna C | Zero-dependency host skeleton and engine client | RUN-002, TOOL-002 | Runs with clean Node 24 and no node_modules |
| TOOL-005 | Luna C | Static local UI shell | TOOL-002 | Local-only assets; status/stream/stop placeholders |
| TOOL-006 | Luna C | Conversation state and fixture streaming | RUN-007, TOOL-004 | State transitions and cancel tests |
| TOOL-007 | Luna C | Strict schema validator and tool parser foundation | TOOL-001 | Positive/negative/depth/size tests |
| TOOL-008 | Luna C | `time.now` fixture tool and confirmation event | TOOL-006, TOOL-007 | Full fixture model→tool→result→answer |
| QA-003 | Luna D | Clean checkout/build test | RUN-004, TOOL-004 | Build from documented command with hashes |
| QA-004 | Luna D | Contract conformance v0.2 | RUN-006, TOOL-008 | Engine/host pass shared fixtures |
| REL-002 | Luna D | Windows cross-build and dependency scan | RUN-004, REL-001 | EXE inspected; unresolved runtime dependencies listed/fixed |
| SEC-002 | Luna D | Initial HTTP/static/path negative suite | RUN-006, TOOL-005 | Auth/origin/body/static traversal checks pass |
| QA-005 | Luna D | Fixture vertical-slice evidence | RUN-007, TOOL-008 | Start, stream, tool, stop, no orphan from fresh package |

### Phase 1 gate

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G1 | Sol | Approve controlled artifact build, source pin, oracle, and fixture vertical slice | Official source plus exact Q4/Q8 identities and reproducible evidence; no target dependency creep |

---

## Phase 2 — Real-model vertical slice

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| RUN-008 | Luna A | Model manifest/file/GGUF verification implementation | RUN-003, RUN-004, MODEL-001 | Valid model accepted; malformed corpus safely rejected before large alloc |
| RUN-009 | Luna A | Real Qwen3.5-9B load through backend adapter | RUN-008, MODEL-003 | Exact manifest-verified local path only; text-only readiness after warmup |
| RUN-010 | Luna A | Tokenization/chat-template integration | RUN-009, MODEL-004 | Golden token/prompt fixtures pass |
| RUN-011 | Luna A | Deterministic prefill/decode, hybrid attention-KV/recurrent-state lifecycle, and SSE streaming | RUN-010 | Real answer streams; complete state commits atomically; stable finish/token usage |
| RUN-012 | Luna A | Request/resource/context/EOS/stop handling | RUN-011 | Boundaries and typed errors pass |
| RUN-013 | Luna A | Basic request cancellation and cleanup | RUN-011 | Cancel prefill/decode; next turn remains correct |
| PERF-005 | Luna B | Engine vs oracle token/logit parity | RUN-011, PERF-003 | Meets specified CPU tolerance with mismatch report |
| PERF-006 | Luna B | 4K/8K component memory curves | RUN-011 | Mapped weights, attention KV, recurrent/MTP state, scratch, shared GPU memory, host, and commit measured separately |
| PERF-007 | Luna B | First product CPU benchmark | RUN-011, PERF-004 | Relative/absolute metrics with raw evidence |
| MODEL-005 | Luna B | Thinking-off/deep, multi-turn, long-prompt, and reset behavior | RUN-011 | Template control, budgets, hybrid-state isolation, long→short contamination tests, and recommended limits |
| TOOL-009 | Luna C | Host integration with real engine | RUN-011, TOOL-006 | Real streamed answer and error handling |
| TOOL-010 | Luna C | Normal/deep mode and Stop UI | TOOL-009, MODEL-005 | Mode request and cancel observable |
| TOOL-011 | Luna C | Real `system.get_info` tool | TOOL-007, TOOL-009 | Bounded non-secret output and end-to-end fixture invocation |
| QA-006 | Luna D | Independent real-model correctness | RUN-011, PERF-005 | Exact build/model verified; deterministic cases pass |
| SEC-003 | Luna D | Tampered model, auth, outbound-network, cancellation tests | RUN-008, RUN-013 | Safe failures; no engine egress |
| QA-007 | Luna D | Repeated start/stop and memory guard | RUN-009 | No orphan/leak trend; low-memory failure typed |
| REL-003 | Luna D | Real-engine package candidate | RUN-011, TOOL-009 | Portable tree runs on test environment; no weights |

### Phase 2 gate

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G2 | Sol | Approve M2 real-model slice | Real local Qwen3.5 response, CPU and hybrid-state correctness, offline core, bounded memory |

---

## Phase 3 — Sessions, cache baseline, local tools

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| RUN-014 | Luna A | Hybrid sequence-state session create/delete/reset manager | RUN-012 | Attention KV and recurrent state move/reset together; opaque IDs, bounds, isolation tests |
| RUN-015 | Luna A | Queue and one-active-generation policy | RUN-014 | Queue depth/busy/cancel behavior |
| RUN-016 | Luna A | Immutable system/tool hybrid-state prefix snapshot | RUN-014, MODEL-004 | Complete state format, exact keying/invalidation, no user cross-session state |
| RUN-017 | Luna A | Transactional cancel/reset behavior | RUN-013, RUN-014 | Cancel does not alter committed session state |
| RUN-018 | Luna A | Cache/session metrics | RUN-016 | Bytes/hits/misses/reset exposed without content |
| PERF-008 | Luna B | CPU thread/batch tuning baseline | PERF-007, RUN-015 | Designed results; safe defaults recommended |
| PERF-009 | Luna B | Prefix hit/miss latency and invalidation | RUN-016 | Correctness plus practical effect |
| PERF-010 | Luna B | Low-memory 4K profile | PERF-006 | Default reductions and start/refuse thresholds |
| TOOL-012 | Luna C | Session/history/context-budget controller | TOOL-009, RUN-014 | Multi-session/reset/truncation tests |
| TOOL-013 | Luna C | Confirmation framework | TOOL-002, TOOL-003 | ID-bound, expiring, deny/approve-once/session |
| TOOL-014 | Luna C | `fs.list`, `fs.read_text`, `fs.search_text` | TOOL-007, TOOL-013 | Workspace bounds, truncation, path tests |
| TOOL-015 | Luna C | `fs.write_new` and `fs.apply_patch` | TOOL-013, TOOL-014 | Diff/base hash/atomic/confirmation |
| TOOL-016 | Luna C | Clipboard tools | TOOL-013 | Bounded, privacy-labeled, confirmation behavior |
| TOOL-017 | Luna C | `app.open` and `browser.open_url` | TOOL-013 | Logical allowlist, URL/argument checks |
| QA-008 | Luna D | Session/cache canary suite | RUN-014, RUN-016 | No cross-session/state leak |
| SEC-004 | Luna D | Filesystem/reparse/ADS/TOCTOU suite | TOOL-014, TOOL-015 | No root escape or unsafe overwrite |
| SEC-005 | Luna D | Confirmation/state/event adversarial suite | TOOL-013 | No replay/mismatch/double execution |
| QA-009 | Luna D | Offline local-tool integration | TOOL-017 | All local tools pass with network disabled |

### Phase 3 gate

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G3 | Sol | Approve M3 local assistant | Stable sessions, safe local tools, confirmed mutations, cache baseline |

---

## Phase 4 — Autonomous tool calls and web providers

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| RUN-019 | Luna A | Tool-role chat messages | RUN-010, MODEL-004 | Prompt/tool result parity |
| RUN-020 | Luna A | Qwen3.5 structured tool-call normalization and grammar/schema interface | RUN-019, TOOL-001 | Template-versioned call parsing; partial JSON hidden; bounded grammar; strict complete-object fallback |
| RUN-021 | Luna A | Multi-call same-turn continuation | RUN-020, RUN-014 | Repeated tool loop context/cancel correct |
| MODEL-006 | Luna B | Tool-call quality corpus and labels | MODEL-002, TOOL-014 | Reviewed scoring and fixed test split |
| MODEL-007 | Luna B | Prompt/tool-bundle/sampling tuning | MODEL-006, RUN-020 | Measured selection/args/no-tool gains |
| MODEL-008 | Luna B | Q4 vs higher-precision tool-quality retention | MODEL-006 | Effect sizes/intervals/category breakdown |
| TOOL-018 | Luna C | Autonomous tool controller | RUN-020, TOOL-013 | Model call → validate → confirm → execute → continue |
| TOOL-019 | Luna C | Grammar generator for active schemas | TOOL-007, RUN-020 | Schema edge cases and size limits |
| TOOL-020 | Luna C | One-repair malformed-call path | TOOL-018 | No execution before valid repair; one-attempt cap |
| TOOL-021 | Luna C | `process.run_allowlisted` | TOOL-013 | No shell; timeout/output/job cleanup |
| TOOL-022 | Luna C | Provider abstraction and disabled/mock adapters | TOOL-003 | Offline/unconfigured typed behavior |
| TOOL-023 | Luna C | Approved search/fetch adapter | TOOL-022, SOL-003 | Injected config, TLS/redirect/size/egress policy |
| TOOL-024 | Luna C | Web-untrusted-content and egress UX | TOOL-023 | Destination/data confirmation and prompt boundary |
| QA-010 | Luna D | Autonomous tool-call functional suite | TOOL-018, MODEL-006 | Selection/args/execution/no-tool metrics recomputed |
| SEC-006 | Luna D | Parser/schema/grammar adversarial suite | RUN-020, TOOL-019 | No malformed execution or grammar escape |
| SEC-007 | Luna D | Process injection/orphan suite | TOOL-021 | No shell/arg escape; process tree killed |
| SEC-008 | Luna D | Web URL/redirect/private-IP/prompt-injection suite | TOOL-023, TOOL-024 | Egress and injection controls pass |
| QA-011 | Luna D | Live provider synthetic demo evidence | TOOL-023 | Clearly live, provider-injected, no sensitive data |
| QA-012 | Luna D | Offline web-failure integration | TOOL-022 | Core assistant continues; no hidden fallback |

### Phase 4 gate

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G4 | Sol | Approve M4 autonomous tool system | Quality threshold, policy enforcement, optional live search, offline preservation |

---

## Phase 5 — Performance and acceleration

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| PERF-011 | Luna B | Exact Intel target receipt analysis and backend candidate decision | PERF-001, SOL-003 | Sol-approved CPU + Vulkan candidate profiles; SYCL explicitly experimental or rejected |
| RUN-022 | Luna A | CPU ISA build/runtime selection | PERF-008 | Correct binary selected; baseline fallback |
| RUN-023 | Luna A | Safe bounded attention-KV/recurrent-state/prefix cache manager | RUN-016, PERF-009 | Complete-state budget/LRU/invalidation/isolation; no partial restore |
| PERF-012 | Luna B | KV type evaluation | RUN-023 | F16/Q8 quality, speed, memory; accepted default |
| RUN-024 | Luna A | Intel Vulkan conservative/tuned integration plus optional experimental SYCL build | PERF-011 | Same adapter/API; actual operator placement; clean-init CPU fallback; corrupt profile quarantine |
| PERF-013 | Luna B | Intel Vulkan offload/FA/cooperative-matrix/batch/cache tuning; SYCL comparison | RUN-024 | Paired correctness/performance/memory; no corruption/device loss/silent fallback |
| PERF-014 | Luna B | Shader/autotune cache | PERF-013 | Signature key, invalidation, no content |
| PERF-015 | Luna B | Final 4K/8K/optional16K CPU/Vulkan/experimental-SYCL matrix | PERF-012, PERF-013 | Cold/warm/cache p50/p95, long→short contamination, actual placement, stability |
| PERF-016 | Luna B | Contention/responsiveness tests | PERF-015 | OS/UI reserve and low-resource profile |
| TOOL-025 | Luna C | Dynamic tool bundle and prompt compaction | MODEL-007, PERF-009 | Token/TTFT reduction with quality retained |
| TOOL-026 | Luna C | Tool-result/context compaction | TOOL-018 | Bounded context; task success retained |
| TOOL-027 | Luna C | UI stream coalescing and responsiveness | TOOL-009 | Event-gap/CPU overhead gate |
| QA-013 | Luna D | CPU/Intel-backend parity, hybrid-state isolation, and corruption regression | RUN-024, RUN-023 | Independent short/long/repeated/reset/cancel/tool pass on every promoted profile |
| QA-014 | Luna D | Independent performance reproduction | PERF-015 | Same release candidate and raw evidence |
| QA-015 | Luna D | 30–60 minute performance/tool soak | TOOL-027, RUN-024 | No memory growth/corruption/orphans |

### Phase 5 gate

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G5 | Sol | Approve M5 CPU and Intel performance profiles | Relative/absolute gates, corruption-free parity, memory, tool quality, exact allowlist, independent reproduction |

---

## Phase 6 — Hardening

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| RUN-025 | Luna A | Parser/server/resource hardening | SEC findings | Sanitizer/fuzz findings closed |
| RUN-026 | Luna A | Failure recovery and stable error taxonomy | RUN-025 | Required failure matrix typed/recoverable |
| MODEL-009 | Luna B | Long-context/memory-pressure and long-prompt→short-prompt regression | PERF-015 | No corruption, stale recurrent state, unsafe allocation, or sustained paging; optional 16K documented |
| PERF-017 | Luna B | Cache/build/Intel device-driver/profile invalidation regression | PERF-014 | All signatures invalidate; known-bad combinations quarantine; no silent fallback |
| TOOL-028 | Luna C | Prompt-injection/policy hardening | SEC-008 | Adversarial corpus passes host enforcement |
| TOOL-029 | Luna C | Freeze prompts/tools/config v1.0 | TOOL-028 | Hash/version and migration notes |
| TOOL-030 | Luna C | All failure-state UX | RUN-026 | User-visible typed/recoverable messages |
| QA-016 | Luna D | Full GGUF/API/schema fuzz campaign | RUN-025, TOOL-029 | No blocker/major crash/corruption |
| QA-017 | Luna D | Network-disabled proof | RUN-026, TOOL-030 | Packet/socket evidence; local features pass |
| QA-018 | Luna D | Long soak and restart/orphan suite | RUN-026, TOOL-030 | No growth/orphan/state corruption |
| SEC-009 | Luna D | Secret/log/cache scanner | TOOL-029, RUN-026 | No prohibited content |
| SEC-010 | Luna D | Tampered release/model and downgrade tests | RUN-026 | Fail closed |
| REL-004 | Luna D | Release-candidate clean-machine matrix | All above | No missing DLL/runtime; paths with spaces/Unicode |

### Phase 6 gate

| ID | Owner | Task | Acceptance |
|---|---|---|---|
| SOL-G6 | Sol | Approve hardened release candidate | Offline/security/soak/failure matrix, no unresolved blockers/majors |

---

## Phase 7 — Release and demonstration

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| REL-005 | Luna D | Final Windows package allowlist build | SOL-G6 | Expected files only; no model/secrets/caches |
| REL-006 | Luna D | Reproducible double build | REL-005 | Byte-identical or accepted normalized diff |
| REL-007 | Luna D | SBOM, notices, license, vulnerability/malware/secret scans | REL-005 | Reports indexed; no unaccepted finding |
| REL-008 | Luna D | Release manifest and checksums | REL-005 | Every file hashed; build/source/model expectations |
| PERF-018 | Luna B | Final release performance/quality report | REL-005 | Exact release binary; required matrices |
| RUN-027 | Luna A | Final engine operator diagnostics | REL-005 | Build/model/backend/status commands stable |
| TOOL-031 | Luna C | Final UI/operator/provider docs | REL-005 | No install/download; accurate capability labels |
| QA-019 | Luna D | Scripted full Shadeform demo | All | All demo checkpoints and video/log evidence |
| DOC-002 | Sol | Known limitations and support matrix | QA-019 | Proven vs conditional vs disabled distinguished |
| REL-009 | Sol | Release decision | REL-006..QA-019 | Acceptance criteria signed |

---

## Phase 8 — Target acceptance

| ID | Owner | Task | Depends on | Acceptance |
|---|---|---|---|---|
| REL-010 | Sol + operator | Approved transfer and checksum verification | REL-009, approvals | Artifact/model receipts match |
| PERF-019 | Luna B + operator | Read-only exact Dell/Intel target hardware and performance receipt | REL-010 | Device/driver/memory, actual placement, backend selection, TTFT/prefill/decode/stability captured |
| QA-020 | Luna D + operator | Target launch acceptance script | REL-010 | Offline chat/local tool/cancel/exit pass |
| SOL-G8 | Sol | Final target acceptance decision | PERF-019, QA-020 | Accepted, limited, or rejected with exact remediation |

## Backlog rules

Sol may split tasks but preserves:

- Original acceptance criteria.
- Owner boundaries.
- Traceability to phase gate.
- Evidence requirements.

New tasks get a prefix and sequential ID. “Misc fixes” is not a valid task.
