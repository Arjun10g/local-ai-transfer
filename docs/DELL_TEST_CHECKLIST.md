# What must be tested on the Dell

Maintained list. **Nothing in this program has ever run on real Windows.** Every
claim below is "unverified" until a line here is ticked with a date and a result.
When a feature is added or a claim changes, edit this file in the same commit.

Last updated: 2026-10-03 (after the recall-memory work, HEAD in `git log`).
Kit and commands: `local/windows/README.md`, `~/Downloads/BMO-Dell-Transfer/START-HERE.txt`.
Send back: everything in `local\out\`, plus console text where a step says so
(raw model output is never written to a file).

Result column: PASS / FAIL / WARN + the number asked for. Tick only with a result.

## A. Does it run at all (gate L0). Do these first, in order.

| # | Step | Command / action | Expect | Result |
|---|---|---|---|---|
| A0 | Model without Hugging Face (only if the USB copy is not used) | `python local\get_model.py --base-url https://github.com/Arjun10g/bmo-qwen35-9b-gguf/releases/download/v1.0 --out C:\bmo-transfer --delete-parts` | ends `OK: ... sha256 c654bc400fa0...`; verified on the Mac against the real release (3 parts, 3:42 to download, byte-identical to the original) | |
| A1 | Model copy | `Get-FileHash` on the .gguf | 5,629,109,088 bytes, sha256 `c654bc40…873b` | |
| A2 | Prebuilt MinGW engine starts | `Test-BMO.ps1 -Mode preflight -VerifyModelHash` | every line PASS/WARN, ends `READY` | |
| A3 | If A2 fails: VS build | `Build-Engine.ps1`, then A2 again | engine prints version | |
| A4 | Does Defender/SmartScreen block the unsigned exe | note any prompt | no block, or the exact message | |
| A5 | Smoke | `Test-BMO.ps1` | `"passed": true`; note `model_load_seconds`, `chat.seconds` | |
| A6 | Terminal chat | `Start-BMO.ps1`; ask 3 questions, `/stats` | answers stream; `/stats` shows reuse on turn 2 | |
| A7 | Ctrl+C mid-reply, `/reset`, `/quit` | by hand | returns to prompt; engine process gone (Task Manager) | |
| A8 | Engine token never on a command line | `Get-CimInstance Win32_Process` while running | token absent from `CommandLine` | |
| A9 | Loopback only | `netstat -ano` for the engine port | bound to 127.0.0.1 only | |

## B. Speed (gate L4). Plugged in, Power mode = Best performance.

| # | Step | Expect / why | Result |
|---|---|---|---|
| B1 | `-Mode bench` default threads | baseline: prefill tok/s, decode tok/s. Estimate to judge against: decode 6-9, prefill 30-60; **above 11 means the estimate is wrong, below 4 means a thread/power problem** | |
| B2 | Thread sweep `-Threads N` / `-ThreadsBatch M` (P-cores only vs all) | literature conflicts (P-core-only 3x faster on Alder Lake, slower on Arrow Lake): measure | |
| B3 | `-Backend intel-vulkan`, all layers offloaded | compare **output** with CPU token by token; drop if it crashes, garbles, or decodes slower. Meteor Lake Vulkan crashes are documented upstream | |
| B4 | `-Speculate 4` on tool-call prompts and on free text | acceptance rate (`/metrics` spec_accepted / spec_drafted) and tok/s. Expect 1.5-2.5x on JSON/tool calls, ~1.0x on prose (an estimate, not a measurement) | |
| B5 | Second-turn saving | turn 2 `prompt_tokens` vs `last_reused_prefix_tokens` / `last_restored_snapshot_tokens` | |
| B6 | Memory + battery | RSS of the engine while idle and while generating | |
| B7 | Idle unload (`-IdleUnloadMinutes 1`): RSS drops by ~5-6 GB after the quiet minute, then the next message **reloads under the Windows share-deny file handle**. Time the reload (page cache warm vs cold). Windows path of the lease is untested | reload works, conversation snapshot restored (`last_restored_snapshot_tokens` > 0) | |
| B8 | Two conversations on one engine (`-Snapshots 4`, the default): UI tab A, then tab B, then tab A again | A's next turn restores its snapshot (`/metrics` `snapshot_count` 2, `last_restored_snapshot_tokens` > 0), not a full re-read | |

## C. Tool-call quality on this hardware (gate L1)

| # | Step | Expect | Result |
|---|---|---|---|
| C1 | `-Mode cases` on `prod-mail-read-state-001,prod-mail-list-001,prod-fs-patch-001` | the A100 run passes the first two; `fs-patch` fails there every time. Does CPU Q4 behave the same? | |
| C2 | `-Mode eval` (37 cases, hours) | A100 got 36/37 three times; CPU should match if kernels are equivalent. Anything below ~34 means a CPU/Vulkan numerical difference | |

## D. Memory and long conversations (the user's MUST)

Recall memory is **on by default** (`memory.mode: "recall"`: no extra engine call). The model-written note is `"summary"`, off by default.

| # | Step | Expect | Result |
|---|---|---|---|
| D1 | `-Mode longctx` small first | previous A100 result 118/120 within the window | |
| D2 | Long chat by hand: state a name, a number and a file value in turns 1-3, then chat ~40 turns (paste a long text a few times), then ask for them again | recall block appears (`memory_recall` metric in the UI/log) and the answer is right | |
| D3 | Ask about something **never said** | answers "I don't know" instead of using a nearby line (the anchor rule) | |
| D4 | Correct a value, push both out of the window, ask for it | the corrected value | |
| D5 | Time cost of recall | turn latency with and without a recall block (it adds ≤ ~300 prompt tokens, no extra call) | |
| D6 | `memory.mode: "summary"`: how long does ONE note take on this CPU | A100: ~4 s. Estimate here: **minutes**. If > 5 min the note is not worth turning on by default | |
| D7 | Prefix reuse still works after a recall block | `/stats` on the turn after one | |
| D8 | Ask about a fact using words that share only the project name ("what did that billing file come to?") | the block still contains it (offline: yes at 200-800 turns; real model with this wording is NOT yet measured, arm `recall_gap`) | |
| D9 | Ask a list question over many facts ("every on-call engineer I told you about") | up to 12 lines come back; with more than 12 facts of that kind some are missing: a stated limit | |

## E. Web UI and safety on Windows

| # | Step | Expect | Result |
|---|---|---|---|
| E1 | `Start-BMO.ps1 -Mode app` | UI loads once; reload fails (nonce is single use); the URL fragment is cleared | |
| E2 | Tools offered | only `time.now`, `system.get_info` and read-only fs for configured workspaces. fs write, app, browser, process, clipboard are OFF on win32 | |
| E3 | Read-only fs: path traversal, `..`, junctions, symlinks, reserved names (`CON`, `NUL`), ADS (`file:stream`), 8.3 short names, UNC paths | all refused. This is untested Windows-specific code | |
| E4 | Confirmation card for a sensitive read | Approve / Deny works; a late click after expiry is refused | |
| E5 | Prompt injection: ask it to read a file containing "ignore previous instructions and ..." | no unrequested tool call | |
| E6 | Action journal | self-blocks on win32 by design: confirm the message is clear, not a crash | |

## F. Copilot delegation (off by default). Only after A-E pass.

| # | Step | Expect | Result |
|---|---|---|---|
| F1 | `Start-BMO.ps1 -EnableDelegation`, `-ShowDelegateKey` | host prints the key once; `host.json` written | |
| F2 | VS Code probe from `docs/research/COPILOT_MCP_COMPATIBILITY.md` (10 min) | `bmo_ask` listed; approval prompt appears on the laptop; denied job returns cleanly | |
| F3 | Copilot CLI ≥ 1.0.81 with `docs/copilot/copilot-cli-mcp.json` | **`${VAR}` env expansion in its config is unverified**: does the key arrive? | |
| F4 | Job while a chat is open | with `-Snapshots` >= 2 the chat's next turn should RESTORE its snapshot, not re-read its history. Verify, and time it | |
| F5 | Rotate key (`-RotateDelegateKey`), old key refused | | |
| F6 | Cloud coding agent | cannot reach a laptop: expect it NOT to work | |

## G. Decisions the results unlock

- **Turn on by default** only what passed here: recall (already on), speculation (needs B4 win + identical output), Vulkan (needs B3), delegation (needs all of F and a conscious choice: it opens a local authenticated door, even with approval on the laptop).
- Note mode (`summary`) default only if D6 is under about a minute.
- Report numbers, not impressions: the A100 figures do not transfer to this CPU.
