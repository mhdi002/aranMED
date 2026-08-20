<#
.SYNOPSIS
  AranMed — one-command full-stack deployment (Windows / Docker Desktop).

.DESCRIPTION
  Builds every image (backend, frontend, medrag, triton, gateway — each
  installs its own deps at build time, no manual pip/npm), downloads the
  required models (Whisper into the shared hf-cache volume, Ollama LLM if
  requested), and brings the whole stack up under Docker Compose.

.PARAMETER WithOllama
  Also run Ollama in-compose (else point OLLAMA_HOST at a host Ollama).

.PARAMETER WithVllm
  Also run the GPU generation server (OpenAI-compatible).

.PARAMETER WithVllmEmbed
  Also run the GPU embedding server.

.PARAMETER SkipModels
  Skip the Whisper/Ollama model prefetch step.

.PARAMETER NoUp
  Build + prefetch only, don't start containers.

.PARAMETER InstallDocker
  Offer to install Docker Desktop via winget if it's missing.

.PARAMETER Yes
  Don't pause for confirmations.

.EXAMPLE
  .\deploy.ps1
.EXAMPLE
  .\deploy.ps1 -WithOllama -WithVllm -WithVllmEmbed
#>
[CmdletBinding()]
param(
  [switch]$WithOllama,
  [switch]$WithVllm,
  [switch]$WithVllmEmbed,
  [switch]$SkipModels,
  [switch]$NoUp,
  [switch]$InstallDocker,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Say($msg)  { Write-Host "══ $msg ══" -ForegroundColor Cyan }
function Step($n, $msg) { Write-Host "[$n] $msg" -ForegroundColor Yellow }
function Ok($msg)   { Write-Host "    v $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Red }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Confirm($prompt) {
  if ($Yes) { return $true }
  $reply = Read-Host "$prompt [y/N]"
  return $reply -match '^[Yy]$'
}

Say "AranMed - full-stack deployment"
Write-Host "  Project root : $Root"

# ── 1) Docker + Compose v2 ───────────────────────────────────────────────────
Step "1/6" "Checking Docker"
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
  Warn "Docker not found."
  if ($InstallDocker -and (Confirm "Install Docker Desktop now via winget?")) {
    winget install --id Docker.DockerDesktop -e
    Warn "Docker Desktop was installed — launch it, finish its first-run setup (WSL2 backend + GPU support), then re-run this script."
    exit 0
  } else {
    Die "Install Docker Desktop (https://www.docker.com/products/docker-desktop/) then re-run, or pass -InstallDocker."
  }
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker Compose v2 plugin not found - update Docker Desktop." }
$dockerVer = (docker --version)
Ok "$dockerVer"

docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker daemon isn't reachable (is Docker Desktop running?)." }

try {
  $gpu = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1
  if ($gpu) { Ok "GPU detected: $gpu" } else { throw }
} catch {
  Warn "nvidia-smi not found - backend/triton/vllm need GPU passthrough (Docker Desktop > Settings > Resources > WSL Integration, with an NVIDIA driver on Windows). They will fail to start without it."
}

# ── 2) .env ───────────────────────────────────────────────────────────────
Step "2/6" "Environment file"
$envPath = Join-Path $Root ".env"
if (-not (Test-Path $envPath)) {
  Copy-Item (Join-Path $Root ".env.example") $envPath
  Ok "Wrote .env from .env.example - review MEDRAG_QDRANT_STORAGE/MEDRAG_CORPUS_DIR (your knowledge corpus), HF_TOKEN, and OLLAMA_MODEL before continuing."
} else {
  Write-Host "    .env already present - leaving as-is." -ForegroundColor DarkGray
}

$envVars = @{}
$seenKeys = @{}
$dupeKeys = New-Object System.Collections.Generic.List[string]
if (Test-Path $envPath) {
  $lineNo = 0
  Get-Content $envPath | ForEach-Object {
    $lineNo++
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
      $k = $Matches[1]
      if ($seenKeys.ContainsKey($k)) {
        if (-not $dupeKeys.Contains($k)) { $dupeKeys.Add($k) }
        $seenKeys[$k] += ",$lineNo"
      } else {
        $seenKeys[$k] = "$lineNo"
      }
      $envVars[$k] = $Matches[2]
    }
  }
}

# A key defined twice in .env silently resolves to one of the two values, and
# the loser is usually the one the operator meant. This bites hardest on path
# keys - a duplicate MEDRAG_QDRANT_STORAGE can mount an empty local directory
# instead of the real knowledge corpus, and the stack comes up "healthy"
# serving zero chunks. Warn loudly rather than guessing.
if ($dupeKeys.Count -gt 0) {
  Warn "Duplicate keys in .env - the later definition wins, which may not be what you intended:"
  foreach ($k in $dupeKeys) { Write-Host "        $k  (lines $($seenKeys[$k]))" }
  Warn "Comment out the stale definition(s) and re-run, or continue if this is intentional."
}

# Corpus sanity: a bind-mounted Qdrant path that doesn't exist (or is empty)
# means the stack will start and report healthy while serving no knowledge.
$qdrantPath = if ($envVars.ContainsKey("MEDRAG_QDRANT_STORAGE")) { $envVars["MEDRAG_QDRANT_STORAGE"] } else { "./qdrant_storage" }
if (-not (Test-Path $qdrantPath)) {
  Warn "MEDRAG_QDRANT_STORAGE points at '$qdrantPath', which does not exist - MedicalRAG will serve 0 chunks."
} elseif (-not (Get-ChildItem (Join-Path $qdrantPath "collections") -ErrorAction SilentlyContinue)) {
  Warn "MEDRAG_QDRANT_STORAGE ('$qdrantPath') has no collections - MedicalRAG will serve 0 chunks."
}

$profileArgs = @()
if ($WithOllama)    { $profileArgs += @("--profile", "ollama") }
if ($WithVllm)      { $profileArgs += @("--profile", "vllm") }
if ($WithVllmEmbed) { $profileArgs += @("--profile", "vllm-embed") }

# ── 3) Build images (backend, frontend, medrag, triton - deps installed in-image) ──
Step "3/6" "Building images (backend + frontend + medrag + triton; each installs its own deps)"
docker compose @profileArgs build
if ($LASTEXITCODE -ne 0) { Die "docker compose build failed." }
Ok "Images built"

# ── 4) Model downloads ──────────────────────────────────────────────────────
if (-not $SkipModels) {
  Step "4/6" "Downloading models"
  Write-Host "  Whisper large-v3 -> shared hf-cache volume (used by backend + triton)"
  docker compose run --rm backend python /app/scripts/install_models.py --skip-ollama
  if ($LASTEXITCODE -ne 0) { Warn "Whisper prefetch failed - it will lazily download on first request instead." }

  $ollamaModel = if ($envVars.ContainsKey("OLLAMA_MODEL")) { $envVars["OLLAMA_MODEL"] } else { "qwen3.5-9b:latest" }
  $hasLocalModelfile = (Test-Path (Join-Path $Root "Modelfile_qwen")) -or (Test-Path (Join-Path $Root "Modelfile"))

  function Test-OllamaHasModel($listOutput) {
    return ($listOutput -match [regex]::Escape($ollamaModel))
  }

  if ($WithOllama) {
    Write-Host "  Ollama model '$ollamaModel' -> in-compose ollama container"
    docker compose --profile ollama up -d ollama
    for ($i = 0; $i -lt 30; $i++) {
      docker compose exec -T ollama ollama list *> $null
      if ($LASTEXITCODE -eq 0) { break }
      Start-Sleep -Seconds 2
    }
    $listOut = docker compose exec -T ollama ollama list 2>$null
    if (Test-OllamaHasModel $listOut) {
      Ok "Ollama already has '$ollamaModel' - skipping pull"
    } else {
      docker compose exec -T ollama ollama pull $ollamaModel
      if ($LASTEXITCODE -ne 0) {
        Warn "Ollama pull of '$ollamaModel' failed - it isn't on the public registry."
        if ($hasLocalModelfile) {
          Warn "This repo ships a local Modelfile - build it instead, e.g.: docker compose exec ollama ollama create $($ollamaModel.Split(':')[0]) -f /Modelfile_qwen (mount the Modelfile + GGUF into the container first)."
        }
      }
    }
  } elseif (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Host "  Ollama model '$ollamaModel' -> host Ollama"
    $listOut = & ollama list 2>$null
    if (Test-OllamaHasModel $listOut) {
      Ok "Ollama already has '$ollamaModel' - skipping pull"
    } else {
      & ollama pull $ollamaModel
      if ($LASTEXITCODE -ne 0) {
        Warn "Host 'ollama pull $ollamaModel' failed - it isn't on the public registry."
        if ($hasLocalModelfile) {
          Warn "This repo ships a local Modelfile - build it instead: ollama create $($ollamaModel.Split(':')[0]) -f Modelfile_qwen (edit its FROM path to your GGUF first)."
        }
      }
    }
  } else {
    Warn "No -WithOllama and no host 'ollama' CLI found. Install Ollama (https://ollama.com), then either 'ollama pull $ollamaModel' or build the repo's local Modelfile."
  }

  if ($WithVllm -or $WithVllmEmbed) {
    Write-Host "  vLLM/vllm-embed models download automatically from Hugging Face on first container start (cached in hf-cache volume)."
    if (-not $envVars.ContainsKey("HF_TOKEN") -or [string]::IsNullOrWhiteSpace($envVars["HF_TOKEN"])) {
      Warn "HF_TOKEN is unset in .env - gated HF models (if any) will fail to download."
    }
  }
  Ok "Model downloads complete"
} else {
  Write-Host "[4/6] Skipping model downloads (-SkipModels)" -ForegroundColor DarkGray
}

# ── 5) Bring the stack up ───────────────────────────────────────────────────
if ($NoUp) {
  Say "Build + model prefetch complete (-NoUp). Start later with:"
  Write-Host "  docker compose $($profileArgs -join ' ') up -d"
  exit 0
}

Step "5/6" "Starting the stack"
docker compose @profileArgs up -d
if ($LASTEXITCODE -ne 0) { Die "docker compose up failed." }
Ok "Containers started"

# nginx resolves upstream container IPs once at its own startup. If gateway
# itself wasn't recreated but backend/medrag/etc. were (e.g. a re-run of this
# script after a code change), it's left pointing at dead IPs -> 502s despite
# every backend service reporting healthy. Force it to re-resolve.
docker compose restart gateway *> $null

# ── 6) Wait for health ──────────────────────────────────────────────────────
Step "6/6" "Waiting for services to become healthy"
$gatewayPort = if ($envVars.ContainsKey("GATEWAY_PUBLISH_PORT")) { $envVars["GATEWAY_PUBLISH_PORT"] } else { "8090" }
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
  try {
    $resp = Invoke-WebRequest -Uri "http://localhost:$gatewayPort/api/health" -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) { $healthy = $true; break }
  } catch {}
  Start-Sleep -Seconds 5
}

Write-Host ""
docker compose ps
Write-Host ""
if ($healthy) {
  Say "AranMed is up"
  Write-Host "  App  -> http://localhost:$gatewayPort"
  Write-Host "  API  -> http://localhost:$gatewayPort/api/health"
} else {
  Warn "Gateway didn't answer /api/health within 5 minutes - check: docker compose logs -f"
}
Write-Host "  Logs -> docker compose logs -f backend frontend triton medrag"
Write-Host "  Stop -> docker compose down"
