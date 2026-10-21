# MYN voice input, one-time setup.
#   1. Installs the microphone library (sounddevice) into local-tts\.venv
#   2. Downloads Whisper large-v3-turbo (about 1.6 GB) into local-stt\
#   3. Runs a self-test on the GPU and the default microphone
# Personal data, settings and the main Python 3.9 install are not touched.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File D:\MYN\setup_voice_input.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $Root "local-tts\.venv\Scripts\python.exe"
$ModelDir = Join-Path $Root "local-stt\whisper-large-v3-turbo"
$Worker = Join-Path $Root "src\core\stt_worker.py"

if (-not (Test-Path $Py)) {
    throw "Local speech environment not found: $Py (set up local TTS first, see AGENTS.md)."
}

Write-Host "[1/3] Installing microphone library (sounddevice) into local-tts\.venv ..."
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    & $uv.Source pip install --python $Py "sounddevice>=0.5"
} else {
    & $Py -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) { & $Py -m ensurepip --upgrade }
    & $Py -m pip install "sounddevice>=0.5"
}
if ($LASTEXITCODE -ne 0) { throw "Installing sounddevice failed." }

Write-Host "Installing the local streaming wake detector ..."
$WakeRequirements = Join-Path $Root "requirements-streaming-wake.txt"
if ($uv) {
    & $uv.Source pip install --python $Py -r $WakeRequirements
} else {
    & $Py -m pip install -r $WakeRequirements
}
if ($LASTEXITCODE -ne 0) { throw "Installing streaming wake dependencies failed." }
& $Py (Join-Path $Root "tools\setup_streaming_wake.py")
if ($LASTEXITCODE -ne 0) { throw "Streaming wake model download failed." }

Write-Host "[2/3] Downloading openai/whisper-large-v3-turbo (about 1.6 GB) to $ModelDir ..."
New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
& $Py -c "import sys; from huggingface_hub import snapshot_download; snapshot_download('openai/whisper-large-v3-turbo', local_dir=sys.argv[1], allow_patterns=['*.json', '*.safetensors', '*.txt'])" $ModelDir
if ($LASTEXITCODE -ne 0) { throw "Model download failed. Check the network and run this script again." }

Write-Host "[3/3] Self-test: loading Whisper on the GPU and opening the default microphone ..."
& $Py $Worker --self-test $ModelDir
if ($LASTEXITCODE -ne 0) {
    Write-Host "Self-test failed. The line above says why (ok=false, error/detail)." -ForegroundColor Red
    exit 1
}
Write-Host "Voice input is ready. Restart MYN, then hold Right Ctrl (or the mic button) and speak." -ForegroundColor Green
