# Planned Repository Layout

```text
local-assistant-engine/
├── AGENTS.md
├── CONTEXT.md
├── README.md
├── CMakeLists.txt
├── CMakePresets.json
├── package/
│   ├── Start-LocalAssistant.ps1
│   ├── Verify-Release.ps1
│   ├── config.example.json
│   └── README-OPERATOR.md
├── native/
│   ├── CMakeLists.txt
│   ├── engine/
│   │   ├── main.cpp
│   │   ├── build_info.*
│   │   ├── runtime_config.*
│   │   └── engine_state.*
│   ├── backend/
│   │   ├── backend.h
│   │   ├── llama_backend.*
│   │   ├── backend_config.*
│   │   ├── backend_profile.*
│   │   ├── placement_receipt.*
│   │   └── patches/
│   ├── model_validation/
│   │   ├── gguf_header.*
│   │   ├── model_manifest.*
│   │   ├── file_identity.*
│   │   └── verification_receipt.*
│   ├── server/
│   │   ├── loopback_server.*
│   │   ├── routes.*
│   │   ├── sse.*
│   │   ├── auth.*
│   │   └── request_limits.*
│   ├── session/
│   │   ├── session_manager.*
│   │   ├── sequence_state.*
│   │   ├── attention_kv_policy.*
│   │   ├── recurrent_state.*
│   │   ├── prefix_cache.*
│   │   ├── cancellation.*
│   │   └── resource_guard.*
│   ├── sampling/
│   │   ├── sampling_config.*
│   │   └── grammar_adapter.*
│   ├── telemetry/
│   │   ├── metrics.*
│   │   ├── memory_metrics.*
│   │   └── redacted_log.*
│   └── platform/
│       ├── windows/
│       └── posix/
├── third_party/
│   ├── README.md
│   ├── manifest.lock
│   ├── llama.cpp/                 # pinned/approved source or populated in remote build
│   └── licenses/
├── host/
│   ├── lae-host.mjs
│   ├── server/
│   ├── agent/
│   │   ├── controller.mjs
│   │   ├── prompt_builder.mjs
│   │   ├── context_budget.mjs
│   │   ├── tool_parser.mjs
│   │   └── grammar_builder.mjs
│   ├── policy/
│   │   ├── risk.mjs
│   │   ├── confirmation.mjs
│   │   ├── paths.mjs
│   │   ├── urls.mjs
│   │   └── egress.mjs
│   ├── tools/
│   │   ├── registry.mjs
│   │   ├── system_info.mjs
│   │   ├── time.mjs
│   │   ├── filesystem.mjs
│   │   ├── clipboard.mjs
│   │   ├── applications.mjs
│   │   ├── browser.mjs
│   │   └── process_allowlist.mjs
│   ├── providers/
│   │   ├── disabled.mjs
│   │   ├── approved_search.mjs
│   │   └── approved_fetch.mjs
│   ├── state/
│   ├── process_supervisor/
│   └── telemetry/
├── ui/
│   ├── index.html
│   ├── app.js
│   ├── app.css
│   └── assets/                    # local only
├── contracts/
│   ├── engine-api/
│   ├── assistant-events/
│   ├── tool-envelope/
│   ├── config-schema/
│   ├── model-manifest/
│   ├── metrics-schema/
│   └── error-codes/
├── model/
│   ├── README.md                  # no weights
│   ├── source-lock/
│   ├── conversion/
│   ├── quantization/
│   ├── manifests/
│   ├── tensor-inventory/
│   ├── chat-template-fixtures/
│   ├── tokenizer-fixtures/
│   ├── hybrid-state-fixtures/
│   └── quality-eval/
├── hardware/
│   ├── windows-probe/
│   ├── backend-profiles/
│   ├── placement-probe/
│   └── shadeform-profiles/
├── performance/
│   ├── harness/
│   ├── corpora/
│   ├── tuning/
│   └── reports/
├── qa/
│   ├── harness/
│   ├── fixtures/
│   ├── conformance/
│   ├── adversarial/
│   ├── fuzz/
│   ├── offline/
│   ├── soak/
│   └── clean-machine/
├── tests/
│   ├── native/
│   ├── host/
│   ├── tools/
│   ├── integration/
│   ├── security/
│   ├── performance/
│   └── release/
├── release/
│   ├── windows/
│   ├── manifests/
│   ├── sbom/
│   ├── notices/
│   ├── checksums/
│   ├── provenance/
│   └── runbooks/
├── scripts/
│   ├── build/
│   ├── model-artifact/
│   ├── test/
│   ├── benchmark/
│   ├── package/
│   └── shadeform/
├── coordination/                  # live execution blackboard
└── docs/
    ├── architecture/
    ├── operations/
    ├── security/
    └── decisions/
```

## Ownership boundaries

| Area | Primary | Required reviewers |
|---|---|---|
| `native/` | Luna A | Sol; Luna D; Luna B for performance changes |
| `model/`, `hardware/`, `performance/` | Luna B | Sol; Luna D; Luna A for runtime/operator contracts |
| `host/`, `ui/` | Luna C | Sol; Luna D |
| `qa/`, `release/` | Luna D | Sol; relevant producer |
| `contracts/` | Producer + consumer | Sol and Luna D |
| `coordination/`, ADRs | Sol | affected workers |
| `third_party/` | Luna A + Luna D | Sol |
| Root constraints/model decision | Sol | all sessions |

## Files that must never enter Git

- `*.gguf`
- Model shards/checkpoints.
- Model caches.
- KV/session caches.
- Provider credentials.
- `.env` containing values.
- AWS credential/config material.
- Browser profiles/cookies.
- Target hardware reports containing disallowed identifiers.
- Prompt/response logs from real data.
- Large raw benchmark artifacts.
- Crash dumps with model/user memory.
- Release signing secrets.

CI should enforce file-size, extension, secret, and entropy checks.

## Build outputs

Never build into source directories. Use:

```text
out/
  build/<preset>/
  test/<build-id>/
  package/<version>/
  evidence/<build-id>/
```

`out/` is ignored. Approved artifact storage receives immutable manifests and checksums.

## Configuration files

Committed:

- `config.example.json`
- Schemas.
- Safe default resource limits.
- Logical tool examples with placeholders.
- No absolute corporate paths or endpoints.

Not committed:

- `config.local.json`
- `hardware-receipt.json` unless fully redacted/approved.
- Provider config.
- Model verification receipt containing local path.
- User workspace mappings.

## Third-party source

`third_party/manifest.lock` records:

- Component.
- Immutable revision/archive hash.
- License.
- Source approval reference.
- Local patch list.
- Build flags.
- SBOM package ID.

The target release contains only the compiled approved artifacts and notices, not necessarily full third-party source unless license/policy requires it.

## Release staging allowlist

Packaging must be allowlist-driven. Only named files/directories are copied. Do not package the repository recursively.

Expected release root:

```text
local-assistant-engine-<version>-windows-x64/
  lae-engine-cpu.exe
  lae-engine-vulkan.exe           # only if promoted for an allowlisted Intel receipt
  backend-profiles.json
  lae-host.mjs
  Start-LocalAssistant.ps1
  Verify-Release.ps1
  config.example.json
  ui/
  schemas/
  licenses/
  THIRD_PARTY_NOTICES.md
  SBOM.spdx.json
  RELEASE_MANIFEST.json
  CHECKSUMS.sha256
  README-OPERATOR.md
```

No model is included.
