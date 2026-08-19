# ASR-Agent — server deployment
# Usage:
#   make deploy            # full automated deployment (see deploy.sh) — start here
#   make install           # venv, deps, models, verify (bare-metal)
#   make run               # start backend + frontend (bare-metal)
#   make stop              # stop services (bare-metal)
#   make verify            # pre-flight checks
#   make docker-install    # Docker build + model cache
#   make docker-run        # docker compose up -d

SHELL := /bin/bash
ROOT := $(CURDIR)
VENV := $(ROOT)/.venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PORT ?= 8010
FRONTEND_PORT ?= 3000
# cu128 is required for PyTorch 2.7+ sm_120 (Blackwell / RTX 50-series) kernels.
# Override for older cards, e.g. TORCH_INDEX=https://download.pytorch.org/whl/cu124
TORCH_INDEX ?= https://download.pytorch.org/whl/cu128
OLLAMA_MODEL ?=

.PHONY: deploy install run stop verify clean docker-install docker-run docker-stop docker-logs help

help:
	@echo "Targets:"
	@echo "  make deploy          Full automated deployment: docker, images, models, triton, vllm, up"
	@echo "                       (pass flags via ARGS, e.g. make deploy ARGS=\"--with-ollama --with-vllm\")"
	@echo "  make install         Install Python/Node deps + download models + verify (bare-metal)"
	@echo "  make run             Start backend (:$(PORT)) and frontend (:$(FRONTEND_PORT)) (bare-metal)"
	@echo "  make stop            Stop backend and frontend (bare-metal)"
	@echo "  make verify          Run deployment checks (add VERIFY_FULL=1 for GPU load test)"
	@echo "  make docker-install  Build images and cache Whisper in volume"
	@echo "  make docker-run      Start stack with Docker Compose"
	@echo "  make docker-stop     Stop Docker Compose stack"

# ── Full automated deployment (Docker: backend, frontend, triton, medrag, ──
# ── qdrant, gateway, + optional ollama/vllm/vllm-embed, + model downloads) ──
deploy:
	@chmod +x deploy.sh
	./deploy.sh $(ARGS)

# ── Bare-metal (upload files to GPU server, then install + run) ─────────────

install: venv deps frontend-deps frontend-build models verify

venv:
	@test -d $(VENV) || $(shell command -v python3) -m venv $(VENV)
	$(PIP) install --upgrade pip wheel setuptools

deps: venv
	$(PIP) install torch torchaudio --index-url $(TORCH_INDEX)
	$(PIP) install -r backend/requirements.txt

frontend-deps:
	cd frontend && (npm ci 2>/dev/null || npm install)

frontend-build: frontend-deps
	cd frontend && npm run build

models: venv
	@mkdir -p $(ROOT)/.run
	PYTHONPATH=backend $(PY) scripts/install_models.py \
		$(if $(OLLAMA_MODEL),--ollama-model $(OLLAMA_MODEL),)

verify: venv
	PYTHONPATH=backend $(PY) scripts/verify_deploy.py \
		$(if $(filter 1 true yes,$(VERIFY_FULL)),--full,)

run:
	@chmod +x scripts/run_stack.sh
	PORT=$(PORT) FRONTEND_PORT=$(FRONTEND_PORT) ./scripts/run_stack.sh

stop:
	@chmod +x scripts/stop_stack.sh
	./scripts/stop_stack.sh

clean:
	rm -rf $(VENV) frontend/.next frontend/node_modules .run
	@echo "Cleaned venv, node_modules, build artifacts."

# ── Docker Compose (GPU server with nvidia-container-toolkit) ───────────────

docker-install:
	docker compose build
	docker compose up -d ollama
	@echo "Waiting for Ollama..."
	@sleep 5
	docker compose exec ollama ollama pull $${OLLAMA_MODEL:-qwen2.5:7b} || true
	docker compose run --rm backend python /app/scripts/install_models.py --skip-ollama
	@echo "Done. Run: make docker-run"

docker-run:
	docker compose up -d
	@echo "App: http://localhost:3000  API: http://localhost:$(PORT)/api/health"

docker-stop:
	docker compose down

docker-logs:
	docker compose logs -f backend frontend
