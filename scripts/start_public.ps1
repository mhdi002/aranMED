# Start backend + frontend + Cloudflare quick tunnel (public share link).
param(
    [switch]$NamedTunnel,
    [switch]$SkipTunnel,
    [int]$BackendPort = 8010
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$VenvPy = Join-Path (Join-Path $Root ".venv") "Scripts/python.exe"
$BackendUrl = "http://127.0.0.1:$BackendPort"
$CloudflareCfg = Join-Path (Join-Path $Root "cloudflare") "config.yml"

function Test-PythonHasTorch([string]$pythonExe) {
    if (-not (Test-Path $pythonExe)) { return $false }
    & $pythonExe -c "import torch" 2>$null | Out-Null
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
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )
    if ($env:CONDA_PREFIX) {
        $candidates = @((Join-Path $env:CONDA_PREFIX "python.exe")) + $candidates
    }

    foreach ($py in $candidates) {
        if (Test-PythonHasTorch $py) {
            return (Resolve-Path $py).Path
        }
    }

    if (Test-PythonHasTorch $VenvPy) {
        return (Resolve-Path $VenvPy).Path
    }

    return $null
}

function Get-ListenerProcessId([int]$port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    return $null
}

function Stop-BackendOnPort([int]$port) {
    $procId = Get-ListenerProcessId $port
    if ($procId) {
        Write-Host "Stopping process on port $port (pid $procId)..." -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

function Test-Port($port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $port)
        $c.Close()
        return $true
    } catch { return $false }
}

function Test-AsrBackend($baseUrl) {
    try {
        $resp = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 5
        return ($null -ne $resp.asr_model) -or ($null -ne $resp.ollama_model)
    } catch {
        return $false
    }
}

function Get-CloudflaredExe {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "cloudflared\cloudflared.exe"),
        (Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe")
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return (Resolve-Path $path).Path }
    }
    return $null
}

function Wait-FrontendReady([int]$timeoutSec = 90) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $page = Invoke-WebRequest -Uri "http://127.0.0.1:3000/" -TimeoutSec 8 -UseBasicParsing
            if ($page.StatusCode -ge 200 -and $page.StatusCode -lt 400) {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:3000/api/health" -TimeoutSec 8
                if ($null -ne $health.asr_model) { return $true }
            }
        } catch {
            # Next.js dev server may still be compiling on first boot.
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Start-Frontend($backendUrl) {
    $env:BACKEND_URL = $backendUrl
    $env:HOST = "0.0.0.0"
    $env:PORT = "3000"
    Start-Process -FilePath "cmd.exe" -ArgumentList @(
        "/c", "set BACKEND_URL=$backendUrl&& set HOST=0.0.0.0&& set PORT=3000&& npm run dev"
    ) -WorkingDirectory $Frontend -WindowStyle Minimized
    Write-Host "Waiting for frontend (Next.js compile can take up to 90s)..." -ForegroundColor DarkGray
    if (-not (Wait-FrontendReady 90)) {
        Write-Host "Frontend did not become ready on :3000" -ForegroundColor Red
        exit 1
    }
}

Write-Host "=== ASR-Agent public stack ===" -ForegroundColor Cyan

$BackendPy = Get-BackendPython
if (-not $BackendPy) {
    Write-Host "No Python with PyTorch found for Whisper ASR." -ForegroundColor Red
    Write-Host "Install torch in conda base, or set ASR_BACKEND_PYTHON to a Python with torch."
    exit 1
}
Write-Host "Backend Python: $BackendPy" -ForegroundColor DarkGray

if (-not (Test-Path $VenvPy)) {
    Write-Host "Note: .venv missing (frontend only); backend uses conda/system Python." -ForegroundColor Yellow
}

if ((Test-Port 8000) -and -not (Test-AsrBackend "http://127.0.0.1:8000")) {
    Write-Host "Port 8000 is used by another app; ASR backend will use :$BackendPort" -ForegroundColor Yellow
}

if (-not (Test-AsrBackend $BackendUrl)) {
    if (Test-Port $BackendPort) {
        Write-Host "Port $BackendPort is busy but health check failed - restarting backend..." -ForegroundColor Yellow
        Stop-BackendOnPort $BackendPort
    }
    Write-Host "Starting ASR backend on $BackendUrl ..."
    Start-Process -FilePath $BackendPy -ArgumentList @(
        "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "$BackendPort"
    ) -WorkingDirectory $Backend -WindowStyle Minimized
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (Test-AsrBackend $BackendUrl) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-AsrBackend $BackendUrl)) {
        Write-Host "ASR backend failed to start on $BackendUrl" -ForegroundColor Red
        exit 1
    }
} else {
    $listenerPid = Get-ListenerProcessId $BackendPort
    if ($listenerPid) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -and ($cmd -like "*\.venv\*") -and ($BackendPy -notlike "*\.venv\*")) {
            Write-Host "Backend on :$BackendPort uses .venv (no torch) - restarting with conda..." -ForegroundColor Yellow
            Stop-BackendOnPort $BackendPort
            Start-Process -FilePath $BackendPy -ArgumentList @(
                "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "$BackendPort"
            ) -WorkingDirectory $Backend -WindowStyle Minimized
            $deadline = (Get-Date).AddSeconds(20)
            while ((Get-Date) -lt $deadline) {
                if (Test-AsrBackend $BackendUrl) { break }
                Start-Sleep -Seconds 1
            }
        }
    }
    if (Test-AsrBackend $BackendUrl) {
        Write-Host "ASR backend already running at $BackendUrl"
    } else {
        Write-Host "ASR backend failed to restart on $BackendUrl" -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Port 3000)) {
    Write-Host "Starting frontend on http://0.0.0.0:3000 ..."
    Start-Frontend $BackendUrl
} else {
    $feOk = $false
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:3000/api/health" -TimeoutSec 8
        $feOk = ($null -ne $h.asr_model)
    } catch { $feOk = $false }
    if (-not $feOk) {
        Write-Host "Frontend on :3000 is not proxying to ASR backend - restarting..." -ForegroundColor Yellow
        Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
        Start-Frontend $BackendUrl
    } else {
        Write-Host "Frontend already listening on :3000 (proxied to ASR backend)"
    }
}

if (-not (Wait-FrontendReady 15)) {
    Write-Host "Frontend health check failed before tunnel start." -ForegroundColor Red
    Write-Host "Try: http://127.0.0.1:3000 locally first." -ForegroundColor Yellow
    exit 1
}

if ($SkipTunnel) {
    Write-Host ""
    Write-Host "Local URLs:" -ForegroundColor Green
    Write-Host "  App:     http://127.0.0.1:3000"
    Write-Host "  API:     $BackendUrl/api/health"
    exit 0
}

$CloudflaredExe = Get-CloudflaredExe
if (-not $CloudflaredExe) {
    Write-Host ""
    Write-Host "cloudflared not found. Install it:" -ForegroundColor Yellow
    Write-Host "  winget install Cloudflare.cloudflared"
    Write-Host ""
    Write-Host "App is running locally at http://127.0.0.1:3000"
    exit 1
}
Write-Host "cloudflared: $CloudflaredExe" -ForegroundColor DarkGray

Write-Host ""
Write-Host "Starting Cloudflare tunnel..." -ForegroundColor Cyan
Write-Host "Share the NEW https://*.trycloudflare.com link (old links die when this window closes)." -ForegroundColor Green
Write-Host "Keep this PowerShell window open - closing it stops the public URL." -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop the tunnel."
Write-Host ""

if ($NamedTunnel) {
    if (-not (Test-Path $CloudflareCfg)) {
        Write-Host "Create cloudflare/config.yml from config.yml.example first." -ForegroundColor Red
        exit 1
    }
    & $CloudflaredExe tunnel --config $CloudflareCfg run
} else {
    & $CloudflaredExe tunnel --url http://127.0.0.1:3000
}
