# Instructions for Claude Code in this repository

**If you are running on the Dell Windows laptop** (the machine this program is being tested on): your job is to
run, measure and report. Read and follow `docs/DELL_AGENT_RUNBOOK.md` top to bottom, then fill in
`docs/DELL_RESULTS_TEMPLATE.md`. The Sol/Luna orchestration protocol in `AGENTS.md` does **not** apply to you:
no claims, no branches, no merges, no pushes, and never the paid cloud tooling.

Hard rules (full list in the runbook, section 1): never print, save or commit a secret (engine token, UI token,
`BMO_DELEGATE_KEY`, any GitHub or Hugging Face token); do not change product code, pins, hashes or the model to make a
step pass, record the failure instead; never save raw model output to a file; say `NOT RUN: <why>` for anything you
did not do.

**If you are on the Mac development machine**, ignore this file's first paragraph and follow `AGENTS.md` and the
session prompt you were given.
