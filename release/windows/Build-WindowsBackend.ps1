[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu-safe', 'intel-sycl-experimental')]
    [string] $Backend,
    [Parameter(Mandatory = $true)]
    [string] $HardwareReceipt,
    [Parameter(Mandatory = $true)]
    [string] $ModelPath,
    [string] $BuildRoot = (Join-Path $PSScriptRoot 'build'),
    [string] $LlamaSource,
    [string] $PythonCommand = 'py',
    [switch] $AllowExperimentalSycl
)

# Build only from already-present sources/toolchains.  This script does not
# download a compiler, model, runtime, or Python package.  Backend selection is
# mandatory: failure to prove SYCL never falls through to CPU.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$releaseRoot = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$receipt = (Resolve-Path -LiteralPath $HardwareReceipt).Path
$model = (Resolve-Path -LiteralPath $ModelPath).Path

if (-not (Test-Path -LiteralPath $model -PathType Leaf)) { throw 'model must be an existing regular file' }
$modelItem = Get-Item -LiteralPath $model
if ($modelItem.Length -ne 5629109088) { throw 'model size is not the approved Q4_K_M size' }
$modelHash = (Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash.ToLowerInvariant()
if ($modelHash -ne 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b') { throw 'model SHA-256 is not the approved Q4_K_M digest' }

New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$planPath = Join-Path $BuildRoot 'backend-plan.json'
$planner = Join-Path $repoRoot 'scripts/windows_backend_plan.py'
& $PythonCommand $planner --backend $Backend --receipt $receipt --model-path $model --model-size ([string]$modelItem.Length) --model-sha256 $modelHash --output $planPath
if ($LASTEXITCODE -ne 0) { throw 'backend capability plan refused the requested build' }

function Invoke-Checked {
    param([string] $Command, [string[]] $Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "build command failed: $Command" }
}

$flags = @()
$binary = $null
if ($Backend -eq 'cpu-safe') {
    $flags = @('-S', $repoRoot, '-B', $BuildRoot, '-DLAE_ENABLE_LLAMA_CPP=ON', '-DCMAKE_BUILD_TYPE=Release')
    Invoke-Checked 'cmake' $flags
    Invoke-Checked 'cmake' @('--build', $BuildRoot, '--config', 'Release', '--target', 'lae-engine')
    $binary = Join-Path $BuildRoot 'lae-engine.exe'
} else {
    if (-not $AllowExperimentalSycl) { throw 'SYCL is experimental; pass -AllowExperimentalSycl explicitly' }
    if ([string]::IsNullOrWhiteSpace($LlamaSource)) { throw 'SYCL requires an existing pinned llama.cpp checkout' }
    $source = (Resolve-Path -LiteralPath $LlamaSource).Path
    if (-not (Test-Path -LiteralPath (Join-Path $source '.git') -PathType Container)) { throw 'llama.cpp source must be a git checkout' }
    $llamaHead = (& git -C $source rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $llamaHead -ne '3581ba0cf591b3f772fbb002de0f70e294bc0396') { throw 'llama.cpp checkout is not the pinned conversion revision' }
    foreach ($tool in @('cmake', 'ninja', 'icx')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "required preinstalled tool is missing: $tool" }
    }
    # The caller must already be in a oneAPI/Visual Studio environment.  We do
    # not source a script or download it because that would make provenance
    # and mutation boundaries opaque.
    $flags = @('-S', $source, '-B', $BuildRoot, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DGGML_SYCL=ON', '-DGGML_SYCL_TARGET=INTEL', '-DGGML_SYCL_F16=ON')
    Invoke-Checked 'cmake' $flags
    Invoke-Checked 'cmake' @('--build', $BuildRoot, '--config', 'Release')
    $binary = Join-Path $BuildRoot 'bin/llama-server.exe'
}
if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw 'requested backend build did not produce the expected executable' }

$toolchain = [ordered]@{
    cmake = [string](& cmake --version | Select-Object -First 1)
    backend = $Backend
    llama_cpp_revision = if ($Backend -eq 'intel-sycl-experimental') { $llamaHead } else { $null }
    build_flags = $flags
    executable_name = [IO.Path]::GetFileName($binary)
    model_sha256 = $modelHash
    hardware_receipt_sha256 = (Get-FileHash -LiteralPath $receipt -Algorithm SHA256).Hash.ToLowerInvariant()
    status = 'built-no-target-validation'
}
$toolchain | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $BuildRoot 'backend-build-receipt.json') -Encoding UTF8
Write-Output "Backend build complete: $Backend / $([IO.Path]::GetFileName($binary))"
