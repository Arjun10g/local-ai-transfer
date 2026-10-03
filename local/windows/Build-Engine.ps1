<#
.SYNOPSIS
  Build lae-engine.exe on this Windows machine (ADR-0007 personal testing).

.DESCRIPTION
  Compiles the engine and the vendored llama.cpp from this repository with
  the Visual Studio 2022 C++ toolchain. Nothing is downloaded: llama.cpp is
  vendored at the pinned revision.

  This is NOT release/windows/Build-WindowsBackend.ps1. That parked enterprise
  script refuses by design until attestation-grade controls exist; this one is
  for one operator building on their own laptop.

.PARAMETER Backend
  cpu (default, works everywhere) or intel-vulkan (Intel Arc / Iris Xe GPU;
  needs the LunarG Vulkan SDK installed so VULKAN_SDK is set).

.EXAMPLE
  .\local\windows\Build-Engine.ps1
  .\local\windows\Build-Engine.ps1 -Backend intel-vulkan
#>
[CmdletBinding()]
param(
    [ValidateSet('cpu', 'intel-vulkan')] [string] $Backend = 'cpu',
    [string] $BuildDir
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $BuildDir) { $BuildDir = Join-Path $Repo "build-windows-$Backend" }
# MSBuild still hits Windows' 260-character path limit deep inside the vendored
# llama.cpp tree (the Vulkan shader generator nests several folders down), and
# the error it gives names a missing file, not the real cause.
if ($BuildDir.Length -gt 80) {
    Write-Warning "The build folder path is $($BuildDir.Length) characters long. If the build fails with a missing file or 'path too long', clone the repository to a short folder such as C:\bmo and build there."
}

function Require-Command([string] $Name, [string] $Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing prerequisite '$Name'. $Hint"
    }
}

Write-Host "== Checking prerequisites" -ForegroundColor Cyan
Require-Command 'cmake' 'Install CMake 3.20+ (winget install Kitware.CMake) and reopen PowerShell.'

# CMake needs a real Python executable at configure time; resolve it through
# the py launcher so a Microsoft Store alias cannot stand in for it.
$Python = $null
if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    $Python = (& py -3 -c 'import sys; print(sys.executable)').Trim()
} elseif (Get-Command 'python' -ErrorAction SilentlyContinue) {
    $Python = (& python -c 'import sys; print(sys.executable)').Trim()
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw 'Missing Python 3.10+. Install it from python.org (tick "Add to PATH") and reopen PowerShell.'
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) {
    throw 'Missing Visual Studio 2022 C++ tools. Install "Build Tools for Visual Studio 2022" with the "Desktop development with C++" workload.'
}
$vs = & $vswhere -version '[17.0,18.0)' -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -latest -property installationPath
if (-not $vs) {
    throw 'Visual Studio 2022 is installed but without the C++ workload. Add "Desktop development with C++".'
}

$cmakeArgs = @('-S', $Repo, '-B', $BuildDir, '-G', 'Visual Studio 17 2022', '-A', 'x64',
               '-DLAE_ENABLE_LLAMA_CPP=ON', "-DPython3_EXECUTABLE=$Python")
if ($Backend -eq 'intel-vulkan') {
    if (-not $env:VULKAN_SDK) {
        throw 'intel-vulkan needs the LunarG Vulkan SDK (https://vulkan.lunarg.com). Install it, reopen PowerShell, and retry.'
    }
    $cmakeArgs += '-DLAE_ENABLE_LLAMA_VULKAN=ON'
}

Write-Host "   python : $Python"
Write-Host "   msvc   : $vs"
Write-Host "   backend: $Backend"
Write-Host "   build  : $BuildDir"

Write-Host "== Configuring" -ForegroundColor Cyan
& cmake @cmakeArgs
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed (exit $LASTEXITCODE)." }

Write-Host "== Building lae-engine (this takes several minutes the first time)" -ForegroundColor Cyan
& cmake --build $BuildDir --config Release --target lae-engine --parallel
if ($LASTEXITCODE -ne 0) { throw "Build failed (exit $LASTEXITCODE)." }

$exe = Join-Path $BuildDir 'native\Release\lae-engine.exe'
if (-not (Test-Path $exe)) { throw "Build reported success but $exe is missing." }

Write-Host "== Built $exe" -ForegroundColor Green
& $exe version
Write-Host ''
Write-Host 'Next:  .\local\windows\Test-BMO.ps1 -ModelPath <path to Qwen3.5-9B-Q4_K_M.gguf>' -ForegroundColor Green
