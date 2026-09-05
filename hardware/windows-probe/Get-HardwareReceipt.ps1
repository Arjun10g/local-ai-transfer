[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $OutputPath = 'hardware-receipt.json',
    [string] $VulkanInfoPath,
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string] $ExpectedVulkanInfoSha256,
    [switch] $NoSummary
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# PowerShell management providers and serializers materialize data before
# script-level byte limits can apply. Refuse before any management query,
# executable probe, input-path inspection, or receipt write.
throw 'NOT_READY: bounded native Windows hardware collector is unavailable; no path was accessed, no query or probe ran, and no output was written'
