# Start Windows OpenAI-compat bge-m3 embed server on CUDA (port from .env).
# Usage (from aranmed root):  pwsh -File scripts/medrag/start_embed_windows.ps1
# Requires free VRAM — keep VLLM_GPU_MEM_UTIL≈0.55 on RTX 3070 8GB.

$ErrorActionPreference = "Stop"
# scripts/medrag → repo root
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
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

if (-not $env:MEDRAG_EMBED_DEVICE) { $env:MEDRAG_EMBED_DEVICE = "cuda" }
if (-not $env:MEDRAG_EMBED_HOST) { $env:MEDRAG_EMBED_HOST = "0.0.0.0" }
if (-not $env:VLLM_EMBED_PORT -and -not $env:MEDRAG_EMBED_PORT) { $env:VLLM_EMBED_PORT = "8001" }
# serve_openai_embed_windows.py resolves repo as parents[1] of scripts/medrag → scripts/
# Prefer MedicalRAG-style layout keys already in .env (VLLM_EMBED_* / MEDRAG_EMBED_*).

Write-Host "Starting Windows embed shim from aranmed: device=$($env:MEDRAG_EMBED_DEVICE)"
# Use MedicalRAG script if present on PATH machine; else local copy with PYTHONPATH=src
$local = Join-Path $Root "scripts\medrag\serve_openai_embed_windows.py"
$env:PYTHONPATH = (Join-Path $Root "src")
# Patch: script parents[1] is scripts/; load dotenv from repo root explicitly via env already imported
python -u $local
