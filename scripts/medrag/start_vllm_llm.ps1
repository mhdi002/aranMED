# Start vLLM LLM server using env from .env / process environment.
# Requires Linux or Docker (native Windows pip vllm lacks vllm._C).
# Usage (from repo root):  pwsh -File scripts/start_vllm_llm.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
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

$model = $env:VLLM_MODEL
if (-not $model) { $model = $env:MEDRAG_LLM_MODEL }
$port = $env:VLLM_PORT
$hostAddr = $env:VLLM_HOST
$maxLen = $env:VLLM_MAX_MODEL_LEN
$mem = $env:VLLM_GPU_MEM_UTIL

if (-not $model) { throw "Set VLLM_MODEL or MEDRAG_LLM_MODEL in .env" }
if (-not $port) { throw "Set VLLM_PORT in .env" }
if (-not $hostAddr) { throw "Set VLLM_HOST in .env" }
if (-not $maxLen) { throw "Set VLLM_MAX_MODEL_LEN in .env" }
if (-not $mem) { throw "Set VLLM_GPU_MEM_UTIL in .env" }

Write-Host "Starting vLLM LLM: model=$model host=$hostAddr port=$port max_model_len=$maxLen gpu_mem=$mem"
& vllm serve $model --host $hostAddr --port $port --max-model-len $maxLen --gpu-memory-utilization $mem
