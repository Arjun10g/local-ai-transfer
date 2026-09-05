[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu-safe', 'intel-vulkan-conservative', 'intel-sycl-experimental')]
    [string] $Backend,
    [Parameter(Mandatory = $true)] [string] $HardwareReceipt,
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [string] $BuildRoot = (Join-Path $PSScriptRoot 'build'),
    [string] $LlamaSource,
    [string] $VulkanAttestation,
    [string] $PythonCommand = 'py',
    [switch] $AllowExperimentalSycl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Refuse before input inspection, output creation, or compiler/tool execution.
# A remotely accepted build supervisor with bounded capture and complete child
# tree teardown has not been supplied.
throw 'NOT_READY: Windows backend build supervisor is unavailable; no path was accessed, no file was written, and no process was started'
