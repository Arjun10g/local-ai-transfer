<#
.SYNOPSIS
  Run and test the local assistant engine on this Windows machine.

.DESCRIPTION
  A thin wrapper over local\bmo_local.py, which does the real work the same way
  on Windows and macOS. The engine's token is generated per run and handed over
  on stdin; the engine refuses token files on Windows by design.

  Receipts are written to local\out\ and never contain prompts or model output.

.PARAMETER EnginePath
  Use this lae-engine.exe. Without it the script uses the engine that
  Build-Engine.ps1 built for -Backend, and for cpu falls back to a prebuilt
  engine copied to local\bin\lae-engine.exe.

.PARAMETER Threads
  CPU threads for the engine. Default: every logical thread. On Intel hybrid
  chips try the number of performance-core threads; see README "If it is slow".

.PARAMETER ThreadsBatch
  CPU threads for prompt processing only (default: same as -Threads). Reading a
  prompt is compute-bound and can use more cores than writing, which is limited
  by memory bandwidth.

.PARAMETER Speculate
  Experimental. Verify up to this many n-gram-drafted tokens per forward pass
  (1-8). Off by default. Try it with -Mode bench and compare decode speed and
  `runtime.spec_accepted` against a run without it.

.PARAMETER Mode
  smoke  - start, check endpoints, one chat reply, report load time (default)
  bench  - measure reading speed, writing speed, and what prefix reuse saves
  eval   - score all 37 shipping tool-call cases
  cases  - score only -Cases, and print raw model output to this console
  longctx - do long conversations hold up: recall, old tool results and tool
           calls at growing prompt sizes, with latency (see README section 5b)
  serve  - start and keep running until Ctrl+C; prints the endpoint, and the
           engine token only with -PrintToken
  preflight - before a demo: one PASS/WARN/FAIL line per check (Python,
           Node, engine, model, disk, memory, power, then a real start of
           the engine and of the web UI host) with a fix for each problem.
           Exits 1 if anything failed. Add -VerifyModelHash to hash the model.

.PARAMETER NodePath
  Preflight only: the node.exe to check and start the web UI host with.
  Default: node.exe from PATH, never from the current folder.

.PARAMETER PrintToken
  Serve only: also print the engine's bearer token. Off by default, because
  a console is easily recorded or shared.

.EXAMPLE
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode eval
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode cases -Cases prod-mail-list-001
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode preflight
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [ValidateSet('smoke', 'bench', 'eval', 'cases', 'longctx', 'serve', 'preflight')] [string] $Mode = 'smoke',
    [ValidateSet('cpu', 'intel-vulkan')] [string] $Backend = 'cpu',
    [string] $Cases,
    [string] $VulkanDeviceName,
    [string] $EnginePath,
    [ValidateRange(1, 256)] [int] $Threads,
    [ValidateRange(1, 99)] [int] $GpuLayers,
    [ValidateRange(1, 256)] [int] $ThreadsBatch,
    [ValidateRange(1, 8)] [int] $Speculate,
    [switch] $VerifyModelHash,
    [string] $NodePath,
    [switch] $PrintToken
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Built = Join-Path $Repo "build-windows-$Backend\native\Release\lae-engine.exe"
# The prebuilt engine is a CPU build, so it is never a stand-in for intel-vulkan.
$Prebuilt = Join-Path $Repo 'local\bin\lae-engine.exe'
if ($EnginePath) {
    if (-not (Test-Path -LiteralPath $EnginePath -PathType Leaf)) { throw "No engine at $EnginePath" }
    $Engine = (Resolve-Path -LiteralPath $EnginePath).Path
} elseif (Test-Path -LiteralPath $Built) {
    $Engine = $Built
} elseif ($Backend -eq 'cpu' -and (Test-Path -LiteralPath $Prebuilt)) {
    $Engine = $Prebuilt
} else {
    throw "No engine for '$Backend'. Run .\local\windows\Build-Engine.ps1 -Backend $Backend, or copy the prebuilt lae-engine.exe to local\bin\."
}
Write-Host "== engine: $Engine" -ForegroundColor Cyan
# -LiteralPath: a folder name with [ or ] is a wildcard pattern to plain -Path.
if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) { throw "No model file at $ModelPath" }
$Model = (Resolve-Path -LiteralPath $ModelPath).Path

# The engine verifies the model's size and SHA-256 itself before loading and
# refuses a mismatch. A cheap size check here just gives a clearer message.
# Preflight checks both itself, so it can report them alongside everything else.
$ExpectedSize = 5629109088
$ActualSize = (Get-Item -LiteralPath $Model).Length
if ($Mode -ne 'preflight' -and $ActualSize -ne $ExpectedSize) {
    throw "Model is $ActualSize bytes; expected $ExpectedSize. The copy is incomplete or the wrong file."
}
if ($VerifyModelHash -and $Mode -ne 'preflight') {
    Write-Host '== Verifying model SHA-256 (about a minute)' -ForegroundColor Cyan
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Model).Hash.ToLowerInvariant()
    if ($hash -ne 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b') {
        throw "Model hash mismatch: $hash"
    }
    Write-Host '   hash OK' -ForegroundColor Green
}

$Python = if (Get-Command 'py' -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
$PyArgs = @()
if ($Python -eq 'py') { $PyArgs += '-3' }
# `python` can be the Microsoft Store placeholder, which opens the Store
# instead of running anything, and `py -3` can pick an old 3.x. Ask the
# interpreter itself, before a minute-long model load rather than after.
$PyOk = $null
try { $PyOk = & $Python @PyArgs -c 'import sys; print(sys.version_info >= (3, 10))' } catch { $PyOk = $null }
if ("$PyOk".Trim() -ne 'True') {
    throw 'Python 3.10 or newer is needed. Install it from python.org (tick "Add python.exe to PATH"), reopen PowerShell, and check with: py -3 --version'
}
$OutDir = Join-Path $Repo 'local\out'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

$Launcher = Join-Path $Repo 'local\bmo_local.py'
$argv = @() + $PyArgs
switch ($Mode) {
    'smoke' { $argv += @($Launcher, 'smoke', '--out', (Join-Path $OutDir "smoke-$Backend-$Stamp.json")) }
    'bench' { $argv += @($Launcher, 'bench', '--out', (Join-Path $OutDir "bench-$Backend-$Stamp.json")) }
    'eval'  { $argv += @($Launcher, 'eval',  '--out', (Join-Path $OutDir "eval-$Backend-$Stamp.json")) }
    'cases' {
        if (-not $Cases) { throw '-Mode cases needs -Cases id1,id2' }
        $argv += @($Launcher, 'eval', '--cases', $Cases, '--show-output',
                   '--out', (Join-Path $OutDir "cases-$Backend-$Stamp.json"))
    }
    'longctx' { $argv += @($Launcher, 'longctx', '--out', (Join-Path $OutDir "longctx-$Backend-$Stamp.json")) }
    'serve' {
        $argv += @($Launcher, 'serve')
        if ($PrintToken) { $argv += '--print-token' }
    }
    'preflight' {
        # The receipt goes to local\out\ by itself; it never holds the prompt or the reply.
        $argv += @($Launcher, 'preflight')
        if ($VerifyModelHash) { $argv += '--verify-hash' }
        if ($NodePath) {
            if (-not (Test-Path -LiteralPath $NodePath -PathType Leaf)) { throw "No node.exe at $NodePath" }
            $argv += @('--node', (Resolve-Path -LiteralPath $NodePath).Path)
        }
    }
}
$argv += @('--engine', $Engine, '--model', $Model, '--backend', $Backend,
           '--log', (Join-Path $OutDir "engine-$Mode-$Backend-$Stamp.log"))
if ($Threads) { $argv += @('--threads', $Threads) }
if ($GpuLayers) { $argv += @('--gpu-layers', $GpuLayers) }
if ($ThreadsBatch) { $argv += @('--threads-batch', $ThreadsBatch) }
if ($Speculate) { $argv += @('--speculate', $Speculate) }
if ($Backend -eq 'intel-vulkan') {
    if (-not $VulkanDeviceName) { throw '-Backend intel-vulkan needs -VulkanDeviceName (see vulkaninfo --summary).' }
    $argv += @('--vulkan-device-name', $VulkanDeviceName)
}

Write-Host "== $Mode on $Backend" -ForegroundColor Cyan
& $Python @argv
exit $LASTEXITCODE
