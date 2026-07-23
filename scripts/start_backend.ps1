# Start ASR backend on port 8010 using a Python that has PyTorch (required for Whisper).
param(
    [int]$Port = 8010
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$VenvPy = Join-Path (Join-Path $Root ".venv") "Scripts\python.exe"

function Test-PythonHasTorch([string]$pythonExe) {
    if (-not (Test-Path $pythonExe)) { return $false }
    try {
        & $pythonExe -c "import torch" *>$null
    } catch {}
    return $LASTEXITCODE -eq 0
}

function Get-BackendPython {
    if ($env:ASR_BACKEND_PYTHON -and (Test-Path $env:ASR_BACKEND_PYTHON)) {
        if (Test-PythonHasTorch $env:ASR_BACKEND_PYTHON) {
            return (Resolve-Path $env:ASR_BACKEND_PYTHON).Path
        }
        Write-Host "ASR_BACKEND_PYTHON is set but torch is missing: $env:ASR_BACKEND_PYTHON" -ForegroundColor Red
        return $null
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE "anaconda3\python.exe"),
        (Join-Path $env:USERPROFILE "miniconda3\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        $VenvPy
    )
    if ($env:CONDA_PREFIX) {
        $candidates = @((Join-Path $env:CONDA_PREFIX "python.exe")) + $candidates
    }

    foreach ($py in $candidates) {
        if (Test-PythonHasTorch $py) {
            return (Resolve-Path $py).Path
        }
    }
    return $null
}

$BackendPy = Get-BackendPython
if (-not $BackendPy) {
    Write-Host "No Python with PyTorch found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Option A - use conda (recommended on Windows):" -ForegroundColor Yellow
    Write-Host "  conda activate base"
    Write-Host "  cd backend"
    Write-Host "  python -m uvicorn app:app --host 0.0.0.0 --port $Port"
    Write-Host ""
    Write-Host "Option B - install torch into .venv:" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128"
    Write-Host "  .\.venv\Scripts\pip install -r backend\requirements.txt"
    exit 1
}

Write-Host "Backend Python: $BackendPy" -ForegroundColor Cyan
Write-Host "API: http://127.0.0.1:$Port/api/health" -ForegroundColor Green
Write-Host ""

Set-Location $Backend
& $BackendPy -m uvicorn app:app --host 0.0.0.0 --port $Port
