# MYN: install the GPU window (WebView2 via pywebview) for the main window.
# Run once:  powershell -ExecutionPolicy Bypass -File D:\MYN\setup_display.ps1
# Without it MYN keeps working with its classic window.
# (ASCII only on purpose: Windows PowerShell 5 misreads non-ASCII script text.)
$ErrorActionPreference = "Stop"
$py = "C:\Program Files\Python39\python.exe"
if (-not (Test-Path $py)) { Write-Host "Python not found: $py" -ForegroundColor Red; exit 1 }
Write-Host "Installing pywebview (MYN main window drawn by WebView2 on the GPU)..."
& $py -m pip install --upgrade "pywebview>=5.1,<6"
if ($LASTEXITCODE -ne 0) { Write-Host "pip failed. MYN keeps using its classic window." -ForegroundColor Yellow; exit 1 }
& $py -c "import webview; print('pywebview OK')"
if ($LASTEXITCODE -ne 0) { Write-Host "pywebview did not import. MYN keeps using its classic window." -ForegroundColor Yellow; exit 1 }
Write-Host "Done. Close MYN and open it again to use the new window." -ForegroundColor Green
Write-Host "(Set the environment variable MYN_UI=tk to go back to the classic window.)"
