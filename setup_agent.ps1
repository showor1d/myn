# MYN background tasks (Claude Agent SDK), one-time setup.
#   1. Creates local-agent\.venv (Python 3.10 or newer)
#   2. Installs claude-agent-sdk (it brings its own Claude Code program)
#   3. Checks that the worker can load it
# Personal data, settings and MYN's own Python 3.9 are not touched.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File D:\MYN\setup_agent.ps1
# (ASCII only on purpose: Windows PowerShell 5 misreads non-ASCII script text.)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = Join-Path $Root "local-tts\.venv\Scripts\python.exe"
$Venv = Join-Path $Root "local-agent\.venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Worker = Join-Path $Root "src\core\agent_worker.py"
$uv = Get-Command uv -ErrorAction SilentlyContinue

Write-Host "[1/3] Preparing local-agent\.venv ..."
if (-not (Test-Path $Py)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Venv) | Out-Null
    if ($uv) {
        & $uv.Source venv --python 3.12 $Venv
    } elseif (Test-Path $Base) {
        & $Base -m venv $Venv
    } else {
        & py -3.12 -m venv $Venv
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Py)) { throw "Could not create a Python 3.10+ environment." }
}

Write-Host "[2/3] Installing claude-agent-sdk ..."
if ($uv) {
    & $uv.Source pip install --python $Py --upgrade "claude-agent-sdk"
} else {
    & $Py -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) { & $Py -m ensurepip --upgrade }
    & $Py -m pip install --upgrade "claude-agent-sdk"
}
if ($LASTEXITCODE -ne 0) { throw "Installing claude-agent-sdk failed. Check the network and run this script again." }

Write-Host "[3/3] Self-test ..."
& $Py $Worker --self-test
if ($LASTEXITCODE -ne 0) {
    Write-Host "Self-test failed. The line above says why." -ForegroundColor Red
    exit 1
}
Write-Host "Background tasks are ready. Restart MYN, then ask it for a bigger job." -ForegroundColor Green
