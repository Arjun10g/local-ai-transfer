# Testing BMO on the Intel Dell laptop

This is the **personal-testing path** under ADR-0007: one operator, their own
Windows laptop. It is not `release/windows/`, whose scripts are the parked
enterprise path and refuse by design.

What a full run tells you, in gate terms:

| Step | Proves | Gate |
|---|---|---|
| `Build-Engine.ps1` (or the prebuilt engine) | the engine runs on Windows | L0 |
| `Test-BMO.ps1` (smoke) | it loads the model and answers, with load time and reply latency | L0, L4 |
| `Test-BMO.ps1 -Mode bench` | reading speed, writing speed, and what a second turn saves | L4 |
| `Test-BMO.ps1 -Mode eval` | tool-call quality on this hardware | L1 |
| `Test-BMO.ps1 -Mode preflight` | this machine is ready for a demo, right now | - |

## Quick start

Everything runs in PowerShell. The transfer folder holds `bmo.bundle`,
`lae-engine.exe`, the model and `SHA256SUMS.txt`; copy it to the laptop's own
disk first, e.g. `C:\bmo-transfer` (not a USB stick: the engine maps the
5.6 GB model on every start).

1. **Prerequisites** (details in §1): Windows 11, Git for Windows, Python
   3.10+ from python.org with "Add python.exe to PATH" ticked. Node.js 24 or
   25 only if you want the web UI; the chat needs Python alone.
2. **Get the code** as a git bundle, with line endings left alone (§2), and
   put the prebuilt engine where the scripts look for it (§3). A short folder
   such as `C:\bmo` also keeps a Visual Studio build clear of Windows' path
   length limit.

   ```powershell
   git clone -c core.autocrlf=false C:\bmo-transfer\bmo.bundle C:\bmo
   cd C:\bmo
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # this window only
   New-Item -ItemType Directory -Force local\bin | Out-Null
   Copy-Item C:\bmo-transfer\lae-engine.exe local\bin\
   ```

3. **Check the model copy.** It is not in git; it must be exactly
   5,629,109,088 bytes with the SHA-256 in §2 (`Get-FileHash` prints it in
   capitals; case does not matter). Pre-flight (step 4) checks both for you
   with `-VerifyModelHash`.

   ```powershell
   (Get-Item C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf).Length
   Get-FileHash -Algorithm SHA256 C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf
   ```

4. **Pre-flight**, before every demo (§4d). A few minutes; every line should
   say PASS or WARN, and it ends with `READY for the demo`:

   ```powershell
   .\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode preflight -VerifyModelHash
   ```

5. **Chat**: the one command (§4a). Wait for `ready`, then type; `/quit` ends it.

   ```powershell
   .\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf
   ```

6. **Web UI** instead of the terminal chat (§4a; needs Node.js 24 or 25):

   ```powershell
   .\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode app
   ```

7. **Measure and evaluate** (optional, and long on a CPU):

   ```powershell
   $M = 'C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf'
   .\local\windows\Test-BMO.ps1 -ModelPath $M -Mode bench      # speed (section 4b)
   .\local\windows\Test-BMO.ps1 -ModelPath $M -Mode longctx    # long conversations (5b), hours
   .\local\windows\Test-BMO.ps1 -ModelPath $M -Mode cases -Cases prod-mail-list-001   # a few tool-call cases (5)
   .\local\windows\Test-BMO.ps1 -ModelPath $M -Mode eval       # all 37 tool-call cases (5), hours
   ```

Receipts land in `local\out\`; they never hold prompts, replies or the
engine's token. If something refuses to start, see "If it does not start".

## What to expect

- **Loading takes over a minute.** Every start re-checks the 5.6 GB model's
  hash and maps it into memory before the engine says `ready`. The very first
  start after copying can be slower still while antivirus scans the new files.
- **The first answer can take minutes on the CPU** when the prompt is long
  (a pasted document, or the evaluation's ~5,800-token tool prompts): the
  engine reads the whole prompt before it writes a word. A short question to
  a fresh chat answers in seconds. Writing runs at single-digit tokens per
  second.
- **Later turns are faster than the first.** The chat resends the whole
  conversation every turn, but the engine keeps a saved snapshot of the
  conversation up to your previous message and restores it, so it reads only
  what is new. `/stats` in the chat shows how much was reused.
- Plugged in and set to Settings > System > Power & battery > Power mode:
  **Best performance**, the same laptop is noticeably faster than on battery.

## 1. What the laptop needs

- Windows 11, **32 GB RAM or more** (the model needs ~6 GB resident), ~15 GB free disk
- [Git for Windows](https://git-scm.com/download/win)
- Python 3.10+ from python.org, with **"Add python.exe to PATH"** ticked

Only to build the engine yourself (skip these if you use the prebuilt one, §3):

- CMake 3.20+ — `winget install Kitware.CMake`
- **Build Tools for Visual Studio 2022**, workload **"Desktop development with C++"**
- *Intel GPU only:* the [LunarG Vulkan SDK](https://vulkan.lunarg.com)

Reopen PowerShell after installing, so PATH changes apply.

## 2. Get the code and the model onto the laptop

The repository has no remote, so it travels as a git bundle made on the Mac:

```powershell
git clone -c core.autocrlf=false bmo.bundle local_assistant_engine_plan_qwen35
cd local_assistant_engine_plan_qwen35
```

`core.autocrlf=false` keeps every file byte-identical to the Mac. Without it
Git for Windows rewrites line endings, which changes the eval fixture's hash,
and the receipt's `fixture_sha256` would no longer match the A100 run's
`d3c4d457…` even though the cases are the same.

Copy the model separately (USB or network share) — it is not in git:

- file: `Qwen3.5-9B-Q4_K_M.gguf`
- size: **5,629,109,088 bytes**
- SHA-256: `c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b`

The engine checks both itself and refuses a mismatch.

If PowerShell refuses to run the scripts, allow them for this window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Clone to a short folder (the quick start uses `C:\bmo`): the Visual Studio
build of the vendored llama.cpp nests deep enough to hit Windows' 260-character
path limit from a long one, and `Build-Engine.ps1` warns when it might.

## 3. Get an engine

**Fast path: the prebuilt engine.** The transfer folder has a `lae-engine.exe`
cross-compiled on the Mac with MinGW-w64 (GCC): CPU backend, AVX2, statically
linked, so it needs no Visual Studio and no extra DLLs. Put it where the test
script looks for it:

```powershell
New-Item -ItemType Directory -Force local\bin | Out-Null
Copy-Item E:\bmo-transfer\lae-engine.exe local\bin\
Get-FileHash local\bin\lae-engine.exe   # compare with SHA256SUMS.txt in the transfer folder
```

It compiled cleanly but **has never run on Windows**. If it refuses to start,
or fails in a way the Visual Studio build does not, that is a MinGW-specific
problem, not an engine problem; build it yourself instead:

**Reference path: build with Visual Studio.**

```powershell
.\local\windows\Build-Engine.ps1
```

The first build takes several minutes. It ends by printing the engine version.
When both exist, the test script prefers the engine you built; `-EnginePath`
picks one explicitly.

## 4. Smoke test

```powershell
.\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -VerifyModelHash
```

`"passed": true` means the engine started, every endpoint answered, and the
model replied. Note `model_load_seconds` and `chat.seconds`.

## 4a. Chat with it

Talk to the model in this PowerShell window. Needs only Python:

```powershell
.\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf
```

Wait for `ready`, then type. The reply appears as it is written.

- **Ctrl+C while it is writing** stops that reply and returns to `you>`. The
  stopped reply is left out of the conversation.
- `/reset` starts over, `/system <text>` sets a system prompt, `/stats` shows
  how long the last reply took (time to first token, tokens per second,
  prompt size), `/quit` (or Ctrl+C at `you>`) stops the engine and exits.
- The engine forgets nothing it is not sent: the chat resends the whole
  conversation every turn. When it no longer fits the 8,192-token context, the
  oldest exchanges are dropped automatically and a note says so.
- A reply is at most 1,024 tokens (`-MaxTokens`, up to 2,048); a cut-off
  reply says so, and `continue` gets more. An engine built before replies could
  exceed 256 tokens is detected on the first message and the chat uses 256.
- Nothing you type and nothing the model writes is saved to disk. The engine
  log in `local\out\` holds only the engine's own diagnostics.

The same flags as `Test-BMO.ps1` apply (`-Threads`, `-ThreadsBatch`,
`-Backend intel-vulkan -VulkanDeviceName ...`, `-Speculate`, `-EnginePath`).

**The web UI** (`-Mode app`) starts the assistant host too and opens it in the
default browser, talking to the real model:

```powershell
.\local\windows\Start-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode app
```

- It needs **Node.js 24 or 25**. Without an installed Node, unzip the
  portable Windows zip from nodejs.org anywhere and add
  `-NodePath C:\path\to\node.exe`; or just use the chat above. Node is
  taken from PATH, never from the current folder, and none of your `NODE_*`
  settings (such as `NODE_OPTIONS`) reach the host.
- The link it prints works **once, within 60 seconds**. If the browser did not
  open in time, or you closed the tab, press Ctrl+C and start it again.
- Ctrl+C in the PowerShell window stops both the host and the engine.
- With the default (empty) host config the model is offered only two tools
  (`time.now`, `system.get_info`), so prompts stay short. The host applies its
  own time limits to each turn; if a long conversation on the CPU hits them,
  try `-Backend intel-vulkan`, or use the chat, which waits up to 30 minutes.

On macOS or Linux, the same programs without PowerShell:

```bash
python3 local/bmo_chat.py --engine out/build-real/native/lae-engine --model /path/to/Qwen3.5-9B-Q4_K_M.gguf
python3 local/bmo_app.py  --engine out/build-real/native/lae-engine --model /path/to/Qwen3.5-9B-Q4_K_M.gguf
```

## 4b. Measure the machine

```powershell
.\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode bench
```

Three numbers, and they decide what is worth tuning:

- `prefill.tokens_per_second` — how fast it reads a prompt. The first turn of
  a conversation waits on this.
- `decode.tokens_per_second` — how fast it writes. This is capped by memory
  bandwidth (roughly 90 GB/s on this laptop against a 5.6 GB model, so the
  ceiling is about 16/s and single digits are normal). No amount of tuning
  beats that ceiling; only a smaller model file would.
- `prefix_reuse.speedup` — how much a tool-call continuation saves by reusing
  the prompt already in the context. Expect well above 1.
- `new_user_turn.speedup` — the same for a NEW user message, which is what a
  normal conversation does every turn. The chat template re-renders earlier
  assistant messages differently from how they were generated, so the live
  context cannot serve it; the engine instead restores a snapshot it saved at
  the end of your previous message. `restored_snapshot_tokens` should be well
  above 0 and the speedup well above 1. If `restored_snapshot_tokens` is 0, the
  engine re-read the whole history, and long conversations will feel slow:
  report it.

Then try `-Threads` (see "If it is slow") and compare.

## 4c. Experimental: speculative decoding

Generating a token costs one full read of the 5.6 GB model, so a laptop is limited
by memory bandwidth, not arithmetic. Speculation guesses several upcoming tokens
from text already in the conversation (tool calls and JSON repeat names and values
from the prompt), then checks all the guesses in a single pass. A guess is kept only
if the model would have chosen the same token itself.

```powershell
.\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode bench -Speculate 4
```

It is **off by default and unproven on this hardware**. Published gains are on GPUs;
nobody has reported a number for an Intel iGPU or laptop CPU. Compare against a run
without it:

- `decode.tokens_per_second` should rise. If it does not, turn it off.
- `runtime.spec_accepted` / `runtime.spec_drafted` is the hit rate. Near zero means
  there was nothing to copy; the bench prompt is not repetitive, so also judge it on
  a real tool-calling session.
- It uses extra memory for rollback snapshots (a few hundred MB at `-Speculate 4`).

Speculation should not change the answer. If you ever see different text with and
without it for the same prompt, report it: tiny numeric differences between batch
sizes can flip a near-tie, and that is exactly why it is opt-in.

## 4d. Pre-flight before a demo

```powershell
.\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode preflight -VerifyModelHash
```

One line per check, in this order, each `PASS`, `WARN` or `FAIL`, and under
anything that is not a PASS a `fix:` line saying what to do:

- **the machine**: Python 3.10+; Node.js 24/25 (missing is only a WARN: the
  chat does not need it; the wrong version is a FAIL); the engine runs
  `lae-engine version`; the model is exactly 5,629,109,088 bytes (and, with
  `-VerifyModelHash`, has the right SHA-256); free disk; total and available
  memory; and, as WARN only, whether the laptop is on battery or on a power
  plan other than High performance.
- **the engine, for real**: it starts and loads the model, then `/healthz`,
  `/readyz`, `/version`, `/build-info` (the model and the backend you asked
  for), one tiny chat reply within `--chat-budget` seconds (300 by default),
  and the `/metrics` fields the tools read (`n_threads`, `context_tokens`,
  `snapshot_tokens`). An engine too old to report them fails here.
- **the web UI host**: with a usable Node, `lae-host.mjs` starts in fixture
  mode (no engine, no model) and its `/healthz` answers.

It ends with `READY for the demo` (exit code 0) or `NOT READY: n check(s)
failed` (exit code 1). The JSON summary is printed and saved as
`local\out\preflight-<backend>-<time>.json`: check names, statuses and timings,
never the prompt, the reply or the token. `-NodePath` checks a portable
node.exe instead of the one on PATH; the same `-Threads`, `-Backend` and
`-EnginePath` options as the other modes apply.

## 5. Tool-call evaluation

```powershell
# all 37 cases
.\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode eval

# only the three cases that still fail, with the model's raw output on screen
.\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode cases `
  -Cases prod-mail-read-state-001,prod-mail-list-001,prod-fs-patch-001
```

On an A100 the full evaluation scored **34/37**. The CPU path is slower but
should score the same or very nearly so; a large gap is itself worth reporting.

**Expect it to take a while on CPU.** Every case sends a ~5,800-token prompt
(the full tool list), and the engine reads all of it again for each case. A
laptop CPU might manage that in 2–5 minutes, so all 37 cases can take a couple
of hours. Run the three-case command first: its per-case `seconds` tell you
how long the full run will take. The script waits up to 30 minutes per case.

## 5b. Do long conversations hold up?

```powershell
.\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode longctx
```

This plants facts in a long conversation (a code word, a number, a name, a
corrected value, an old tool result, a workspace id the model must pass to a
tool call) at 10%, 50% and 90% of the way through, pads it with realistic
chat, JSON, code and tool output to about 1,000, 2,000, 4,000 and 6,000
tokens, and checks the answers by exact match. It does this in three shapes:
many short turns, one long pasted message, and an agent tool loop.

It runs the smallest size first and rewrites the receipt after every answer,
so stopping early still leaves usable data. On a laptop CPU the full grid is
a few hours; for a first look, run the direct command with fewer cells:

```powershell
py -3 local\bmo_local.py longctx --engine local\bin\lae-engine.exe `
  --model C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf --styles turns --sizes 1000,4000 `
  --out local\out\longctx-quick.json
# continue the same file later with more sizes:  ... --sizes 1000,4000,6000 --resume
```

How to read `longctx-*.json` (it holds counts and timings, never the prompts
or the model's answers):

- `summary.by_size.<size>.accuracy` — the headline: share of answers correct
  at that size. A drop as size grows is the finding. `by_probe_size`,
  `by_depth_size` and `by_style_size` say *what* degrades: early facts
  (`d0.1`) usually go first; `tool_call` failing while recall holds means the
  tool-call format, not memory, is what breaks.
- Each cell's `reason`: `missing_fact`, `stale_value` (answered the old value
  after a correction), `distractor_answer` (another project's value), or a
  tool-call code such as `argument_value_mismatch`. `coherent: false` flags an
  empty, looping or garbled answer.
- `summary.latency_by_size` — how waiting time grows. `prompt_tokens` is the
  engine's real count; `prefill_seconds_est` is total time minus the measured
  writing speed, so it is an estimate.
- `summary.tool_loop_reuse` and each cell's `reused_prefix_tokens` — whether
  the engine reused the prompt it already held. Expect reuse in the
  `tool_loop` shape and none elsewhere (see §4b).

**`context_overflow` at the largest size is expected, not a bug in the tool.**
It means the prompt plus the answer budget no longer fit the engine's context
(8,192 tokens by default), and the engine refused with HTTP 400 — that is a
finding about the limit. `request_too_large` is the same kind of finding about
the engine's request bounds (a single message over 32 KiB, over 48 KiB of
history, a body over 64 KiB, or more than 64 messages); the one-long-message
shape reaches it first. Neither is counted as a wrong answer.

## 6. Optional: the Intel GPU

```powershell
.\local\windows\Build-Engine.ps1 -Backend intel-vulkan
vulkaninfo --summary          # note the Intel deviceName
.\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf `
  -Backend intel-vulkan -VulkanDeviceName "Intel(R) Arc(TM) Graphics"
```

All 32 layers go to the GPU unless `-GpuLayers` says otherwise. Compare
`chat.seconds` against the CPU smoke test.

## 7. What to send back

Everything in `local\out\`, including the `preflight-*.json` receipt.
Receipts carry timings, pass/fail counts, case ids and reason codes — **never prompts or model output**, so they are safe to share.
Engine logs (`engine-*.log`) help diagnose a failure to start.

The raw output printed by `-Mode cases` appears on screen only. Copy it
manually if you want to share it.

## Known issue: a timed-out request leaves the engine busy

If a client gives up on a reply before the engine finishes generating it, the
engine keeps working on the abandoned request and answers the **next** chat
requests with `HTTP 409` (`busy`) until it is done. This was reproduced on
2026-09-16: one request hit its 600 s timeout and the following two were
refused instantly. (`ENGINE-ABANDONED-REQUEST-STATE-001`.) Engines built before
that fix answered `503` (`not_ready`) instead, which wrongly suggested the
engine was still loading; `503` now means only that: loading or shutting down.

The test script waits 30 minutes per case, which should keep this from
arising even on a slow CPU. If you do see a run of 409s, stop the engine
(Ctrl+C, or end `lae-engine.exe`) and start it again rather than retrying.
The terminal chat avoids it: Ctrl+C cancels the reply on the engine itself
and waits for it to go idle.

## If it is slow

Two things worth knowing before tuning anything:

- By default the engine uses **every logical CPU thread**. On Intel hybrid
  chips (Core Ultra, 12th gen and later) llama.cpp is often faster on the
  performance cores alone, because the efficiency cores hold the others back.
  Try `-Threads` with the number of performance-core threads and compare
  `chat.seconds`, or better, one case's `seconds`:

  ```powershell
  # e.g. a Core Ultra 7 165H has 6 performance cores with hyper-threading = 12
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode cases `
    -Cases prod-mail-list-001 -Threads 12
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode cases `
    -Cases prod-mail-list-001 -Threads 6
  ```

  The smoke receipt records the thread counts the engine actually used under
  `runtime.n_threads` and `runtime.n_threads_batch`, and `host.logical_cpus`.

  Reading a prompt and writing tokens usually want different counts: prompt
  processing is compute-bound (more cores help), generation is limited by memory
  bandwidth (extra cores often hurt). `-ThreadsBatch` sets the first on its own:

  ```powershell
  .\local\windows\Test-BMO.ps1 -ModelPath C:\bmo-transfer\Qwen3.5-9B-Q4_K_M.gguf -Mode bench `
    -Threads 6 -ThreadsBatch 12
  ```

  Compare `prefill.tokens_per_second` and `decode.tokens_per_second` separately.
- The Intel GPU (§6) usually reads long prompts far faster than the CPU.

## If it does not start

- **"running scripts is disabled on this system"**: run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window.
  If the scripts came out of a downloaded zip rather than the bundle, also
  `Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File`.
- **"Python 3.10 or newer is needed"**: the scripts use `py -3` when the
  Python launcher is installed, otherwise `python`. A bare `python` that opens
  the Microsoft Store is Windows' placeholder, not Python: install Python from
  python.org with "Add python.exe to PATH" ticked and reopen PowerShell.
  `py -3 --version` shows which Python runs.
- **"could not start the engine" / the engine binary check fails**: Windows
  Security may have quarantined or blocked `lae-engine.exe`, or still be
  scanning it. Look in Windows Security > Protection history, restore or allow
  it, wait a minute and retry. An exit code mentioning a missing DLL means a
  build that needs Visual Studio's runtime; use the prebuilt engine (it is
  statically linked) or build it with `Build-Engine.ps1`.
- **A build fails with a missing file or "path too long"**: clone to a short
  folder such as `C:\bmo` (see §2).
- **Odd characters instead of the model's text** when output is piped or
  redirected: the console then uses the ANSI code page, and characters it
  cannot show are replaced rather than crashing the program. On screen, in a
  normal console window, everything shows.
- Paths with spaces or brackets are fine; quote them as usual in PowerShell.

## Security notes

- The engine binds to **127.0.0.1 only** and requires a per-run bearer token.
- The token is generated fresh each run and passed on stdin — never on the
  command line, never in a file. On Windows the engine refuses token files by
  design, since a pathname cannot be bound to an access-control check.
- Nothing prints the token: `-Mode serve` prints the endpoint and prints the
  token only with `-PrintToken`, for a program on this machine that needs it.
- The web UI host is the only other process that gets the token, in its
  environment. Its `node` comes from PATH or `-NodePath`, never from the
  current folder, and it inherits no `NODE_*` settings.
- Nothing here sends data off the machine.
