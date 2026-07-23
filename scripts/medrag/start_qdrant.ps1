# Start Qdrant server on :6333 (Windows native binary — no Docker required).
# Prefer: docker compose up -d   when Docker Desktop is installed.
#
# Storage resolution (first match wins):
#   1) MEDRAG_QDRANT_STORAGE env
#   2) MEDRAG_DATA_DIR/qdrant_storage  (single data dir deploy)
#   3) <repo>/qdrant_storage           (current live default — leave alone while running)
#
# Local archive folders (qdrant_data, qdrant_expand*_data, qdrant_standards_data)
# belong under data/qdrant_archives/ — they are NOT used in server mode.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BinDir = Join-Path $Root "bin"

if ($env:MEDRAG_QDRANT_STORAGE -and $env:MEDRAG_QDRANT_STORAGE.Trim() -ne "") {
    $Storage = $env:MEDRAG_QDRANT_STORAGE.Trim()
} elseif ($env:MEDRAG_DATA_DIR -and $env:MEDRAG_DATA_DIR.Trim() -ne "") {
    $Storage = Join-Path $env:MEDRAG_DATA_DIR.Trim() "qdrant_storage"
} else {
    $Storage = Join-Path $Root "qdrant_storage"
}

$Exe = Join-Path $BinDir "qdrant.exe"

New-Item -ItemType Directory -Force -Path $BinDir, $Storage | Out-Null

# Already healthy?
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:6333/collections" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -eq 200) {
        Write-Host "Qdrant already running on :6333 (leave existing storage untouched)"
        exit 0
    }
} catch {
    # continue to start
}

if (-not (Test-Path $Exe)) {
    Write-Host "Downloading Qdrant v1.13.2..."
    $Zip = Join-Path $env:TEMP "qdrant.zip"
    Invoke-WebRequest -Uri "https://github.com/qdrant/qdrant/releases/download/v1.13.2/qdrant-x86_64-pc-windows-msvc.zip" -OutFile $Zip
    Expand-Archive -Path $Zip -DestinationPath $BinDir -Force
    Remove-Item $Zip
}

Write-Host "Starting Qdrant on :6333 (storage: $Storage)"
$env:QDRANT__STORAGE__STORAGE_PATH = $Storage
Start-Process -FilePath $Exe -WorkingDirectory $BinDir -WindowStyle Hidden
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:6333/collections" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
}
if (-not $ok) {
    Write-Error "Qdrant failed to become healthy on :6333"
    exit 1
}
Write-Host "Qdrant healthy: http://localhost:6333"
Write-Host "Storage path: $Storage"
