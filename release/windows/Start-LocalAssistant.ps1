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
# The production launcher receives a random per-launch token through a
# protected handle. This fixture does not start a server or print credentials.
& $engine 'serve' '--config' $configPath
