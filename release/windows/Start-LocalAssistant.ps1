Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$releaseRoot = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$configPath = Join-Path $releaseRoot 'config.local.json'
$engine = Join-Path $releaseRoot 'lae-engine-cpu.exe'
if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) {
    throw 'lae-engine-cpu.exe is missing; install no software and provide the approved build artifact.'
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'config.local.json is required; copy config.example.json and set an approved local model path.'
}
$token = [Environment]::GetEnvironmentVariable('LAE_ENGINE_TOKEN')
if ([string]::IsNullOrEmpty($token)) {
    throw 'LAE_ENGINE_TOKEN must be supplied through the protected launch environment.'
}
# Do not leak the secret into the child environment; retain it only in this
# process long enough to write the inherited stdin pipe.
Remove-Item Env:LAE_ENGINE_TOKEN -ErrorAction SilentlyContinue
# Inherit the secret through stdin; it is never present in the native process
# command line or a temporary file. The native CLI consumes this explicit
# config and defaults its backend profile to CPU.
$token | & $engine 'serve' '--config' $configPath '--token-stdin'
