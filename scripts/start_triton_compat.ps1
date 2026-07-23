# Start Triton-compat Whisper HTTP server on CUDA (host :8002).
# Usage (from aranmed or 151-main root):  pwsh -File scripts/start_triton_compat.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot | Split-Path -Parent
Set-Location $Root

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { return }
        $k, $v = $line.Split("=", 2)
        $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'")
        if ($k -and -not [Environment]::GetEnvironmentVariable($k)) {
            [Environment]::SetEnvironmentVariable($k, $v, "Process")
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

Import-DotEnv (Join-Path $Root ".env")

if (-not $env:WHISPER_DEVICE) { $env:WHISPER_DEVICE = "cuda" }
$env:WHISPER_DEVICE = $env:WHISPER_DEVICE

$compat = Join-Path $Root "deploy\triton\compat_http_server.py"
if (-not (Test-Path $compat)) { throw "Missing $compat" }

Write-Host "Starting Triton compat Whisper: WHISPER_DEVICE=$($env:WHISPER_DEVICE)"
python .\deploy\triton\compat_http_server.py
