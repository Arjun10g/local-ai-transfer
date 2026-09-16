# Testing BMO on the Intel Dell laptop

This is the **personal-testing path** under ADR-0007: one operator, their own
Windows laptop. It is not `release/windows/`, whose scripts are the parked
enterprise path and refuse by design.

What a full run tells you, in gate terms:

| Step | Proves | Gate |
|---|---|---|
| `Build-Engine.ps1` (or the prebuilt engine) | the engine runs on Windows | L0 |
| `Test-BMO.ps1` (smoke) | it loads the model and answers, with load time and reply latency | L0, L4 |
| `Test-BMO.ps1 -Mode eval` | tool-call quality on this hardware | L1 |

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

Everything in `local\out\`. Receipts carry timings, pass/fail counts, case ids
and reason codes — **never prompts or model output**, so they are safe to share.
Engine logs (`engine-*.log`) help diagnose a failure to start.

The raw output printed by `-Mode cases` appears on screen only. Copy it
manually if you want to share it.

## Known issue: a timed-out request leaves the engine busy

If a client gives up on a reply before the engine finishes generating it, the
engine keeps working on the abandoned request and answers the **next** requests
with `HTTP 503` until it is done. This was reproduced on 2026-09-16: one request
hit its 600 s timeout and the following two were refused instantly.
(`ENGINE-ABANDONED-REQUEST-STATE-001`.)

The test script waits 30 minutes per case, which should keep this from
arising even on a slow CPU. If you do see a run of 503s, stop the engine
(Ctrl+C, or end `lae-engine.exe`) and start it again rather than retrying.

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

  The smoke receipt records the thread count the engine actually used under
  `runtime.n_threads`, and `host.logical_cpus`.
- The Intel GPU (§6) usually reads long prompts far faster than the CPU.

## Security notes

- The engine binds to **127.0.0.1 only** and requires a per-run bearer token.
- The token is generated fresh each run and passed on stdin — never on the
  command line, never in a file. On Windows the engine refuses token files by
  design, since a pathname cannot be bound to an access-control check.
- Nothing here sends data off the machine.
