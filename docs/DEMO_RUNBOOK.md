# BMO live demo runbook (Windows 11, Intel Core Ultra laptop)

For the presenter. Commands assume the repo is at `C:\bmo` and the transfer
folder at `C:\bmo-transfer` (see `local/windows/README.md`). Run everything in
PowerShell from `C:\bmo`. Every fact below is from the code or a recorded
receipt; anything not measured on this laptop is marked **unmeasured**.

## What this demo can honestly show

- A 9-billion-parameter model (Qwen3.5-9B, Q4_K_M) answering on this laptop,
  with no internet connection needed. The web UI shows **Network: off (local only)**.
- Two tools the model can call on Windows by default: `time.now` (the clock)
  and `system.get_info` (OS, CPU count, memory, uptime). Source:
  `host/tools/local/index.mjs` (both `always_available`).
- Optionally, read-only file tools (`fs.list`, `fs.read_text`, `fs.search_text`)
  inside ONE folder you configured yourself. Never a whole drive, never a
  network share, never a write (`host/tools/local/README.md`).
- A live status line with a running timer, a **Stop** button, a **Tools on/off**
  switch, a **Continue** button when an answer hits the length limit, **Reset**,
  and **Export** (downloads the conversation as Markdown).
- A conversation that remembers earlier turns, and survives a page reload.

## What it cannot do yet

- No shell, no terminal access, no program launching on Windows. The allowlisted
  process tool, `app.open`, `browser.open_url` and the clipboard are not
  registered on Windows (`index.mjs`, reason `unsafe_subprocess_boundary`).
  Terminal access is gate L2 and has not started (`coordination/RELEASE_GATES.md`).
- No file writes or edits on Windows (`fs.write_new`, `fs.apply_patch` stay not ready).
- No email, Teams, Copilot or web browsing: those integrations are parked.
- No confirmation card will appear: none of the tools offered on Windows changes
  anything, so none needs approval.
- Not yet proven on Windows at all: gate L0 is `NOT_STARTED` until this laptop
  produces receipts. The read-only file tools have never run on real Windows.
- No measured speed on this laptop. Nothing here says how fast it will be.
- The terminal chat (`-Mode chat`) has no tools at all; tools need the web UI.

## The numbers, and how far they go

| Claim | Status | Source |
|---|---|---|
| Tool-call evaluation 36/37 | One run on a cloud A100 GPU, all 33 eval tools, temperature 0. Two changes landed at once (tokenizer fix and scorer coercion), so it is **not a controlled result** | `coordination/RELEASE_GATES.md` L1 |
| Previous evaluation 34/37 | Same A100 setup, before both changes (the Windows README still quotes this one) | same row |
| Model load | Over a minute per start; first start after copying is slower (antivirus scan) | `local/windows/README.md` |
| First answer on this CPU | **Unmeasured.** Estimates only: CPU reads 30-60 tokens/s, writes 6-9 tokens/s | `docs/research/INFERENCE_AND_QUALITY_REVIEW_2026-10-02.md` (labelled estimate) |
| Later turns quicker (snapshot restore) | Works on the Mac at engine level (3 turns); **speed gain unmeasured**; never exercised through the web UI | `coordination/TASK_CLAIMS.md` ENGINE-TURN-BOUNDARY-SNAPSHOT-001 |
| Full web UI + real model + tool call | Once, on a swapping 8 GiB Mac: 683 s to first token, says nothing about this laptop | TASK_CLAIMS HOST-E2E-REAL-MODEL-001 |

Why the first answer is slow: before writing a word, the engine reads the whole
prompt, including the tool descriptions. The full 12-tool set is about 1,182
tokens; with the default Windows setup only 2 tools are described, so the
preamble is shorter (its exact size here is unmeasured). **Tools off** drops it.

## Night before

1. Allow scripts in this window, then check both files against the transfer folder:
   ```powershell
   cd C:\bmo
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   Get-FileHash -Algorithm SHA256 C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf
   Get-FileHash local\bin\lae-engine.exe
   ```
   The model must be 5,629,109,088 bytes with SHA-256
   `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`.
   The engine hash must match `SHA256SUMS.txt` in `C:\bmo-transfer`.
2. Full pre-flight with the hash check. Every line PASS or WARN, ending `READY for the demo`:
   ```powershell
   .\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode preflight -VerifyModelHash
   ```
3. If Windows Security blocked `lae-engine.exe`: Windows Security > Protection
   history > allow that one file, wait a minute, re-run step 2. Do the first
   start tonight, so the antivirus scan is not part of the demo.
4. Web UI only: `node --version` must print v24.x or v25.x.
5. Measure, so you can quote real numbers tomorrow (note `prefill.tokens_per_second`,
   `decode.tokens_per_second`, `new_user_turn.restored_snapshot_tokens`):
   ```powershell
   .\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode bench
   ```
6. Rehearse the scripted flow below once, end to end, with a stopwatch.
7. Pause Windows Update for the day (Settings > Windows Update), so it does not
   restart or download during the demo.
8. Optional workspace (only if you rehearse it): create `C:\bmo-demo` with a
   short `notes.txt`, and `C:\bmo-demo\host-config.json`:
   ```json
   {"version": "0.1.0", "workspace_roots": [{"id": "demo", "path": "C:\\bmo-demo", "read": true, "write": false}]}
   ```
   Start the web UI with that config:
   ```powershell
   .\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode app -HostConfig C:\bmo-demo\host-config.json
   ```

## 30 minutes before

1. Charger in. Settings > System > Power & battery > Power mode: **Best performance**.
   Laptop on a hard surface (thermals are **unmeasured**).
2. Close every other app (browsers, Teams, sync clients). Turn on Do not disturb.
3. Quick pre-flight (hash already checked last night):
   ```powershell
   .\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode preflight
   ```
4. Start the web UI and leave it running:
   ```powershell
   .\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode app
   ```
   The browser opens on its own. The link works once; the launcher says within
   60 s (the host allows 3 minutes; do not rely on the slack). Badge should read **Ready**.
5. Warm up: send `Say hello in one short sentence.` and wait for the answer.
   This pulls the 5.6 GB model into memory so your first live question does not
   also pay for disk reads (reasoning, not measured). Then press **Reset**.
6. Keep that tab. Never close it; reloading it is fine.

## Scripted flow (web UI, Tools on unless stated)

Type each prompt exactly. Read the status line aloud: it is your narration.

1. **Plain question.** `In two sentences, what is a local AI assistant?`
   Status shows *Reading your message…* and a timer; after 30 s a hint says it
   is still working. Then *Writing…* and the answer streams in.
2. **Clock tool.** `What time is it right now? Use your tools.`
   A card **Checking the time (time.now): done**, then *Reading the tool result…*,
   then the answer with date, time and offset. This exact prompt is the one that
   worked end to end on the Mac.
3. **System tool.** `What computer is this? Use your tools.`
   Card **Checking system information (system.get_info): done**. It reports
   Windows (`win32`), architecture, logical CPU count, total/free memory, uptime.
   It cannot name the brand or CPU model; say so before it is asked.
4. **Follow-up (memory).** `Is that enough memory to run you comfortably? One sentence.`
   It answers from the earlier turn. Say: *the engine restores a snapshot of the
   conversation so it only reads what is new; on this laptop the saving is not
   measured yet.*
5. **Workspace (only if configured and rehearsed).**
   `List the files in the workspace with id demo.` then
   `Read notes.txt from the workspace with id demo and summarise it in one sentence.`
   Cards **Listing files in a folder** and **Reading a file**.
6. **Long-ish prompt (shows the slow part honestly).** Paste two paragraphs of
   any plain text, then `Summarise that in three bullet points.` Point at the
   timer: the engine reads every token before writing; on a CPU that is the wait.
   If the answer stops at the length limit, press **Continue**.
7. **Stop.** `Write a 500-word story about a robot learning to cook.`
   Press **Stop** after a few lines. Status: *Stopping… the engine finishes its
   current step first.* A note reads "Stopped." Wait a few seconds before the
   next message.
8. **Tools off.** Press **Reset** first (changing the tool list mid-conversation
   likely forces a full re-read). Click **Tools on** so it reads **Tools off**, then
   `Give me three tips for a good demo, one line each.` No tool preamble, so a
   shorter prompt. Finish with **Export** to show the conversation can be saved.

**Presenting the slowness.** Do not apologise and do not fill the silence with
speed promises. Say what is true: *This is a 9-billion-parameter model on the
laptop's CPU, offline. The first answer reads the whole prompt first; the timer
is real. Nothing you type leaves this machine.* If you measured `-Mode bench`
last night, quote that number and say where it came from.

## Failure playbook

| Symptom | UI says | Do this |
|---|---|---|
| Engine loading / stopped (503) | "The model is still loading or has stopped." | Wait a minute. If it persists, Ctrl+C in PowerShell and start again |
| Busy (409) | "The engine is still finishing an earlier request (about a minute)." | Wait, send again. A run of them: restart (Ctrl+C, start again) |
| Conversation too long | "This conversation has grown too long." | Press Reset. (The host first shortens old turns itself and shows a note) |
| Model made a bad tool call | "The assistant made a bad tool request." | Rephrase, or switch Tools off for that question |
| Tool used while tools off | "The assistant tried to use a tool while tools were switched off." | Turn tools on, or rephrase |
| File outside the workspace | "BMO cannot use that file or folder." | Only the configured folder is readable; that is the point |
| Link expired / reused | "This launch link has already been used or has expired." | Use the original tab, or Ctrl+C and restart |
| New tab, no link | "This tab is not connected to BMO." | Use the original tab, or restart |
| PowerShell window closed | "Lost contact with the BMO host." | Badge shows **Offline**. Start again (about a minute of loading) |
| Answer took too long | "That took too long." | Shorter message, or Reset |
| Stream dropped | "The connection to BMO dropped while it was answering." | Check the PowerShell window, then send again |
| Reset while working | "BMO is still working on the last message, so the conversation cannot be reset yet." | Press Stop, wait, then Reset |
| Page reload | (nothing) | Fine: the tab keeps the conversation |
| Wrong answer | (nothing) | Say it is wrong, once. Do not argue with it or re-roll on stage |
| Getting slower mid-demo | (nothing) | Check charger and Power mode; likely power or heat (unmeasured) |

Error wording also mentions a *shortcut*: there is none on this laptop; it
means the `Start-BMO.ps1` command.

**Abort the demo** (fall back to slides or `-Mode chat`) if: pre-flight says
`NOT READY` 30 minutes before; the engine is not `ready` 5 minutes after start;
two restarts in a row fail; or a short question has not started answering after
5 minutes. The terminal chat needs no Node and no browser:
```powershell
.\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode chat
```
It has no tools; Ctrl+C stops a reply, `/stats` shows timings, `/quit` exits.

## Security talking points (true)

- The engine listens on 127.0.0.1 only and needs a bearer token generated
  fresh each run, handed over on stdin: never on the command line, never in a file.
- The web UI's launch link is single-use and is removed from the address bar
  before the page makes any request.
- Nothing is sent off the machine. Receipts in `local\out\` hold timings and
  counts, never prompts or answers; the conversation lives only in this tab.
- Untrusted text (a file, a tool result) is defused before the model sees it,
  so it cannot fake a chat-role switch or close a tool result early. That is
  not a cure for prompt injection in general; do not present it as one.
- Windows file access is read-only and limited to the folder you configured.
- The UI has confirmation cards that show the exact tool and what it will do
  before anything that changes something runs; on Windows today no such action
  exists, so you will not see one.

**Do not claim:**
- Never say it has shell or terminal access on Windows; it has none (gate L2 not started).
- Never say it has been tested on Windows until this laptop's receipts exist.
- Never present 36/37 as this laptop's score: it is from a cloud GPU, one run, confounded.
- No speed figures you did not measure on this laptop.
- No email, Teams, browsing, clipboard or app launching.

## What to record while demoing

- `local\out\preflight-cpu-<time>.json` from tonight and from 30 minutes before.
- `local\out\bench-cpu-<time>.json` from tonight.
- `local\out\engine-app-cpu-<time>.log` from the demo run (engine diagnostics only).
- On paper, per scripted step: the timer value at the first word and at the end,
  and whether it worked. The UI shows the timer but saves nothing.
- Any error card's `code:` line, exactly.
- How long from `Start-BMO.ps1` to the browser opening (the app mode does not print load time).
- **Export** only if you want the text: unlike receipts, it contains the conversation.

Send everything in `local\out\` back with your notes (`local/windows/README.md`, section 7).
