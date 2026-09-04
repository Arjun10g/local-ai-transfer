[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu-safe', 'intel-vulkan-conservative', 'intel-sycl-experimental')]
    [string] $Backend,
    [Parameter(Mandatory = $true)]
    [string] $HardwareReceipt,
    [Parameter(Mandatory = $true)]
    [string] $ModelPath,
    [string] $Engine,
    [string] $ConfigPath,
    [string] $VulkanAttestation,
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
if ($VulkanAttestation) { Assert-SafePathInput $VulkanAttestation 'Vulkan attestation' }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$receiptItem = Get-Item -LiteralPath $HardwareReceipt
if ($receiptItem.LinkType -or $receiptItem.PSIsContainer) { throw 'hardware receipt must be a regular non-link file' }
$receipt = $receiptItem.FullName
$modelItem = Get-Item -LiteralPath $ModelPath
if ($modelItem.LinkType -or $modelItem.PSIsContainer) { throw 'model must be a regular non-link file' }
$model = $modelItem.FullName
$planner = Join-Path $repoRoot 'scripts/windows_backend_plan.py'
$plannerArguments = @($planner, '--backend', $Backend, '--receipt', $receipt, '--model-path', $model)
if ($Backend -eq 'intel-vulkan-conservative') {
    if ([string]::IsNullOrWhiteSpace($VulkanAttestation)) { throw 'Vulkan requires -VulkanAttestation bound to this receipt' }
    $attestationItem = Get-Item -LiteralPath $VulkanAttestation
    if ($attestationItem.LinkType -or $attestationItem.PSIsContainer) { throw 'Vulkan attestation must be a regular non-link file' }
    $plannerArguments += @('--attestation', $attestationItem.FullName)
}
& $PythonCommand @plannerArguments
if ($LASTEXITCODE -ne 0) { throw 'backend capability plan refused the requested runtime' }

if ($Backend -eq 'cpu-safe' -or $Backend -eq 'intel-vulkan-conservative') {
    if ([string]::IsNullOrWhiteSpace($Engine) -or [string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'runtime requires -Engine and -ConfigPath' }
    $engineItem = Get-Item -LiteralPath $Engine
    $configItem = Get-Item -LiteralPath $ConfigPath
    if ($engineItem.LinkType -or $engineItem.PSIsContainer) { throw 'engine must be a regular non-link file' }
    if ($configItem.LinkType -or $configItem.PSIsContainer) { throw 'config must be a regular non-link file' }
    $enginePath = $engineItem.FullName
    $config = $configItem.FullName
    $token = [Environment]::GetEnvironmentVariable('LAE_ENGINE_TOKEN')
    if ([string]::IsNullOrEmpty($token)) { throw 'LAE_ENGINE_TOKEN must be supplied through the protected launch environment' }
    # Remove the token from the child environment; the native engine reads it
    # from stdin.  The requested model/backend were already hash/capability
    # checked by the planner.
    Remove-Item Env:LAE_ENGINE_TOKEN
    if ($Backend -eq 'intel-vulkan-conservative') {
        $token | & $enginePath serve --config $config --backend intel-vulkan --gpu-layers 20 --token-stdin
    } else {
        $token | & $enginePath serve --config $config --backend cpu --token-stdin
    }
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($Engine)) { throw 'SYCL runtime requires an upstream llama-cli executable' }
$engineItem = Get-Item -LiteralPath $Engine
if ($engineItem.LinkType -or $engineItem.PSIsContainer) { throw 'SYCL engine must be a regular non-link file' }
$enginePath = $engineItem.FullName
# This is an explicit bounded upstream CLI diagnostic, not an unauthenticated
# server.  Select the SYCL device; an unavailable device is a hard failure.
& $enginePath --model $model --device SYCL0 --n-gpu-layers 99 --ctx-size 8192 --prompt '' --n-predict 1
exit $LASTEXITCODE
