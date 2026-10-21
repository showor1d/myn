param(
    [string]$Python = 'python',
    [string]$Iscc = 'ISCC.exe',
    [string]$OutputDir = ''
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $OutputDir) { $OutputDir = Join-Path $project 'release' }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$src = Join-Path $project 'src'
$dist = Join-Path $project 'dist'
$pyi = Join-Path $project 'pyi-work'
$exe = Join-Path $dist 'MYN\MYN.exe'
& $Python -m unittest discover -s (Join-Path $project 'tests') -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name MYN `
    --version-file (Join-Path $PSScriptRoot 'myn_version.txt') `
    --paths (Join-Path $src 'core') --distpath $dist --workpath $pyi `
    --specpath $PSScriptRoot (Join-Path $src 'myn_gui.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash
$clean = Join-Path $PSScriptRoot 'cleanup_legacy.ps1'
$body = Get-Content -LiteralPath $clean -Raw -Encoding UTF8
$body = $body -replace "(?<=-ne ')[A-F0-9_]{64,}(?=')", $hash
# On first build, replace the placeholder. Future builds update the one hash line.
$body = $body.Replace('__NEW_SHA256__', $hash)
Set-Content -LiteralPath $clean -Value $body -Encoding UTF8
& $Iscc ("/O" + $OutputDir) (Join-Path $PSScriptRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
