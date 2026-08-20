<#
.SYNOPSIS
    One-shot bootstrap for the ASR-Agent project.

.DESCRIPTION
    Checks for Python >=3.10, Node.js >=18, and Ollama; installs any that are
    missing via winget (Windows) or brew/apt (macOS/Linux). Then creates the
    Python virtual env, installs backend deps, pulls the Ollama LLM, installs
    frontend deps, and starts both services as background jobs.

.EXAMPLE
    pwsh ./scripts/setup.ps1
    pwsh ./scripts/setup.ps1 -OllamaModel qwen3:8b -SkipFrontend
    pwsh ./scripts/setup.ps1 -NoStart
    # One local GGUF (no download needed):
    pwsh ./scripts/setup.ps1 -LocalGGUF "C:\path\to\Radiology-Infer-Mini-Q8_0.gguf"
    # Two GGUFs — first becomes the active model, both are imported:
    pwsh ./scripts/setup.ps1 -LocalGGUF "C:\path\to\Radiology-Infer-Mini-Q8_0.gguf","C:\path\to\Qwen3.5-9B-Q4_K_M.gguf"
    # Two GGUFs with custom names:
    pwsh ./scripts/setup.ps1 -LocalGGUF "C:\...\Radiology-Infer-Mini-Q8_0.gguf","C:\...\Qwen3.5-9B-Q4_K_M.gguf" -LocalModelName "radiology-mini","qwen3-5-9b"
#>

[CmdletBinding()]
param(
    [string]$OllamaModel       = "qwen2.5:7b",
    [string]$AsrModel          = "facebook/omniASR-LLM-7B",
    [int]   $BackendPort       = 8000,
    [int]   $FrontendPort      = 3000,
    # One or more local .gguf files to import into Ollama (comma-separated array).
    # The FIRST entry becomes the active model used by the backend.
    # Example (one):  -LocalGGUF "C:\path\to\Radiology-Infer-Mini-Q8_0.gguf"
    # Example (two):  -LocalGGUF "C:\...\Radiology-Infer-Mini-Q8_0.gguf","C:\...\Qwen3.5-9B-Q4_K_M.gguf"
    [string[]]$LocalGGUF       = @(),
    # Optional Ollama names, parallel to -LocalGGUF. Leave empty to auto-derive from filenames.
    # Example: -LocalModelName "radiology-mini","qwen3-5-9b"
    [string[]]$LocalModelName  = @(),
    [switch]$AutoSelect,
    [switch]$SkipModelDownload,
    [switch]$SkipFrontend,
    [switch]$NoStart,
    # Delete the HuggingFace on-disk cache for the ASR model (frees space).
    [switch]$ClearAsrCache
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "── ASR-Agent setup ──────────────────────────────" -ForegroundColor Cyan
Write-Host "Project root : $root"
Write-Host "ASR model    : $AsrModel"
Write-Host "Ollama model : $OllamaModel"
Write-Host ""

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
function Write-Step ([string]$msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-OK   ([string]$msg) { Write-Host "  v  $msg" -ForegroundColor Green }
function Write-Warn ([string]$msg) { Write-Host "  !  $msg" -ForegroundColor DarkYellow }
function Write-Fail ([string]$msg) { Write-Host "  X  $msg" -ForegroundColor Red }

# Run an executable quietly and return its stdout.
# NOTE: parameter is named $cmdArgs (NOT $args) to avoid clashing
# with PowerShell's built-in automatic $args variable.
function Invoke-Quietly {
    param([string]$exe, [string[]]$cmdArgs)
    try {
        $output = & $exe $cmdArgs 2>$null
        return $output
    } catch {
        return $null
    }
}

# ─────────────────────────────────────────────────────────────
# Find a Python 3.10+ executable
# Tries, in order:
#   1. "python"  on PATH  (Anaconda, official installer with PATH checked, etc.)
#   2. "python3" on PATH  (Linux / macOS)
#   3. Common Windows install directories
# Skips the Microsoft Store stub (WindowsApps\python.exe).
# ─────────────────────────────────────────────────────────────
function Find-Python {
    $candidates = [System.Collections.Generic.List[string]]::new()

    # 1 & 2 — bare names on PATH
    foreach ($name in @("python", "python3")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        if ($found.Source -like "*WindowsApps*") { continue }   # Store stub — skip

        $ver = Invoke-Quietly $found.Source @("--version")
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
            $candidates.Add($found.Source)
        }
    }

    # 3 — common Windows paths (official installer without PATH, conda, etc.)
    $searchRoots = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:USERPROFILE\anaconda3",
        "$env:USERPROFILE\miniconda3",
        "$env:ProgramData\anaconda3",
        "$env:ProgramData\miniconda3",
        "C:\Python310", "C:\Python311", "C:\Python312", "C:\Python313"
    )
    foreach ($dir in $searchRoots) {
        $exe = Join-Path $dir "python.exe"
        if (-not (Test-Path $exe)) { continue }
        $ver = Invoke-Quietly $exe @("--version")
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
            $candidates.Add($exe)
        }
    }

    if ($candidates.Count -gt 0) { return $candidates[0] }
    return $null
}

# ─────────────────────────────────────────────────────────────
# Install a package via winget (Windows only)
# ─────────────────────────────────────────────────────────────
function Install-Winget {
    param([string]$id, [string]$displayName)
    Write-Warn "$displayName not found — installing via winget..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Fail "winget is not available. Please install $displayName manually."
        exit 1
    }
    winget install --id $id --source winget `
        --accept-package-agreements --accept-source-agreements --silent
    # Refresh PATH so newly installed tools are visible in this session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ─────────────────────────────────────────────────────────────
# [0/5]  Prerequisite checks & auto-install
# ─────────────────────────────────────────────────────────────
Write-Step "[0/5] Checking prerequisites"

# ── Python ──────────────────────────────────────────────────
$pythonExe = Find-Python

if ($pythonExe) {
    $pyVer = Invoke-Quietly $pythonExe @("--version")
    Write-OK "Python : $pyVer  ($pythonExe)"
} else {
    if ($IsWindows) {
        Install-Winget "Python.Python.3.12" "Python 3.12"
        $pythonExe = Find-Python
    } elseif ($IsMacOS) {
        if (Get-Command brew -ErrorAction SilentlyContinue) { brew install python@3.12 }
        $pythonExe = Find-Python
    } else {
        # NB: no `&&` here — it is a PowerShell 7+ operator and Windows
        # PowerShell 5.1 fails to PARSE the whole file when it appears, even
        # though this Linux-only branch never runs there.
        sudo apt-get update -qq
        if ($?) { sudo apt-get install -y python3.12 python3.12-venv }
        $pythonExe = Find-Python
    }
    if (-not $pythonExe) {
        Write-Fail "Python 3.10+ not found after install attempt."
        Write-Fail "Install from https://www.python.org/downloads/ and check 'Add Python to PATH'."
        exit 1
    }
    Write-OK "Python : $(Invoke-Quietly $pythonExe @('--version'))  ($pythonExe)"
}

# ── Node.js ─────────────────────────────────────────────────
if (-not $SkipFrontend) {
    $nodeExe = Get-Command node -ErrorAction SilentlyContinue
    $nodeMajor = 0
    if ($nodeExe) {
        $nodeVer = Invoke-Quietly $nodeExe.Source @("--version")
        if ($nodeVer -match "v(\d+)") { $nodeMajor = [int]$Matches[1] }
    }

    if ($nodeMajor -ge 18) {
        Write-OK "Node.js: $nodeVer"
    } else {
        if ($IsWindows) {
            Install-Winget "OpenJS.NodeJS.LTS" "Node.js LTS"
        } elseif ($IsMacOS) {
            brew install node
        } else {
            curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
            sudo apt-get install -y nodejs
        }
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
            Write-Fail "Node.js install failed. Install from https://nodejs.org."
            exit 1
        }
        Write-OK "Node.js: $(node --version)"
    }
}

# ── Ollama ───────────────────────────────────────────────────
$ollamaOk = [bool](Get-Command ollama -ErrorAction SilentlyContinue)
if ($ollamaOk) {
    Write-OK "Ollama : $(Invoke-Quietly ollama @('--version'))"
} else {
    if ($IsWindows) {
        Install-Winget "Ollama.Ollama" "Ollama"
    } elseif ($IsMacOS) {
        brew install ollama
    } else {
        curl -fsSL https://ollama.com/install.sh | sh
    }
    $ollamaOk = [bool](Get-Command ollama -ErrorAction SilentlyContinue)
    if ($ollamaOk) { Write-OK "Ollama installed." }
    else            { Write-Warn "Ollama not installed — LLM step will be skipped." }
}

Write-Host ""

# ─────────────────────────────────────────────────────────────
# [1/5]  Python venv
# ─────────────────────────────────────────────────────────────
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) {
    Write-Step "[1/5] Creating venv at $venv"
    & $pythonExe -m venv $venv
} else {
    Write-Host "[1/5] venv already present — reusing." -ForegroundColor DarkGray
}

$py  = if ($IsWindows) { Join-Path $venv "Scripts\python.exe" } `
                  else { Join-Path $venv "bin/python" }
$pip = if ($IsWindows) { Join-Path $venv "Scripts\pip.exe"    } `
                  else { Join-Path $venv "bin/pip" }

if (-not (Test-Path $py)) {
    Write-Fail "Venv python not found at $py — venv creation failed."
    exit 1
}

# ─────────────────────────────────────────────────────────────
# [2/5]  Backend requirements
# ─────────────────────────────────────────────────────────────
Write-Step "[2/5] Installing backend requirements"
& $py -m pip install --upgrade pip wheel | Out-Host
& $pip install -r (Join-Path $root "backend/requirements.txt") | Out-Host

# ─────────────────────────────────────────────────────────────
# [3/5]  Model selection
# ─────────────────────────────────────────────────────────────
if ($LocalGGUF.Count -gt 0) {
    # Local GGUFs supplied — skip the interactive selector entirely.
    # models.yaml will be written after Ollama import in step [4/5].
    Write-Host "[3/5] Skipping model selector (local GGUFs will set models.yaml)" -ForegroundColor DarkGray
} elseif (-not $SkipModelDownload) {
    Write-Step "[3/5] Scanning for available models"
    $selArgs = if ($AutoSelect) { @("--auto") } else { @() }
    & $py (Join-Path $root "scripts/select_models.py") @selArgs
} else {
    Write-Host "[3/5] Skipping model selection (-SkipModelDownload)" -ForegroundColor DarkGray
}

# After select_models.py runs it writes backend/models.yaml.
# Read the chosen core model from there so [4/5] pulls the right one
# instead of blindly using the -OllamaModel default.
$modelsYaml = Join-Path $root "backend/models.yaml"
$pullModel  = $OllamaModel   # fallback: whatever was passed on the CLI

if (Test-Path $modelsYaml) {
    # Parse the "core" line — yaml looks like:  core: "qwen3:14b  [ollama]"
    $coreLine = Get-Content $modelsYaml | Where-Object { $_ -match "^\s*core\s*:" }
    if ($coreLine -match "core\s*:\s*[`"']?([^\s`"'\[]+)") {
        $candidate = $Matches[1].Trim()
        # Only use it for ollama pull when the backend is ollama (not huggingface/omniasr)
        $backendTag = ""
        if ($coreLine -match "\[([^\]]+)\]") { $backendTag = $Matches[1].Trim() }
        if ($backendTag -eq "ollama" -and $candidate -ne "") {
            $pullModel = $candidate
        }
    }
}

# ─────────────────────────────────────────────────────────────
# [4/5]  Ollama — import local GGUF  OR  pull from registry
# ─────────────────────────────────────────────────────────────
if ($ollamaOk) {
    # Start daemon if not already running
    try {
        Invoke-WebRequest "http://127.0.0.1:11434" -UseBasicParsing -TimeoutSec 2 -EA Stop | Out-Null
    } catch {
        Write-Host "  Starting Ollama daemon..."
        Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 3
    }

    if ($LocalGGUF.Count -gt 0) {
        # ── Local GGUF import(s) ─────────────────────────────
        Write-Step "[4/5] Importing $($LocalGGUF.Count) local GGUF(s) into Ollama"

        $modelfileDir = Join-Path $root "backend/data"
        New-Item -ItemType Directory -Force -Path $modelfileDir | Out-Null

        # Helper: derive clean Ollama name from a GGUF path
        # "Qwen3.5-9B-Q4_K_M.gguf" -> "qwen3.5-9b"
        function Get-GgufName([string]$path) {
            $stem = [System.IO.Path]::GetFileNameWithoutExtension($path)
            $stem = $stem -replace "-[QFIqfi]\d[\w]*$", ""   # strip quant suffix
            return $stem.ToLower()
        }

        for ($i = 0; $i -lt $LocalGGUF.Count; $i++) {
            $ggufPath = $LocalGGUF[$i].Trim()

            if (-not (Test-Path $ggufPath)) {
                Write-Fail "GGUF file not found: $ggufPath"
                exit 1
            }

            # Use supplied name if available, otherwise auto-derive
            $modelName = if ($i -lt $LocalModelName.Count -and $LocalModelName[$i] -ne "") {
                $LocalModelName[$i].Trim()
            } else {
                Get-GgufName $ggufPath
            }

            Write-Host "  [$($i+1)/$($LocalGGUF.Count)] $modelName"
            Write-Host "       <- $ggufPath"

            # Write per-model Modelfile (Ollama needs forward slashes on Windows)
            $ggufForward   = $ggufPath -replace "\\", "/"
            $modelfilePath = Join-Path $modelfileDir "Modelfile_$modelName"
            Set-Content -Path $modelfilePath -Value "FROM $ggufForward"

            ollama create $modelName -f $modelfilePath
            Write-OK "Imported '$modelName'"

            # First GGUF becomes the active model used by the backend
            if ($i -eq 0) { $pullModel = $modelName }
        }

        Write-Host ""
        Write-OK "Active model set to: $pullModel"
        Write-Host "  (all imported models are available via 'ollama list')"

        # Write models.yaml so the backend picks up the imported models.
        $modelsYamlPath = Join-Path $root "backend/models.yaml"
        $yamlLines = @("# written by setup.ps1 — local GGUF import")
        $yamlLines += "core: $pullModel [ollama]"
        # Additional GGUFs are noted as comments for reference
        for ($j = 1; $j -lt $LocalGGUF.Count; $j++) {
            $extraName = if ($j -lt $LocalModelName.Count -and $LocalModelName[$j] -ne "") {
                $LocalModelName[$j].Trim()
            } else {
                $extraStem = [System.IO.Path]::GetFileNameWithoutExtension($LocalGGUF[$j])
                ($extraStem -replace "-[QFIqfi]\d[\w]*$", "").ToLower()
            }
            $yamlLines += "# also available: $extraName [ollama]"
        }
        $yamlLines | Set-Content -Path $modelsYamlPath
        Write-OK "Wrote backend/models.yaml  (core: $pullModel)"

    } else {
        # ── Normal registry pull ─────────────────────────────
        Write-Step "[4/5] Pulling Ollama model: $pullModel"
        ollama pull $pullModel
    }
} else {
    Write-Host "[4/5] Skipping Ollama step (not installed)" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────
# ASR cache cleanup
# ─────────────────────────────────────────────────────────────
if ($ClearAsrCache) {
    Write-Step "Clearing ASR HuggingFace cache"

    # HF stores models under  <cache_root>/hub/models--<org>--<name>
    # Possible cache roots (in priority order)
    $hfCacheRoots = @(
        $env:HF_HOME,
        (Join-Path $env:USERPROFILE ".cache/huggingface/hub"),
        (Join-Path $env:LOCALAPPDATA   "huggingface/hub")
    ) | Where-Object { $_ -and (Test-Path $_) }

    # Read the current ASR model id from models.yaml (or fall back to $AsrModel)
    $asrModelId = $AsrModel
    $modelsYamlPath = Join-Path $root "backend/models.yaml"
    if (Test-Path $modelsYamlPath) {
        $asrLine = Get-Content $modelsYamlPath | Where-Object { $_ -match "^\s*asr\s*:" }
        if ($asrLine -match "asr\s*:\s*[`"']?([^\s`"'\[]+)") { $asrModelId = $Matches[1].Trim() }
    }

    # "facebook/omniASR-LLM-300M" -> "models--facebook--omniASR-LLM-300M"
    $folderName = "models--" + ($asrModelId -replace "/", "--")

    $deleted = $false
    foreach ($root_ in $hfCacheRoots) {
        $target = Join-Path $root_ $folderName
        if (Test-Path $target) {
            Write-Host "  Removing $target"
            Remove-Item -Recurse -Force $target
            $deleted = $true
            break
        }
    }

    if ($deleted) { Write-OK "ASR cache cleared ($asrModelId)" }
    else          { Write-Warn "ASR cache folder not found — nothing to delete" }
}

# ─────────────────────────────────────────────────────────────
# [5/5]  Frontend deps
# ─────────────────────────────────────────────────────────────
if (-not $SkipFrontend) {
    Write-Step "[5/5] Installing frontend deps"
    Push-Location (Join-Path $root "frontend")
    & npm install | Out-Host
    Pop-Location
} else {
    Write-Host "[5/5] Skipping frontend (-SkipFrontend)" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────
# Write .env if missing
# ─────────────────────────────────────────────────────────────
$envFile    = Join-Path $root "backend/.env"
$envExample = Join-Path $root "backend/.env.example"
if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    (Get-Content $envFile) `
        -replace "^OLLAMA_MODEL=.*", "OLLAMA_MODEL=$pullModel" `
        -replace "^ASR_MODEL_ID=.*", "ASR_MODEL_ID=$AsrModel" |
        Set-Content $envFile
    Write-OK "Wrote backend/.env"
}

# ─────────────────────────────────────────────────────────────
# NoStart — stop here
# ─────────────────────────────────────────────────────────────
if ($NoStart) {
    Write-Host "`nSetup complete. Start services manually:" -ForegroundColor Green
    Write-Host "  $py backend/app.py"
    if (-not $SkipFrontend) { Write-Host "  npm --prefix frontend run dev" }
    return
}

# ─────────────────────────────────────────────────────────────
# Launch services as background jobs
# ─────────────────────────────────────────────────────────────
Write-Host "`nLaunching services..." -ForegroundColor Cyan

$backendJob = Start-Job -Name asr-backend -ScriptBlock {
    param($py, $root, $port)
    Set-Location (Join-Path $root "backend")
    $env:PORT = $port
    & $py app.py
} -ArgumentList $py, $root, $BackendPort

if (-not $SkipFrontend) {
    $frontendJob = Start-Job -Name asr-frontend -ScriptBlock {
        param($root, $port, $backendPort)
        Set-Location (Join-Path $root "frontend")
        $env:BACKEND_URL = "http://127.0.0.1:$backendPort"
        & npm run dev -- -p $port
    } -ArgumentList $root, $FrontendPort, $BackendPort
}

Write-Host ""
Write-OK "Backend  → http://127.0.0.1:$BackendPort  (job id $($backendJob.Id))"
if (-not $SkipFrontend) {
    Write-OK "Frontend → http://127.0.0.1:$FrontendPort (job id $($frontendJob.Id))"
}
Write-Host ""
Write-Host "View logs : Receive-Job <id> -Keep"
Write-Host "Stop all  : Get-Job | Stop-Job; Get-Job | Remove-Job"