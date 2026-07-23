# Start Next.js frontend on port 3000 (proxies /api to backend on 8010).
param(
    [int]$Port = 3000,
    [int]$BackendPort = 8010
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $Root "frontend"
$BackendUrl = "http://127.0.0.1:$BackendPort"

$env:BACKEND_URL = $BackendUrl
$env:HOST = "0.0.0.0"
$env:PORT = "$Port"

Write-Host "Frontend: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "API proxy -> $BackendUrl" -ForegroundColor Cyan
Write-Host ""

Set-Location $Frontend
npm run dev
