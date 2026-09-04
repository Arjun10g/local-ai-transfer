[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu-safe', 'intel-sycl-experimental')]
    [string] $Backend,
    [Parameter(Mandatory = $true)]
    [string] $HardwareReceipt,
    [Parameter(Mandatory = $true)]
    [string] $ModelPath,
    [string] $Engine,
    [string] $ConfigPath,
    [string] $PythonCommand = 'py'
)

# The backend is an operator decision.  There is intentionally no catch block
# that retries with CPU when the requested accelerated backend is unavailable.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Assert-SafePathInput {
    param([string] $Value, [string] $Label)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '^(?i:\\\\|\\\\\?\\|\\\\\.\\)') { throw "$Label must be a local non-device path" }
}
Assert-SafePathInput $HardwareReceipt 'hardware receipt'
Assert-SafePathInput $ModelPath 'model'
if ($Engine) { Assert-SafePathInput $Engine 'engine' }
if ($ConfigPath) { Assert-SafePathInput $ConfigPath 'config' }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$receiptItem = Get-Item -LiteralPath $HardwareReceipt
if ($receiptItem.LinkType -or $receiptItem.PSIsContainer) { throw 'hardware receipt must be a regular non-link file' }
$receipt = $receiptItem.FullName
$modelItem = Get-Item -LiteralPath $ModelPath
if ($modelItem.LinkType -or $modelItem.PSIsContainer) { throw 'model must be a regular non-link file' }
$model = $modelItem.FullName
$planner = Join-Path $repoRoot 'scripts/windows_backend_plan.py'
$planPath = Join-Path ([IO.Path]::GetTempPath()) 'lae-windows-backend-plan.json'
& $PythonCommand $planner --backend $Backend --receipt $receipt --model-path $model --output $planPath
if ($LASTEXITCODE -ne 0) { throw 'backend capability plan refused the requested runtime' }

if ($Backend -eq 'cpu-safe') {
    if ([string]::IsNullOrWhiteSpace($Engine) -or [string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'CPU runtime requires -Engine and -ConfigPath' }
    $enginePath = (Resolve-Path -LiteralPath $Engine).Path
    $config = (Resolve-Path -LiteralPath $ConfigPath).Path
    if (-not (Test-Path -LiteralPath $enginePath -PathType Leaf)) { throw 'CPU engine is missing' }
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'CPU config is missing' }
    $token = [Environment]::GetEnvironmentVariable('LAE_ENGINE_TOKEN')
    if ([string]::IsNullOrEmpty($token)) { throw 'LAE_ENGINE_TOKEN must be supplied through the protected launch environment' }
    # Remove the token from the child environment; the native engine reads it
    # from stdin.  The requested model/backend were already hash/capability
    # checked by the planner.
    Remove-Item Env:LAE_ENGINE_TOKEN
    $token | & $enginePath serve --config $config --token-stdin
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($Engine)) { throw 'SYCL runtime requires an upstream llama-cli executable' }
$enginePath = (Resolve-Path -LiteralPath $Engine).Path
if (-not (Test-Path -LiteralPath $enginePath -PathType Leaf)) { throw 'SYCL engine is missing' }
# This is an explicit bounded upstream CLI diagnostic, not an unauthenticated
# server.  Select the SYCL device; an unavailable device is a hard failure.
& $enginePath --model $model --device SYCL0 --n-gpu-layers 99 --ctx-size 8192 --prompt '' --n-predict 1
exit $LASTEXITCODE
