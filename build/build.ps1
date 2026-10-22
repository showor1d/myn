param(
    [Parameter(Mandatory=$true)][string]$Iscc,
    [Parameter(Mandatory=$true)][string]$PayloadDir,
    [Parameter(Mandatory=$true)][string]$WebViewBootstrapper,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [switch]$TestMode
)
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $WebViewBootstrapper
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft') { throw 'Unverified WebView installer.' }
if (-not (Test-Path -LiteralPath (Join-Path $PayloadDir 'package-manifest.json'))) { throw 'Payload manifest missing.' }
$arguments = @(('/DPayloadDir=' + $PayloadDir), ('/DWebViewBootstrapper=' + $WebViewBootstrapper), ('/O' + $OutputDir))
if ($TestMode) { $arguments += '/DTestMode' }
$arguments += Join-Path $PSScriptRoot 'installer.iss'
& $Iscc @arguments
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
