# Post-reboot: finish WSL2 + Ubuntu, then install/serve real vLLM for MedicalRAG.
# Usage (repo root):
#   powershell -File scripts/install_wsl_and_vllm.ps1 -Serve llm
#   powershell -File scripts/install_wsl_and_vllm.ps1 -Serve both
#   powershell -File scripts/install_wsl_and_vllm.ps1 -Serve none -SkipVerify

param(
    [ValidateSet("none", "llm", "embed", "both")]
    [string]$Serve = "llm",
    [switch]$SkipVerify,
    [string]$Distro = "Ubuntu-24.04"
)

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

Write-Host "==> Checking WSL..."
$wslOk = $false
try {
    $ver = & wsl.exe --version 2>&1 | Out-String
    if ($ver -match "WSL version") { $wslOk = $true; Write-Host $ver }
} catch {}

if (-not $wslOk) {
    Write-Host "WSL store package missing - trying local predownload, then winget..."
    $pre = Join-Path $env:TEMP "wsl-predownload"
    $localWsl = Get-ChildItem -Path $pre -Filter "Microsoft.WSL*.msixbundle" -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 1
    if ($localWsl) {
        Write-Host "Installing $($localWsl.FullName)"
        try { Add-AppxPackage -Path $localWsl.FullName } catch { Write-Warning "$_" }
    } else {
        winget install --id Microsoft.WSL -e --accept-package-agreements --accept-source-agreements
    }
    Write-Host "If this is the first install, reboot again, then re-run this script."
}

$listed = (& wsl.exe -l -v 2>&1 | Out-String)
Write-Host $listed
if ($listed -notmatch "Ubuntu") {
    $pre = Join-Path $env:TEMP "wsl-predownload"
    $localMsix = Get-ChildItem -Path $pre -Filter "*.msix*" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "Ubuntu" } |
        Sort-Object Length -Descending |
        Select-Object -First 1
    if ($localMsix) {
        Write-Host "==> Installing pre-downloaded Ubuntu package: $($localMsix.FullName)"
        try {
            Add-AppxPackage -Path $localMsix.FullName
        } catch {
            Write-Warning "Add-AppxPackage failed: $_ - falling back to winget"
            winget install --id Canonical.Ubuntu.2404 -e --accept-package-agreements --accept-source-agreements
        }
    } else {
        Write-Host "==> Installing Ubuntu via winget (Canonical.Ubuntu.2404)..."
        winget install --id Canonical.Ubuntu.2404 -e --accept-package-agreements --accept-source-agreements
    }
    & wsl.exe -d $Distro -u root -- bash -lc "id" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $Distro = "Ubuntu"
        & wsl.exe -d $Distro -u root -- bash -lc "echo distro_ok"
    }
}

try { & wsl.exe --set-default-version 2 } catch {}
try { & wsl.exe --set-default $Distro } catch {}

$dotenvWin = Join-Path $Root ".env"
$dotenvWsl = (& wsl.exe wslpath -a $dotenvWin 2>$null)
if (-not $dotenvWsl) {
    $dotenvWsl = "/mnt/c/" + ($dotenvWin.Substring(3) -replace "\\", "/")
}

$setupWin = Join-Path $Root "scripts\wsl\setup_vllm_wsl.sh"
$setupWsl = (& wsl.exe wslpath -a $setupWin 2>$null)
if (-not $setupWsl) {
    $setupWsl = "/mnt/c/" + ($setupWin.Substring(3) -replace "\\", "/")
}

Write-Host "==> Running WSL setup (venv + vLLM + prefetch). Serve=$Serve"
& wsl.exe -d $Distro -- bash -lc "sed -i 's/\r`$//' '$setupWsl' && bash '$setupWsl' --dotenv '$dotenvWsl' --serve '$Serve'"
if ($LASTEXITCODE -ne 0) {
    throw "WSL vLLM setup failed (exit $LASTEXITCODE). Check nvidia-smi inside WSL and logs in ~/.medrag-vllm-logs/"
}

if ($SkipVerify) {
    Write-Host "SkipVerify set - done."
    exit 0
}

Write-Host "==> Waiting for OpenAI endpoints..."
function Wait-Http([string]$Url, [int]$TimeoutSec = 600) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) { return $true }
        } catch {}
        Start-Sleep -Seconds 5
    }
    return $false
}

$llmPort = if ($env:VLLM_PORT) { $env:VLLM_PORT } else { "8000" }
$embedPort = if ($env:VLLM_EMBED_PORT) { $env:VLLM_EMBED_PORT } else { "8001" }

if ($Serve -in @("llm", "both")) {
    if (-not (Wait-Http "http://127.0.0.1:$llmPort/v1/models" 900)) {
        Write-Warning "LLM :$llmPort not healthy yet - see WSL ~/.medrag-vllm-logs/llm.log"
    }
}
if ($Serve -in @("embed", "both")) {
    if (-not (Wait-Http "http://127.0.0.1:$embedPort/v1/models" 900)) {
        Write-Warning "Embed :$embedPort not healthy - on 8GB prefer sequential or CPU embed shim."
    }
}

Write-Host "==> Running verification..."
$env:PYTHONPATH = "src"
python -u scripts/verify_vllm_wsl.py
exit $LASTEXITCODE
