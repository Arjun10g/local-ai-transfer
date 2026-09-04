Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# This verifier is intentionally for the generated package emitted by
# qa.clean_machine.package_runner. The checked-in release/windows tree is a
# source skeleton and must not be represented as a finished package.
$root = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$manifestPath = Join-Path $root 'RELEASE_MANIFEST.json'
$checksumsPath = Join-Path $root 'CHECKSUMS.sha256'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'RELEASE_MANIFEST.json is missing' }
if (-not (Test-Path -LiteralPath $checksumsPath -PathType Leaf)) { throw 'CHECKSUMS.sha256 is missing' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.kind -eq 'fixture-skeleton') { throw 'checked-in source skeleton is not a generated package; run the approved package builder first' }
if ($manifest.schema_version -ne 'release-manifest.v1' -or $manifest.kind -ne 'portable-windows-x64-cpu' -or $manifest.model_included -ne $false -or $manifest.python_required_on_target -ne $false) {
    throw 'Release manifest identity is invalid'
}
$listed = @($manifest.files)
if ($listed.Count -ne (@($listed | Sort-Object -Unique)).Count) { throw 'Release manifest contains duplicate files' }
$actual = @(Get-ChildItem -LiteralPath $root -File -Recurse | ForEach-Object { $_.FullName.Substring($root.Length + 1).Replace('\', '/') } | Sort-Object)
if ((Compare-Object -ReferenceObject @($listed | Sort-Object) -DifferenceObject $actual).Count -ne 0) { throw 'Release manifest file list does not match the package' }
foreach ($relative in $listed) {
    if ($relative -notmatch '^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$' -or $relative -match '(^|/)\.\.?(?:/|$)') { throw "Unsafe release path: $relative" }
    $file = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing release file: $relative" }
    $item = Get-Item -LiteralPath $file
    if ($item.LinkType) { throw "Linked release file is forbidden: $relative" }
}
$expected = @{}
foreach ($line in Get-Content -LiteralPath $checksumsPath) {
    if ($line -notmatch '^([0-9a-f]{64})  ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)$') { throw 'CHECKSUMS.sha256 contains an invalid line' }
    if ($expected.ContainsKey($Matches[2])) { throw 'CHECKSUMS.sha256 contains a duplicate file' }
    $expected[$Matches[2]] = $Matches[1]
}
$hashFiles = @($listed | Where-Object { $_ -ne 'CHECKSUMS.sha256' })
if ((Compare-Object -ReferenceObject @($expected.Keys | Sort-Object) -DifferenceObject @($hashFiles | Sort-Object)).Count -ne 0) { throw 'Checksum file list does not match the manifest' }
foreach ($relative in $hashFiles) {
    $actualHash = (Get-FileHash -LiteralPath (Join-Path $root $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expected[$relative]) { throw "Checksum mismatch: $relative" }
}
Write-Output 'Release manifest and SHA-256 checksums verified.'
