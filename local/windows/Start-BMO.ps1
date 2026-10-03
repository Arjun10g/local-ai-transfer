<#
.SYNOPSIS
  Start the local assistant on this Windows machine and talk to it.

.DESCRIPTION
  -Mode chat (default) talks to the model in this window. Needs only Python.
  -Mode app  starts the assistant host as well and opens the web UI in the
             default browser. Needs Node.js 24 or 25.

  A thin wrapper over local\bmo_chat.py and local\bmo_app.py. The engine's
  token is generated per run and handed over on stdin; the engine refuses token
  files on Windows by design. Nothing the model writes is saved to disk.

  Ctrl+C while a reply is being written stops that reply (chat mode).
  /quit, or Ctrl+C at the prompt, stops the engine. In app mode Ctrl+C in
  this window stops everything.

  Before a demo, check the whole setup once (Python, Node, engine, model,
  memory, power, then a real start of the engine and the web UI host):
    .\local\windows\Test-BMO.ps1 -ModelPath <gguf> -Mode preflight

.PARAMETER EnginePath
  Use this lae-engine.exe. Without it the script uses the engine that
  Build-Engine.ps1 built for -Backend, and for cpu falls back to a prebuilt
  engine copied to local\bin\lae-engine.exe.

.PARAMETER Threads
  CPU threads for the engine. Default: every logical thread. See README
  "If it is slow".

.PARAMETER ThreadsBatch
  CPU threads for prompt processing only (default: same as -Threads).

.PARAMETER Speculate
  Experimental. Verify up to this many n-gram-drafted tokens per forward pass
  (1-8). Off by default.

.PARAMETER MaxTokens
  Chat mode only: the longest reply, in tokens (default 1024, at most 2048).
  A longer limit leaves less of the 8,192-token context for the conversation.

.PARAMETER HostConfig
  App mode only: a host config JSON, which is how the assistant is given a
  folder it may READ (a `workspaces` entry; see config.example.json). On Windows
  only the read-only fs.list / fs.read_text / fs.search_text tools exist, and only
  for folders named here. Without it the model is offered time.now and
  system.get_info only.

.PARAMETER NodePath
  App mode only: the node.exe to use, e.g. from the portable nodejs.org zip.
  Default: node.exe from PATH, never from the current folder.

.EXAMPLE
  .\local\windows\Start-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf
  .\local\windows\Start-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Mode app
  .\local\windows\Start-BMO.ps1 -ModelPath D:\models\Qwen3.5-9B-Q4_K_M.gguf -Backend intel-vulkan -VulkanDeviceName 'Intel(R) Arc(TM) Graphics'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [ValidateSet('chat', 'app')] [string] $Mode = 'chat',
    [ValidateSet('cpu', 'intel-vulkan')] [string] $Backend = 'cpu',
    [string] $VulkanDeviceName,
    [string] $EnginePath,
    [ValidateRange(1, 256)] [int] $Threads,
    [ValidateRange(1, 256)] [int] $ThreadsBatch,
    [ValidateRange(1, 8)] [int] $Speculate,
    [ValidateRange(1, 99)] [int] $GpuLayers,
    [ValidateRange(1, 2048)] [int] $MaxTokens,
    [string] $NodePath,
    [string] $HostConfig,
    [switch] $NoBrowser
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Engine resolution is the same as Test-BMO.ps1, so both scripts run the same binary.
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
$ExpectedSize = 5629109088
$ActualSize = (Get-Item -LiteralPath $Model).Length
if ($ActualSize -ne $ExpectedSize) {
    throw "Model is $ActualSize bytes; expected $ExpectedSize. The copy is incomplete or the wrong file."
}

if ($Backend -eq 'intel-vulkan' -and -not $VulkanDeviceName) {
    throw '-Backend intel-vulkan needs -VulkanDeviceName (see vulkaninfo --summary).'
}
# Checked before the model load, so a missing Node costs seconds, not a minute.
# Resolved here and handed over as an absolute path: the host gets the engine
# token in its environment, so a node.exe that happens to sit in the current
# folder must never be the one that runs. Get-Command honours a '.' or other
# relative PATH entry, so a result in the current folder is dropped.
$Node = $null
if ($Mode -eq 'app') {
    if ($NodePath) {
        $Node = Get-Command -Name $NodePath -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty Source
        if (-not $Node) { throw "No node.exe at $NodePath" }
    } else {
        $Here = (Get-Location).ProviderPath.TrimEnd('\')
        $Node = Get-Command -Name 'node' -CommandType Application -All -ErrorAction SilentlyContinue |
            Where-Object { [System.IO.Path]::IsPathRooted($_.Source) -and
                           ([System.IO.Path]::GetDirectoryName($_.Source).TrimEnd('\') -ne $Here) } |
            Select-Object -First 1 -ExpandProperty Source
        if (-not $Node) {
            throw 'The web UI needs Node.js 24 or 25, and node is not on PATH. Use -Mode chat (Python only), or pass -NodePath to a portable node.exe.'
        }
    }
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

$argv = @() + $PyArgs
if ($Mode -eq 'chat') {
    $argv += @((Join-Path $Repo 'local\bmo_chat.py'))
    if ($MaxTokens) { $argv += @('--max-tokens', $MaxTokens) }
} else {
    $argv += @((Join-Path $Repo 'local\bmo_app.py'))
    $argv += @('--node', $Node)
    if ($HostConfig) {
        if (-not (Test-Path -LiteralPath $HostConfig -PathType Leaf)) { throw "No host config at $HostConfig" }
        $argv += @('--host-config', (Resolve-Path -LiteralPath $HostConfig).Path)
    }
    if ($NoBrowser) { $argv += '--no-browser' }
}
# The engine log holds the engine's own diagnostics, never prompts or replies.
$argv += @('--engine', $Engine, '--model', $Model, '--backend', $Backend,
           '--log', (Join-Path $OutDir "engine-$Mode-$Backend-$Stamp.log"))
if ($Threads) { $argv += @('--threads', $Threads) }
if ($GpuLayers) { $argv += @('--gpu-layers', $GpuLayers) }
if ($ThreadsBatch) { $argv += @('--threads-batch', $ThreadsBatch) }
if ($Speculate) { $argv += @('--speculate', $Speculate) }
if ($Backend -eq 'intel-vulkan') { $argv += @('--vulkan-device-name', $VulkanDeviceName) }

Write-Host "== $Mode on $Backend" -ForegroundColor Cyan
& $Python @argv
exit $LASTEXITCODE
