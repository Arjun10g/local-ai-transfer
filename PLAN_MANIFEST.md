# Plan File Manifest

## Session prompts

- `sessions/00_SOL_ORCHESTRATOR.md` — paste into the GPT-5.6 Sol orchestrator.
- `sessions/01_LUNA_RUNTIME.md` — native runtime worker.
- `sessions/02_LUNA_MODEL_PERFORMANCE.md` — model, hardware, caching, and performance worker.
- `sessions/03_LUNA_AGENT_TOOLS.md` — agent host, tool, web, and UI worker.
- `sessions/04_LUNA_QA_RELEASE.md` — independent QA, security, and release worker.

## Root context

- `README.md` — launch and product overview.
- `REVISION_NOTES.md` — model/GPU revision summary and migration notes.
- `CONTEXT.md` — goal, restrictions, scope, assumptions.
- `AGENTS.md` — global execution rules.

## Governance

- `governance/CONSTRAINTS.md` — target restrictions and compliance gates.
- `governance/MODEL_DECISION.md` — one-model selection, controlled artifact build, memory, and quality policy.
- `governance/INTEL_GPU_BACKEND.md` — exact-device probe and CPU/Vulkan/SYCL promotion ladder.
- `governance/ARCHITECTURE.md` — complete technical architecture.
- `governance/SECURITY_AND_TOOL_POLICY.md` — threat model, tool tiers, egress, validation.
- `governance/INTERSESSION_PROTOCOL.md` — branch, status, handoff, contract, and review flow.
- `governance/RISK_REGISTER.md` — initial risks and contingencies.

## Execution

- `execution/PHASES.md` — nine-phase delivery program.
- `execution/TASK_BOARD.md` — initial task IDs, dependencies, owners, acceptance.
- `execution/SHADEFORM_EXECUTION.md` — remote build/test/profile/budget plan.
- `execution/TEST_AND_BENCHMARK_PLAN.md` — correctness, quality, performance, security methodology.
- `execution/REPOSITORY_LAYOUT.md` — target repository structure and ownership.
- `execution/ACCEPTANCE_CRITERIA.md` — non-negotiable release gates.
- `execution/DEMO_SCRIPT.md` — full end-to-end demonstration.

## Templates

- `templates/STATUS_PACKET.md`
- `templates/HANDOFF.md`
- `templates/ADR.md`
- `templates/TEST_EVIDENCE.md`
- `templates/MODEL_MANIFEST.md`

Start with `README.md`, then launch the five session prompts.
