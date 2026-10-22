param([Parameter(Mandatory=$true)][string]$Root, [string]$DiagnosticPath)
$ErrorActionPreference = 'Stop'
function FileDigest([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)) }
    finally { $sha.Dispose(); $stream.Dispose() }
}
try {
    $target = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($target -eq [IO.Path]::GetPathRoot($target).TrimEnd('\')) { throw 'A drive root is not an install folder.' }
    $ancestor = $target
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked install folder.' }
        $ancestor = Split-Path -Parent $ancestor
    }
    $running = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.IndexOf((Join-Path $target 'src\myn_gui.py'), [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    if ($running.Count) { throw 'MYN processes are running.' }
    if (-not (Test-Path -LiteralPath $target)) { exit 0 }
    $owned = @('src','tests','build','tools','runtime','local-stt','AGENTS.md','MYN.exe','package-manifest.json','dependency-versions.json')
    $files = @()
    $pending = New-Object 'Collections.Generic.Stack[string]'
    foreach ($name in $owned) { $path = Join-Path $target $name; if (Test-Path -LiteralPath $path) { $pending.Push($path) } }
    while ($pending.Count) {
        $path = $pending.Pop()
        $item = Get-Item -LiteralPath $path -Force
        if (-not $item.FullName.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path outside install folder.' }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked program file.' }
        if ($item.PSIsContainer) { foreach ($child in Get-ChildItem -LiteralPath $path -Force) { $pending.Push($child.FullName) } }
        elseif (-not ($item.FullName.StartsWith((Join-Path $target 'runtime') + '\', [StringComparison]::OrdinalIgnoreCase) -or $item.FullName.StartsWith((Join-Path $target 'local-stt') + '\', [StringComparison]::OrdinalIgnoreCase))) { $files += $item }
    }
    if ($files.Count) {
        $state = Join-Path $target 'self-update'
        if ((Test-Path -LiteralPath $state) -and ((Get-Item -LiteralPath $state -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked recovery folder.' }
        $backup = Join-Path $state ('installer-backup-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fffffff'))
        New-Item -ItemType Directory -Path $backup | Out-Null
        foreach ($item in $files) {
            $relative = $item.FullName.Substring($target.Length + 1)
            $copy = Join-Path $backup $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $copy) -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $copy
            if ((FileDigest $item.FullName) -ne (FileDigest $copy)) { throw 'Recovery verification failed.' }
        }
    }
    exit 0
} catch {
    if ($DiagnosticPath) {
        [IO.File]::WriteAllText($DiagnosticPath, $_.Exception.Message, (New-Object Text.UTF8Encoding($false)))
    }
    Write-Error $_
    exit 1
}
