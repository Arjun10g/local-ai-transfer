[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [ValidateSet('cpu-safe')] [string] $Backend = 'cpu-safe',
    [string] $Engine = (Join-Path $PSScriptRoot 'lae-engine-cpu.exe'),
    [string] $HostConfig,
    [switch] $NoBrowser,
    [switch] $RevealBootstrapUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Refuse before inspecting, hashing, or executing a caller/package path. A
# PowerShell pathname check cannot bind the bytes consumed by CreateProcess,
# and target-side helper compilation is prohibited.
throw 'NOT_READY: identity-pinned native Node/engine supervisor is unavailable; no path was accessed and no process was started'
