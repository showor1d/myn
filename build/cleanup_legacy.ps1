param(
    [string]$Root = 'D:\MYN',
    [string]$ArchiveDir = '',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
trap {
    try { ($_ | Out-String) | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'cleanup-error.log') -Encoding UTF8 }
    catch {}
    exit 1
}

function FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}
function Assert-Child([string]$Parent, [string]$Child) {
    $prefix = $Parent.TrimEnd('\') + '\'
    if (-not $Child.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Cleanup path is outside the MYN install directory."
    }
}
function Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '')
    }
    finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}
function TreeMatches([string]$Tree, $Expected) {
    if (-not (Test-Path -LiteralPath $Tree -PathType Container)) { return $false }
    $all = @(Get-ChildItem -LiteralPath $Tree -Force -Recurse)
    if ($all.Count -ne @($Expected.PSObject.Properties).Count) { return $false }
    foreach ($entry in $all) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        $relative = $entry.FullName.Substring($Tree.Length).TrimStart('\').Replace('\', '/')
        $known = $Expected.PSObject.Properties[$relative]
        if ($null -eq $known) { return $false }
        if ($entry.PSIsContainer) {
            if ($null -ne $known.Value) { return $false }
        } else {
            if ($null -eq $known.Value -or (Sha256 $entry.FullName) -ne $known.Value) { return $false }
        }
    }
    return $true
}
function CopyAndVerify([string]$Source, [string]$Target) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Target -Recurse -Force
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        if ((Sha256 $Source) -ne (Sha256 $Target)) { throw 'Archive file verification failed.' }
    } else {
        $sourceFiles = @(Get-ChildItem -LiteralPath $Source -File -Recurse -Force)
        $targetFiles = @(Get-ChildItem -LiteralPath $Target -File -Recurse -Force)
        if ($sourceFiles.Count -ne $targetFiles.Count) { throw 'Archive tree verification failed.' }
        foreach ($file in $sourceFiles) {
            $relative = $file.FullName.Substring($Source.Length).TrimStart('\')
            $copy = Join-Path $Target $relative
            if (-not (Test-Path -LiteralPath $copy -PathType Leaf) -or
                (Sha256 $file.FullName) -ne (Sha256 $copy)) {
                throw 'Archive tree verification failed.'
            }
        }
    }
}

$rootPath = FullPath $Root
if ([System.IO.Path]::GetFileName($rootPath) -ne 'MYN') { throw 'Install directory must be named MYN.' }
$newExe = Join-Path $rootPath 'versions\0.13.2\MYN.exe'
if (-not (Test-Path -LiteralPath $newExe -PathType Leaf)) { throw 'New MYN executable is missing.' }
if ((Sha256 $newExe) -ne '46E63ABA07A07F6882D3AF6E5A2A2EE196658EC85CA45B78042895A09D3A942C') { throw 'New MYN executable is not the tested release.' }
if (Get-Process -Name MYN -ErrorAction SilentlyContinue) { throw 'Close MYN before cleanup.' }
$manifestPath = Join-Path $PSScriptRoot 'known-program-files.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Known program manifest is missing.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

$candidates = @()
foreach ($known in $manifest.root_files.PSObject.Properties) {
    $path = FullPath (Join-Path $rootPath $known.Name)
    Assert-Child $rootPath $path
    if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Sha256 $path) -eq $known.Value) {
        $candidates += [pscustomobject]@{ Relative = $known.Name; Path = $path }
    }
}
foreach ($version in @('0.13.0', '0.13.1')) {
    $relative = 'versions\' + $version
    $path = FullPath (Join-Path $rootPath $relative)
    Assert-Child $rootPath $path
    $expected = $manifest.versions.PSObject.Properties[$version].Value
    if (TreeMatches $path $expected) {
        $candidates += [pscustomobject]@{ Relative = $relative; Path = $path }
    }
}

if (-not $Apply) {
    Write-Output 'Preview: only these verified old program components would be archived and removed:'
    $candidates | ForEach-Object { Write-Output ('  ' + $_.Relative) }
    Write-Output 'Data, backups, exports, media, configuration, authorization, local models, and unknown files are untouched.'
    exit 0
}
if ($candidates.Count -eq 0) { Write-Output 'No verified obsolete program components found.'; exit 0 }

if (-not $ArchiveDir) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable; no cleanup performed.' }
    $ArchiveDir = Join-Path $env:LOCALAPPDATA ('MYN\legacy-program-archive\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$archivePath = FullPath $ArchiveDir
if ($archivePath -eq $rootPath -or $archivePath.StartsWith($rootPath + '\',
    [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Archive must be outside the MYN install directory.'
}
if (Test-Path -LiteralPath $archivePath) { throw 'Archive destination already exists.' }

$db = Join-Path $rootPath 'data\jarvis.db'
$dbBefore = if (Test-Path -LiteralPath $db -PathType Leaf) { Sha256 $db } else { $null }
New-Item -ItemType Directory -Path $archivePath -Force | Out-Null
foreach ($item in $candidates) {
    $target = Join-Path $archivePath $item.Relative
    CopyAndVerify $item.Path $target
}
if ($dbBefore -and (Sha256 $db) -ne $dbBefore) { throw 'Database changed during cleanup; no files removed.' }

$manifestLines = @('Source=' + $rootPath, 'DatabaseSHA256=' + $dbBefore)
foreach ($item in $candidates) { $manifestLines += 'Archived=' + $item.Relative }
$manifestLines | Set-Content -LiteralPath (Join-Path $archivePath 'manifest.txt') -Encoding UTF8
foreach ($item in $candidates) {
    $targetPath = FullPath $item.Path
    Assert-Child $rootPath $targetPath
    if (Test-Path -LiteralPath $targetPath) {
        Remove-Item -LiteralPath $targetPath -Recurse -Force -ErrorAction Stop
    }
}
if ($dbBefore -and (Sha256 $db) -ne $dbBefore) {
    throw 'Database changed after cleanup; inspect the archive before continuing.'
}
Write-Output ('Archived and removed ' + $candidates.Count + ' verified program components: ' + $archivePath)


