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

.PARAMETER Mode
  smoke  - start, check endpoints, one chat reply, report load time (default)
  eval   - score all 37 shipping tool-call cases
  cases  - score only -Cases, and print raw model output to this console
  serve  - start and keep running until Ctrl+C

.EXAMPLE
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode eval
  .\local\windows\Test-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode cases -Cases prod-mail-list-001
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [ValidateSet('smoke', 'eval', 'cases', 'serve')] [string] $Mode = 'smoke',
    [ValidateSet('cpu', 'intel-vulkan')] [string] $Backend = 'cpu',
    [string] $Cases,
    [string] $VulkanDeviceName,
    [string] $EnginePath,
    [ValidateRange(1, 256)] [int] $Threads,
    [ValidateRange(1, 99)] [int] $GpuLayers,
    [switch] $VerifyModelHash
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Built = Join-Path $Repo "build-windows-$Backend\native\Release\lae-engine.exe"
# The prebuilt engine is a CPU build, so it is never a stand-in for intel-vulkan.
$Prebuilt = Join-Path $Repo 'local\bin\lae-engine.exe'
if ($EnginePath) {
    if (-not (Test-Path $EnginePath)) { throw "No engine at $EnginePath" }
    $Engine = (Resolve-Path $EnginePath).Path
} elseif (Test-Path $Built) {
    $Engine = $Built
} elseif ($Backend -eq 'cpu' -and (Test-Path $Prebuilt)) {
    $Engine = $Prebuilt
} else {
    throw "No engine for '$Backend'. Run .\local\windows\Build-Engine.ps1 -Backend $Backend, or copy the prebuilt lae-engine.exe to local\bin\."
}
Write-Host "== engine: $Engine" -ForegroundColor Cyan
$Model = (Resolve-Path $ModelPath).Path

# The engine verifies the model's size and SHA-256 itself before loading and
# refuses a mismatch. A cheap size check here just gives a clearer message.
$ExpectedSize = 5629109088
$ActualSize = (Get-Item $Model).Length
if ($ActualSize -ne $ExpectedSize) {
    throw "Model is $ActualSize bytes; expected $ExpectedSize. The copy is incomplete or the wrong file."
}
if ($VerifyModelHash) {
    Write-Host '== Verifying model SHA-256 (about a minute)' -ForegroundColor Cyan
    $hash = (Get-FileHash -Algorithm SHA256 $Model).Hash.ToLowerInvariant()
    if ($hash -ne 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b') {
        throw "Model hash mismatch: $hash"
    }
    Write-Host '   hash OK' -ForegroundColor Green
}

$Python = if (Get-Command 'py' -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
$OutDir = Join-Path $Repo 'local\out'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

$Launcher = Join-Path $Repo 'local\bmo_local.py'
$argv = @()
if ($Python -eq 'py') { $argv += '-3' }
switch ($Mode) {
    'smoke' { $argv += @($Launcher, 'smoke', '--out', (Join-Path $OutDir "smoke-$Backend-$Stamp.json")) }
    'eval'  { $argv += @($Launcher, 'eval',  '--out', (Join-Path $OutDir "eval-$Backend-$Stamp.json")) }
    'cases' {
        if (-not $Cases) { throw '-Mode cases needs -Cases id1,id2' }
        $argv += @($Launcher, 'eval', '--cases', $Cases, '--show-output',
                   '--out', (Join-Path $OutDir "cases-$Backend-$Stamp.json"))
    }
    'serve' { $argv += @($Launcher, 'serve') }
}
$argv += @('--engine', $Engine, '--model', $Model, '--backend', $Backend,
           '--log', (Join-Path $OutDir "engine-$Mode-$Backend-$Stamp.log"))
if ($Threads) { $argv += @('--threads', $Threads) }
if ($GpuLayers) { $argv += @('--gpu-layers', $GpuLayers) }
if ($Backend -eq 'intel-vulkan') {
    if (-not $VulkanDeviceName) { throw '-Backend intel-vulkan needs -VulkanDeviceName (see vulkaninfo --summary).' }
    $argv += @('--vulkan-device-name', $VulkanDeviceName)
}

Write-Host "== $Mode on $Backend" -ForegroundColor Cyan
& $Python @argv
exit $LASTEXITCODE
