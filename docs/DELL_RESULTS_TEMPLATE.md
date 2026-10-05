# Dell results (fill every row: a number, PASS/FAIL/WARN, or `NOT RUN: <why>`)

Copy to `local\out\DELL_RESULTS.md`. Quote numbers exactly as printed. Never put a secret or raw model output here.
Reference values are from the A100 / Mac and are only a yardstick.

## Machine and run

| Item | Value |
|---|---|
| Date, who ran it (agent name/version) | |
| Repo commit (`git log --oneline -1`) | |
| Windows / build | |
| CPU (name, cores, logical) | |
| RAM, free disk | |
| GPU + driver | |
| Power plan / plugged in? | |
| Python / Node / Git versions | |
| Engine used (prebuilt MinGW exe / Visual Studio build / Vulkan build) and its sha256 | |
| Model file verified (size + sha256 match)? how obtained (USB / GitHub release) | |

## Gate L0: does it run (runbook section 4)

| Step | Result | Notes (numbers, first error lines) |
|---|---|---|
| 4.1 preflight READY | | |
| 4.2 smoke: passed / model_load_seconds / chat.seconds | | |
| 4.3 token not on command line | | |
| 4.4 listening on 127.0.0.1 only | | |
| 4.5 terminal chat, Ctrl+C, /reset, /quit, no stray process | | |
| Blocked by Defender/SmartScreen? message | | |

## Speed (section 5)

| Setting | decode tok/s | prefill tok/s | load s | Notes |
|---|---|---|---|---|
| default threads | | | | reference estimate: decode 6-9, prefill 30-60 |
| -Threads 4 | | | | |
| -Threads 8 | | | | |
| -Threads 12 | | | | |
| -Threads 16 | | | | |
| -Threads 8 -ThreadsBatch 16 | | | | |
| best setting | | | | |
| -Speculate 4 (decode tok/s, spec_drafted, spec_accepted, acceptance %) | | | | |
| intel-vulkan (decode, output matches CPU?) | | | | NOT RUN if no Vulkan build |
| reuse: second turn saved how many prompt tokens / seconds | | | | |

## Tool-call quality (section 6)

| Item | Result | Reference |
|---|---|---|
| prod-mail-read-state-001 | | pass |
| prod-mail-list-001 | | pass |
| prod-fs-patch-001 | | fails (omits an argument): what does it print here? |
| seconds per case | | |
| full 37-case eval: passed / 37, failing ids | | 36/37 |

## Memory and long conversations (section 7)

| Item | Result | Reference |
|---|---|---|
| longctx pass counts, timeouts, max prompt tokens | | 118/120 (A100) |
| host probe, recall mode: HOST PROBE OK? each check PASS/FAIL (number_exact, name_gap, room_corrected, absent_project) | | all PASS |
| host probe, off mode (control): which checks failed | | first three fail |
| recall turns: lines recalled, extra prompt tokens, extra seconds vs a turn without recall | | <= ~500 tokens, no extra engine call |
| slowest turn seconds in the probe | | |
| note (summary mode): minutes for one note on this CPU | | A100 ~4-10 s; "not worth default" if > ~5 min |

## Engine features (section 8)

| Item | Result | Reference |
|---|---|---|
| snapshots probe OK? A2/B2/A3 restored tokens; seconds per step | | restored 15/15/34 on the Mac |
| idle probe OK? unloaded after (s), memory_mb loaded -> idle, reload message seconds | | unload ~65 s |
| any engine log error lines (first 20) | | |

## Web UI and safety (section 9)

| Item | Result |
|---|---|
| E1 single-use link, fragment cleared | |
| E2 tools offered | |
| E3 read of notes.txt after confirmation | |
| E4 each refused path (list each: C:\Windows\win.ini, ..\ traversal, UNC, :stream, CON/NUL, 8.3 short name, junction): refused? | |
| E5 injected.txt did not trigger a tool call | |
| E6 late click on an expired card refused | |
| E7 action-journal message clear? | |
| node --test windows tests: pass / fail, first failing test | |

## Copilot delegation (section 10) (needs the user present)

| Item | Result |
|---|---|
| VS Code / Copilot Chat / `copilot --version` / plan | |
| bmo_health without a prompt | |
| bmo_ask approval card on the laptop; denied job; stopped job | |
| Copilot CLI: does `${BMO_DELEGATE_KEY}` expand from its own environment? (the unverified item) | |
| user's own next chat turn after a delegated job: restored or re-read? seconds | |

## The five most important findings

1.
2.
3.
4.
5.

## Not run / blocked, and why

-
