[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu-safe', 'intel-vulkan-conservative', 'intel-sycl-experimental')]
    [string] $Backend,
    [Parameter(Mandatory = $true)]
    [string] $HardwareReceipt,
    [Parameter(Mandatory = $true)]
    [string] $ModelPath,
    [Parameter(Mandatory = $true)]
    [string] $Engine,
    [string] $VulkanAttestation,
    [string] $PythonCommand = 'py'
)

# The backend is an operator decision. There is intentionally no catch block
# that retries with CPU when the requested accelerated backend is unavailable.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Assert-SafePathInput {
    param([string] $Value, [string] $Label)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '^(?i:\\\\|\\\\\?\\|\\\\\.\\)') { throw "$Label must be a local non-device path" }
}
function Get-RegularNonLinkFile {
    param([string] $Value, [string] $Label)
    Assert-SafePathInput $Value $Label
    $item = Get-Item -LiteralPath $Value
    if ($item.LinkType -or $item.PSIsContainer -or (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { throw "$Label must be a regular non-link file" }
    return $item
}

$receiptItem = Get-RegularNonLinkFile $HardwareReceipt 'hardware receipt'
$modelItem = Get-RegularNonLinkFile $ModelPath 'model'
$engineItem = Get-RegularNonLinkFile $Engine 'engine'
$receipt = $receiptItem.FullName
$model = $modelItem.FullName
$enginePath = $engineItem.FullName

# The planner is included beside this runner. A Python interpreter is still an
# explicit package prerequisite; absence is a hard, clearly diagnosed stop.
$plannerPath = Join-Path $PSScriptRoot 'windows_backend_plan.py'
$plannerItem = Get-Item -LiteralPath $plannerPath -ErrorAction SilentlyContinue
if (-not $plannerItem -or $plannerItem.LinkType -or $plannerItem.PSIsContainer) {
    throw 'packaged backend planner is missing or is not a regular non-link file'
}
if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "required preinstalled Python interpreter is unavailable: $PythonCommand"
}
$plannerArguments = @($plannerItem.FullName, '--backend', $Backend, '--receipt', $receipt, '--model-path', $model)
if ($Backend -eq 'intel-vulkan-conservative') {
    if ([string]::IsNullOrWhiteSpace($VulkanAttestation)) { throw 'Vulkan requires -VulkanAttestation bound to this receipt' }
    $attestationItem = Get-RegularNonLinkFile $VulkanAttestation 'Vulkan attestation'
    $plannerArguments += @('--attestation', $attestationItem.FullName)
}
$plannerJson = (& $PythonCommand @plannerArguments | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'backend capability plan refused the requested runtime' }
try { $plan = $plannerJson | ConvertFrom-Json } catch { throw 'backend planner did not return valid JSON' }

if ($plan.schema -ne 'local_bmo.windows-backend-plan.v2' -or $plan.backend -ne $Backend) { throw 'backend planner returned a mismatched plan' }
if ($plan.planning_complete -ne $true -or $plan.launch_preconditions_verified -ne $true) { throw 'backend plan lacks verified artifact launch preconditions' }
if ($plan.execution_ready -ne $false -or $plan.execution_evidence -notmatch '^UNPROVEN-') { throw 'backend plan execution evidence state is invalid' }
if ($plan.model.path -ne $model -or $plan.model.name -ne 'Qwen3.5-9B-Q4_K_M.gguf' -or $plan.model.size_bytes -ne 5629109088 -or $plan.model.sha256 -ne 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b' -or $plan.model.artifact_verified -ne $true) {
    throw 'backend plan is not bound to the exact requested model artifact'
}
if ($plan.hardware_receipt_sha256 -ne (Get-FileHash -LiteralPath $receipt -Algorithm SHA256).Hash.ToLowerInvariant()) {
    throw 'backend plan is not bound to the supplied hardware receipt'
}

if ($Backend -eq 'cpu-safe' -or $Backend -eq 'intel-vulkan-conservative') {
    $buildInfoJson = (& $enginePath print-build-info | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'native engine build identity query failed' }
    try { $buildInfo = $buildInfoJson | ConvertFrom-Json } catch { throw 'native engine build identity was not valid JSON' }
    $expectedCompiledBackend = if ($Backend -eq 'cpu-safe') { 'llama.cpp/3581ba0c/cpu' } else { 'llama.cpp/3581ba0c/vulkan' }
    if ($buildInfo.compiled_backend -ne $expectedCompiledBackend -or $buildInfo.llama_cpp_revision -ne '3581ba0cf591b3f772fbb002de0f70e294bc0396') { throw 'native engine binary does not match the planned backend and revision' }
    if ($buildInfo.model -ne 'qwen35-9b-q4-k-m' -or $buildInfo.model_size_bytes -ne 5629109088 -or $buildInfo.model_sha256 -ne 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b') { throw 'native engine binary does not contain the pinned model identity' }
    $plannedEngineBackend = if ($Backend -eq 'cpu-safe') { 'cpu' } else { 'intel-vulkan' }
    if ($plan.runtime.engine_cli_backend -ne $plannedEngineBackend) { throw 'backend plan runtime selection is inconsistent' }

    $token = [Environment]::GetEnvironmentVariable('LAE_ENGINE_TOKEN')
    if ([string]::IsNullOrEmpty($token)) { throw 'LAE_ENGINE_TOKEN must be supplied through the protected launch environment' }
    Remove-Item Env:LAE_ENGINE_TOKEN
    $engineArguments = @('serve', '--model', $plan.model.path, '--backend', $plan.runtime.engine_cli_backend, '--context', ([string]$plan.runtime.context_tokens), '--token-stdin')
    if ($Backend -eq 'intel-vulkan-conservative') {
        $engineArguments += @('--gpu-layers', ([string]$plan.runtime.gpu_layers), '--vulkan-device-name', ([string]$plan.runtime.vulkan_device_name))
    }
    $token | & $enginePath @engineArguments
    exit $LASTEXITCODE
}

if ($plan.runtime.compiled_backend -ne 'sycl' -or $plan.runtime.device_selector -ne 'SYCL0' -or $plan.runtime.gpu_layers -ne 99 -or $plan.runtime.context_tokens -ne 8192) {
    throw 'SYCL backend plan runtime selection is inconsistent'
}
# This is an explicit bounded upstream CLI diagnostic, not an unauthenticated
# server. Select the planned device; an unavailable device is a hard failure.
& $enginePath --model $plan.model.path --device $plan.runtime.device_selector --n-gpu-layers $plan.runtime.gpu_layers --ctx-size $plan.runtime.context_tokens --prompt '' --n-predict 1
exit $LASTEXITCODE
