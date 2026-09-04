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
# The native CLI consumes this explicit config and defaults its backend profile
# to CPU when backend_profile is omitted. No model path or token is inferred.
& $engine 'serve' '--config' $configPath '--token' $token
