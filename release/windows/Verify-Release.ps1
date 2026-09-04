Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$manifestPath = Join-Path $root 'RELEASE_MANIFEST.json'
$checksumsPath = Join-Path $root 'CHECKSUMS.sha256'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'RELEASE_MANIFEST.json is missing' }
if (-not (Test-Path -LiteralPath $checksumsPath -PathType Leaf)) { throw 'CHECKSUMS.sha256 is missing' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
foreach ($relative in $manifest.files) {
    $file = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing release file: $relative" }
    $null = Get-FileHash -LiteralPath $file -Algorithm SHA256
}
Write-Output 'Release manifest files are present; verify CHECKSUMS.sha256 with the approved acceptance runner.'
