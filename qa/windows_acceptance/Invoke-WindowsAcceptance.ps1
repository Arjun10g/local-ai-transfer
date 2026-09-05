[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $PackageRoot,
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [Parameter(Mandatory = $true)] [string] $HardwareReceiptPath,
    [Parameter(Mandatory = $true)] [string] $VulkanEnginePath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string] $ExpectedVulkanEngineSha256,
    [Parameter(Mandatory = $true)] [string] $OutputDirectory,
    [string] $HostConfigPath,
    [ValidateRange(1, 99)] [int] $VulkanGpuLayers = 20,
    [switch] $RunLiveChecks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Acceptance cannot manufacture evidence for deliberately absent package,
# collector, and launch paths. Refuse before input/account access, directory or
# receipt creation, process/browser/network activity, or model access.
throw 'NOT_READY: portable package, launch, and bounded hardware collection are unavailable; no path was accessed, no action ran, and no receipt was written'
