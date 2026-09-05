[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu-safe', 'intel-vulkan-conservative', 'intel-sycl-experimental')]
    [string] $Backend,
    [Parameter(Mandatory = $true)] [string] $HardwareReceipt,
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [Parameter(Mandatory = $true)] [string] $Engine,
    [string] $VulkanAttestation,
    [string] $PythonCommand = 'py'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Refuse before reading receipts, resolving Python, consuming a token, running
# the planner, or starting an engine. The accepted native launch boundary does
# not exist yet.
throw 'NOT_READY: Windows backend execution requires an accepted identity-pinned native launcher; no path was accessed and no process was started'
