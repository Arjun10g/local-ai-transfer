# Copilot instructions for this repository

**On the Dell Windows laptop, your job is to run, measure and report BMO**, not to develop it.
Follow `docs/DELL_AGENT_RUNBOOK.md` step by step and fill in `docs/DELL_RESULTS_TEMPLATE.md`.
**Override:** `AGENTS.md` (the Sol/Luna protocol) does not apply to you here: no claims, branches, merges or pushes.

Rules that always apply: never print, save or commit a secret (engine token, UI token, `BMO_DELEGATE_KEY`, any
GitHub or Hugging Face token); do not modify product code, pins, hashes or the model to make a step pass, record the
failure with the exact command, exit code and first error lines; never write raw model output to a file; write
`NOT RUN: <why>` for anything you skipped. Never run `scripts/j1m_orchestrator.py` or any cloud tooling.

Windows tools are intentionally limited to `time.now`, `system.get_info` and read-only `fs.list` / `fs.read_text` /
`fs.search_text` on configured folders; the absence of write, app, browser, process and clipboard tools is by design.
